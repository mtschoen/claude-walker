// events subcommand: emit one NDJSON record per accepted assistant turn.
// Reuses cost-mode's parse/dedup/filter/pricing verbatim; only aggregation
// differs (per-turn output instead of accumulated totals).
// See ../SPEC.md §events for the full contract.

use rayon::prelude::*;
use serde::Serialize;
use std::fs::File;
use std::io::{BufRead, BufReader, Write};
use std::path::PathBuf;

use crate::transcript::{cost_for, discover_groups, Entry};
use crate::{current_unix, default_projects_root, parse_iso8601, walker_roots};

// ── Output record ─────────────────────────────────────────────────────────────

/// One emitted line per accepted assistant turn. Field declaration order equals
/// serialization order (serde_json uses declaration order by default).
/// SPEC mandates: ts, usd, model, session_id, slug.
#[derive(Serialize)]
pub(crate) struct EventRecord {
    pub(crate) ts: f64,
    pub(crate) usd: f64,
    pub(crate) model: String,
    pub(crate) session_id: String,
    pub(crate) slug: String,
}

// ── Args ──────────────────────────────────────────────────────────────────────

pub(crate) struct EventsArgs {
    pub(crate) period_seconds: u64,
    /// Defaults to `now - period` when not supplied by the caller.
    pub(crate) win_start_unix: f64,
    pub(crate) now_unix: f64,
    pub(crate) projects_root: PathBuf,
    pub(crate) extra_projects_roots: Vec<PathBuf>,
    pub(crate) read_config: bool,
}

pub(crate) fn parse_events_args(raw: &[String]) -> Result<EventsArgs, String> {
    let mut period_seconds: u64 = 0;
    let mut win_start_raw: Option<f64> = None;
    let mut now_raw: Option<f64> = None;
    let mut projects_root: Option<PathBuf> = None;
    let mut extra_projects_roots: Vec<PathBuf> = Vec::new();
    let mut read_config = true;

    let mut iter = raw.iter();
    while let Some(flag) = iter.next() {
        match flag.as_str() {
            "--period" => {
                period_seconds = iter
                    .next()
                    .ok_or("--period needs a value")?
                    .parse()
                    .map_err(|e| format!("--period: {e}"))?;
            }
            "--win-start" => {
                win_start_raw = Some(
                    iter.next()
                        .ok_or("--win-start needs a value")?
                        .parse()
                        .map_err(|e| format!("--win-start: {e}"))?,
                );
            }
            "--now" => {
                now_raw = Some(
                    iter.next()
                        .ok_or("--now needs a value")?
                        .parse()
                        .map_err(|e| format!("--now: {e}"))?,
                );
            }
            "--projects-root" => {
                projects_root = Some(PathBuf::from(
                    iter.next().ok_or("--projects-root needs a value")?,
                ));
            }
            "--extra-projects-root" => {
                extra_projects_roots.push(PathBuf::from(
                    iter.next().ok_or("--extra-projects-root needs a value")?,
                ));
            }
            "--no-config" => {
                read_config = false;
            }
            "--version" => {
                println!("rust/{}", env!("CARGO_PKG_VERSION"));
                std::process::exit(0);
            }
            _ => return Err(format!("unknown flag: {flag}")),
        }
    }

    if period_seconds == 0 {
        return Err("--period is required".into());
    }

    let now_unix = now_raw.unwrap_or_else(current_unix);
    // When --win-start is omitted, default to now - period (simplifies
    // the predicate to ts >= now - period, per SPEC).
    let win_start_unix = win_start_raw.unwrap_or(now_unix - period_seconds as f64);

    Ok(EventsArgs {
        period_seconds,
        win_start_unix,
        now_unix,
        projects_root: projects_root.unwrap_or_else(default_projects_root),
        extra_projects_roots,
        read_config,
    })
}

// ── Per-group walker ──────────────────────────────────────────────────────────

/// Walk one (slug, session_id) group and collect EventRecords for every
/// accepted assistant turn. Dedup is per-group via a local seen_ids set,
/// exactly matching cost-mode's walk_group contract.
fn walk_group_events(
    paths: &[PathBuf],
    slug: &str,
    session_id: &str,
    cutoff: f64,
) -> Vec<EventRecord> {
    let mut records: Vec<EventRecord> = Vec::new();
    let mut seen_ids: std::collections::HashSet<String> = Default::default();

    // Reused line buffer; see walk_group in main.rs for the rationale.
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
            // Filter 1: assistant role only.
            if msg.role.as_deref() != Some("assistant") {
                continue;
            }
            // Filter 2: dedup by message.id within this group.
            if let Some(ref mid) = msg.id {
                if !seen_ids.insert(mid.clone()) {
                    continue;
                }
            }
            // Filter 3: timestamp must parse.
            let ts_str = match entry.timestamp {
                Some(s) if !s.is_empty() => s,
                _ => continue,
            };
            let ts = match parse_iso8601(&ts_str) {
                Some(t) => t,
                None => continue,
            };
            // Filter 4: window predicate — ts >= cutoff (= min(now-period, win_start)).
            if ts < cutoff {
                continue;
            }

            let usage = msg.usage.unwrap_or_default();
            let model = msg.model.unwrap_or_default().to_ascii_lowercase();
            let usd = cost_for(&usage, &model, &ts_str);

            records.push(EventRecord {
                ts,
                usd,
                model,
                session_id: session_id.to_string(),
                slug: slug.to_string(),
            });
        }
    }

    records
}

// ── Entry point ───────────────────────────────────────────────────────────────

/// Main entry point for the `events` subcommand. Returns 0 on success, 1 on
/// root-assembly error (mirrors cost-mode's error path).
pub(crate) fn run(raw: &[String]) -> i32 {
    let args = match parse_events_args(raw) {
        Ok(a) => a,
        Err(e) => {
            eprintln!("walker: events: {e}");
            std::process::exit(2);
        }
    };

    // Effective cutoff = min(now - period, win_start), per SPEC §events.
    let period_cutoff = args.now_unix - args.period_seconds as f64;
    let cutoff = period_cutoff.min(args.win_start_unix);

    let roots = walker_roots::resolve_roots(
        args.projects_root,
        &args.extra_projects_roots,
        args.read_config,
    );

    if roots.is_empty() {
        // Primary root doesn't exist — not a hard error, just nothing to walk.
        // Emit no records and exit 0, consistent with cost-mode's empty-fleet case.
        return 0;
    }

    // discover_groups applies the mtime prune using the same cutoff.
    let groups = discover_groups(&roots, cutoff);

    // Convert to an owned vec of (key, paths) so rayon can par_iter over it.
    let group_list: Vec<((String, String), Vec<PathBuf>)> = groups.into_iter().collect();

    // Cap pool matching cost-mode concurrency policy.
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

    // Parallel walk: collect all records from all groups, then serialize
    // serially. This matches search.rs's collect-then-emit pattern and avoids
    // interleaved partial writes across rayon threads.
    let mut all_records: Vec<EventRecord> = pool.install(|| {
        group_list
            .par_iter()
            .map(|((slug, session_id), paths)| walk_group_events(paths, slug, session_id, cutoff))
            .reduce(Vec::new, |mut acc, mut next| {
                if acc.is_empty() {
                    next
                } else {
                    acc.append(&mut next);
                    acc
                }
            })
    });

    // Sort for deterministic output: (ts, session_id, model) — matches the
    // multiset tiebreaker defined in SPEC §events §Ordering.
    all_records.sort_by(|a, b| {
        a.ts.partial_cmp(&b.ts)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| a.session_id.cmp(&b.session_id))
            .then_with(|| a.model.cmp(&b.model))
    });

    // Emit NDJSON — one line per record. Lock stdout once for the full write.
    let stdout = std::io::stdout();
    let mut out = stdout.lock();
    emit_records(&mut out, &all_records);

    0
}

/// Write one NDJSON line per record. serde_json::to_string can't fail on
/// EventRecord (no Map<K,V> keys, no serializers that error on the fixed
/// primitive/string fields), so we unwrap rather than carrying a dead error
/// branch. Broken-pipe writes are silently absorbed: `walker events | head`
/// is a normal usage pattern, not an error.
fn emit_records<W: Write>(out: &mut W, records: &[EventRecord]) {
    // Serialize every record into one buffer, then write it in a single call.
    // A StdoutLock is unbuffered, so the previous per-record writeln! issued one
    // syscall per line (233k+ for a weekly fleet), which dominated events-mode
    // wall time. to_writer appends into the Vec without a per-record allocation.
    let mut buf: Vec<u8> = Vec::with_capacity(records.len() * 128);
    for record in records {
        serde_json::to_writer(&mut buf, record).expect("EventRecord serializes");
        buf.push(b'\n');
    }
    // One write_all: a broken pipe (`walker events | head`) surfaces as a single
    // failed attempt, silently absorbed — matching the prior break-on-error.
    let _ = out.write_all(&buf);
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    fn tempdir_path(suffix: &str) -> PathBuf {
        let mut p = std::env::temp_dir();
        let pid = std::process::id();
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        p.push(format!("rust-events-test-{suffix}-{pid}-{nanos}"));
        fs::create_dir_all(&p).unwrap();
        p
    }

    fn s(x: &str) -> String {
        x.to_string()
    }

    #[test]
    fn parse_events_args_requires_period() {
        // Missing --period entirely.
        assert!(parse_events_args(&[]).is_err());
        // --period 0 is the unset sentinel → error.
        assert!(parse_events_args(&[s("--period"), s("0")]).is_err());
    }

    #[test]
    fn parse_events_args_period_needs_value() {
        assert!(parse_events_args(&[s("--period")]).is_err());
    }

    #[test]
    fn parse_events_args_period_non_numeric() {
        assert!(parse_events_args(&[s("--period"), s("notnum")]).is_err());
    }

    #[test]
    fn parse_events_args_flag_value_errors() {
        for flag in [
            "--win-start",
            "--now",
            "--projects-root",
            "--extra-projects-root",
        ] {
            assert!(
                parse_events_args(&[s("--period"), s("60"), s(flag)]).is_err(),
                "{} should error with no value",
                flag
            );
        }
    }

    #[test]
    fn parse_events_args_unknown_flag_errors() {
        assert!(parse_events_args(&[s("--period"), s("60"), s("--what")]).is_err());
    }

    #[test]
    fn parse_events_args_win_start_default_now_minus_period() {
        // Omit --win-start → defaults to now - period.
        let r = parse_events_args(&[s("--period"), s("100"), s("--now"), s("1000.0")]).unwrap();
        assert_eq!(r.period_seconds, 100);
        assert_eq!(r.now_unix, 1000.0);
        assert_eq!(r.win_start_unix, 900.0);
    }

    #[test]
    fn parse_events_args_no_config_disables_config() {
        let r = parse_events_args(&[s("--period"), s("60"), s("--now"), s("0"), s("--no-config")])
            .unwrap();
        assert!(!r.read_config);
    }

    #[test]
    fn parse_events_args_accepts_extras() {
        let r = parse_events_args(&[
            s("--period"),
            s("60"),
            s("--now"),
            s("0"),
            s("--projects-root"),
            s("/tmp/p"),
            s("--extra-projects-root"),
            s("/tmp/q"),
            s("--win-start"),
            s("50.0"),
        ])
        .unwrap();
        assert_eq!(r.projects_root, PathBuf::from("/tmp/p"));
        assert_eq!(r.extra_projects_roots, vec![PathBuf::from("/tmp/q")]);
        assert_eq!(r.win_start_unix, 50.0);
    }

    #[test]
    fn walk_group_events_missing_file_yields_no_records() {
        let recs = walk_group_events(&[PathBuf::from("/no/such/file.jsonl")], "slug", "sid", 0.0);
        assert!(recs.is_empty());
    }

    /// Writer that returns `BrokenPipe` on every write. Mimics `walker events
    /// | head` closing the pipe after the first record.
    struct BrokenPipeWriter {
        writes_attempted: usize,
    }

    impl std::io::Write for BrokenPipeWriter {
        fn write(&mut self, _buf: &[u8]) -> std::io::Result<usize> {
            self.writes_attempted += 1;
            Err(std::io::Error::from(std::io::ErrorKind::BrokenPipe))
        }
        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
        fn write_all(&mut self, _buf: &[u8]) -> std::io::Result<()> {
            self.writes_attempted += 1;
            Err(std::io::Error::from(std::io::ErrorKind::BrokenPipe))
        }
    }

    #[test]
    fn emit_records_absorbs_broken_pipe() {
        let records = vec![
            EventRecord {
                ts: 1.0,
                usd: 0.1,
                model: s("sonnet"),
                session_id: s("sid1"),
                slug: s("slug1"),
            },
            EventRecord {
                ts: 2.0,
                usd: 0.2,
                model: s("opus"),
                session_id: s("sid2"),
                slug: s("slug2"),
            },
            EventRecord {
                ts: 3.0,
                usd: 0.3,
                model: s("haiku"),
                session_id: s("sid3"),
                slug: s("slug3"),
            },
        ];
        let mut writer = BrokenPipeWriter {
            writes_attempted: 0,
        };
        // Must NOT panic, must NOT propagate the error.
        emit_records(&mut writer, &records);
        // First write fails → loop breaks → exactly one attempt despite 3 records.
        assert_eq!(writer.writes_attempted, 1);
    }

    #[test]
    fn emit_records_writes_all_to_healthy_sink() {
        let records = vec![
            EventRecord {
                ts: 1.0,
                usd: 0.5,
                model: s("sonnet"),
                session_id: s("sid"),
                slug: s("slug"),
            },
            EventRecord {
                ts: 2.0,
                usd: 0.6,
                model: s("opus"),
                session_id: s("sid"),
                slug: s("slug"),
            },
        ];
        let mut buf: Vec<u8> = Vec::new();
        emit_records(&mut buf, &records);
        let text = String::from_utf8(buf).unwrap();
        let lines: Vec<_> = text.lines().collect();
        assert_eq!(lines.len(), 2);
        // Verify the field order matches SPEC (ts, usd, model, session_id, slug).
        assert!(
            lines[0].starts_with(r#"{"ts":1.0,"usd":0.5,"model":"sonnet""#),
            "unexpected line 0: {}",
            lines[0]
        );
    }

    #[test]
    fn walk_group_events_filters_and_collects() {
        let dir = tempdir_path("walk");
        let path = dir.join("session.jsonl");
        let body = concat!(
            // blank
            "\n",
            // malformed
            "{bogus\n",
            // no message
            "{}\n",
            // not assistant — filtered
            r#"{"timestamp":"2025-01-01T00:00:00Z","message":{"role":"user","content":"hi"}}"#,
            "\n",
            // valid assistant turn
            r#"{"timestamp":"2025-01-01T00:00:01Z","message":{"role":"assistant","id":"m1","model":"claude-3-5-sonnet","usage":{"input_tokens":100,"output_tokens":50}}}"#,
            "\n",
            // duplicate id — dedup'd out
            r#"{"timestamp":"2025-01-01T00:00:02Z","message":{"role":"assistant","id":"m1","model":"claude-3-5-sonnet","usage":{"input_tokens":1}}}"#,
            "\n",
            // missing timestamp — skipped
            r#"{"message":{"role":"assistant","id":"m2","model":"sonnet"}}"#,
            "\n",
            // unparseable timestamp — skipped
            r#"{"timestamp":"garbage","message":{"role":"assistant","id":"m3","model":"sonnet"}}"#,
            "\n",
            // before cutoff — skipped
            r#"{"timestamp":"1970-01-01T00:00:00Z","message":{"role":"assistant","id":"m4","model":"sonnet"}}"#,
            "\n",
        );
        fs::write(&path, body).unwrap();
        let recs = walk_group_events(&[path], "slug-x", "sid-y", 100.0);
        assert_eq!(recs.len(), 1);
        assert_eq!(recs[0].slug, "slug-x");
        assert_eq!(recs[0].session_id, "sid-y");
        assert!(recs[0].usd > 0.0);
        let _ = fs::remove_dir_all(&dir);
    }
}
