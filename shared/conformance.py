"""Run each implementation against the conformance corpus, assert agreement.

Usage:
    python shared/conformance.py [<lang> ...]

With no arguments, runs every implementation it can find a binary for.
With explicit args, runs only those (e.g. `python shared/conformance.py rust`).

Discovers binaries via:
    rust  -> rust/target/release/walker(.exe)
    go    -> go/walker(.exe)
    cpp   -> cpp/build/walker(.exe)  or  cpp/build/Release/walker.exe
    zig   -> zig/zig-out/bin/walker(.exe)

For each implementation we run:
    1. Aggregate: full corpus root, expect totals to match expected.json::_aggregate
    2. Per-fixture: each fixture isolated in a temp root, expect totals to match
       expected.json::fixtures[name]. Catches over/under-counting that cancels
       in the aggregate.

Tolerance: ±$0.01 on trailing_usd and window_usd per fixture and aggregate.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Literal, overload

from process_safe import ProcessResult, run_captured

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "shared" / "corpus"
EXPECTED = ROOT / "shared" / "expected.json"
BEACON_CORPUS = ROOT / "shared" / "corpus" / "beacons"
MULTI_ROOT_CORPUS = ROOT / "shared" / "corpus" / "multi_root"
SEARCH_CORPUS = ROOT / "shared" / "corpus" / "search"
SEARCH_MULTI_ROOT_CORPUS = ROOT / "shared" / "corpus" / "search_multi_root"
EVENTS_CORPUS = ROOT / "shared" / "corpus" / "events"
EVENTS_EXPECTED = EVENTS_CORPUS / "expected_events.json"
EXPECTED_LATEST = BEACON_CORPUS / "expected_latest.json"
EXPECTED_HISTORY = BEACON_CORPUS / "expected_history.json"
TOLERANCE = 0.01  # $
BIAS_TOLERANCE = 0.001
EVENTS_USD_TOLERANCE = 1e-4
EVENTS_TS_TOLERANCE = 1e-6

# Hit fields that vary per run (absolute tempdir paths) — strip before compare.
SEARCH_HIT_STRIP_KEYS = {"host_root", "file_path"}
# Summary fields that vary per run — strip before compare.
SEARCH_SUMMARY_STRIP_KEYS = {"elapsed_ms", "files_walked"}

# Impls that have implemented the `search` subcommand. Add languages here as
# their search ports land. Until the set contains an impl, its search check
# is skipped (rather than reported as failure).
IMPLS_WITH_SEARCH: set[str] = {"rust", "cpp", "go", "zig"}

# Impls that have implemented the `events` subcommand. Add languages here as
# their events ports land. Until the set contains an impl, its events check
# is skipped (rather than reported as failure).
IMPLS_WITH_EVENTS: set[str] = {"rust", "cpp", "go", "zig"}

CANDIDATES = {
    "rust": [
        ROOT / "rust" / "target" / "release" / "walker.exe",
        ROOT / "rust" / "target" / "release" / "walker",
    ],
    "go": [
        ROOT / "go" / "walker.exe",
        ROOT / "go" / "walker",
    ],
    "cpp": [
        ROOT / "cpp" / "build" / "Release" / "walker.exe",
        ROOT / "cpp" / "build" / "walker.exe",
        ROOT / "cpp" / "build" / "walker",
    ],
    "zig": [
        ROOT / "zig" / "zig-out" / "bin" / "walker.exe",
        ROOT / "zig" / "zig-out" / "bin" / "walker",
    ],
}


def find_binary(lang: str) -> Path | None:
    # Coverage orchestrator (shared/coverage.py) sets WALKER_BIN_<LANG> to an
    # instrumented build so we can collect coverage without clobbering the
    # release binaries at the default discovery paths.
    override = os.environ.get(f"WALKER_BIN_{lang.upper()}")
    if override:
        path = Path(override)
        return path if path.is_file() else None
    for path in CANDIDATES.get(lang, []):
        if path.is_file():
            return path
    return None


def run_walker(lang: str, binary: Path, meta: dict, projects_root: Path, extras: list[Path] | None = None) -> dict:
    """Run the walker binary against `projects_root`, return parsed JSON output."""
    cmd = [
        str(binary),
        "--period", str(meta["period_seconds"]),
        "--win-start", repr(meta["win_start_unix"]),
        "--now", repr(meta["now_unix"]),
        "--projects-root", str(projects_root),
        "--no-config",
    ]
    for extra in extras or []:
        cmd.extend(["--extra-projects-root", str(extra)])
    result = run_captured(cmd, text=True, encoding="utf-8", timeout=10)
    if result.returncode != 0:
        raise RuntimeError(
            f"{binary.name} exited {result.returncode}\n"
            f"stderr:\n{result.stderr}"
        )
    line = result.stdout.strip().splitlines()[-1]
    return json.loads(line)


def within_tolerance(got: dict, target: dict) -> tuple[bool, float, float]:
    dt = got.get("trailing_usd", 0) - target["trailing_usd"]
    dw = got.get("window_usd", 0) - target["window_usd"]
    ok = abs(dt) <= TOLERANCE and abs(dw) <= TOLERANCE
    return ok, dt, dw


def check_aggregate(lang: str, binary: Path, expected: dict) -> bool:
    meta = expected["_meta"]
    target = expected["_aggregate"]
    try:
        got = run_walker(lang, binary, meta, CORPUS)
    except Exception as e:
        print(f"  [{lang:>4s}] aggregate    FAIL  {e}")
        return False
    ok, dt, dw = within_tolerance(got, target)
    badge = " OK " if ok else "FAIL"
    print(
        f"  [{lang:>4s}] aggregate    {badge}  "
        f"trailing=${got.get('trailing_usd', 0):.6f} (d=${dt:+.6f})  "
        f"window=${got.get('window_usd', 0):.6f} (d=${dw:+.6f})  "
        f"{got.get('elapsed_ms', '?')}ms"
    )
    return ok


def check_fixture(
    lang: str, binary: Path, meta: dict, fixture_name: str, target: dict
) -> bool:
    """Run the binary against just one fixture in an isolated temp root."""
    with tempfile.TemporaryDirectory(prefix="walker-fixture-") as tmp:
        # Recreate <tmp>/<fixture_name>/... from <CORPUS>/<fixture_name>/...
        # Walker's discovery expects <root>/<slug>/<session>.jsonl, with the
        # fixture directory acting as the slug.
        shutil.copytree(CORPUS / fixture_name, Path(tmp) / fixture_name)
        try:
            got = run_walker(lang, binary, meta, Path(tmp))
        except Exception as e:
            print(f"  [{lang:>4s}] {fixture_name:20s} FAIL  {e}")
            return False
    ok, dt, dw = within_tolerance(got, target)
    badge = " OK " if ok else "FAIL"
    print(
        f"  [{lang:>4s}] {fixture_name:20s} {badge}  "
        f"trailing=${got.get('trailing_usd', 0):.6f} (d=${dt:+.6f})  "
        f"window=${got.get('window_usd', 0):.6f} (d=${dw:+.6f})"
    )
    return ok


def check_implementation(lang: str, binary: Path, expected: dict) -> bool:
    aggregate_ok = check_aggregate(lang, binary, expected)
    fixtures_ok = True
    for name, target in expected["fixtures"].items():
        if not check_fixture(lang, binary, expected["_meta"], name, target):
            fixtures_ok = False
    return aggregate_ok and fixtures_ok


def run_walker_subcommand(lang: str, binary: Path, subcommand: str, args: list[str]) -> dict:
    """Invoke a walker subcommand, return parsed JSON of last stdout line."""
    cmd = [str(binary), subcommand, *args, "--no-config"]
    result = run_captured(cmd, text=True, encoding="utf-8", timeout=10)
    if result.returncode != 0:
        raise RuntimeError(
            f"{binary.name} {subcommand} exited {result.returncode}\n"
            f"stderr:\n{result.stderr}"
        )
    line = result.stdout.strip().splitlines()[-1]
    return json.loads(line)


def assert_beacons_latest(lang: str, binary: Path, expected: dict) -> bool:
    """Run beacons-latest against each scenario in isolation."""
    meta = expected["_meta"]
    overall = True
    for scenario, target in expected["fixtures"].items():
        with tempfile.TemporaryDirectory(prefix="walker-beacon-latest-") as tmp:
            shutil.copytree(BEACON_CORPUS / scenario, Path(tmp) / scenario)
            label = f"beacons-latest:{scenario}"
            try:
                got = run_walker_subcommand(lang, binary, "beacons-latest", [
                    "--session-id", target["session_id"],
                    "--projects-root", str(tmp),
                    "--now", repr(meta["now_unix"]),
                ])
            except Exception as e:
                print(f"  [{lang:>4s}] {label:38s} FAIL  {e}")
                overall = False
                continue
        got_cmp = {
            "beacon": got.get("beacon"),
            "emitted_at": got.get("emitted_at"),
            "age_seconds": got.get("age_seconds"),
        }
        target_cmp = {
            "beacon": target.get("beacon"),
            "emitted_at": target.get("emitted_at"),
            "age_seconds": target.get("age_seconds"),
        }
        ok = got_cmp == target_cmp
        badge = " OK " if ok else "FAIL"
        print(f"  [{lang:>4s}] {label:38s} {badge}")
        if not ok:
            print(f"        got:    {got_cmp}")
            print(f"        target: {target_cmp}")
            overall = False
    return overall


def assert_beacons_history(lang: str, binary: Path, expected: dict) -> bool:
    """Run beacons-history against each history scenario in isolation."""
    meta = expected["_meta"]
    overall = True

    def pairs_key(p):
        return (p["begin_eta"], p["actual_elapsed"])

    for scenario, target in expected["fixtures"].items():
        label = f"beacons-history:{scenario}"
        with tempfile.TemporaryDirectory(prefix="walker-beacon-history-") as tmp:
            # Each scenario dir is a projects-root containing slug subdirs.
            tree = Path(tmp) / "tree"
            shutil.copytree(BEACON_CORPUS / scenario, tree)
            try:
                got = run_walker_subcommand(lang, binary, "beacons-history", [
                    "--period", "604800",  # 7 days, generous
                    "--win-start", "0",
                    "--projects-root", str(tree),
                    "--now", repr(meta["now_unix"]),
                ])
            except Exception as e:
                print(f"  [{lang:>4s}] {label:38s} FAIL  {e}")
                overall = False
                continue

        pairs_ok = sorted(map(pairs_key, got.get("pairs", []))) == sorted(
            map(pairs_key, target["pairs"])
        )
        counts_ok = (
            got.get("session_count") == target["session_count"]
            and got.get("n_pairs") == target["n_pairs"]
        )
        bias_got = got.get("bias_factor")
        bias_tgt = target.get("bias_factor")
        if bias_got is None and bias_tgt is None:
            bias_ok = True
        elif bias_got is None or bias_tgt is None:
            bias_ok = False
        else:
            bias_ok = abs(bias_got - bias_tgt) <= BIAS_TOLERANCE
        ok = pairs_ok and counts_ok and bias_ok
        badge = " OK " if ok else "FAIL"
        print(
            f"  [{lang:>4s}] {label:38s} {badge}  "
            f"n_pairs={got.get('n_pairs')}  bias={bias_got}"
        )
        if not ok:
            print(f"        got:    {got}")
            print(f"        target: {target}")
            overall = False
    return overall


def check_beacons(lang: str, binary: Path) -> bool:
    """Run all beacon-mode assertions if expected files are present."""
    if not EXPECTED_LATEST.is_file() or not EXPECTED_HISTORY.is_file():
        print(f"  [{lang:>4s}] beacon expected files missing -- skipping beacon assertions")
        return True
    expected_latest = json.loads(EXPECTED_LATEST.read_text(encoding="utf-8"))
    expected_history = json.loads(EXPECTED_HISTORY.read_text(encoding="utf-8"))
    latest_ok = assert_beacons_latest(lang, binary, expected_latest)
    history_ok = assert_beacons_history(lang, binary, expected_history)
    return latest_ok and history_ok


def check_multi_root(lang: str, binary: Path) -> bool:
    """Run each multi-root scenario; assert binary sums match expected.json."""
    if not MULTI_ROOT_CORPUS.is_dir():
        return True  # no scenarios — skip cleanly
    all_ok = True
    for scenario_dir in sorted(MULTI_ROOT_CORPUS.iterdir()):
        if not scenario_dir.is_dir():
            continue
        expected_file = scenario_dir / "expected.json"
        if not expected_file.is_file():
            continue
        data = json.loads(expected_file.read_text())
        meta = data["_meta"]
        primary = scenario_dir / data["primary_root"]
        extras = [scenario_dir / r for r in data["extra_roots"]]
        try:
            got = run_walker(lang, binary, meta, primary, extras=extras)
        except Exception as e:
            print(f"  [{lang:>4s}] {scenario_dir.name:<22s} FAIL  {e}")
            all_ok = False
            continue
        target = data["expected"]
        ok, dt, dw = within_tolerance(got, target)
        badge = " OK " if ok else "FAIL"
        print(
            f"  [{lang:>4s}] {scenario_dir.name:<22s} {badge}  "
            f"trailing=${got.get('trailing_usd', 0):.6f} (d=${dt:+.6f})  "
            f"window=${got.get('window_usd', 0):.6f} (d=${dw:+.6f})"
        )
        if not ok:
            all_ok = False
    return all_ok


def run_walker_search(
    lang: str,
    binary: Path,
    projects_root: Path,
    pattern: str,
    flags: list[str],
    now_unix: float,
    extras: list[Path] | None = None,
) -> tuple[list[dict], dict | None]:
    """Invoke `walker search <pattern> <flags> --format jsonl ...`.

    `extras` adds `--extra-projects-root <path>` flags (the multi-root case).
    Returns (hits, summary). Hits are stripped of per-run path fields
    (host_root, file_path); summary is stripped of elapsed_ms / files_walked.
    Raises RuntimeError on non-zero exit.
    """
    cmd = [
        str(binary), "search", pattern,
        "--projects-root", str(projects_root),
        "--now", repr(now_unix),
        "--format", "jsonl",
        "--no-config",
    ]
    for extra in extras or []:
        cmd.extend(["--extra-projects-root", str(extra)])
    cmd.extend(flags)
    result = run_captured(cmd, text=True, encoding="utf-8", timeout=10)
    if result.returncode != 0:
        raise RuntimeError(
            f"{binary.name} search exited {result.returncode}\n"
            f"cmd: {' '.join(cmd)}\n"
            f"stderr:\n{result.stderr}"
        )
    hits: list[dict] = []
    summary: dict | None = None
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"non-JSON line on stdout: {line!r} ({e})")
        kind = obj.get("type")
        if kind == "hit":
            for key in SEARCH_HIT_STRIP_KEYS:
                obj.pop(key, None)
            hits.append(obj)
        elif kind == "summary":
            for key in SEARCH_SUMMARY_STRIP_KEYS:
                obj.pop(key, None)
            summary = obj
        else:
            raise RuntimeError(f"unknown record type on stdout: {obj!r}")
    return hits, summary


def assert_search_combo(
    lang: str,
    binary: Path,
    scenario_name: str,
    combo_name: str,
    combo: dict,
    now_unix: float,
) -> bool:
    """Run one (scenario, combo) and structurally compare to expected output."""
    label = f"search/{scenario_name}/{combo_name}"
    with tempfile.TemporaryDirectory(prefix=f"walker-search-{scenario_name}-") as tmp:
        shutil.copytree(SEARCH_CORPUS / scenario_name, Path(tmp) / scenario_name)
        try:
            got_hits, got_summary = run_walker_search(
                lang, binary, Path(tmp),
                combo["pattern"], combo["flags"], now_unix,
            )
        except Exception as e:
            print(f"  [{lang:>4s}] {label:48s} FAIL  {e}")
            return False

    exp_hits = combo["hits"]
    exp_summary = combo["summary"]
    hits_ok = got_hits == exp_hits
    summary_ok = got_summary == exp_summary
    ok = hits_ok and summary_ok
    badge = " OK " if ok else "FAIL"
    print(f"  [{lang:>4s}] {label:48s} {badge}  hits={len(got_hits)}")
    if not hits_ok:
        print(f"        hits mismatch: got {len(got_hits)}, expected {len(exp_hits)}")
        for i in range(max(len(got_hits), len(exp_hits))):
            g = got_hits[i] if i < len(got_hits) else None
            e = exp_hits[i] if i < len(exp_hits) else None
            if g != e:
                print(f"          hit[{i}] got:      {json.dumps(g, sort_keys=True)}")
                print(f"          hit[{i}] expected: {json.dumps(e, sort_keys=True)}")
                break
    if not summary_ok:
        print(f"        summary mismatch:")
        print(f"          got:      {json.dumps(got_summary, sort_keys=True)}")
        print(f"          expected: {json.dumps(exp_summary, sort_keys=True)}")
    return ok


def check_search(lang: str, binary: Path) -> bool:
    """Run every scenario/combo in shared/corpus/search/."""
    if not SEARCH_CORPUS.is_dir():
        return True  # corpus missing -- nothing to test
    if lang not in IMPLS_WITH_SEARCH:
        print(f"  [{lang:>4s}] search subcommand -- skipping (not in IMPLS_WITH_SEARCH)")
        return True
    all_ok = True
    for scenario_dir in sorted(SEARCH_CORPUS.iterdir()):
        if not scenario_dir.is_dir():
            continue
        expected_file = scenario_dir / "expected.json"
        if not expected_file.is_file():
            continue
        data = json.loads(expected_file.read_text(encoding="utf-8"))
        now_unix = data["_meta"]["now_unix"]
        for combo_name, combo in data["combos"].items():
            if not assert_search_combo(
                lang, binary, scenario_dir.name, combo_name, combo, now_unix,
            ):
                all_ok = False
    return all_ok


def run_walker_search_pretty(
    binary: Path,
    projects_root: Path,
    pattern: str,
    flags: list[str],
    now_unix: float,
) -> tuple[int, str, str]:
    """Invoke `walker search` with `--format pretty`, return (returncode, stdout, stderr).

    Does not raise on non-zero exit (the caller asserts on exit + output).
    """
    cmd = [
        str(binary), "search", pattern,
        "--projects-root", str(projects_root),
        "--now", repr(now_unix),
        "--format", "pretty",
        "--no-config",
    ]
    cmd.extend(flags)
    result = run_captured(cmd, text=True, encoding="utf-8", timeout=10)
    return result.returncode, result.stdout, result.stderr


# --- Pretty-format assertions ---
#
# SPEC "Pretty format" (decided 2026-06-10) pins the renderer across impls:
# the highlight line is exactly `  >>> <pre>[<match>]<post> <<<` built from
# the first match_offsets pair, and the stream ends with ONE human-readable
# summary line (never a JSONL record). These assertions check the exact
# highlight line per hit and the exact summary line (files/elapsed counts
# vary per run, so those two fields are wildcarded).

def _count_hit_headers(stdout: str) -> int:
    """A hit header looks like `[<timestamp>] cwd=... role=... session=...`."""
    count = 0
    for line in stdout.splitlines():
        stripped = line.lstrip()
        if (stripped.startswith("[") and "cwd=" in stripped
                and "role=" in stripped and "session=" in stripped):
            count += 1
    return count


def assert_search_pretty(
    lang: str,
    binary: Path,
    scenario_name: str,
    combo_name: str,
    combo: dict,
    now_unix: float,
) -> bool:
    """Structurally check pretty-format output against the same combo as jsonl.

    Asserts (cross-impl shared contract):
      - exit 0 (no errors)
      - one hit header line per expected hit
      - the `>>>` and `<<<` highlight delimiters appear once per hit
      - each expected hit's `match_offsets[0]` substring appears as `[<match>]`
        somewhere in stdout
      - `before:` and `after:` line totals match summed expected context-turn
        counts (per impl, when there are context turns)
    """
    label = f"search-pretty/{scenario_name}/{combo_name}"
    expected_hits = combo["hits"]
    expected_summary = combo["summary"]
    with tempfile.TemporaryDirectory(prefix=f"walker-search-pretty-{scenario_name}-") as tmp:
        shutil.copytree(SEARCH_CORPUS / scenario_name, Path(tmp) / scenario_name)
        returncode, stdout, stderr = run_walker_search_pretty(
            binary, Path(tmp),
            combo["pattern"], combo["flags"], now_unix,
        )
    if returncode != 0:
        print(f"  [{lang:>4s}] {label:48s} FAIL  exit={returncode} stderr={stderr!r}")
        return False

    # If count_only suppresses hits, expect 0 headers and 0 highlight markers.
    suppress_hits = "--count-only" in combo["flags"]
    expected_hit_count = 0 if suppress_hits else len(expected_hits)

    got_headers = _count_hit_headers(stdout)
    got_open = stdout.count(">>>")
    got_close = stdout.count("<<<")

    problems: list[str] = []
    if got_headers != expected_hit_count:
        problems.append(f"header lines: got {got_headers}, expected {expected_hit_count}")
    if got_open != expected_hit_count:
        problems.append(f">>> count: got {got_open}, expected {expected_hit_count}")
    if got_close != expected_hit_count:
        problems.append(f"<<< count: got {got_close}, expected {expected_hit_count}")

    # Each hit must render the SPEC-exact highlight line, built from the
    # first match_offsets pair (byte offsets into the snippet).
    if not suppress_hits:
        stdout_lines = stdout.splitlines()
        for index, hit in enumerate(expected_hits):
            offsets = hit.get("match_offsets") or []
            snippet = hit.get("snippet", "")
            if not offsets:
                continue
            start, end = offsets[0]
            snippet_bytes = snippet.encode("utf-8")
            expected_line = "  >>> {}[{}]{} <<<".format(
                snippet_bytes[:start].decode("utf-8"),
                snippet_bytes[start:end].decode("utf-8"),
                snippet_bytes[end:].decode("utf-8"),
            )
            if expected_line not in stdout_lines:
                problems.append(
                    f"hit[{index}] missing exact highlight line {expected_line!r}")
                break

        # before:/after: line counts (only meaningful when --context > 0).
        expected_before = sum(len(h.get("context_before", [])) for h in expected_hits)
        expected_after = sum(len(h.get("context_after", [])) for h in expected_hits)
        got_before = sum(1 for line in stdout.splitlines()
                         if line.lstrip().startswith("before:"))
        got_after = sum(1 for line in stdout.splitlines()
                        if line.lstrip().startswith("after:"))
        if got_before != expected_before:
            problems.append(f"before: lines: got {got_before}, expected {expected_before}")
        if got_after != expected_after:
            problems.append(f"after: lines: got {got_after}, expected {expected_after}")

    # The stream must end with the SPEC-exact summary line (files walked and
    # elapsed ms vary per run; hits/sessions/roots/truncated are pinned).
    expected_hit_total = expected_summary.get("hits", len(expected_hits))
    summary_re = re.compile(
        r"^{hits} hits in {sessions} sessions across {roots} roots "
        r"\(\d+ files\)\. truncated={trunc} elapsed \d+ms\.$".format(
            hits=expected_hit_total,
            sessions=expected_summary.get("sessions_matched", 0),
            roots=expected_summary.get("roots_walked", 1),
            trunc="true" if expected_summary.get("truncated") else "false",
        ))
    final_lines = [line for line in stdout.splitlines() if line.strip()]
    if not final_lines or not summary_re.match(final_lines[-1]):
        problems.append(
            f"missing SPEC summary line matching {summary_re.pattern!r}; "
            f"last line: {final_lines[-1]!r}" if final_lines else
            "empty pretty stdout")

    if problems:
        print(f"  [{lang:>4s}] {label:48s} FAIL  " + "; ".join(problems))
        return False
    print(f"  [{lang:>4s}] {label:48s}  OK   hits={got_headers}")
    return True


# Pretty-format check uses a curated subset of search scenarios (not every
# combo) — the goal is to exercise the renderer's paths (header, file-info,
# before/after, highlight, count-only suppression), not to re-prove jsonl-vs-
# expected agreement. The jsonl path in check_search already does that.
PRETTY_SCENARIOS: list[tuple[str, str]] = [
    # (scenario, combo) — chosen to cover: no-context, before-only,
    # before+after, after-only, count-only suppression, and long-context
    # ellipsis truncation (25's context turns exceed the 120-char preview).
    ("01-basic", "default"),
    ("02-multi-match-per-session", "default"),
    ("09-count-only", "count-only"),
    ("25-context-rich", "default"),
]


def check_search_pretty(lang: str, binary: Path) -> bool:
    """Drive the pretty renderer across a curated subset of search fixtures."""
    if not SEARCH_CORPUS.is_dir():
        return True
    if lang not in IMPLS_WITH_SEARCH:
        print(f"  [{lang:>4s}] search pretty -- skipping (not in IMPLS_WITH_SEARCH)")
        return True
    all_ok = True
    for scenario_name, combo_name in PRETTY_SCENARIOS:
        expected_file = SEARCH_CORPUS / scenario_name / "expected.json"
        if not expected_file.is_file():
            continue
        data = json.loads(expected_file.read_text(encoding="utf-8"))
        combo = data["combos"].get(combo_name)
        if combo is None:
            continue
        now_unix = data["_meta"]["now_unix"]
        if not assert_search_pretty(
            lang, binary, scenario_name, combo_name, combo, now_unix,
        ):
            all_ok = False
    return all_ok


# --- Result truncation (gap A2) ---
#
# A multi-hit fixture invoked with `--limit 1` exercises:
#   - jsonl: summary record `"truncated": true`
#   - pretty: `truncated=true` token (rust/zig) or `"truncated":true` (cpp/go)
#   - stderr: the `walker: search: truncated to --limit=N (had M total)` warning
#     — text is identical across all four impls (confirmed empirically).

TRUNCATION_STDERR_TOKEN = "walker: search: truncated to --limit="


def assert_search_truncated(lang: str, binary: Path) -> bool:
    """Run a multi-hit fixture with `--limit 1` and verify truncation signals."""
    scenario_name = "01-basic"  # 3 hits across 3 sessions
    label = f"search-truncated/{scenario_name}/limit1"
    expected_file = SEARCH_CORPUS / scenario_name / "expected.json"
    if not expected_file.is_file():
        # Corpus missing — silently pass; check_search would also skip.
        return True
    data = json.loads(expected_file.read_text(encoding="utf-8"))
    now_unix = data["_meta"]["now_unix"]
    base_combo = data["combos"]["default"]
    total_hits = len(base_combo["hits"])
    if total_hits < 2:
        print(f"  [{lang:>4s}] {label:48s} SKIP  base fixture has <2 hits")
        return True

    # --- jsonl path ---
    with tempfile.TemporaryDirectory(prefix=f"walker-search-trunc-jsonl-") as tmp:
        shutil.copytree(SEARCH_CORPUS / scenario_name, Path(tmp) / scenario_name)
        cmd = [
            str(binary), "search", base_combo["pattern"],
            "--projects-root", str(tmp),
            "--now", repr(now_unix),
            "--format", "jsonl",
            "--no-config",
            "--limit", "1",
        ]
        result = run_captured(cmd, text=True, encoding="utf-8", timeout=10)
    if result.returncode != 0:
        print(f"  [{lang:>4s}] {label:48s} FAIL  jsonl exit={result.returncode} stderr={result.stderr!r}")
        return False
    # Parse jsonl: exactly 1 hit + 1 summary, summary truncated=true, hits=1.
    hits = []
    summary = None
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            print(f"  [{lang:>4s}] {label:48s} FAIL  jsonl non-JSON line: {line!r} ({e})")
            return False
        if obj.get("type") == "hit":
            hits.append(obj)
        elif obj.get("type") == "summary":
            summary = obj
    problems: list[str] = []
    if len(hits) != 1:
        problems.append(f"jsonl hits: got {len(hits)}, expected 1")
    if summary is None:
        problems.append("jsonl: missing summary record")
    else:
        if not summary.get("truncated"):
            problems.append(f"jsonl summary.truncated: got {summary.get('truncated')!r}, expected true")
        if summary.get("hits") != 1:
            problems.append(f"jsonl summary.hits: got {summary.get('hits')!r}, expected 1")
    if TRUNCATION_STDERR_TOKEN not in result.stderr:
        problems.append(f"jsonl stderr missing {TRUNCATION_STDERR_TOKEN!r}")

    # --- pretty path ---
    with tempfile.TemporaryDirectory(prefix=f"walker-search-trunc-pretty-") as tmp:
        shutil.copytree(SEARCH_CORPUS / scenario_name, Path(tmp) / scenario_name)
        returncode, stdout, stderr = run_walker_search_pretty(
            binary, Path(tmp),
            base_combo["pattern"], ["--limit", "1"], now_unix,
        )
    if returncode != 0:
        print(f"  [{lang:>4s}] {label:48s} FAIL  pretty exit={returncode} stderr={stderr!r}")
        return False
    if "truncated=true" not in stdout:
        problems.append("pretty stdout missing truncated=true marker")
    if TRUNCATION_STDERR_TOKEN not in stderr:
        problems.append(f"pretty stderr missing {TRUNCATION_STDERR_TOKEN!r}")
    # exactly 1 hit rendered in pretty (suppressed when count-only, but we
    # didn't pass count-only here, so a single hit header must appear).
    got_headers = _count_hit_headers(stdout)
    if got_headers != 1:
        problems.append(f"pretty hit headers: got {got_headers}, expected 1")

    if problems:
        print(f"  [{lang:>4s}] {label:48s} FAIL  " + "; ".join(problems))
        return False
    print(f"  [{lang:>4s}] {label:48s}  OK   jsonl+pretty+stderr truncation signals present")
    return True


def check_search_truncated(lang: str, binary: Path) -> bool:
    """Wrapper to match the check_* naming pattern."""
    if not SEARCH_CORPUS.is_dir():
        return True
    if lang not in IMPLS_WITH_SEARCH:
        print(f"  [{lang:>4s}] search truncated -- skipping (not in IMPLS_WITH_SEARCH)")
        return True
    return assert_search_truncated(lang, binary)


def check_search_multi_root(lang: str, binary: Path) -> bool:
    """Multi-root search: assert --extra-projects-root reaches a second root,
    hits aggregate + sort across roots, and roots_walked reflects the count.

    Regression guard for the cpp perf-pass-2 search rewrite, which dropped root
    resolution (ignored --extra-projects-root / walker-roots.json, hardcoded
    roots_walked=1). rust/go/zig already pass this. Runs read-only directly
    against the corpus (search never writes), mirroring check_multi_root.
    """
    if not SEARCH_MULTI_ROOT_CORPUS.is_dir():
        return True  # corpus missing -- nothing to test
    if lang not in IMPLS_WITH_SEARCH:
        print(f"  [{lang:>4s}] search multi-root -- skipping (not in IMPLS_WITH_SEARCH)")
        return True
    all_ok = True
    for scenario_dir in sorted(SEARCH_MULTI_ROOT_CORPUS.iterdir()):
        if not scenario_dir.is_dir():
            continue
        expected_file = scenario_dir / "expected.json"
        if not expected_file.is_file():
            continue
        data = json.loads(expected_file.read_text(encoding="utf-8"))
        now_unix = data["_meta"]["now_unix"]
        primary = scenario_dir / data["primary_root"]
        extras = [scenario_dir / r for r in data["extra_roots"]]
        for combo_name, combo in data["combos"].items():
            label = f"search-mr/{scenario_dir.name}/{combo_name}"
            try:
                got_hits, got_summary = run_walker_search(
                    lang, binary, primary,
                    combo["pattern"], combo["flags"], now_unix, extras=extras,
                )
            except Exception as e:
                print(f"  [{lang:>4s}] {label:48s} FAIL  {e}")
                all_ok = False
                continue
            hits_ok = got_hits == combo["hits"]
            summary_ok = got_summary == combo["summary"]
            ok = hits_ok and summary_ok
            badge = " OK " if ok else "FAIL"
            walked = got_summary.get("roots_walked") if got_summary else "?"
            print(f"  [{lang:>4s}] {label:48s} {badge}  hits={len(got_hits)} roots_walked={walked}")
            if not hits_ok:
                print(f"        hits mismatch: got {len(got_hits)}, expected {len(combo['hits'])}")
                for i in range(max(len(got_hits), len(combo["hits"]))):
                    g = got_hits[i] if i < len(got_hits) else None
                    e = combo["hits"][i] if i < len(combo["hits"]) else None
                    if g != e:
                        print(f"          hit[{i}] got:      {json.dumps(g, sort_keys=True)}")
                        print(f"          hit[{i}] expected: {json.dumps(e, sort_keys=True)}")
                        break
            if not summary_ok:
                print(f"        summary got:      {json.dumps(got_summary, sort_keys=True)}")
                print(f"        summary expected: {json.dumps(combo['summary'], sort_keys=True)}")
            if not ok:
                all_ok = False
    return all_ok


def check_search_duplicate_root(lang: str, binary: Path) -> bool:
    """A20: duplicate-root dedup. Passing the same path via both
    `--projects-root` AND `--extra-projects-root` must produce the SAME hits +
    summary as a single-root invocation (no double-counted hits, roots_walked
    still 1). All four impls canonicalize each root via realpath (Rust
    `fs::canonicalize`, C++ `fs::canonical`, Go `filepath.EvalSymlinks`, Zig
    `realpath`/path-clean) and dedup via the canonical key before walking.

    We exercise the dedup path by:
      1) running search against a single root P and capturing baseline output;
      2) running the SAME pattern + flags against `--projects-root P
         --extra-projects-root P` and asserting identical hits + summary
         (including roots_walked=1).

    Reuses the 01-basic scenario (3 hits, 3 sessions) for the substrate.
    """
    if not SEARCH_CORPUS.is_dir():
        return True
    if lang not in IMPLS_WITH_SEARCH:
        print(f"  [{lang:>4s}] search dedup -- skipping (not in IMPLS_WITH_SEARCH)")
        return True
    scenario_name = "01-basic"
    expected_file = SEARCH_CORPUS / scenario_name / "expected.json"
    if not expected_file.is_file():
        return True
    data = json.loads(expected_file.read_text(encoding="utf-8"))
    now_unix = data["_meta"]["now_unix"]
    combo = data["combos"]["default"]
    label = f"search-dedup/{scenario_name}/duplicate-root"

    with tempfile.TemporaryDirectory(prefix=f"walker-search-dedup-") as tmp:
        tmp_path = Path(tmp)
        shutil.copytree(SEARCH_CORPUS / scenario_name, tmp_path / scenario_name)
        try:
            baseline_hits, baseline_summary = run_walker_search(
                lang, binary, tmp_path,
                combo["pattern"], combo["flags"], now_unix,
            )
            # Pass the SAME root via --extra-projects-root. Both args resolve
            # to the same canonical inode -> dedup must drop the duplicate.
            dup_hits, dup_summary = run_walker_search(
                lang, binary, tmp_path,
                combo["pattern"], combo["flags"], now_unix,
                extras=[tmp_path],
            )
        except Exception as e:
            print(f"  [{lang:>4s}] {label:48s} FAIL  {e}")
            return False

    problems: list[str] = []
    if dup_hits != baseline_hits:
        problems.append(
            f"hits diverged from baseline: got {len(dup_hits)} vs baseline {len(baseline_hits)}"
        )
    if dup_summary != baseline_summary:
        problems.append(
            f"summary diverged: got {dup_summary!r} vs baseline {baseline_summary!r}"
        )
    # Explicit: roots_walked MUST be 1, not 2 (dedup pre-walk).
    walked = (dup_summary or {}).get("roots_walked")
    if walked != 1:
        problems.append(f"roots_walked: got {walked!r}, expected 1 (dedup pre-walk)")

    if problems:
        print(f"  [{lang:>4s}] {label:48s} FAIL  " + "; ".join(problems))
        return False
    print(
        f"  [{lang:>4s}] {label:48s}  OK   hits={len(dup_hits)} "
        f"roots_walked={walked}"
    )
    return True


def _events_sort_key(record: dict) -> tuple:
    """Stable sort key for events-mode NDJSON records (multiset comparison)."""
    return (
        record.get("ts", 0.0),
        record.get("session_id", ""),
        record.get("model", ""),
    )


def check_events(lang: str, binary: Path) -> bool:
    """Run events subcommand against each fixture; compare NDJSON output to expected."""
    if not EVENTS_EXPECTED.is_file():
        print(f"  [{lang:>4s}] events expected file missing -- skipping events assertions")
        return True
    if lang not in IMPLS_WITH_EVENTS:
        print(f"  [{lang:>4s}] events subcommand -- skipping (not in IMPLS_WITH_EVENTS)")
        return True

    expected_data = json.loads(EVENTS_EXPECTED.read_text(encoding="utf-8"))
    pin_now = expected_data["pin_now"]
    pin_period = expected_data["pin_period"]
    pin_win_start = expected_data["pin_win_start"]
    all_ok = True

    for fixture_name, target_records in sorted(expected_data["fixtures"].items()):
        fixture_root = EVENTS_CORPUS / fixture_name
        if not fixture_root.is_dir():
            print(f"  [{lang:>4s}] events/{fixture_name:20s} FAIL  fixture dir missing")
            all_ok = False
            continue

        label = f"events/{fixture_name}"
        cmd = [
            str(binary), "events",
            "--period", repr(pin_period),
            "--win-start", repr(pin_win_start),
            "--projects-root", str(fixture_root),
            "--now", repr(pin_now),
            "--no-config",
        ]
        result = run_captured(cmd, text=True, encoding="utf-8", timeout=10)
        if result.returncode != 0:
            print(
                f"  [{lang:>4s}] {label:30s} FAIL  "
                f"exit {result.returncode}: {result.stderr.strip()[:120]}"
            )
            all_ok = False
            continue

        # Parse NDJSON output — one JSON object per non-blank line.
        got_records: list[dict] = []
        parse_error: str | None = None
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            try:
                got_records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                parse_error = f"non-JSON line: {line!r} ({exc})"
                break

        if parse_error:
            print(f"  [{lang:>4s}] {label:30s} FAIL  {parse_error}")
            all_ok = False
            continue

        # Sort both lists by (ts, session_id, model) for stable multiset comparison.
        got_sorted = sorted(got_records, key=_events_sort_key)
        target_sorted = sorted(target_records, key=_events_sort_key)

        if len(got_sorted) != len(target_sorted):
            print(
                f"  [{lang:>4s}] {label:30s} FAIL  "
                f"length mismatch: got {len(got_sorted)}, expected {len(target_sorted)}"
            )
            all_ok = False
            continue

        first_diff: str | None = None
        records_ok = True
        for index, (got, target) in enumerate(zip(got_sorted, target_sorted)):
            ts_ok = abs(got.get("ts", 0.0) - target.get("ts", 0.0)) <= EVENTS_TS_TOLERANCE
            usd_ok = abs(got.get("usd", 0.0) - target.get("usd", 0.0)) <= EVENTS_USD_TOLERANCE
            fields_ok = (
                got.get("model") == target.get("model")
                and got.get("session_id") == target.get("session_id")
                and got.get("slug") == target.get("slug")
            )
            if not (ts_ok and usd_ok and fields_ok):
                records_ok = False
                first_diff = (
                    f"record[{index}] mismatch: "
                    f"got {json.dumps(got, sort_keys=True)} "
                    f"expected {json.dumps(target, sort_keys=True)}"
                )
                break

        badge = " OK " if records_ok else "FAIL"
        print(
            f"  [{lang:>4s}] {label:30s} {badge}  "
            f"records={len(got_sorted)}"
        )
        if first_diff:
            print(f"        {first_diff}")
            all_ok = False

    return all_ok


def check_empty_root(lang: str, binary: Path, expected: dict) -> bool:
    """A17: every mode pointed at a nonexistent --projects-root must exit 0
    with empty/zero output (no crash, no stderr noise that consumers
    misinterpret as a fault). Covers cost / events / beacons-latest /
    beacons-history / search.

    The harness already exercises the *existing-but-empty* root case (the
    `04-empty` cost fixture), but never the *nonexistent* path. The
    discovery layer must skip a non-existent root silently per SPEC.md
    §Roots: "Non-existent extras are skipped silently with a stderr
    diagnostic. ... walker must keep going."
    """
    meta = expected["_meta"]
    all_ok = True

    with tempfile.TemporaryDirectory(prefix="walker-empty-root-") as tmp:
        # A path that DOES NOT EXIST under tmp.
        missing = Path(tmp) / "does-not-exist"
        common_args = [
            "--projects-root", str(missing),
            "--no-config",
            "--now", repr(meta["now_unix"]),
        ]

        # 1. cost mode (bare flags) — JSON with zero totals, exit 0.
        cmd = [str(binary),
               "--period", str(meta["period_seconds"]),
               "--win-start", repr(meta["win_start_unix"]),
               *common_args]
        result = run_captured(cmd, text=True,
                                encoding="utf-8", timeout=10)
        cost_ok = result.returncode == 0
        if cost_ok:
            try:
                line = result.stdout.strip().splitlines()[-1]
                payload = json.loads(line)
                cost_ok = (
                    abs(payload.get("trailing_usd", 1) - 0.0) < TOLERANCE
                    and abs(payload.get("window_usd", 1) - 0.0) < TOLERANCE
                )
            except Exception:
                cost_ok = False
        print(f"  [{lang:>4s}] {'empty-root: cost':38s} "
              f"{' OK ' if cost_ok else 'FAIL'}")
        if not cost_ok:
            print(f"        exit={result.returncode} stdout={result.stdout!r}")
            all_ok = False

        # 2. events — exit 0, no NDJSON records (empty stdout).
        cmd = [str(binary), "events",
               "--period", str(meta["period_seconds"]),
               "--win-start", repr(meta["win_start_unix"]),
               *common_args]
        result = run_captured(cmd, text=True,
                                encoding="utf-8", timeout=10)
        # events allows empty stdout per SPEC §events ("Exit 0 even when
        # the output stream is empty.").
        events_ok = result.returncode == 0 and all(
            not line.strip() for line in result.stdout.splitlines()
        )
        print(f"  [{lang:>4s}] {'empty-root: events':38s} "
              f"{' OK ' if events_ok else 'FAIL'}")
        if not events_ok:
            print(f"        exit={result.returncode} stdout={result.stdout!r}")
            all_ok = False

        # 3. beacons-latest — exit 0, beacon = null.
        cmd = [str(binary), "beacons-latest",
               "--session-id", "no-such-session",
               *common_args]
        result = run_captured(cmd, text=True,
                                encoding="utf-8", timeout=10)
        bl_ok = result.returncode == 0
        if bl_ok:
            try:
                line = result.stdout.strip().splitlines()[-1]
                payload = json.loads(line)
                bl_ok = payload.get("beacon") is None
            except Exception:
                bl_ok = False
        print(f"  [{lang:>4s}] {'empty-root: beacons-latest':38s} "
              f"{' OK ' if bl_ok else 'FAIL'}")
        if not bl_ok:
            print(f"        exit={result.returncode} stdout={result.stdout!r}")
            all_ok = False

        # 4. beacons-history — exit 0, n_pairs = 0, bias_factor = null.
        cmd = [str(binary), "beacons-history",
               "--period", str(meta["period_seconds"]),
               "--win-start", repr(meta["win_start_unix"]),
               *common_args]
        result = run_captured(cmd, text=True,
                                encoding="utf-8", timeout=10)
        bh_ok = result.returncode == 0
        if bh_ok:
            try:
                line = result.stdout.strip().splitlines()[-1]
                payload = json.loads(line)
                bh_ok = (
                    payload.get("n_pairs") == 0
                    and payload.get("bias_factor") is None
                )
            except Exception:
                bh_ok = False
        print(f"  [{lang:>4s}] {'empty-root: beacons-history':38s} "
              f"{' OK ' if bh_ok else 'FAIL'}")
        if not bh_ok:
            print(f"        exit={result.returncode} stdout={result.stdout!r}")
            all_ok = False

        # 5. search — exit 0, zero hits in jsonl output.
        if lang in IMPLS_WITH_SEARCH:
            cmd = [str(binary), "search", "pattern",
                   "--format", "jsonl",
                   *common_args]
            result = run_captured(cmd, text=True,
                                    encoding="utf-8", timeout=10)
            search_ok = result.returncode == 0
            hits_count = 0
            summary_ok = False
            if search_ok:
                try:
                    for line in result.stdout.splitlines():
                        if not line.strip():
                            continue
                        obj = json.loads(line)
                        kind = obj.get("type")
                        if kind == "hit":
                            hits_count += 1
                        elif kind == "summary":
                            summary_ok = obj.get("hits", -1) == 0
                except Exception:
                    search_ok = False
            search_ok = search_ok and hits_count == 0 and summary_ok
            print(f"  [{lang:>4s}] {'empty-root: search':38s} "
                  f"{' OK ' if search_ok else 'FAIL'}")
            if not search_ok:
                print(f"        exit={result.returncode} stdout={result.stdout!r}")
                all_ok = False

    return all_ok


def check_help(lang: str, binary: Path) -> bool:
    """Parity guard for the friendly help / default output.

    Wording is per-impl, so we assert structure only: --help and no-args
    print an overview to stdout and exit 0; a subcommand + --help does too;
    a bogus flag is a usage error (exit 2). Catches an impl that forgot a
    subcommand in its overview without pinning exact text.
    """
    required_substrings = [
        "USAGE",
        "cost",
        "search",
        "events",
        "beacons-latest",
        "beacons-history",
        "--period",
    ]
    all_ok = True

    def run(args: list[str]) -> ProcessResult:
        return run_captured(
            [str(binary), *args],
            text=True, encoding="utf-8", timeout=10,
        )

    # 1. --help: exit 0, overview on stdout with every subcommand + --period.
    result = run(["--help"])
    missing = [s for s in required_substrings if s not in result.stdout]
    ok = result.returncode == 0 and not missing
    print(f"  [{lang:>4s}] {'help: --help':38s} {' OK ' if ok else 'FAIL'}")
    if not ok:
        print(f"        exit={result.returncode} missing={missing}")
        all_ok = False

    # 2. No args: friendly overview on stdout, exit 0 (not the terse error).
    result = run([])
    ok = result.returncode == 0 and result.stdout.strip() != ""
    print(f"  [{lang:>4s}] {'help: no-args':38s} {' OK ' if ok else 'FAIL'}")
    if not ok:
        print(f"        exit={result.returncode} stdout_empty={result.stdout.strip() == ''}")
        all_ok = False

    # 3. Subcommand + --help: same overview, exit 0 (rule 3).
    result = run(["search", "--help"])
    ok = result.returncode == 0 and "USAGE" in result.stdout
    print(f"  [{lang:>4s}] {'help: search --help':38s} {' OK ' if ok else 'FAIL'}")
    if not ok:
        print(f"        exit={result.returncode}")
        all_ok = False

    # 4. Bogus flag: usage error, exit 2 (still an error, not help).
    result = run(["--bogus-flag"])
    ok = result.returncode == 2
    print(f"  [{lang:>4s}] {'help: bad flag -> exit 2':38s} {' OK ' if ok else 'FAIL'}")
    if not ok:
        print(f"        exit={result.returncode} (expected 2)")
        all_ok = False

    return all_ok


def check_cli_argument_matrix(lang: str, binary: Path) -> bool:
    """Exit-code matrix for arg-validation across every subcommand.

    Each row drives one invocation and asserts:
      - exit code (always 2 for the error rows, 0 for --version),
      - stderr-non-empty when exit is 2 (an impl that exits 2 silently
        violates the SPEC error-diagnostic contract),
      - an optional ``stderr_must_contain`` substring for cases where a
        stable token is documented (e.g. ``--period`` in the missing-period
        error). Wording past that token is per-impl and NOT pinned.

    The matrix is the SPEC §"## CLI" / §"### Help & usage" contract translated
    into observable behavior. It catches an impl that drifts off the
    arg-validation grammar — e.g. a panic on unknown subcommand, or a regex
    engine that silently accepts an unclosed pattern — without pinning any
    impl's exact diagnostic text. See COVERAGE-GAPS.md gap A18.
    """
    all_ok = True

    def run(args: list[str]) -> ProcessResult:
        return run_captured(
            [str(binary), *args],
            text=True, encoding="utf-8", timeout=10,
        )

    # (label, args, expected_exit, stderr_must_contain_or_None)
    #
    # `--no-config` is appended where the error path is hit *after* roots
    # resolution (so the impl doesn't waste effort on the user's real config).
    # Several error paths fail before any roots work, so it's harmless either
    # way; included consistently for the impls that defer the check.
    cases: list[tuple[str, list[str], int, str | None]] = [
        # --- cost mode (no subcommand prefix) ---
        ("cost: missing --period",
         ["--win-start", "0", "--no-config"], 2, "--period"),
        ("cost: missing flag value (--period)",
         ["--period", "--no-config"], 2, "--period"),
        ("cost: unparseable --period",
         ["--period", "x", "--win-start", "0", "--no-config"], 2, "--period"),
        ("cost: unparseable --win-start",
         ["--period", "60", "--win-start", "nope", "--no-config"], 2, "--win-start"),
        ("cost: unparseable --now",
         ["--period", "60", "--win-start", "0", "--now", "nope", "--no-config"], 2, "--now"),
        ("cost: unknown flag",
         ["--period", "60", "--win-start", "0", "--frobnicate", "--no-config"], 2, "--frobnicate"),
        ("cost: unknown subcommand",
         ["bogus", "--period", "60", "--win-start", "0", "--no-config"], 2, "bogus"),

        # --- cost mode: missing-value rows (flag is the LAST argv token) ---
        ("cost: --win-start missing value",
         ["--period", "60", "--win-start"], 2, "--win-start"),
        ("cost: --now missing value",
         ["--period", "60", "--win-start", "0", "--now"], 2, "--now"),
        ("cost: --projects-root missing value",
         ["--period", "60", "--win-start", "0", "--projects-root"], 2,
         "--projects-root"),
        ("cost: --extra-projects-root missing value",
         ["--period", "60", "--win-start", "0", "--extra-projects-root"], 2,
         "--extra-projects-root"),
        ("cost: missing --win-start",
         ["--period", "60", "--no-config"], 2, "--win-start"),

        # --- events ---
        ("events: missing --period",
         ["events", "--no-config"], 2, "--period"),
        ("events: unparseable --period",
         ["events", "--period", "x", "--no-config"], 2, "--period"),
        ("events: unknown flag",
         ["events", "--period", "60", "--frobnicate", "--no-config"], 2, "--frobnicate"),
        ("events: --period missing value",
         ["events", "--period"], 2, "--period"),
        ("events: --win-start missing value",
         ["events", "--period", "60", "--win-start"], 2, "--win-start"),
        ("events: unparseable --win-start",
         ["events", "--period", "60", "--win-start", "nope", "--no-config"],
         2, "--win-start"),
        ("events: --now missing value",
         ["events", "--period", "60", "--now"], 2, "--now"),
        ("events: unparseable --now",
         ["events", "--period", "60", "--now", "nope", "--no-config"],
         2, "--now"),
        ("events: --projects-root missing value",
         ["events", "--period", "60", "--projects-root"], 2, "--projects-root"),
        ("events: --extra-projects-root missing value",
         ["events", "--period", "60", "--extra-projects-root"], 2,
         "--extra-projects-root"),

        # --- beacons-latest ---
        ("beacons-latest: missing --session-id",
         ["beacons-latest", "--no-config"], 2, "--session-id"),
        ("beacons-latest: unknown flag",
         ["beacons-latest", "--session-id", "deadbeef", "--frobnicate", "--no-config"],
         2, "--frobnicate"),
        ("beacons-latest: --session-id missing value",
         ["beacons-latest", "--session-id"], 2, "--session-id"),
        ("beacons-latest: --projects-root missing value",
         ["beacons-latest", "--session-id", "deadbeef", "--projects-root"],
         2, "--projects-root"),
        ("beacons-latest: --now missing value",
         ["beacons-latest", "--session-id", "deadbeef", "--now"], 2, "--now"),
        ("beacons-latest: unparseable --now",
         ["beacons-latest", "--session-id", "deadbeef", "--now", "nope",
          "--no-config"], 2, "--now"),
        ("beacons-latest: --extra-projects-root missing value",
         ["beacons-latest", "--session-id", "deadbeef", "--extra-projects-root"],
         2, "--extra-projects-root"),

        # --- beacons-history ---
        ("beacons-history: missing --period",
         ["beacons-history", "--no-config"], 2, "--period"),
        ("beacons-history: unparseable --period",
         ["beacons-history", "--period", "x", "--no-config"], 2, "--period"),
        ("beacons-history: unknown flag",
         ["beacons-history", "--period", "60", "--frobnicate", "--no-config"],
         2, "--frobnicate"),
        ("beacons-history: --period missing value",
         ["beacons-history", "--period"], 2, "--period"),
        ("beacons-history: --win-start missing value",
         ["beacons-history", "--period", "60", "--win-start"], 2, "--win-start"),
        ("beacons-history: unparseable --win-start",
         ["beacons-history", "--period", "60", "--win-start", "nope",
          "--no-config"], 2, "--win-start"),
        ("beacons-history: --now missing value",
         ["beacons-history", "--period", "60", "--now"], 2, "--now"),
        ("beacons-history: unparseable --now",
         ["beacons-history", "--period", "60", "--now", "nope", "--no-config"],
         2, "--now"),
        ("beacons-history: --projects-root missing value",
         ["beacons-history", "--period", "60", "--projects-root"],
         2, "--projects-root"),
        ("beacons-history: --extra-projects-root missing value",
         ["beacons-history", "--period", "60", "--extra-projects-root"],
         2, "--extra-projects-root"),

        # --- search ---
        ("search: missing pattern",
         ["search", "--no-config"], 2, None),
        ("search: empty pattern",
         ["search", "", "--no-config"], 2, None),
        ("search: invalid --role",
         ["search", "hello", "--role", "bogus", "--no-config"], 2, "--role"),
        ("search: invalid --format",
         ["search", "hello", "--format", "bogus", "--no-config"], 2, "--format"),
        ("search: --cwd + --any-cwd mutex",
         ["search", "hello", "--cwd", "foo", "--any-cwd", "--no-config"],
         2, "--cwd"),
        ("search: duplicate positional",
         ["search", "a", "b", "--no-config"], 2, None),
        ("search: malformed regex (trailing backslash)",
         ["search", "\\", "--regex", "--no-config"], 2, None),
        # COVERAGE-GAPS.md A7 / SPEC §"Search" — unsupported regex
        # metachars (grouping, alternation, bounded repetition) must reject
        # with exit 2. Zig's hand-rolled engine silently accepted `(` as a
        # literal pre-task-#13; that fix added explicit rejection so all four
        # impls now parity-fail this case.
        ("search: malformed regex (unsupported metachar)",
         ["search", "(", "--regex", "--no-config"], 2, None),
        ("search: unknown flag",
         ["search", "hello", "--frobnicate", "--no-config"], 2, "--frobnicate"),
        # COVERAGE-GAPS.md A4 — `bad time` diagnostic. SPEC §"### search":
        # "unparseable --since/--until (`bad time: ...`)". Exit code is the
        # shared contract; the exact diagnostic wording (whether the flag
        # name appears, vs. only the offending value or the `bad time:`
        # token) is per-impl — cpp's parse_time_arg path emits "not RFC3339
        # or relative: ..." without the flag name, rust prepends "bad time:
        # --since=...", etc. So we pin only exit=2 + non-empty stderr here.
        # Each of these targets a distinct code path:
        #   malformed-{since,until} -> parse_iso8601 fallback + error format
        #   empty value             -> parse_time_arg's "empty value" branch
        #   missing value           -> iter.next().ok_or("--since needs a value")
        ("search: malformed --since",
         ["search", "hello", "--since", "not-a-time", "--no-config"], 2, None),
        ("search: malformed --until",
         ["search", "hello", "--until", "tomorrow", "--no-config"], 2, None),
        ("search: empty --since value",
         ["search", "hello", "--since", "", "--no-config"], 2, None),
        ("search: --since missing value",
         ["search", "hello", "--since"], 2, None),
        # Missing-value rows for every value-taking search flag. Wording is
        # per-impl; the flag name itself is the stable token.
        ("search: --role missing value",
         ["search", "hello", "--role"], 2, "--role"),
        ("search: --until missing value",
         ["search", "hello", "--until"], 2, "--until"),
        ("search: --cwd missing value",
         ["search", "hello", "--cwd"], 2, "--cwd"),
        ("search: --context missing value",
         ["search", "hello", "--context"], 2, "--context"),
        ("search: --limit missing value",
         ["search", "hello", "--limit"], 2, "--limit"),
        ("search: --format missing value",
         ["search", "hello", "--format"], 2, "--format"),
        ("search: --snippet-chars missing value",
         ["search", "hello", "--snippet-chars"], 2, "--snippet-chars"),
        ("search: --projects-root missing value",
         ["search", "hello", "--projects-root"], 2, "--projects-root"),
        ("search: --now missing value",
         ["search", "hello", "--now"], 2, "--now"),
        ("search: --extra-projects-root missing value",
         ["search", "hello", "--extra-projects-root"], 2,
         "--extra-projects-root"),
        # Invalid-numeric rows: each targets the per-flag parse-failure branch.
        ("search: invalid --context",
         ["search", "hello", "--context", "x", "--no-config"], 2, "--context"),
        ("search: invalid --limit",
         ["search", "hello", "--limit", "x", "--no-config"], 2, "--limit"),
        ("search: invalid --snippet-chars",
         ["search", "hello", "--snippet-chars", "x", "--no-config"], 2,
         "--snippet-chars"),
        ("search: invalid --now",
         ["search", "hello", "--now", "x", "--no-config"], 2, "--now"),

        # --- --version (cost + events) — exit 0, stdout non-empty ---
        # (kept in the same matrix so reporting stays uniform; the stdout-check
        # below handles the 0-exit case.)
        ("--version (cost)", ["--version"], 0, None),
        ("--version (events)", ["events", "--version"], 0, None),
    ]

    for label, args, expected_exit, must_contain in cases:
        result = run(args)
        ok = result.returncode == expected_exit
        if ok and expected_exit == 2:
            # All exit-2 paths MUST emit a diagnostic to stderr.
            if result.stderr.strip() == "":
                ok = False
            if ok and must_contain is not None and must_contain not in result.stderr:
                ok = False
        if ok and expected_exit == 0:
            # --version: stdout non-empty (don't pin the version string).
            if result.stdout.strip() == "":
                ok = False
        badge = " OK " if ok else "FAIL"
        print(f"  [{lang:>4s}] {'cli/' + label:48s} {badge}")
        if not ok:
            stderr_preview = result.stderr.strip().splitlines()[:2]
            stdout_preview = result.stdout.strip().splitlines()[:2]
            print(
                f"        exit={result.returncode} (expected {expected_exit}) "
                f"must_contain={must_contain!r}"
            )
            if stderr_preview:
                print(f"        stderr: {stderr_preview!r}")
            if stdout_preview:
                print(f"        stdout: {stdout_preview!r}")
            all_ok = False

    return all_ok


def check_omit_now_smoke(lang: str, binary: Path) -> bool:
    """One invocation per mode that omits --now → exit 0.

    The other runners always pass --now (so tests are deterministic), which
    leaves the "current time when --now is omitted" default code path
    (``current_unix``/``nowUnix``/…) entirely uncovered. This smoke fires
    each mode against an empty fixture root so the result is trivially
    empty — we assert only exit 0 (the value is nondeterministic by design).

    See COVERAGE-GAPS.md §B2.
    """
    all_ok = True

    with tempfile.TemporaryDirectory(prefix="walker-omit-now-") as empty:
        # ``empty`` is an existing-but-empty dir → no transcripts → no output.
        # SPEC.Roots says non-existent extras get skipped silently; we want a
        # real-dir to keep the no-warning path (some impls emit a stderr line
        # for the missing-root case which is fine but noisier than needed).
        cases: list[tuple[str, list[str]]] = [
            ("cost", ["--period", "60", "--win-start", "0",
                      "--projects-root", empty, "--no-config"]),
            ("events", ["events", "--period", "60", "--win-start", "0",
                        "--projects-root", empty, "--no-config"]),
            ("beacons-history", ["beacons-history", "--period", "60",
                                  "--win-start", "0",
                                  "--projects-root", empty, "--no-config"]),
            ("beacons-latest", ["beacons-latest", "--session-id", "deadbeef",
                                "--projects-root", empty, "--no-config"]),
            ("search", ["search", "hello",
                        "--projects-root", empty, "--no-config",
                        "--format", "jsonl"]),
        ]
        for label, args in cases:
            result = run_captured(
                [str(binary), *args],
                text=True, encoding="utf-8", timeout=10,
            )
            ok = result.returncode == 0
            badge = " OK " if ok else "FAIL"
            print(f"  [{lang:>4s}] {'omit-now/' + label:48s} {badge}")
            if not ok:
                print(f"        exit={result.returncode} "
                      f"stderr={result.stderr.strip()[:200]!r}")
                all_ok = False

    return all_ok


@overload
def run_walker_env(
    lang: str, binary: Path, meta: dict, env: dict, projects_root: Path | None = None,
    *, return_stderr: Literal[False] = False,
) -> dict: ...
@overload
def run_walker_env(
    lang: str, binary: Path, meta: dict, env: dict, projects_root: Path | None = None,
    *, return_stderr: Literal[True],
) -> tuple[dict, str]: ...
def run_walker_env(
    lang: str, binary: Path, meta: dict, env: dict, projects_root: Path | None = None,
    return_stderr: bool = False,
) -> dict | tuple[dict, str]:
    """Run cost mode WITHOUT --no-config (so walker-roots.json is read) under a
    custom environment (to control home-dir resolution). With projects_root
    omitted, the binary falls back to its default <home>/.claude/projects.

    Returns the parsed JSON dict. With return_stderr=True returns
    (dict, stderr_text) so callers can assert on diagnostic emission."""
    cmd = [
        str(binary),
        "--period", str(meta["period_seconds"]),
        "--win-start", repr(meta["win_start_unix"]),
        "--now", repr(meta["now_unix"]),
    ]
    if projects_root is not None:
        cmd.extend(["--projects-root", str(projects_root)])
    result = run_captured(
        cmd, text=True, encoding="utf-8", timeout=10, env=env
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{binary.name} exited {result.returncode}\nstderr:\n{result.stderr}"
        )
    line = result.stdout.strip().splitlines()[-1]
    parsed = json.loads(line)
    if return_stderr:
        return parsed, result.stderr
    return parsed


def check_config_resolution(lang: str, binary: Path, expected: dict) -> bool:
    """Exercise home-dir + walker-roots.json resolution.

    The --no-config runners above never touch either, so a wrong HOME-vs-
    USERPROFILE precedence or a dropped config root sails through. Lay out a
    fake home:

        <home>/.claude/projects/<fixtureA>/...         -> default primary root
        <home>/.claude/walker-roots.json -> [<extra>]  -> config extra
        <extra>/<fixtureB>/...                         -> extra root data

    Point the canonical home var (USERPROFILE on Windows, else HOME) at <home>
    and the *other* var at a config-less dir, per the precedence contract in
    SPEC.md::Roots. Run with NO --projects-root and NO --no-config, so passing
    requires the binary to resolve BOTH the default root and the config file
    from the canonical home var. Expect the fixtureA+fixtureB sum.
    """
    meta = expected["_meta"]
    names = list(expected["fixtures"].keys())
    label = "config-resolution"
    if len(names) < 2:
        print(f"  [{lang:>4s}] {label:20s} SKIP  need >=2 fixtures")
        return True
    # Pick the two priciest fixtures so the sum is discriminating (a dropped
    # config root or a 0-cost primary can't masquerade as a pass).
    ranked = sorted(names, key=lambda n: expected["fixtures"][n]["trailing_usd"], reverse=True)
    fixture_a, fixture_b = ranked[0], ranked[1]
    target = {
        "trailing_usd": expected["fixtures"][fixture_a]["trailing_usd"]
        + expected["fixtures"][fixture_b]["trailing_usd"],
        "window_usd": expected["fixtures"][fixture_a]["window_usd"]
        + expected["fixtures"][fixture_b]["window_usd"],
    }
    with tempfile.TemporaryDirectory(prefix="walker-cfg-home-") as home, \
         tempfile.TemporaryDirectory(prefix="walker-cfg-extra-") as extra, \
         tempfile.TemporaryDirectory(prefix="walker-cfg-bogus-") as bogus:
        home_p, extra_p = Path(home), Path(extra)
        shutil.copytree(CORPUS / fixture_a, home_p / ".claude" / "projects" / fixture_a)
        shutil.copytree(CORPUS / fixture_b, extra_p / fixture_b)
        (home_p / ".claude" / "walker-roots.json").write_text(
            json.dumps({"extra_roots": [str(extra_p)]}), encoding="utf-8"
        )
        env = dict(os.environ)
        if sys.platform == "win32":
            env["USERPROFILE"], env["HOME"] = str(home_p), str(bogus)
        else:
            env["HOME"], env["USERPROFILE"] = str(home_p), str(bogus)
        try:
            got = run_walker_env(lang, binary, meta, env)
        except Exception as e:
            print(f"  [{lang:>4s}] {label:20s} FAIL  {e}")
            return False
    ok, dt, dw = within_tolerance(got, target)
    badge = " OK " if ok else "FAIL"
    print(
        f"  [{lang:>4s}] {label:20s} {badge}  "
        f"trailing=${got.get('trailing_usd', 0):.6f} (d=${dt:+.6f})  "
        f"window=${got.get('window_usd', 0):.6f} (d=${dw:+.6f})"
    )
    return ok


# Malformed walker-roots.json variants. Each tuple is
#   (label, body, expect_diagnostic)
# where `body` is the file contents and `expect_diagnostic` is True when the
# binary MUST emit a stderr diagnostic (parse error or wrong-shape root).
# Variants that produce silent no-extras (missing key, empty array, valid
# unrelated-key object) have expect_diagnostic=False.
#
# Per SPEC.md::Roots, every variant must:
#   1. NOT crash (the binary keeps going with no extras),
#   2. yield totals matching the default-root-only scan (i.e. no extras pulled
#      in from the malformed file).
# The "nonexistent-extra" variant has a syntactically valid file but its one
# extra path does not exist; SPEC says the binary emits a stderr "extra root
# not a directory, skipping" line and continues.
MALFORMED_CONFIG_VARIANTS: list[tuple[str, str, bool]] = [
    ("empty",              "",                                     False),
    ("malformed-json",     "{not valid json",                      True),
    # Unclosed string fails JSON tokenization itself (a different parse
    # stage than the bareword case above in some parsers).
    ("unclosed-string",    '{"extra_roots": ["/x',                 True),
    ("non-object-array",   "[]",                                   True),
    ("non-object-scalar",  '"hello"',                              True),
    ("missing-key",        "{}",                                   False),
    ("unrelated-key",      '{"unrelated": []}',                    False),
    ("empty-extra-array",  '{"extra_roots": []}',                  False),
    ("nonexistent-extra",  '{"extra_roots": ["/does/not/exist"]}', True),
    # Valid object with wrong-typed extras array. Reaches Go's typed
    # json.Unmarshal failure branch (walker_roots.go:71); Rust/C++/Zig
    # silently skip non-string elements. Diagnostic optional — Go emits
    # one, the others don't.
    ("wrong-typed-extras", '{"extra_roots":[1,2,3]}',              False),
    # Valid object wrapped in leading/trailing whitespace (exercises the
    # pre-parse whitespace skip in Go's object sniff; a no-op elsewhere).
    ("surrounding-ws",     '\n\t  {"extra_roots": []}  \n\n',      False),
]


def check_config_malformed(lang: str, binary: Path, expected: dict) -> bool:
    """Exercise every malformed walker-roots.json variant.

    For each variant: lay out a fake home with one populated fixture under
    ~/.claude/projects/<fixture>/, drop the malformed body at
    ~/.claude/walker-roots.json, run cost mode WITHOUT --no-config under
    controlled HOME, and assert:
      (a) exit 0 (graceful, no crash);
      (b) totals match the single-fixture target (NO extras pulled in);
      (c) for variants where SPEC requires a diagnostic, stderr is non-empty.

    Picks the priciest fixture so a 0-cost masquerade can't pass."""
    meta = expected["_meta"]
    names = list(expected["fixtures"].keys())
    if not names:
        print(f"  [{lang:>4s}] {'config-malformed':20s} SKIP  no fixtures")
        return True
    fixture = max(names, key=lambda n: expected["fixtures"][n]["trailing_usd"])
    target = {
        "trailing_usd": expected["fixtures"][fixture]["trailing_usd"],
        "window_usd": expected["fixtures"][fixture]["window_usd"],
    }
    overall = True
    for variant_label, body, expect_diagnostic in MALFORMED_CONFIG_VARIANTS:
        label = f"config-malformed:{variant_label}"
        with tempfile.TemporaryDirectory(prefix="walker-cfg-mal-home-") as home, \
             tempfile.TemporaryDirectory(prefix="walker-cfg-mal-bogus-") as bogus:
            home_p = Path(home)
            shutil.copytree(CORPUS / fixture, home_p / ".claude" / "projects" / fixture)
            (home_p / ".claude" / "walker-roots.json").write_text(body, encoding="utf-8")
            env = dict(os.environ)
            if sys.platform == "win32":
                env["USERPROFILE"], env["HOME"] = str(home_p), str(bogus)
            else:
                env["HOME"], env["USERPROFILE"] = str(home_p), str(bogus)
            try:
                got, stderr_text = run_walker_env(
                    lang, binary, meta, env, return_stderr=True
                )
            except Exception as e:
                print(f"  [{lang:>4s}] {label:38s} FAIL  {e}")
                overall = False
                continue
        ok_totals, dt, dw = within_tolerance(got, target)
        if expect_diagnostic:
            ok_stderr = bool(stderr_text.strip())
        else:
            # Silent path: stderr SHOULD be empty (but a benign warning isn't a
            # failure — only an exit-nonzero or wrong totals would be). Don't
            # assert stderr is empty; just record it for the operator.
            ok_stderr = True
        ok = ok_totals and ok_stderr
        badge = " OK " if ok else "FAIL"
        diag_hint = ""
        if expect_diagnostic and not ok_stderr:
            diag_hint = "  (expected stderr diagnostic, got none)"
        print(
            f"  [{lang:>4s}] {label:38s} {badge}  "
            f"trailing=${got.get('trailing_usd', 0):.6f} (d=${dt:+.6f})  "
            f"window=${got.get('window_usd', 0):.6f} (d=${dw:+.6f}){diag_hint}"
        )
        if not ok:
            overall = False
    return overall


def check_cost_subcommand(lang: str, binary: Path, expected: dict) -> bool:
    """Explicit `cost` subcommand must behave identically to the bare-flag
    routing (the back-compat default). Runs the priciest fixture through
    `walker cost ...` and compares totals to expected.json."""
    meta = expected["_meta"]
    names = list(expected["fixtures"].keys())
    fixture = max(names, key=lambda n: expected["fixtures"][n]["trailing_usd"])
    target = expected["fixtures"][fixture]
    label = "cost-subcommand"
    with tempfile.TemporaryDirectory(prefix="walker-cost-sub-") as tmp:
        shutil.copytree(CORPUS / fixture, Path(tmp) / fixture)
        cmd = [
            str(binary), "cost",
            "--period", str(meta["period_seconds"]),
            "--win-start", repr(meta["win_start_unix"]),
            "--now", repr(meta["now_unix"]),
            "--projects-root", tmp,
            "--no-config",
        ]
        result = run_captured(cmd, text=True,
                                encoding="utf-8", timeout=10)
    ok = result.returncode == 0
    got: dict = {}
    if ok:
        try:
            got = json.loads(result.stdout.strip().splitlines()[-1])
        except Exception:
            ok = False
    if ok:
        ok, _, _ = within_tolerance(got, target)
    print(f"  [{lang:>4s}] {label:38s} {' OK ' if ok else 'FAIL'}")
    if not ok:
        print(f"        exit={result.returncode} stdout={result.stdout!r}")
    return ok


def check_beacons_extra_root(lang: str, binary: Path) -> bool:
    """--extra-projects-root on beacons-latest and beacons-history.

    The flag is parsed by both subcommands (and used by bench.py), but no
    conformance scenario exercised it: a dropped extra root in either mode
    sailed through. Latest: beacon lives only under the extra root. History:
    one slug in the primary root, the other in the extra root - pairs must
    aggregate across both.
    """
    if not EXPECTED_LATEST.is_file() or not EXPECTED_HISTORY.is_file():
        return True
    expected_latest = json.loads(EXPECTED_LATEST.read_text(encoding="utf-8"))
    expected_history = json.loads(EXPECTED_HISTORY.read_text(encoding="utf-8"))
    now_unix = expected_latest["_meta"]["now_unix"]
    all_ok = True

    # --- latest: empty primary, scenario under the extra root ---
    scenario = "clean_lifecycle"
    target = expected_latest["fixtures"][scenario]
    label = "beacons-latest: extra root"
    with tempfile.TemporaryDirectory(prefix="walker-bl-extra-") as tmp:
        primary = Path(tmp) / "primary"
        extra = Path(tmp) / "extra"
        primary.mkdir()
        shutil.copytree(BEACON_CORPUS / scenario, extra / scenario)
        try:
            got = run_walker_subcommand(lang, binary, "beacons-latest", [
                "--session-id", target["session_id"],
                "--projects-root", str(primary),
                "--extra-projects-root", str(extra),
                "--now", repr(now_unix),
            ])
        except Exception as e:
            print(f"  [{lang:>4s}] {label:38s} FAIL  {e}")
            got = None
    if got is not None:
        ok = (
            got.get("beacon") == target.get("beacon")
            and got.get("emitted_at") == target.get("emitted_at")
            and got.get("age_seconds") == target.get("age_seconds")
        )
        print(f"  [{lang:>4s}] {label:38s} {' OK ' if ok else 'FAIL'}")
        if not ok:
            print(f"        got: {got}")
            print(f"        target: {target}")
            all_ok = False
    else:
        all_ok = False

    # --- history: slug_a in primary, slug_b in extra root ---
    scenario = "cross_session_pairs"
    target = expected_history["fixtures"][scenario]
    label = "beacons-history: extra root"
    with tempfile.TemporaryDirectory(prefix="walker-bh-extra-") as tmp:
        primary = Path(tmp) / "primary"
        extra = Path(tmp) / "extra"
        shutil.copytree(BEACON_CORPUS / scenario / "slug_a", primary / "slug_a")
        shutil.copytree(BEACON_CORPUS / scenario / "slug_b", extra / "slug_b")
        try:
            got = run_walker_subcommand(lang, binary, "beacons-history", [
                "--period", "604800",
                "--win-start", "0",
                "--projects-root", str(primary),
                "--extra-projects-root", str(extra),
                "--now", repr(now_unix),
            ])
        except Exception as e:
            print(f"  [{lang:>4s}] {label:38s} FAIL  {e}")
            got = None
    if got is not None:
        def pairs_key(p):
            return (p["begin_eta"], p["actual_elapsed"])
        pairs_ok = sorted(map(pairs_key, got.get("pairs", []))) == sorted(
            map(pairs_key, target["pairs"]))
        bias_got, bias_tgt = got.get("bias_factor"), target.get("bias_factor")
        if bias_got is None or bias_tgt is None:
            bias_ok = bias_got == bias_tgt
        else:
            bias_ok = abs(bias_got - bias_tgt) <= BIAS_TOLERANCE
        ok = (pairs_ok and bias_ok
              and got.get("session_count") == target["session_count"]
              and got.get("n_pairs") == target["n_pairs"])
        print(f"  [{lang:>4s}] {label:38s} {' OK ' if ok else 'FAIL'}")
        if not ok:
            print(f"        got: {got}")
            print(f"        target: {target}")
            all_ok = False
    else:
        all_ok = False
    return all_ok


def check_events_extra_root_profile(lang: str, binary: Path) -> bool:
    """events with --extra-projects-root, run under WALKER_PROFILE=1.

    The extra-root flag was parsed but never exercised in events mode; the
    profile env var (cpp prints per-phase timings to stderr, others ignore
    it) was likewise never set. Output records must match the fixture's
    expectation exactly; stdout must stay clean NDJSON.
    """
    if not EVENTS_EXPECTED.is_file() or lang not in IMPLS_WITH_EVENTS:
        return True
    expected_data = json.loads(EVENTS_EXPECTED.read_text(encoding="utf-8"))
    fixture_name = "02-single"
    target_records = expected_data["fixtures"].get(fixture_name)
    if target_records is None:
        return True
    label = "events: extra root + profile env"
    env = dict(os.environ, WALKER_PROFILE="1")
    with tempfile.TemporaryDirectory(prefix="walker-ev-extra-") as tmp:
        primary = Path(tmp) / "primary"
        primary.mkdir()
        cmd = [
            str(binary), "events",
            "--period", repr(expected_data["pin_period"]),
            "--win-start", repr(expected_data["pin_win_start"]),
            "--projects-root", str(primary),
            "--extra-projects-root", str(EVENTS_CORPUS / fixture_name),
            "--now", repr(expected_data["pin_now"]),
            "--no-config",
        ]
        result = run_captured(cmd, text=True,
                                encoding="utf-8", timeout=10, env=env)
    ok = result.returncode == 0
    got_records: list[dict] = []
    if ok:
        try:
            got_records = [json.loads(line) for line in result.stdout.splitlines()
                           if line.strip()]
        except Exception:
            ok = False
    if ok:
        got_sorted = sorted(got_records, key=_events_sort_key)
        target_sorted = sorted(target_records, key=_events_sort_key)
        ok = len(got_sorted) == len(target_sorted) and all(
            abs(g.get("ts", 0.0) - t.get("ts", 0.0)) <= EVENTS_TS_TOLERANCE
            and abs(g.get("usd", 0.0) - t.get("usd", 0.0)) <= EVENTS_USD_TOLERANCE
            and g.get("model") == t.get("model")
            and g.get("session_id") == t.get("session_id")
            and g.get("slug") == t.get("slug")
            for g, t in zip(got_sorted, target_sorted)
        )
    print(f"  [{lang:>4s}] {label:38s} {' OK ' if ok else 'FAIL'}")
    if not ok:
        print(f"        exit={result.returncode} stdout={result.stdout!r} "
              f"stderr={result.stderr!r}")
    return ok


def check_search_cpuprofile(lang: str, binary: Path) -> bool:
    """One search run with WALKER_CPUPROFILE set (go writes a pprof profile;
    the other impls ignore the env var). Hits must match the combo exactly."""
    if not SEARCH_CORPUS.is_dir() or lang not in IMPLS_WITH_SEARCH:
        return True
    scenario_name = "01-basic"
    expected_file = SEARCH_CORPUS / scenario_name / "expected.json"
    if not expected_file.is_file():
        return True
    data = json.loads(expected_file.read_text(encoding="utf-8"))
    combo = data["combos"]["default"]
    now_unix = data["_meta"]["now_unix"]
    label = "search: cpuprofile env"
    with tempfile.TemporaryDirectory(prefix="walker-search-prof-") as tmp:
        shutil.copytree(SEARCH_CORPUS / scenario_name, Path(tmp) / scenario_name)
        env = dict(os.environ, WALKER_CPUPROFILE=str(Path(tmp) / "cpu.prof"))
        cmd = [
            str(binary), "search", combo["pattern"],
            "--projects-root", tmp,
            "--now", repr(now_unix),
            "--format", "jsonl",
            "--no-config",
            *combo["flags"],
        ]
        result = run_captured(cmd, text=True,
                                encoding="utf-8", timeout=10, env=env)
    ok = result.returncode == 0
    hits = []
    if ok:
        try:
            for line in result.stdout.splitlines():
                if not line.strip():
                    continue
                obj = json.loads(line)
                if obj.get("type") == "hit":
                    for key in SEARCH_HIT_STRIP_KEYS:
                        obj.pop(key, None)
                    hits.append(obj)
        except Exception:
            ok = False
    ok = ok and hits == combo["hits"]
    print(f"  [{lang:>4s}] {label:38s} {' OK ' if ok else 'FAIL'}")
    if not ok:
        print(f"        exit={result.returncode} hits={len(hits)} "
              f"expected={len(combo['hits'])}")
    return ok


def _write_beacon_lines(path: Path,
                        entries: list[tuple[float, str, str]]) -> None:
    """Write a minimal beacon transcript: entries are (unix_ts, kind, eta_json)
    triples rendered as assistant turns carrying one beacon block each."""
    from datetime import datetime, timezone
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for index, (ts, kind, eta) in enumerate(entries):
            iso = datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.000Z")
            beacon = ('{"kind": "%s", "eta_seconds": %s, "summary": "odd %d"}'
                      % (kind, eta, index))
            entry = {
                "type": "assistant",
                "timestamp": iso,
                "message": {
                    "id": f"msg_odd_{index}",
                    "role": "assistant",
                    "model": "claude-opus-4-7",
                    "content": [{"type": "text",
                                 "text": f"<progress-beacon>{beacon}"
                                         f"</progress-beacon>"}],
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            }
            f.write(json.dumps(entry))
            f.write("\n")


def _populate_oddities(root: Path) -> None:
    """Drop the discovery-oddity decorations into `root`: entries that every
    walker must skip without crashing or double-counting."""
    (root / "stray.txt").write_text("not a slug dir\n", encoding="utf-8")
    slug_a = next(p for p in root.iterdir() if p.is_dir())
    (slug_a / "notes.txt").write_text("not a transcript\n", encoding="utf-8")
    (slug_a / "noext").write_text("no extension\n", encoding="utf-8")
    (slug_a / "plain-session-dir").mkdir()
    (slug_a / "sess-file-subagents").mkdir()
    (slug_a / "sess-file-subagents" / "subagents").write_text(
        "a file, not a dir\n", encoding="utf-8")
    # A directory named like a parent transcript.
    dir_jsonl = root / "slug-dir" / "phantom.jsonl"
    dir_jsonl.mkdir(parents=True)


def check_discovery_oddities(lang: str, binary: Path, expected: dict) -> bool:
    """Stray files/dirs every discovery walk must skip: non-dir entries in the
    projects root, non-jsonl files in slug dirs, `subagents` existing as a
    regular file, session dirs without subagents, non-agent files inside
    subagents/, and a *directory* named `<sid>.jsonl`. Also passes one
    nonexistent --extra-projects-root (skipped with a diagnostic, exit 0).

    Cost mode: totals must equal fixtureA+fixtureB (the only real data).
    Beacons-latest/history: run the same tree shape built from beacon
    fixtures; the decorations must not change the result.
    """
    meta = expected["_meta"]
    all_ok = True

    # --- cost mode ---
    label = "oddities: cost walk"
    fix_parent = "01-single-parent"
    fix_agent = "07-unknown-model"
    target = {
        "trailing_usd": expected["fixtures"][fix_parent]["trailing_usd"]
        + expected["fixtures"][fix_agent]["trailing_usd"],
        "window_usd": expected["fixtures"][fix_parent]["window_usd"]
        + expected["fixtures"][fix_agent]["window_usd"],
    }
    with tempfile.TemporaryDirectory(prefix="walker-oddities-") as tmp:
        root = Path(tmp)
        shutil.copytree(CORPUS / fix_parent, root / "slug-main")
        _populate_oddities(root)
        # Subagents dir with strays + one REAL agent transcript (fixtureB's
        # session file re-homed as a subagent).
        sub = root / "slug-main" / "sess-odd" / "subagents"
        sub.mkdir(parents=True)
        (sub / "stray.txt").write_text("skip me\n", encoding="utf-8")
        (sub / "notagent.jsonl").write_text("", encoding="utf-8")
        (sub / "agent-x.txt").write_text("wrong extension\n", encoding="utf-8")
        (sub / "nested-dir").mkdir()
        agent_src = next((CORPUS / fix_agent).glob("*.jsonl"))
        shutil.copy(agent_src, sub / "agent-odd.jsonl")
        missing_extra = root / "does-not-exist"
        try:
            got = run_walker(lang, binary, meta, root, extras=[missing_extra])
            ok, _, _ = within_tolerance(got, target)
            print(f"  [{lang:>4s}] {label:38s} {' OK ' if ok else 'FAIL'}")
            if not ok:
                print(f"        got: {got}")
                all_ok = False
        except Exception as e:
            print(f"  [{lang:>4s}] {label:38s} FAIL  {e}")
            all_ok = False

    # --- beacons-latest + beacons-history over one decorated tree ---
    now_unix = meta["now_unix"]
    with tempfile.TemporaryDirectory(prefix="walker-oddities-bc-") as tmp:
        root = Path(tmp)
        # Parent transcript with a begin->end lifecycle (sid "oddsess").
        _write_beacon_lines(root / "slug-a" / "oddsess.jsonl", [
            (now_unix - 600, "begin", "600"),
            (now_unix - 100, "end", "0"),
        ])
        _populate_oddities(root)
        # Subagent transcript with its own lifecycle, plus strays.
        sub = root / "slug-a" / "sess2" / "subagents"
        sub.mkdir(parents=True)
        (sub / "stray.txt").write_text("skip\n", encoding="utf-8")
        (sub / "notagent.jsonl").write_text("", encoding="utf-8")
        (sub / "nested-dir").mkdir()
        _write_beacon_lines(sub / "agent-sub.jsonl", [
            (now_unix - 1000, "begin", "200"),
            (now_unix - 700, "end", "0"),
        ])

        label = "oddities: beacons-latest walk"
        try:
            got = run_walker_subcommand(lang, binary, "beacons-latest", [
                "--session-id", "oddsess",
                "--projects-root", str(root),
                "--extra-projects-root", str(root / "does-not-exist"),
                "--now", repr(now_unix),
            ])
            ok = (got.get("beacon") or {}).get("kind") == "end" and \
                got.get("emitted_at") == now_unix - 100
        except Exception as e:
            print(f"  [{lang:>4s}] {label:38s} FAIL  {e}")
            ok = False
        print(f"  [{lang:>4s}] {label:38s} {' OK ' if ok else 'FAIL'}")
        if not ok:
            all_ok = False

        label = "oddities: beacons-history walk"
        try:
            got = run_walker_subcommand(lang, binary, "beacons-history", [
                "--period", "604800",
                "--win-start", "0",
                "--projects-root", str(root),
                "--now", repr(now_unix),
            ])
            got_pairs = sorted(
                (p["begin_eta"], p["actual_elapsed"]) for p in got.get("pairs", []))
            ok = (
                got_pairs == [(200.0, 300.0), (600.0, 500.0)]
                and got.get("n_pairs") == 2
                and got.get("session_count") == 2
                and abs((got.get("bias_factor") or 0.0)
                        - ((500.0 / 600.0 + 300.0 / 200.0) / 2.0)) <= BIAS_TOLERANCE
            )
        except Exception as e:
            print(f"  [{lang:>4s}] {label:38s} FAIL  {e}")
            ok = False
        print(f"  [{lang:>4s}] {label:38s} {' OK ' if ok else 'FAIL'}")
        if not ok:
            all_ok = False

    return all_ok


def check_mtime_prune(lang: str, binary: Path, expected: dict) -> bool:
    """Cost-mode mtime pruning. Conformance fixtures are copied fresh (mtime =
    now, far after the pinned cutoff), so the prune branch never fired. Age
    one fixture's file far before the pinned cutoff via os.utime and assert
    its cost vanishes from the totals: once with an aged PARENT transcript,
    once with an aged SUBAGENT transcript (separate branch in some impls).
    """
    meta = expected["_meta"]
    fix_keep = "01-single-parent"
    fix_prune = "07-unknown-model"
    target = {
        "trailing_usd": expected["fixtures"][fix_keep]["trailing_usd"],
        "window_usd": expected["fixtures"][fix_keep]["window_usd"],
    }
    cutoff = min(meta["now_unix"] - meta["period_seconds"], meta["win_start_unix"])
    aged = (cutoff - 30 * 86400, cutoff - 30 * 86400)
    all_ok = True

    # --- aged parent transcript ---
    label = "mtime-prune: parent"
    with tempfile.TemporaryDirectory(prefix="walker-prune-parent-") as tmp:
        root = Path(tmp)
        shutil.copytree(CORPUS / fix_keep, root / fix_keep)
        shutil.copytree(CORPUS / fix_prune, root / fix_prune)
        for f in (root / fix_prune).glob("*.jsonl"):
            os.utime(f, aged)
        try:
            got = run_walker(lang, binary, meta, root)
            ok, _, _ = within_tolerance(got, target)
        except Exception as e:
            print(f"  [{lang:>4s}] {label:38s} FAIL  {e}")
            ok = False
    print(f"  [{lang:>4s}] {label:38s} {' OK ' if ok else 'FAIL'}")
    if not ok:
        all_ok = False

    # --- aged subagent transcript ---
    label = "mtime-prune: subagent"
    with tempfile.TemporaryDirectory(prefix="walker-prune-sub-") as tmp:
        root = Path(tmp)
        shutil.copytree(CORPUS / fix_keep, root / fix_keep)
        parent_file = next((root / fix_keep).glob("*.jsonl"))
        sub = root / fix_keep / parent_file.stem / "subagents"
        sub.mkdir(parents=True)
        agent_file = sub / "agent-aged.jsonl"
        shutil.copy(next((CORPUS / fix_prune).glob("*.jsonl")), agent_file)
        os.utime(agent_file, aged)
        try:
            got = run_walker(lang, binary, meta, root)
            ok, _, _ = within_tolerance(got, target)
        except Exception as e:
            print(f"  [{lang:>4s}] {label:38s} FAIL  {e}")
            ok = False
    print(f"  [{lang:>4s}] {label:38s} {' OK ' if ok else 'FAIL'}")
    if not ok:
        all_ok = False
    return all_ok


def check_search_mtime_prune(lang: str, binary: Path) -> bool:
    """SPEC §search: 'the --since mtime fast-path prune applies per file to
    parents and subagents alike.' Age the subagent transcript of the
    18-subagent-traversal scenario to 1970 and search with --since 365d:
    the parent hit survives (fresh copy mtime), the subagent hit must be
    pruned without ever parsing the file."""
    if not SEARCH_CORPUS.is_dir() or lang not in IMPLS_WITH_SEARCH:
        return True
    scenario_name = "18-subagent-traversal"
    expected_file = SEARCH_CORPUS / scenario_name / "expected.json"
    if not expected_file.is_file():
        return True
    data = json.loads(expected_file.read_text(encoding="utf-8"))
    combo = data["combos"]["default"]
    now_unix = data["_meta"]["now_unix"]
    label = "search: --since mtime prune"
    with tempfile.TemporaryDirectory(prefix="walker-search-mtime-") as tmp:
        scen = Path(tmp) / scenario_name
        shutil.copytree(SEARCH_CORPUS / scenario_name, scen)
        aged: list[Path] = []
        for sub in scen.rglob("agent-*.jsonl"):
            os.utime(sub, (0, 0))
            aged.append(sub)
        try:
            got_hits, got_summary = run_walker_search(
                lang, binary, Path(tmp),
                combo["pattern"], ["--since", "365d"], now_unix,
            )
        except Exception as e:
            print(f"  [{lang:>4s}] {label:38s} FAIL  {e}")
            return False
    # Expected: the combo's hits minus those sourced from the aged subagent
    # files. Subagent hits share the parent's session_id, so identify them
    # by snippet instead (the two fixtures carry distinct text).
    sub_snippets = set()
    for sub in aged:
        # The fixture generator writes one assistant turn per subagent file.
        for line in (SEARCH_CORPUS / scenario_name /
                     sub.relative_to(scen)).read_text(encoding="utf-8").splitlines():
            if line.strip():
                blocks = json.loads(line)["message"]["content"]
                sub_snippets.update(b["text"] for b in blocks
                                    if b.get("type") == "text")
    exp_hits = [h for h in combo["hits"] if h["snippet"] not in sub_snippets]
    sessions = len({h["session_id"] for h in exp_hits})
    ok = (got_hits == exp_hits
          and got_summary is not None
          and got_summary.get("hits") == len(exp_hits)
          and got_summary.get("sessions_matched") == sessions)
    print(f"  [{lang:>4s}] {label:38s} {' OK ' if ok else 'FAIL'}")
    if not ok:
        print(f"        got {len(got_hits)} hits, expected {len(exp_hits)}; "
              f"summary={got_summary}")
        return False

    # Phase 2: age the PARENT transcripts too -- the parent-side prune arm
    # is distinct code in every impl (zig walks parents and subagents in
    # separate scanners). All hits must now be pruned.
    label2 = "search: --since mtime prune (parents)"
    with tempfile.TemporaryDirectory(prefix="walker-search-mtime2-") as tmp:
        scen = Path(tmp) / scenario_name
        shutil.copytree(SEARCH_CORPUS / scenario_name, scen)
        for transcript in scen.rglob("*.jsonl"):
            os.utime(transcript, (0, 0))
        try:
            got_hits, got_summary = run_walker_search(
                lang, binary, Path(tmp),
                combo["pattern"], ["--since", "365d"], now_unix,
            )
        except Exception as e:
            print(f"  [{lang:>4s}] {label2:38s} FAIL  {e}")
            return False
    ok = (got_hits == [] and got_summary is not None
          and got_summary.get("hits") == 0)
    print(f"  [{lang:>4s}] {label2:38s} {' OK ' if ok else 'FAIL'}")
    if not ok:
        print(f"        got {len(got_hits)} hits, expected 0; "
              f"summary={got_summary}")
    return ok


def check_search_tool_blocks_rich(lang: str, binary: Path) -> bool:
    """--include-tool-blocks over rich tool_use/tool_result shapes (nested
    objects, arrays, numbers, bools, nulls, string inputs, blocks without a
    type, non-object blocks, scalar content).

    SPEC "Tool blocks" (decided 2026-06-10) pins the serialization: compact
    JSON, source key order, string inputs keep their quotes. The canonical
    combos match INSIDE the tool dump and assert those exact fragments; the
    first two combos keep the original looser shape (the pattern sits in a
    plain text block, far from the dump).
    """
    if not SEARCH_CORPUS.is_dir() or lang not in IMPLS_WITH_SEARCH:
        return True
    now_unix = 1778414400.0
    ts = "2026-05-09T11:58:00.000Z"
    pad = "padding words " * 30  # keep the snippet window inside the text block
    rich_input = {
        "cmd": "run", "count": 3, "ratio": 1.5, "ok": True, "none": None,
        "obj": {"k": [1, "two", False, None, {"d": 2.5}]},
        "arr": [{"x": "y"}, [2, 3], "s", True, None],
        # Escape-needing characters inside dumped tool-input strings.
        "esc": "tab\there\nquote\"back\\slash bs\bff\fcr\rctl\x01",
        # Searchable needle INSIDE the dump for the canonical-form combo.
        "needle_key": "rich-tool-needle",
    }
    lines = [
        {"type": "assistant", "timestamp": ts, "message": {
            "id": "m-rich", "role": "assistant", "model": "claude-opus-4-7",
            "content": [
                {"type": "text", "text": pad + " rich-needle " + pad},
                {"type": "tool_use", "id": "t1", "name": "Bash",
                 "input": rich_input},
                {"type": "tool_use", "id": "t2", "name": "Bash",
                 "input": "pre-stringified input"},
            ],
            "usage": {"input_tokens": 1, "output_tokens": 1}}},
        # Decoy shapes that must be skipped silently in every impl.
        {"type": "user", "timestamp": ts, "message": {
            "role": "user", "content": 42}},
        {"type": "user", "timestamp": ts, "message": {
            "role": "user", "content": ["bare string block",
                                        {"text": "typeless block"},
                                        {"type": "image", "source": "x"}]}},
        {"type": "user", "timestamp": ts, "message": {
            "role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1"},
                {"type": "tool_result", "tool_use_id": "t2", "content": 7},
                {"type": "tool_result", "tool_use_id": "t3",
                 "content": "plain result string"},
            ]}},
        # Text block FIRST, then tool_result string + array, then a SECOND
        # text block: exercises the newline-join arms of both the text and
        # tool-block extraction paths.
        {"type": "user", "timestamp": ts, "message": {
            "role": "user", "content": [
                {"type": "text", "text": "lead text"},
                {"type": "tool_result", "tool_use_id": "t5",
                 "content": "trailing result"},
                {"type": "tool_result", "tool_use_id": "t6",
                 "content": [{"type": "text", "text": "inner text"},
                             {"type": "image", "src": "x"},
                             "bare-inner"]},
                {"type": "text", "text": "tail text"},
            ]}},
        # Content whose FIRST (and only) block is an object without a type:
        # the only-tool-blocks classifier must bail on the missing type.
        {"type": "user", "timestamp": ts, "message": {
            "role": "user", "content": [{"text": "typeless only"}]}},
    ]
    all_ok = True
    with tempfile.TemporaryDirectory(prefix="walker-search-rich-") as tmp:
        scen = Path(tmp) / "rich-tools"
        scen.mkdir()
        with (scen / "richsess.jsonl").open("w", encoding="utf-8",
                                            newline="\n") as f:
            for line in lines:
                f.write(json.dumps(line))
                f.write("\n")
        for combo_label, flags in (
            ("search: rich tool blocks (default)", []),
            ("search: rich tool blocks (include)", ["--include-tool-blocks"]),
        ):
            cmd = [
                str(binary), "search", "rich-needle",
                "--projects-root", tmp,
                "--now", repr(now_unix),
                "--format", "jsonl",
                "--no-config",
                *flags,
            ]
            result = run_captured(cmd, text=True,
                                    encoding="utf-8", timeout=10)
            ok = result.returncode == 0
            hits = []
            if ok:
                try:
                    hits = [json.loads(line) for line in result.stdout.splitlines()
                            if line.strip() and json.loads(line).get("type") == "hit"]
                except Exception:
                    ok = False
            ok = (ok and len(hits) == 1
                  and hits[0].get("session_id") == "richsess"
                  and "rich-needle" in hits[0].get("snippet", ""))
            print(f"  [{lang:>4s}] {combo_label:48s} {' OK ' if ok else 'FAIL'}")
            if not ok:
                print(f"        exit={result.returncode} hits={len(hits)} "
                      f"stderr={result.stderr.strip()[:160]!r}")
                all_ok = False

        # Canonical-form combos (SPEC "Tool blocks"): the pattern matches
        # inside the dumped tool_use input, and the snippet must carry the
        # canonical serialization fragments verbatim. Escape-free fragments
        # only -- escape rendering of control characters stays impl-local.
        for combo_label, pattern, fragments in (
            ("search: tool dump canonical object form",
             "rich-tool-needle",
             ['"needle_key":"rich-tool-needle"',
              '"obj":{"k":[1,"two",false,null,{"d":2.5}]}',
              '"none":null',
              '"ratio":1.5']),
            ("search: tool dump string input keeps quotes",
             "pre-stringified",
             ['"pre-stringified input"']),
        ):
            cmd = [
                str(binary), "search", pattern,
                "--projects-root", tmp,
                "--now", repr(now_unix),
                "--format", "jsonl",
                "--no-config",
                "--include-tool-blocks",
                "--snippet-chars", "600",
            ]
            result = run_captured(cmd, text=True,
                                    encoding="utf-8", timeout=10)
            ok = result.returncode == 0
            hits = []
            if ok:
                try:
                    hits = [json.loads(line) for line in result.stdout.splitlines()
                            if line.strip() and json.loads(line).get("type") == "hit"]
                except Exception:
                    ok = False
            snippet = hits[0].get("snippet", "") if hits else ""
            missing = [f for f in fragments if f not in snippet]
            ok = ok and len(hits) == 1 and not missing
            print(f"  [{lang:>4s}] {combo_label:48s} {' OK ' if ok else 'FAIL'}")
            if not ok:
                print(f"        exit={result.returncode} hits={len(hits)} "
                      f"missing={missing!r} snippet={snippet[:200]!r}")
                all_ok = False
    return all_ok


def check_home_fallbacks(lang: str, binary: Path, expected: dict) -> bool:
    """Home-dir resolution fallbacks that check_config_resolution never hits:
    (a) the canonical home var unset, the cross-platform fallback var set;
    (b) BOTH unset -> the relative `.claude/projects` default, resolved
    against the process CWD."""
    meta = expected["_meta"]
    names = list(expected["fixtures"].keys())
    fixture = max(names, key=lambda n: expected["fixtures"][n]["trailing_usd"])
    target = {
        "trailing_usd": expected["fixtures"][fixture]["trailing_usd"],
        "window_usd": expected["fixtures"][fixture]["window_usd"],
    }
    # No --no-config: config resolution must also fall back (the fake home
    # carries no walker-roots.json, and the relative-default variant resolves
    # `.claude/walker-roots.json` against the CWD), exercising the
    # config-path fallbacks alongside the projects-root ones.
    base_cmd = [
        str(binary),
        "--period", str(meta["period_seconds"]),
        "--win-start", repr(meta["win_start_unix"]),
        "--now", repr(meta["now_unix"]),
    ]
    all_ok = True
    for label, drop_vars, set_fallback, use_cwd in (
        ("home-fallback: cross var",
         ("USERPROFILE",) if sys.platform == "win32" else ("HOME",), True, False),
        ("home-fallback: relative default", ("HOME", "USERPROFILE"), False, True),
    ):
        with tempfile.TemporaryDirectory(prefix="walker-home-fb-") as home:
            home_p = Path(home)
            shutil.copytree(CORPUS / fixture,
                            home_p / ".claude" / "projects" / fixture)
            env = dict(os.environ)
            for var in drop_vars:
                env.pop(var, None)
            if set_fallback:
                # The opposite-platform var is the documented fallback.
                fallback = "HOME" if sys.platform == "win32" else "USERPROFILE"
                env[fallback] = str(home_p)
            try:
                result = run_captured(
                    base_cmd, text=True, encoding="utf-8",
                    timeout=10, env=env, cwd=str(home_p) if use_cwd else None,
                )
                got = json.loads(result.stdout.strip().splitlines()[-1])
                ok, _, _ = within_tolerance(got, target)
                ok = ok and result.returncode == 0
                # Same fallback resolution through the events subcommand's
                # default-root path: exit 0 with records on stdout.
                ev_cmd = [str(binary), "events", *base_cmd[1:]]
                ev = run_captured(
                    ev_cmd, text=True, encoding="utf-8",
                    timeout=10, env=env, cwd=str(home_p) if use_cwd else None,
                )
                ok = ok and ev.returncode == 0 and ev.stdout.strip() != ""
            except Exception as e:
                print(f"  [{lang:>4s}] {label:38s} FAIL  {e}")
                all_ok = False
                continue
        print(f"  [{lang:>4s}] {label:38s} {' OK ' if ok else 'FAIL'}")
        if not ok:
            print(f"        exit={result.returncode} stdout={result.stdout!r}")
            all_ok = False
    return all_ok


def main():
    if not EXPECTED.is_file():
        print(f"missing {EXPECTED} -- run shared/generate_corpus.py first")
        sys.exit(2)
    expected = json.loads(EXPECTED.read_text(encoding="utf-8"))

    requested = sys.argv[1:] or list(CANDIDATES.keys())
    overall_ok = True
    print(f"Conformance corpus: {CORPUS}")
    print(f"Pinned now={expected['_meta']['now_unix']}  "
          f"period={expected['_meta']['period_seconds']}  "
          f"win_start={expected['_meta']['win_start_unix']}")
    print(f"Fixtures: {len(expected['fixtures'])}\n")

    for lang in requested:
        binary = find_binary(lang)
        if binary is None:
            print(f"  [{lang:>4s}] SKIP  no built binary "
                  f"(checked {[str(p) for p in CANDIDATES.get(lang, [])]})")
            continue
        if not check_implementation(lang, binary, expected):
            overall_ok = False
        if not check_beacons(lang, binary):
            overall_ok = False
        if not check_multi_root(lang, binary):
            overall_ok = False
        if not check_config_resolution(lang, binary, expected):
            overall_ok = False
        if not check_config_malformed(lang, binary, expected):
            overall_ok = False
        if not check_search(lang, binary):
            overall_ok = False
        if not check_search_pretty(lang, binary):
            overall_ok = False
        if not check_search_truncated(lang, binary):
            overall_ok = False
        if not check_search_multi_root(lang, binary):
            overall_ok = False
        if not check_search_duplicate_root(lang, binary):
            overall_ok = False
        if not check_events(lang, binary):
            overall_ok = False
        if not check_help(lang, binary):
            overall_ok = False
        if not check_empty_root(lang, binary, expected):
            overall_ok = False
        if not check_cli_argument_matrix(lang, binary):
            overall_ok = False
        if not check_omit_now_smoke(lang, binary):
            overall_ok = False
        if not check_cost_subcommand(lang, binary, expected):
            overall_ok = False
        if not check_beacons_extra_root(lang, binary):
            overall_ok = False
        if not check_events_extra_root_profile(lang, binary):
            overall_ok = False
        if not check_search_cpuprofile(lang, binary):
            overall_ok = False
        if not check_discovery_oddities(lang, binary, expected):
            overall_ok = False
        if not check_mtime_prune(lang, binary, expected):
            overall_ok = False
        if not check_search_mtime_prune(lang, binary):
            overall_ok = False
        if not check_search_tool_blocks_rich(lang, binary):
            overall_ok = False
        if not check_home_fallbacks(lang, binary, expected):
            overall_ok = False
        print()

    sys.exit(0 if overall_ok else 1)


if __name__ == "__main__":
    main()
