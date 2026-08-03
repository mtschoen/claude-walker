# LINTER-SETUP.md — claude-walker

Recommended linting setup for claude-walker — fleet survey 2026-05-29.

---

## Current state

**Languages confirmed in-tree:**

| Directory | Language | Source files |
|---|---|---|
| `go/` | Go | `*.go` (main, walker_roots, events, search, beacons + tests) |
| `rust/src/` | Rust | `*.rs` (main, beacons, content, events, search, transcript, walker_roots) |
| `cpp/` | C++ | `*.cpp` / `*.hpp` (main, beacons, events, search, walker_roots, common, json_writer, pricing) |
| `zig/src/` | Zig | `*.zig` (main, beacons, events, search, walker_roots) |
| `shared/`, `mcp/`, `ci/` | Python | conformance, coverage, bench, corpus generators, MCP server, CI post-status |

**Existing linter/formatter config:** `ruff.toml` and `.aislop/config.yml`.
There is no `.golangci.yml`, `.clang-format`, `.clang-tidy`, `Cargo.toml
[lints]`, `pyproject.toml`, or `.pre-commit-config.yaml`.

**Existing CI** (`.gitea/workflows/ci.yml`): runs the aislop quality gate,
then builds changed native implementations on Linux + Windows and runs matching
conformance and coverage gates. Language-specific lint commands remain local
checks.

**Existing Claude Code on-save hook** (`.claude/settings.local.json`): no `PostToolUse` hook present.

---

## Three-tier model

Linting is organized into three tiers so feedback arrives at the right speed:

1. **On-save ①** — fast, per-file, Claude Code `PostToolUse` hook. Instant formatter catches issues as code is written.
2. **Validate ②** — full-repo, all rules, authoritative. Run on demand and by `/maintaining-full-coverage`. "0 findings is the bar."
3. **CI ③** — automates tier 2 so regressions block at merge.

Where tier ① and tier ② use the same tool they are noted below.

---

## Three-tier recommendation

### Go

| Tier | Tool | Command | Why |
|---|---|---|---|
| ① On-save | `gofmt` | `gofmt -w <file>` | Ships with Go; zero-config; industry standard |
| ② Validate | `golangci-lint` | `golangci-lint run ./...` (from `go/`) | De-facto meta-linter; aggregates 50+ linters; used by Kubernetes/Prometheus/Terraform |
| ③ CI | `golangci-lint` | `golangci-lint run ./...` | Same tool as ② |

Config file to add: `go/.golangci.yml` (start minimal; expand rule set as you go).

**Suggested starter `.golangci.yml`:**
```yaml
run:
  timeout: 5m
linters:
  enable:
    - gofmt
    - govet
    - errcheck
    - staticcheck
    - unused
    - gosimple
    - ineffassign
```

### Rust

| Tier | Tool | Command | Why |
|---|---|---|---|
| ① On-save | `cargo fmt` | `cargo fmt -- <file>` | Ships with rustup; zero-config; `rustfmt.toml` optional |
| ② Validate | `cargo clippy` | `cargo clippy --all-targets -- -D warnings` (from `rust/`) | 550+ lints; -D warnings makes it a hard gate |
| ③ CI | both | `cargo fmt --check && cargo clippy --all-targets -- -D warnings` | clippy is project-level so validate + CI; fmt is per-file for on-save |

No extra config file needed to start; add `[lints]` in `Cargo.toml` if you want to pin specific lints.

### C++

| Tier | Tool | Command | Why |
|---|---|---|---|
| ① On-save | `clang-format` | `clang-format -i <file>` | Instant; needs `.clang-format` at `cpp/` (or repo root) |
| ② Validate | `clang-tidy` + `cppcheck` | see below | Complementary: tidy = style/modernize/bugprone; cppcheck = zero-false-positive real bugs |
| ③ CI | all three | see below | |

**Validate commands (from repo root, after `cmake -DCMAKE_EXPORT_COMPILE_COMMANDS=ON`):**
```bash
clang-tidy cpp/*.cpp --checks='-*,bugprone-*,modernize-*,performance-*,readability-*' -p cpp/build
cppcheck --enable=all --suppress=missingIncludeSystem --error-exitcode=1 cpp/*.cpp
```

**Starter `.clang-format` (place at `cpp/.clang-format` or repo root):**
```yaml
BasedOnStyle: Google
IndentWidth: 4
ColumnLimit: 100
```

Note: `clang-tidy` needs `compile_commands.json` — the CI already builds with CMake; add `-DCMAKE_EXPORT_COMPILE_COMMANDS=ON` to the cmake step to generate it. `clang-format` works on-save without it.

### Zig

| Tier | Tool | Command | Why |
|---|---|---|---|
| ① On-save | `zig fmt` | `zig fmt <file>` | Ships with Zig; zero-config |
| ② Validate | `zig fmt --check` + `zig build` | `zig fmt --check src/ && zig build` (from `zig/`) | No mature 3rd-party linter as of 2026; build warnings are the linter |
| ③ CI | same | `zig fmt --check src/ && zig build` | Same as ② |

### Python

The Python surface here is tooling (conformance harness, coverage scripts, MCP server, corpus generators, CI status poster) — not a library or application. That makes linting high-value: bugs in these scripts silently break CI or measurements.

| Tier | Tool | Command | Why |
|---|---|---|---|
| ① On-save | `ruff format` + `ruff check --fix` | see hook below | Same binary as ②; 10–100× faster than flake8+black+isort combined |
| ② Validate | `ruff check` | `ruff check .` (from repo root) | Full-repo, all rules, authoritative; 0 findings is the bar |
| ③ CI | `ruff check` + `ruff format --check` + `pyright` | see CI snippet below | pyright: type-check; 2–5× faster than mypy |

**Suggested `pyproject.toml` (place at repo root):**
```toml
[tool.ruff]
target-version = "py311"

[tool.ruff.lint]
select = ["F", "I", "B", "UP", "SIM", "RET", "PIE", "C4", "W", "RUF"]
# E501 (line length) intentionally omitted — formatter handles wrapping
ignore = []

[tool.pyright]
pythonVersion = "3.11"
typeCheckingMode = "basic"
```

---

## On-save hook (Claude Code PostToolUse)

Add to `.claude/settings.local.json` under `"hooks"`. This covers the four fast on-save formatters; Go and Python feed findings back to Claude.

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Write|Edit",
        "hooks": [
          {
            "type": "command",
            "command": "f=$(jq -r '.tool_input.file_path // .tool_response.filePath // empty'); case \"$f\" in *.py) o=$(ruff check \"$f\" 2>/dev/null); [ -n \"$o\" ] && jq -n --arg c \"ruff:\\n$o\" '{hookSpecificOutput:{hookEventName:\"PostToolUse\",additionalContext:$c}}';; *.go) cd \"$(dirname \"$f\")\" && gofmt -w \"$(basename \"$f\")\";; *.rs) cargo fmt -- \"$f\" 2>/dev/null || true;; *.zig) zig fmt \"$f\" 2>/dev/null || true;; *.cpp|*.hpp) clang-format -i \"$f\" 2>/dev/null || true;; esac; exit 0"
          }
        ]
      }
    ]
  }
}
```

The Python branch returns ruff findings as `additionalContext` so Claude sees them inline. The other branches silently format in place (no output needed for pure formatters).

---

## CI step

Add a `lint` job to `.gitea/workflows/ci.yml`. It can run in parallel with the existing `test-linux` job since it has no binary-build dependency.

```yaml
  lint:
    name: Lint
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-go@v5
        with:
          go-version-file: go/go.mod

      - name: Install golangci-lint
        run: curl -sSfL https://raw.githubusercontent.com/golangci/golangci-lint/master/install.sh | sh -s -- -b $(go env GOPATH)/bin v1.64.0

      - uses: dtolnay/rust-toolchain@stable
        with:
          components: clippy,rustfmt

      - name: Install Zig 0.16.0
        run: |
          set -e
          ZIG_VERSION=0.16.0
          curl -fsSL "https://ziglang.org/download/${ZIG_VERSION}/zig-x86_64-linux-${ZIG_VERSION}.tar.xz" -o /tmp/zig.tar.xz
          mkdir -p "$HOME/.local/zig"
          tar -xf /tmp/zig.tar.xz -C "$HOME/.local/zig" --strip-components=1
          echo "$HOME/.local/zig" >> "$GITHUB_PATH"

      - name: Install clang-format + clang-tidy + cppcheck
        run: |
          if command -v sudo >/dev/null; then SUDO=sudo; else SUDO=; fi
          $SUDO apt-get update
          $SUDO apt-get install -y --no-install-recommends clang-format clang-tidy cppcheck

      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install ruff + pyright
        run: pip install ruff pyright

      # Go
      - name: Go — gofmt check
        run: test -z "$(gofmt -l ./go/...)" || (gofmt -l ./go && exit 1)

      - name: Go — golangci-lint
        working-directory: go
        run: golangci-lint run ./...

      # Rust
      - name: Rust — fmt check
        working-directory: rust
        run: cargo fmt --check

      - name: Rust — clippy
        working-directory: rust
        run: cargo clippy --all-targets -- -D warnings

      # C++
      - name: C++ — clang-format check
        run: clang-format --dry-run --Werror cpp/*.cpp cpp/*.hpp

      - name: C++ — cppcheck
        run: cppcheck --enable=all --suppress=missingIncludeSystem --error-exitcode=1 cpp/*.cpp

      # Zig
      - name: Zig — fmt check
        working-directory: zig
        run: zig fmt --check src/

      # Python
      - name: Python — ruff check
        run: ruff check .

      - name: Python — ruff format check
        run: ruff format --check .

      - name: Python — pyright
        run: pyright shared/ mcp/ ci/
```

---

## AI-slop gate (aislop)

**aislop** (https://github.com/scanaislop/aislop, MIT, Node >= 20) is a
language-agnostic, deterministic AI-slop quality gate with no LLM and 40+ rules,
scored 0–100. It flags agent slop: narrative/trivial comments, swallowed/broad
exceptions, `as any`, dead code, unused/hallucinated imports, innerHTML/XSS
sinks, and similar patterns.

The project uses the `mtschoen/aislop` fork so the scan also runs cppcheck over
the C++ surface. Zig still relies on `zig fmt` and `zig build` for its
language-specific checks.

### Per-edit (① on-save)

```bash
aislop hook install --claude --project
```

Pin the binary version in the hook — avoid `@latest`. The hook does a network
version check on every edit; `@latest` re-resolves on every save, which is
both slow and non-deterministic.

### PR / CI gate (③)

```bash
aislop ci .
```

The `quality` job in `.gitea/workflows/ci.yml` installs the project-standard
`mtschoen/aislop#schoen/main` fork and runs this command before builds and
tests. The gate scans the entire repository so changes cannot hide behind
language selection.

### Config (`.aislop/config.yml`)

```yaml
ci:
  # Ratchet the warning-heavy legacy baseline while every error remains fatal.
  failBelow: 20
exclude:
  - shared/corpus/**
  - shared/corpus-perf/**
  - shared/corpus-perf-beacons/**
  # Build, coverage, cache, and local environment outputs are also excluded.
```

The initial scan scores 20/100 with 164 warning-severity style and complexity
findings. Those are retained so the score can ratchet upward. Error-severity
findings are always fatal, independent of the score threshold.

Run `aislop scan .` locally to inspect findings. Before committing, use
`aislop scan --staged` to check only the proposed change.

---

## Rollout

The recommended approach (as done in projdash PRs #113/#115/#116):

1. **Autofix sweep** — run all formatters + `--fix` modes in one commit: `gofmt -w ./go`, `cargo fmt` in `rust/`, `clang-format -i` on `cpp/*.{cpp,hpp}`, `zig fmt src/` in `zig/`, `ruff format . && ruff check --fix .`. One commit, no logic changes.
2. **Hand-fix real findings** — address the real lint findings (clippy warnings, golangci-lint, cppcheck, ruff non-auto-fixable). One or more commits.
3. **Bake into CI + add on-save hook** — add the `lint` job above, add the hook snippet to `.claude/settings.local.json`. The gate is now automatic.

Whether to do this as a single PR, stacked PRs, or manually is your call — the three steps are separable and each is independently safe to land.

> **Note on clang-tidy:** skipped from the starter CI above because it requires `compile_commands.json` (add `-DCMAKE_EXPORT_COMPILE_COMMANDS=ON` to the cmake build step) and can be noisy to bootstrap. Recommend adding it in a follow-up PR once the formatter/cppcheck baseline is clean.
