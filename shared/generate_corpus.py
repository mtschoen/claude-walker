"""Synthesize the conformance corpus and compute expected outputs in one pass.

Run from anywhere; writes to `shared/corpus/` and `shared/expected.json`
relative to this file. Re-run any time the fixtures or pricing change.

Each fixture is a small JSONL file (or directory of files) under
`shared/corpus/`. The expected output for each fixture is computed by the
SAME logic the spec mandates, then locked into `shared/expected.json`.
Languages must reproduce these values to within $0.01.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CORPUS = ROOT / "corpus"
EXPECTED_PATH = ROOT / "expected.json"

# Pinned "now" for conformance: 2026-05-09 12:00:00 UTC.
NOW_UNIX = 1778414400.0
PERIOD_SECONDS = 86400  # 1 day -> period_cutoff = NOW - 1 day
WIN_START_UNIX = NOW_UNIX - 6 * 3600  # 6 hours ago

# Pricing: must match SPEC.md.
WEB_SEARCH_COST_USD = 0.01  # flat charge per server-side web search request

# Sonnet 5 introductory pricing runs through 2026-08-31 inclusive; standard
# pricing applies from 2026-09-01 onward. We select via a lexicographic
# compare on the ISO-8601 date prefix (first 10 chars, "YYYY-MM-DD"), which
# is monotonic for zero-padded dates and sidesteps timezone hair-splitting.
SONNET5_STANDARD_FROM = "2026-09-01"
SONNET5_INTRO_RATES = (2.0, 10.0)
SONNET5_STANDARD_RATES = (3.0, 15.0)


def rates_for(model_id: str, date_prefix: str = "") -> tuple[float, float]:
    name = (model_id or "").lower()
    # fable/mythos first: the Fable family bills at $10/$50 and must win
    # before any substring that could collide.
    if "fable" in name or "mythos" in name:
        return (10.0, 50.0)
    if "opus" in name:
        return (5.0, 25.0)
    if "haiku" in name:
        return (1.0, 5.0)
    # sonnet-5 BEFORE the generic sonnet fallback. "sonnet-5" does NOT match
    # "claude-sonnet-4-5" (no "sonnet-5" substring there), so 4-5 keeps the
    # generic sonnet rates below.
    if "sonnet-5" in name:
        if date_prefix and date_prefix[:10] >= SONNET5_STANDARD_FROM:
            return SONNET5_STANDARD_RATES
        return SONNET5_INTRO_RATES
    # sonnet — and any unknown model — falls back to sonnet rates.
    return (3.0, 15.0)


def cost_for(usage: dict, model_id: str, date_prefix: str = "") -> float:
    inp, out = rates_for(model_id, date_prefix)

    def as_int(value) -> int:
        # SPEC "Lenient per-field parsing": any JSON number is truncated
        # toward zero and used iff it lands in [0, 2^64); everything else
        # (strings, objects, out-of-range values) prices as zero.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 0
        whole = int(value)  # truncates floats toward zero
        return whole if 0 <= whole < 2**64 else 0

    i = as_int(usage.get("input_tokens", 0))
    r = as_int(usage.get("cache_read_input_tokens", 0))
    w = as_int(usage.get("cache_creation_input_tokens", 0))
    o = as_int(usage.get("output_tokens", 0))
    server_tool_use = usage.get("server_tool_use")
    web = as_int(server_tool_use.get("web_search_requests", 0)
                 if isinstance(server_tool_use, dict) else 0)
    token_cost = (i * inp + r * inp * 0.10 + w * inp * 1.25 + o * out) / 1_000_000
    return token_cost + web * WEB_SEARCH_COST_USD


def iso(unix: float) -> str:
    return datetime.fromtimestamp(unix, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )


def turn(model: str, ts_unix: float, *, msg_id: str | None,
         input_tokens: int = 0, output_tokens: int = 0,
         cache_read: int = 0, cache_write: int = 0,
         web_search_requests: int = 0) -> dict:
    usage: dict = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_write,
    }
    # Only emit the nested server_tool_use object when there are searches,
    # so fixtures also exercise the absent-object path (web count -> 0).
    if web_search_requests:
        usage["server_tool_use"] = {"web_search_requests": web_search_requests}
    return {
        "type": "assistant",
        "timestamp": iso(ts_unix),
        "message": {
            "role": "assistant",
            "id": msg_id,
            "model": model,
            "usage": usage,
        },
    }


def write_jsonl(path: Path, entries: list[dict | str | bytes]) -> None:
    """Write one entry per line. Dicts are json-dumped; raw strings are
    written verbatim (malformed-line rungs); raw bytes are written as-is
    (invalid-UTF-8 rungs). Binary mode keeps byte rungs and embedded CR
    exact on every platform."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        for entry in entries:
            if isinstance(entry, bytes):
                f.write(entry)
            else:
                text = entry if isinstance(entry, str) else json.dumps(entry)
                f.write(text.encode("utf-8"))
            f.write(b"\n")


# Build fixtures and walk them inline to compute expected outputs.
# Each fixture entry: (group_key, files_dict). files_dict maps relative path
# (under CORPUS) to its list of JSONL entries (dicts or raw strings for
# malformed-line cases).

period_cutoff = NOW_UNIX - PERIOD_SECONDS

# Convenient time anchors for fixtures
FRESH = NOW_UNIX - 1800       # 30 min ago: in trailing AND in window
RECENT = NOW_UNIX - 5 * 3600  # 5h ago: in trailing AND in window
PRE_WIN = NOW_UNIX - 12 * 3600  # 12h ago: in trailing, NOT in window
OLD = NOW_UNIX - 2 * 86400    # 2 days ago: out of trailing AND window
# Future anchor past the Sonnet 5 intro->standard boundary (2026-09-01). It is
# after NOW so it lands in BOTH buckets (no upper bound on ts), letting the
# standard-rate branch be exercised and priced.
SONNET5_STANDARD_TS = datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc).timestamp()


def fixture_01_single_parent():
    """One parent with mixed-model turns; all in BOTH buckets."""
    slug = "01-single-parent"
    sid = "alpha"
    turns = [
        turn("claude-opus-4-7", FRESH, msg_id="m01",
             input_tokens=1000, output_tokens=500,
             cache_read=10000, cache_write=2000),
        turn("claude-sonnet-4-6", RECENT, msg_id="m02",
             input_tokens=500, output_tokens=200,
             cache_read=5000, cache_write=0),
        turn("claude-haiku-4-5", FRESH, msg_id="m03",
             input_tokens=100, output_tokens=50),
    ]
    return slug, {f"{sid}.jsonl": turns}


def fixture_02_parent_acompact():
    """Parent + acompact-subagent share msg ids -> dedup must apply."""
    slug = "02-parent-acompact"
    sid = "beta"
    parent_turns = [
        turn("claude-opus-4-7", FRESH, msg_id="shared-1",
             input_tokens=2000, output_tokens=1000),
        turn("claude-opus-4-7", RECENT, msg_id="shared-2",
             input_tokens=2000, output_tokens=1000),
        turn("claude-sonnet-4-6", FRESH, msg_id="parent-only",
             input_tokens=500, output_tokens=200),
    ]
    subagent_turns = [
        turn("claude-opus-4-7", FRESH, msg_id="shared-1",
             input_tokens=2000, output_tokens=1000),  # dup -> skip
        turn("claude-opus-4-7", RECENT, msg_id="shared-2",
             input_tokens=2000, output_tokens=1000),  # dup -> skip
        turn("claude-haiku-4-5", FRESH, msg_id="sub-only",
             input_tokens=300, output_tokens=100),
    ]
    return slug, {
        f"{sid}.jsonl": parent_turns,
        f"{sid}/subagents/agent-acompact-x.jsonl": subagent_turns,
    }


def fixture_03_malformed_lines():
    """Malformed JSON, missing fields, non-assistant role -- all skipped."""
    slug = "03-malformed-lines"
    sid = "gamma"
    entries = [
        "this is not JSON",
        '{"unclosed":',
        json.dumps({"type": "user", "message": {"role": "user"}}),
        json.dumps({"type": "assistant", "message": {"role": "user"}}),  # wrong role
        json.dumps({"type": "assistant", "message": {"role": "assistant", "id": "no-ts",
                    "model": "claude-opus-4-7", "usage": {"input_tokens": 100}}}),
        # missing timestamp -> skipped
        turn("claude-opus-4-7", FRESH, msg_id="ok",
             input_tokens=1000, output_tokens=500),
        json.dumps({"type": "assistant", "message": {"role": "assistant",
                    "model": "claude-opus-4-7", "usage": {}, "id": "bad-ts"},
                    "timestamp": "not-a-timestamp"}),
        # bad timestamp -> skipped
    ]
    return slug, {f"{sid}.jsonl": entries}


def fixture_04_empty():
    """Zero-byte file."""
    slug = "04-empty"
    return slug, {"empty.jsonl": []}


def fixture_05_out_of_range():
    """All turns predate period_cutoff -> contribute zero."""
    slug = "05-out-of-range"
    sid = "delta"
    turns = [
        turn("claude-opus-4-7", OLD, msg_id="old1",
             input_tokens=999999, output_tokens=999999),
        turn("claude-opus-4-7", OLD, msg_id="old2",
             input_tokens=999999, output_tokens=999999),
    ]
    return slug, {f"{sid}.jsonl": turns}


def fixture_06_period_only():
    """Turn is in the period (trailing) but predates win_start."""
    slug = "06-period-only"
    sid = "epsilon"
    turns = [
        turn("claude-opus-4-7", PRE_WIN, msg_id="p1",
             input_tokens=1000, output_tokens=500),
        # Above is between period_cutoff and win_start: in trailing only.
        turn("claude-opus-4-7", FRESH, msg_id="p2",
             input_tokens=1000, output_tokens=500),
        # Above is in BOTH buckets.
    ]
    return slug, {f"{sid}.jsonl": turns}


def fixture_07_unknown_model():
    """Unknown model family -> falls back to sonnet rates."""
    slug = "07-unknown-model"
    sid = "zeta"
    turns = [
        turn("claude-mystery-x", FRESH, msg_id="u1",
             input_tokens=1000, output_tokens=500),  # priced as sonnet
    ]
    return slug, {f"{sid}.jsonl": turns}


def fixture_08_web_search():
    """Server-side web search: $0.01 per request on top of token cost.

    Both turns fall in trailing AND window. 5 + 3 = 8 searches -> $0.08
    added beyond tokens, far above the $0.01 tolerance, so an impl that
    ignores web search (or mis-parses the nested server_tool_use object)
    fails. The other fixtures omit server_tool_use, covering the absent case.
    """
    slug = "08-web-search"
    sid = "eta"
    turns = [
        turn("claude-opus-4-7", FRESH, msg_id="ws1",
             input_tokens=1000, output_tokens=500, web_search_requests=5),
        turn("claude-sonnet-4-6", RECENT, msg_id="ws2",
             input_tokens=200, output_tokens=100, web_search_requests=3),
    ]
    return slug, {f"{sid}.jsonl": turns}


def dirty_ladder_entries(good_msg_id: str = "dirty-good") -> list[dict | str]:
    """A reusable "skip-ladder" sequence of bad/dirty lines + one good turn.

    Exercises (A8 in COVERAGE-GAPS.md) the shared line filter used by cost,
    events, beacons, and search scanners. Each implementation must SKIP
    every bad line without crashing and ONLY count the single good turn at
    the end. The good turn is anchored at FRESH so its cost lands in both
    trailing and window buckets.

    Bad-line ladder (each kind appears at least once):
      1. blank line
      2. line of only whitespace
      3. malformed JSON (unclosed object)
      4. malformed JSON (bareword garbage)
      5. non-object root (a JSON array)
      6. non-assistant role ({"type":"user", role:"user"})
      7. assistant with empty content array
      8. assistant missing `timestamp` field
      9. assistant with unparseable timestamp
     10. assistant with role mismatch (type=assistant but role=user)
    Then one valid assistant turn that should be counted.
    """
    entries: list[dict | str] = [
        "",                                   # 1: blank
        "   ",                                # 2: whitespace only
        '{"type":"assistant","timestamp":',   # 3: unclosed JSON
        "this is not JSON at all",            # 4: bareword garbage
        json.dumps([1, 2, 3]),                # 5: non-object root
        json.dumps({                           # 6: user role -- not assistant
            "type": "user", "timestamp": iso(FRESH),
            "message": {"role": "user", "content": "hi"},
        }),
        json.dumps({                           # 7: missing timestamp
            "type": "assistant",
            "message": {"role": "assistant", "id": "no-ts",
                        "model": "claude-opus-4-7",
                        "usage": {"input_tokens": 99}},
        }),
        json.dumps({                           # 8: unparseable timestamp
            "type": "assistant", "timestamp": "definitely-not-iso",
            "message": {"role": "assistant", "id": "bad-ts",
                        "model": "claude-opus-4-7",
                        "usage": {"input_tokens": 99}},
        }),
        json.dumps({                           # 8b: space in place of the
            # ISO 'T' separator -- digit-positional parsers must reject it
            # (decided 2026-06-10; rust/cpp previously accepted it).
            "type": "assistant", "timestamp": "2026-05-09 11:00:00.000Z",
            "message": {"role": "assistant", "id": "space-sep-ts",
                        "model": "claude-opus-4-7",
                        "usage": {"input_tokens": 99}},
        }),
        json.dumps({                           # 9: type=assistant, role=user
            "type": "assistant", "timestamp": iso(FRESH),
            "message": {"role": "user", "id": "role-mismatch",
                        "model": "claude-opus-4-7",
                        "usage": {"input_tokens": 99}},
        }),
        turn("claude-sonnet-4-6", FRESH, msg_id=good_msg_id,
             input_tokens=1000, output_tokens=500),   # the one good turn
    ]
    return entries


def fixture_09_dirty_ladder():
    """Reusable dirty/skip-ladder fixture (A8). Every line except the last
    is malformed or rejected by the line filter; only the last turn counts.
    """
    slug = "09-dirty-ladder"
    sid = "theta"
    return slug, {f"{sid}.jsonl": dirty_ladder_entries()}


def fixture_10_iso_variants():
    """ISO 8601 timestamp variants (A14): non-Z offset, fractional seconds,
    and a malformed timestamp side-by-side. Only the well-formed entries
    contribute to the totals; the malformed one is skipped per the line
    filter without crashing.
    """
    slug = "10-iso-variants"
    sid = "iota"
    # Build ISO strings with non-Z offset and fractional seconds, both
    # equivalent to FRESH (so an impl that accepts them produces the
    # expected cost). Using FRESH directly so both land in trailing+window.
    fresh_utc = datetime.fromtimestamp(FRESH, tz=timezone.utc)
    # 1) Non-Z numeric offset (+05:30) — local clock = UTC + 5h30m.
    from datetime import timedelta
    offset = timedelta(hours=5, minutes=30)
    local = fresh_utc + offset
    iso_offset = local.strftime("%Y-%m-%dT%H:%M:%S") + "+05:30"
    # 2) Fractional seconds with Z (e.g., .123456Z) — same FRESH.
    iso_frac = fresh_utc.strftime("%Y-%m-%dT%H:%M:%S.123456Z")
    entries: list[dict | str] = [
        # Non-Z numeric offset → equivalent FRESH timestamp.
        json.dumps({
            "type": "assistant", "timestamp": iso_offset,
            "message": {"role": "assistant", "id": "iso-offset",
                        "model": "claude-opus-4-7",
                        "usage": {"input_tokens": 1000, "output_tokens": 500,
                                  "cache_read_input_tokens": 0,
                                  "cache_creation_input_tokens": 0}},
        }),
        # Fractional seconds → also FRESH.
        json.dumps({
            "type": "assistant", "timestamp": iso_frac,
            "message": {"role": "assistant", "id": "iso-frac",
                        "model": "claude-sonnet-4-6",
                        "usage": {"input_tokens": 500, "output_tokens": 100,
                                  "cache_read_input_tokens": 0,
                                  "cache_creation_input_tokens": 0}},
        }),
        # Malformed timestamp → skipped.
        json.dumps({
            "type": "assistant", "timestamp": "2026-13-99T99:99:99Z",
            "message": {"role": "assistant", "id": "iso-malformed",
                        "model": "claude-opus-4-7",
                        "usage": {"input_tokens": 999999}},
        }),
    ]
    return slug, {f"{sid}.jsonl": entries}


def fixture_11_byte_edges():
    """Byte-level and per-field edge shapes the line filter must survive:
    CRLF line endings, an invalid-UTF-8 line, duplicate message ids inside
    one file, wrong-typed usage fields (parse to zero everywhere), message
    as a bare string / null, a lone-surrogate root key, non-string and
    >=19-char-malformed timestamps, and a pre-1970 timestamp (skipped as
    out-of-range, but the date math must not blow up)."""
    slug = "11-byte-edges"
    sid = "kappa"
    good = turn("claude-opus-4-7", FRESH, msg_id="be-good",
                input_tokens=1000, output_tokens=500)
    dup = turn("claude-sonnet-4-6", FRESH, msg_id="be-dup",
               input_tokens=400, output_tokens=200)
    pre_epoch = dict(turn("claude-opus-4-7", FRESH, msg_id="be-1965",
                          input_tokens=777777, output_tokens=777777))
    pre_epoch["timestamp"] = "1965-03-01T00:00:00Z"
    entries: list[dict | str | bytes] = [
        # CRLF-terminated good turn (the \r must be stripped, then counted).
        json.dumps(good) + "\r",
        b'\xff\xfe invalid utf-8 line',
        '{"\\ud800": "lone surrogate key"}',
        dup,
        dup,  # same message id -> second occurrence deduped
        {"type": "assistant", "timestamp": iso(FRESH), "message": "bare"},
        {"type": "assistant", "timestamp": iso(FRESH), "message": None},
        {"type": "assistant", "timestamp": 12345,
         "message": {"role": "assistant", "id": "be-num-ts",
                     "model": "claude-opus-4-7", "usage": {"input_tokens": 5}}},
        {"type": "assistant", "timestamp": "2026-XX-09TZZ:00:00Z",
         "message": {"role": "assistant", "id": "be-long-bad-ts",
                     "model": "claude-opus-4-7", "usage": {"input_tokens": 5}}},
        # Wrong-typed usage values: every impl prices them as zero (whether
        # it rejects the line or zeroes the fields), so totals agree.
        {"type": "assistant", "timestamp": iso(FRESH),
         "message": {"role": "assistant", "id": "be-bad-usage",
                     "model": "claude-opus-4-7",
                     "usage": {"input_tokens": "many", "output_tokens": None,
                               "cache_read_input_tokens": [],
                               "cache_creation_input_tokens": {}}}},
        # Lone-surrogate keys at message / usage / server_tool_use depth:
        # strict parsers reject the line, per-key parsers skip the key; all
        # carried costs are zero, so totals agree either way.
        ('{"type": "assistant", "timestamp": "%s", "message": '
         '{"\\ud800": 1, "role": "assistant", "id": "be-msg-badkey", '
         '"model": "claude-opus-4-7", "usage": {}}}' % iso(FRESH)),
        ('{"type": "assistant", "timestamp": "%s", "message": '
         '{"role": "assistant", "id": "be-usage-badkey", '
         '"model": "claude-opus-4-7", "usage": {"\\ud800": 1}}}' % iso(FRESH)),
        ('{"type": "assistant", "timestamp": "%s", "message": '
         '{"role": "assistant", "id": "be-stu-badkey", '
         '"model": "claude-opus-4-7", '
         '"usage": {"server_tool_use": {"\\ud800": 1}}}}' % iso(FRESH)),
        # Wrong-shaped usage / server_tool_use containers and a wrong-typed
        # web_search_requests: all price to zero in every impl.
        {"type": "assistant", "timestamp": iso(FRESH),
         "message": {"role": "assistant", "id": "be-usage-str",
                     "model": "claude-opus-4-7", "usage": "notanobject"}},
        {"type": "assistant", "timestamp": iso(FRESH),
         "message": {"role": "assistant", "id": "be-stu-scalar",
                     "model": "claude-opus-4-7",
                     "usage": {"server_tool_use": 5}}},
        {"type": "assistant", "timestamp": iso(FRESH),
         "message": {"role": "assistant", "id": "be-stu-wrong-typed",
                     "model": "claude-opus-4-7",
                     "usage": {"server_tool_use":
                               {"web_search_requests": "five"}}}},
        pre_epoch,
    ]
    return slug, {f"{sid}.jsonl": entries}


def fixture_12_wrong_typed_usage():
    """SPEC "Lenient per-field parsing" (COVERAGE-PLAN item 4): wrong-typed
    usage fields never poison the line. Numeric tokens truncate toward zero
    (1.5 -> 1, 2e2 -> 200), out-of-range numbers (negative, 1e300, 24-digit
    integers) price as absent without panicking, non-number tokens price as
    absent, wrong-typed model/id behave as missing, unknown usage /
    server_tool_use keys are skipped, and an id-less assistant turn still
    counts (no dedup participation)."""
    slug = "12-wrong-typed-usage"
    sid = "wrongu"
    entries: list[dict | str] = [
        # usage is not an object -> zero-usd turn, still counted.
        {"type": "assistant", "timestamp": iso(FRESH),
         "message": {"role": "assistant", "id": "wt-1",
                     "model": "claude-sonnet-4-6", "usage": "not-an-object"}},
        # One wrong-typed field among good ones -> only that field absent.
        {"type": "assistant", "timestamp": iso(FRESH),
         "message": {"role": "assistant", "id": "wt-2",
                     "model": "claude-sonnet-4-6",
                     "usage": {"input_tokens": "abc", "output_tokens": 50}}},
        # Fractional + exponent numbers truncate toward zero (1.5 -> 1,
        # 2e2 -> 200).
        {"type": "assistant", "timestamp": iso(FRESH),
         "message": {"role": "assistant", "id": "wt-3",
                     "model": "claude-sonnet-4-6",
                     "usage": {"input_tokens": 1.5, "output_tokens": 2e2}}},
        # Negative count absent; non-object server_tool_use absent.
        {"type": "assistant", "timestamp": iso(FRESH),
         "message": {"role": "assistant", "id": "wt-4",
                     "model": "claude-sonnet-4-6",
                     "usage": {"input_tokens": -5, "output_tokens": 50,
                               "server_tool_use": "nope"}}},
        # Wrong-typed model -> empty model -> sonnet rates; wrong-typed
        # web_search_requests absent; unknown server_tool_use key skipped.
        {"type": "assistant", "timestamp": iso(FRESH),
         "message": {"role": "assistant", "id": "wt-5", "model": 7,
                     "usage": {"input_tokens": 100, "output_tokens": 10,
                               "server_tool_use": {"web_search_requests": "x",
                                                   "other": 1}}}},
        # Wrong-typed id -> no dedup participation; unknown usage key skipped.
        {"type": "assistant", "timestamp": iso(FRESH),
         "message": {"role": "assistant", "id": 42,
                     "model": "claude-opus-4-7",
                     "usage": {"input_tokens": 100, "output_tokens": 10,
                               "service_tier": "standard"}}},
        # Out-of-u64-range numbers price as absent and MUST NOT panic
        # (1e300 aborted the zig walker before the 2026-06-10 fix).
        {"type": "assistant", "timestamp": iso(FRESH),
         "message": {"role": "assistant", "id": "wt-7",
                     "model": "claude-sonnet-4-6",
                     "usage": {"input_tokens": 1e300, "output_tokens": 5}}},
        ('{"type": "assistant", "timestamp": "%s", "message": '
         '{"role": "assistant", "id": "wt-8", "model": "claude-sonnet-4-6", '
         '"usage": {"input_tokens": 99999999999999999999999, '
         '"output_tokens": 5}}}' % iso(FRESH)),
        # Fractional web_search_requests truncates (2.9 -> 2 -> $0.02).
        {"type": "assistant", "timestamp": iso(FRESH),
         "message": {"role": "assistant", "id": "wt-9",
                     "model": "claude-sonnet-4-6",
                     "usage": {"server_tool_use":
                               {"web_search_requests": 2.9}}}},
        # No id field at all -> counted, dedup is skipped for this turn.
        {"type": "assistant", "timestamp": iso(FRESH),
         "message": {"role": "assistant",
                     "model": "claude-haiku-4-5",
                     "usage": {"input_tokens": 100, "output_tokens": 50}}},
    ]
    return slug, {f"{sid}.jsonl": entries}


def fixture_13_fable():
    """Fable family (claude-fable-5) prices at $10/$50, not the $3/$15
    unknown-family fallback. Anchored at FRESH -> both buckets."""
    slug = "13-fable"
    sid = "fable"
    turns = [
        turn("claude-fable-5", FRESH, msg_id="fa1",
             input_tokens=1000, output_tokens=500,
             cache_read=10000, cache_write=2000),
    ]
    return slug, {f"{sid}.jsonl": turns}


def fixture_14_mythos():
    """Mythos ids also map to the Fable family ($10/$50). Exercises the
    second alternative of the fable/mythos matcher."""
    slug = "14-mythos"
    sid = "mythos"
    turns = [
        turn("claude-mythos-preview", FRESH, msg_id="my1",
             input_tokens=1000, output_tokens=500,
             cache_read=10000, cache_write=2000),
    ]
    return slug, {f"{sid}.jsonl": turns}


def fixture_15_sonnet5_intro():
    """Sonnet 5 at an intro-window timestamp (before 2026-09-01) -> $2/$10.
    FRESH is 2026-05 so it is intro-priced and lands in both buckets."""
    slug = "15-sonnet5-intro"
    sid = "sonnet5intro"
    turns = [
        turn("claude-sonnet-5", FRESH, msg_id="s5i1",
             input_tokens=1000, output_tokens=500,
             cache_read=10000, cache_write=2000),
    ]
    return slug, {f"{sid}.jsonl": turns}


def fixture_16_sonnet5_standard():
    """Sonnet 5 at/after the 2026-09-01 boundary -> standard $3/$15.
    Dated 2026-09-15 (after NOW, so still counted in both buckets)."""
    slug = "16-sonnet5-standard"
    sid = "sonnet5std"
    turns = [
        turn("claude-sonnet-5", SONNET5_STANDARD_TS, msg_id="s5s1",
             input_tokens=1000, output_tokens=500,
             cache_read=10000, cache_write=2000),
    ]
    return slug, {f"{sid}.jsonl": turns}


def fixture_17_sonnet45_noncollision():
    """Regression guard: claude-sonnet-4-5 must NOT match the "sonnet-5"
    branch (no such substring) and prices at the generic sonnet $3/$15,
    date-independent. Dated at SONNET5_STANDARD_TS to prove the date rule
    does not touch it."""
    slug = "17-sonnet45-noncollision"
    sid = "sonnet45"
    turns = [
        turn("claude-sonnet-4-5", SONNET5_STANDARD_TS, msg_id="s45a",
             input_tokens=1000, output_tokens=500,
             cache_read=10000, cache_write=2000),
        turn("claude-sonnet-4-5", FRESH, msg_id="s45b",
             input_tokens=1000, output_tokens=500,
             cache_read=10000, cache_write=2000),
    ]
    return slug, {f"{sid}.jsonl": turns}


FIXTURES = [
    fixture_01_single_parent,
    fixture_02_parent_acompact,
    fixture_03_malformed_lines,
    fixture_04_empty,
    fixture_05_out_of_range,
    fixture_06_period_only,
    fixture_07_unknown_model,
    fixture_08_web_search,
    fixture_09_dirty_ladder,
    fixture_10_iso_variants,
    fixture_11_byte_edges,
    fixture_12_wrong_typed_usage,
    fixture_13_fable,
    fixture_14_mythos,
    fixture_15_sonnet5_intro,
    fixture_16_sonnet5_standard,
    fixture_17_sonnet45_noncollision,
]


def walk_group(files: dict[str, list]) -> tuple[float, float]:
    """Reference walk: matches the spec exactly."""
    trailing = window = 0.0
    seen_ids: set[str] = set()
    for entries in files.values():
        for entry in entries:
            if isinstance(entry, bytes):
                continue  # invalid-UTF-8 rung: skipped by every impl
            if isinstance(entry, str):
                stripped = entry.strip()
                if not stripped:
                    continue  # blank / whitespace-only line
                try:
                    entry = json.loads(stripped)
                except Exception:
                    continue  # malformed JSON
            if not isinstance(entry, dict):
                continue  # non-object root (e.g., JSON array)
            msg = entry.get("message") or {}
            if not isinstance(msg, dict):
                continue  # message is a bare string / wrong type
            if msg.get("role") != "assistant":
                continue
            mid = msg.get("id")
            # Non-string ids behave as absent (SPEC lenient parsing): the
            # turn is counted but does not participate in dedup.
            if isinstance(mid, str) and mid:
                if mid in seen_ids:
                    continue
                seen_ids.add(mid)
            ts_str = entry.get("timestamp")
            if not ts_str or not isinstance(ts_str, str):
                continue
            try:
                ts = datetime.fromisoformat(
                    ts_str.replace("Z", "+00:00")
                ).timestamp()
            except (ValueError, TypeError):
                continue
            earliest = min(period_cutoff, WIN_START_UNIX)
            if ts < earliest:
                continue
            usage = msg.get("usage")
            if not isinstance(usage, dict):
                usage = {}  # wrong-shaped usage prices as zero everywhere
            model = msg.get("model")
            if not isinstance(model, str):
                model = ""  # wrong-typed model behaves as absent -> sonnet
            c = cost_for(usage, model, ts_str[:10])
            if ts >= period_cutoff:
                trailing += c
            if ts >= WIN_START_UNIX:
                window += c
    return trailing, window


def main():
    CORPUS.mkdir(parents=True, exist_ok=True)

    # Build first so we know which cost-fixture slug dirs we own, then wipe
    # ONLY those (so a removed fixture still takes effect). shared/corpus/ is
    # shared with the beacons/, events/, search/, and multi_root/ trees — each
    # owned by its own generator or hand-maintained — so wiping the whole tree
    # here would destroy them. The sibling generators each wipe only their own
    # subdir; mirror that.
    built = [build() for build in FIXTURES]
    for slug, _files in built:
        slug_dir = CORPUS / slug
        if slug_dir.exists():
            for p in sorted(slug_dir.rglob("*"), reverse=True):
                if p.is_file():
                    p.unlink()
                elif p.is_dir():
                    p.rmdir()
            slug_dir.rmdir()

    expected = {
        "_meta": {
            "now_unix": NOW_UNIX,
            "period_seconds": PERIOD_SECONDS,
            "win_start_unix": WIN_START_UNIX,
            "note": "Generated by generate_corpus.py. Do not hand-edit.",
        },
        "_aggregate": None,  # filled in below
        "fixtures": {},
    }

    agg_t = agg_w = 0.0
    file_count = 0
    for slug, files in built:
        slug_dir = CORPUS / slug
        for rel, entries in files.items():
            target = slug_dir / rel
            write_jsonl(target, entries)
            file_count += 1
        t, w = walk_group(files)
        expected["fixtures"][slug] = {
            "trailing_usd": round(t, 6),
            "window_usd": round(w, 6),
            "files": sorted(files.keys()),
        }
        agg_t += t
        agg_w += w

    expected["_aggregate"] = {
        "trailing_usd": round(agg_t, 6),
        "window_usd": round(agg_w, 6),
        "files_walked_min": file_count,  # may be lower if any are mtime-pruned
        "groups": len(FIXTURES),
    }

    with open(EXPECTED_PATH, "w", encoding="utf-8") as f:
        json.dump(expected, f, indent=2)
        f.write("\n")

    print(f"Wrote {file_count} fixture files under {CORPUS}")
    print(f"Wrote {EXPECTED_PATH}")
    print(f"Aggregate: trailing=${agg_t:.6f}  window=${agg_w:.6f}")


if __name__ == "__main__":
    main()
