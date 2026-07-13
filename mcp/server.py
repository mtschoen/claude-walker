"""MCP shim exposing claude-walker's `search` subcommand as a FastMCP tool.

This is a thin wrapper: it discovers the installed native binary, subprocesses
`walker search ... --format jsonl`, and reshapes the JSONL output into a
structured `{hits, summary, note}` response. All search logic lives in the
native impls (see SPEC.md "Subcommands"); this layer only exists so the agent
gets auto-discovered, cross-cwd recall without having to remember a CLI.

Launched by absolute path (`python <repo>/mcp/server.py`) rather than
`python -m mcp` — the directory is named `mcp/` to match the spec, but `-m mcp`
would collide with the `mcp` SDK package on sys.path. Running by script path
puts only this directory on sys.path[0], so `import mcp.server.fastmcp`
resolves to the installed SDK, not this folder.
"""

from __future__ import annotations

import functools
import json
import os
import shutil
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

# shared/ is a sibling of this file's parent (repo layout: <repo>/mcp/server.py,
# <repo>/shared/process_safe.py); it is NOT on sys.path[0] the way `shared/`
# scripts get it for free, since this file's own directory (`mcp/`) is what
# lands there when launched by absolute path (see the module docstring above).
_SHARED_DIR = Path(__file__).resolve().parent.parent / "shared"
if str(_SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(_SHARED_DIR))

from mcp.server.fastmcp import FastMCP
from process_safe import ProcessTimeout, run_captured

# Per-process request log, mirroring projdash's pattern: one JSON line per
# event (session_start / call / return / error). Tail it to diagnose a hung
# tool — a `call` with no matching `return` for the same session pinpoints it.
LOG_PATH: Path = Path.home() / ".claude-walker-mcp.log"

# The walker should never take this long unless a network root is hung; the
# CLI is supposed to degrade gracefully, so the timeout is just the safety net.
SUBPROCESS_TIMEOUT_SECONDS = 30

_ARGUMENT_REPR_LIMIT = 200
_ERROR_MESSAGE_LIMIT = 500


def _write_log(session_id: str, payload: dict[str, Any]) -> None:
    record = {
        "ts": datetime.now(tz=timezone.utc).isoformat(timespec="milliseconds"),
        "session": session_id,
        "pid": os.getpid(),
        "ppid": os.getppid(),
        **payload,
    }
    try:
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str) + "\n")
    except OSError:
        # Logging must never break a tool call.
        pass


def _truncate_argument(value: Any) -> Any:
    text = repr(value)
    if len(text) <= _ARGUMENT_REPR_LIMIT:
        return value
    return text[:_ARGUMENT_REPR_LIMIT] + "...<truncated>"


def _instrument_tool(session_id: str, function: Callable[..., Any]) -> Callable[..., Any]:
    """Bracket each tool call with call/return/error log entries.

    `functools.wraps` keeps `inspect.signature` (which FastMCP uses to derive
    the parameter schema) following `__wrapped__` back to the original.
    """
    tool_name = function.__name__

    @functools.wraps(function)
    def wrapper(**kwargs: Any) -> Any:
        start = time.monotonic()
        _write_log(session_id, {
            "event": "call",
            "tool": tool_name,
            "args": {key: _truncate_argument(value) for key, value in kwargs.items()},
        })
        try:
            result = function(**kwargs)
        except Exception as error:
            duration_ms = int((time.monotonic() - start) * 1000)
            _write_log(session_id, {
                "event": "error",
                "tool": tool_name,
                "duration_ms": duration_ms,
                "error_type": type(error).__name__,
                "error": str(error)[:_ERROR_MESSAGE_LIMIT],
            })
            raise
        duration_ms = int((time.monotonic() - start) * 1000)
        _write_log(session_id, {"event": "return", "tool": tool_name, "duration_ms": duration_ms})
        return result

    return wrapper


def _binary_candidates() -> list[Path]:
    """Discovery chain, first existing wins (per SPEC.md MCP shim section).

    1. $CLAUDE_WALKER_BINARY (exact path).
    2. ~/.claude/walker[.exe]
    3. ~/.local/bin/claude-walker[.exe]
    4. PATH lookup for claude-walker / walker.
    """
    candidates: list[Path] = []

    override = os.environ.get("CLAUDE_WALKER_BINARY")
    if override:
        candidates.append(Path(override))

    home = Path.home()
    for directory, stem in ((home / ".claude", "walker"), (home / ".local" / "bin", "claude-walker")):
        candidates.append(directory / f"{stem}.exe")
        candidates.append(directory / stem)

    for name in ("claude-walker", "walker"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))

    return candidates


def _resolve_binary() -> Path:
    for candidate in _binary_candidates():
        if candidate.is_file():
            return candidate
    raise RuntimeError(
        "claude-walker binary not found. Install it (run install.sh / install.bat in the "
        "claude-walker repo), or set CLAUDE_WALKER_BINARY to its path. Looked in "
        "$CLAUDE_WALKER_BINARY, ~/.claude/, ~/.local/bin/, and PATH."
    )


def _run_search(arguments: list[str]) -> dict[str, Any]:
    """Subprocess `walker search ... --format jsonl` and reshape the output.

    Returns {"hits": [...], "summary": {...} | None, "note": str | None}.
    `note` carries the walker's stderr (e.g. the truncation hint) on success.
    Raises RuntimeError on a non-zero exit (bad pattern / regex / time / flag)
    or a subprocess timeout — FastMCP surfaces it as an MCP tool error.

    Shells out via process_safe.run_captured rather than bare subprocess.run:
    this is the live MCP server behind claude_walker_search, so a wedged
    child (e.g. a grandchild process inheriting the stdout pipe on Windows,
    bpo-31935) would stall the whole stdio connection, not just one call.
    run_captured's abandon-reader-thread pattern guarantees the timeout below
    is a hard wall clock even in that case.
    """
    binary = _resolve_binary()
    command = [str(binary), "search", *arguments, "--format", "jsonl"]
    try:
        completed = run_captured(
            command,
            encoding="utf-8",
            errors="replace",
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
    except ProcessTimeout as error:
        raise RuntimeError(
            f"walker search timed out after {SUBPROCESS_TIMEOUT_SECONDS}s — a configured "
            f"root may be unreachable. Command: {' '.join(command)}"
        ) from error

    stderr = (completed.stderr or "").strip()
    if completed.returncode != 0:
        # Walker uses exit 2 for bad input (empty pattern, bad regex/time, unknown flag).
        raise RuntimeError(
            f"walker search failed (exit {completed.returncode}): {stderr or 'no stderr output'}"
        )

    # stdout can be None even on a clean exit — observed on Windows when the
    # target .jsonl is mid-write by a live session (see
    # BUG-mcp-search-stdout-none.md). Guard it, and since a successful run
    # always emits at least a summary line, treat empty stdout on exit 0 as a
    # transient read failure with a real error rather than an opaque
    # AttributeError downstream.
    stdout = (completed.stdout or "").strip()
    if not stdout:
        raise RuntimeError(
            "walker search returned no output on a clean exit. This can happen "
            "transiently when a target transcript is being written by a live "
            f"session; retry shortly. stderr: {stderr or 'none'}. "
            f"Command: {' '.join(command)}"
        )

    hits: list[dict[str, Any]] = []
    summary: dict[str, Any] | None = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        record = json.loads(line)
        if record.get("type") == "summary":
            summary = record
        else:
            hits.append(record)

    # On success, stderr is the truncation hint (or empty), not a failure.
    return {"hits": hits, "summary": summary, "note": stderr or None}


def create_mcp_server() -> FastMCP:
    """Create and return the configured agent-walker MCP server."""
    server = FastMCP(
        "agent-walker",
        instructions=(
            "Search past Claude Code session transcripts across all configured roots, "
            "including cross-machine mounted roots. Use claude_walker_search when the user "
            "refers to something said in an earlier conversation."
        ),
    )

    session_id = uuid.uuid4().hex[:8]
    _write_log(session_id, {"event": "session_start"})

    original_tool_decorator = server.tool

    def instrumented_tool(*decorator_args: Any, **decorator_kwargs: Any):
        register = original_tool_decorator(*decorator_args, **decorator_kwargs)

        def decorator(function: Callable[..., Any]) -> Callable[..., Any]:
            return register(_instrument_tool(session_id, function))

        return decorator

    server.tool = instrumented_tool  # type: ignore[method-assign]

    @server.tool()
    def claude_walker_search(
        pattern: str,
        regex: bool = False,
        case_sensitive: bool = False,
        role: Literal["user", "assistant", "both"] = "both",
        since: str | None = None,
        until: str | None = None,
        cwd_slug: str | None = None,
        context_turns: int = 1,
        limit: int = 50,
        count_only: bool = False,
        include_tool_blocks: bool = False,
    ) -> dict[str, Any]:
        """Search past Claude Code session transcripts across all configured roots
        (including cross-machine mounted roots) for a substring or regex pattern.
        Returns matching messages with file paths, timestamps, and surrounding
        context. Use this when the user says something like "you said X a few
        sessions ago" or asks to find a past conversation — sessions on the other
        machine are often where the agent forgets to look; each hit's `host_root`
        tells you which machine's mount it came from.

        Args:
            pattern: The substring or regex to search for. Required, non-empty.
            regex: Treat `pattern` as an RE2 regex (no lookaround/backreferences).
            case_sensitive: Default is case-insensitive (the usual recall case).
            role: Restrict to "user", "assistant", or "both" message roles.
            since: RFC3339 timestamp or relative form ("7d", "12h"). Lower bound.
            until: Same parsing as `since`. Upper bound; defaults to now.
            cwd_slug: Restrict to one project slug (the ~/.claude/projects/<slug> dir name).
            context_turns: Turns of context before AND after each hit (0 = hit only).
            limit: Cap on returned hits; the summary's `truncated` flag reports overflow.
            count_only: Return only the summary record (no hits) — a cheap pre-flight
                to size a query before pulling full snippets and context.
            include_tool_blocks: Also search inside tool_use / tool_result blocks.
        """
        arguments: list[str] = [pattern]
        if regex:
            arguments.append("--regex")
        if case_sensitive:
            arguments.append("--case-sensitive")
        if role != "both":
            arguments += ["--role", role]
        if since is not None:
            arguments += ["--since", since]
        if until is not None:
            arguments += ["--until", until]
        if cwd_slug is not None:
            arguments += ["--cwd", cwd_slug]
        arguments += ["--context", str(context_turns)]
        arguments += ["--limit", str(limit)]
        if count_only:
            arguments.append("--count-only")
        if include_tool_blocks:
            arguments.append("--include-tool-blocks")
        return _run_search(arguments)

    return server


if __name__ == "__main__":
    create_mcp_server().run(transport="stdio")
