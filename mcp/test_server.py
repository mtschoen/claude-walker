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
    monkeypatch.setattr(server, "run_captured",
                        lambda *a, **k: _fake_result(0, None, "trunc hint"))
    with pytest.raises(RuntimeError) as excinfo:
        server._run_search(["anything"])
    assert "no output" in str(excinfo.value).lower()
    assert "trunc hint" in str(excinfo.value)


def test_empty_stdout_on_clean_exit_raises(monkeypatch):
    monkeypatch.setattr(server, "run_captured",
                        lambda *a, **k: _fake_result(0, "   \n", ""))
    with pytest.raises(RuntimeError):
        server._run_search(["anything"])


def test_parses_hits_and_summary(monkeypatch):
    stdout = (
        '{"type": "hit", "session_id": "s1", "snippet": "found it"}\n'
        '{"type": "summary", "hits": 1, "truncated": false}\n'
    )
    monkeypatch.setattr(server, "run_captured",
                        lambda *a, **k: _fake_result(0, stdout, "narrow with --since"))
    result = server._run_search(["pattern"])
    assert len(result["hits"]) == 1
    assert result["hits"][0]["snippet"] == "found it"
    assert result["summary"]["hits"] == 1
    assert result["note"] == "narrow with --since"


def test_nonzero_exit_raises_with_stderr(monkeypatch):
    monkeypatch.setattr(server, "run_captured",
                        lambda *a, **k: _fake_result(2, "", "bad regex"))
    with pytest.raises(RuntimeError) as excinfo:
        server._run_search(["("])
    assert "exit 2" in str(excinfo.value)
    assert "bad regex" in str(excinfo.value)


def test_no_hits_still_succeeds_via_summary_line(monkeypatch):
    # A real "zero matches" run still emits a summary line, so it must NOT be
    # treated as the empty-stdout failure case.
    monkeypatch.setattr(server, "run_captured",
                        lambda *a, **k: _fake_result(0, '{"type": "summary", "hits": 0}\n', ""))
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
