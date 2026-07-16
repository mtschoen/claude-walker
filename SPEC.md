# claude-walker — Interface & Correctness Spec

Every implementation in this repo MUST honor this spec. The conformance
harness (`shared/conformance.py`) verifies it.

## CLI contract

### Invocation

```
walker --period <seconds> --win-start <unix-epoch> [--projects-root <path>]
```

| Flag             | Required | Type    | Default                 |
| ---------------- | -------- | ------- | ----------------------- |
| `--period`       | yes      | u64     | —                       |
| `--win-start`    | yes      | f64     | —                       |
| `--projects-root`| no       | path    | `~/.claude/projects`    |
| `--now`          | no       | f64     | current wall clock      |

`--win-start` and `--now` accept Unix epochs with optional fractional
seconds. `--now` exists so conformance tests can pin "now" to a fixed
moment; production callers omit it.

### Output

One JSON line on stdout, exit 0:

```json
{"trailing_usd": 1480.150500, "window_usd": 1480.150500, "files_walked": 294, "groups": 129, "elapsed_ms": 47}
```

Fields:

| Field         | Required | Type | Notes                                       |
| ------------- | -------- | ---- | ------------------------------------------- |
| `trailing_usd`| yes      | f64  | Cost in `[now - period, now]`               |
| `window_usd`  | yes      | f64  | Cost in `[win_start, ∞)`                    |
| `files_walked`| no       | u32  | Diagnostic — files that survived mtime skip |
| `groups`      | no       | u32  | Diagnostic — distinct (slug, session) keys  |
| `elapsed_ms`  | no       | u64  | Diagnostic — wall-clock from arg parse to print |

Unknown fields are reserved; consumers ignore them.

### Errors

Anything other than exit 0 with a single JSON line on stdout is "fall back
to caller's reference path." Stderr is for diagnostics. The walker MUST
NOT panic, hang, or write partial output. Bad input means clean error +
non-zero exit.

On a usage error (unknown flag, missing required flag, bad value, unknown
subcommand) the stderr diagnostic is followed by a pointer line:

```
walker: --period is required
Run 'claude-walker --help' for usage.
```

### Help & usage

A human running the binary with no arguments (or asking for help) gets a
friendly overview on **stdout** and exit **0** (not the terse error the
status-line caller never sees, since that caller always passes `--period`).

Help is shown (full overview to stdout, exit 0) when **any** of:

1. No arguments are given, OR
2. The first argument is `-h` or `--help`, OR
3. The first argument is a known subcommand and the *next* argument is
   `-h` or `--help` (e.g. `walker search --help`).

The overview lists every subcommand with a one-line description and the
**cost**-mode flag table (the common case). Per-subcommand flag tables are
out of scope for now: `walker search --help` shows the same overview.
Exact wording is per-impl and NOT conformance-pinned; the parity guard
asserts only structure: exit 0, and stdout containing `USAGE`, every
subcommand name, and `--period`. The reference text:

```
claude-walker - fast cost & progress walker over Claude Code transcripts

USAGE:
    claude-walker [SUBCOMMAND] [OPTIONS]

With no subcommand it runs `cost` (back-compat for the status line).

SUBCOMMANDS:
    cost              Trailing + window USD over the transcript fleet (default)
    search <pattern>  Cross-root/-machine content search over transcripts
    events            One NDJSON line per assistant turn (ts, usd, model, session)
    beacons-latest    Most recent <progress-beacon> for a session
    beacons-history   Calibration bias_factor over begin/end beacon pairs

COST OPTIONS (default mode):
    --period <seconds>            Required. Trailing-window length.
    --win-start <unix>            Required. Cost-window start (unix epoch).
    --projects-root <path>        Transcript root (default: ~/.claude/projects).
    --extra-projects-root <path>  Additional root; repeatable.
    --no-config                   Skip ~/.claude/walker-roots.json extras.
    --now <unix>                  Pin "now" (default: wall clock; for tests).

GLOBAL:
    -h, --help     Show this help.
    --version      Print <lang>/<version>.

Full contract: SPEC.md in the source tree.
```

## Roots

Every subcommand walks an effective set of project roots assembled as:

1. **Primary root.** From `--projects-root <path>` if given, else
   `~/.claude/projects`.
2. **CLI extras.** Zero or more `--extra-projects-root <path>` flags.
3. **Config extras.** Read from `~/.claude/walker-roots.json` unless
   `--no-config` is passed.

### Home directory

`~` in both the default primary root (`~/.claude/projects`) and the config
path (`~/.claude/walker-roots.json`) resolves identically across subcommands:

- **Windows:** `USERPROFILE`, falling back to `HOME`. `USERPROFILE` is the
  canonical Windows home; `HOME` is frequently unset, or set by git-bash to a
  POSIX-style path (`/c/Users/...`) that is not a valid native path.
- **Other platforms:** `HOME`, falling back to `USERPROFILE`.
- Neither set → the relative path `.claude/...` (empty-fleet / CI fallback).

### Config file shape

`~/.claude/walker-roots.json`:

```json
{
  "extra_roots": [
    "/mnt/chonkers/Users/mtsch/.claude/projects"
  ]
}
```

Single key `extra_roots`: array of absolute paths. Per-host; NOT
synced via memory-sync. Missing file → no extras. Malformed JSON →
stderr diagnostic, treat as no extras (must NOT error).

### Resolution

The combined list is:

- Deduplicated by `fs::canonical` (realpath); if `canonical` fails for
  an entry, fall back to its lexically-normalized form. The canonical form is
  used **only as the dedup key** — the path actually handed to discovery must
  stay in an enumerable form. (On Windows, canonicalizing a mapped network
  drive can yield a UNC / `\\?\` verbatim path that some directory walkers
  cannot enumerate; walk the original path, dedup by the canonical.)
- Filtered to existing directories. Non-existent extras are skipped
  silently with a stderr diagnostic. (This is the SMB-mount-unreachable
  case — walker must keep going.)
- Order: primary first, CLI extras in order, config extras in order.
  Order is informational; results are aggregated and must not depend on
  it within float epsilon.

Per-group dedup (`seen_ids` on `message.id`) is unchanged. Per-file
mtime filter is unchanged. All applied uniformly across roots.

## Discovery

Glob `<projects-root>/*/*.jsonl` for parents and
`<projects-root>/*/*/subagents/agent-*.jsonl` for subagents. Group by
`(parent_dir_name, session_id)` where `session_id` is the parent file's
stem or the subagent's grandparent dir name.

## Filters

### File-level (mtime)

Skip any file where `mtime < min(now - period, win_start)`. Prunes the
~80% of historical transcripts that can't possibly contain in-range
entries.

### Line-level

Within each surviving file, accept a line iff:

1. It parses as JSON (skip silently otherwise).
2. `entry.message.role == "assistant"` (skip otherwise).
3. If `entry.message.id` is set and already seen **in this group's
   dedup set**, skip.
4. `entry.timestamp` parses as ISO 8601 (with optional `Z` suffix,
   interpreted as UTC). Skip otherwise.
5. `entry.timestamp >= min(period_cutoff, win_start)`.

## Pricing

Per-MTok input/output rates by family (substring match on lowercased
model id). The match is ordered — the **first** family whose needle is a
substring of the lowercased model id wins:

| Order | Family                     | Needle(s)          | Input | Output |
| ----- | -------------------------- | ------------------ | ----- | ------ |
| 1     | fable / mythos             | `fable`, `mythos`  | 10.0  | 50.0   |
| 2     | opus                       | `opus`             | 5.0   | 25.0   |
| 3     | haiku                      | `haiku`            | 1.0   | 5.0    |
| 4     | sonnet-5 (date-aware)      | `sonnet-5`         | 2.0/3.0 | 10.0/15.0 |
| 5     | sonnet / unknown (default) | —                  | 3.0   | 15.0   |

- `cache_read = input_rate × 0.10`
- `cache_write = input_rate × 1.25`
- **Match order is load-bearing:** `fable`/`mythos` are checked first, and
  `sonnet-5` is checked *before* the generic sonnet default. The `sonnet-5`
  needle does **not** match `claude-sonnet-4-5` (the substring `sonnet-5` is
  absent there — `sonnet-4-5` contains `sonnet-4` and a trailing `-5`, not
  `sonnet-5`), so Sonnet 4.5 correctly prices at the generic sonnet default.
- **Sonnet 5 is date-aware.** Introductory pricing **$2.00 / $10.00** applies
  to turns timestamped through **2026-08-31** inclusive; standard pricing
  **$3.00 / $15.00** applies from **2026-09-01** onward. The selection is a
  lexicographic comparison of the ISO-8601 timestamp's 10-char date prefix
  (`YYYY-MM-DD`) against `"2026-09-01"`: `prefix >= "2026-09-01"` → standard,
  else intro. This is monotonic for zero-padded dates and avoids timezone
  normalization (a turn's own recorded local date decides its rate). All four
  impls and the Python reference thread the entry's `timestamp` into the rate
  lookup for this; no other family consults the timestamp.
- Unknown family falls back to **sonnet** default rates (matches Python).
- Opus 1M-tier doubling is **not** applied here — the harness does not apply it
  in practice (measured 0% error vs authoritative `costUSD` on big-context Opus,
  incl. 26M-cache-read sessions), so token rates alone match.
- **Web search:** each server-side web search request is charged a flat
  **$0.01** (billed at $10 / 1,000), added on top of token cost and NOT
  divided by 1M. The count comes from
  `usage.server_tool_use.web_search_requests` (nested object; absent → 0).
  Verified to the cent against the authoritative per-model `costUSD` in
  `~/.claude.json`. Applies to cost, events, and beacon modes alike — they
  all share this one pricing formula.

Cost for one assistant turn (all token counts default to 0 if missing):

```
cost = (
    input_tokens * input_rate
  + cache_read_tokens * input_rate * 0.10
  + cache_write_tokens * input_rate * 1.25
  + output_tokens * output_rate
) / 1_000_000
  + web_search_requests * 0.01
```

Token field names in the JSONL `usage` object:
`input_tokens`, `output_tokens`, `cache_read_input_tokens`,
`cache_creation_input_tokens`. The web-search count is nested one level
deeper: `usage.server_tool_use.web_search_requests` (uint, default 0 when
the object or field is absent).

### Lenient per-field parsing

Wrong-typed fields inside an otherwise-valid JSONL entry MUST NOT poison
the line. A turn that passes the line filters (assistant role, parseable
timestamp, window) is priced from whatever fields are usable; in the
worst case it is emitted/counted as a zero-usd record. Decided 2026-06-10
(closes the strict-vs-lenient divergence: rust/go previously dropped the
whole line on any type mismatch, cpp/zig already parsed per-field):

- A wrong-typed leaf is treated as **absent**. Non-string `model`, `id`,
  `timestamp`, `role` behave exactly like a missing field (so a
  non-string `timestamp` still skips the line via the existing
  missing-timestamp filter, and a non-string `id` simply does not
  participate in dedup).
- A non-object `usage` or `server_tool_use` is treated as an absent
  subtree (all counts 0).
- A token-count field accepts any JSON **number**: the value used is
  `trunc(n)` (toward zero) when `0 <= trunc(n) <= u64::MAX`, otherwise
  the field is treated as absent (0). So `1.5` counts 1, `2e2` counts
  200, `-5`, `1e300`, and 24-digit integers count 0. Out-of-range values
  MUST NOT panic, saturate, or abort the walker. Non-number tokens
  (strings, objects, …) are treated as absent.
- Unknown keys inside `usage` and `server_tool_use` are skipped.

These rules apply wherever the pricing formula is applied (cost, events
— they share one parser per impl).

## Bucketing

For each accepted assistant turn:

```
if ts >= now - period:   trailing += cost
if ts >= win_start:      window   += cost
```

Both can be true (overlapping ranges); the same cost contributes to both
buckets.

## Dedup scope

Per **session group**. The dedup `seen_ids` set is local to one
`(slug, session_id)` group; it covers `parent.jsonl` and any
`subagents/agent-*.jsonl` under the same session_id. Cross-session dedup
is NOT performed — the only collision pattern observed in real corpora
is parent ↔ its-own-acompact-subagent, which session-grouping handles.

Within a group, files are walked in any order; a single shared
`seen_ids` set is consulted before counting.

## Concurrency

Free choice per implementation. Goal: pin all available cores on the
parse work. Recommended starting point: spawn min(8, ncpu) workers; one
group per work unit; merge sums after.

Determinism: the same input MUST produce the same output regardless of
how groups are scheduled. Float addition order within a group is
deterministic (sequential walk inside a group); cross-group sums use
fully-associative addition over a small set so reordering is acceptable
within float epsilon (±$0.01 budget covers it).

## Subcommands

The bare `walker --period ... --win-start ...` invocation maps to the
`cost` subcommand and stays the back-compat shape for existing callers.
A subcommand is introduced when the first positional argument matches
a known name; otherwise the bare-flag invocation is treated as `cost`.

### `beacons-latest --session-id <id> [--projects-root <path>] [--now <unix>]`

Walks the matching transcript (parent or `subagents/agent-<id>.jsonl`)
backwards, finds the most recent assistant message containing a
`<progress-beacon>...</progress-beacon>` block. The JSON inside must
parse and contain the required fields `kind` and `summary`.
`eta_seconds` is required for `begin`/`report` but **optional when
`kind` is `"end"`**, defaulting to `0`: an end beacon carries no
remaining-time estimate, agents routinely omit the field in practice,
and rejecting those beacons left lifecycles permanently open (the
status line and the recency-nudge hook then treat the session as
forever mid-turn). `drift` is **optional** — accepted and passed
through when present, but its absence no longer rejects the beacon
(`beats_left` is likewise optional). When the source beacon omits
`drift`, the returned `beacon` object omits it too; an omitted
`eta_seconds` on an end beacon IS normalized to `0` in the output.

Output:

```json
{"beacon": {...} | null, "emitted_at": <unix> | null, "age_seconds": <num> | null, "elapsed_ms": <u64>}
```

If multiple beacons exist in the matching transcript, return the one
with the highest `timestamp`. Malformed JSON or missing required
fields → silently skip (treated as no beacon).

`--now` exists for conformance determinism (otherwise `age_seconds`
varies per wall clock); production callers omit it.

### `beacons-history --period <seconds> [--win-start <unix>] [--projects-root <path>] [--now <unix>]`

Walks the full fleet under the time window. For each session group
(same grouping as `cost` mode), iterate that group's beacons in
**timestamp-ascending order** (stable on ties), tracking a single
in-flight `pending_begin` (a `(timestamp, beacon)` or null):

- On `kind: "begin"`: if a `pending_begin` is already held, **orphan
  it** (emit no pair for that prior begin) and replace it with this
  one; otherwise just record it.
- On `kind: "end"`: if a `pending_begin` is held and the end's
  `timestamp > pending_begin.timestamp`, emit one pair (fields below)
  and clear `pending_begin`. An `end` with no held begin is
  **orphaned** (no pair).
- On `kind: "report"` (or any other kind): ignored for pairing.

This emits **one pair per properly-closed begin→end lifecycle**, so a
session with N lifecycles yields up to N pairs. (The previous rule —
"earliest begin + latest end per group" — collapsed every lifecycle in
a session into one oversized span, dividing whole-session wall-clock by
the first lifecycle's eta; this replaces that bug.) Each emitted pair
has three elapsed fields:

- `actual_elapsed = end_timestamp - begin_timestamp` (wall-clock)
- `idle_excluded` = sum of gaps inside that pair's `[begin_ts, end_ts]`
  that immediately precede a real user prompt (`type: "user"` entries
  with non-`tool_result` content). Tool-result entries don't count as
  idle because they're agent-active time waiting on tool execution.
- `active_elapsed = max(0, actual_elapsed - idle_excluded)`

A pair qualifies for the window when its `begin_ts` survives the
per-beacon collection filter `begin_ts >= window_lo`, where
`window_lo = max(now - period, win_start)`. The window filters on the
begin timestamp; the derived span may extend past it.

Computes `bias_factor = median(active_elapsed / begin_eta)` across all
emitted pairs. Even-count median is the mean of the two middle values.
The calculation excludes user-idle time because including it makes the
bias unrepresentative of the agent's actual estimation accuracy — a
session where the user walked away for an hour shouldn't punish the
agent's ETA the same as one where the agent genuinely worked an hour.

Output:

```json
{"pairs": [{"begin_eta": <num>, "actual_elapsed": <num>, "idle_excluded": <num>, "active_elapsed": <num>}, ...], "session_count": <num>, "n_pairs": <num>, "bias_factor": <f64> | null, "elapsed_ms": <u64>}
```

If `n_pairs == 0`, `bias_factor` is `null`.

### `events --period <seconds> [--win-start <unix>] [--projects-root <path>] [--no-config] [--extra-projects-root <path>...] [--now <unix>]`

Emits one JSON object per line (NDJSON) on stdout — one entry per
accepted assistant turn. Reuses cost-mode's parse, dedup, filter, and
pricing logic verbatim; only aggregation differs (per-turn output
instead of accumulated totals).

Flag summary:

| Flag                    | Required | Type    | Default              |
| ----------------------- | -------- | ------- | -------------------- |
| `--period`              | yes      | u64     | —                    |
| `--win-start`           | no       | f64     | `now - period`       |
| `--projects-root`       | no       | path    | `~/.claude/projects` |
| `--no-config`           | no       | bool    | false                |
| `--extra-projects-root` | no       | path[]  | (empty)              |
| `--now`                 | no       | f64     | current wall clock   |

`--no-config` suppresses loading `~/.claude/walker-roots.json`.
`--extra-projects-root` may be repeated; each value appends an extra
root (same semantics as cost mode). `--now` exists for conformance
determinism; production callers omit it.

**Window predicate.** A turn is emitted iff:

```
ts >= min(now - period, win_start)
```

When `--win-start` is omitted, `win_start` defaults to `now - period`,
so the predicate simplifies to `ts >= now - period`.

Every turn that passes this predicate (the same one used by cost
mode's step-5 line filter) is emitted as its own NDJSON line. There
is no further bucketing into `trailing` vs `window` totals — that
split is cost mode's aggregation, not events'.

**Output format.** One JSON object per accepted turn, field order
fixed (matters for line-equality conformance):

```json
{"ts": 1716480000.123, "usd": 0.004217, "model": "claude-sonnet-4-6", "session_id": "abc123", "slug": "C--Users-mtsch--claude-projects--myproject"}
```

Fields:

| Field        | Type   | Notes                                                        |
| ------------ | ------ | ------------------------------------------------------------ |
| `ts`         | f64    | Unix epoch (seconds, fractional) of the assistant turn       |
| `usd`        | f64    | Cost of this turn in USD, computed by the Pricing formula    |
| `model`      | string | Lowercased model id from the transcript; empty string if absent |
| `session_id` | string | Session identifier — same grouping key as cost mode          |
| `slug`       | string | Parent directory name of the `.jsonl` file                   |

No summary or final line is emitted. The consumer reads until EOF.
Exit 0 even when the output stream is empty.

**Ordering.** Implementations MAY emit lines in any order. Conformance
compares event sets as a multiset, sorted by `(ts, session_id, model)`
for tie-breaking stability.
### `search <pattern> [flags]`

Content search over transcripts for the recall problem ("you said X a few
sessions ago, but didn't commit it to memory"). The differentiator is
**cross-root / cross-machine** lookup: search inherits the multi-root
resolution from `## Roots`, so a query reaches into mounted remote-host
transcripts when configured. Read-only — search MUST NOT write to a
transcript or to memory. `<pattern>` is required and positional; an empty
pattern is an error.

| Flag | Default | Notes |
| ---- | ------- | ----- |
| `--regex` | false | Treat pattern as an RE2 regex (no lookaround/backreferences). |
| `--case-sensitive` | false | Default is case-insensitive (the usual recall case). |
| `--role <user\|assistant\|both>` | both | Restrict by message role. |
| `--since <t>` / `--until <t>` | none / now | RFC3339 timestamp or relative (`7d`, `12h`). |
| `--cwd <slug>` | any | Restrict to one project slug (the `~/.claude/projects/<slug>` dir name). |
| `--context <N>` | 1 | Turns of context before AND after each hit (`0` = hit only). |
| `--limit <N>` | 50 | Soft cap; overflow sets `truncated` and emits a stderr narrowing hint. |
| `--count-only` | false | Emit only the summary record — a cheap pre-flight to size a query. |
| `--include-tool-blocks` | false | Also search inside `tool_use` / `tool_result` blocks. |
| `--include-queue-ops` | false | Also index content-bearing `queue-operation` entries as `role: user`. |
| `--format <pretty\|jsonl>` | pretty | `jsonl` is agent-consumable (one record per line). |
| `--snippet-chars <N>` | 240 | Max snippet preview chars per hit. |

**Discovery.** Search walks parent transcripts
(`<root>/<slug>/<sid>.jsonl`) AND subagent transcripts
(`<root>/<slug>/<session>/subagents/agent-*.jsonl`) in every resolved
root, mirroring `## Discovery`. A subagent hit reports `session_id` =
the enclosing session directory name (its parent session), so subagent
hits group with their parent in `sessions_matched`. `--cwd <slug>`
restricts both forms; the `--since` mtime fast-path prune applies per
file to parents and subagents alike.

Filters apply cheapest-first per `## Filters`: file mtime, slug, role,
tool-block exclusion, time window, then the pattern match.

**Content extraction.** `message.content` is sometimes a bare string (older
user-prompt format) and sometimes an array of content blocks; parse it untyped
and concatenate the `{"type":"text"}` blocks — strict typed deserialization
silently drops ~10% of older user prompts. Search reaches `role: user` and
`role: assistant` messages only.

**Queue operations.** When you type into the prompt while the agent is busy,
Claude Code queues the input as a `type: "queue-operation"` entry with no
`message` object — invisible to search by default. With `--include-queue-ops`,
such an entry is indexed as `role: user`, reading the **root-level `content`
field** (not `message.content`) and the root `timestamp`; an entry with empty
or absent `content` is skipped, so only the content-bearing `enqueue` and
`popAll` operations surface (`remove`/`dequeue` carry none). There is no
task-notification filtering and no dedup — the flag is the only gate. Queue-ops
count as `role: user` (so `--role assistant` excludes them even with the flag
on) and the time-window filter applies.

Output (`--format jsonl`): one hit record per line, a summary record last.

```json
{"type":"hit","session_id":"...","cwd_slug":"...","host_root":"...","file_path":"...","line_number":147,"timestamp":"...","role":"assistant","snippet":"...","match_offsets":[[1,16]],"context_before":[{"role":"...","text":"...","timestamp":"..."}],"context_after":[{"role":"...","text":"...","timestamp":"..."}]}
{"type":"summary","hits":3,"sessions_matched":4,"roots_walked":2,"files_walked":218,"truncated":false,"elapsed_ms":142}
```

`host_root` is the killer field — it names which machine's mount the hit came
from, closing the "agent didn't think to check the other host" gap. Ordering
is newest-first, tiebroken by `(timestamp DESC, session_id ASC, line_number
ASC)`. `pretty` mode renders the same data human-readably.

**Pretty format** (`--format pretty`, the default). Decided 2026-06-10
(previously the highlight spacing, the summary shape, and go's dropped
post-match suffix diverged per impl). Per hit, in order:

```
[<timestamp>] cwd=<slug> role=<role> session=<session_id>
  <file_path>:<line_number>
  before: <context text, truncated to 120 chars + ellipsis>
  >>> <pre>[<match>]<post> <<<
  after:  <context text>
<blank line>
```

The highlight line brackets the FIRST `match_offsets` pair with the full
snippet on both sides: exactly `  >>> `, `[`, `]`, ` <<<`. (Offsets are
always produced by re-running the matcher on the snippet, which is built
around the first match; in the never-expected empty case the snippet line
is omitted.) After the hits comes ONE human-readable summary line — never
a JSONL record:

```
<hits> hits in <sessions> sessions across <roots> roots (<files> files). truncated=<true|false> elapsed <N>ms.
```

`--count-only` suppresses hit rendering but keeps the summary line. The
truncation warning on stderr is shared verbatim across impls:
`walker: search: truncated to --limit=<N> (had <M> total); narrow with --since`.

**Tool blocks.** With `--include-tool-blocks`, an entry's searchable text
additionally includes, newline-joined in content order (decided
2026-06-10; previously rust sorted dump keys while cpp dropped non-string
inputs and unwrapped string inputs):

- `tool_use.input` — the input value's compact JSON serialization (no
  added whitespace), object key order preserved from the source. A bare
  string input keeps its JSON quotes.
- `tool_result.content` — a string contributes its raw text; an array
  contributes each `{"type":"text"}` block's `text`.

**Snippet boundaries.** `match_offsets` and the snippet window are byte
offsets. The snippet is the byte range `[match_start − ⌊snippet_chars/2⌋,
match_end + ⌊snippet_chars/2⌋)` clamped to the text, with each end then (1)
nudged **forward to the next UTF-8 character boundary**, (2) if it was clipped
(not already at text start/end), nudged outward to the nearest ASCII whitespace
within ±20 bytes, then (3) nudged forward to a character boundary again. The
character-boundary nudge is mandatory — offsets are byte offsets, and slicing
mid-codepoint would emit invalid UTF-8 in `snippet`. The emitted `match_offsets`
are byte offsets **within the snippet** (the matcher is re-run against the
snippet text), not within the original message.

Errors (exit 2, stderr diagnostic): empty pattern (`pattern must be
non-empty`), unparseable regex (`bad regex: <why>`), unparseable
`--since`/`--until` (`bad time: ...`). Malformed JSONL lines are skipped, never
a panic.

Decided constraints: the regex surface is RE2 (no lookaround/backreferences)
across all four impls; substring matching treats newlines as whitespace and
regex honors an explicit `(?m)` in the pattern. No on-disk index, no semantic
search, no mutation.

## MCP shim

`mcp/server.py` is a FastMCP stdio server exposing one tool,
`claude_walker_search`, that subprocesses `walker search ... --format jsonl`
and reshapes the output into `{hits, summary, note}`. It exists so agents get
auto-discovered, cross-cwd recall without constructing a CLI string — the
cross-machine miss is exactly what an always-present tool closes.

- **Binary discovery** (first hit wins): `$CLAUDE_WALKER_BINARY`,
  `~/.claude/walker[.exe]`, `~/.local/bin/claude-walker[.exe]`, then `PATH`.
- **Tool parameters** mirror the CLI flags: `pattern` (required), `regex`,
  `case_sensitive`, `role`, `since`, `until`, `cwd_slug`, `context_turns`,
  `limit`, `count_only`, `include_tool_blocks`.
- **Errors:** a non-zero walker exit (bad input) or a 30 s subprocess timeout
  raises an MCP tool error carrying the walker's stderr; the truncation hint
  from a successful run is passed through as `note`.
- **Logging:** one JSONL event per call (`session_start`/`call`/`return`/
  `error`) at `~/.claude-walker-mcp.log`, mirroring projdash — tail it to trace
  a hang.
- **Registration:** user-scope via
  `claude mcp add --scope user agent-walker -- python <repo>/mcp/server.py`,
  so it's available from every cwd. Launched by absolute script path, not
  `python -m mcp`, to avoid colliding with the `mcp` SDK package name.

## Conformance fixtures

`shared/corpus/<NN>-<name>/<sid>.jsonl` (and optional `<NN>-<name>/subagents/`)
plus `shared/corpus/expected.json` mapping fixture name → expected
output for cost mode. Beacon fixtures live under
`shared/corpus/beacons/<scenario>/<sid>.jsonl` with sibling
`expected_latest.json` and `expected_history.json`. Search fixtures live under
`shared/corpus/search/<scenario>/` with sibling `expected.json`; the harness
structurally compares the JSONL hit/summary records, ignoring `elapsed_ms` and
`files_walked` (they vary per run). The harness
invokes each binary against the corpus and asserts agreement to
±$0.01 for cost and ±0.001 for `bias_factor`.

## Versioning

Each binary supports `--version` printing `<lang>/<version>` (e.g.
`rust/0.1.0`). Future ABI changes bump a `spec_version` field in the
output JSON.
