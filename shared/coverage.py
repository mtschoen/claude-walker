#!/usr/bin/env python3
"""Coverage orchestrator for agent-walker's four implementations.

Builds each implementation in an *instrumented* mode, drives the existing
``conformance.py`` harness against the instrumented binary (so every walker
subprocess emits coverage data), then collects per-language line coverage and
writes ``TEST-REPORT.md`` at the repo root.

The conformance harness runs the binary as a subprocess inheriting our
environment, so we hook coverage in by (a) setting the right env vars
(``LLVM_PROFILE_FILE``, ``GOCOVERDIR``, …) and (b) pointing the harness at the
instrumented build via ``WALKER_BIN_<LANG>`` (see ``conformance.find_binary``).

Per-language mechanism:

    rust -> cargo-llvm-cov, "external test" show-env workflow. Binary lands at
            the normal rust/target/release/walker path.
    cpp  -> clang -fprofile-instr-generate -fcoverage-mapping (WALKER_COVERAGE
            CMake option), separate cpp/build-cov tree; llvm-profdata + llvm-cov.
            Vendored simdjson (build/_deps) excluded from the denominator.
    go   -> `go build -cover` into go/walker-cov; GOCOVERDIR per run; go tool
            covdata textfmt -> statement counts.
    zig  -> LLVM-backend Debug build (`-Dcoverage=true`; the default self-hosted
            backend emits DWARF kcov cannot parse), run under a DWARF5-capable
            kcov (see find_kcov). Each invocation accumulates into one outdir.

Usage:
    python shared/coverage.py [rust cpp go zig]   # default: all available
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from process_safe import ProcessResult, run_captured

ROOT = Path(__file__).resolve().parent.parent
RUST_DIR = ROOT / "rust"
CPP_DIR = ROOT / "cpp"
GO_DIR = ROOT / "go"
ZIG_DIR = ROOT / "zig"
COVERAGE_DIR = ROOT / "coverage"
CONFORMANCE = ROOT / "shared" / "conformance.py"
REPORT = ROOT / "TEST-REPORT.md"
SUMMARY = COVERAGE_DIR / "summary.json"  # machine-readable sidecar for CI

ALL_LANGS = ["rust", "cpp", "go", "zig"]


@dataclass
class Result:
    lang: str
    metric: str                       # "lines" | "statements"
    covered: int
    total: int
    files: list[tuple[str, int, int]] = field(default_factory=list)  # (name, covered, total)
    conformance_ok: bool = False
    conformance_passed: int = 0
    conformance_failed: int = 0
    note: str = ""

    @property
    def percent(self) -> float:
        return 100.0 * self.covered / self.total if self.total else 0.0

    @property
    def uncovered(self) -> int:
        return self.total - self.covered


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
# Backstop, not a tight budget: builds (cargo/cmake/zig) and the conformance
# harness they chain into can legitimately run for minutes. This only exists
# so a genuine hang (e.g. a wedged walker subprocess inside conformance.py's
# own run_captured calls, or a stuck compiler) fails in a bounded time instead
# of wedging the coverage run forever.
DEFAULT_RUN_TIMEOUT_SECONDS = 1800


def run(cmd, *, cwd=None, env=None, check=True, capture=True,
        timeout=DEFAULT_RUN_TIMEOUT_SECONDS) -> ProcessResult:
    assert capture, "coverage.run() only supports capture=True (no caller passes False)"
    return run_captured(
        cmd, cwd=cwd, env=env, check=check, timeout=timeout, encoding="utf-8",
    )


def run_conformance(lang: str, env: dict) -> tuple[bool, int, int]:
    """Run conformance.py for one language with `env`; return (ok, passed, failed)."""
    proc = run(
        [sys.executable, str(CONFORMANCE), lang],
        cwd=ROOT, env=env, check=False,
    )
    out = proc.stdout + proc.stderr
    passed = out.count(" OK ") + out.count(" OK\n")
    failed = sum(out.count(tok) for tok in ("FAIL", "MISMATCH"))
    ok = proc.returncode == 0
    if not ok:
        sys.stderr.write(f"\n[coverage] conformance({lang}) FAILED (exit {proc.returncode}):\n")
        sys.stderr.write("\n".join(out.splitlines()[-25:]) + "\n")
    return ok, passed, failed


def git_describe() -> tuple[str, str]:
    short = run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, check=False).stdout.strip()
    branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=ROOT, check=False).stdout.strip()
    return short or "unknown", branch or "unknown"


def _have(tool: str) -> bool:
    return shutil.which(tool) is not None


def find_kcov() -> str | None:
    """Locate a DWARF5-capable kcov.

    Stock kcov releases (and the liyu1981/zig-kcov fork) cannot parse the DWARF
    that modern Zig/Clang emit — they crash or silently report zero lines. The
    upstream master build does work, so we look for it explicitly first.
    Override with the KCOV env var.
    """
    explicit = os.environ.get("KCOV")
    if explicit and Path(explicit).is_file():
        return explicit
    candidates = [
        Path.home() / ".local/src/kcov-master/build/src/kcov",
        Path.home() / ".local/bin/kcov-master",
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    return shutil.which("kcov")


def _rust_test_module_starts() -> dict[str, int]:
    """Map of source file path -> first line of its `#[cfg(test)] mod tests {`
    block, or sys.maxsize when the file has no inline tests. Used to exclude
    test-module lines from the Rust coverage denominator, since cargo test's
    instrumentation otherwise counts test fns alongside production code.
    """
    starts: dict[str, int] = {}
    for src in (RUST_DIR / "src").rglob("*.rs"):
        first = sys.maxsize
        with src.open("r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, start=1):
                stripped = line.strip()
                if stripped.startswith("#[cfg(test)]"):
                    first = lineno
                    break
        starts[str(src)] = first
    return starts


def _accumulate_region_lines(export_json: str,
                             per_file_lines: dict[str, dict[int, bool]],
                             exclude: str | None = None) -> None:
    """Fold one `llvm-cov export` blob's per-function region detail into a
    {filename -> {line -> covered}} map with OR semantics: if any region from
    any export covers a line, the line is covered. Used to union coverage
    across multiple binaries built from the same sources (llvm-cov's own
    multi-object report treats each binary's copy of a function as a separate
    instantiation, which misreports lines missed in only one copy)."""
    import re as _re
    exclude_re = _re.compile(exclude) if exclude else None
    data = json.loads(export_json)
    block = data["data"][0]
    for fun in block.get("functions", []):
        filenames = fun.get("filenames", [])
        if not filenames:
            continue
        filename = filenames[0]
        # -ignore-filename-regex only filters the export's `files` array;
        # the `functions` detail still carries vendored/test sources.
        if exclude_re and exclude_re.search(filename):
            continue
        lines = per_file_lines.setdefault(filename, {})
        for region in fun.get("regions", []):
            # llvm-cov region tuple: [LineStart, ColStart, LineEnd, ColEnd,
            # ExecutionCount, FileID, ExpandedFileID, Kind]
            if len(region) < 5:
                continue
            line_start, line_end, count = region[0], region[2], region[4]
            covered = count > 0
            for ln in range(line_start, line_end + 1):
                lines[ln] = lines.get(ln, False) or covered


def _summarize_lines(per_file_lines: dict[str, dict[int, bool]],
                     cutoffs: dict[str, int] | None = None) -> tuple[int, int, list[tuple[str, int, int]]]:
    """Reduce a line map to (covered, total, per-file rows), optionally
    dropping lines at/after a per-file cutoff (the rust #[cfg(test)] marker)."""
    total = 0
    covered_count = 0
    files: list[tuple[str, int, int]] = []
    for filename, lines in per_file_lines.items():
        cutoff = (cutoffs or {}).get(filename, sys.maxsize)
        file_total = 0
        file_covered = 0
        for ln, line_covered in lines.items():
            if ln >= cutoff:
                continue
            file_total += 1
            if line_covered:
                file_covered += 1
        if file_total > 0:
            total += file_total
            covered_count += file_covered
            files.append((Path(filename).name, file_covered, file_total))
    files.sort()
    return covered_count, total, files


def _llvm_lines_excluding_tests(export_json: str, test_starts: dict[str, int]) -> tuple[int, int, list[tuple[str, int, int]]]:
    """Line summary from full region detail, excluding line ranges at or
    beyond each file's #[cfg(test)] marker so test-module instrumentation is
    removed from both numerator and denominator."""
    per_file_lines: dict[str, dict[int, bool]] = {}
    _accumulate_region_lines(export_json, per_file_lines)
    return _summarize_lines(per_file_lines, test_starts)


# --------------------------------------------------------------------------- #
# Rust — cargo-llvm-cov external-test workflow
# --------------------------------------------------------------------------- #
def _rust_env() -> dict:
    env = dict(os.environ)
    out = run([_CARGO, "llvm-cov", "show-env", "--sh"], cwd=RUST_DIR).stdout
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("export ") or "=" not in line:
            continue
        key, value = line[len("export "):].split("=", 1)
        env[key.strip()] = value.strip().strip("'\"")
    # The release profile strips symbols and uses fat LTO; both corrupt coverage
    # mapping. Override via env so we never have to touch Cargo.toml.
    env["CARGO_PROFILE_RELEASE_STRIP"] = "false"
    env["CARGO_PROFILE_RELEASE_LTO"] = "false"
    return env


_CARGO = "cargo"


def cov_rust() -> Result | None:
    if not _have(_CARGO) or not (RUST_DIR / "Cargo.toml").exists():
        return None
    if run([_CARGO, "llvm-cov", "--version"], check=False).returncode != 0:
        return Result("rust", "lines", 0, 0, note="cargo-llvm-cov not installed (cargo install cargo-llvm-cov)")
    print("[coverage] rust: build instrumented + run conformance + cargo test …")
    env = _rust_env()
    run([_CARGO, "llvm-cov", "clean", "--workspace"], cwd=RUST_DIR, env=env)
    run([_CARGO, "build", "--release"], cwd=RUST_DIR, env=env)
    ok, passed, failed = run_conformance("rust", env)
    # Also exercise #[cfg(test)] modules. `cargo test --release` produces
    # profraw files under the same LLVM_PROFILE_FILE pattern as the build,
    # so the unit-test runs merge into the same coverage report. A failing
    # unit test MUST fail the gate (a silent check=False here once masked
    # two broken tests).
    test_proc = run([_CARGO, "test", "--release", "--no-fail-fast"],
                    cwd=RUST_DIR, env=env, check=False)
    if test_proc.returncode != 0:
        ok = False
        failed += 1
        sys.stderr.write("[coverage] rust: cargo test FAILED:\n")
        sys.stderr.write((test_proc.stdout or "")[-2000:] + "\n")
    else:
        passed += 1
    # Use the full export (with function/region detail) so we can filter out
    # inline #[cfg(test)] mod tests blocks from the denominator. Otherwise the
    # cargo-test binary's instrumentation inflates both covered and total with
    # test-only code, distorting the metric.
    export_json = run(
        [_CARGO, "llvm-cov", "report", "--release", "--json"],
        cwd=RUST_DIR, env=env,
    ).stdout
    test_starts = _rust_test_module_starts()
    covered, total, files = _llvm_lines_excluding_tests(export_json, test_starts)
    return Result("rust", "lines", covered, total, files, ok, passed, failed)


# --------------------------------------------------------------------------- #
# C++ — clang source-based coverage
# --------------------------------------------------------------------------- #
def cov_cpp() -> Result | None:
    if not (_have("clang++") and _have("llvm-profdata") and _have("llvm-cov") and _have("cmake")):
        return Result("cpp", "lines", 0, 0, note="need clang++, cmake, llvm-profdata, llvm-cov")
    print("[coverage] cpp: configure WALKER_COVERAGE build + run conformance …")
    build = CPP_DIR / "build-cov"
    profraw_dir = build / "profraw"
    profraw_dir.mkdir(parents=True, exist_ok=True)
    for stale in profraw_dir.glob("*.profraw"):
        stale.unlink()
    cfg_env = dict(os.environ, CC="clang", CXX="clang++")
    cmake_args = [
        "cmake", "-S", str(CPP_DIR), "-B", str(build),
        "-DCMAKE_BUILD_TYPE=Debug", "-DWALKER_COVERAGE=ON",
        "-DWALKER_BUILD_TESTS=ON",
    ]
    # Reuse already-fetched simdjson if a normal build tree has it.
    fetched = CPP_DIR / "build" / "_deps"
    if fetched.is_dir():
        cmake_args.append(f"-DFETCHCONTENT_BASE_DIR={fetched}")
    run(cmake_args, env=cfg_env)
    run(["cmake", "--build", str(build), "-j"], env=cfg_env)
    binary = build / "walker"
    test_binary = build / "walker_unit_tests"
    env = dict(os.environ)
    env["LLVM_PROFILE_FILE"] = str(profraw_dir / "cpp-%p.profraw")
    env["WALKER_BIN_CPP"] = str(binary)
    ok, passed, failed = run_conformance("cpp", env)
    # Native unit tests cover the error arms (unreadable files, the worker
    # seam, lenient_count bounds) the harness cannot reach portably. The test
    # TU #includes the production sources, so its profile merges into the
    # same per-line counts via the extra -object below.
    test_proc = run([str(test_binary)], env=env, check=False)
    if test_proc.returncode != 0:
        ok = False
        failed += 1
        sys.stderr.write("[coverage] cpp: walker_unit_tests FAILED:\n")
        sys.stderr.write((test_proc.stderr or "")[-2000:] + "\n")
    else:
        passed += 1
    profdata = build / "cpp.profdata"
    raws = [str(p) for p in profraw_dir.glob("*.profraw")]
    run(["llvm-profdata", "merge", "-sparse", *raws, "-o", str(profdata)])
    # Export each binary separately and union per line. The two binaries
    # compile the same production sources (the test TU #includes them), and
    # llvm-cov's multi-object report counts each binary's copy of a function
    # as its own instantiation: a line missed in just one copy reports as
    # missed even when the other copy ran it.
    per_file_lines: dict[str, dict[int, bool]] = {}
    for obj in (binary, test_binary):
        export_json = run([
            "llvm-cov", "export", str(obj),
            f"-instr-profile={profdata}",
            "-ignore-filename-regex=_deps/|tests/",
        ]).stdout
        _accumulate_region_lines(export_json, per_file_lines,
                                 exclude=r"_deps/|/tests/")
    covered, total, files = _summarize_lines(per_file_lines)
    return Result("cpp", "lines", covered, total, files, ok, passed, failed)


# --------------------------------------------------------------------------- #
# Go — integration coverage (go build -cover)
# --------------------------------------------------------------------------- #
def cov_go() -> Result | None:
    if not _have("go") or not (GO_DIR / "go.mod").exists():
        return None
    print("[coverage] go: build -cover + run conformance + go test -cover …")
    binary = GO_DIR / "walker-cov"
    run(["go", "build", "-cover", "-o", str(binary), "."], cwd=GO_DIR)
    covdir = GO_DIR / "covdata"
    if covdir.exists():
        shutil.rmtree(covdir)
    covdir.mkdir()
    env = dict(os.environ, GOCOVERDIR=str(covdir), WALKER_BIN_GO=str(binary))
    ok, passed, failed = run_conformance("go", env)
    # Also run native unit tests, writing their coverage into a sibling dir
    # in covdata (binary) format so we can merge with the integration data.
    unitdir = GO_DIR / "covdata-unit"
    if unitdir.exists():
        shutil.rmtree(unitdir)
    unitdir.mkdir()
    test_proc = run(
        ["go", "test", "-count=1", "-cover", "./...",
         "-args", f"-test.gocoverdir={unitdir}"],
        cwd=GO_DIR, env=env, check=False,
    )
    if test_proc.returncode != 0:
        ok = False
        failed += 1
        sys.stderr.write("[coverage] go: go test FAILED:\n")
        sys.stderr.write((test_proc.stdout or "")[-2000:] + "\n")
    else:
        passed += 1
    # Merge integration + unit coverage by passing both dirs to textfmt.
    txt = GO_DIR / "go-cov.txt"
    merged_input = f"{covdir},{unitdir}" if any(unitdir.iterdir()) else str(covdir)
    run(["go", "tool", "covdata", "textfmt", f"-i={merged_input}", f"-o={txt}"], cwd=GO_DIR)
    covered, total, per_file = _go_counts(txt)
    files = sorted((Path(f).name, c, t) for f, (c, t) in per_file.items())
    return Result("go", "statements", covered, total, files, ok, passed, failed)


def _go_counts(txt_path: Path) -> tuple[int, int, dict[str, tuple[int, int]]]:
    total = covered = 0
    per_file: dict[str, list[int]] = {}
    for line in txt_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("mode:"):
            continue
        # "walker/main.go:120.13,134.2 4 1"  ->  loc  numstmt  count
        loc, numstmt, count = line.rsplit(" ", 2)
        fname = loc.split(":", 1)[0]
        statements, hits = int(numstmt), int(count)
        total += statements
        slot = per_file.setdefault(fname, [0, 0])
        slot[1] += statements
        if hits > 0:
            covered += statements
            slot[0] += statements
    return covered, total, {f: (c, t) for f, (c, t) in per_file.items()}


# --------------------------------------------------------------------------- #
# Zig — LLVM-backend build under kcov
# --------------------------------------------------------------------------- #
def cov_zig() -> Result | None:
    if not _have("zig") or not (ZIG_DIR / "build.zig").exists():
        return None
    kcov = find_kcov()
    if not kcov:
        return Result("zig", "lines", 0, 0,
                      note="no DWARF5-capable kcov found (build SimonKagstrom/kcov master; set $KCOV)")
    print("[coverage] zig: LLVM-backend build + run conformance under kcov …")
    for stale in (ZIG_DIR / ".zig-cache", ZIG_DIR / "zig-out"):
        if stale.exists():
            shutil.rmtree(stale)
    run(["zig", "build", "-Dcoverage=true", "-Doptimize=Debug"], cwd=ZIG_DIR)
    real_bin = ZIG_DIR / "zig-out" / "bin" / "walker"
    kcov_out = COVERAGE_DIR / "zig"
    runs_dir = COVERAGE_DIR / "zig-runs"
    for stale_dir in (kcov_out, runs_dir):
        if stale_dir.exists():
            shutil.rmtree(stale_dir)
    kcov_out.mkdir(parents=True)
    runs_dir.mkdir(parents=True)
    # Wrapper the harness invokes in place of the binary. Each invocation
    # collects into its OWN outdir; a final `kcov --merge` unions them.
    # In-place accumulation into a single outdir (the previous approach)
    # silently DROPS lines hit only by earlier runs - verified 2026-06-10:
    # a search run followed by a cost run zeroed the search defers/cleanup
    # lines, which is exactly the "kcov artifact" set the gap analysis had
    # blamed on DWARF attribution. --collect-only skips per-run report
    # generation; --merge accepts collect-only dirs.
    wrapper = COVERAGE_DIR / "walker-kcov.sh"
    wrapper.write_text(
        "#!/bin/sh\n"
        f'd=$(mktemp -d "{runs_dir}/run-XXXXXXXX")\n'
        f'exec "{kcov}" --collect-only --include-path="{ZIG_DIR / "src"}" "$d" "{real_bin}" "$@"\n'
    )
    wrapper.chmod(0o755)
    env = dict(os.environ, WALKER_BIN_ZIG=str(wrapper))
    ok, passed, failed = run_conformance("zig", env)
    run_dirs = sorted(str(p) for p in runs_dir.iterdir() if p.is_dir())
    if run_dirs:
        run([kcov, "--merge", str(kcov_out), *run_dirs])
    cov_json = _find_kcov_json(kcov_out)
    if not cov_json:
        return Result("zig", "lines", 0, 0, conformance_ok=ok,
                      conformance_passed=passed, conformance_failed=failed,
                      note="kcov produced no coverage.json")
    data = json.loads(cov_json.read_text())
    covered = int(data["covered_lines"])
    total = int(data["total_lines"])
    files = sorted(
        (Path(f["file"]).name, int(f["covered_lines"]), int(f["total_lines"]))
        for f in data["files"]
    )
    return Result("zig", "lines", covered, total, files, ok, passed, failed)


def _find_kcov_json(outdir: Path) -> Path | None:
    candidates = [p for p in outdir.rglob("coverage.json")]
    # Prefer a merged report with actual files over an empty per-run stub.
    best = None
    for path in candidates:
        try:
            data = json.loads(path.read_text())
        except Exception:
            continue
        if int(data.get("total_lines", 0)) > 0:
            if best is None or int(data["total_lines"]) >= int(json.loads(best.read_text())["total_lines"]):
                best = path
    return best


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
COLLECTORS = {"rust": cov_rust, "cpp": cov_cpp, "go": cov_go, "zig": cov_zig}


def cumulative_percent(measured: list[Result]) -> tuple[float, int, int]:
    """Pool covered/total across measured impls and return (percent, covered,
    total). Mirrors projdash/ci/post-coverage-status.py's cobertura-merge
    approach: union the per-source numbers and report one rolled-up percent.
    Mixed metrics (rust/cpp/zig 'lines', go 'statements') are pooled as-is —
    the rolled-up number is a CI-glance summary, not a strict apples-to-apples
    line count."""
    covered = sum(r.covered for r in measured)
    total = sum(r.total for r in measured)
    return (100.0 * covered / total if total else 0.0), covered, total


def write_summary_json(results: list[Result]) -> None:
    """Emit coverage/summary.json with the cumulative + per-impl numbers.
    ci/post-coverage-status.py reads this to post the Gitea commit status,
    mirroring how projdash consumes pytest-cov's coverage.json totals."""
    measured = [r for r in results if r.total > 0]
    cum_pct, cum_cov, cum_tot = (
        cumulative_percent(measured) if measured else (0.0, 0, 0)
    )
    payload = {
        "cumulative_percent": round(cum_pct, 2),
        "cumulative_covered": cum_cov,
        "cumulative_total": cum_tot,
        "measured_impls": len(measured),
        "per_impl": [
            {"lang": r.lang, "metric": r.metric, "percent": round(r.percent, 2),
             "covered": r.covered, "total": r.total}
            for r in results if r.total > 0
        ],
        "skipped": [
            {"lang": r.lang, "note": r.note} for r in results if r.total == 0
        ],
    }
    SUMMARY.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"[coverage] wrote {SUMMARY}")


def write_report(results: list[Result]) -> None:
    short, branch = git_describe()
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    measured = [r for r in results if r.total > 0]
    all_100 = bool(measured) and all(r.covered == r.total for r in measured)
    conformance_ok = all(r.conformance_ok for r in results if r.note == "")
    total_tests = sum(r.conformance_passed for r in results)

    lines: list[str] = []
    lines.append(f"agent-walker test report — {ts}")
    lines.append("=" * 60)
    lines.append("")
    lines.append(f"Status:       {'PASS' if all_100 else 'BASELINE'} "
                 f"({'100% all impls' if all_100 else 'Phase 4 baseline-gated — CI rejects regression vs ci.yml thresholds'})")
    lines.append(f"Conformance:  {'PASS' if conformance_ok else 'FAIL'} ({total_tests} checks across measured impls)")
    if measured:
        cum_pct, cum_cov, cum_tot = cumulative_percent(measured)
        lines.append(f"Cumulative:   {cum_pct:.2f}% line/statement coverage "
                     f"({cum_cov}/{cum_tot} pooled across {len(measured)} impls)")
    lines.append(f"Git:          {short} ({branch})")
    lines.append(f"Target:       100% line/statement coverage in all four implementations")
    lines.append("")
    lines.append("Per-implementation coverage")
    lines.append("-" * 60)
    lines.append(f"{'impl':<6} {'metric':<11} {'covered/total':>16} {'cover':>8}   conformance")
    for r in results:
        if r.note and r.total == 0:
            lines.append(f"{r.lang:<6} {'—':<11} {'(skipped)':>16} {'—':>8}   {r.note}")
            continue
        conf = "PASS" if r.conformance_ok else "FAIL"
        lines.append(
            f"{r.lang:<6} {r.metric:<11} {f'{r.covered}/{r.total}':>16} "
            f"{r.percent:>7.2f}%   {conf} ({r.conformance_passed} ok)"
        )
    lines.append("")
    for r in results:
        if r.total == 0:
            continue
        lines.append(f"### {r.lang} — {r.percent:.2f}% ({r.uncovered} {r.metric} uncovered)")
        for name, c, t in r.files:
            pct = 100.0 * c / t if t else 0.0
            flag = "" if c == t else f"   <-- {t - c} uncovered"
            lines.append(f"    {name:<22} {c:>5}/{t:<5} {pct:6.2f}%{flag}")
        lines.append("")
    lines.append("Regenerate: `python shared/coverage.py`  (see CLAUDE.md)")
    lines.append("")
    REPORT.write_text("\n".join(lines))
    print(f"\n[coverage] wrote {REPORT}")
    write_summary_json(results)


def _parse_baseline(spec: str) -> dict[str, float]:
    """Parse `rust=97.47,cpp=98.40,...` into a {lang: percent} threshold map."""
    out: dict[str, float] = {}
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        lang, _, pct = part.partition("=")
        lang = lang.strip()
        if lang not in ALL_LANGS:
            raise ValueError(f"unknown lang in --baseline: {lang!r}")
        out[lang] = float(pct.strip())
    return out


def main() -> int:
    # Optional `--baseline rust=97.47,cpp=98.40,go=96.73,zig=93.63` flag.
    # When supplied, gate on per-impl regression instead of strict 100%;
    # this is the Phase 4 CI gate locking in the documented baseline until
    # the remaining COVERAGE-PLAN items (4, 6) close the gap to 100%.
    args = sys.argv[1:]
    baseline: dict[str, float] = {}
    if "--baseline" in args:
        i = args.index("--baseline")
        if i + 1 >= len(args):
            sys.stderr.write("--baseline needs a value (e.g. rust=97.47,cpp=98.40)\n")
            return 2
        baseline = _parse_baseline(args[i + 1])
        args = args[:i] + args[i + 2 :]

    langs = [a for a in args if a in ALL_LANGS] or ALL_LANGS
    COVERAGE_DIR.mkdir(exist_ok=True)
    results: list[Result] = []
    for lang in langs:
        try:
            r = COLLECTORS[lang]()
        except subprocess.CalledProcessError as exc:
            # run() captures stdout/stderr by default — surface them so the
            # CI log actually shows the underlying compile/link/test failure
            # rather than just the bare exception message.
            sys.stderr.write(f"[coverage] {lang}: command failed: {exc}\n")
            stdout = (exc.stdout or "").strip() if isinstance(exc.stdout, str) else ""
            stderr = (exc.stderr or "").strip() if isinstance(exc.stderr, str) else ""
            if stdout:
                sys.stderr.write(f"[coverage] {lang}: captured stdout:\n{stdout}\n")
            if stderr:
                sys.stderr.write(f"[coverage] {lang}: captured stderr:\n{stderr}\n")
            r = Result(lang, "lines", 0, 0, note=f"collection error: {exc}")
        if r is not None:
            results.append(r)
            if r.total:
                print(f"[coverage] {lang}: {r.percent:.2f}% ({r.covered}/{r.total} {r.metric})")

    print("\n" + "=" * 60)
    for r in results:
        if r.total:
            print(f"  {r.lang:<6} {r.percent:6.2f}%  ({r.covered}/{r.total} {r.metric})")
        else:
            print(f"  {r.lang:<6}  skipped — {r.note}")
    measured = [r for r in results if r.total > 0]
    if measured:
        cum_pct, cum_cov, cum_tot = cumulative_percent(measured)
        print(f"  {'cum.':<6} {cum_pct:6.2f}%  ({cum_cov}/{cum_tot} pooled across {len(measured)} impls)")
    print("=" * 60)

    write_report(results)

    # A failing conformance run or native test suite fails the gate in EVERY
    # mode, before any percentage comparison. The percentage paths below once
    # masked a real cargo-test failure: floors were met, so --baseline
    # returned 0 with "cargo test FAILED" sitting in the log.
    failing = [r.lang for r in measured if not r.conformance_ok]
    if failing:
        print(f"\n[coverage] GATE FAILED: conformance or native tests failed "
              f"for: {', '.join(failing)}")
        return 1

    if baseline:
        # Compare on the rounded percent shown in TEST-REPORT.md so a threshold
        # of "97.47" matches the report value, not the unrounded 97.4683... .
        regressions: list[tuple[str, float, float]] = []
        missing: list[str] = []
        for lang, want in baseline.items():
            r = next((x for x in measured if x.lang == lang), None)
            if r is None:
                # An impl listed in baseline but with no result row (build
                # failed / tool missing / skipped) MUST fail the gate. The
                # earlier silent-pass bug let cpp slip through after its
                # cmake build errored out.
                missing.append(lang)
                continue
            if round(r.percent, 2) < want:
                regressions.append((lang, r.percent, want))
        if regressions or missing:
            print("\n[coverage] BASELINE GATE FAILED:")
            for lang in missing:
                note = next((r.note for r in results if r.lang == lang), "")
                print(f"  {lang}: no coverage result (gate requires {baseline[lang]:.2f}%) — {note}")
            for lang, got, want in regressions:
                print(f"  {lang}: {got:.4f}% < baseline {want:.2f}%")
            if measured:
                cum_pct, cum_cov, cum_tot = cumulative_percent(measured)
                print(f"[coverage] cumulative: {cum_pct:.2f}% line/statement coverage "
                      f"({cum_cov}/{cum_tot} pooled across {len(measured)} impls)")
            return 1
        cum_pct, cum_cov, cum_tot = cumulative_percent(measured)
        print(f"\n[coverage] all impls at or above documented baseline "
              f"(cumulative {cum_pct:.2f}% — {cum_cov}/{cum_tot} pooled across {len(measured)} impls).")
        return 0

    all_100 = bool(measured) and all(r.covered == r.total for r in measured)
    return 0 if all_100 else 1


if __name__ == "__main__":
    sys.exit(main())
