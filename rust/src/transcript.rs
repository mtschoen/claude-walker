// Shared transcript parsing, discovery, and pricing helpers.
// Used by both cost mode (main.rs) and the events subcommand (events.rs).
// Extracted from main.rs so both modules reference a single definition.

use std::collections::HashMap;
use std::fs::{read_dir, DirEntry};
use std::path::PathBuf;
use std::time::UNIX_EPOCH;

use serde::{Deserialize, Deserializer};

// ── Entry types ──────────────────────────────────────────────────────────────
//
// Wrong-typed fields are treated as absent rather than poisoning the line,
// per SPEC.md §"Lenient per-field parsing". The lenient_* helpers below back
// the deserialize_with attributes.

#[derive(Deserialize)]
pub(crate) struct Entry {
    #[serde(default, deserialize_with = "lenient_string")]
    pub(crate) timestamp: Option<String>,
    pub(crate) message: Option<Message>,
}

#[derive(Deserialize)]
pub(crate) struct Message {
    #[serde(default, deserialize_with = "lenient_string")]
    pub(crate) role: Option<String>,
    #[serde(default, deserialize_with = "lenient_string")]
    pub(crate) id: Option<String>,
    #[serde(default, deserialize_with = "lenient_string")]
    pub(crate) model: Option<String>,
    #[serde(default, deserialize_with = "lenient_usage")]
    pub(crate) usage: Option<Usage>,
}

#[derive(Deserialize, Default)]
pub(crate) struct Usage {
    #[serde(default, deserialize_with = "lenient_count")]
    pub(crate) input_tokens: u64,
    #[serde(default, deserialize_with = "lenient_count")]
    pub(crate) output_tokens: u64,
    #[serde(default, deserialize_with = "lenient_count")]
    pub(crate) cache_read_input_tokens: u64,
    #[serde(default, deserialize_with = "lenient_count")]
    pub(crate) cache_creation_input_tokens: u64,
    #[serde(default, deserialize_with = "lenient_server_tool_use")]
    pub(crate) server_tool_use: ServerToolUse,
}

#[derive(Deserialize, Default)]
pub(crate) struct ServerToolUse {
    #[serde(default, deserialize_with = "lenient_count")]
    pub(crate) web_search_requests: u64,
}

/// Non-string values are treated as an absent field.
fn lenient_string<'de, D: Deserializer<'de>>(d: D) -> Result<Option<String>, D::Error> {
    let value = serde_json::Value::deserialize(d)?;
    Ok(match value {
        serde_json::Value::String(s) => Some(s),
        _ => None,
    })
}

/// Token counts accept any JSON number, truncated toward zero; values
/// outside [0, u64::MAX] and non-number tokens are treated as absent (0).
fn lenient_count<'de, D: Deserializer<'de>>(d: D) -> Result<u64, D::Error> {
    let value = serde_json::Value::deserialize(d)?;
    Ok(match value {
        serde_json::Value::Number(n) => {
            if let Some(u) = n.as_u64() {
                u
            } else {
                match n.as_f64() {
                    Some(f) if (0.0..18446744073709551616.0).contains(&f) => f as u64,
                    _ => 0,
                }
            }
        }
        _ => 0,
    })
}

/// A non-object `usage` is an absent subtree (all counts 0).
fn lenient_usage<'de, D: Deserializer<'de>>(d: D) -> Result<Option<Usage>, D::Error> {
    let value = serde_json::Value::deserialize(d)?;
    Ok(Usage::deserialize(value).ok())
}

/// A non-object `server_tool_use` is an absent subtree.
fn lenient_server_tool_use<'de, D: Deserializer<'de>>(d: D) -> Result<ServerToolUse, D::Error> {
    let value = serde_json::Value::deserialize(d)?;
    Ok(ServerToolUse::deserialize(value).unwrap_or_default())
}

// ── Pricing ───────────────────────────────────────────────────────────────────

/// Flat charge per server-side web search request (billed $10 / 1,000),
/// added on top of token cost. Matches SPEC.md and the Python reference.
pub(crate) const WEB_SEARCH_COST_USD: f64 = 0.01;

/// Sonnet 5 intro->standard boundary. Intro pricing runs through 2026-08-31
/// inclusive; standard applies from 2026-09-01 onward. Selection is a
/// lexicographic compare of the turn's ISO-8601 date prefix ("YYYY-MM-DD")
/// against this string — monotonic for zero-padded dates, no timezone math.
pub(crate) const SONNET5_STANDARD_FROM: &str = "2026-09-01";

/// (input_per_mtok, output_per_mtok). Matches SPEC.md exactly. `date_prefix`
/// is the turn's ISO-8601 timestamp (only its leading 10-char "YYYY-MM-DD" is
/// consulted, and only for the sonnet-5 family).
pub(crate) fn rates_for(model: &str, date_prefix: &str) -> (f64, f64) {
    let m = model.to_ascii_lowercase();
    // fable/mythos first: the Fable family bills at $10/$50 and must win
    // before any substring that could collide.
    if m.contains("fable") || m.contains("mythos") {
        (10.0, 50.0)
    } else if m.contains("opus") {
        (5.0, 25.0)
    } else if m.contains("haiku") {
        (1.0, 5.0)
    } else if m.contains("sonnet-5") {
        // sonnet-5 BEFORE the generic sonnet default. "sonnet-5" does NOT
        // match "claude-sonnet-4-5" (no such substring), so Sonnet 4.5 keeps
        // the generic sonnet rates below, date-independent.
        if date_prefix.len() >= 10 && &date_prefix[..10] >= SONNET5_STANDARD_FROM {
            (3.0, 15.0) // standard
        } else {
            (2.0, 10.0) // intro
        }
    } else {
        // sonnet — and any unknown model falls back to sonnet rates, per SPEC.md
        (3.0, 15.0)
    }
}

pub(crate) fn cost_for(usage: &Usage, model: &str, date_prefix: &str) -> f64 {
    let (i_rate, o_rate) = rates_for(model, date_prefix);
    let token_cost = (usage.input_tokens as f64 * i_rate
        + usage.cache_read_input_tokens as f64 * i_rate * 0.10
        + usage.cache_creation_input_tokens as f64 * i_rate * 1.25
        + usage.output_tokens as f64 * o_rate)
        / 1_000_000.0;
    token_cost + usage.server_tool_use.web_search_requests as f64 * WEB_SEARCH_COST_USD
}

// ── Discovery ─────────────────────────────────────────────────────────────────

/// True when the entry's mtime is readable and earlier than `earliest`.
/// Unreadable mtimes err on the side of inclusion (matches the prior glob
/// path and C++'s mtimeAtOrAfter). Uses DirEntry::metadata so the stat comes
/// from the directory scan's own data where the platform provides it.
pub(crate) fn entry_mtime_before(entry: &DirEntry, earliest: f64) -> bool {
    // and_then folds the unreadable-metadata and no-mtime failures into one
    // arm (reachable when the entry is deleted after listing); a pre-epoch
    // mtime fails duration_since. All failures err on the side of inclusion.
    match entry.metadata().and_then(|meta| meta.modified()) {
        Ok(mtime) => match mtime.duration_since(UNIX_EPOCH) {
            Ok(d) => d.as_secs_f64() < earliest,
            Err(_) => false,
        },
        Err(_) => false,
    }
}

/// Discover all `.jsonl` files under `roots`, grouped by `(slug, session_id)`.
/// Files whose mtime is earlier than `earliest` are skipped (fast-path prune);
/// pass `f64::NEG_INFINITY` to disable the prune (skips the stat entirely).
///
/// Single fused walk per slug dir: parents (`<slug>/<sid>.jsonl`) and
/// subagents (`<slug>/<session>/subagents/agent-*.jsonl`) are classified in
/// one directory pass, replacing the prior two-glob approach that re-read
/// every slug dir twice and paid an extra stat per file.
pub(crate) fn discover_groups(
    roots: &[PathBuf],
    earliest: f64,
) -> HashMap<(String, String), Vec<PathBuf>> {
    let mut groups: HashMap<(String, String), Vec<PathBuf>> = HashMap::new();
    let prune = earliest > f64::NEG_INFINITY;

    for root in roots {
        let slug_entries = match read_dir(root) {
            Ok(e) => e,
            Err(_) => continue,
        };
        for slug_entry in slug_entries.flatten() {
            if !slug_entry.file_type().map(|t| t.is_dir()).unwrap_or(false) {
                continue;
            }
            let slug = slug_entry.file_name().to_string_lossy().to_string();
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
                    // Parent: <root>/<slug>/<session_id>.jsonl
                    let name = entry.file_name();
                    let name = name.to_string_lossy();
                    let stem = match name.strip_suffix(".jsonl") {
                        Some(s) => s,
                        None => continue,
                    };
                    if prune && entry_mtime_before(&entry, earliest) {
                        continue;
                    }
                    groups
                        .entry((slug.clone(), stem.to_string()))
                        .or_default()
                        .push(entry.path());
                } else if file_type.is_dir() {
                    // Subagents: <root>/<slug>/<session>/subagents/agent-*.jsonl
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
                        if prune && entry_mtime_before(&sub, earliest) {
                            continue;
                        }
                        groups
                            .entry((slug.clone(), sid.clone()))
                            .or_default()
                            .push(sub.path());
                    }
                }
            }
        }
    }

    groups
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
        p.push(format!("rust-transcript-test-{suffix}-{pid}-{nanos}"));
        fs::create_dir_all(&p).unwrap();
        p
    }

    #[test]
    fn rates_for_opus_haiku_default() {
        assert_eq!(rates_for("claude-3-opus-20240229", ""), (5.0, 25.0));
        assert_eq!(rates_for("CLAUDE-OPUS-4-5", ""), (5.0, 25.0));
        assert_eq!(rates_for("claude-3-5-haiku", ""), (1.0, 5.0));
        assert_eq!(rates_for("claude-3-5-sonnet", ""), (3.0, 15.0));
        assert_eq!(rates_for("unknown-model", ""), (3.0, 15.0));
    }

    #[test]
    fn rates_for_fable_mythos() {
        // Fable family: $10/$50, date-independent.
        assert_eq!(rates_for("claude-fable-5", "2026-05-10"), (10.0, 50.0));
        assert_eq!(rates_for("CLAUDE-FABLE-5", "2026-12-31"), (10.0, 50.0));
        assert_eq!(rates_for("claude-mythos-preview", ""), (10.0, 50.0));
    }

    #[test]
    fn rates_for_sonnet5_date_aware() {
        // Intro through 2026-08-31 inclusive; standard from 2026-09-01.
        assert_eq!(rates_for("claude-sonnet-5", "2026-05-10"), (2.0, 10.0));
        assert_eq!(rates_for("claude-sonnet-5", "2026-08-31"), (2.0, 10.0));
        assert_eq!(rates_for("claude-sonnet-5", "2026-09-01"), (3.0, 15.0));
        assert_eq!(rates_for("claude-sonnet-5", "2026-09-15"), (3.0, 15.0));
        // Missing/short date prefix defaults to intro.
        assert_eq!(rates_for("claude-sonnet-5", ""), (2.0, 10.0));
    }

    #[test]
    fn rates_for_sonnet45_not_sonnet5() {
        // "sonnet-4-5" does NOT contain the "sonnet-5" substring -> generic
        // sonnet rates ($3/$15), date-independent (must not intro-price).
        assert_eq!(rates_for("claude-sonnet-4-5", "2026-05-10"), (3.0, 15.0));
        assert_eq!(rates_for("claude-sonnet-4-5", "2026-09-15"), (3.0, 15.0));
    }

    #[test]
    fn cost_for_includes_web_search_flat_fee() {
        let usage = Usage {
            input_tokens: 0,
            output_tokens: 0,
            cache_read_input_tokens: 0,
            cache_creation_input_tokens: 0,
            server_tool_use: ServerToolUse {
                web_search_requests: 3,
            },
        };
        // 3 * $0.01 = $0.03, no token cost.
        assert!((cost_for(&usage, "sonnet", "") - 0.03).abs() < 1e-9);
    }

    #[test]
    fn cost_for_token_breakdown() {
        // 1M input @ sonnet ($3) + 1M output @ sonnet ($15) = $18
        let usage = Usage {
            input_tokens: 1_000_000,
            output_tokens: 1_000_000,
            cache_read_input_tokens: 0,
            cache_creation_input_tokens: 0,
            server_tool_use: ServerToolUse::default(),
        };
        assert!((cost_for(&usage, "sonnet", "") - 18.0).abs() < 1e-6);
        // cache_read at 0.1× input rate; cache_creation at 1.25× input rate.
        let usage2 = Usage {
            input_tokens: 0,
            output_tokens: 0,
            cache_read_input_tokens: 10_000_000, // 10M * $3 * 0.10 = $3
            cache_creation_input_tokens: 800_000, // 800k * $3 * 1.25 = $3
            server_tool_use: ServerToolUse::default(),
        };
        assert!((cost_for(&usage2, "sonnet", "") - 6.0).abs() < 1e-6);
    }

    #[test]
    fn discover_groups_prunes_old_mtimes() {
        // Cover lines 96/122 (mtime < earliest continue). We pass an
        // `earliest` far in the future, so the just-created file's mtime
        // is necessarily below it and the entry is pruned.
        let root = tempdir_path("prune");
        let slug = root.join("test-slug");
        fs::create_dir_all(&slug).unwrap();
        fs::write(slug.join("session-1.jsonl"), b"").unwrap();
        // Mirror in a subagent layout to cover the second prune branch.
        let subagents = slug.join("session-2").join("subagents");
        fs::create_dir_all(&subagents).unwrap();
        fs::write(subagents.join("agent-a.jsonl"), b"").unwrap();

        let far_future = (SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_secs_f64())
            .unwrap_or(0.0))
            + 1e9;
        let groups = discover_groups(std::slice::from_ref(&root), far_future);
        assert!(
            groups.is_empty(),
            "future cutoff should prune everything, got {:?}",
            groups
        );
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn discover_groups_includes_subagent_files() {
        let root = tempdir_path("subagent");
        let session_dir = root.join("slug").join("session-1");
        let subagents = session_dir.join("subagents");
        fs::create_dir_all(&subagents).unwrap();
        let agent_file = subagents.join("agent-aaa.jsonl");
        fs::write(&agent_file, b"").unwrap();
        let groups = discover_groups(std::slice::from_ref(&root), 0.0);
        // Subagent file should be discovered under (slug, session-1).
        let key = ("slug".to_string(), "session-1".to_string());
        assert!(
            groups.contains_key(&key),
            "expected key in {:?}",
            groups.keys().collect::<Vec<_>>()
        );
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn discover_groups_empty_when_no_roots() {
        let groups = discover_groups(&[], 0.0);
        assert!(groups.is_empty());
    }

    /// entry_mtime_before fallthrough: a file whose mtime is before the Unix
    /// epoch causes duration_since(UNIX_EPOCH) to fail, so the function falls
    /// through to `false` (err on inclusion).
    #[cfg(unix)]
    #[test]
    fn entry_mtime_before_deleted_entry_returns_false() {
        // Deleting the file after listing makes DirEntry::metadata() fail
        // (it stats lazily) -> the folded Err arm errs on inclusion.
        let root = tempdir_path("mtime-deleted");
        let file_path = root.join("gone.jsonl");
        fs::write(&file_path, b"").unwrap();
        let entry = std::fs::read_dir(&root)
            .expect("read_dir")
            .flatten()
            .next()
            .expect("one entry");
        fs::remove_file(&file_path).unwrap();
        assert!(!entry_mtime_before(&entry, f64::MAX));
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn entry_mtime_before_pre_epoch_mtime_returns_false() {
        use std::time::Duration;
        let root = tempdir_path("mtime-pre-epoch");
        let file_path = root.join("sentinel.jsonl");
        fs::write(&file_path, b"").unwrap();

        // Attempt to set mtime before the Unix epoch. If the filesystem or OS
        // rejects the time (e.g. FAT32 clamps at 1980), skip rather than fail.
        let pre_epoch = SystemTime::UNIX_EPOCH
            .checked_sub(Duration::from_secs(86400))
            .expect("UNIX_EPOCH - 1 day overflows SystemTime");
        if fs::File::open(&file_path)
            .ok()
            .and_then(|f| f.set_modified(pre_epoch).ok())
            .is_none()
        {
            eprintln!("skip: set_modified(pre-epoch) not supported on this filesystem");
            let _ = fs::remove_dir_all(&root);
            return;
        }

        // Obtain a DirEntry via read_dir so we call the real entry_mtime_before.
        let entry = std::fs::read_dir(&root)
            .expect("read_dir")
            .flatten()
            .next()
            .expect("one entry");

        // Positive cutoff: a normal "now" timestamp. duration_since(UNIX_EPOCH)
        // will fail for the pre-epoch mtime, so entry_mtime_before must return
        // false (include the file) rather than treating it as old.
        let cutoff = SystemTime::now()
            .duration_since(SystemTime::UNIX_EPOCH)
            .map(|d| d.as_secs_f64())
            .unwrap_or(0.0);
        assert!(
            !entry_mtime_before(&entry, cutoff),
            "pre-epoch mtime should fall through to false (err on inclusion)"
        );
        let _ = fs::remove_dir_all(&root);
    }

    /// discover_groups returns empty when the root directory is unreadable.
    #[cfg(unix)]
    #[test]
    fn discover_groups_unreadable_root_returns_empty() {
        use std::os::unix::fs::PermissionsExt;
        let root = tempdir_path("unreadable-root");
        fs::set_permissions(&root, fs::Permissions::from_mode(0o000)).unwrap();
        let groups = discover_groups(std::slice::from_ref(&root), f64::NEG_INFINITY);
        // Restore before cleanup so the temp dir removal succeeds.
        fs::set_permissions(&root, fs::Permissions::from_mode(0o755)).unwrap();
        let _ = fs::remove_dir_all(&root);
        assert!(
            groups.is_empty(),
            "unreadable root should yield no groups"
        );
    }

    /// discover_groups skips a slug directory that is unreadable.
    #[cfg(unix)]
    #[test]
    fn discover_groups_unreadable_slug_dir_skipped() {
        use std::os::unix::fs::PermissionsExt;
        let root = tempdir_path("unreadable-slug");
        let slug = root.join("bad-slug");
        fs::create_dir_all(&slug).unwrap();
        // Create a readable sibling so we can check it is still found.
        let good_slug = root.join("good-slug");
        fs::create_dir_all(&good_slug).unwrap();
        fs::write(good_slug.join("session-ok.jsonl"), b"").unwrap();
        fs::set_permissions(&slug, fs::Permissions::from_mode(0o000)).unwrap();

        let groups = discover_groups(std::slice::from_ref(&root), f64::NEG_INFINITY);

        fs::set_permissions(&slug, fs::Permissions::from_mode(0o755)).unwrap();
        let _ = fs::remove_dir_all(&root);

        // The bad slug is skipped; the good slug's file is discovered.
        let bad_key = ("bad-slug".to_string(), "session-ok".to_string());
        assert!(
            !groups.contains_key(&bad_key),
            "bad slug should be absent from groups"
        );
        let good_key = ("good-slug".to_string(), "session-ok".to_string());
        assert!(
            groups.contains_key(&good_key),
            "good slug should be present in groups"
        );
    }

    /// discover_groups falls through the "neither file nor dir" branch for a
    /// dangling symlink: file_type() for a symlink returns the symlink's own
    /// type, which is neither is_file() nor is_dir(), so it is skipped.
    #[cfg(unix)]
    #[test]
    fn discover_groups_dangling_symlink_skipped() {
        use std::os::unix::fs::symlink;
        let root = tempdir_path("dangling-symlink");
        let slug = root.join("slug-a");
        fs::create_dir_all(&slug).unwrap();
        // Create a dangling symlink pointing to a non-existent target.
        symlink("/nonexistent/target.jsonl", slug.join("dangling.jsonl")).unwrap();

        let groups = discover_groups(std::slice::from_ref(&root), f64::NEG_INFINITY);
        let _ = fs::remove_dir_all(&root);

        // The dangling symlink should not produce any group entry.
        assert!(
            groups.is_empty(),
            "dangling symlink should be skipped, got {:?}",
            groups
        );
    }
}
