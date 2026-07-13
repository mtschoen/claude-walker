"""Benchmark each available implementation across all five walker modes.

Usage:
    python shared/bench.py [--runs 5] [--mode MODE] [--all-modes]
                           [--corpus DIR] [--target-mb 150] [--regen]
                           [--interleave] [--no-python] [--live] [<lang> ...]

By default the bench runs against a FIXED synthetic perf corpus
(`shared/corpus-perf/`, auto-generated via generate_perf_corpus.py if absent)
so results are reproducible -- the live `~/.claude/projects` fleet changes
constantly and can't be compared run-to-run. Pass `--live` to bench the live
fleet instead (cost / events / beacons-history only; the per-session search
and beacons-latest modes need the manifest's pinned id/pattern).

Modes (`--mode`, or `--all-modes` for every applicable one):
    cost            trailing + window USD over the fleet (default)
    events          one NDJSON line per assistant turn (/spend feed)
    beacons-history begin/end pairing + bias_factor over the window
    beacons-latest  newest beacon in one pinned session (corpus only)
    search          full-text scan for the pinned pattern (corpus only)

In `cost` mode the Python reference is the real shipping statusline parallel
fleet-walk (`statusline_lib.pace._walk_pace_hourly`), pointed at the corpus
and summed to a USD total -- the only remaining pure-Python fleet walker.
The other modes have no Python implementation (statusline shells out to the
native walker), so their Python row is skipped.

By default the native impls run sequentially (rust x N, then cpp x N, ...);
`--interleave` round-robins them after a warm-up so background noise smears
evenly. Reports min / median / max wall-clock, the median walker-reported
`elapsed_ms` (in-binary work, exposing per-process startup overhead), each
mode's headline output, and a speedup summary.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from process_safe import run_captured, run_inherit

ROOT = Path(__file__).resolve().parent.parent
HOME = Path(os.path.expanduser("~"))
DEFAULT_CORPUS = ROOT / "shared" / "corpus-perf"

CANDIDATES = {
    "rust": [
        ROOT / "rust" / "target" / "release" / "walker.exe",
        ROOT / "rust" / "target" / "release" / "walker",
    ],
    "go": [ROOT / "go" / "walker.exe", ROOT / "go" / "walker"],
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

ALL_MODES = ("cost", "events", "beacons-history", "beacons-latest", "search")
# Modes that need a pinned session-id / pattern from the corpus manifest, so
# they only run in corpus mode (not against the live fleet).
CORPUS_ONLY_MODES = ("beacons-latest", "search")

LIVE_PERIOD = 7 * 86400  # weekly window for --live


def find_binary(lang: str):
    for path in CANDIDATES.get(lang, []):
        if path.is_file():
            return path
    return None


def ensure_corpus(corpus_dir: Path, target_mb: float, regen: bool) -> dict:
    """Return the corpus manifest, generating the corpus first if needed."""
    manifest_path = corpus_dir / "manifest.json"
    if regen or not manifest_path.is_file():
        gen = ROOT / "shared" / "generate_perf_corpus.py"
        print(f"Generating perf corpus (~{target_mb:.0f} MB) at {corpus_dir} ...")
        cmd = [
            sys.executable,
            str(gen),
            "--target-mb",
            str(target_mb),
            "--out",
            str(corpus_dir),
        ]
        if regen:
            cmd.append("--force")
        rc = run_inherit(cmd)
        if rc != 0:
            raise subprocess.CalledProcessError(rc, cmd)
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def corpus_context(manifest: dict, corpus_dir: Path) -> dict:
    return {
        "live": False,
        "root": corpus_dir,
        "now": manifest["now_unix"],
        "period": int(manifest["period_seconds"]),
        "win_start": manifest["win_start_unix"],
        "session_id": manifest["beacon_session_id"],
        # The dense beacons-latest session lives in its own root, passed only to
        # the beacons-latest run as --extra-projects-root (older manifests
        # without this key fall back to the main corpus root).
        "beacon_latest_root": manifest.get("beacon_latest_root"),
        "pattern": manifest["search_pattern"],
    }


def live_context() -> dict:
    now = time.time()
    return {
        "live": True,
        "root": None,
        "now": now,
        "period": LIVE_PERIOD,
        "win_start": now - LIVE_PERIOD + 3600,
        "session_id": None,
        "beacon_latest_root": None,
        "pattern": None,
    }


def build_cmd(binary: Path, mode: str, ctx: dict) -> list:
    """Construct the argv for one (binary, mode) under the bench context."""
    root_flags = ["--no-config"]
    if ctx["root"] is not None:
        root_flags = ["--projects-root", str(ctx["root"]), "--no-config"]
    window = [
        "--period",
        str(ctx["period"]),
        "--win-start",
        repr(ctx["win_start"]),
        "--now",
        repr(ctx["now"]),
    ]
    base = [str(binary)]
    if mode == "cost":
        return base + window + root_flags
    if mode == "events":
        return base + ["events"] + window + root_flags
    if mode == "beacons-history":
        return base + ["beacons-history"] + window + root_flags
    if mode == "beacons-latest":
        # The dense session lives in a separate root so it is not a straggler for
        # the other (full-fleet) modes; beacons-latest reaches it via an extra
        # root while still traversing the main corpus to find it realistically.
        extra_root = []
        if ctx.get("beacon_latest_root"):
            extra_root = ["--extra-projects-root", str(ctx["beacon_latest_root"])]
        return (
            base
            + [
                "beacons-latest",
                "--session-id",
                ctx["session_id"],
                "--now",
                repr(ctx["now"]),
            ]
            + root_flags
            + extra_root
        )
    if mode == "search":
        # --count-only does the full scan + match but skips snippet/context
        # building, so the timing reflects the dominant I/O + parse + match path
        # with clean, deterministic single-line output.
        return (
            base
            + ["search", ctx["pattern"], "--count-only", "--format", "jsonl"]
            + root_flags
        )
    raise ValueError(f"unknown mode: {mode}")


def run_once(cmd: list):
    """One timed invocation. Returns (wall_ms, stdout, err)."""
    t0 = time.perf_counter()
    result = run_captured(cmd, timeout=60)
    wall = (time.perf_counter() - t0) * 1000
    if result.returncode != 0:
        return None, None, result.stderr
    return wall, result.stdout, None


def summarize(mode: str, stdout: str):
    """Extract (walker_ms, headline_str) from a mode's stdout."""
    if mode == "events":
        lines = [ln for ln in stdout.splitlines() if ln.strip()]
        return None, f"events={len(lines)}"
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    if not lines:
        return None, ""
    try:
        obj = json.loads(lines[-1])
    except (ValueError, IndexError):
        return None, ""
    walker_ms = obj.get("elapsed_ms")
    if mode == "cost":
        head = (
            f"trailing=${obj.get('trailing_usd', 0):.2f} "
            f"window=${obj.get('window_usd', 0):.2f} "
            f"files={obj.get('files_walked', 0)}"
        )
    elif mode == "beacons-history":
        bias = obj.get("bias_factor")
        bias_str = f"{bias:.4f}" if bias is not None else "n/a"
        head = f"n_pairs={obj.get('n_pairs', 0)} bias={bias_str}"
    elif mode == "beacons-latest":
        beacon = obj.get("beacon")
        kind = beacon.get("kind") if isinstance(beacon, dict) else "none"
        head = f"beacon={kind} age={obj.get('age_seconds')}"
    elif mode == "search":
        head = (
            f"hits={obj.get('hits', 0)} "
            f"sessions={obj.get('sessions_matched', 0)} "
            f"files={obj.get('files_walked', 0)}"
        )
    else:
        head = ""
    return walker_ms, head


def time_binary(binary: Path, mode: str, ctx: dict, runs: int):
    """Sequential timing: run one impl `runs` times back-to-back."""
    cmd = build_cmd(binary, mode, ctx)
    elapsed, walker = [], []
    head = ""
    for _ in range(runs):
        wall, stdout, err = run_once(cmd)
        if err is not None:
            return None, None, None, err
        elapsed.append(wall)
        walker_ms, head = summarize(mode, stdout)
        if walker_ms is not None:
            walker.append(walker_ms)
    return elapsed, walker, head, None


def time_binaries_interleaved(specs: list, mode: str, ctx: dict, runs: int):
    """Round-robin timing across specs after an untimed warm-up round."""
    cmds = {lang: build_cmd(binary, mode, ctx) for lang, binary in specs}
    elapsed = {lang: [] for lang, _ in specs}
    walker = {lang: [] for lang, _ in specs}
    heads = {lang: "" for lang, _ in specs}
    errors = {lang: None for lang, _ in specs}

    for lang, _ in specs:  # warm-up (untimed): touch the disk cache for each
        _, _, err = run_once(cmds[lang])
        if err is not None:
            errors[lang] = err

    for _ in range(runs):
        for lang, _ in specs:
            if errors[lang] is not None:
                continue
            wall, stdout, err = run_once(cmds[lang])
            if err is not None:
                errors[lang] = err
                continue
            elapsed[lang].append(wall)
            walker_ms, heads[lang] = summarize(mode, stdout)
            if walker_ms is not None:
                walker[lang].append(walker_ms)
    return elapsed, walker, heads, errors


def time_python_cost(ctx: dict, runs: int):
    """Bench the shipping statusline parallel Python fleet-walk against the corpus.

    `_walk_pace_hourly` is the only remaining pure-Python fleet walker; it
    resolves roots via `_walker_root_list`, which we monkeypatch to the corpus.
    Summing its hourly buckets yields a USD total comparable to native cost mode.
    """
    sys.path.insert(0, str(HOME / "schoen-claude-status"))
    try:
        import statusline_lib.pace as pace
    except ImportError as e:
        return None, None, f"could not import statusline_lib.pace: {e}"
    if ctx["root"] is not None:
        root = str(ctx["root"])
        pace._walker_root_list = lambda: [root]
    elapsed, total = [], 0.0
    for _ in range(runs):
        t0 = time.perf_counter()
        hourly = pace._walk_pace_hourly(ctx["win_start"])
        elapsed.append((time.perf_counter() - t0) * 1000)
        total = sum(hourly)
    return elapsed, f"trailing=${total:.2f} (hourly-pace fleet walk)", None


def fmt_stats(label: str, elapsed: list, walker: list, head: str):
    e = sorted(elapsed)
    median = e[len(e) // 2]
    line = (
        f"  {label:18s}  min={e[0]:>6.0f}ms  median={median:>6.0f}ms  "
        f"max={e[-1]:>6.0f}ms"
    )
    if walker:
        w = sorted(walker)
        line += f"  walker={w[len(w) // 2]:>5.0f}ms"
    if head:
        line += f"  {head}"
    return line, median


def run_mode(
    mode: str, specs: list, ctx: dict, runs: int, interleave: bool, do_python: bool
):
    print(f"\n=== mode: {mode} ===")
    medians: dict[str, float] = {}

    if mode == "cost" and do_python:
        elapsed, head, err = time_python_cost(ctx, runs)
        if err:
            print(f"  {'python':18s}  SKIP  {err}")
        else:
            line, m = fmt_stats("python (parallel)", elapsed, [], head)
            print(line)
            medians["python"] = m
    elif mode in CORPUS_ONLY_MODES and ctx["live"]:
        print(f"  (skipped: {mode} needs the corpus manifest's pinned id/pattern)")
        return medians

    if interleave:
        elapsed_map, walker_map, head_map, error_map = time_binaries_interleaved(
            specs, mode, ctx, runs
        )
        for lang, _ in specs:
            if error_map[lang] is not None:
                print(f"  {lang:18s}  FAIL  {error_map[lang].strip()[:120]}")
                continue
            line, m = fmt_stats(
                lang, elapsed_map[lang], walker_map[lang], head_map[lang]
            )
            print(line)
            medians[lang] = m
    else:
        for lang, binary in specs:
            elapsed, walker, head, err = time_binary(binary, mode, ctx, runs)
            if err:
                print(f"  {lang:18s}  FAIL  {err.strip()[:120]}")
                continue
            line, m = fmt_stats(lang, elapsed, walker, head)
            print(line)
            medians[lang] = m

    if medians:
        baseline = max(medians.values())
        print(
            "  speedup vs slowest:  "
            + "  ".join(
                f"{label}={baseline / m:.2f}x"
                for label, m in sorted(medians.items(), key=lambda kv: kv[1])
            )
        )
    return medians


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--mode", choices=ALL_MODES, default="cost")
    parser.add_argument(
        "--all-modes",
        action="store_true",
        help="Bench every applicable mode, not just --mode",
    )
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument(
        "--target-mb",
        type=float,
        default=150.0,
        help="Corpus size to generate if absent (default: 150)",
    )
    parser.add_argument(
        "--regen", action="store_true", help="Regenerate the corpus before benching"
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Bench the live ~/.claude/projects fleet instead of the fixed corpus",
    )
    parser.add_argument("--interleave", action="store_true")
    parser.add_argument("--no-python", action="store_true")
    parser.add_argument("langs", nargs="*", default=None)
    args = parser.parse_args()

    if args.live:
        ctx = live_context()
        scope = "live ~/.claude/projects fleet (weekly window)"
    else:
        manifest = ensure_corpus(args.corpus, args.target_mb, args.regen)
        ctx = corpus_context(manifest, args.corpus.resolve())
        scope = (
            f"corpus {args.corpus} -- {manifest['file_count']} files, "
            f"{manifest['total_bytes'] / 1024 / 1024:.0f} MB"
        )

    modes = ALL_MODES if args.all_modes else (args.mode,)
    if args.live:
        modes = tuple(m for m in modes if m not in CORPUS_ONLY_MODES)

    order = "interleaved" if args.interleave else "sequential"
    print(f"Bench  --  {scope}\n{args.runs} runs each ({order})")

    specs = []
    requested = args.langs or list(CANDIDATES.keys())
    for lang in requested:
        binary = find_binary(lang)
        if binary is None:
            print(f"  {lang:18s}  SKIP  no binary")
            continue
        specs.append((lang, binary))

    for mode in modes:
        run_mode(mode, specs, ctx, args.runs, args.interleave, not args.no_python)


if __name__ == "__main__":
    main()
