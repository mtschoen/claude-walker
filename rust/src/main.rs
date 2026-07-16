// Native pace-walker -- Rust implementation.
// See ../SPEC.md for the contract every implementation must honor.

use chrono::DateTime;
use rayon::prelude::*;
use std::fs::File;
use std::io::{BufRead, BufReader, Write};
use std::path::PathBuf;
use std::time::{Instant, SystemTime, UNIX_EPOCH};

use crate::transcript::{cost_for, discover_groups, Entry};

mod beacons;
mod content;
mod events;
mod search;
mod transcript;
mod walker_roots;

const SUBCOMMANDS: [&str; 5] = [
    "cost",
    "beacons-latest",
    "beacons-history",
    "search",
    "events",
];

const HELP: &str = r#"claude-walker - fast cost & progress walker over Claude Code transcripts

USAGE:
    claude-walker [SUBCOMMAND] [OPTIONS]

With no subcommand it runs `cost` (back-compat for the status line).

SUBCOMMANDS:
    cost              Trailing + window USD over the transcript fleet (default)
    search <pattern>  Cross-root/-machine content search over transcripts
    events            One NDJSON line per assistant turn (ts, usd, model, session)
    beacons-latest    Most recent <progress-beacon> for a session
    beacons-history   Calibration bias_factor over begin/end beacon pairs

COST OPTIONS (default mode):
    --period <seconds>            Required. Trailing-window length.
    --win-start <unix>            Required. Cost-window start (unix epoch).
    --projects-root <path>        Transcript root (default: ~/.claude/projects).
    --extra-projects-root <path>  Additional root; repeatable.
    --no-config                   Skip ~/.claude/walker-roots.json extras.
    --now <unix>                  Pin "now" (default: wall clock; for tests).

GLOBAL:
    -h, --help     Show this help.
    --version      Print <lang>/<version>.

Full contract: SPEC.md in the source tree.
"#;

fn is_help_flag(arg: &str) -> bool {
    arg == "-h" || arg == "--help"
}

// Help is shown when: no args, or the first arg is -h/--help, or the first
// arg is a known subcommand followed by -h/--help. See SPEC.md "Help & usage".
fn wants_help(raw: &[String]) -> bool {
    match raw.first().map(|s| s.as_str()) {
        None => true,
        Some(first) if is_help_flag(first) => true,
        Some(first) if SUBCOMMANDS.contains(&first) => {
            raw.get(1).map(|s| is_help_flag(s)).unwrap_or(false)
        }
        _ => false,
    }
}

fn usage_pointer() {
    eprintln!("Run 'claude-walker --help' for usage.");
}

#[derive(Default)]
struct Args {
    period_seconds: u64,
    win_start_unix: f64,
    now_unix: Option<f64>,
    projects_root: Option<PathBuf>,
    extra_projects_roots: Vec<PathBuf>,
    read_config: bool,
}

fn parse_cost_args(args: &[String]) -> Result<Args, String> {
    let mut result = Args {
        read_config: true,
        ..Args::default()
    };
    let mut iter = args.iter();
    let mut win_start_set = false;
    while let Some(flag) = iter.next() {
        match flag.as_str() {
            "--period" => {
                result.period_seconds = iter
                    .next()
                    .ok_or("--period needs a value")?
                    .parse()
                    .map_err(|e| format!("--period: {e}"))?
            }
            "--win-start" => {
                result.win_start_unix = iter
                    .next()
                    .ok_or("--win-start needs a value")?
                    .parse()
                    .map_err(|e| format!("--win-start: {e}"))?;
                win_start_set = true;
            }
            "--now" => {
                result.now_unix = Some(
                    iter.next()
                        .ok_or("--now needs a value")?
                        .parse()
                        .map_err(|e| format!("--now: {e}"))?,
                )
            }
            "--projects-root" => {
                result.projects_root = Some(PathBuf::from(
                    iter.next().ok_or("--projects-root needs a value")?,
                ))
            }
            "--extra-projects-root" => {
                result.extra_projects_roots.push(PathBuf::from(
                    iter.next().ok_or("--extra-projects-root needs a value")?,
                ));
            }
            "--no-config" => {
                result.read_config = false;
            }
            "--version" => {
                println!("rust/{}", env!("CARGO_PKG_VERSION"));
                std::process::exit(0);
            }
            _ => return Err(format!("unknown flag: {flag}")),
        }
    }
    if result.period_seconds == 0 {
        return Err("--period is required".into());
    }
    if !win_start_set {
        return Err("--win-start is required".into());
    }
    Ok(result)
}

pub(crate) fn parse_iso8601(ts: &str) -> Option<f64> {
    // chrono tolerates a space as the date/time separator (RFC 3339 note);
    // go/zig require the literal 'T', so enforce it here for parity.
    if ts.as_bytes().get(10) != Some(&b'T') {
        return None;
    }
    // chrono accepts the "Z" suffix natively (RFC 3339 defines it as +00:00),
    // so no normalization pass (and no per-call allocation) is needed.
    DateTime::parse_from_rfc3339(ts)
        .ok()
        .map(|dt| dt.timestamp() as f64 + dt.timestamp_subsec_nanos() as f64 / 1e9)
}

struct GroupResult {
    trailing: f64,
    window: f64,
}

fn walk_group(paths: &[PathBuf], period_cutoff: f64, win_start_unix: f64) -> GroupResult {
    let earliest = period_cutoff.min(win_start_unix);
    let mut trailing = 0.0;
    let mut window = 0.0;
    let mut seen_ids: std::collections::HashSet<String> = Default::default();
    // One line buffer reused across all lines and files: BufReader::lines()
    // allocates a fresh String per line, which dominates allocator traffic on
    // a multi-hundred-MB fleet. read_line() retains capacity across clear().
    let mut line = String::with_capacity(8 * 1024);
    for path in paths {
        let file = match File::open(path) {
            Ok(f) => f,
            Err(_) => continue,
        };
        let mut reader = BufReader::new(file);
        loop {
            line.clear();
            match reader.read_line(&mut line) {
                Ok(0) => break,
                Ok(_) => {}
                // Same semantics as lines(): an invalid-UTF-8 line is consumed
                // and skipped, iteration continues with the next line.
                Err(_) => continue,
            }
            let trimmed = line.trim();
            if trimmed.is_empty() {
                continue;
            }
            let entry: Entry = match serde_json::from_str(trimmed) {
                Ok(e) => e,
                Err(_) => continue,
            };
            let msg = match entry.message {
                Some(m) => m,
                None => continue,
            };
            if msg.role.as_deref() != Some("assistant") {
                continue;
            }
            if let Some(ref mid) = msg.id {
                if !seen_ids.insert(mid.clone()) {
                    continue;
                }
            }
            let ts_str = match entry.timestamp {
                Some(s) if !s.is_empty() => s,
                _ => continue,
            };
            let ts = match parse_iso8601(&ts_str) {
                Some(t) => t,
                None => continue,
            };
            if ts < earliest {
                continue;
            }
            let usage = msg.usage.unwrap_or_default();
            let model = msg.model.unwrap_or_default();
            let c = cost_for(&usage, &model, &ts_str);
            if ts >= period_cutoff {
                trailing += c;
            }
            if ts >= win_start_unix {
                window += c;
            }
        }
    }
    GroupResult { trailing, window }
}

pub(crate) fn default_projects_root() -> PathBuf {
    match walker_roots::home_directory() {
        Some(home) => PathBuf::from(home).join(".claude").join("projects"),
        None => PathBuf::from(".claude/projects"),
    }
}

pub(crate) fn current_unix() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

enum Subcommand {
    Cost,
    BeaconsLatest,
    BeaconsHistory,
    Search,
    Events,
}

fn main() {
    let raw: Vec<String> = std::env::args().skip(1).collect();
    if wants_help(&raw) {
        print!("{HELP}");
        std::process::exit(0);
    }
    let first = raw.first().map(|s| s.as_str());
    let (subcommand, rest): (Subcommand, &[String]) = match first {
        Some("cost") => (Subcommand::Cost, &raw[1..]),
        Some("beacons-latest") => (Subcommand::BeaconsLatest, &raw[1..]),
        Some("beacons-history") => (Subcommand::BeaconsHistory, &raw[1..]),
        Some("search") => (Subcommand::Search, &raw[1..]),
        Some("events") => (Subcommand::Events, &raw[1..]),
        Some(s) if !s.starts_with('-') => {
            eprintln!("walker: unknown subcommand: {}", s);
            usage_pointer();
            std::process::exit(2);
        }
        // Bare-flag invocation = cost mode (back-compat). wants_help()
        // already exited for the no-args case, so None folds harmlessly
        // into this arm.
        _ => (Subcommand::Cost, &raw[..]),
    };

    match subcommand {
        Subcommand::Cost => run_cost(rest),
        Subcommand::BeaconsLatest => beacons::run_latest(rest),
        Subcommand::BeaconsHistory => beacons::run_history(rest),
        Subcommand::Search => search::run(rest),
        Subcommand::Events => std::process::exit(events::run(rest)),
    }
}

fn run_cost(args: &[String]) {
    let started = Instant::now();
    let parsed = match parse_cost_args(args) {
        Ok(a) => a,
        Err(e) => {
            eprintln!("walker: {e}");
            usage_pointer();
            std::process::exit(2);
        }
    };

    let now_unix = parsed.now_unix.unwrap_or_else(current_unix);
    let period_cutoff = now_unix - parsed.period_seconds as f64;
    let earliest = period_cutoff.min(parsed.win_start_unix);
    let primary = parsed.projects_root.unwrap_or_else(default_projects_root);
    let roots =
        walker_roots::resolve_roots(primary, &parsed.extra_projects_roots, parsed.read_config);

    let groups = discover_groups(&roots, earliest);
    let total_files: usize = groups.values().map(|v| v.len()).sum();
    let total_groups = groups.len();

    let group_paths: Vec<Vec<PathBuf>> = groups.into_values().collect();

    // Cap pool so the small-corpus case doesn't pay startup tax for nothing.
    let workers = std::cmp::min(
        8,
        std::thread::available_parallelism()
            .map(|n| n.get())
            .unwrap_or(4),
    );
    let pool = rayon::ThreadPoolBuilder::new()
        .num_threads(workers)
        .build()
        .expect("rayon pool");

    let (trailing, window) = pool
        .install(|| {
            group_paths
                .par_iter()
                .map(|paths| walk_group(paths, period_cutoff, parsed.win_start_unix))
                .reduce(
                    || GroupResult {
                        trailing: 0.0,
                        window: 0.0,
                    },
                    |a, b| GroupResult {
                        trailing: a.trailing + b.trailing,
                        window: a.window + b.window,
                    },
                )
        })
        .into();

    let elapsed_ms = started.elapsed().as_millis() as u64;
    let stdout = std::io::stdout();
    let mut out = stdout.lock();
    writeln!(
        out,
        "{{\"trailing_usd\":{:.6},\"window_usd\":{:.6},\"files_walked\":{},\"groups\":{},\"elapsed_ms\":{}}}",
        trailing, window, total_files, total_groups, elapsed_ms
    )
    .ok();
}

impl From<GroupResult> for (f64, f64) {
    fn from(g: GroupResult) -> Self {
        (g.trailing, g.window)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::sync::Mutex;

    static ENV_LOCK: Mutex<()> = Mutex::new(());

    fn with_home_env<F, R>(home: Option<&str>, up: Option<&str>, body: F) -> R
    where
        F: FnOnce() -> R,
    {
        let _guard = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let saved_home = std::env::var_os("HOME");
        let saved_up = std::env::var_os("USERPROFILE");
        match home {
            Some(v) => std::env::set_var("HOME", v),
            None => std::env::remove_var("HOME"),
        }
        match up {
            Some(v) => std::env::set_var("USERPROFILE", v),
            None => std::env::remove_var("USERPROFILE"),
        }
        let r = body();
        match saved_home {
            Some(v) => std::env::set_var("HOME", v),
            None => std::env::remove_var("HOME"),
        }
        match saved_up {
            Some(v) => std::env::set_var("USERPROFILE", v),
            None => std::env::remove_var("USERPROFILE"),
        }
        r
    }

    fn tempdir_path(suffix: &str) -> PathBuf {
        let mut p = std::env::temp_dir();
        let pid = std::process::id();
        let nanos = std::time::SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        p.push(format!("rust-main-test-{suffix}-{pid}-{nanos}"));
        fs::create_dir_all(&p).unwrap();
        p
    }

    fn s(x: &str) -> String {
        x.to_string()
    }

    #[test]
    fn wants_help_no_args() {
        let raw: Vec<String> = vec![];
        assert!(wants_help(&raw));
    }

    #[test]
    fn wants_help_h_flag() {
        assert!(wants_help(&[s("-h")]));
        assert!(wants_help(&[s("--help")]));
    }

    #[test]
    fn wants_help_subcommand_then_help() {
        assert!(wants_help(&[s("search"), s("-h")]));
        assert!(wants_help(&[s("cost"), s("--help")]));
    }

    #[test]
    fn wants_help_no_help_when_not_present() {
        assert!(!wants_help(&[s("--period"), s("60")]));
        assert!(!wants_help(&[s("search"), s("foo")]));
    }

    #[test]
    fn parse_cost_args_requires_period() {
        // Period defaults to 0; period==0 → required error.
        assert!(parse_cost_args(&[]).is_err());
    }

    #[test]
    fn parse_cost_args_flag_value_errors() {
        for flag in [
            "--period",
            "--win-start",
            "--now",
            "--projects-root",
            "--extra-projects-root",
        ] {
            assert!(
                parse_cost_args(&[s(flag)]).is_err(),
                "{} should error when value missing",
                flag
            );
        }
        assert!(parse_cost_args(&[s("--period"), s("notnum")]).is_err());
        assert!(parse_cost_args(&[s("--win-start"), s("nope"), s("--period"), s("60")]).is_err());
        assert!(parse_cost_args(&[s("--unknown")]).is_err());
    }

    #[test]
    fn parse_cost_args_accepts_all_flags() {
        let a = parse_cost_args(&[
            s("--period"),
            s("3600"),
            s("--win-start"),
            s("100.5"),
            s("--now"),
            s("200.0"),
            s("--projects-root"),
            s("/tmp/p"),
            s("--extra-projects-root"),
            s("/tmp/q"),
            s("--no-config"),
        ])
        .unwrap();
        assert_eq!(a.period_seconds, 3600);
        assert_eq!(a.win_start_unix, 100.5);
        assert_eq!(a.now_unix, Some(200.0));
        assert_eq!(a.projects_root, Some(PathBuf::from("/tmp/p")));
        assert_eq!(a.extra_projects_roots, vec![PathBuf::from("/tmp/q")]);
        assert!(!a.read_config);
    }

    #[test]
    fn parse_iso8601_valid_z_and_offset_and_fractional() {
        assert!(parse_iso8601("2025-01-01T00:00:00Z").unwrap() > 0.0);
        assert!(parse_iso8601("2025-01-01T00:00:00+05:30").unwrap() > 0.0);
        assert!(parse_iso8601("2025-01-01T00:00:00.123Z").unwrap() > 0.0);
        assert!(parse_iso8601("garbage").is_none());
    }

    #[test]
    fn current_unix_positive() {
        // It calls SystemTime::now(); just confirm it's a positive timestamp.
        assert!(current_unix() > 1_700_000_000.0);
    }

    #[test]
    fn default_projects_root_with_home() {
        with_home_env(Some("/tmp/myhome"), Some("/tmp/profile"), || {
            let p = default_projects_root();
            if cfg!(windows) {
                assert_eq!(
                    p,
                    PathBuf::from("/tmp/profile")
                        .join(".claude")
                        .join("projects")
                );
            } else {
                assert_eq!(
                    p,
                    PathBuf::from("/tmp/myhome")
                        .join(".claude")
                        .join("projects")
                );
            }
        });
    }

    #[test]
    fn default_projects_root_no_home_fallback() {
        // Covers main.rs:214 — neither HOME nor USERPROFILE set → relative path.
        with_home_env(None, None, || {
            let p = default_projects_root();
            assert_eq!(p, PathBuf::from(".claude/projects"));
        });
    }

    #[test]
    fn walk_group_open_error_returns_zero() {
        let r = walk_group(&[PathBuf::from("/no/such/file.jsonl")], 0.0, 0.0);
        assert_eq!(r.trailing, 0.0);
        assert_eq!(r.window, 0.0);
    }

    #[test]
    fn walk_group_skip_ladder() {
        // Exercise every skip branch + a valid turn.
        let dir = tempdir_path("walk-group");
        let path = dir.join("session.jsonl");
        let body = concat!(
            "\n",         // blank line
            "{garbage\n", // malformed JSON
            "{}\n",       // entry.message=None → continue
            r#"{"timestamp":"2025-01-01T00:00:00Z","message":{"role":"user"}}"#,
            "\n", // not assistant
            r#"{"timestamp":"2025-01-01T00:00:01Z","message":{"role":"assistant","id":"m1","model":"sonnet","usage":{"input_tokens":1000000,"output_tokens":1000000}}}"#,
            "\n", // valid
            r#"{"timestamp":"2025-01-01T00:00:02Z","message":{"role":"assistant","id":"m1","model":"sonnet"}}"#,
            "\n", // dup id
            r#"{"message":{"role":"assistant","id":"m2","model":"sonnet"}}"#,
            "\n", // no timestamp
            r#"{"timestamp":"garbage","message":{"role":"assistant","id":"m3","model":"sonnet"}}"#,
            "\n", // bad ts
            r#"{"timestamp":"1970-01-01T00:00:00Z","message":{"role":"assistant","id":"m4","model":"sonnet","usage":{"input_tokens":1000000}}}"#,
            "\n", // before earliest
        );
        fs::write(&path, body).unwrap();
        // period_cutoff far in the past, win_start_unix at 2025
        let win_start = parse_iso8601("2025-01-01T00:00:00.5Z").unwrap();
        let r = walk_group(&[path], 0.0, win_start);
        // earliest = min(period_cutoff=0, win_start) = 0, so 1970 entry passes
        // the earliest filter, BUT we set win_start to mid-2025, so:
        // trailing accrues whenever ts >= period_cutoff=0 (both 2025 + 1970 if usage),
        // window accrues only for ts >= win_start = mid 2025.
        // Only the 2025-01-01T00:00:01 turn has usage and id "m1" (unique).
        // 1970 turn has usage but no id collision; its ts < win_start → only trailing.
        assert!(r.trailing > 0.0);
        assert!(r.window > 0.0);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn group_result_from_into_tuple() {
        let g = GroupResult {
            trailing: 1.0,
            window: 2.0,
        };
        let t: (f64, f64) = g.into();
        assert_eq!(t, (1.0, 2.0));
    }
}
