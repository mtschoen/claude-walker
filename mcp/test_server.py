"""Unit tests for the FastMCP shim's _run_search reshaping.

Run from the repo root:
    uv run --python 3.13 --with mcp --with pytest python -m pytest mcp/test_server.py

These exercise the parse/guard logic in isolation by monkeypatching
server.run_captured and _resolve_binary — no native binary or live sessions
needed. Regression coverage for BUG-mcp-search-stdout-none.md.

_run_search shells out via process_safe.run_captured (see server.py's
docstring on that call), not bare subprocess.run, so the fakes here return a
process_safe.ProcessResult and raise process_safe.ProcessTimeout, matching
that module's contract.
"""

from pathlib import Path

import pytest

# Import server first: it inserts shared/ (where process_safe.py lives) onto
# sys.path as a side effect of import, since this file's own directory
# (mcp/) is what pytest/script-launch puts there by default.
import server
from process_safe import ProcessResult, ProcessTimeout


def _fake_result(returncode=0, stdout="", stderr=""):
    return ProcessResult(returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture(autouse=True)
def _stub_binary(monkeypatch):
    monkeypatch.setattr(server, "_resolve_binary", lambda: Path("walker"))


def test_stdout_none_raises_runtimeerror_not_attributeerror(monkeypatch):
    # The original bug: stdout came back None on a clean exit and the shim
    # did completed.stdout.splitlines() -> AttributeError. Must now raise a
    # descriptive RuntimeError instead.
    monkeypatch.setattr(
        server, "run_captured", lambda *a, **k: _fake_result(0, None, "trunc hint")
    )
    with pytest.raises(RuntimeError) as excinfo:
        server._run_search(["anything"])
    assert "no output" in str(excinfo.value).lower()
    assert "trunc hint" in str(excinfo.value)


def test_empty_stdout_on_clean_exit_raises(monkeypatch):
    monkeypatch.setattr(
        server, "run_captured", lambda *a, **k: _fake_result(0, "   \n", "")
    )
    with pytest.raises(RuntimeError):
        server._run_search(["anything"])


def test_parses_hits_and_summary(monkeypatch):
    stdout = (
        '{"type": "hit", "session_id": "s1", "snippet": "found it"}\n'
        '{"type": "summary", "hits": 1, "truncated": false}\n'
    )
    monkeypatch.setattr(
        server,
        "run_captured",
        lambda *a, **k: _fake_result(0, stdout, "narrow with --since"),
    )
    result = server._run_search(["pattern"])
    assert len(result["hits"]) == 1
    assert result["hits"][0]["snippet"] == "found it"
    assert result["summary"]["hits"] == 1
    assert result["note"] == "narrow with --since"


def test_nonzero_exit_raises_with_stderr(monkeypatch):
    monkeypatch.setattr(
        server, "run_captured", lambda *a, **k: _fake_result(2, "", "bad regex")
    )
    with pytest.raises(RuntimeError) as excinfo:
        server._run_search(["("])
    assert "exit 2" in str(excinfo.value)
    assert "bad regex" in str(excinfo.value)


def test_no_hits_still_succeeds_via_summary_line(monkeypatch):
    # A real "zero matches" run still emits a summary line, so it must NOT be
    # treated as the empty-stdout failure case.
    monkeypatch.setattr(
        server,
        "run_captured",
        lambda *a, **k: _fake_result(0, '{"type": "summary", "hits": 0}\n', ""),
    )
    result = server._run_search(["pattern"])
    assert result["hits"] == []
    assert result["summary"]["hits"] == 0
    assert result["note"] is None


def test_timeout_raises_runtimeerror_not_process_timeout(monkeypatch):
    # process_safe.run_captured raises ProcessTimeout (not
    # subprocess.TimeoutExpired) when the walker binary wedges past
    # SUBPROCESS_TIMEOUT_SECONDS; _run_search must translate it into the same
    # RuntimeError contract FastMCP expects from every other failure mode,
    # not let a process_safe-specific exception type leak through.
    def _raise_timeout(*args, **kwargs):
        raise ProcessTimeout(["walker", "search"], server.SUBPROCESS_TIMEOUT_SECONDS)

    monkeypatch.setattr(server, "run_captured", _raise_timeout)
    with pytest.raises(RuntimeError) as excinfo:
        server._run_search(["anything"])
    assert "timed out" in str(excinfo.value).lower()
    assert str(server.SUBPROCESS_TIMEOUT_SECONDS) in str(excinfo.value)


def test_agent_search_merges_and_sorts_providers(monkeypatch):
    monkeypatch.setattr(
        server,
        "_run_search",
        lambda arguments: {
            "hits": [
                {
                    "session_id": "claude",
                    "timestamp": "2024-01-01T00:00:00Z",
                    "line_number": 1,
                }
            ],
            "summary": {
                "hits": 1,
                "sessions_matched": 1,
                "roots_walked": 1,
                "files_walked": 2,
                "truncated": False,
            },
            "note": None,
        },
    )
    monkeypatch.setattr(
        server,
        "search_opencode",
        lambda options: {
            "hits": [
                {
                    "provider": "opencode",
                    "session_id": "oc",
                    "timestamp": "2025-01-01T00:00:00Z",
                    "line_number": 2,
                }
            ],
            "summary": {
                "hits": 1,
                "sessions_matched": 1,
                "roots_walked": 1,
                "files_walked": 1,
                "truncated": False,
            },
            "note": None,
        },
    )
    result = server._run_agent_search(server.SearchOptions(pattern="needle"), None)
    assert [hit["provider"] for hit in result["hits"]] == ["opencode", "claude"]
    assert result["summary"]["hits"] == 2
    assert set(result["summary"]["providers"]) == {"claude", "opencode"}


def test_agent_search_degrades_when_one_provider_fails(monkeypatch):
    monkeypatch.setattr(
        server,
        "_run_search",
        lambda arguments: (_ for _ in ()).throw(RuntimeError("binary missing")),
    )
    monkeypatch.setattr(
        server,
        "search_opencode",
        lambda options: {
            "hits": [],
            "summary": {
                "hits": 0,
                "sessions_matched": 0,
                "roots_walked": 1,
                "files_walked": 1,
                "truncated": False,
            },
            "note": None,
        },
    )
    result = server._run_agent_search(server.SearchOptions(pattern="needle"), None)
    assert result["hits"] == []
    assert "claude: binary missing" in result["note"]
    assert result["summary"]["providers"]["claude"]["available"] is False
    assert result["summary"]["providers"]["claude"]["error"] == "binary missing"


def test_agent_search_raises_when_every_provider_fails(monkeypatch):
    monkeypatch.setattr(
        server,
        "_run_search",
        lambda arguments: (_ for _ in ()).throw(RuntimeError("claude failed")),
    )
    monkeypatch.setattr(
        server,
        "search_opencode",
        lambda options: (_ for _ in ()).throw(
            server.OpenCodeSearchError("opencode failed")
        ),
    )
    with pytest.raises(
        RuntimeError, match="all selected.*claude failed.*opencode failed"
    ):
        server._run_agent_search(server.SearchOptions(pattern="needle"), None)


def test_agent_search_rejects_unknown_or_empty_providers():
    options = server.SearchOptions(pattern="needle")
    with pytest.raises(ValueError, match="unknown"):
        server._run_agent_search(options, ["pi"])
    with pytest.raises(ValueError, match="empty"):
        server._run_agent_search(options, [])


def test_search_argument_builder_maps_every_option():
    options = server.SearchOptions(
        pattern="needle",
        regex=True,
        case_sensitive=True,
        role="user",
        since="7d",
        until="1h",
        cwd="project",
        context_turns=2,
        limit=3,
        count_only=True,
        include_tool_blocks=True,
    )
    assert server._search_arguments(options) == [
        "needle",
        "--regex",
        "--case-sensitive",
        "--role",
        "user",
        "--since",
        "7d",
        "--until",
        "1h",
        "--cwd",
        "project",
        "--context",
        "2",
        "--limit",
        "3",
        "--count-only",
        "--include-tool-blocks",
    ]


def test_merge_count_only_and_global_truncation():
    result = server._merge_provider_results(
        {
            "claude": {
                "hits": [
                    {"timestamp": "bad-time", "session_id": "a", "line_number": 1},
                    {
                        "timestamp": "2025-01-01T00:00:00Z",
                        "session_id": "b",
                        "line_number": 2,
                    },
                ],
                "summary": {"hits": 7, "sessions_matched": 2, "truncated": False},
                "note": "note",
            },
        },
        limit=1,
        count_only=True,
        elapsed_ms=4,
    )
    assert result["hits"] == []
    assert result["summary"]["hits"] == 7
    assert result["summary"]["truncated"] is True
    assert result["note"] == "claude: note"


def test_agent_search_validates_numeric_options():
    with pytest.raises(ValueError, match="limit"):
        server._run_agent_search(
            server.SearchOptions(pattern="x", limit=0), ["opencode"]
        )
    with pytest.raises(ValueError, match="context_turns"):
        server._run_agent_search(
            server.SearchOptions(pattern="x", context_turns=-1), ["opencode"]
        )


def test_agent_search_honors_single_provider_selection(monkeypatch):
    calls = []
    monkeypatch.setattr(
        server,
        "_run_search",
        lambda arguments: (
            calls.append("claude")
            or {
                "hits": [],
                "summary": {},
                "note": None,
            }
        ),
    )
    monkeypatch.setattr(
        server,
        "search_opencode",
        lambda options: (
            calls.append("opencode")
            or {
                "hits": [],
                "summary": {},
                "note": None,
            }
        ),
    )
    options = server.SearchOptions(pattern="x")
    server._run_agent_search(options, ["claude"])
    assert calls == ["claude"]
    calls.clear()
    server._run_agent_search(options, ["opencode"])
    assert calls == ["opencode"]


def test_mcp_tools_register_and_dispatch(monkeypatch):
    monkeypatch.setattr(server, "_write_log", lambda *args: None)
    monkeypatch.setattr(
        server,
        "_run_agent_search",
        lambda options, providers: {
            "kind": "generic",
            "pattern": options.pattern,
            "providers": providers,
        },
    )
    instance = server.create_mcp_server()
    tools = {tool.name: tool.fn for tool in instance._tool_manager.list_tools()}
    assert set(tools) == {"agent_walker_search"}
    assert tools["agent_walker_search"](pattern="find me", providers=["opencode"]) == {
        "kind": "generic",
        "pattern": "find me",
        "providers": ["opencode"],
    }
