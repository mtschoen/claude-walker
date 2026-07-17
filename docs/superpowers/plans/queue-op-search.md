# Plan — `search`: index queued-while-busy input (`--include-queue-ops`)

**Status:** ready to implement. Design is settled (corpus-verified, see
"Findings" below — do NOT re-investigate the transcript shape). Single
handoff doc: contract + findings + per-impl steps. Delete this file at
branch-finish (the contract folds into `SPEC.md`; this is scaffolding).

**Author of this plan verified everything against the live corpus on
2026-05-26.** The numbers and data shapes here are real, not from the
older (now-deleted) search-fanout handoff, which was wrong about queue-op
structure — see "What the old handoff got wrong."

## Goal

The `search` subcommand currently indexes `role: user` and
`role: assistant` messages only. When you type into the prompt while the
agent is busy, Claude Code **queues** the message and logs it as a
`type: "queue-operation"` entry — invisible to search. Close that gap so
"find what I typed while it was busy" is recoverable, **behind an opt-in
flag** (the queue is mostly system noise; default-off keeps normal
searches clean).

## Contract (the SPEC.md change)

Add one flag to `search`:

| Flag | Default | Notes |
| ---- | ------- | ----- |
| `--include-queue-ops` | false | Also index content-bearing queue-operation entries as `role: user`. |

Behavior:

- **Off (default):** identical to today — queue-ops ignored entirely.
- **On:** index queue-operation entries that carry content, as
  `role: user`, reading the **root-level `content` field** (NOT
  `message.content` — queue-ops have no `message` object). Only the
  `enqueue` and `popAll` operations carry content; `remove`/`dequeue`
  carry none and are skipped naturally.
- **No task-notification filtering** and **no dedup** — the flag is the
  gate. When you opt in, you get the complete queue record. (Decided with
  the user: a content heuristic would be fragile to port across four
  impls, and the default-off flag already keeps the noise out of normal
  use.)
- **Filters:** time-window applies (queue-ops have a root `timestamp`);
  they count as `role: user` (so `--role user` includes them with the
  flag on, `--role assistant` excludes them); tool-block-skip is N/A.

MCP shim gets a matching `include_queue_ops: bool = False` parameter (see
"MCP shim" step below).

## Findings (corpus-verified — trust these, don't re-derive)

Surveyed ~200 transcripts; 1720 of your transcripts contain queue-ops.

**Entry shape** (keys: `type`, `operation`, `content`, `sessionId`,
`timestamp`):

```json
{"type":"queue-operation","operation":"enqueue","content":"actually also do Y","timestamp":"2026-04-28T22:20:13.120Z","sessionId":"..."}
{"type":"queue-operation","operation":"remove","timestamp":"2026-04-28T22:20:20.774Z","sessionId":"..."}
```

**Operation distribution** (content-bearing in **bold**):

| operation | count (sample) | carries content? |
| --------- | -------------- | ---------------- |
| **enqueue** | 1135 | **yes — always non-empty** |
| remove | 677 | no |
| dequeue | 446 | no |
| **popAll** | 7 | **yes (rare/legacy)** |

So: index `enqueue` + `popAll`; everything else has no content and is
skipped by the "must have non-empty content" check.

**Content breakdown of the 1135+7 content-bearing entries:**

- **72% (~822) are `<task-notification>`** — system-injected
  background-agent status pings (parallel sub-agents posting "task done").
  System noise, not user input. This is *why* the feature is opt-in.
- **27% (~309) plain user text** — the actual recall target.
- A handful of `<channel source="plugin:discord:discord" ...>` — real
  user messages relayed through the Discord plugin integration. Kept
  (they're genuine user input; the no-filter decision keeps them).
- 1 `<<autonomous-loop-dynamic>>` loop sentinel. Harmless.

## What the old (deleted) handoff got wrong

The prior search-fanout handoff claimed enqueue entries "stream as the
user types (multiple, with growing partial content); popAll is what the
agent actually saw." **False for current Claude Code.** Verified: each
`enqueue` is a single complete message; consecutive enqueues are distinct
messages, not partials of one. `popAll` is rare (7 vs 1135). **Therefore
there is NO dedup problem** — the whole "emit one hit per popAll + last
enqueue before it" scheme the old handoff agonized over is moot. Just
index each content-bearing entry once.

## Execution — follow claude-walker's CLAUDE.md feature workflow

Global `Bash(*)` / `Edit(**)` grants are present in
`~/.claude/settings.json`, so the parallel worktree subagent fanout
(step 6) is unblocked — no permission preflight needed.

### 0. Branch / worktree

```bash
cd ~/claude-walker
git worktree add ~/claude-walker-worktrees/queue-op-search -b feature/queue-op-search main
cd ~/claude-walker-worktrees/queue-op-search
```

Confirm `git status` clean and `git log origin/main..HEAD` empty before
starting. (Remotes here are the opposite of skills-dev:
`origin` = GitHub, `gitea` = Gitea.)

### 1. SPEC.md

In the `### search` subsection: add the `--include-queue-ops` row to the
flag table, and extend the **Content extraction** paragraph (currently
ending "Search reaches `role: user` and `role: assistant` messages
only.") to describe queue-op handling per the Contract above. Keep it
tight — mirror the density of the existing search prose.

### 2. Fixtures

Add a `12-queue-operation/` scenario under `shared/corpus/search/` via
the generator (`shared/generate_search_corpus.py` — extend it; do not
hand-write fixtures). The fixture session should contain:

- A plain-text `enqueue` (the recall target).
- A `<task-notification>` `enqueue` (proves no-filter: included when the
  flag is on).
- A `remove` and a `dequeue` (no content — proves they're skipped).
- A regular `role: assistant` message (proves `--role assistant` excludes
  queue-ops even with the flag on).

Sibling `expected.json` with at least these flag combos:

- `default` (no `--include-queue-ops`) → queue-ops absent (current
  behavior, regression guard).
- `--include-queue-ops` → both content-bearing enqueues present as
  `role: user`, remove/dequeue absent.
- `--include-queue-ops --role assistant` → queue-ops absent, only the
  assistant message.

Regenerate expected values with the generator; never hand-edit.

### 3. Conformance (TDD signal)

Extend `shared/conformance.py` so the new scenario + flag combos are
asserted. It will fail for all four impls — that's the signal. Mirror the
existing search dispatcher; structural compare ignoring `elapsed_ms` /
`files_walked`.

### 4. Rust reference impl

- `rust/src/content.rs`: add a helper to extract text from a queue-op
  entry — `entry.content` (root string), distinct from the
  `message.content` path. Likely `extract_queue_op_text(entry: &Value) ->
  Option<String>` returning `None` for empty/missing content.
- `rust/src/search.rs`: the per-line scan recognizes
  `entry["type"] == "queue-operation"`; **only when the
  `--include-queue-ops` flag is set**, emit a `ScanMessage` with
  `role = "user"`, `text = extract_queue_op_text(...)`, `timestamp` from
  the root. Skip if no content. The flag plumbs through `SearchArgs`
  (clap derive) → the scan loop.
- `rust/src/main.rs`: no routing change (still the `search` subcommand);
  just the new flag in the args struct.
- Pass `python shared/conformance.py rust` from the worktree (new
  scenario + all pre-existing green). Commit: `search: --include-queue-ops
  (rust reference)`.

### 5. STOP — diff review

PR-shaped commit on the branch. Get a look before fanning out.

### 6. Fan out C++ / Go / Zig (parallel subagents)

One subagent per language, dispatched from the worktree. Each gets:

- This plan + the **Contract** and **Findings** sections (so they don't
  re-investigate the corpus).
- The Rust impl (`rust/src/search.rs`, `rust/src/content.rs`) as the
  reference.
- Conformance bar: `python shared/conformance.py <lang>` green on the new
  scenario + all existing; add `"<lang>"` to the search allow-list if
  gated.
- Hard constraints: don't touch other impls, the spec, the fixtures, or
  the harness except the allow-list line. Surface spec ambiguity, don't
  guess.

Per-language note on extraction: each impl already has its content
extractor for `message.content` (loose `Value`/`Node`/manual scan). Add
the queue-op branch alongside: when `type == "queue-operation"` and the
flag is on, read root `content`. Go (`sonic.Node`), C++
(`nlohmann`/simdjson on-demand), Zig (manual field scan) — same shape as
their existing per-line dispatch.

Commits: `search: --include-queue-ops (go impl)`, etc.

### 7. Merge + reconform

Merge each with `--no-ff`; rerun `python shared/conformance.py rust cpp go
zig` against the merged tree. All green before proceeding.

### 8. Rebuild + reinstall the C++ binary; update the MCP shim

**The MCP shim subprocesses the installed binary** (`~/.local/bin/
claude-walker.exe`), so the feature isn't live for the MCP tool until the
production C++ binary is rebuilt and reinstalled:

```bash
bash install.sh        # or install.bat — rebuilds cpp + copies to ~/.local/bin
~/.local/bin/claude-walker.exe --version   # confirm rebuilt
```

Then in `mcp/server.py`: add `include_queue_ops: bool = False` to the
`agent_walker_search` tool signature + docstring, and map it to the Claude
provider's `--include-queue-ops` argument (mirror `include_tool_blocks`).
Update the `## MCP shim` section's parameter list in `SPEC.md`. Smoke-test
the shim (the previous session's pattern: instantiate `create_mcp_server`,
`list_tools`, call with `include_queue_ops=True` against a known queued
message, confirm a `role: user` hit comes back).

### 9. Commit + push

Push the branch to both `origin` (GitHub) and `gitea`. Author as Matt
Schoen. Delete this plan file + (if still present) revisit the stale
`docs/superpowers/{specs,plans}/beacon-pairing-fix.md` pair — they look
like already-shipped scaffolding (the pairing fix is in SPEC.md). Confirm
before deleting those; they're not part of this feature.

## Verification bar (before declaring done)

- [ ] `python shared/conformance.py rust cpp go zig` green incl. the
      `12-queue-operation` scenario.
- [ ] Default search unchanged (regression: queue-ops absent without the
      flag).
- [ ] `--include-queue-ops` surfaces a real queued message from the live
      corpus (end-to-end, not just fixtures). Good test query: search a
      session known to have plain-text enqueues with and without the flag;
      the flag should add the queued message as a `role: user` hit.
- [ ] C++ binary rebuilt + reinstalled; MCP shim smoke-passes with
      `include_queue_ops=True`.

## File index

- Contract home: `SPEC.md` → `### search` + `## MCP shim`.
- Search logic: `rust/src/search.rs`, `rust/src/content.rs` (reference);
  `cpp/search.{cpp,hpp}`, `go/search.go`, `zig/src/search.zig`.
- Fixtures: `shared/corpus/search/` + `shared/generate_search_corpus.py`.
- Conformance: `shared/conformance.py`.
- MCP shim: `mcp/server.py`.
- Install (rebuilds cpp): `install.sh` / `install.bat`.

## First message to send the new session

> Picking up the `--include-queue-ops` search feature. Plan at
> `docs/superpowers/plans/queue-op-search.md` — contract + corpus findings
> are settled, don't re-investigate the transcript shape. Verifying a
> clean worktree on `feature/queue-op-search`, then starting with the
> SPEC.md contract change + the `12-queue-operation` fixture (TDD: extend
> conformance to fail first). Will return for diff review after the Rust
> reference impl, before fanning out C++/Go/Zig.
