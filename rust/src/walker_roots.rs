// Roots discovery: primary root + extras from CLI flags + extras from
// ~/.claude/walker-roots.json. Deduped via fs::canonicalize, filtered to
// existing directories.
//
// Mirrors cpp/walker_roots.hpp. Failure modes follow the SPEC.md contract:
//   * Missing config file -> no extras (silent).
//   * Malformed JSON -> stderr diagnostic, treat as no extras (must NOT error).
//   * Listed path doesn't exist on disk -> skip silently with stderr line.
//   * canonicalize() fails (broken symlink etc) -> fall back to the raw path.
//   * Primary is allowed to not exist (empty-fleet case); no stderr for it.

use serde_json::Value;
use std::collections::HashSet;
use std::ffi::OsString;
use std::fs;
use std::path::PathBuf;

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub(crate) enum TranscriptFormat {
    ClaudeCode,
    Codex,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct TranscriptRoot {
    pub(crate) path: PathBuf,
    pub(crate) format: TranscriptFormat,
}

/// Resolve the user's home directory the way every walker subcommand must.
///
/// On Windows, `USERPROFILE` is the canonical home; `HOME` is frequently
/// unset, or set by git-bash to a POSIX-style path (`/c/Users/...`) that is
/// not a valid native path — so prefer `USERPROFILE`, fall back to `HOME`.
/// On other platforms, `HOME` is canonical (fall back to `USERPROFILE`).
#[cfg(windows)]
pub fn home_directory() -> Option<OsString> {
    std::env::var_os("USERPROFILE").or_else(|| std::env::var_os("HOME"))
}

#[cfg(not(windows))]
pub fn home_directory() -> Option<OsString> {
    std::env::var_os("HOME").or_else(|| std::env::var_os("USERPROFILE"))
}

pub fn walker_config_path() -> PathBuf {
    match home_directory() {
        Some(h) => PathBuf::from(h).join(".claude").join("walker-roots.json"),
        None => PathBuf::from(".claude/walker-roots.json"),
    }
}

pub fn read_extra_roots_from_config() -> Vec<PathBuf> {
    read_tagged_extra_roots_from_config()
        .into_iter()
        .filter(|root| root.format == TranscriptFormat::ClaudeCode)
        .map(|root| root.path)
        .collect()
}

pub(crate) fn read_tagged_extra_roots_from_config() -> Vec<TranscriptRoot> {
    let config = walker_config_path();
    if !config.exists() {
        return Vec::new();
    }
    let body = match fs::read_to_string(&config) {
        Ok(s) => s,
        Err(_) => return Vec::new(),
    };
    if body.trim().is_empty() {
        return Vec::new();
    }
    let parsed: Value = match serde_json::from_str(&body) {
        Ok(v) => v,
        Err(_) => {
            eprintln!(
                "walker: malformed {} -- ignoring extra roots",
                config.display()
            );
            return Vec::new();
        }
    };
    let object = match parsed.as_object() {
        Some(o) => o,
        None => {
            eprintln!(
                "walker: {} is not a JSON object -- ignoring",
                config.display()
            );
            return Vec::new();
        }
    };
    let array = match object.get("extra_roots").and_then(|v| v.as_array()) {
        Some(a) => a,
        None => return Vec::new(),
    };
    let mut extras = Vec::new();
    for element in array {
        if let Some(s) = element.as_str() {
            if !s.is_empty() {
                extras.push(TranscriptRoot {
                    path: PathBuf::from(s),
                    format: TranscriptFormat::ClaudeCode,
                });
            }
        } else if let Some(tagged) = element.as_object() {
            let Some(path) = tagged.get("path").and_then(Value::as_str) else {
                continue;
            };
            let format = match tagged.get("format").and_then(Value::as_str) {
                Some("claude-code") => TranscriptFormat::ClaudeCode,
                Some("codex") => TranscriptFormat::Codex,
                _ => continue,
            };
            if !path.is_empty() {
                extras.push(TranscriptRoot {
                    path: PathBuf::from(path),
                    format,
                });
            }
        }
    }
    extras
}

pub(crate) fn default_codex_root() -> PathBuf {
    match home_directory() {
        Some(home) => PathBuf::from(home).join(".codex").join("sessions"),
        None => PathBuf::from(".codex/sessions"),
    }
}

/// Resolve search roots with an explicit format tag. Existing CLI roots and
/// string config entries remain Claude Code for backward compatibility. When
/// the primary root is not overridden, the local Codex sessions root is added.
pub(crate) fn resolve_search_roots(
    primary: Option<PathBuf>,
    cli_extras: &[PathBuf],
    read_config: bool,
) -> Vec<TranscriptRoot> {
    let using_defaults = primary.is_none();
    let mut combined = vec![TranscriptRoot {
        path: primary.unwrap_or_else(crate::default_projects_root),
        format: TranscriptFormat::ClaudeCode,
    }];
    if using_defaults {
        combined.push(TranscriptRoot {
            path: default_codex_root(),
            format: TranscriptFormat::Codex,
        });
    }
    combined.extend(cli_extras.iter().cloned().map(|path| TranscriptRoot {
        path,
        format: TranscriptFormat::ClaudeCode,
    }));
    if read_config {
        combined.extend(read_tagged_extra_roots_from_config());
    }

    let mut result = Vec::new();
    let mut seen: HashSet<(TranscriptFormat, String)> = HashSet::new();
    for (index, root) in combined.into_iter().enumerate() {
        if !root.path.is_dir() {
            if index > 0 && root.path.exists() {
                eprintln!(
                    "walker: extra root not a directory, skipping: {}",
                    root.path.display()
                );
            }
            continue;
        }
        let key = fs::canonicalize(&root.path)
            .map(|path| path.to_string_lossy().into_owned())
            .unwrap_or_else(|_| root.path.to_string_lossy().into_owned());
        if seen.insert((root.format, key)) {
            result.push(root);
        }
    }
    result
}

pub fn resolve_roots(primary: PathBuf, cli_extras: &[PathBuf], read_config: bool) -> Vec<PathBuf> {
    let mut combined: Vec<(PathBuf, bool)> = Vec::new();
    combined.push((primary, true));
    for p in cli_extras {
        combined.push((p.clone(), false));
    }
    if read_config {
        for p in read_extra_roots_from_config() {
            combined.push((p, false));
        }
    }

    let mut result = Vec::new();
    let mut seen: HashSet<String> = HashSet::new();
    for (path, is_primary) in combined {
        if !path.exists() || !path.is_dir() {
            if !is_primary {
                eprintln!(
                    "walker: extra root not a directory, skipping: {}",
                    path.display()
                );
            }
            continue;
        }
        // Dedup by canonical path (realpath) per SPEC, but WALK the original
        // path. On Windows `fs::canonicalize` returns extended-length `\\?\`
        // verbatim forms — and a mapped network drive (e.g. `Y:`) resolves to
        // a UNC target — which the `glob`-based discovery in transcript.rs
        // cannot enumerate, silently dropping the whole root. The canonical
        // form is only needed to detect two roots pointing at the same place.
        let key = fs::canonicalize(&path)
            .map(|c| c.to_string_lossy().into_owned())
            .unwrap_or_else(|_| path.to_string_lossy().into_owned());
        if seen.insert(key) {
            result.push(path);
        }
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::sync::Mutex;

    // Env vars are process-global; serialize tests that mutate them.
    static ENV_LOCK: Mutex<()> = Mutex::new(());

    /// Run `body` with HOME and USERPROFILE saved, set to the given values
    /// (None => remove), then restored regardless of panic.
    fn with_home_env<F, R>(home: Option<&str>, userprofile: Option<&str>, body: F) -> R
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
        match userprofile {
            Some(v) => std::env::set_var("USERPROFILE", v),
            None => std::env::remove_var("USERPROFILE"),
        }
        let result = body();
        match saved_home {
            Some(v) => std::env::set_var("HOME", v),
            None => std::env::remove_var("HOME"),
        }
        match saved_up {
            Some(v) => std::env::set_var("USERPROFILE", v),
            None => std::env::remove_var("USERPROFILE"),
        }
        result
    }

    #[test]
    fn home_directory_prefers_home_on_unix() {
        // On Linux, HOME is canonical even if USERPROFILE is set.
        with_home_env(Some("/tmp/fakehome"), Some("/tmp/fakeprofile"), || {
            let h = home_directory().unwrap();
            if cfg!(windows) {
                assert_eq!(h, std::ffi::OsString::from("/tmp/fakeprofile"));
            } else {
                assert_eq!(h, std::ffi::OsString::from("/tmp/fakehome"));
            }
        });
    }

    #[test]
    fn home_directory_falls_back_to_userprofile_when_home_unset() {
        // HOME unset on Unix → USERPROFILE secondary kicks in (line 28 else branch).
        with_home_env(None, Some("/tmp/fakeprofile"), || {
            let h = home_directory();
            assert_eq!(h, Some(std::ffi::OsString::from("/tmp/fakeprofile")));
        });
    }

    #[test]
    fn home_directory_returns_none_when_both_env_unset() {
        with_home_env(None, None, || {
            assert!(home_directory().is_none());
        });
    }

    #[test]
    fn walker_config_path_falls_back_when_no_home() {
        // Covers line 35: the None arm of walker_config_path().
        with_home_env(None, None, || {
            let p = walker_config_path();
            assert_eq!(p, PathBuf::from(".claude/walker-roots.json"));
        });
    }

    #[test]
    fn read_extra_roots_returns_empty_when_config_missing() {
        // Covers line 42: config !exists silent path.
        let tmp = tempdir_path("walker-cfg-missing");
        with_home_env(Some(tmp.to_str().unwrap()), None, || {
            // No .claude dir at all → config doesn't exist.
            let v = read_extra_roots_from_config();
            assert!(v.is_empty());
        });
        let _ = fs::remove_dir_all(&tmp);
    }

    #[test]
    fn read_extra_roots_unreadable_returns_empty() {
        // Covers line 46: fs::read_to_string Err branch.
        // Make the config file a directory so open fails with IsADirectory.
        let tmp = tempdir_path("walker-cfg-unreadable");
        let claude_dir = tmp.join(".claude");
        let bogus_config = claude_dir.join("walker-roots.json");
        fs::create_dir_all(&bogus_config).unwrap();
        with_home_env(Some(tmp.to_str().unwrap()), None, || {
            // .exists() returns true for a directory; fs::read_to_string errors.
            let v = read_extra_roots_from_config();
            assert!(v.is_empty());
        });
        let _ = fs::remove_dir_all(&tmp);
    }

    #[test]
    fn read_extra_roots_skips_non_string_elements() {
        // Covers the `element.as_str()` returns None branch (the `else` of
        // the `if let Some(s) = …` at line 77).
        let tmp = tempdir_path("walker-cfg-mixed");
        let claude_dir = tmp.join(".claude");
        fs::create_dir_all(&claude_dir).unwrap();
        let config_path = claude_dir.join("walker-roots.json");
        // "extra_roots" with an integer (non-string), a valid string, an empty
        // string, and a null. Only the valid non-empty string survives.
        fs::write(&config_path, br#"{"extra_roots":[42,"/tmp/x","",null]}"#).unwrap();
        with_home_env(Some(tmp.to_str().unwrap()), None, || {
            let v = read_extra_roots_from_config();
            assert_eq!(v, vec![PathBuf::from("/tmp/x")]);
        });
        let _ = fs::remove_dir_all(&tmp);
    }

    #[test]
    fn tagged_config_parses_known_formats_and_skips_malformed_objects() {
        let tmp = tempdir_path("walker-cfg-tagged");
        let claude_dir = tmp.join(".claude");
        fs::create_dir_all(&claude_dir).unwrap();
        fs::write(
            claude_dir.join("walker-roots.json"),
            br#"{"extra_roots":[{"format":"codex"},{"path":"/bad","format":"unknown"},{"path":"/claude","format":"claude-code"},{"path":"/codex","format":"codex"}]}"#,
        )
        .unwrap();
        with_home_env(Some(tmp.to_str().unwrap()), None, || {
            assert_eq!(
                read_tagged_extra_roots_from_config(),
                vec![
                    TranscriptRoot {
                        path: PathBuf::from("/claude"),
                        format: TranscriptFormat::ClaudeCode
                    },
                    TranscriptRoot {
                        path: PathBuf::from("/codex"),
                        format: TranscriptFormat::Codex
                    },
                ]
            );
        });
        let _ = fs::remove_dir_all(&tmp);
    }

    #[test]
    fn codex_default_and_non_directory_extra_paths() {
        with_home_env(None, None, || {
            assert_eq!(default_codex_root(), PathBuf::from(".codex/sessions"));
        });
        let tmp = tempdir_path("walker-search-roots");
        let primary = tmp.join("primary");
        fs::create_dir(&primary).unwrap();
        let not_directory = tmp.join("not-directory");
        fs::write(&not_directory, b"x").unwrap();
        let roots = resolve_search_roots(Some(primary.clone()), &[not_directory], false);
        assert_eq!(
            roots,
            vec![TranscriptRoot {
                path: primary,
                format: TranscriptFormat::ClaudeCode
            }]
        );
        let _ = fs::remove_dir_all(&tmp);
    }

    fn tempdir_path(suffix: &str) -> PathBuf {
        let mut p = std::env::temp_dir();
        let pid = std::process::id();
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        p.push(format!("rust-walker-test-{suffix}-{pid}-{nanos}"));
        fs::create_dir_all(&p).unwrap();
        p
    }
}
