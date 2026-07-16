"""Read-only historical search over OpenCode's SQLite session store."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal


class OpenCodeSearchError(RuntimeError):
    """The OpenCode store or query is incompatible with historical search."""


@dataclass(frozen=True)
class SearchOptions:
    pattern: str
    regex: bool = False
    case_sensitive: bool = False
    role: Literal["user", "assistant", "both"] = "both"
    since: str | None = None
    until: str | None = None
    cwd: str | None = None
    context_turns: int = 1
    limit: int = 50
    count_only: bool = False
    include_tool_blocks: bool = False
    db_path: str | None = None


@dataclass
class _Message:
    rowid: int
    part_id: str
    message_id: str
    session_id: str
    timestamp_ms: int
    role: str
    directory: str
    session_slug: str
    session_title: str
    text_parts: list[str] = field(default_factory=list)
    tool_parts: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(self.text_parts)

    @property
    def searchable_text(self) -> str:
        return "\n".join((*self.text_parts, *self.tool_parts))


_REQUIRED_COLUMNS = {
    "session": {"id", "slug", "directory", "title"},
    "message": {"id", "session_id", "time_created", "data"},
    "part": {"id", "message_id", "session_id", "time_created", "data"},
}


def _database_candidates(override: str | None) -> list[Path]:
    if override:
        return [Path(override).expanduser()]
    environment_override = os.environ.get("AGENT_WALKER_OPENCODE_DB")
    if environment_override:
        return [Path(environment_override).expanduser()]
    candidates: list[Path] = []
    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    if xdg_data_home:
        candidates.append(Path(xdg_data_home).expanduser() / "opencode" / "opencode.db")
    candidates.append(Path.home() / ".local" / "share" / "opencode" / "opencode.db")
    return candidates


def resolve_database(override: str | None = None) -> Path | None:
    """Return the first configured OpenCode database that exists."""
    return next(
        (path for path in _database_candidates(override) if path.is_file()), None
    )


def _open_database(path: Path) -> sqlite3.Connection:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=2)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _validate_schema(connection: sqlite3.Connection) -> None:
    tables = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    missing_tables = _REQUIRED_COLUMNS.keys() - tables
    if missing_tables:
        raise OpenCodeSearchError(
            f"OpenCode database is missing tables: {', '.join(sorted(missing_tables))}"
        )
    for table, required in _REQUIRED_COLUMNS.items():
        columns = {
            row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
        }
        missing_columns = required - columns
        if missing_columns:
            raise OpenCodeSearchError(
                f"OpenCode table {table} is missing columns: "
                f"{', '.join(sorted(missing_columns))}"
            )


def _tool_text(part: dict[str, Any]) -> str:
    state = part.get("state")
    if not isinstance(state, dict):
        return ""
    values: list[str] = []
    if "input" in state:
        values.append(
            json.dumps(state["input"], ensure_ascii=False, separators=(",", ":"))
        )
    output = state.get("output")
    if isinstance(output, str) and output:
        values.append(output)
    return "\n".join(values)


def _load_messages(
    connection: sqlite3.Connection, include_tools: bool
) -> list[_Message]:
    rows = connection.execute(
        """
        SELECT p.rowid AS part_rowid, p.id AS part_id, p.message_id, p.session_id,
               m.time_created, m.data AS message_data, p.data AS part_data,
               s.directory, s.slug, s.title
          FROM part AS p
          JOIN message AS m ON m.id = p.message_id
          JOIN session AS s ON s.id = p.session_id
         WHERE json_extract(p.data, '$.type') = 'text'
            OR (? AND json_extract(p.data, '$.type') = 'tool')
         ORDER BY p.session_id, m.time_created, p.time_created, p.rowid
        """,
        (include_tools,),
    )
    messages: dict[str, _Message] = {}
    for row in rows:
        try:
            message_data = json.loads(row["message_data"])
            part_data = json.loads(row["part_data"])
        except (TypeError, json.JSONDecodeError):
            continue
        role = message_data.get("role")
        if role not in {"user", "assistant"}:
            continue
        message = messages.get(row["message_id"])
        if message is None:
            message = _Message(
                rowid=row["part_rowid"],
                part_id=row["part_id"],
                message_id=row["message_id"],
                session_id=row["session_id"],
                timestamp_ms=row["time_created"],
                role=role,
                directory=row["directory"],
                session_slug=row["slug"],
                session_title=row["title"],
            )
            messages[row["message_id"]] = message
        part_type = part_data.get("type")
        if part_type == "text" and isinstance(part_data.get("text"), str):
            message.text_parts.append(part_data["text"])
        elif part_type == "tool":
            text = _tool_text(part_data)
            if text:
                message.tool_parts.append(text)
    return list(messages.values())


def _parse_time(value: str | None, now: float) -> int | None:
    if value is None:
        return None
    cleaned = value.strip()
    relative = re.fullmatch(r"(\d+(?:\.\d+)?)([dhms])", cleaned)
    if relative:
        multipliers = {"d": 86400, "h": 3600, "m": 60, "s": 1}
        seconds = float(relative.group(1)) * multipliers[relative.group(2)]
        return int((now - seconds) * 1000)
    try:
        parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    except ValueError as error:
        raise OpenCodeSearchError(f"bad time: {value}") from error
    if parsed.tzinfo is None:
        raise OpenCodeSearchError(f"bad time: {value} is missing a timezone")
    return int(parsed.timestamp() * 1000)


def _compile_pattern(options: SearchOptions) -> re.Pattern[str]:
    if not options.pattern:
        raise OpenCodeSearchError("pattern must be non-empty")
    if options.regex:
        raise OpenCodeSearchError(
            "regex search is not supported by the OpenCode provider; "
            "its Python runtime does not provide the native walker's linear-time RE2 semantics"
        )
    pieces = re.split(r"(\s+)", options.pattern)
    source = "".join(
        r"\s+" if piece.isspace() else re.escape(piece) for piece in pieces
    )
    flags = 0 if options.case_sensitive else re.IGNORECASE
    return re.compile(source, flags)


def _timestamp_string(timestamp_ms: int) -> str:
    value = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _snippet(text: str, match: tuple[int, int], limit: int = 240) -> str:
    start, end = match
    half = limit // 2
    lower = max(0, start - half)
    upper = min(len(text), end + half)
    return text[lower:upper]


def _match_offsets(pattern: re.Pattern[str], text: str) -> list[list[int]]:
    offsets: list[list[int]] = []
    for match in pattern.finditer(text):
        start, end = match.span()
        offsets.append(
            [
                len(text[:start].encode("utf-8")),
                len(text[:end].encode("utf-8")),
            ]
        )
    return offsets


def _cwd_matches(directory: str, filter_value: str | None) -> bool:
    if filter_value is None:
        return True
    return filter_value.casefold() in directory.casefold()


def _context(
    messages: list[_Message], index: int, count: int
) -> tuple[list[dict], list[dict]]:
    def render(message: _Message) -> dict[str, str]:
        return {
            "role": message.role,
            "text": message.text,
            "timestamp": _timestamp_string(message.timestamp_ms),
        }

    before = [render(message) for message in messages[max(0, index - count) : index]]
    after = [render(message) for message in messages[index + 1 : index + 1 + count]]
    return before, after


def _build_hits(
    messages: list[_Message],
    options: SearchOptions,
    pattern: re.Pattern[str],
    database: Path,
    now: float,
) -> tuple[list[dict[str, Any]], int]:
    since_ms = _parse_time(options.since, now)
    until_ms = _parse_time(options.until, now)
    sessions: dict[str, list[_Message]] = {}
    for message in messages:
        sessions.setdefault(message.session_id, []).append(message)
    hits: list[dict[str, Any]] = []
    for session_messages in sessions.values():
        for index, message in enumerate(session_messages):
            if options.role != "both" and message.role != options.role:
                continue
            if since_ms is not None and message.timestamp_ms < since_ms:
                continue
            if until_ms is not None and message.timestamp_ms > until_ms:
                continue
            if not _cwd_matches(message.directory, options.cwd):
                continue
            text = message.searchable_text
            matches = list(pattern.finditer(text))
            if not text or not matches:
                continue
            snippet = _snippet(text, matches[0].span())
            before, after = _context(session_messages, index, options.context_turns)
            hits.append(
                {
                    "type": "hit",
                    "provider": "opencode",
                    "session_id": message.session_id,
                    "session_slug": message.session_slug,
                    "session_title": message.session_title,
                    "cwd": message.directory,
                    "cwd_slug": Path(message.directory).name,
                    "host_root": str(database.parent),
                    "file_path": str(database),
                    "source_id": message.part_id,
                    "line_number": message.rowid,
                    "timestamp": _timestamp_string(message.timestamp_ms),
                    "role": message.role,
                    "snippet": snippet,
                    "match_offsets": _match_offsets(pattern, snippet),
                    "context_before": before,
                    "context_after": after,
                }
            )
    hits.sort(
        key=lambda hit: (
            -datetime.fromisoformat(
                hit["timestamp"].replace("Z", "+00:00")
            ).timestamp(),
            hit["session_id"],
            hit["line_number"],
        )
    )
    sessions_matched = len({hit["session_id"] for hit in hits})
    return hits, sessions_matched


def search(options: SearchOptions) -> dict[str, Any]:
    """Search OpenCode sessions and return the MCP provider result shape."""
    if options.limit < 1:
        raise OpenCodeSearchError("limit must be at least 1")
    if options.context_turns < 0:
        raise OpenCodeSearchError("context_turns must not be negative")
    if options.role not in {"user", "assistant", "both"}:
        raise OpenCodeSearchError(f"unknown role: {options.role}")
    started = time.monotonic()
    database = resolve_database(options.db_path)
    if database is None:
        candidates = ", ".join(
            str(path) for path in _database_candidates(options.db_path)
        )
        return {
            "hits": [],
            "summary": {
                "type": "summary",
                "provider": "opencode",
                "available": False,
                "hits": 0,
                "sessions_matched": 0,
                "roots_walked": 0,
                "files_walked": 0,
                "truncated": False,
                "elapsed_ms": 0,
            },
            "note": f"OpenCode database not found; looked in {candidates}",
        }
    pattern = _compile_pattern(options)
    try:
        with closing(_open_database(database)) as connection:
            _validate_schema(connection)
            messages = _load_messages(connection, options.include_tool_blocks)
    except sqlite3.Error as error:
        raise OpenCodeSearchError(
            f"cannot read OpenCode database {database}: {error}"
        ) from error
    all_hits, sessions_matched = _build_hits(
        messages, options, pattern, database, time.time()
    )
    total_hits = len(all_hits)
    truncated = total_hits > options.limit
    returned_hits = [] if options.count_only else all_hits[: options.limit]
    elapsed_ms = int((time.monotonic() - started) * 1000)
    summary_hits = total_hits if options.count_only else len(returned_hits)
    return {
        "hits": returned_hits,
        "summary": {
            "type": "summary",
            "provider": "opencode",
            "available": True,
            "hits": summary_hits,
            "sessions_matched": sessions_matched,
            "roots_walked": 1,
            "files_walked": 1,
            "truncated": truncated,
            "elapsed_ms": elapsed_ms,
            "database": str(database),
        },
        "note": (
            f"OpenCode search truncated to limit={options.limit}; narrow with since"
            if truncated
            else None
        ),
    }
