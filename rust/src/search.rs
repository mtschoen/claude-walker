// Search subcommand: substring/regex match across transcript content.
// See ../SPEC.md (post-merge) or
// skills-dev/docs/superpowers/specs/claude-walker-search.md (pre-merge)
// for the CLI contract.

use rayon::prelude::*;
use regex::Regex;
use serde_json::{json, Value};
use std::collections::HashSet;
use std::fs::{read_dir, DirEntry};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::time::{Instant, UNIX_EPOCH};

use crate::content::{
    extract_codex_event, extract_queue_op_text, extract_text, is_only_tool_blocks,
};
use crate::walker_roots::{TranscriptFormat, TranscriptRoot};
use crate::{current_unix, parse_iso8601, walker_roots};

// === Flag types ===

#[derive(Clone, Copy, Debug, PartialEq)]
enum Role {
    User,
    Assistant,
    Both,
}

#[derive(Clone, Copy, Debug, PartialEq)]
enum Format {
    Pretty,
    Jsonl,
}

struct SearchArgs {
    pattern: String,
    regex: bool,
    case_sensitive: bool,
    role: Role,
    since: Option<f64>,
    until: Option<f64>,
    cwd: Option<String>,
    context: u32,
    limit: u32,
    count_only: bool,
    include_tool_blocks: bool,
    include_queue_ops: bool,
    format: Format,
    snippet_chars: u32,
    projects_root: Option<PathBuf>,
    extra_projects_roots: Vec<PathBuf>,
    read_config: bool,
}

// === Arg parsing ===

fn parse_args(raw: &[String]) -> Result<SearchArgs, String> {
    let mut pattern: Option<String> = None;
    let mut regex = false;
    let mut case_sensitive = false;
    let mut role = Role::Both;
    let mut since_raw: Option<String> = None;
    let mut until_raw: Option<String> = None;
    let mut cwd: Option<String> = None;
    let mut any_cwd_explicit = false;
    let mut context: u32 = 1;
    let mut limit: u32 = 50;
    let mut count_only = false;
    let mut include_tool_blocks = false;
    let mut include_queue_ops = false;
    let mut format = Format::Pretty;
    let mut snippet_chars: u32 = 240;
    let mut projects_root: Option<PathBuf> = None;
    let mut extra_projects_roots: Vec<PathBuf> = Vec::new();
    let mut read_config = true;
    let mut now_override: Option<f64> = None;

    let mut iter = raw.iter();
    while let Some(arg) = iter.next() {
        match arg.as_str() {
            "--regex" => regex = true,
            "--case-sensitive" => case_sensitive = true,
            "--role" => {
                let v = iter.next().ok_or("--role needs a value")?;
                role = match v.as_str() {
                    "user" => Role::User,
                    "assistant" => Role::Assistant,
                    "both" => Role::Both,
                    other => {
                        return Err(format!(
                            "--role: invalid value {other}; expected user|assistant|both"
                        ))
                    }
                };
            }
            "--since" => since_raw = Some(iter.next().ok_or("--since needs a value")?.clone()),
            "--until" => until_raw = Some(iter.next().ok_or("--until needs a value")?.clone()),
            "--cwd" => cwd = Some(iter.next().ok_or("--cwd needs a value")?.clone()),
            "--any-cwd" => any_cwd_explicit = true,
            "--context" => {
                context = iter
                    .next()
                    .ok_or("--context needs a value")?
                    .parse()
                    .map_err(|e| format!("--context: {e}"))?;
            }
            "--limit" => {
                limit = iter
                    .next()
                    .ok_or("--limit needs a value")?
                    .parse()
                    .map_err(|e| format!("--limit: {e}"))?;
            }
            "--count-only" => count_only = true,
            "--include-tool-blocks" => include_tool_blocks = true,
            "--include-queue-ops" => include_queue_ops = true,
            "--format" => {
                let v = iter.next().ok_or("--format needs a value")?;
                format = match v.as_str() {
                    "pretty" => Format::Pretty,
                    "jsonl" => Format::Jsonl,
                    other => {
                        return Err(format!(
                            "--format: invalid value {other}; expected pretty|jsonl"
                        ))
                    }
                };
            }
            "--snippet-chars" => {
                snippet_chars = iter
                    .next()
                    .ok_or("--snippet-chars needs a value")?
                    .parse()
                    .map_err(|e| format!("--snippet-chars: {e}"))?;
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
            "--now" => {
                now_override = Some(
                    iter.next()
                        .ok_or("--now needs a value")?
                        .parse()
                        .map_err(|e| format!("--now: {e}"))?,
                );
            }
            s if s.starts_with("--") => return Err(format!("unknown flag: {s}")),
            _ => {
                if pattern.is_some() {
                    return Err(format!("unexpected positional argument: {arg}"));
                }
                pattern = Some(arg.clone());
            }
        }
    }

    let pattern = pattern.ok_or("pattern must be non-empty")?;
    if pattern.is_empty() {
        return Err("pattern must be non-empty".into());
    }
    if cwd.is_some() && any_cwd_explicit {
        return Err("--cwd and --any-cwd are mutually exclusive".into());
    }

    let now = now_override.unwrap_or_else(current_unix);
    let since = match since_raw {
        Some(s) => {
            Some(parse_time_arg(&s, now).map_err(|e| format!("bad time: --since={s} ({e})"))?)
        }
        None => None,
    };
    let until = match until_raw {
        Some(s) => {
            Some(parse_time_arg(&s, now).map_err(|e| format!("bad time: --until={s} ({e})"))?)
        }
        None => None,
    };

    Ok(SearchArgs {
        pattern,
        regex,
        case_sensitive,
        role,
        since,
        until,
        cwd,
        context,
        limit,
        count_only,
        include_tool_blocks,
        include_queue_ops,
        format,
        snippet_chars,
        projects_root,
        extra_projects_roots,
        read_config,
    })
}

fn parse_time_arg(s: &str, now: f64) -> Result<f64, String> {
    let trimmed = s.trim();
    if trimmed.is_empty() {
        return Err("empty value".into());
    }
    // trimmed is non-empty (guarded above), so last() always yields a char.
    let last = trimmed.chars().next_back().expect("non-empty after guard");
    let multiplier = match last {
        'd' => Some(86_400.0_f64),
        'h' => Some(3_600.0),
        'm' => Some(60.0),
        's' => Some(1.0),
        _ => None,
    };
    if let Some(multiplier) = multiplier {
        let head = &trimmed[..trimmed.len() - last.len_utf8()];
        if !head.is_empty() && head.chars().all(|c| c.is_ascii_digit() || c == '.') {
            let n: f64 = head.parse().map_err(|e| format!("relative prefix: {e}"))?;
            return Ok(now - n * multiplier);
        }
    }
    parse_iso8601(trimmed).ok_or_else(|| format!("not RFC3339 or relative: {trimmed}"))
}

// === Scan / file IO ===

#[derive(Debug)]
struct ScanMessage {
    line_number: u32,
    timestamp: Option<f64>,
    timestamp_str: String,
    role: String,
    text_default: String,
    text_with_tools: String,
    is_only_tool_blocks: bool,
}

fn scan_file(
    path: &Path,
    transcript_format: TranscriptFormat,
    include_queue_ops: bool,
    include_tool_blocks: bool,
    prefilter: Option<&PreFilter>,
) -> Vec<ScanMessage> {
    let mut out: Vec<ScanMessage> = Vec::new();
    let data = match std::fs::read(path) {
        Ok(d) => d,
        Err(_) => return out,
    };
    // File-level literal pre-filter: when the pattern is a plain literal whose
    // bytes survive JSON string-escaping unchanged, a transcript whose raw
    // bytes never contain it cannot produce a hit; skip all JSON parsing.
    // Context turns are only emitted for files WITH hits, so whole-file
    // skipping cannot change any output.
    if let Some(pf) = prefilter {
        if !pf.contains(&data) {
            return out;
        }
    }
    for (idx, raw) in data.split(|&b| b == b'\n').enumerate() {
        // Mirrors the previous BufReader::lines() semantics: invalid-UTF-8
        // lines are skipped, surrounding lines keep their numbering.
        let line = match std::str::from_utf8(raw) {
            Ok(s) => s.trim(),
            Err(_) => continue,
        };
        if line.is_empty() {
            continue;
        }
        let entry: Value = match serde_json::from_str(line) {
            Ok(e) => e,
            Err(_) => continue,
        };
        if transcript_format == TranscriptFormat::Codex {
            if entry.get("type").and_then(Value::as_str) != Some("event_msg") {
                continue;
            }
            let Some(payload) = entry.get("payload") else {
                continue;
            };
            let Some((role, text)) = extract_codex_event(payload) else {
                continue;
            };
            let timestamp_str = entry
                .get("timestamp")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string();
            let timestamp = parse_iso8601(&timestamp_str);
            out.push(ScanMessage {
                line_number: (idx + 1) as u32,
                timestamp,
                timestamp_str,
                role: role.to_string(),
                text_default: text.clone(),
                text_with_tools: text,
                is_only_tool_blocks: false,
            });
            continue;
        }
        // Queue-operation entries have no `message` object: the text lives in a
        // root-level `content` string. Only indexed when --include-queue-ops is
        // set; content-bearing enqueue/popAll surface, empty remove/dequeue are
        // dropped by extract_queue_op_text. They count as role:user.
        if entry.get("type").and_then(|v| v.as_str()) == Some("queue-operation") {
            if !include_queue_ops {
                continue;
            }
            let text = match extract_queue_op_text(&entry) {
                Some(t) => t,
                None => continue,
            };
            let timestamp_str = entry
                .get("timestamp")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            let timestamp = if timestamp_str.is_empty() {
                None
            } else {
                parse_iso8601(&timestamp_str)
            };
            out.push(ScanMessage {
                line_number: (idx + 1) as u32,
                timestamp,
                timestamp_str,
                role: "user".to_string(),
                text_default: text.clone(),
                text_with_tools: text,
                is_only_tool_blocks: false,
            });
            continue;
        }
        let message = match entry.get("message") {
            Some(m) => m,
            None => continue,
        };
        let role = message
            .get("role")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        if role.is_empty() {
            continue;
        }
        let content = match message.get("content") {
            Some(c) => c,
            None => continue,
        };
        let timestamp_str = entry
            .get("timestamp")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let timestamp = if timestamp_str.is_empty() {
            None
        } else {
            parse_iso8601(&timestamp_str)
        };
        let text_default = extract_text(content, false);
        // The with-tools variant costs a second content walk + allocation;
        // only build it when --include-tool-blocks will actually read it.
        let text_with_tools = if include_tool_blocks {
            extract_text(content, true)
        } else {
            String::new()
        };
        let only_tool_blocks = is_only_tool_blocks(content);
        out.push(ScanMessage {
            line_number: (idx + 1) as u32,
            timestamp,
            timestamp_str,
            role,
            text_default,
            text_with_tools,
            is_only_tool_blocks: only_tool_blocks,
        });
    }
    out
}

// === Discovery ===

struct DiscoveredFile {
    path: PathBuf,
    slug: String,
    session_id: String,
    host_root: String,
    format: TranscriptFormat,
}

fn mtime_pruned(entry: &DirEntry, since: Option<f64>) -> bool {
    let cutoff = match since {
        Some(c) => c,
        None => return false,
    };
    // and_then folds the unreadable-metadata and no-mtime failures into one
    // arm (reachable when the entry is deleted after listing); a pre-epoch
    // mtime fails duration_since. All failures err on the side of inclusion.
    match entry.metadata().and_then(|meta| meta.modified()) {
        Ok(mtime) => match mtime.duration_since(UNIX_EPOCH) {
            Ok(d) => d.as_secs_f64() < cutoff,
            Err(_) => false,
        },
        Err(_) => false,
    }
}

/// Walk parents (`<root>/<slug>/<sid>.jsonl`) and subagents
/// (`<root>/<slug>/<session>/subagents/agent-*.jsonl`) per SPEC "Discovery"
/// under `search`. A subagent file reports session_id = its enclosing
/// session directory's name (the parent session), so its hits group with
/// the parent in sessions_matched.
fn discover_files(
    roots: &[TranscriptRoot],
    since: Option<f64>,
    cwd_slug: Option<&str>,
) -> Vec<DiscoveredFile> {
    let mut files: Vec<DiscoveredFile> = Vec::new();
    for root in roots {
        if root.format == TranscriptFormat::Codex {
            discover_codex_files(root, since, cwd_slug, &mut files);
            continue;
        }
        let host_root = root.path.display().to_string();
        let slug_entries = match read_dir(&root.path) {
            Ok(e) => e,
            Err(_) => continue,
        };
        for slug_entry in slug_entries.flatten() {
            if !slug_entry.file_type().map(|t| t.is_dir()).unwrap_or(false) {
                continue;
            }
            let slug = slug_entry.file_name().to_string_lossy().to_string();
            if let Some(want) = cwd_slug {
                if slug != want {
                    continue;
                }
            }
            let entries = match read_dir(slug_entry.path()) {
                Ok(e) => e,
                Err(_) => continue,
            };
            // file_type() on a freshly-listed entry fails only on a
            // filesystem race; fold that failure into the iterator filter.
            for (entry, file_type) in entries
                .flatten()
                .filter_map(|entry| entry.file_type().ok().map(|t| (entry, t)))
            {
                if file_type.is_file() {
                    let name = entry.file_name();
                    let name = name.to_string_lossy();
                    let stem = match name.strip_suffix(".jsonl") {
                        Some(s) => s,
                        None => continue,
                    };
                    if mtime_pruned(&entry, since) {
                        continue;
                    }
                    files.push(DiscoveredFile {
                        path: entry.path(),
                        slug: slug.clone(),
                        session_id: stem.to_string(),
                        host_root: host_root.clone(),
                        format: TranscriptFormat::ClaudeCode,
                    });
                } else if file_type.is_dir() {
                    let sid = entry.file_name().to_string_lossy().to_string();
                    let sub_entries = match read_dir(entry.path().join("subagents")) {
                        Ok(e) => e,
                        Err(_) => continue,
                    };
                    for sub in sub_entries.flatten() {
                        if !sub.file_type().map(|t| t.is_file()).unwrap_or(false) {
                            continue;
                        }
                        let sub_name = sub.file_name();
                        let sub_name = sub_name.to_string_lossy();
                        if !sub_name.starts_with("agent-") || !sub_name.ends_with(".jsonl") {
                            continue;
                        }
                        if mtime_pruned(&sub, since) {
                            continue;
                        }
                        files.push(DiscoveredFile {
                            path: sub.path(),
                            slug: slug.clone(),
                            session_id: sid.clone(),
                            host_root: host_root.clone(),
                            format: TranscriptFormat::ClaudeCode,
                        });
                    }
                }
            }
        }
    }
    files
}

fn discover_codex_files(
    root: &TranscriptRoot,
    since: Option<f64>,
    cwd_filter: Option<&str>,
    files: &mut Vec<DiscoveredFile>,
) {
    for year in codex_directory_entries(&root.path)
        .into_iter()
        .filter(|entry| {
            entry.file_type().map(|kind| kind.is_dir()).unwrap_or(false)
                && is_codex_date_component(entry, 4)
        })
    {
        for month in codex_directory_entries(&year.path())
            .into_iter()
            .filter(|entry| {
                entry.file_type().map(|kind| kind.is_dir()).unwrap_or(false)
                    && is_codex_date_component(entry, 2)
            })
        {
            for day in codex_directory_entries(&month.path())
                .into_iter()
                .filter(|entry| {
                    entry.file_type().map(|kind| kind.is_dir()).unwrap_or(false)
                        && is_codex_date_component(entry, 2)
                })
            {
                for rollout in codex_directory_entries(&day.path())
                    .into_iter()
                    .filter(|entry| {
                        entry
                            .file_type()
                            .map(|kind| kind.is_file())
                            .unwrap_or(false)
                    })
                {
                    let name = rollout.file_name();
                    let name = name.to_string_lossy();
                    if !name.starts_with("rollout-")
                        || !name.ends_with(".jsonl")
                        || mtime_pruned(&rollout, since)
                    {
                        continue;
                    }
                    let Some((session_id, cwd)) = read_codex_session_meta(&rollout.path()) else {
                        continue;
                    };
                    if cwd_filter.is_some_and(|wanted| wanted != cwd) {
                        continue;
                    }
                    files.push(DiscoveredFile {
                        path: rollout.path(),
                        slug: cwd,
                        session_id,
                        host_root: root.path.display().to_string(),
                        format: TranscriptFormat::Codex,
                    });
                }
            }
        }
    }
}

fn is_codex_date_component(entry: &DirEntry, expected_length: usize) -> bool {
    let name = entry.file_name();
    let bytes = name.as_encoded_bytes();
    bytes.len() == expected_length && bytes.iter().all(u8::is_ascii_digit)
}

fn read_codex_session_meta(path: &Path) -> Option<(String, String)> {
    let file = std::fs::File::open(path).ok()?;
    let mut first = String::new();
    if BufReader::new(file).read_line(&mut first).ok()? == 0 {
        return None;
    }
    let meta: Value = serde_json::from_str(&first).ok()?;
    if meta.get("type").and_then(Value::as_str) != Some("session_meta") {
        return None;
    }
    let payload = meta.get("payload")?;
    let cwd = payload.get("cwd").and_then(Value::as_str)?;
    let session_id = payload
        .get("session_id")
        .or_else(|| payload.get("id"))
        .and_then(Value::as_str)?;
    Some((session_id.to_string(), cwd.to_string()))
}

fn codex_directory_entries(path: &Path) -> Vec<DirEntry> {
    read_dir(path)
        .map(|entries| entries.flatten().collect())
        .unwrap_or_default()
}

// === Pattern matching ===

/// Raw-byte necessary-condition check for literal (non --regex) patterns,
/// applied to a whole file's bytes before any JSON parsing. Sound because:
/// - the pattern is restricted to ASCII without `"`, `\`, or control bytes,
///   so JSON string-escaping never alters an occurrence of it: if the
///   extracted text contains the literal, the raw line bytes do too;
/// - false positives only cost a normal parse, never an output change.
enum PreFilter {
    // Boxed: Finder embeds its searcher tables and would otherwise dwarf the
    // other variant (clippy::large_enum_variant).
    CaseSensitive(Box<memchr::memmem::Finder<'static>>),
    /// `lower` is the pattern lowercased. `fold_hazard` is set when the
    /// pattern contains k/s (either case): under Unicode simple case folding
    /// those also match U+212A KELVIN SIGN / U+017F LONG S, whose UTF-8 lead
    /// bytes are 0xE2/0xC5 - when the haystack contains either lead byte the
    /// filter passes the file through rather than risk a false negative.
    /// (Same edge Go locks with TestSearchMatcherFastPathParity.)
    AsciiInsensitive {
        lower: Vec<u8>,
        fold_hazard: bool,
    },
}

impl PreFilter {
    fn build(args: &SearchArgs) -> Option<PreFilter> {
        if args.regex {
            return None;
        }
        let bytes = args.pattern.as_bytes();
        if bytes.is_empty()
            || bytes
                .iter()
                .any(|&b| !b.is_ascii() || b == b'"' || b == b'\\' || b.is_ascii_control())
        {
            return None;
        }
        if args.case_sensitive {
            return Some(PreFilter::CaseSensitive(Box::new(
                memchr::memmem::Finder::new(bytes).into_owned(),
            )));
        }
        let fold_hazard = bytes
            .iter()
            .any(|&b| matches!(b, b'k' | b'K' | b's' | b'S'));
        Some(PreFilter::AsciiInsensitive {
            lower: args.pattern.to_ascii_lowercase().into_bytes(),
            fold_hazard,
        })
    }

    fn contains(&self, hay: &[u8]) -> bool {
        match self {
            PreFilter::CaseSensitive(finder) => finder.find(hay).is_some(),
            PreFilter::AsciiInsensitive { lower, fold_hazard } => {
                contains_ascii_ci(hay, lower)
                    || (*fold_hazard && memchr::memchr2(0xE2, 0xC5, hay).is_some())
            }
        }
    }
}

/// Allocation-free ASCII case-insensitive substring scan: SIMD memchr on the
/// first byte (both cases), then a window compare at each candidate.
fn contains_ascii_ci(hay: &[u8], lower: &[u8]) -> bool {
    let n = lower.len();
    if n == 0 || hay.len() < n {
        return false;
    }
    let b0 = lower[0];
    let b0_upper = b0.to_ascii_uppercase();
    let last_start = hay.len() - n;
    let candidate_hits = |i: usize| hay[i..i + n].eq_ignore_ascii_case(lower);
    if b0 == b0_upper {
        for i in memchr::memchr_iter(b0, hay) {
            if i > last_start {
                break;
            }
            if candidate_hits(i) {
                return true;
            }
        }
    } else {
        for i in memchr::memchr2_iter(b0, b0_upper, hay) {
            if i > last_start {
                break;
            }
            if candidate_hits(i) {
                return true;
            }
        }
    }
    false
}

fn build_pattern_regex(args: &SearchArgs) -> Result<Regex, String> {
    let body = if args.regex {
        args.pattern.clone()
    } else {
        regex::escape(&args.pattern)
    };
    let full = if args.case_sensitive {
        body
    } else {
        format!("(?i){}", body)
    };
    Regex::new(&full).map_err(|e| format!("bad regex: {e}"))
}

// Find all non-overlapping match ranges within `text`. Returns byte offsets.
fn find_all_matches(re: &Regex, text: &str) -> Vec<(usize, usize)> {
    re.find_iter(text).map(|m| (m.start(), m.end())).collect()
}

// === Snippet generation ===

fn nudge_char_boundary(text: &str, mut idx: usize) -> usize {
    while idx < text.len() && !text.is_char_boundary(idx) {
        idx += 1;
    }
    idx
}

// Nudge `cut` outward toward the nearest whitespace within ±max_nudge bytes,
// favoring the direction given (negative = left/decrease, positive = right).
fn nudge_to_whitespace(text: &str, cut: usize, direction: i32, max_nudge: usize) -> usize {
    if cut == 0 || cut == text.len() {
        return cut;
    }
    let bytes = text.as_bytes();
    if direction < 0 {
        // Walk left up to max_nudge looking for whitespace.
        let lo = cut.saturating_sub(max_nudge);
        for i in (lo..cut).rev() {
            if text.is_char_boundary(i) && bytes[i].is_ascii_whitespace() {
                return i + 1;
            }
        }
    } else {
        let hi = (cut + max_nudge).min(text.len());
        for (i, &b) in bytes.iter().enumerate().take(hi).skip(cut) {
            if text.is_char_boundary(i) && b.is_ascii_whitespace() {
                return i;
            }
        }
    }
    cut
}

fn make_snippet(text: &str, first_match: (usize, usize), snippet_chars: u32) -> String {
    let half = (snippet_chars / 2) as usize;
    let (mstart, mend) = first_match;
    let raw_lo = mstart.saturating_sub(half);
    let raw_hi = (mend + half).min(text.len());
    let lo = nudge_char_boundary(text, raw_lo);
    let hi = nudge_char_boundary(text, raw_hi);
    // Only nudge to whitespace if we actually clipped on that side.
    let lo = if lo > 0 {
        nudge_to_whitespace(text, lo, -1, 20)
    } else {
        lo
    };
    let hi = if hi < text.len() {
        nudge_to_whitespace(text, hi, 1, 20)
    } else {
        hi
    };
    let lo = nudge_char_boundary(text, lo);
    let hi = nudge_char_boundary(text, hi);
    text[lo..hi].to_string()
}

// === Hit assembly ===

struct Hit {
    timestamp: f64, // for sorting; not emitted
    timestamp_str: String,
    session_id: String,
    cwd_slug: String,
    host_root: String,
    file_path: String,
    line_number: u32,
    role: String,
    snippet: String,
    match_offsets: Vec<(usize, usize)>,
    context_before: Vec<ContextTurn>,
    context_after: Vec<ContextTurn>,
}

struct ContextTurn {
    role: String,
    text: String,
    timestamp: String,
}

fn role_matches(filter: Role, role: &str) -> bool {
    matches!(
        (filter, role),
        (Role::Both, _) | (Role::User, "user") | (Role::Assistant, "assistant")
    )
}

fn build_context_turns(
    messages: &[ScanMessage],
    hit_idx: usize,
    context_n: u32,
) -> (Vec<ContextTurn>, Vec<ContextTurn>) {
    if context_n == 0 {
        return (vec![], vec![]);
    }
    let n = context_n as usize;
    let before: Vec<ContextTurn> = (hit_idx.saturating_sub(n)..hit_idx)
        .map(|i| ContextTurn {
            role: messages[i].role.clone(),
            text: messages[i].text_default.clone(),
            timestamp: messages[i].timestamp_str.clone(),
        })
        .collect();
    let end = (hit_idx + 1 + n).min(messages.len());
    let after: Vec<ContextTurn> = (hit_idx + 1..end)
        .map(|i| ContextTurn {
            role: messages[i].role.clone(),
            text: messages[i].text_default.clone(),
            timestamp: messages[i].timestamp_str.clone(),
        })
        .collect();
    (before, after)
}

fn process_file(
    file: &DiscoveredFile,
    args: &SearchArgs,
    re: &Regex,
    prefilter: Option<&PreFilter>,
) -> Vec<Hit> {
    let messages = scan_file(
        &file.path,
        file.format,
        args.include_queue_ops,
        args.include_tool_blocks,
        prefilter,
    );
    let mut hits: Vec<Hit> = Vec::new();
    for (idx, m) in messages.iter().enumerate() {
        if !role_matches(args.role, &m.role) {
            continue;
        }
        if !args.include_tool_blocks && m.is_only_tool_blocks {
            continue;
        }
        if let Some(ts) = m.timestamp {
            if let Some(s) = args.since {
                if ts < s {
                    continue;
                }
            }
            if let Some(u) = args.until {
                if ts > u {
                    continue;
                }
            }
        } else if args.since.is_some() || args.until.is_some() {
            // No parseable timestamp + a time filter → skip (safer than including).
            continue;
        }
        let searchable_text = if args.include_tool_blocks {
            &m.text_with_tools
        } else {
            &m.text_default
        };
        if searchable_text.is_empty() {
            continue;
        }
        let matches = find_all_matches(re, searchable_text);
        if matches.is_empty() {
            continue;
        }
        let snippet = make_snippet(searchable_text, matches[0], args.snippet_chars);
        // Match offsets are snippet-relative; re-find inside the snippet so
        // any matches that fall within the window (not just the first) are
        // captured. The first match is always wholly inside the snippet
        // because the window is centered on it.
        let snippet_matches = find_all_matches(re, &snippet);
        let (context_before, context_after) = build_context_turns(&messages, idx, args.context);
        hits.push(Hit {
            timestamp: m.timestamp.unwrap_or(0.0),
            timestamp_str: m.timestamp_str.clone(),
            session_id: file.session_id.clone(),
            cwd_slug: file.slug.clone(),
            host_root: file.host_root.clone(),
            file_path: file.path.display().to_string(),
            line_number: m.line_number,
            role: m.role.clone(),
            snippet,
            match_offsets: snippet_matches,
            context_before,
            context_after,
        });
    }
    hits
}

// === Output ===

fn hit_to_json(h: &Hit) -> Value {
    json!({
        "type": "hit",
        "session_id": h.session_id,
        "cwd_slug": h.cwd_slug,
        "host_root": h.host_root,
        "file_path": h.file_path,
        "line_number": h.line_number,
        "timestamp": h.timestamp_str,
        "role": h.role,
        "snippet": h.snippet,
        "match_offsets": h.match_offsets.iter()
            .map(|(a, b)| json!([a, b]))
            .collect::<Vec<_>>(),
        "context_before": h.context_before.iter()
            .map(|t| json!({"role": t.role, "text": t.text, "timestamp": t.timestamp}))
            .collect::<Vec<_>>(),
        "context_after": h.context_after.iter()
            .map(|t| json!({"role": t.role, "text": t.text, "timestamp": t.timestamp}))
            .collect::<Vec<_>>(),
    })
}

/// Aggregate counters for a completed search, shared by the JSON-summary and
/// pretty-print output paths (replaces a 6-arg group threaded through both).
struct SearchSummary {
    total_hits: usize,
    sessions_matched: usize,
    roots_walked: usize,
    files_walked: usize,
    truncated: bool,
    elapsed_ms: u64,
}

fn summary_json(summary: &SearchSummary) -> Value {
    json!({
        "type": "summary",
        "hits": summary.total_hits,
        "sessions_matched": summary.sessions_matched,
        "roots_walked": summary.roots_walked,
        "files_walked": summary.files_walked,
        "truncated": summary.truncated,
        "elapsed_ms": summary.elapsed_ms,
    })
}

fn write_jsonl(hits: &[Hit], summary: &Value, suppress_hits: bool) {
    let stdout = std::io::stdout();
    let mut out = stdout.lock();
    if !suppress_hits {
        for h in hits {
            let _ = writeln!(out, "{}", hit_to_json(h));
        }
    }
    let _ = writeln!(out, "{}", summary);
}

fn write_pretty(hits: &[Hit], summary: &SearchSummary, suppress_hits: bool) {
    let stdout = std::io::stdout();
    let mut out = stdout.lock();
    if !suppress_hits {
        for h in hits {
            let _ = writeln!(
                out,
                "[{}] cwd={} role={} session={}",
                h.timestamp_str, h.cwd_slug, h.role, h.session_id
            );
            let _ = writeln!(out, "  {}:{}", h.file_path, h.line_number);
            for t in &h.context_before {
                let _ = writeln!(out, "  before: {}", truncate(&t.text, 120));
            }
            // match_offsets come from re-running the matcher on a snippet
            // built around the first match, so they are always present and
            // in-bounds; SPEC omits the snippet line otherwise.
            if let Some((mstart, mend)) = h.match_offsets.first() {
                let pre = &h.snippet[..(*mstart).min(h.snippet.len())];
                let mid = &h.snippet[*mstart..(*mend).min(h.snippet.len())];
                let post = &h.snippet[(*mend).min(h.snippet.len())..];
                let _ = writeln!(out, "  >>> {}[{}]{} <<<", pre, mid, post);
            }
            for t in &h.context_after {
                let _ = writeln!(out, "  after:  {}", truncate(&t.text, 120));
            }
            let _ = writeln!(out);
        }
    }
    let _ = writeln!(
        out,
        "{} hits in {} sessions across {} roots ({} files). truncated={} elapsed {}ms.",
        summary.total_hits,
        summary.sessions_matched,
        summary.roots_walked,
        summary.files_walked,
        summary.truncated,
        summary.elapsed_ms
    );
}

fn truncate(s: &str, max: usize) -> String {
    if s.len() <= max {
        s.to_string()
    } else {
        let mut cut = max;
        while cut > 0 && !s.is_char_boundary(cut) {
            cut -= 1;
        }
        format!("{}…", &s[..cut])
    }
}

// === Top-level run ===

pub fn run(raw: &[String]) {
    let started = Instant::now();
    let args = match parse_args(raw) {
        Ok(a) => a,
        Err(e) => {
            eprintln!("walker: search: {e}");
            std::process::exit(2);
        }
    };

    let re = match build_pattern_regex(&args) {
        Ok(r) => r,
        Err(e) => {
            eprintln!("walker: search: {e}");
            std::process::exit(2);
        }
    };
    let prefilter = PreFilter::build(&args);

    let roots = walker_roots::resolve_search_roots(
        args.projects_root.clone(),
        &args.extra_projects_roots,
        args.read_config,
    );
    let files = discover_files(&roots, args.since, args.cwd.as_deref());
    let files_walked = files.len();
    let roots_walked = roots.len();

    // Parallel per-file scan. Work unit = one file; each rayon task returns a
    // local Vec<Hit> which we concat via reduce. The sort below restores the
    // deterministic ordering required by SPEC. regex::Regex is Send+Sync for
    // read-only matching, and serde_json::from_str is per-call thread-safe,
    // so the shared `re` is fine. Each file carries its own host_root (the
    // root it was discovered under). Mirrors the cost-mode pattern in
    // main.rs::run_cost.
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

    let mut hits: Vec<Hit> = pool.install(|| {
        files
            .par_iter()
            .map(|f| process_file(f, &args, &re, prefilter.as_ref()))
            .reduce(Vec::new, |mut acc, mut next| {
                if acc.is_empty() {
                    next
                } else {
                    acc.append(&mut next);
                    acc
                }
            })
    });

    // Sort newest first by timestamp; tiebreak by (session_id, line_number) for
    // deterministic ordering when timestamps collide.
    hits.sort_by(|a, b| {
        b.timestamp
            .partial_cmp(&a.timestamp)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| a.session_id.cmp(&b.session_id))
            .then_with(|| a.line_number.cmp(&b.line_number))
    });

    // sessions_matched is counted BEFORE truncation: how many distinct sessions
    // had any matching message at all.
    let sessions_matched = hits
        .iter()
        .map(|h| (h.cwd_slug.as_str(), h.session_id.as_str()))
        .collect::<HashSet<_>>()
        .len();

    let total_unfiltered = hits.len();
    let truncated = total_unfiltered > args.limit as usize;
    if truncated {
        hits.truncate(args.limit as usize);
    }

    let elapsed_ms = started.elapsed().as_millis() as u64;
    let summary_stats = SearchSummary {
        total_hits: if args.count_only {
            total_unfiltered
        } else {
            hits.len()
        },
        sessions_matched,
        roots_walked,
        files_walked,
        truncated,
        elapsed_ms,
    };

    match args.format {
        Format::Jsonl => write_jsonl(&hits, &summary_json(&summary_stats), args.count_only),
        Format::Pretty => write_pretty(&hits, &summary_stats, args.count_only),
    }

    if truncated {
        eprintln!(
            "walker: search: truncated to --limit={} (had {} total); narrow with --since",
            args.limit, total_unfiltered
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::time::SystemTime;

    fn tempdir_path(suffix: &str) -> PathBuf {
        let mut p = std::env::temp_dir();
        let pid = std::process::id();
        let nanos = std::time::SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        p.push(format!("rust-search-test-{suffix}-{pid}-{nanos}"));
        fs::create_dir_all(&p).unwrap();
        p
    }

    #[test]
    fn parse_time_arg_relative_units() {
        let now = 1_000_000.0;
        // d / h / m / s suffixes.
        assert!((parse_time_arg("3d", now).unwrap() - (now - 3.0 * 86400.0)).abs() < 1e-9);
        assert!((parse_time_arg("2h", now).unwrap() - (now - 2.0 * 3600.0)).abs() < 1e-9);
        assert!((parse_time_arg("30m", now).unwrap() - (now - 30.0 * 60.0)).abs() < 1e-9);
        assert!((parse_time_arg("10s", now).unwrap() - (now - 10.0)).abs() < 1e-9);
        // Fractional value.
        assert!((parse_time_arg("0.5h", now).unwrap() - (now - 1800.0)).abs() < 1e-9);
    }

    #[test]
    fn parse_time_arg_iso8601() {
        // Absolute RFC3339 path.
        let v = parse_time_arg("2025-01-15T00:00:00Z", 0.0).unwrap();
        assert!(v > 1_700_000_000.0 && v < 1_900_000_000.0);
    }

    #[test]
    fn parse_time_arg_errors() {
        assert!(parse_time_arg("", 0.0).is_err());
        assert!(parse_time_arg("   ", 0.0).is_err());
        assert!(parse_time_arg("garbage", 0.0).is_err());
        // 'd' suffix with non-numeric head → falls through to ISO parse → err.
        assert!(parse_time_arg("xd", 0.0).is_err());
    }

    #[test]
    fn nudge_char_boundary_walks_forward() {
        // "héllo" — 'é' is two bytes, so index 2 isn't a char boundary.
        let s = "héllo";
        // Index 2 is mid-é; nudge to next boundary (3).
        assert_eq!(nudge_char_boundary(s, 2), 3);
        // 0 and len() are always boundaries.
        assert_eq!(nudge_char_boundary(s, 0), 0);
        assert_eq!(nudge_char_boundary(s, s.len()), s.len());
    }

    #[test]
    fn nudge_to_whitespace_no_op_at_endpoints() {
        // Covers line 347-348: cut at 0 or cut at end returns immediately.
        let text = "hello world";
        assert_eq!(nudge_to_whitespace(text, 0, -1, 20), 0);
        assert_eq!(nudge_to_whitespace(text, text.len(), 1, 20), text.len());
    }

    #[test]
    fn nudge_to_whitespace_walks_left() {
        // "hello world", cut at 7 (inside "world") → step left to whitespace
        // index 5 → return 5 + 1 = 6.
        let text = "hello world";
        assert_eq!(nudge_to_whitespace(text, 7, -1, 20), 6);
    }

    #[test]
    fn nudge_to_whitespace_walks_right() {
        let text = "hello world";
        // Cut at 3 (inside "hello"), step right to whitespace at 5.
        assert_eq!(nudge_to_whitespace(text, 3, 1, 20), 5);
    }

    #[test]
    fn nudge_to_whitespace_no_whitespace_in_range() {
        // No whitespace within max_nudge → returns original cut.
        let text = "aaaaaaaaaaaa";
        assert_eq!(nudge_to_whitespace(text, 5, -1, 20), 5);
        assert_eq!(nudge_to_whitespace(text, 5, 1, 20), 5);
    }

    #[test]
    fn make_snippet_centers_match() {
        let text = "alpha beta gamma delta epsilon zeta eta theta iota";
        // Match "gamma" at byte 11..16; snippet_chars=20 → ~10 chars each side.
        let mstart = text.find("gamma").unwrap();
        let mend = mstart + "gamma".len();
        let snippet = make_snippet(text, (mstart, mend), 20);
        assert!(snippet.contains("gamma"));
        assert!(snippet.len() <= 40); // ~snippet_chars + a little nudge slack
    }

    #[test]
    fn truncate_short_input_returns_as_is() {
        // Cover line 605: s.len() <= max returns the string verbatim.
        assert_eq!(truncate("short", 10), "short");
        assert_eq!(truncate("", 5), "");
    }

    #[test]
    fn truncate_long_input_appends_ellipsis() {
        // Covers lines 607-611: cut at max + ellipsis.
        let result = truncate("0123456789abcdef", 5);
        assert_eq!(result, "01234…");
    }

    #[test]
    fn truncate_walks_back_to_char_boundary() {
        // "héllo" — 'é' is bytes 1..3. max=2 lands mid-é; walk back to 1.
        let result = truncate("héllo world", 2);
        assert_eq!(result, "h…");
    }

    #[test]
    fn scan_file_open_error_returns_empty() {
        // Covers lines 212-214: File::open Err → return empty vec.
        let missing = PathBuf::from("/nonexistent/path/to/file.jsonl");
        let msgs = scan_file(&missing, TranscriptFormat::ClaudeCode, false, false, None);
        assert!(msgs.is_empty());
    }

    #[test]
    fn scan_file_skips_malformed_lines() {
        // Covers blank-line + bad-JSON + missing-message + missing-content
        // + empty-role + unparseable timestamp branches.
        let dir = tempdir_path("scan-skip");
        let path = dir.join("session.jsonl");
        let lines = [
            "",                                                                                 // blank
            "   ",                            // whitespace-only
            "{garbage",                       // bad JSON
            "{}",                             // no message
            r#"{"message":{}}"#,              // missing role
            r#"{"message":{"role":""}}"#,     // empty role
            r#"{"message":{"role":"user"}}"#, // no content
            r#"{"message":{"role":"user","content":""},"timestamp":"garbage"}"#, // bad ts
            r#"{"message":{"role":"user","content":"hi"},"timestamp":"2025-01-01T00:00:00Z"}"#, // good
        ];
        fs::write(&path, lines.join("\n")).unwrap();
        let msgs = scan_file(&path, TranscriptFormat::ClaudeCode, false, false, None);
        assert_eq!(msgs.len(), 2); // the empty-content user and the good user line
                                   // First valid message has content "" (line 7); second has "hi".
        assert_eq!(msgs.last().unwrap().text_default, "hi");
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn codex_directory_entries_missing_path_is_empty() {
        assert!(codex_directory_entries(Path::new("/nonexistent/codex-root")).is_empty());
        assert!(read_codex_session_meta(Path::new("/nonexistent/codex-rollout")).is_none());
    }

    #[test]
    fn discover_files_prunes_old_mtimes_with_since() {
        // Covers lines 289-293: since-filter mtime branches.
        let root = tempdir_path("discover-prune");
        let slug = root.join("project-x");
        fs::create_dir_all(&slug).unwrap();
        fs::write(slug.join("sid-1.jsonl"), b"").unwrap();
        // Cutoff far in the future → entry is filtered out.
        let far_future = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs_f64()
            + 1e9;
        let tagged = TranscriptRoot {
            path: root.clone(),
            format: TranscriptFormat::ClaudeCode,
        };
        let files = discover_files(std::slice::from_ref(&tagged), Some(far_future), None);
        assert!(files.is_empty());
        // Without a cutoff, the file is discovered.
        let files2 = discover_files(std::slice::from_ref(&tagged), None, None);
        assert_eq!(files2.len(), 1);
        let _ = fs::remove_dir_all(&root);
    }

    /// mtime_pruned fallthrough: a pre-epoch mtime causes duration_since(UNIX_EPOCH)
    /// to fail, so the function returns false (err on inclusion).
    #[cfg(unix)]
    #[test]
    fn mtime_pruned_deleted_entry_returns_false() {
        // Deleting the file after listing makes DirEntry::metadata() fail
        // (it stats lazily) -> the folded Err arm errs on inclusion.
        let root = tempdir_path("mtime-pruned-deleted");
        let file_path = root.join("gone.jsonl");
        fs::write(&file_path, b"").unwrap();
        let entry = std::fs::read_dir(&root)
            .expect("read_dir")
            .flatten()
            .next()
            .expect("one entry");
        fs::remove_file(&file_path).unwrap();
        assert!(!mtime_pruned(&entry, Some(f64::MAX)));
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn mtime_pruned_pre_epoch_mtime_returns_false() {
        use std::time::Duration;
        let root = tempdir_path("mtime-pruned-pre-epoch");
        let file_path = root.join("file.jsonl");
        fs::write(&file_path, b"").unwrap();

        let pre_epoch = SystemTime::UNIX_EPOCH
            .checked_sub(Duration::from_secs(86400))
            .expect("UNIX_EPOCH - 1 day overflows");
        if fs::File::open(&file_path)
            .ok()
            .and_then(|f| f.set_modified(pre_epoch).ok())
            .is_none()
        {
            eprintln!("skip: set_modified(pre-epoch) not supported on this filesystem");
            let _ = fs::remove_dir_all(&root);
            return;
        }

        let entry = std::fs::read_dir(&root)
            .expect("read_dir")
            .flatten()
            .next()
            .expect("one entry");

        let cutoff = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_secs_f64())
            .unwrap_or(0.0);
        // duration_since(UNIX_EPOCH) fails for pre-epoch mtime, so mtime_pruned
        // must return false (include the file, don't prune it).
        assert!(
            !mtime_pruned(&entry, Some(cutoff)),
            "pre-epoch mtime should fall through to false (err on inclusion)"
        );
        let _ = fs::remove_dir_all(&root);
    }

    /// discover_files returns empty when the root directory is unreadable.
    #[cfg(unix)]
    #[test]
    fn discover_files_unreadable_root_returns_empty() {
        use std::os::unix::fs::PermissionsExt;
        let root = tempdir_path("search-unreadable-root");
        fs::set_permissions(&root, fs::Permissions::from_mode(0o000)).unwrap();
        let tagged = TranscriptRoot {
            path: root.clone(),
            format: TranscriptFormat::ClaudeCode,
        };
        let files = discover_files(std::slice::from_ref(&tagged), None, None);
        fs::set_permissions(&root, fs::Permissions::from_mode(0o755)).unwrap();
        let _ = fs::remove_dir_all(&root);
        assert!(files.is_empty(), "unreadable root should yield no files");
    }

    /// discover_files skips non-directory entries at the root level (i.e. a
    /// plain file directly under root, which passes the flatten but fails the
    /// is_dir check). No cfg(unix) needed: creating a plain file is portable.
    #[test]
    fn discover_files_non_dir_at_root_level_skipped() {
        let root = tempdir_path("search-nondirroot");
        // A plain file directly under root: the slug scan skips it.
        fs::write(root.join("not-a-slug.jsonl"), b"").unwrap();
        let tagged = TranscriptRoot {
            path: root.clone(),
            format: TranscriptFormat::ClaudeCode,
        };
        let files = discover_files(std::slice::from_ref(&tagged), None, None);
        let _ = fs::remove_dir_all(&root);
        assert!(
            files.is_empty(),
            "a plain file at root level should be skipped"
        );
    }

    /// discover_files skips a slug directory that is unreadable.
    #[cfg(unix)]
    #[test]
    fn discover_files_unreadable_slug_dir_skipped() {
        use std::os::unix::fs::PermissionsExt;
        let root = tempdir_path("search-unreadable-slug");
        let bad_slug = root.join("bad-slug");
        fs::create_dir_all(&bad_slug).unwrap();
        // Write a file inside the directory before locking it out.
        fs::write(bad_slug.join("session-x.jsonl"), b"").unwrap();
        fs::set_permissions(&bad_slug, fs::Permissions::from_mode(0o000)).unwrap();
        // chmod 000 does not block root (CI containers) or permissive
        // filesystems; the precondition is unobtainable there, so skip.
        if fs::read_dir(&bad_slug).is_ok() {
            fs::set_permissions(&bad_slug, fs::Permissions::from_mode(0o755)).unwrap();
            let _ = fs::remove_dir_all(&root);
            eprintln!("skipping: environment cannot produce an unreadable dir");
            return;
        }

        // Also set up a readable slug to confirm only the bad one is skipped.
        let good_slug = root.join("good-slug");
        fs::create_dir_all(&good_slug).unwrap();
        fs::write(good_slug.join("session-y.jsonl"), b"").unwrap();

        let tagged = TranscriptRoot {
            path: root.clone(),
            format: TranscriptFormat::ClaudeCode,
        };
        let files = discover_files(std::slice::from_ref(&tagged), None, None);

        fs::set_permissions(&bad_slug, fs::Permissions::from_mode(0o755)).unwrap();
        let _ = fs::remove_dir_all(&root);

        // Only the good slug's file appears; the bad slug is silently skipped.
        assert_eq!(files.len(), 1, "expected only the good slug's file");
        assert!(
            files[0].slug == "good-slug",
            "wrong slug: {}",
            files[0].slug
        );
    }

    /// discover_files skips a dangling symlink inside a slug directory: its
    /// file_type() returns the symlink type (not is_file/is_dir), which is
    /// neither branch, and the entry is skipped.
    #[cfg(unix)]
    #[test]
    fn discover_files_dangling_symlink_in_slug_skipped() {
        use std::os::unix::fs::symlink;
        let root = tempdir_path("search-dangling-symlink");
        let slug = root.join("slug-b");
        fs::create_dir_all(&slug).unwrap();
        // Dangling symlink: target does not exist.
        symlink("/nonexistent/ghost.jsonl", slug.join("ghost.jsonl")).unwrap();

        let tagged = TranscriptRoot {
            path: root.clone(),
            format: TranscriptFormat::ClaudeCode,
        };
        let files = discover_files(std::slice::from_ref(&tagged), None, None);
        let _ = fs::remove_dir_all(&root);

        assert!(
            files.is_empty(),
            "dangling symlink should be skipped, got {} files",
            files.len()
        );
    }

    /// contains_ascii_ci break branch: fires when the pattern's first byte is
    /// non-alphabetic (b0 == b0_upper) and the only occurrence of that byte in
    /// the haystack is at an index > last_start (too close to the end for the
    /// pattern to fit). The break exits the iterator and returns false.
    #[test]
    fn contains_ascii_ci_break_when_first_byte_only_at_tail() {
        // Pattern "1ab": first byte is b'1' (non-alphabetic: b0 == b0_upper).
        // Haystack "xx1": the only '1' is at index 2, but last_start = 0
        // (haystack.len() 3 - pattern.len() 3 = 0), so i=2 > last_start=0
        // triggers the break and the function returns false.
        assert!(
            !contains_ascii_ci(b"xx1", b"1ab"),
            "candidate at index > last_start should break and return false"
        );

        // Sanity: the pattern IS found when there is enough room.
        assert!(
            contains_ascii_ci(b"1ab", b"1ab"),
            "should find pattern at index 0"
        );
    }
}
