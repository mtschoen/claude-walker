"""Tests for the read-only OpenCode SQLite search provider."""

import json
import sqlite3
from pathlib import Path

import pytest

import opencode_search
from opencode_search import OpenCodeSearchError, SearchOptions, resolve_database, search


def _json(value):
    return json.dumps(value, separators=(",", ":"))


@pytest.fixture
def opencode_db(tmp_path: Path) -> Path:
    path = tmp_path / "opencode.db"
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE session (
            id TEXT PRIMARY KEY, slug TEXT NOT NULL, directory TEXT NOT NULL,
            title TEXT NOT NULL
        );
        CREATE TABLE message (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
            time_created INTEGER NOT NULL, data TEXT NOT NULL
        );
        CREATE TABLE part (
            id TEXT PRIMARY KEY, message_id TEXT NOT NULL, session_id TEXT NOT NULL,
            time_created INTEGER NOT NULL, data TEXT NOT NULL
        );
    """)
    connection.executemany(
        "INSERT INTO session VALUES (?, ?, ?, ?)",
        [
            ("ses_old", "quiet-fox", "/work/alpha", "Alpha session"),
            ("ses_new", "bright-owl", "/work/beta", "Beta session"),
        ],
    )
    messages = [
        ("m1", "ses_old", 1_700_000_000_000, _json({"role": "user"})),
        ("m2", "ses_old", 1_700_000_001_000, _json({"role": "assistant"})),
        ("m3", "ses_new", 1_710_000_000_000, _json({"role": "user"})),
        ("m4", "ses_new", 1_710_000_001_000, _json({"role": "assistant"})),
    ]
    connection.executemany("INSERT INTO message VALUES (?, ?, ?, ?)", messages)
    parts = [
        (
            "p1",
            "m1",
            "ses_old",
            1_700_000_000_000,
            _json({"type": "text", "text": "Remember the violet lighthouse"}),
        ),
        (
            "p2",
            "m2",
            "ses_old",
            1_700_000_001_000,
            _json({"type": "text", "text": "I will remember it"}),
        ),
        (
            "p3",
            "m3",
            "ses_new",
            1_710_000_000_000,
            _json({"type": "text", "text": "VIOLET\nlighthouse appears again"}),
        ),
        (
            "p4",
            "m4",
            "ses_new",
            1_710_000_001_000,
            _json({"type": "text", "text": "No prose match here"}),
        ),
        (
            "p5",
            "m4",
            "ses_new",
            1_710_000_001_001,
            _json(
                {
                    "type": "tool",
                    "state": {
                        "input": {"query": "violet lighthouse"},
                        "output": "tool found the beacon",
                    },
                }
            ),
        ),
    ]
    connection.executemany("INSERT INTO part VALUES (?, ?, ?, ?, ?)", parts)
    connection.commit()
    connection.close()
    return path


def test_resolve_database_honors_environment(monkeypatch, opencode_db):
    monkeypatch.setenv("AGENT_WALKER_OPENCODE_DB", str(opencode_db))
    assert resolve_database() == opencode_db


def test_database_candidates_honor_xdg_then_default(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_WALKER_OPENCODE_DB", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(opencode_search.Path, "home", lambda: tmp_path / "home")
    assert opencode_search._database_candidates(None) == [
        tmp_path / "xdg" / "opencode" / "opencode.db",
        tmp_path / "home" / ".local" / "share" / "opencode" / "opencode.db",
    ]
    monkeypatch.delenv("XDG_DATA_HOME")
    assert opencode_search._database_candidates(None) == [
        tmp_path / "home" / ".local" / "share" / "opencode" / "opencode.db",
    ]


def test_search_returns_newest_first_with_context(opencode_db):
    result = search(
        SearchOptions(
            pattern="violet lighthouse",
            db_path=str(opencode_db),
            context_turns=1,
        )
    )
    assert [hit["session_id"] for hit in result["hits"]] == ["ses_new", "ses_old"]
    newest = result["hits"][0]
    assert newest["provider"] == "opencode"
    assert newest["cwd"] == "/work/beta"
    assert newest["session_title"] == "Beta session"
    assert newest["context_after"][0]["text"] == "No prose match here"
    assert newest["match_offsets"] == [[0, 17]]
    assert result["summary"]["sessions_matched"] == 2


def test_filters_count_and_truncation(opencode_db):
    result = search(
        SearchOptions(
            pattern="remember",
            role="user",
            cwd="alpha",
            db_path=str(opencode_db),
            since="2023-01-01T00:00:00Z",
            until="2024-01-01T00:00:00Z",
            context_turns=0,
            limit=1,
            count_only=True,
        )
    )
    assert result["hits"] == []
    assert result["summary"]["hits"] == 1
    assert result["summary"]["truncated"] is False


def test_case_sensitivity_and_tool_blocks(opencode_db):
    without_tools = search(SearchOptions(pattern="beacon", db_path=str(opencode_db)))
    assert without_tools["hits"] == []
    with_tools = search(
        SearchOptions(
            pattern="beacon",
            db_path=str(opencode_db),
            include_tool_blocks=True,
        )
    )
    assert len(with_tools["hits"]) == 1
    case_sensitive = search(
        SearchOptions(
            pattern="VIOLET lighthouse",
            case_sensitive=True,
            db_path=str(opencode_db),
        )
    )
    assert len(case_sensitive["hits"]) == 1


def test_regex_is_rejected_without_backtracking(opencode_db):
    with pytest.raises(OpenCodeSearchError, match="linear-time RE2"):
        search(
            SearchOptions(
                pattern="(a+)+$",
                regex=True,
                db_path=str(opencode_db),
            )
        )


def test_reads_committed_rows_from_live_wal(opencode_db):
    writer = sqlite3.connect(opencode_db)
    writer.execute("PRAGMA journal_mode = WAL")
    writer.execute("PRAGMA wal_autocheckpoint = 0")
    writer.execute(
        "INSERT INTO message VALUES (?, ?, ?, ?)",
        ("m_wal", "ses_new", 1_720_000_000_000, _json({"role": "user"})),
    )
    writer.execute(
        "INSERT INTO part VALUES (?, ?, ?, ?, ?)",
        (
            "p_wal",
            "m_wal",
            "ses_new",
            1_720_000_000_000,
            _json({"type": "text", "text": "visible from the live wal"}),
        ),
    )
    writer.commit()
    try:
        result = search(SearchOptions(pattern="live wal", db_path=str(opencode_db)))
    finally:
        writer.close()
    assert [hit["source_id"] for hit in result["hits"]] == ["p_wal"]


def test_missing_database_is_reported_without_failure(tmp_path):
    result = search(
        SearchOptions(pattern="anything", db_path=str(tmp_path / "missing.db"))
    )
    assert result["summary"]["available"] is False
    assert "not found" in result["note"]


def test_schema_drift_has_actionable_error(tmp_path):
    path = tmp_path / "bad.db"
    sqlite3.connect(path).execute("CREATE TABLE session (id TEXT)").connection.close()
    with pytest.raises(OpenCodeSearchError, match="missing tables"):
        search(SearchOptions(pattern="anything", db_path=str(path)))


def test_missing_required_column_has_actionable_error(opencode_db):
    connection = sqlite3.connect(opencode_db)
    connection.execute("ALTER TABLE session RENAME TO old_session")
    connection.execute("CREATE TABLE session (id TEXT, slug TEXT, directory TEXT)")
    connection.commit()
    connection.close()
    with pytest.raises(OpenCodeSearchError, match="session is missing columns: title"):
        search(SearchOptions(pattern="anything", db_path=str(opencode_db)))


def test_malformed_message_and_non_chat_role_are_skipped(opencode_db):
    connection = sqlite3.connect(opencode_db)
    connection.executemany(
        "INSERT INTO message VALUES (?, ?, ?, ?)",
        [
            ("m_bad", "ses_new", 1_730_000_000_000, "not-json"),
            ("m_system", "ses_new", 1_730_000_001_000, _json({"role": "system"})),
            ("m_numeric", "ses_new", 1_730_000_002_000, _json({"role": "user"})),
        ],
    )
    connection.executemany(
        "INSERT INTO part VALUES (?, ?, ?, ?, ?)",
        [
            (
                "p_bad",
                "m_bad",
                "ses_new",
                1_730_000_000_000,
                _json({"type": "text", "text": "should be skipped"}),
            ),
            (
                "p_system",
                "m_system",
                "ses_new",
                1_730_000_001_000,
                _json({"type": "text", "text": "should be skipped"}),
            ),
            (
                "p_numeric",
                "m_numeric",
                "ses_new",
                1_730_000_002_000,
                _json({"type": "text", "text": 42}),
            ),
        ],
    )
    connection.commit()
    connection.close()
    result = search(
        SearchOptions(pattern="should be skipped", db_path=str(opencode_db))
    )
    assert result["hits"] == []


def test_tool_parts_without_searchable_state_are_skipped(opencode_db):
    connection = sqlite3.connect(opencode_db)
    connection.executemany(
        "INSERT INTO part VALUES (?, ?, ?, ?, ?)",
        [
            ("p_no_state", "m4", "ses_new", 1_710_000_001_002, _json({"type": "tool"})),
            (
                "p_empty_state",
                "m4",
                "ses_new",
                1_710_000_001_003,
                _json({"type": "tool", "state": {"output": ""}}),
            ),
            (
                "p_input_only",
                "m4",
                "ses_new",
                1_710_000_001_004,
                _json({"type": "tool", "state": {"input": "input-only-needle"}}),
            ),
        ],
    )
    connection.commit()
    connection.close()
    result = search(
        SearchOptions(
            pattern="input-only-needle",
            db_path=str(opencode_db),
            include_tool_blocks=True,
        )
    )
    assert len(result["hits"]) == 1


@pytest.mark.parametrize("value", ["nonsense", "2024-01-01T00:00:00"])
def test_bad_time_is_rejected(opencode_db, value):
    with pytest.raises(OpenCodeSearchError, match="bad time"):
        search(SearchOptions(pattern="violet", since=value, db_path=str(opencode_db)))


def test_relative_time_and_filters_exclude_messages(opencode_db, monkeypatch):
    monkeypatch.setattr(opencode_search.time, "time", lambda: 1_720_000_000.0)
    result = search(
        SearchOptions(
            pattern="violet lighthouse",
            since="180d",
            cwd="beta",
            db_path=str(opencode_db),
        )
    )
    assert [hit["session_id"] for hit in result["hits"]] == ["ses_new"]
    cwd_filtered = search(
        SearchOptions(
            pattern="remember",
            cwd="beta",
            db_path=str(opencode_db),
        )
    )
    assert cwd_filtered["hits"] == []


def test_empty_pattern_and_sqlite_read_error(opencode_db, monkeypatch):
    with pytest.raises(OpenCodeSearchError, match="non-empty"):
        search(SearchOptions(pattern="", db_path=str(opencode_db)))
    monkeypatch.setattr(
        opencode_search,
        "_open_database",
        lambda path: (_ for _ in ()).throw(sqlite3.OperationalError("locked")),
    )
    with pytest.raises(OpenCodeSearchError, match="locked"):
        search(SearchOptions(pattern="x", db_path=str(opencode_db)))


@pytest.mark.parametrize(
    ("options", "message"),
    [
        (SearchOptions(pattern="x", limit=0), "limit"),
        (SearchOptions(pattern="x", context_turns=-1), "context_turns"),
        (SearchOptions(pattern="x", role="system"), "role"),
    ],
)
def test_invalid_options_are_rejected(options, message):
    with pytest.raises(OpenCodeSearchError, match=message):
        search(options)
