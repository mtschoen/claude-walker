"""Synthesize search-subcommand fixtures + expected outputs.

Writes to `shared/corpus/search/<scenario>/`:
- one or more JSONL transcript files (named `sid1.jsonl`, `sid2.jsonl`, ...)
- a sibling `expected.json` mapping flag-combo names to {pattern, flags, hits,
  summary}.

The conformance harness picks one scenario at a time, copies it into a temp
tree (so the scenario dir doubles as the cwd-slug), and runs
    walker search <pattern> <flags> --format jsonl
        --projects-root <tmp> --now <NOW_UNIX>
per combo. Structural-JSON equality vs. `hits`/`summary` after stripping
`host_root`, `file_path`, `elapsed_ms`, and `files_walked` (those vary per
run).

Anchored to the same NOW_UNIX as the beacon corpus (2026-05-09 12:00:00 UTC)
so the time-window scenario is reproducible.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parent
CORPUS_SEARCH = ROOT / "corpus" / "search"
CORPUS_SEARCH_MULTI_ROOT = ROOT / "corpus" / "search_multi_root"
CORPUS_SEARCH_CODEX = ROOT / "corpus" / "search_codex"

NOW_UNIX = 1778414400.0  # 2026-05-09 12:00:00 UTC -- matches beacon corpus

DAY = 86400.0


def iso(unix: float) -> str:
    return datetime.fromtimestamp(unix, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )


def assistant_text(unix_ts: float, msg_id: str, text: str) -> dict:
    """Assistant entry with a single text block."""
    return {
        "type": "assistant",
        "timestamp": iso(unix_ts),
        "message": {
            "id": msg_id,
            "role": "assistant",
            "model": "claude-opus-4-7",
            "content": [{"type": "text", "text": text}],
            "usage": {
                "input_tokens": 50,
                "output_tokens": 25,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        },
    }


def user_text_array(unix_ts: float, text: str) -> dict:
    """User entry with an array of text blocks (modern format)."""
    return {
        "type": "user",
        "timestamp": iso(unix_ts),
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": text}],
        },
    }


def user_bare_string(unix_ts: float, text: str) -> dict:
    """User entry with bare-string content (legacy format)."""
    return {
        "type": "user",
        "timestamp": iso(unix_ts),
        "message": {
            "role": "user",
            "content": text,
        },
    }


def codex_session_meta(unix_ts: float, session_id: str, cwd: str) -> dict:
    """Codex rollout metadata. `id` is accepted as a legacy alias by walkers."""
    return {
        "timestamp": iso(unix_ts),
        "type": "session_meta",
        "payload": {"session_id": session_id, "cwd": cwd},
    }


def codex_event(unix_ts: float, event_type: str, message: object) -> dict:
    """Codex event_msg record; non-string messages exercise lenient parsing."""
    return {
        "timestamp": iso(unix_ts),
        "type": "event_msg",
        "payload": {"type": event_type, "message": message},
    }


def user_tool_result(unix_ts: float, tool_use_id: str, content: str) -> dict:
    """User entry whose content is purely a tool_result block.

    Default search skips this; --include-tool-blocks picks it up.
    """
    return {
        "type": "user",
        "timestamp": iso(unix_ts),
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
            ],
        },
    }


def user_tool_result_array(unix_ts: float, tool_use_id: str, texts: list[str]) -> dict:
    """User entry whose content is a tool_result whose `content` is itself an
    array of text blocks (a shape some Claude API responses use).

    Default search skips it; --include-tool-blocks concatenates the inner text
    blocks (mirrors content.rs::extract_text 'tool_result content array' branch).
    """
    return {
        "type": "user",
        "timestamp": iso(unix_ts),
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": [{"type": "text", "text": t} for t in texts],
                }
            ],
        },
    }


def assistant_with_tool_use(
    unix_ts: float, msg_id: str, text: str, tool_use_id: str, tool_name: str,
    tool_input: dict | str,
) -> dict:
    """Assistant entry mixing a text block with a tool_use block. `tool_input`
    may be a dict (modern, structured) or a string (older clients that ship
    pre-stringified JSON). Both shapes round-trip through serde_json::Value /
    nlohmann::json / encoding/json / std.json — the impls dump the whole value
    back via Value::to_string-equivalent."""
    return {
        "type": "assistant",
        "timestamp": iso(unix_ts),
        "message": {
            "id": msg_id,
            "role": "assistant",
            "model": "claude-opus-4-7",
            "content": [
                {"type": "text", "text": text},
                {
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": tool_name,
                    "input": tool_input,
                },
            ],
            "usage": {
                "input_tokens": 50,
                "output_tokens": 25,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        },
    }


def assistant_text_no_timestamp(msg_id: str, text: str) -> dict:
    """Assistant entry missing the top-level `timestamp` field. Under a
    `--since`/`--until` filter the scanner MUST skip it (covers the
    'else if since.is_some() || until.is_some()' branch in every impl)."""
    return {
        "type": "assistant",
        "message": {
            "id": msg_id,
            "role": "assistant",
            "model": "claude-opus-4-7",
            "content": [{"type": "text", "text": text}],
            "usage": {
                "input_tokens": 50,
                "output_tokens": 25,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        },
    }


def queue_operation(unix_ts, operation, content=None) -> dict:
    """A `queue-operation` entry (input queued while the agent was busy).

    No `message` object — the text lives in a root-level `content` field, and
    the timestamp is root-level. `enqueue`/`popAll` carry content; `remove`/
    `dequeue` carry none. With `--include-queue-ops`, content-bearing entries
    index as `role: user`; without it, queue-ops are ignored entirely.

    `unix_ts=None` omits the timestamp (exercises the empty-timestamp arm).
    `content=None` omits the field entirely (the remove/dequeue shape); a
    non-str `content` (e.g. an int) exercises go's non-string-content skip arm
    (rust/cpp/zig fold that into the missing-field arm).
    """
    entry: dict = {"type": "queue-operation", "operation": operation}
    if unix_ts is not None:
        entry["timestamp"] = iso(unix_ts)
    if content is not None:
        entry["content"] = content
    return entry


def write_jsonl(path: Path, lines: "Sequence[dict | str | bytes]") -> None:
    """Dicts are json-dumped; raw strings written verbatim (malformed-line
    rungs); raw bytes as-is. Binary mode keeps byte rungs exact. An empty
    list produces an empty (zero-byte) file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        for line in lines:
            if isinstance(line, bytes):
                f.write(line)
            else:
                text = line if isinstance(line, str) else json.dumps(line)
                f.write(text.encode("utf-8"))
            f.write(b"\n")


# Hit-record helpers --------------------------------------------------------

def find_offset(text: str, needle: str, *, case_sensitive: bool = False) -> tuple[int, int]:
    if case_sensitive:
        idx = text.find(needle)
    else:
        idx = text.casefold().find(needle.casefold())
    assert idx >= 0, f"needle {needle!r} not in {text!r}"
    return (idx, idx + len(needle))


# Byte-level snippet reference ----------------------------------------------
# Replicates the snippet construction every impl performs (see SPEC.md
# "Snippet boundaries"). Works on UTF-8 *bytes* because offsets are byte
# offsets; the char-boundary nudge keeps a cut from splitting a codepoint.

_WHITESPACE = b" \t\n\r"


def _nudge_char_boundary(data: bytes, idx: int) -> int:
    """Nudge idx forward to the next UTF-8 character boundary."""
    while idx < len(data) and (data[idx] & 0xC0) == 0x80:
        idx += 1
    return idx


def _nudge_to_whitespace(data: bytes, cut: int, direction: int, max_nudge: int) -> int:
    """Nudge cut outward to the nearest ASCII whitespace within max_nudge bytes."""
    if cut == 0 or cut == len(data):
        return cut
    if direction < 0:
        lo = max(cut - max_nudge, 0)
        for i in range(cut, lo, -1):
            if data[i - 1] in _WHITESPACE:
                return i
    else:
        hi = min(cut + max_nudge, len(data))
        for i in range(cut, hi):
            if data[i] in _WHITESPACE:
                return i
    return cut


def snippet_and_offsets(
    text: str, needle: str, snippet_chars: int, *, case_sensitive: bool = False
) -> tuple[str, list[list[int]]]:
    """Return (snippet, match_offsets-within-snippet) for the first match of a
    literal `needle` in `text`, mirroring the impls. match_offsets are byte
    offsets within the emitted snippet (the matcher is re-run on the snippet)."""
    data = text.encode("utf-8")
    nbytes = needle.encode("utf-8")
    hay = data if case_sensitive else data.lower()
    nee = nbytes if case_sensitive else nbytes.lower()
    mstart = hay.find(nee)
    assert mstart >= 0, f"needle {needle!r} not found in {text!r}"
    mend = mstart + len(nbytes)

    half = snippet_chars // 2
    lo = _nudge_char_boundary(data, max(mstart - half, 0))
    hi = _nudge_char_boundary(data, min(mend + half, len(data)))
    if lo > 0:
        lo = _nudge_to_whitespace(data, lo, -1, 20)
    if hi < len(data):
        hi = _nudge_to_whitespace(data, hi, 1, 20)
    lo = _nudge_char_boundary(data, lo)
    hi = _nudge_char_boundary(data, hi)
    snip = data[lo:hi]

    # Re-run the (non-overlapping) matcher against the snippet bytes.
    snip_hay = snip if case_sensitive else snip.lower()
    offsets: list[list[int]] = []
    pos = 0
    while (found := snip_hay.find(nee, pos)) >= 0:
        offsets.append([found, found + len(nbytes)])
        pos = found + len(nbytes)
    return snip.decode("utf-8"), offsets


def hit(
    *,
    session_id: str,
    cwd_slug: str,
    line_number: int,
    timestamp: str,
    role: str,
    snippet: str,
    match_offsets: list[list[int]],
    context_before: list[dict] | None = None,
    context_after: list[dict] | None = None,
) -> dict:
    return {
        "type": "hit",
        "session_id": session_id,
        "cwd_slug": cwd_slug,
        "line_number": line_number,
        "timestamp": timestamp,
        "role": role,
        "snippet": snippet,
        "match_offsets": match_offsets,
        "context_before": context_before or [],
        "context_after": context_after or [],
    }


def ctx(role: str, text: str, timestamp: str) -> dict:
    return {"role": role, "text": text, "timestamp": timestamp}


def summary(*, hits: int, sessions_matched: int, roots_walked: int = 1, truncated: bool = False) -> dict:
    return {
        "type": "summary",
        "hits": hits,
        "sessions_matched": sessions_matched,
        "roots_walked": roots_walked,
        "truncated": truncated,
    }


# Scenarios -----------------------------------------------------------------

def scenario_01_basic():
    """Three sessions, one match each, default flags."""
    scenario = "01-basic"
    t1, t2, t3 = NOW_UNIX - 3000, NOW_UNIX - 2000, NOW_UNIX - 1000
    text1 = "first session mentions needle one time"
    text2 = "second session also contains needle here"
    text3 = "third session has the needle word"
    files = {
        "sid1.jsonl": [assistant_text(t1, "msg_01_s1", text1)],
        "sid2.jsonl": [assistant_text(t2, "msg_01_s2", text2)],
        "sid3.jsonl": [assistant_text(t3, "msg_01_s3", text3)],
    }
    o1 = find_offset(text1, "needle")
    o2 = find_offset(text2, "needle")
    o3 = find_offset(text3, "needle")
    # Newest-first ordering.
    hits = [
        hit(
            session_id="sid3", cwd_slug=scenario, line_number=1,
            timestamp=iso(t3), role="assistant", snippet=text3,
            match_offsets=[[o3[0], o3[1]]],
        ),
        hit(
            session_id="sid2", cwd_slug=scenario, line_number=1,
            timestamp=iso(t2), role="assistant", snippet=text2,
            match_offsets=[[o2[0], o2[1]]],
        ),
        hit(
            session_id="sid1", cwd_slug=scenario, line_number=1,
            timestamp=iso(t1), role="assistant", snippet=text1,
            match_offsets=[[o1[0], o1[1]]],
        ),
    ]
    expected = {
        "default": {
            "description": "Default flags: 3 hits across 3 sessions, newest first.",
            "pattern": "needle",
            "flags": [],
            "hits": hits,
            "summary": summary(hits=3, sessions_matched=3),
        },
    }
    return scenario, files, expected


def scenario_02_multi_match_per_session():
    """One session, three matching messages — verify all emitted, newest first."""
    scenario = "02-multi-match-per-session"
    t1, t2, t3 = NOW_UNIX - 3000, NOW_UNIX - 2000, NOW_UNIX - 1000
    text1 = "first mention of needle in turn one"
    text2 = "second mention of needle in turn two"
    text3 = "third mention of needle in turn three"
    files = {
        "sid1.jsonl": [
            assistant_text(t1, "msg_02_m1", text1),
            assistant_text(t2, "msg_02_m2", text2),
            assistant_text(t3, "msg_02_m3", text3),
        ],
    }
    o1 = find_offset(text1, "needle")
    o2 = find_offset(text2, "needle")
    o3 = find_offset(text3, "needle")
    # Newest first; each hit's context is its immediate neighbors.
    hits = [
        hit(
            session_id="sid1", cwd_slug=scenario, line_number=3,
            timestamp=iso(t3), role="assistant", snippet=text3,
            match_offsets=[[o3[0], o3[1]]],
            context_before=[ctx("assistant", text2, iso(t2))],
            context_after=[],
        ),
        hit(
            session_id="sid1", cwd_slug=scenario, line_number=2,
            timestamp=iso(t2), role="assistant", snippet=text2,
            match_offsets=[[o2[0], o2[1]]],
            context_before=[ctx("assistant", text1, iso(t1))],
            context_after=[ctx("assistant", text3, iso(t3))],
        ),
        hit(
            session_id="sid1", cwd_slug=scenario, line_number=1,
            timestamp=iso(t1), role="assistant", snippet=text1,
            match_offsets=[[o1[0], o1[1]]],
            context_before=[],
            context_after=[ctx("assistant", text2, iso(t2))],
        ),
    ]
    expected = {
        "default": {
            "description": "Three matches in one session, newest first, with --context=1 neighbors.",
            "pattern": "needle",
            "flags": [],
            "hits": hits,
            "summary": summary(hits=3, sessions_matched=1),
        },
    }
    return scenario, files, expected


def scenario_03_bare_string_user_content():
    """Older-format user message with bare-string content — must NOT be dropped."""
    scenario = "03-bare-string-user-content"
    t1, t2 = NOW_UNIX - 2000, NOW_UNIX - 1000
    assistant_msg = "no match here in the assistant turn"
    user_msg = "please find the needle in this haystack"
    files = {
        "sid1.jsonl": [
            assistant_text(t1, "msg_03_a1", assistant_msg),
            user_bare_string(t2, user_msg),
        ],
    }
    o = find_offset(user_msg, "needle")
    hits = [
        hit(
            session_id="sid1", cwd_slug=scenario, line_number=2,
            timestamp=iso(t2), role="user", snippet=user_msg,
            match_offsets=[[o[0], o[1]]],
            context_before=[ctx("assistant", assistant_msg, iso(t1))],
            context_after=[],
        ),
    ]
    expected = {
        "default": {
            "description": "Bare-string user content matches; strict typing would drop it.",
            "pattern": "needle",
            "flags": [],
            "hits": hits,
            "summary": summary(hits=1, sessions_matched=1),
        },
    }
    return scenario, files, expected


def scenario_04_role_filter():
    """Pattern in both user and assistant turns; --role partitions."""
    scenario = "04-role-filter"
    t1, t2, t3, t4 = (
        NOW_UNIX - 4000,
        NOW_UNIX - 3000,
        NOW_UNIX - 2000,
        NOW_UNIX - 1000,
    )
    a1 = "assistant talks about needle first"
    u2 = "user mentions needle as a reply"
    a3 = "assistant restates needle later"
    u4 = "user says nothing relevant"
    files = {
        "sid1.jsonl": [
            assistant_text(t1, "msg_04_a1", a1),
            user_text_array(t2, u2),
            assistant_text(t3, "msg_04_a3", a3),
            user_text_array(t4, u4),
        ],
    }
    oa1 = find_offset(a1, "needle")
    ou2 = find_offset(u2, "needle")
    oa3 = find_offset(a3, "needle")
    hit_a1 = hit(
        session_id="sid1", cwd_slug=scenario, line_number=1,
        timestamp=iso(t1), role="assistant", snippet=a1,
        match_offsets=[[oa1[0], oa1[1]]],
        context_before=[],
        context_after=[ctx("user", u2, iso(t2))],
    )
    hit_u2 = hit(
        session_id="sid1", cwd_slug=scenario, line_number=2,
        timestamp=iso(t2), role="user", snippet=u2,
        match_offsets=[[ou2[0], ou2[1]]],
        context_before=[ctx("assistant", a1, iso(t1))],
        context_after=[ctx("assistant", a3, iso(t3))],
    )
    hit_a3 = hit(
        session_id="sid1", cwd_slug=scenario, line_number=3,
        timestamp=iso(t3), role="assistant", snippet=a3,
        match_offsets=[[oa3[0], oa3[1]]],
        context_before=[ctx("user", u2, iso(t2))],
        context_after=[ctx("user", u4, iso(t4))],
    )
    expected = {
        "default": {
            "description": "Default role=both: 3 hits (a1, u2, a3), newest first.",
            "pattern": "needle",
            "flags": [],
            "hits": [hit_a3, hit_u2, hit_a1],
            "summary": summary(hits=3, sessions_matched=1),
        },
        "role-user": {
            "description": "--role user: just the user turn.",
            "pattern": "needle",
            "flags": ["--role", "user"],
            "hits": [hit_u2],
            "summary": summary(hits=1, sessions_matched=1),
        },
        "role-assistant": {
            "description": "--role assistant: just the two assistant turns, newest first.",
            "pattern": "needle",
            "flags": ["--role", "assistant"],
            "hits": [hit_a3, hit_a1],
            "summary": summary(hits=2, sessions_matched=1),
        },
    }
    return scenario, files, expected


def scenario_05_tool_block_skip():
    """Pattern only inside a tool_result; default skips, --include-tool-blocks finds."""
    scenario = "05-tool-block-skip"
    t1, t2 = NOW_UNIX - 2000, NOW_UNIX - 1000
    assistant_msg = "I'll search for it now"
    tool_output = "needle: found at line 42"
    files = {
        "sid1.jsonl": [
            assistant_text(t1, "msg_05_a1", assistant_msg),
            user_tool_result(t2, "toolu_05_01", tool_output),
        ],
    }
    o = find_offset(tool_output, "needle")
    hits_with_tool = [
        hit(
            session_id="sid1", cwd_slug=scenario, line_number=2,
            timestamp=iso(t2), role="user", snippet=tool_output,
            match_offsets=[[o[0], o[1]]],
            context_before=[ctx("assistant", assistant_msg, iso(t1))],
            context_after=[],
        ),
    ]
    expected = {
        "default": {
            "description": "Default skips tool_result-only messages: 0 hits.",
            "pattern": "needle",
            "flags": [],
            "hits": [],
            "summary": summary(hits=0, sessions_matched=0),
        },
        "include-tool-blocks": {
            "description": "--include-tool-blocks picks up tool_result content.",
            "pattern": "needle",
            "flags": ["--include-tool-blocks"],
            "hits": hits_with_tool,
            "summary": summary(hits=1, sessions_matched=1),
        },
    }
    return scenario, files, expected


def scenario_06_regex():
    """--regex 'foo\\d+' against foo1, foo, foo42; expect 2 matches."""
    scenario = "06-regex"
    t1, t2, t3 = NOW_UNIX - 3000, NOW_UNIX - 2000, NOW_UNIX - 1000
    text1 = "calling foo1 now"
    text2 = "just foo without digits"  # no digit follow-up, no match
    text3 = "calling foo42 here"
    files = {
        "sid1.jsonl": [
            assistant_text(t1, "msg_06_a1", text1),
            assistant_text(t2, "msg_06_a2", text2),
            assistant_text(t3, "msg_06_a3", text3),
        ],
    }
    o1 = (text1.find("foo1"), text1.find("foo1") + 4)
    o3 = (text3.find("foo42"), text3.find("foo42") + 5)
    hit_t1 = hit(
        session_id="sid1", cwd_slug=scenario, line_number=1,
        timestamp=iso(t1), role="assistant", snippet=text1,
        match_offsets=[[o1[0], o1[1]]],
        context_before=[],
        context_after=[ctx("assistant", text2, iso(t2))],
    )
    hit_t3 = hit(
        session_id="sid1", cwd_slug=scenario, line_number=3,
        timestamp=iso(t3), role="assistant", snippet=text3,
        match_offsets=[[o3[0], o3[1]]],
        context_before=[ctx("assistant", text2, iso(t2))],
        context_after=[],
    )
    expected = {
        "regex": {
            "description": "--regex 'foo\\\\d+' matches foo1 and foo42, skips bare foo.",
            "pattern": "foo\\d+",
            "flags": ["--regex"],
            "hits": [hit_t3, hit_t1],
            "summary": summary(hits=2, sessions_matched=1),
        },
    }
    return scenario, files, expected


def scenario_07_case():
    """Case-insensitive default returns 3 hits; --case-sensitive returns 1."""
    scenario = "07-case"
    t1, t2, t3 = NOW_UNIX - 3000, NOW_UNIX - 2000, NOW_UNIX - 1000
    text1 = "found Needle uppercase N"
    text2 = "found NEEDLE all caps"
    text3 = "found needle lowercase"
    files = {
        "sid1.jsonl": [
            assistant_text(t1, "msg_07_a1", text1),
            assistant_text(t2, "msg_07_a2", text2),
            assistant_text(t3, "msg_07_a3", text3),
        ],
    }
    # Case-insensitive: substring "needle" matches all three.
    o1_ci = find_offset(text1, "needle")
    o2_ci = find_offset(text2, "needle")
    o3_ci = find_offset(text3, "needle")
    hit_t1_ci = hit(
        session_id="sid1", cwd_slug=scenario, line_number=1,
        timestamp=iso(t1), role="assistant", snippet=text1,
        match_offsets=[[o1_ci[0], o1_ci[1]]],
        context_before=[],
        context_after=[ctx("assistant", text2, iso(t2))],
    )
    hit_t2_ci = hit(
        session_id="sid1", cwd_slug=scenario, line_number=2,
        timestamp=iso(t2), role="assistant", snippet=text2,
        match_offsets=[[o2_ci[0], o2_ci[1]]],
        context_before=[ctx("assistant", text1, iso(t1))],
        context_after=[ctx("assistant", text3, iso(t3))],
    )
    hit_t3_ci = hit(
        session_id="sid1", cwd_slug=scenario, line_number=3,
        timestamp=iso(t3), role="assistant", snippet=text3,
        match_offsets=[[o3_ci[0], o3_ci[1]]],
        context_before=[ctx("assistant", text2, iso(t2))],
        context_after=[],
    )
    # Case-sensitive: only the lowercase one.
    o3_cs = find_offset(text3, "needle", case_sensitive=True)
    hit_t3_cs = hit(
        session_id="sid1", cwd_slug=scenario, line_number=3,
        timestamp=iso(t3), role="assistant", snippet=text3,
        match_offsets=[[o3_cs[0], o3_cs[1]]],
        context_before=[ctx("assistant", text2, iso(t2))],
        context_after=[],
    )
    expected = {
        "default": {
            "description": "Default case-insensitive: all three variants match.",
            "pattern": "needle",
            "flags": [],
            "hits": [hit_t3_ci, hit_t2_ci, hit_t1_ci],
            "summary": summary(hits=3, sessions_matched=1),
        },
        "case-sensitive": {
            "description": "--case-sensitive: only the lowercase variant matches.",
            "pattern": "needle",
            "flags": ["--case-sensitive"],
            "hits": [hit_t3_cs],
            "summary": summary(hits=1, sessions_matched=1),
        },
    }
    return scenario, files, expected


def scenario_08_time_window():
    """Three matches at 30d/14d/3d ago; --since 7d returns only the 3d-old hit."""
    scenario = "08-time-window"
    t_30d = NOW_UNIX - 30 * DAY
    t_14d = NOW_UNIX - 14 * DAY
    t_3d = NOW_UNIX - 3 * DAY
    text1 = "needle thirty days ago"
    text2 = "needle fourteen days ago"
    text3 = "needle three days ago"
    files = {
        "sid1.jsonl": [
            assistant_text(t_30d, "msg_08_a1", text1),
            assistant_text(t_14d, "msg_08_a2", text2),
            assistant_text(t_3d, "msg_08_a3", text3),
        ],
    }
    o1 = find_offset(text1, "needle")
    o2 = find_offset(text2, "needle")
    o3 = find_offset(text3, "needle")
    hit_30d = hit(
        session_id="sid1", cwd_slug=scenario, line_number=1,
        timestamp=iso(t_30d), role="assistant", snippet=text1,
        match_offsets=[[o1[0], o1[1]]],
        context_before=[],
        context_after=[ctx("assistant", text2, iso(t_14d))],
    )
    hit_14d = hit(
        session_id="sid1", cwd_slug=scenario, line_number=2,
        timestamp=iso(t_14d), role="assistant", snippet=text2,
        match_offsets=[[o2[0], o2[1]]],
        context_before=[ctx("assistant", text1, iso(t_30d))],
        context_after=[ctx("assistant", text3, iso(t_3d))],
    )
    hit_3d = hit(
        session_id="sid1", cwd_slug=scenario, line_number=3,
        timestamp=iso(t_3d), role="assistant", snippet=text3,
        match_offsets=[[o3[0], o3[1]]],
        context_before=[ctx("assistant", text2, iso(t_14d))],
        context_after=[],
    )
    # Context is positional and unfiltered — it shows the transcript neighbors
    # of a hit regardless of which filter (role, time-window, etc.) excluded
    # those neighbors from emitting their own hits. This matches the precedent
    # set by the role-filter scenario, where context crosses the --role
    # boundary. Spec edge case to confirm at review time.
    hit_3d_with_filtered_ctx = hit(
        session_id="sid1", cwd_slug=scenario, line_number=3,
        timestamp=iso(t_3d), role="assistant", snippet=text3,
        match_offsets=[[o3[0], o3[1]]],
        context_before=[ctx("assistant", text2, iso(t_14d))],
        context_after=[],
    )
    expected = {
        "default": {
            "description": "No --since: all three hits, newest first.",
            "pattern": "needle",
            "flags": [],
            "hits": [hit_3d, hit_14d, hit_30d],
            "summary": summary(hits=3, sessions_matched=1),
        },
        "since-7d": {
            "description": "--since 7d: only the 3d-old hit survives the time filter.",
            "pattern": "needle",
            "flags": ["--since", "7d"],
            "hits": [hit_3d_with_filtered_ctx],
            "summary": summary(hits=1, sessions_matched=1),
        },
    }
    return scenario, files, expected


def scenario_09_count_only():
    """5 matching messages; default returns 5 hits + summary, --count-only just summary."""
    scenario = "09-count-only"
    timestamps = [NOW_UNIX - (5 - i) * 500 for i in range(5)]
    texts = [f"needle hit number {i + 1}" for i in range(5)]
    files = {
        "sid1.jsonl": [
            assistant_text(timestamps[i], f"msg_09_a{i + 1}", texts[i])
            for i in range(5)
        ],
    }
    hits_default = []
    # Build newest-first hits with context (prev/next text turn).
    for i in reversed(range(5)):
        ts = timestamps[i]
        text = texts[i]
        o = find_offset(text, "needle")
        cb = [ctx("assistant", texts[i - 1], iso(timestamps[i - 1]))] if i - 1 >= 0 else []
        ca = [ctx("assistant", texts[i + 1], iso(timestamps[i + 1]))] if i + 1 < 5 else []
        hits_default.append(hit(
            session_id="sid1", cwd_slug=scenario, line_number=i + 1,
            timestamp=iso(ts), role="assistant", snippet=text,
            match_offsets=[[o[0], o[1]]],
            context_before=cb,
            context_after=ca,
        ))
    expected = {
        "default": {
            "description": "Default: 5 hits + summary.",
            "pattern": "needle",
            "flags": [],
            "hits": hits_default,
            "summary": summary(hits=5, sessions_matched=1),
        },
        "count-only": {
            "description": "--count-only: no hit records, just the summary.",
            "pattern": "needle",
            "flags": ["--count-only"],
            "hits": [],
            "summary": summary(hits=5, sessions_matched=1),
        },
    }
    return scenario, files, expected


def scenario_10_context_zero():
    """7-turn session, hit in middle; --context 0 returns hit with empty context arrays."""
    scenario = "10-context-zero"
    timestamps = [NOW_UNIX - (7 - i) * 500 for i in range(7)]
    # Turns alternate user/assistant. Hit is in turn 4 (index 3).
    a1 = "intro from assistant"
    u1 = "user prompt one"
    a2 = "assistant reply one"
    a3 = "assistant says needle in middle"
    u2 = "user follow-up"
    a4 = "assistant reply two"
    u3 = "user thanks"
    entries = [
        assistant_text(timestamps[0], "msg_10_a1", a1),
        user_text_array(timestamps[1], u1),
        assistant_text(timestamps[2], "msg_10_a2", a2),
        assistant_text(timestamps[3], "msg_10_a3", a3),
        user_text_array(timestamps[4], u2),
        assistant_text(timestamps[5], "msg_10_a4", a4),
        user_text_array(timestamps[6], u3),
    ]
    files = {"sid1.jsonl": entries}
    o = find_offset(a3, "needle")
    # --context 0: empty context arrays.
    hit_ctx0 = hit(
        session_id="sid1", cwd_slug=scenario, line_number=4,
        timestamp=iso(timestamps[3]), role="assistant", snippet=a3,
        match_offsets=[[o[0], o[1]]],
        context_before=[],
        context_after=[],
    )
    # Default --context 1: one turn before, one after.
    hit_ctx1 = hit(
        session_id="sid1", cwd_slug=scenario, line_number=4,
        timestamp=iso(timestamps[3]), role="assistant", snippet=a3,
        match_offsets=[[o[0], o[1]]],
        context_before=[ctx("assistant", a2, iso(timestamps[2]))],
        context_after=[ctx("user", u2, iso(timestamps[4]))],
    )
    expected = {
        "default": {
            "description": "Default --context 1: one neighbor on each side.",
            "pattern": "needle",
            "flags": [],
            "hits": [hit_ctx1],
            "summary": summary(hits=1, sessions_matched=1),
        },
        "context-zero": {
            "description": "--context 0: hit message only, empty context arrays.",
            "pattern": "needle",
            "flags": ["--context", "0"],
            "hits": [hit_ctx0],
            "summary": summary(hits=1, sessions_matched=1),
        },
    }
    return scenario, files, expected


def scenario_11_multibyte_snippet_boundary():
    """Multibyte UTF-8 around the match + a small --snippet-chars forces the
    snippet cut to land mid-codepoint. There is no whitespace near the cut, so
    the whitespace nudge is a no-op and ONLY the char-boundary nudge keeps the
    snippet valid UTF-8. Without it, three of the four impls would slice a
    partial codepoint. See SPEC.md "Snippet boundaries"."""
    scenario = "11-multibyte-snippet-boundary"
    t1 = NOW_UNIX - 1000
    # 6 three-byte CJK chars, "needle", 6 more — no spaces anywhere.
    text = "中" * 6 + "needle" + "中" * 6
    files = {"sid1.jsonl": [assistant_text(t1, "msg_11_a1", text)]}
    snip, offsets = snippet_and_offsets(text, "needle", 20)
    hits = [
        hit(
            session_id="sid1", cwd_slug=scenario, line_number=1,
            timestamp=iso(t1), role="assistant", snippet=snip,
            match_offsets=offsets,
        ),
    ]
    expected = {
        "snippet-chars-20": {
            "description": "Multibyte chars + small --snippet-chars: snippet must "
                           "stay on UTF-8 boundaries (no split codepoints).",
            "pattern": "needle",
            "flags": ["--snippet-chars", "20"],
            "hits": hits,
            "summary": summary(hits=1, sessions_matched=1),
        },
    }
    return scenario, files, expected


def scenario_12_tool_use_and_result_array():
    """Cover the tool_use input + tool_result content-array branches.

    SPEC §"Search" + CONTENT extraction: with `--include-tool-blocks`, search
    pulls (a) tool_use.input and (b) tool_result.content when the latter is an
    array of text blocks (not just the string shape exercised by scenario 05).
    Without the flag those branches must be invisible.

    Layout — two sessions, each isolating one shape:

      sid_a: assistant text "ok" + tool_use w/ STRING input "the needle here"
              -> hits the `tool_use if include_tool_blocks` branch in every
                 impl's extract_text equivalent. The exact in-snippet form is
                 PER-IMPL drift (rust dumps via Value::to_string() — quoted;
                 cpp unwraps strings — unquoted; go preserves source bytes —
                 quoted), so this combo uses `--count-only` and asserts only
                 the hit count, not the snippet bytes.
      sid_c: user content = [tool_result whose content is a text-block array]
              -> hits the `tool_result.content as_array` branch. All impls
                 join inner text blocks with "\n" deterministically.

    sid_a has a non-empty text block ("ok") so default-mode still scans the
    message and walks all blocks — verifying the tool branches stay gated by
    --include-tool-blocks even when the entry is NOT pure tool_use. sid_c is
    pure tool_result-array, default-skipped via is_only_tool_blocks.
    """
    scenario = "12-tool-use-and-result-array"
    t_a, t_c = NOW_UNIX - 3000, NOW_UNIX - 1000

    text_a = "ok"
    string_input = "the needle here"
    arr_texts = ["before block", "needle in array block", "after block"]

    files = {
        "sid_a.jsonl": [
            assistant_with_tool_use(
                t_a, "msg_12_a", text_a, "toolu_12_a", "fake_tool", string_input,
            ),
        ],
        "sid_c.jsonl": [
            user_tool_result_array(t_c, "toolu_12_c", arr_texts),
        ],
    }

    blob_c = "\n".join(arr_texts)
    snip_c, off_c = snippet_and_offsets(blob_c, "needle", 240)

    hit_c = hit(
        session_id="sid_c", cwd_slug=scenario, line_number=1,
        timestamp=iso(t_c), role="user", snippet=snip_c,
        match_offsets=off_c,
    )

    expected = {
        "default": {
            "description": "Default mode: tool_use input and tool_result-array "
                           "are invisible (sid_a 'ok' text has no needle, "
                           "sid_c is only-tool-blocks). 0 hits.",
            "pattern": "needle",
            "flags": [],
            "hits": [],
            "summary": summary(hits=0, sessions_matched=0),
        },
        "include-tool-blocks-array": {
            "description": "--include-tool-blocks finds the needle inside a "
                           "tool_result.content text-block array (sid_c only; "
                           "sid_a's tool_use input snippet is per-impl).",
            "pattern": "needle",
            "flags": ["--include-tool-blocks", "--cwd", scenario, "--role", "user"],
            "hits": [hit_c],
            "summary": summary(hits=1, sessions_matched=1),
        },
        "include-tool-blocks-count": {
            "description": "--include-tool-blocks + --count-only across both "
                           "sessions: count alone (snippet bytes for tool_use "
                           "differ per impl, so we don't pin them).",
            "pattern": "needle",
            "flags": ["--include-tool-blocks", "--count-only"],
            "hits": [],
            "summary": summary(hits=2, sessions_matched=2),
        },
    }
    return scenario, files, expected


def scenario_13_time_units_and_edges():
    """Exercise every `--since`/`--until` relative-unit arm + an ISO absolute,
    a message past `--until`, and an entry with no top-level timestamp.

    Layout — one session, four entries:
      e1 (t-30d):   "needle thirty days ago"
      e2 (t-2h):    "needle two hours ago"
      e3 (t-45s):   "needle forty-five seconds ago"
      e4 (no ts):   "needle but no timestamp"  (must be skipped under any
                                                  --since/--until filter; emits
                                                  under no-time-filter cases)

    Combos:
      since-2h:   e3 only (e2 is exactly at the boundary; we set t-2h-1 below
                  to land it inside). e1 too old, e4 has no ts -> skipped.
      since-30m:  e3 only.
      since-10s:  empty (e3 is 45s ago, older than 10s).
      since-iso:  RFC3339 absolute matching the --since=2h cutoff exactly.
      until-1h:   e1 only (e3/e2 are newer than the --until cutoff; e4 skipped
                  because it has no timestamp under a time filter).
      no-filter:  all four entries with timestamps emit; the e4 (no-ts) entry
                  emits too (no time filter -> the missing-timestamp branch
                  is not reached). The newest-first sort key for e4 is
                  whatever the scanner emits — every impl falls back to "" in
                  the JSON output. (Asserted below.)
    """
    scenario = "13-time-units-and-edges"
    # Slack the t-2h entry past the 2h boundary so it filters reliably across
    # impls (a hair past the cutoff, not exactly on it).
    t_e1 = NOW_UNIX - 30 * DAY
    t_e2 = NOW_UNIX - 7201.0   # 2h 1s old
    t_e3 = NOW_UNIX - 45.0     # 45s old
    text_e1 = "needle thirty days ago"
    text_e2 = "needle two hours ago"
    text_e3 = "needle forty-five seconds ago"
    text_e4 = "needle but no timestamp"

    files = {
        "sid1.jsonl": [
            assistant_text(t_e1, "msg_13_e1", text_e1),
            assistant_text(t_e2, "msg_13_e2", text_e2),
            assistant_text(t_e3, "msg_13_e3", text_e3),
            assistant_text_no_timestamp("msg_13_e4", text_e4),
        ],
    }

    o_e1 = find_offset(text_e1, "needle")
    o_e2 = find_offset(text_e2, "needle")
    o_e3 = find_offset(text_e3, "needle")
    o_e4 = find_offset(text_e4, "needle")

    h_e1 = hit(
        session_id="sid1", cwd_slug=scenario, line_number=1,
        timestamp=iso(t_e1), role="assistant", snippet=text_e1,
        match_offsets=[[o_e1[0], o_e1[1]]],
        context_before=[],
        context_after=[ctx("assistant", text_e2, iso(t_e2))],
    )
    h_e2 = hit(
        session_id="sid1", cwd_slug=scenario, line_number=2,
        timestamp=iso(t_e2), role="assistant", snippet=text_e2,
        match_offsets=[[o_e2[0], o_e2[1]]],
        context_before=[ctx("assistant", text_e1, iso(t_e1))],
        context_after=[ctx("assistant", text_e3, iso(t_e3))],
    )
    h_e3 = hit(
        session_id="sid1", cwd_slug=scenario, line_number=3,
        timestamp=iso(t_e3), role="assistant", snippet=text_e3,
        match_offsets=[[o_e3[0], o_e3[1]]],
        context_before=[ctx("assistant", text_e2, iso(t_e2))],
        # e4 has no timestamp -> emits as ts="" in context (matches scanner
        # default-fallback across all four impls).
        context_after=[ctx("assistant", text_e4, "")],
    )
    h_e4 = hit(
        session_id="sid1", cwd_slug=scenario, line_number=4,
        timestamp="", role="assistant", snippet=text_e4,
        match_offsets=[[o_e4[0], o_e4[1]]],
        context_before=[ctx("assistant", text_e3, iso(t_e3))],
        context_after=[],
    )

    # An RFC3339 timestamp equal to "2h ago" relative to NOW_UNIX, so
    # since-iso must produce the same hits as since-2h.
    iso_2h_cutoff = iso(NOW_UNIX - 7200.0)

    expected = {
        "since-2h": {
            "description": "--since 2h: only the 45s-old entry survives (e2 is "
                           "past 2h boundary; e4 skipped — no timestamp).",
            "pattern": "needle",
            "flags": ["--since", "2h"],
            "hits": [h_e3],
            "summary": summary(hits=1, sessions_matched=1),
        },
        "since-30m": {
            "description": "--since 30m: only the 45s-old entry survives.",
            "pattern": "needle",
            "flags": ["--since", "30m"],
            "hits": [h_e3],
            "summary": summary(hits=1, sessions_matched=1),
        },
        "since-10s": {
            "description": "--since 10s: nothing matches (e3 is 45s old; "
                           "e4 still skipped under any time filter).",
            "pattern": "needle",
            "flags": ["--since", "10s"],
            "hits": [],
            "summary": summary(hits=0, sessions_matched=0),
        },
        "since-iso": {
            "description": "--since RFC3339 absolute (equiv. to 2h relative).",
            "pattern": "needle",
            "flags": ["--since", iso_2h_cutoff],
            "hits": [h_e3],
            "summary": summary(hits=1, sessions_matched=1),
        },
        "until-1h": {
            "description": "--until 1h: the 30d and ~2h entries match (e3 newer "
                           "than cutoff; e4 skipped — missing timestamp + filter).",
            "pattern": "needle",
            "flags": ["--until", "1h"],
            "hits": [h_e2, h_e1],
            "summary": summary(hits=2, sessions_matched=1),
        },
        "no-filter": {
            "description": "No --since/--until: all four entries match, e4 "
                           "(no-ts) emits with empty timestamp string.",
            "pattern": "needle",
            "flags": [],
            # h_e4 has timestamp "" so newest-first puts it last (lexicographic
            # comparison; empty < any RFC3339 string).
            "hits": [h_e3, h_e2, h_e1, h_e4],
            "summary": summary(hits=4, sessions_matched=1),
        },
    }
    return scenario, files, expected


def scenario_14_snippet_whitespace_nudge():
    """A match deep inside a long ASCII paragraph where the raw snippet cuts
    land mid-word, with ASCII whitespace within ±20 bytes of each cut so the
    whitespace-nudge loop fires on BOTH sides (left and right).

    Scenario 11 already exercises the char-boundary nudge with no whitespace
    nearby. This sibling covers the orthogonal axis: whitespace IS available,
    so the nudge moves the cut outward to a word boundary, producing a snippet
    that begins after whitespace and ends at whitespace. snippet_and_offsets()
    mirrors the impl exactly, so the expected output comes from the helper.
    """
    scenario = "14-snippet-whitespace-nudge"
    t1 = NOW_UNIX - 1000
    # Build a paragraph: pad words on both sides of "needle" so the raw cut at
    # ±half snippet_chars lands inside a word, with whitespace within ±20.
    # snippet_chars=60 -> half=30. We place spaces at byte offsets ~10-15 from
    # the cut so the whitespace nudge picks them up.
    left = "alpha beta gamma delta epsilon zeta eta theta iota kappa "
    right = " lambda mu nu xi omicron pi rho sigma tau upsilon phi chi psi"
    text = left + "needle" + right
    files = {"sid1.jsonl": [assistant_text(t1, "msg_14_a1", text)]}
    snip, offsets = snippet_and_offsets(text, "needle", 60)
    # Sanity: the snippet should start/end at a whitespace-trimmed boundary
    # AND must NOT be the full text (cuts were made + nudged). If either
    # condition fails the helper drifted from the impls' algorithm.
    assert snip != text, "snippet did not get cut — bump padding length"
    assert not snip.startswith(" ") and not snip.endswith(" "), (
        "whitespace nudge produced leading/trailing space"
    )
    hits = [
        hit(
            session_id="sid1", cwd_slug=scenario, line_number=1,
            timestamp=iso(t1), role="assistant", snippet=snip,
            match_offsets=offsets,
        ),
    ]
    expected = {
        "snippet-chars-60": {
            "description": "Long ASCII paragraph + small --snippet-chars: both "
                           "snippet cuts land mid-word and get nudged outward "
                           "to whitespace boundaries (no codepoint splits).",
            "pattern": "needle",
            "flags": ["--snippet-chars", "60"],
            "hits": hits,
            "summary": summary(hits=1, sessions_matched=1),
        },
    }
    return scenario, files, expected


REGEX_CLASS_SPECS = [
    # (combo_name, description, text, pattern, match_offsets)
    # Patterns are raw strings: r"\d+" -> the two chars `\d+`.
    # Offsets are byte offsets within `text`; with default snippet_chars=240
    # snippet == text.
    ("digit-class",
     "\\d+ — digit class escape",
     "alpha 7 beta", r"\d+", [[6, 7]]),
    ("non-digit-class",
     "\\D+ — non-digit class escape, greedy",
     "alpha-7-beta", r"\D+", [[0, 6], [7, 12]]),
    ("word-class",
     "\\w+ — word class escape",
     "foo bar", r"\w+", [[0, 3], [4, 7]]),
    ("non-word-class",
     "\\W — non-word class escape",
     "ping!pong", r"\W", [[4, 5]]),
    ("default-escape",
     "\\! — default-escape arm (literal `!`, escape char NOT in dDwWsSntr)",
     "ping!pong", r"\!", [[4, 5]]),
    ("space-class",
     "\\s — whitespace class escape (single ASCII space)",
     "foo bar", r"\s", [[3, 4]]),
    ("non-space-class",
     "\\S+ — non-whitespace class escape, greedy",
     "foo bar", r"\S+", [[0, 3], [4, 7]]),
    ("dot-metachar",
     ". — dot atom (any byte except newline)",
     "section 5: end", r"5.", [[8, 10]]),
    ("escaped-dot",
     "\\. — escaped dot literal",
     "literal a.b dot", r"a\.b", [[8, 11]]),
    ("class-range",
     "[a-z]+ — char class with range expansion",
     "items cat dog", r"[a-z]+", [[0, 5], [6, 9], [10, 13]]),
    ("class-negated",
     "[^a-z]+ — negated class (case-sensitive so uppercase/digits land)",
     "code Z and 9 here", r"[^a-z]+", [[4, 7], [10, 13]]),
    ("class-simple",
     "[abc]+ — class set without ranges",
     "show me a or b or c", r"[abc]+", [[8, 9], [13, 14], [18, 19]]),
    ("quant-star",
     "a*b — `*` quantifier with backtracking",
     "aaab tail", r"a*b", [[0, 4]]),
    ("quant-opt",
     "colou?r — `?` quantifier",
     "color colour", r"colou?r", [[0, 5], [6, 12]]),
    ("quant-plus-on-class",
     "[0-9]+x — `+` quantifier on a class atom",
     "123x done", r"[0-9]+x", [[0, 4]]),
    ("class-escape-in-class",
     "[\\n\\t\\r] — escape-in-class arm; matches a tab between letters",
     "left\tright", r"[\n\t\r]", [[4, 5]]),
    ("escape-atoms",
     "\\t/\\r/\\n - control-escape atoms OUTSIDE a class",
     "a\tb\rc\nd tail", r"a\tb\rc\nd", [[0, 7]]),
    ("class-escaped-range-end",
     "[%-\\\\]+ - class range whose END is an escaped char",
     "see + or . mark", r"[%-\\]+", [[4, 5], [9, 10]]),
]


def _make_regex_class_scenario(index: int, name: str, description: str,
                                text: str, pattern: str,
                                match_offsets: list[list[int]]):
    """A7 helper: each pattern gets its OWN scenario dir so walker only scans
    one message — eliminates cross-pattern leakage when the regex matches
    multiple sessions in the same scenario dir.

    Naming: `15a-regex-class-<name>` ... `15p-regex-...`. Letters keep the
    sort order stable for kcov-comparison diffs.

    `flags` is fixed at --regex --case-sensitive: the engine's case-insensitive
    class expansion would let some negated/positive classes match both halves
    of the alphabet and offsets drift across impls.
    """
    suffix = chr(ord('a') + index)
    scenario = f"15{suffix}-regex-{name}"
    ts = NOW_UNIX - 5000 + index * 100
    files = {
        "sid1.jsonl": [assistant_text(ts, f"msg_15{suffix}_a1", text)],
    }
    expected = {
        "default": {
            "description": description,
            "pattern": pattern,
            "flags": ["--regex", "--case-sensitive"],
            "hits": [hit(
                session_id="sid1", cwd_slug=scenario, line_number=1,
                timestamp=iso(ts), role="assistant", snippet=text,
                match_offsets=match_offsets,
                context_before=[], context_after=[],
            )],
            "summary": summary(hits=1, sessions_matched=1),
        },
    }
    return scenario, files, expected


def _regex_class_scenario_factory(index: int):
    """Return a zero-arg scenario function bound to spec[index]. Returning a
    closure lets us register each as a separate entry in SCENARIOS."""
    name, description, text, pattern, match_offsets = REGEX_CLASS_SPECS[index]
    def scenario_fn():
        return _make_regex_class_scenario(
            index, name, description, text, pattern, match_offsets,
        )
    scenario_fn.__name__ = f"scenario_15{chr(ord('a') + index)}_regex_{name.replace('-', '_')}"
    scenario_fn.__doc__ = description
    return scenario_fn


# Generated scenario functions: one per row of REGEX_CLASS_SPECS.
REGEX_CLASS_SCENARIOS = [
    _regex_class_scenario_factory(i) for i in range(len(REGEX_CLASS_SPECS))
]


def scenario_16_same_timestamp():
    """A16: Two messages with the SAME timestamp in two different sessions
    must sort by (session_id, line_number) for deterministic order.

    Layout:
      sid_a.jsonl line 1 at T
      sid_a.jsonl line 2 at T (same ts, same session) — tiebreak by line_number
      sid_b.jsonl line 1 at T (same ts, different session) — tiebreak by session_id

    With case-insensitive default search for "needle" all three match.
    Expected order (per all four impls' sort comparator: timestamp desc, then
    session_id asc, then line_number asc):
      1. sid_a / line 1
      2. sid_a / line 2
      3. sid_b / line 1
    """
    scenario = "16-same-timestamp"
    t = NOW_UNIX - 1000
    text_a1 = "needle one in session A line 1"
    text_a2 = "needle two in session A line 2"
    text_b1 = "needle three in session B line 1"
    files = {
        "sid_a.jsonl": [
            assistant_text(t, "msg_16_a1", text_a1),
            assistant_text(t, "msg_16_a2", text_a2),
        ],
        "sid_b.jsonl": [
            assistant_text(t, "msg_16_b1", text_b1),
        ],
    }
    o_a1 = find_offset(text_a1, "needle")
    o_a2 = find_offset(text_a2, "needle")
    o_b1 = find_offset(text_b1, "needle")
    # All three hits have identical timestamps. The deterministic sort key is
    # (session_id asc, line_number asc) after timestamp-desc primary. Context
    # is the message's intra-file neighbors only (no cross-file context).
    hit_a1 = hit(
        session_id="sid_a", cwd_slug=scenario, line_number=1,
        timestamp=iso(t), role="assistant", snippet=text_a1,
        match_offsets=[[o_a1[0], o_a1[1]]],
        context_before=[],
        context_after=[ctx("assistant", text_a2, iso(t))],
    )
    hit_a2 = hit(
        session_id="sid_a", cwd_slug=scenario, line_number=2,
        timestamp=iso(t), role="assistant", snippet=text_a2,
        match_offsets=[[o_a2[0], o_a2[1]]],
        context_before=[ctx("assistant", text_a1, iso(t))],
        context_after=[],
    )
    hit_b1 = hit(
        session_id="sid_b", cwd_slug=scenario, line_number=1,
        timestamp=iso(t), role="assistant", snippet=text_b1,
        match_offsets=[[o_b1[0], o_b1[1]]],
        context_before=[],
        context_after=[],
    )
    expected = {
        "tiebreak": {
            "description": "Same timestamp across hits: sort by session_id asc, then line_number asc.",
            "pattern": "needle",
            "flags": [],
            "hits": [hit_a1, hit_a2, hit_b1],
            "summary": summary(hits=3, sessions_matched=2),
        },
    }
    return scenario, files, expected


def multi_root_scenario_01_two_roots():
    """Same pattern in a primary-root session AND an extra-root session.

    Regression guard for cpp search: the perf-pass-2 rewrite dropped root
    resolution, so cpp ignored --extra-projects-root / walker-roots.json and
    hardcoded roots_walked=1. rust/go/zig kept it. This asserts both roots are
    walked (roots_walked=2) and that hits aggregate + sort newest-first ACROSS
    roots. Layout: <scenario>/<root>/<slug>/<sid>.jsonl.

    Returns (scenario, primary_root, extra_roots, files, combos)."""
    scenario = "01-two-roots"
    t_primary = NOW_UNIX - 2000
    t_extra = NOW_UNIX - 1000  # newer -> sorts ahead of the primary-root hit
    text_p = "primary root mentions needle once"
    text_e = "extra root also has needle here"
    files = {
        "primary/proj-primary/sid_p.jsonl": [assistant_text(t_primary, "msg_mr_p", text_p)],
        "extra/proj-extra/sid_e.jsonl": [assistant_text(t_extra, "msg_mr_e", text_e)],
        # Root-LEVEL stray file (slug position): discovery must skip a
        # non-directory entry where a slug dir is expected.
        "primary/stray.txt": ["needle inside a stray root-level file"],
    }
    o_p = find_offset(text_p, "needle")
    o_e = find_offset(text_e, "needle")
    hit_e = hit(
        session_id="sid_e", cwd_slug="proj-extra", line_number=1,
        timestamp=iso(t_extra), role="assistant", snippet=text_e,
        match_offsets=[[o_e[0], o_e[1]]],
    )
    hit_p = hit(
        session_id="sid_p", cwd_slug="proj-primary", line_number=1,
        timestamp=iso(t_primary), role="assistant", snippet=text_p,
        match_offsets=[[o_p[0], o_p[1]]],
    )
    combos = {
        "default": {
            "description": "Pattern in both roots: 2 hits newest-first, roots_walked=2.",
            "pattern": "needle",
            "flags": [],
            "hits": [hit_e, hit_p],
            "summary": summary(hits=2, sessions_matched=2, roots_walked=2),
        },
    }
    return scenario, "primary", ["extra"], files, combos


def scenario_17_queue_operation():
    """Queued-while-busy input via `type: queue-operation` entries.

    Default search ignores queue-ops entirely (regression guard).
    --include-queue-ops indexes the content-bearing enqueues as role:user,
    reading the root-level `content` field; remove/dequeue carry no content and
    are skipped naturally. No task-notification filtering: the <task-notification>
    enqueue is included too. --role assistant excludes queue-ops even with the
    flag on (they count as role:user).

    Coverage-bearing edge entries — every queue-op skip/edge arm is exercised
    so the new code carries no uncovered lines under the coverage gate:
      * remove/dequeue        -> missing-`content` skip arm
      * empty-string content  -> the `content == ""` skip arm
      * non-string content    -> go's `Unmarshal err` skip arm (rust/cpp/zig
                                 fold this into the missing-field arm)
      * content but NO timestamp -> the empty-timestamp arm; surfaces as a hit
                                 with timestamp "" that sorts last, per
                                 scenario 13's no-ts precedent.
    """
    scenario = "17-queue-operation"
    t1, t2, t3, t4, t5, t6, t7 = (
        NOW_UNIX - 5000,
        NOW_UNIX - 4000,
        NOW_UNIX - 3000,
        NOW_UNIX - 2000,
        NOW_UNIX - 1000,
        NOW_UNIX - 900,
        NOW_UNIX - 800,
    )
    a1 = "assistant mentions needle in reply"
    enqueue_plain = "please also check the needle while you work"
    enqueue_notif = "<task-notification>background needle task done</task-notification>"
    enqueue_no_ts = "queued needle with no timestamp"
    files = {
        "sid1.jsonl": [
            assistant_text(t1, "msg_17_a1", a1),
            queue_operation(t2, "enqueue", enqueue_plain),
            queue_operation(t3, "enqueue", enqueue_notif),
            queue_operation(t4, "remove"),                    # no content -> skip
            queue_operation(t5, "dequeue"),                   # no content -> skip
            queue_operation(t6, "enqueue", ""),               # empty content -> skip
            queue_operation(t7, "enqueue", 42),               # non-string content -> skip
            queue_operation(None, "popAll", enqueue_no_ts),   # no timestamp -> hit, ts ""
        ],
    }
    oa1 = find_offset(a1, "needle")
    op = find_offset(enqueue_plain, "needle")
    on = find_offset(enqueue_notif, "needle")
    ots = find_offset(enqueue_no_ts, "needle")

    # --- default combo: queue-ops invisible, only the assistant message scans.
    hit_a1_default = hit(
        session_id="sid1", cwd_slug=scenario, line_number=1,
        timestamp=iso(t1), role="assistant", snippet=a1,
        match_offsets=[[oa1[0], oa1[1]]],
        context_before=[],
        context_after=[],
    )

    # --- include-queue-ops combo: the scanned (ScanMessage) list is, in FILE
    # order, only the content-bearing entries:
    #   [assistant(line1), enqueue_plain(line2), enqueue_notif(line3),
    #    popAll-no-ts(line8)]
    # remove/dequeue (no content), the empty-string enqueue, and the non-string
    # enqueue produce NO ScanMessage. Context turns are positional over THIS
    # list (default context = 1 neighbour each side). Newest-first by timestamp;
    # the no-ts entry has timestamp "" and sorts last (empty < any RFC3339).
    hit_notif = hit(
        session_id="sid1", cwd_slug=scenario, line_number=3,
        timestamp=iso(t3), role="user", snippet=enqueue_notif,
        match_offsets=[[on[0], on[1]]],
        context_before=[ctx("user", enqueue_plain, iso(t2))],
        context_after=[ctx("user", enqueue_no_ts, "")],
    )
    hit_plain = hit(
        session_id="sid1", cwd_slug=scenario, line_number=2,
        timestamp=iso(t2), role="user", snippet=enqueue_plain,
        match_offsets=[[op[0], op[1]]],
        context_before=[ctx("assistant", a1, iso(t1))],
        context_after=[ctx("user", enqueue_notif, iso(t3))],
    )
    hit_a1_with_q = hit(
        session_id="sid1", cwd_slug=scenario, line_number=1,
        timestamp=iso(t1), role="assistant", snippet=a1,
        match_offsets=[[oa1[0], oa1[1]]],
        context_before=[],
        context_after=[ctx("user", enqueue_plain, iso(t2))],
    )
    hit_no_ts = hit(
        session_id="sid1", cwd_slug=scenario, line_number=8,
        timestamp="", role="user", snippet=enqueue_no_ts,
        match_offsets=[[ots[0], ots[1]]],
        context_before=[ctx("user", enqueue_notif, iso(t3))],
        context_after=[],
    )

    expected = {
        "default": {
            "description": "No flag: queue-ops invisible, only the assistant message hits.",
            "pattern": "needle",
            "flags": [],
            "hits": [hit_a1_default],
            "summary": summary(hits=1, sessions_matched=1),
        },
        "include-queue-ops": {
            "description": "--include-queue-ops: the content-bearing enqueues "
                           "index as role:user (incl. the task-notification, no "
                           "filtering); remove/dequeue/empty/non-string skipped. "
                           "The no-timestamp popAll surfaces with ts \"\" and "
                           "sorts last. Newest first.",
            "pattern": "needle",
            "flags": ["--include-queue-ops"],
            "hits": [hit_notif, hit_plain, hit_a1_with_q, hit_no_ts],
            "summary": summary(hits=4, sessions_matched=1),
        },
        "include-queue-ops-role-assistant": {
            "description": "--include-queue-ops --role assistant: queue-ops are "
                           "role:user, so only the assistant message emits a hit "
                           "(its positional context still spans the enqueue, per "
                           "the role-filter precedent).",
            "pattern": "needle",
            "flags": ["--include-queue-ops", "--role", "assistant"],
            "hits": [hit_a1_with_q],
            "summary": summary(hits=1, sessions_matched=1),
        },
    }
    return scenario, files, expected


def scenario_18_subagent_traversal():
    """Search must walk subagent transcripts
    (`<slug>/<session>/subagents/agent-*.jsonl`) alongside parents, per SPEC
    "Discovery" under `search`. A subagent hit reports session_id = the
    enclosing session directory name (its parent session), so both hits here
    share session_id and sessions_matched stays 1. The subagent message is
    newer, so it sorts first.
    """
    scenario = "18-subagent-traversal"
    t_parent, t_sub = NOW_UNIX - 2000, NOW_UNIX - 1000
    text_parent = "parent transcript mentions needle here"
    text_sub = "subagent transcript found the needle too"
    files = {
        "sid_parent.jsonl": [assistant_text(t_parent, "msg_18_p1", text_parent)],
        "sid_parent/subagents/agent-sub1.jsonl": [
            assistant_text(t_sub, "msg_18_s1", text_sub)
        ],
    }
    o_p = find_offset(text_parent, "needle")
    o_s = find_offset(text_sub, "needle")
    hit_sub = hit(
        session_id="sid_parent", cwd_slug=scenario, line_number=1,
        timestamp=iso(t_sub), role="assistant", snippet=text_sub,
        match_offsets=[[o_s[0], o_s[1]]],
    )
    hit_parent = hit(
        session_id="sid_parent", cwd_slug=scenario, line_number=1,
        timestamp=iso(t_parent), role="assistant", snippet=text_parent,
        match_offsets=[[o_p[0], o_p[1]]],
    )
    expected = {
        "default": {
            "description": "Subagent transcript is searched; its hit carries the "
                           "parent session's id, so sessions_matched stays 1.",
            "pattern": "needle",
            "flags": [],
            "hits": [hit_sub, hit_parent],
            "summary": summary(hits=2, sessions_matched=1),
        },
    }
    return scenario, files, expected


def scenario_19_prefilter_edges():
    """Literal-prefilter edge shapes (raw-byte skip before JSON parsing):
    a file with no occurrence of the pattern's first byte, a file whose only
    first-byte occurrence sits in the final bytes (too close to EOF to fit
    the pattern), an empty file (shorter than any pattern), a file carrying
    fold-hazard lead bytes (0xC5/0xE2 from LONG S / KELVIN-adjacent chars)
    that must defeat the skip when the pattern contains k/s, and a pattern
    starting with a non-letter byte."""
    scenario = "19-prefilter-edges"
    t1, t2, t3, t4 = (NOW_UNIX - 4000, NOW_UNIX - 3000,
                      NOW_UNIX - 2000, NOW_UNIX - 1000)
    text_hit = "report: zebra count five"
    text_digits = "code 7zulu engaged"
    text_miss = "nothing interesting here at all"
    text_tail = "tail one lacks the pattern"
    text_tail2 = "tail two lacks the pattern as well"
    text_hazard = "Łukasz noted … in the margin"
    files = {
        "hit.jsonl": [assistant_text(t4, "msg_19_hit", text_hit)],
        # CRLF-terminated entry: the scanner must strip the trailing CR and
        # still index the line (the digit-lead combo depends on this hit).
        "digits.jsonl": [
            json.dumps(assistant_text(t3, "msg_19_dig", text_digits)) + "\r",
        ],
        "miss.jsonl": [assistant_text(t2, "msg_19_miss", text_miss)],
        # Junk shapes the scanner must skip: blank line, invalid UTF-8,
        # message as a bare string, role-less / empty-role / content-less
        # messages. The trailing malformed line ends the FILE with the byte
        # 'z' -- the only 'z' in the file, too close to EOF for "zebra" to
        # fit (candidate found beyond the last viable start).
        "tail.jsonl": [assistant_text(t1, "msg_19_tail", text_tail),
                       "",
                       b'\xff\xfe not utf-8',
                       '{"message": "bare", "timestamp": "x"}',
                       '{"message": {"content": [{"type": "text", '
                       '"text": "no role"}]}}',
                       '{"message": {"role": "", "content": []}}',
                       '{"message": {"role": "user"}}',
                       "garbagez"],
        # Variant where the final 'z' sits EXACTLY at the last viable match
        # start -- the candidate is window-compared, fails, and the scan
        # advances past the end (the loop-exhausted return).
        "tail2.jsonl": [assistant_text(t1, "msg_19_tail2", text_tail2),
                        "zqqq"],
        "hazard.jsonl": [assistant_text(t1, "msg_19_haz", text_hazard)],
        "tiny.jsonl": [],
    }
    o_hit = find_offset(text_hit, "zebra")
    o_dig = find_offset(text_digits, "7zulu")
    combos = {
        "insensitive-one-hit": {
            "description": "Pattern present in one file; the rest are "
                           "prefilter-skipped or parsed without a match.",
            "pattern": "zebra",
            "flags": [],
            "hits": [hit(
                session_id="hit", cwd_slug=scenario, line_number=1,
                timestamp=iso(t4), role="assistant", snippet=text_hit,
                match_offsets=[[o_hit[0], o_hit[1]]],
            )],
            "summary": summary(hits=1, sessions_matched=1),
        },
        "fold-hazard-zero": {
            "description": "k/s pattern absent everywhere; the hazard file's "
                           "0xC5/0xE2 lead bytes force a full parse anyway.",
            "pattern": "kelvins",
            "flags": [],
            "hits": [],
            "summary": summary(hits=0, sessions_matched=0),
        },
        "digit-lead": {
            "description": "Pattern starting with a non-letter byte.",
            "pattern": "7zulu",
            "flags": [],
            "hits": [hit(
                session_id="digits", cwd_slug=scenario, line_number=1,
                timestamp=iso(t3), role="assistant", snippet=text_digits,
                match_offsets=[[o_dig[0], o_dig[1]]],
            )],
            "summary": summary(hits=1, sessions_matched=1),
        },
        "sensitive-miss": {
            "description": "Case-sensitive pattern that only differs by case "
                           "-> zero hits via the sensitive prefilter path.",
            "pattern": "Zebra",
            "flags": ["--case-sensitive"],
            "hits": [],
            "summary": summary(hits=0, sessions_matched=0),
        },
    }
    return scenario, files, combos


def scenario_20_nonascii_pattern():
    """A multibyte (non-ASCII) pattern: ineligible for the raw-byte prefilter
    and, in cpp, routed through the std::search tolower fallback instead of
    the ASCII memchr fast path. The match itself only case-folds the ASCII
    'c' (the accented byte pair is identical), so every impl agrees."""
    scenario = "20-nonascii-pattern"
    t1 = NOW_UNIX - 1000
    text = "Latte order: Café con leche, por favor"
    files = {"sid1.jsonl": [assistant_text(t1, "msg_20_a1", text)]}
    snip, offsets = snippet_and_offsets(text, "café", 240)
    combos = {
        "default": {
            "description": "Case-insensitive multibyte literal pattern.",
            "pattern": "café",
            "flags": [],
            "hits": [hit(
                session_id="sid1", cwd_slug=scenario, line_number=1,
                timestamp=iso(t1), role="assistant", snippet=snip,
                match_offsets=offsets,
            )],
            "summary": summary(hits=1, sessions_matched=1),
        },
    }
    return scenario, files, combos


def scenario_21_snippet_escape_chars():
    """Matched text packed with characters the JSONL hit writer must escape:
    quote, backslash, backspace, formfeed, carriage return, tab, and a raw
    control byte. Comparison happens on PARSED output, so this asserts the
    escapes are correct, not their textual form."""
    scenario = "21-snippet-escape-chars"
    t1 = NOW_UNIX - 1000
    text = ('alert "quoted" back\\slash bs\bspot form\ffeed '
            'carriage\rreturn tab\there ctl\x01dot needle end')
    files = {"sid1.jsonl": [assistant_text(t1, "msg_21_a1", text)]}
    o = find_offset(text, "needle")
    combos = {
        "default": {
            "description": "Control/escape characters in the snippet "
                           "round-trip through the JSON writer.",
            "pattern": "needle",
            "flags": [],
            "hits": [hit(
                session_id="sid1", cwd_slug=scenario, line_number=1,
                timestamp=iso(t1), role="assistant", snippet=text,
                match_offsets=[[o[0], o[1]]],
            )],
            "summary": summary(hits=1, sessions_matched=1),
        },
    }
    return scenario, files, combos


def scenario_22_regex_fold_range():
    """Case-INSENSITIVE regex char class with a range: [a-d]+ must fold both
    cases in every engine (zig expands the class to both cases; the others
    compile with their icase flag)."""
    scenario = "22-regex-fold-range"
    t1 = NOW_UNIX - 1000
    text = "abCD over"
    files = {"sid1.jsonl": [assistant_text(t1, "msg_22_a1", text)]}
    combos = {
        "default": {
            "description": "Insensitive class range matches mixed case.",
            "pattern": "[a-d]+",
            "flags": ["--regex"],
            "hits": [hit(
                session_id="sid1", cwd_slug=scenario, line_number=1,
                timestamp=iso(t1), role="assistant", snippet=text,
                match_offsets=[[0, 4]],
            )],
            "summary": summary(hits=1, sessions_matched=1),
        },
    }
    return scenario, files, combos


def scenario_23_regex_fold_single():
    """Case-INSENSITIVE regex char class with plain members: [xy]+ folds
    single (non-range) class members in every engine."""
    scenario = "23-regex-fold-single"
    t1 = NOW_UNIX - 1000
    text = "yX marks"
    files = {"sid1.jsonl": [assistant_text(t1, "msg_23_a1", text)]}
    combos = {
        "default": {
            "description": "Insensitive class members match both cases.",
            "pattern": "[xy]+",
            "flags": ["--regex"],
            "hits": [hit(
                session_id="sid1", cwd_slug=scenario, line_number=1,
                timestamp=iso(t1), role="assistant", snippet=text,
                match_offsets=[[0, 2]],
            )],
            "summary": summary(hits=1, sessions_matched=1),
        },
    }
    return scenario, files, combos


def scenario_24_snippet_no_whitespace():
    """Snippet cuts inside long unbroken character runs: the whitespace nudge
    scans 20 bytes in each direction, finds none, and must keep the raw cut
    (the nudge helper's fall-through return)."""
    scenario = "24-snippet-no-whitespace"
    t1 = NOW_UNIX - 1000
    text = "a" * 150 + " needle " + "b" * 150
    files = {"sid1.jsonl": [assistant_text(t1, "msg_24_a1", text)]}
    snip, offsets = snippet_and_offsets(text, "needle", 240)
    combos = {
        "default": {
            "description": "Cuts land mid-run; no whitespace within the "
                           "20-byte nudge window on either side.",
            "pattern": "needle",
            "flags": [],
            "hits": [hit(
                session_id="sid1", cwd_slug=scenario, line_number=1,
                timestamp=iso(t1), role="assistant", snippet=snip,
                match_offsets=offsets,
            )],
            "summary": summary(hits=1, sessions_matched=1),
        },
    }
    return scenario, files, combos


def scenario_25_context_rich():
    """--context 2 around a single hit, with context-turn texts longer than
    120 chars: exercises multi-entry context arrays (the JSONL comma arm) and
    the pretty renderer's long-context ellipsis truncation."""
    scenario = "25-context-rich"
    t1, t2, t3, t4, t5 = (NOW_UNIX - 5000, NOW_UNIX - 4000, NOW_UNIX - 3000,
                          NOW_UNIX - 2000, NOW_UNIX - 1000)
    ctx1 = "context turn one " + "alpha " * 25   # > 120 chars
    ctx2 = "context turn two " + "bravo " * 25
    ctx4 = "context turn four " + "delta " * 25
    ctx5 = "context turn five " + "echo " * 30
    text_hit = "the needle sits in the middle turn"
    files = {
        "sid1.jsonl": [
            assistant_text(t1, "msg_25_a1", ctx1),
            assistant_text(t2, "msg_25_a2", ctx2),
            assistant_text(t3, "msg_25_a3", text_hit),
            assistant_text(t4, "msg_25_a4", ctx4),
            assistant_text(t5, "msg_25_a5", ctx5),
        ],
    }
    o = find_offset(text_hit, "needle")
    combos = {
        "default": {
            "description": "Two long context turns on each side of one hit.",
            "pattern": "needle",
            "flags": ["--context", "2"],
            "hits": [hit(
                session_id="sid1", cwd_slug=scenario, line_number=3,
                timestamp=iso(t3), role="assistant", snippet=text_hit,
                match_offsets=[[o[0], o[1]]],
                context_before=[ctx("assistant", ctx1, iso(t1)),
                                ctx("assistant", ctx2, iso(t2))],
                context_after=[ctx("assistant", ctx4, iso(t4)),
                               ctx("assistant", ctx5, iso(t5))],
            )],
            "summary": summary(hits=1, sessions_matched=1),
        },
    }
    return scenario, files, combos


def multi_root_scenario_02_cwd_filter():
    """--cwd over a primary root holding TWO slug dirs: the filter must skip
    the non-matching slug during discovery (the single-slug scenarios never
    exercise the mismatch arm). Returns the multi-root layout with no extra
    roots."""
    scenario = "02-cwd-filter"
    t_one, t_two = NOW_UNIX - 2000, NOW_UNIX - 1000
    text_one = "needle in slug one"
    text_two = "needle in slug two"
    files = {
        "primary/slug-one/sid1.jsonl": [assistant_text(t_one, "msg_cwd_1", text_one)],
        "primary/slug-two/sid2.jsonl": [assistant_text(t_two, "msg_cwd_2", text_two)],
    }
    o1 = find_offset(text_one, "needle")
    o2 = find_offset(text_two, "needle")
    hit_one = hit(
        session_id="sid1", cwd_slug="slug-one", line_number=1,
        timestamp=iso(t_one), role="assistant", snippet=text_one,
        match_offsets=[[o1[0], o1[1]]],
    )
    hit_two = hit(
        session_id="sid2", cwd_slug="slug-two", line_number=1,
        timestamp=iso(t_two), role="assistant", snippet=text_two,
        match_offsets=[[o2[0], o2[1]]],
    )
    combos = {
        "no-filter": {
            "description": "Both slugs match without --cwd.",
            "pattern": "needle",
            "flags": [],
            "hits": [hit_two, hit_one],
            "summary": summary(hits=2, sessions_matched=2),
        },
        "cwd-one": {
            "description": "--cwd slug-one drops the other slug at discovery.",
            "pattern": "needle",
            "flags": ["--cwd", "slug-one"],
            "hits": [hit_one],
            "summary": summary(hits=1, sessions_matched=1),
        },
    }
    return scenario, "primary", [], files, combos


def scenario_26_dirty_with_pattern():
    """A transcript whose DIRTY lines carry the pattern bytes, so the
    literal prefilter cannot skip the file and the per-line skip ladder runs
    with the pattern in play: blank line, invalid UTF-8 containing the
    pattern, malformed JSON containing the pattern, message-as-bare-string,
    empty role, and content-less messages. Only the one valid line hits.
    The regex combo disables the prefilter entirely, driving the same ladder
    through the regex scan path."""
    scenario = "26-dirty-with-pattern"
    t1 = NOW_UNIX - 1000
    text_hit = "the only real dirtneedle line in this file"
    files = {
        "dirty.jsonl": [
            assistant_text(t1, "msg_26_hit", text_hit),
            "",
            b"\xff\xfe dirtneedle inside invalid utf-8",
            '{"bad": dirtneedle',
            '{"message": "bare", "x": "dirtneedle"}',
            '{"message": {"role": "", "content": [], "y": "dirtneedle"}}',
            '{"message": {"role": "user", "z": "dirtneedle"}}',
        ],
    }
    snip, offsets = snippet_and_offsets(text_hit, "dirtneedle", 240)
    the_hit = hit(
        session_id="dirty", cwd_slug=scenario, line_number=1,
        timestamp=iso(t1), role="assistant", snippet=snip,
        match_offsets=offsets,
    )
    combos = {
        "default": {
            "description": "Substring scan over a prefilter-defeating dirty file.",
            "pattern": "dirtneedle",
            "flags": [],
            "hits": [the_hit],
            "summary": summary(hits=1, sessions_matched=1),
        },
        "regex": {
            "description": "Same ladder via the regex path (no prefilter).",
            "pattern": "dirtneedl[e]",
            "flags": ["--regex"],
            "hits": [the_hit],
            "summary": summary(hits=1, sessions_matched=1),
        },
    }
    return scenario, files, combos


def scenario_27_padded_time_args():
    """`--since`/`--until` values with surrounding whitespace must be trimmed
    before parsing (every impl trims; cpp does so byte-by-byte)."""
    scenario = "27-padded-time-args"
    t1 = NOW_UNIX - 1000
    text = "padded time argument padneedle survives trimming"
    files = {"sid1.jsonl": [assistant_text(t1, "msg_27_a1", text)]}
    o = find_offset(text, "padneedle")
    the_hit = hit(
        session_id="sid1", cwd_slug=scenario, line_number=1,
        timestamp=iso(t1), role="assistant", snippet=text,
        match_offsets=[[o[0], o[1]]],
    )
    combos = {
        "leading-space-since": {
            "description": "--since value with a leading space.",
            "pattern": "padneedle",
            "flags": ["--since", " 7d"],
            "hits": [the_hit],
            "summary": summary(hits=1, sessions_matched=1),
        },
        "trailing-space-until": {
            "description": "--until value with a trailing space.",
            "pattern": "padneedle",
            "flags": ["--since", "7d ", "--until", " 1s "],
            "hits": [the_hit],
            "summary": summary(hits=1, sessions_matched=1),
        },
    }
    return scenario, files, combos


def scenario_28_regex_backtrack():
    """A greedy `+` quantifier that must backtrack: in "rally" the engine
    consumes both l's, fails on the trailing literal, backtracks to one l,
    fails again, and gives up; in "totallx" the same pattern succeeds. Zig's
    hand-rolled engine walks an explicit end-decrement loop here; the other
    impls route through their regex libraries and must agree on the match."""
    scenario = "28-regex-backtrack"
    t1 = NOW_UNIX - 1000
    text = "they rally around totallx results"
    files = {"sid1.jsonl": [assistant_text(t1, "msg_28_a1", text)]}
    mstart = text.find("allx")
    combos = {
        "plus-backtrack": {
            "description": "Greedy + with a failing suffix forces backtracking.",
            "pattern": "al+x",
            "flags": ["--regex"],
            "hits": [hit(
                session_id="sid1", cwd_slug=scenario, line_number=1,
                timestamp=iso(t1), role="assistant", snippet=text,
                match_offsets=[[mstart, mstart + 4]],
            )],
            "summary": summary(hits=1, sessions_matched=1),
        },
    }
    return scenario, files, combos


def scenario_29_discovery_oddities():
    """Discovery-layer oddities inside a slug: a session directory WITHOUT a
    subagents/ child, a subagents/ dir containing a non-agent-named file and
    a stray subdirectory, and a non-.jsonl file at the slug level. The
    walker must skip them all silently and still find the parent + subagent
    hits."""
    scenario = "29-discovery-oddities"
    t_parent, t_sub = NOW_UNIX - 2000, NOW_UNIX - 1000
    text_parent = "parent oddneedle line"
    text_sub = "subagent oddneedle line"
    files = {
        "main.jsonl": [assistant_text(t_parent, "msg_29_p1", text_parent)],
        "main/subagents/agent-ok.jsonl": [
            assistant_text(t_sub, "msg_29_s1", text_sub)
        ],
        # Skipped: bad name in subagents, stray dir in subagents, session
        # dir without subagents, non-.jsonl file at slug level.
        "main/subagents/notanagent.txt": ["oddneedle in a non-agent file"],
        "main/subagents/straydir/notes.txt": ["oddneedle in a stray dir"],
        "sess-no-subagents/notes.txt": ["oddneedle in a bare session dir"],
        "README.txt": ["oddneedle in a non-jsonl slug file"],
    }
    o_p = find_offset(text_parent, "oddneedle")
    o_s = find_offset(text_sub, "oddneedle")
    combos = {
        "default": {
            "description": "Oddity entries are skipped; real hits survive.",
            "pattern": "oddneedle",
            "flags": [],
            "hits": [
                hit(session_id="main", cwd_slug=scenario, line_number=1,
                    timestamp=iso(t_sub), role="assistant", snippet=text_sub,
                    match_offsets=[[o_s[0], o_s[1]]]),
                hit(session_id="main", cwd_slug=scenario, line_number=1,
                    timestamp=iso(t_parent), role="assistant",
                    snippet=text_parent,
                    match_offsets=[[o_p[0], o_p[1]]]),
            ],
            "summary": summary(hits=2, sessions_matched=1),
        },
    }
    return scenario, files, combos


SCENARIOS = [
    scenario_01_basic,
    scenario_02_multi_match_per_session,
    scenario_03_bare_string_user_content,
    scenario_04_role_filter,
    scenario_05_tool_block_skip,
    scenario_06_regex,
    scenario_07_case,
    scenario_08_time_window,
    scenario_09_count_only,
    scenario_10_context_zero,
    scenario_11_multibyte_snippet_boundary,
    scenario_12_tool_use_and_result_array,
    scenario_13_time_units_and_edges,
    scenario_14_snippet_whitespace_nudge,
    *REGEX_CLASS_SCENARIOS,
    scenario_16_same_timestamp,
    scenario_17_queue_operation,
    scenario_18_subagent_traversal,
    scenario_19_prefilter_edges,
    scenario_20_nonascii_pattern,
    scenario_21_snippet_escape_chars,
    scenario_22_regex_fold_range,
    scenario_23_regex_fold_single,
    scenario_24_snippet_no_whitespace,
    scenario_25_context_rich,
    scenario_26_dirty_with_pattern,
    scenario_27_padded_time_args,
    scenario_28_regex_backtrack,
    scenario_29_discovery_oddities,
]

MULTI_ROOT_SCENARIOS = [
    multi_root_scenario_01_two_roots,
    multi_root_scenario_02_cwd_filter,
]


def main() -> None:
    if CORPUS_SEARCH.exists():
        for p in sorted(CORPUS_SEARCH.rglob("*"), reverse=True):
            if p.is_file():
                p.unlink()
            elif p.is_dir():
                p.rmdir()
        CORPUS_SEARCH.rmdir()
    CORPUS_SEARCH.mkdir(parents=True, exist_ok=True)

    meta = {
        "now_unix": NOW_UNIX,
        "note": "Generated by generate_search_corpus.py. Do not hand-edit.",
    }

    total_files = 0
    for build in SCENARIOS:
        scenario, files, combos = build()
        for rel, lines in files.items():
            write_jsonl(CORPUS_SEARCH / scenario / rel, lines)
            total_files += 1
        expected_path = CORPUS_SEARCH / scenario / "expected.json"
        expected_path.write_text(
            json.dumps({"_meta": meta, "combos": combos}, indent=2) + "\n",
            encoding="utf-8",
        )

    # Multi-root search scenarios live in a sibling corpus dir with their own
    # layout (<scenario>/<root>/<slug>/<sid>.jsonl + an expected.json carrying
    # primary_root / extra_roots). check_search_multi_root runs
    # `walker search --projects-root <primary> --extra-projects-root <extra>`.
    if CORPUS_SEARCH_MULTI_ROOT.exists():
        for p in sorted(CORPUS_SEARCH_MULTI_ROOT.rglob("*"), reverse=True):
            if p.is_file():
                p.unlink()
            elif p.is_dir():
                p.rmdir()
        CORPUS_SEARCH_MULTI_ROOT.rmdir()
    CORPUS_SEARCH_MULTI_ROOT.mkdir(parents=True, exist_ok=True)

    mr_files = 0
    for build in MULTI_ROOT_SCENARIOS:
        scenario, primary_root, extra_roots, files, combos = build()
        for rel, lines in files.items():
            write_jsonl(CORPUS_SEARCH_MULTI_ROOT / scenario / rel, lines)
            mr_files += 1
        expected_path = CORPUS_SEARCH_MULTI_ROOT / scenario / "expected.json"
        expected_path.write_text(
            json.dumps(
                {
                    "_meta": meta,
                    "primary_root": primary_root,
                    "extra_roots": extra_roots,
                    "combos": combos,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    print(f"Wrote {total_files} JSONL fixtures across {len(SCENARIOS)} scenarios")
    print(f"  under {CORPUS_SEARCH}")
    print(f"Wrote {mr_files} JSONL fixtures across {len(MULTI_ROOT_SCENARIOS)} multi-root scenarios")
    print(f"  under {CORPUS_SEARCH_MULTI_ROOT}")

    if CORPUS_SEARCH_CODEX.exists():
        for p in sorted(CORPUS_SEARCH_CODEX.rglob("*"), reverse=True):
            if p.is_file():
                p.unlink()
            elif p.is_dir():
                p.rmdir()
        CORPUS_SEARCH_CODEX.rmdir()

    codex_cwd = "/worktrees/codex-project"
    other_cwd = "/worktrees/other-project"
    session_id = "019fb6bc-1111-7222-8333-444444444444"
    other_session_id = "019fb6bc-aaaa-7bbb-8ccc-dddddddddddd"
    codex_files = {
        "2026/05/09/rollout-2026-05-09T11-00-00-"
        f"{session_id}.jsonl": [
            codex_session_meta(NOW_UNIX - 3600, session_id, codex_cwd),
            codex_event(NOW_UNIX - 3500, "user_message", "codexneedle from user"),
            codex_event(NOW_UNIX - 3400, "token_count", "codexneedle ignored"),
            codex_event(NOW_UNIX - 3300, "agent_message", "codexneedle from agent"),
            codex_event(NOW_UNIX - 3200, "agent_message", {"not": "text"}),
            {"timestamp": iso(NOW_UNIX - 3100), "type": "event_msg"},
        ],
        "2026/05/09/rollout-2026-05-09T10-00-00-"
        f"{other_session_id}.jsonl": [
            {"timestamp": iso(NOW_UNIX - 7200), "type": "session_meta",
             "payload": {"id": other_session_id, "cwd": other_cwd}},
            codex_event(NOW_UNIX - 7100, "agent_message", "codexneedle other cwd"),
        ],
        "2026/05/09/not-a-rollout.jsonl": [
            codex_session_meta(NOW_UNIX - 100, "ignored", codex_cwd),
            codex_event(NOW_UNIX - 90, "agent_message", "codexneedle ignored filename"),
        ],
        "2026/05/09/rollout-empty.jsonl": [],
        "2026/05/09/rollout-invalid-utf8.jsonl": [b"\xff"],
        "2026/05/09/rollout-malformed.jsonl": ["{bad json"],
        "2026/05/09/rollout-wrong-type.jsonl": [{"type": "event_msg"}],
        "2026/05/09/rollout-no-payload.jsonl": [{"type": "session_meta"}],
        "2026/05/09/rollout-no-cwd.jsonl": [
            {"type": "session_meta", "payload": {"session_id": "no-cwd"}}
        ],
        "2026/05/09/rollout-no-session-id.jsonl": [
            {"type": "session_meta", "payload": {"cwd": codex_cwd}}
        ],
        "20x6/05/09/rollout-invalid-year.jsonl": [
            codex_session_meta(NOW_UNIX - 80, "invalid-year", codex_cwd),
            codex_event(NOW_UNIX - 70, "agent_message", "codexneedle invalid year"),
        ],
        "wrong-year/05/09/rollout-invalid-year-length.jsonl": [
            codex_session_meta(NOW_UNIX - 75, "invalid-year-length", codex_cwd),
            codex_event(
                NOW_UNIX - 65, "agent_message", "codexneedle invalid year length"
            ),
        ],
        "2026/0x/09/rollout-invalid-month.jsonl": [
            codex_session_meta(NOW_UNIX - 60, "invalid-month", codex_cwd),
            codex_event(NOW_UNIX - 50, "agent_message", "codexneedle invalid month"),
        ],
        "2026/05/0x/rollout-invalid-day.jsonl": [
            codex_session_meta(NOW_UNIX - 40, "invalid-day", codex_cwd),
            codex_event(NOW_UNIX - 30, "agent_message", "codexneedle invalid day"),
        ],
    }
    for rel, lines in codex_files.items():
        write_jsonl(CORPUS_SEARCH_CODEX / rel, lines)

    def codex_hit(session: str, cwd: str, line: int, timestamp: float,
                  role: str, text: str) -> dict:
        start = text.index("codexneedle")
        return hit(
            session_id=session, cwd_slug=cwd, line_number=line,
            timestamp=iso(timestamp), role=role, snippet=text,
            match_offsets=[[start, start + len("codexneedle")]],
        )

    codex_expected = {
        "_meta": meta,
        "pattern": "codexneedle",
        "cwd": codex_cwd,
        "all": {
            "hits": [
                codex_hit(session_id, codex_cwd, 4, NOW_UNIX - 3300,
                          "assistant", "codexneedle from agent"),
                codex_hit(session_id, codex_cwd, 2, NOW_UNIX - 3500,
                          "user", "codexneedle from user"),
                codex_hit(other_session_id, other_cwd, 2, NOW_UNIX - 7100,
                          "assistant", "codexneedle other cwd"),
            ],
            "summary": summary(hits=3, sessions_matched=2, roots_walked=2),
        },
        "cwd_filtered": {
            "hits": [
                codex_hit(session_id, codex_cwd, 4, NOW_UNIX - 3300,
                          "assistant", "codexneedle from agent"),
                codex_hit(session_id, codex_cwd, 2, NOW_UNIX - 3500,
                          "user", "codexneedle from user"),
            ],
            "summary": summary(hits=2, sessions_matched=1, roots_walked=2),
        },
    }
    (CORPUS_SEARCH_CODEX / "expected.json").write_text(
        json.dumps(codex_expected, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Wrote codex rollout fixtures under {CORPUS_SEARCH_CODEX}")


if __name__ == "__main__":
    main()
