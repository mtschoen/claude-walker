#!/usr/bin/env bash
# Build the C++ walker (production impl) and install it as `agent-walker`
# at ~/.local/bin (with a `claude-walker` copy kept as a back-compat alias
# for existing callers), then register the search MCP server. Smoke test
# before reporting success.
#
# Usage: install.sh [--project [DIR]]
#   (no flag)        register the MCP server at `user` scope (global, every project)
#   --project        register at `local` scope for the directory you invoke from
#   --project DIR    register at `local` scope for DIR
set -euo pipefail

# Capture the invocation directory BEFORE cd-ing into the repo so --project can
# default to "the project the user ran the installer from".
INVOCATION_DIR="$PWD"

MCP_SCOPE="user"
PROJECT_DIR=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --project)
            MCP_SCOPE="local"
            # Optional path argument (anything not starting with --).
            if [[ -n "${2:-}" && "${2:-}" != --* ]]; then
                PROJECT_DIR="$2"
                shift
            fi
            ;;
        *)
            echo "install.sh: unknown argument: $1" >&2
            echo "Usage: install.sh [--project [DIR]]" >&2
            exit 2
            ;;
    esac
    shift
done
if [[ "$MCP_SCOPE" == "local" && -z "$PROJECT_DIR" ]]; then
    PROJECT_DIR="$INVOCATION_DIR"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Build whichever impl this host has a toolchain for. C++ is the production
# binary and is tried first; Go and Rust are conformance-equal fallbacks for
# hosts without a C++ toolchain (e.g. SteamOS's immutable root, where there is
# no cmake/cc at all but a user-local `go` works). Each builder writes its tool
# output to stderr and prints ONLY the resulting binary path to stdout, so the
# caller can capture the path via command substitution; a non-zero return means
# "toolchain absent or build failed, try the next one".
build_cpp() {
    command -v cmake >/dev/null 2>&1 || return 1
    cmake -S cpp -B cpp/build -DCMAKE_BUILD_TYPE=Release >&2 || return 1
    cmake --build cpp/build --config Release -j >&2 || return 1
    # Check .exe variants first because git-bash on Windows resolves
    # `[[ -f "walker" ]]` true for an existing `walker.exe`, which would land us
    # on the wrong candidate string and skip the case-based suffix below.
    local candidate
    for candidate in cpp/build/Release/walker.exe cpp/build/walker.exe cpp/build/Release/walker cpp/build/walker; do
        if [[ -f "$candidate" ]]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

build_go() {
    command -v go >/dev/null 2>&1 || return 1
    # CGO off so the pure-Go linker is used -- no system `cc` required.
    ( cd go && CGO_ENABLED=0 go build -o walker . ) >&2 || return 1
    [[ -f go/walker ]] && { printf '%s\n' "go/walker"; return 0; }
    return 1
}

build_rust() {
    command -v cargo >/dev/null 2>&1 || return 1
    # rustc shells out to a system linker; skip if there is no C compiler.
    command -v cc >/dev/null 2>&1 || command -v gcc >/dev/null 2>&1 \
        || command -v clang >/dev/null 2>&1 || return 1
    ( cd rust && cargo build --release ) >&2 || return 1
    [[ -f rust/target/release/walker ]] && {
        printf '%s\n' "rust/target/release/walker"
        return 0
    }
    return 1
}

walker_bin=""
for builder in build_cpp build_go build_rust; do
    if walker_bin="$("$builder")"; then
        echo "built walker via ${builder#build_}: $walker_bin" >&2
        break
    fi
done
if [[ -z "$walker_bin" ]]; then
    echo "install.sh: no usable toolchain to build the walker." >&2
    echo "            install one of: cmake + a C++ compiler, or go, or cargo + cc." >&2
    exit 1
fi

mkdir -p "$HOME/.local/bin"
target="$HOME/.local/bin/agent-walker"
alias_target="$HOME/.local/bin/claude-walker"
case "$walker_bin" in
    *.exe) target="$target.exe"; alias_target="$alias_target.exe" ;;
esac
cp "$walker_bin" "$target"
# claude-walker is a deprecated back-compat alias for existing callers
# (progress-beacon hooks, statusline lookups). Remove once every consumer
# has migrated to the agent-walker name.
cp "$walker_bin" "$alias_target"
echo "installed $walker_bin -> $target (alias: $alias_target)"

# Smoke test: bare-flag invocation routes to cost mode.
if "$target" --period 86400 --win-start 0 >/dev/null; then
    echo "smoke test ok"
else
    echo "smoke test FAILED" >&2
    exit 1
fi

if ! command -v agent-walker >/dev/null 2>&1; then
    echo
    echo "Note: $HOME/.local/bin is not on PATH. Add it before the recency-nudge"
    echo "hook or status line can find agent-walker (or its claude-walker alias) by name."
fi

# --- Register the search MCP server -----------------------------------------
# Additive: a registration failure warns but does not fail the binary install.
# The server runs out of a dedicated venv at mcp/.venv to host the `mcp` SDK,
# so the registration doesn't depend on whatever `python` happens to be on PATH
# (PEP 668 blocks system-wide pip on modern macOS/Linux distros anyway).
server_path="$SCRIPT_DIR/mcp/server.py"
venv_dir="$SCRIPT_DIR/mcp/.venv"
# Venv layout differs between POSIX (bin/) and Windows-under-git-bash (Scripts/).
case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*) venv_py="$venv_dir/Scripts/python.exe" ;;
    *)                    venv_py="$venv_dir/bin/python" ;;
esac

# Find a Python >=3.10 (the `mcp` SDK's floor). Tries newest first.
pick_python() {
    local candidate ver_major ver_minor
    for candidate in python3.13 python3.12 python3.11 python3.10 python3 python; do
        command -v "$candidate" >/dev/null 2>&1 || continue
        # Single-shot version probe: prints "MAJOR MINOR" or nothing on failure.
        read -r ver_major ver_minor < <("$candidate" -c \
            'import sys; print(sys.version_info[0], sys.version_info[1])' 2>/dev/null) || continue
        if [[ "$ver_major" -gt 3 || ( "$ver_major" -eq 3 && "$ver_minor" -ge 10 ) ]]; then
            echo "$candidate"
            return 0
        fi
    done
    return 1
}

ensure_venv() {
    # A venv is reusable only if its interpreter actually RUNS. Testing for the
    # file alone is not enough: a venv whose base Python was uninstalled or
    # relocated still has bin/python, but it resolves `home` out of pyvenv.cfg
    # and dies at startup. Re-running `-m venv` over the existing directory
    # rewrites pyvenv.cfg and the launcher while leaving site-packages intact,
    # so falling through to the create step repairs in place.
    if [[ -x "$venv_py" ]] && "$venv_py" -c 'pass' >/dev/null 2>&1; then
        return 0
    fi
    if [[ -e "$venv_py" ]]; then
        echo "venv at $venv_dir has a dead base interpreter; repairing in place"
    fi
    local py
    if ! py=$(pick_python); then
        echo "warning: no Python >=3.10 found on PATH; can't create $venv_dir" >&2
        echo "         install Python 3.10+ (e.g. 'brew install python@3.13') and re-run." >&2
        return 1
    fi
    echo "creating MCP server venv at $venv_dir (using $py)"
    "$py" -m venv "$venv_dir" || return 1
}

if ! command -v claude >/dev/null 2>&1; then
    echo
    echo "Note: 'claude' CLI not on PATH; skipped MCP server registration."
    echo "Register later with:"
    echo "  claude mcp add agent-walker -s user -- \"$venv_py\" \"$server_path\""
else
    venv_ready=0
    if ensure_venv; then
        # Idempotent: pip install is fast when the wheel is already cached.
        # Pinned to the 2.x line on purpose. server.py targets the v2 API
        # (`mcp.server.MCPServer`); an unpinned --upgrade silently crossed the
        # 1.x -> 2.x boundary, which removed `mcp.server.fastmcp` and left the
        # server failing at import with no signal until the next connect
        # attempt. Bump deliberately when porting to 3.x.
        if "$venv_py" -m pip install --quiet --upgrade 'mcp~=2.0'; then
            venv_ready=1
        else
            echo "warning: failed to install 'mcp' SDK into $venv_dir" >&2
        fi
    fi

    if (( venv_ready == 0 )); then
        echo
        echo "Note: MCP server venv isn't ready. Registration will still land, but the"
        echo "server won't start until the venv exists and has the 'mcp' SDK installed."
    fi

    if [[ "$MCP_SCOPE" == "local" ]]; then
        if ( cd "$PROJECT_DIR" \
                && { claude mcp remove claude-walker -s local >/dev/null 2>&1 || true; } \
                && { claude mcp remove agent-walker -s local >/dev/null 2>&1 || true; } \
                && claude mcp add agent-walker -s local -- "$venv_py" "$server_path" ); then
            echo "registered agent-walker MCP server (local scope) for $PROJECT_DIR"
        else
            echo "warning: MCP registration (local scope) failed for $PROJECT_DIR" >&2
        fi
    else
        { claude mcp remove claude-walker -s user >/dev/null 2>&1 || true; }
        { claude mcp remove agent-walker -s user >/dev/null 2>&1 || true; }
        if claude mcp add agent-walker -s user -- "$venv_py" "$server_path"; then
            echo "registered agent-walker MCP server (user/global scope)"
        else
            echo "warning: MCP registration (user scope) failed" >&2
        fi
    fi
fi
