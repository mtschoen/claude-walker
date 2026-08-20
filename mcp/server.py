"""MCP server for provider-aware historical agent-session search.

Claude Code search remains delegated to the native agent-walker binary
(installed as `claude-walker` as a back-compat alias). Provider modules
handle stores with different formats, beginning with OpenCode's SQLite
database. Cost, event, and beacon modes remain Claude-only.

Launched by absolute path (`python <repo>/mcp/server.py`) rather than
`python -m mcp` — the directory is named `mcp/` to match the spec, but `-m mcp`
would collide with the `mcp` SDK package on sys.path. Running by script path
puts only this directory on sys.path[0], so `import mcp.server` resolves to the
installed SDK, not this folder.
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

from mcp.server import MCPServer
from opencode_search import (
    OpenCodeSearchError,
    SearchOptions,
    search as search_opencode,
)
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


def _instrument_tool(
    session_id: str, function: Callable[..., Any]
) -> Callable[..., Any]:
    """Bracket each tool call with call/return/error log entries.

    `functools.wraps` keeps `inspect.signature` (which MCPServer uses to derive
    the parameter schema) following `__wrapped__` back to the original.
    """
    tool_name = function.__name__

    @functools.wraps(function)
    def wrapper(**kwargs: Any) -> Any:
        start = time.monotonic()
        _write_log(
            session_id,
            {
                "event": "call",
                "tool": tool_name,
                "args": {
                    key: _truncate_argument(value) for key, value in kwargs.items()
                },
            },
        )
        try:
            result = function(**kwargs)
        except Exception as error:
            duration_ms = int((time.monotonic() - start) * 1000)
            _write_log(
                session_id,
                {
                    "event": "error",
                    "tool": tool_name,
                    "duration_ms": duration_ms,
                    "error_type": type(error).__name__,
                    "error": str(error)[:_ERROR_MESSAGE_LIMIT],
                },
            )
            raise
        duration_ms = int((time.monotonic() - start) * 1000)
        _write_log(
            session_id,
            {"event": "return", "tool": tool_name, "duration_ms": duration_ms},
        )
        return result

    return wrapper


def _binary_candidates() -> list[Path]:
    """Discovery chain, first existing wins (per SPEC.md MCP shim section).

    1. $AGENT_WALKER_BINARY (exact path).
    2. $CLAUDE_WALKER_BINARY (exact path; back-compat alias for #1).
    3. ~/.claude/walker[.exe]
    4. ~/.local/bin/agent-walker[.exe]
    5. ~/.local/bin/claude-walker[.exe] (back-compat alias)
    6. PATH lookup for agent-walker / claude-walker / walker.
    """
    candidates: list[Path] = []

    override = os.environ.get("AGENT_WALKER_BINARY") or os.environ.get(
        "CLAUDE_WALKER_BINARY"
    )
    if override:
        candidates.append(Path(override))

    home = Path.home()
    for directory, stem in (
        (home / ".claude", "walker"),
        (home / ".local" / "bin", "agent-walker"),
        (home / ".local" / "bin", "claude-walker"),
    ):
        candidates.append(directory / f"{stem}.exe")
        candidates.append(directory / stem)

    for name in ("agent-walker", "claude-walker", "walker"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))

    return candidates


def _resolve_binary() -> Path:
    for candidate in _binary_candidates():
        if candidate.is_file():
            return candidate
    raise RuntimeError(
        "agent-walker binary not found. Install it (run install.sh / install.bat in the "
        "agent-walker repo), or set AGENT_WALKER_BINARY (or the legacy CLAUDE_WALKER_BINARY) "
        "to its path. Looked in $AGENT_WALKER_BINARY, $CLAUDE_WALKER_BINARY, ~/.claude/, "
        "~/.local/bin/, and PATH."
    )


def _run_search(arguments: list[str]) -> dict[str, Any]:
    """Subprocess `walker search ... --format jsonl` and reshape the output.

    Returns {"hits": [...], "summary": {...} | None, "note": str | None}.
    `note` carries the walker's stderr (e.g. the truncation hint) on success.
    Raises RuntimeError on a non-zero exit (bad pattern / regex / time / flag)
    or a subprocess timeout - MCPServer surfaces it as an MCP tool error.

    Shells out via process_safe.run_captured rather than bare subprocess.run:
    this is the live MCP server behind agent_walker_search, so a wedged
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


def _search_arguments(options: SearchOptions) -> list[str]:
    arguments: list[str] = [options.pattern]
    if options.regex:
        arguments.append("--regex")
    if options.case_sensitive:
        arguments.append("--case-sensitive")
    if options.role != "both":
        arguments += ["--role", options.role]
    if options.since is not None:
        arguments += ["--since", options.since]
    if options.until is not None:
        arguments += ["--until", options.until]
    if options.cwd is not None:
        arguments += ["--cwd", options.cwd]
    arguments += [
        "--context",
        str(options.context_turns),
        "--limit",
        str(options.limit),
    ]
    if options.count_only:
        arguments.append("--count-only")
    if options.include_tool_blocks:
        arguments.append("--include-tool-blocks")
    return arguments


def _hit_sort_key(hit: dict[str, Any]) -> tuple[float, str, str, int]:
    timestamp = str(hit.get("timestamp", ""))
    try:
        timestamp_value = datetime.fromisoformat(
            timestamp.replace("Z", "+00:00")
        ).timestamp()
    except ValueError:
        timestamp_value = 0.0
    return (
        -timestamp_value,
        str(hit.get("provider", "")),
        str(hit.get("session_id", "")),
        int(hit.get("line_number", 0)),
    )


def _merge_provider_results(
    results: dict[str, dict[str, Any]],
    limit: int,
    count_only: bool,
    elapsed_ms: int,
) -> dict[str, Any]:
    hits: list[dict[str, Any]] = []
    notes: list[str] = []
    summaries: dict[str, dict[str, Any]] = {}
    for provider, result in results.items():
        summary = dict(result.get("summary") or {})
        summary.setdefault("provider", provider)
        summary.setdefault("available", True)
        summaries[provider] = summary
        for hit in result.get("hits", []):
            normalized = dict(hit)
            normalized.setdefault("provider", provider)
            hits.append(normalized)
        if result.get("note"):
            notes.append(f"{provider}: {result['note']}")
    hits.sort(key=_hit_sort_key)
    provider_truncated = any(
        summary.get("truncated", False) for summary in summaries.values()
    )
    globally_truncated = len(hits) > limit
    if count_only:
        merged_hits: list[dict[str, Any]] = []
        hit_count = sum(int(summary.get("hits", 0)) for summary in summaries.values())
    else:
        merged_hits = hits[:limit]
        hit_count = len(merged_hits)
    summary = {
        "type": "summary",
        "hits": hit_count,
        "sessions_matched": sum(
            int(provider_summary.get("sessions_matched", 0))
            for provider_summary in summaries.values()
        ),
        "roots_walked": sum(
            int(provider_summary.get("roots_walked", 0))
            for provider_summary in summaries.values()
        ),
        "files_walked": sum(
            int(provider_summary.get("files_walked", 0))
            for provider_summary in summaries.values()
        ),
        "truncated": provider_truncated or globally_truncated,
        "elapsed_ms": elapsed_ms,
        "providers": summaries,
    }
    return {"hits": merged_hits, "summary": summary, "note": "; ".join(notes) or None}


def _run_agent_search(
    options: SearchOptions,
    providers: list[str] | None,
) -> dict[str, Any]:
    selected = ["claude", "opencode"] if providers is None else providers
    unknown = sorted(set(selected) - {"claude", "opencode"})
    if unknown:
        raise ValueError(f"unknown search providers: {', '.join(unknown)}")
    if not selected:
        raise ValueError("providers must not be empty")
    if options.limit < 1:
        raise ValueError("limit must be at least 1")
    if options.context_turns < 0:
        raise ValueError("context_turns must not be negative")
    started = time.monotonic()
    results: dict[str, dict[str, Any]] = {}
    failures: dict[str, str] = {}
    arguments = _search_arguments(options)
    if "claude" in selected:
        try:
            results["claude"] = _run_search(arguments)
        except RuntimeError as error:
            failures["claude"] = str(error)
    if "opencode" in selected:
        try:
            results["opencode"] = search_opencode(options)
        except OpenCodeSearchError as error:
            failures["opencode"] = str(error)
    if not results:
        details = "; ".join(
            f"{provider}: {error}" for provider, error in failures.items()
        )
        raise RuntimeError("all selected search providers failed: " + details)
    for provider, error in failures.items():
        results[provider] = {
            "hits": [],
            "summary": {
                "type": "summary",
                "provider": provider,
                "available": False,
                "hits": 0,
                "sessions_matched": 0,
                "roots_walked": 0,
                "files_walked": 0,
                "truncated": False,
                "error": error,
            },
            "note": error,
        }
    merged = _merge_provider_results(
        results,
        options.limit,
        options.count_only,
        int((time.monotonic() - started) * 1000),
    )
    return merged


def create_mcp_server() -> MCPServer:
    """Create and return the configured agent-walker MCP server."""
    server = MCPServer(
        "agent-walker",
        instructions=(
            "Search past coding-agent sessions. Use agent_walker_search when the user "
            "refers to an earlier conversation; it searches Claude Code and OpenCode by "
            "default. Set providers to scope recall to one or more agent harnesses."
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
    def agent_walker_search(
        pattern: str,
        providers: list[Literal["claude", "opencode"]] | None = None,
        regex: bool = False,
        case_sensitive: bool = False,
        role: Literal["user", "assistant", "both"] = "both",
        since: str | None = None,
        until: str | None = None,
        cwd: str | None = None,
        context_turns: int = 1,
        limit: int = 50,
        count_only: bool = False,
        include_tool_blocks: bool = False,
        opencode_db: str | None = None,
    ) -> dict[str, Any]:
        """Search historical sessions across coding-agent providers.

        Claude Code JSONLs and OpenCode's SQLite store are searched by default.
        Results carry a `provider` field and retain source-specific provenance.
        This tool is historical recall only: statusline costs, events, and progress
        beacons remain isolated to their native agent session source.

        Args:
            pattern: Required substring or regex.
            providers: Providers to search; omitted means every supported provider.
            regex: Treat pattern as an RE2 regular expression. OpenCode is
                reported unavailable for regex queries until it has a
                linear-time matcher; Claude search continues normally.
            case_sensitive: Default is case-insensitive.
            role: Restrict to user, assistant, or both.
            since: RFC3339 timestamp or relative value such as 7d or 12h.
            until: RFC3339 timestamp or relative upper bound.
            cwd: Project identifier/path filter. Claude expects its project slug;
                OpenCode matches against the stored session directory.
            context_turns: Messages before and after each hit.
            limit: Global cap after provider results are merged newest-first.
            count_only: Return aggregate and per-provider counts without hits.
            include_tool_blocks: Search tool inputs and outputs as well as prose.
            opencode_db: Optional OpenCode database override. The environment
                variable AGENT_WALKER_OPENCODE_DB provides a persistent override.
        """
        return _run_agent_search(
            SearchOptions(
                pattern=pattern,
                regex=regex,
                case_sensitive=case_sensitive,
                role=role,
                since=since,
                until=until,
                cwd=cwd,
                context_turns=context_turns,
                limit=limit,
                count_only=count_only,
                include_tool_blocks=include_tool_blocks,
                db_path=opencode_db,
            ),
            providers,
        )

    return server


if __name__ == "__main__":
    create_mcp_server().run(transport="stdio")
