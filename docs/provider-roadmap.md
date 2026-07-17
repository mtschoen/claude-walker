# Agent session provider roadmap

agent-walker has two deliberately separate jobs:

1. Native Claude Code walking for latency-sensitive statusline costs, events,
   and progress beacons.
2. Provider-neutral historical recall through the MCP server.

Only the second job aggregates agents. A provider must never enter another
agent's cost, active-session, event, or beacon calculation merely because it
became searchable.

## Provider contract

Historical-search providers normalize these fields while preserving native
provenance:

- provider and native session/message identifiers
- role, timestamp, searchable text, and neighboring context
- working directory or project identity
- source path/database and source-local row or line identifier
- provider availability, schema diagnostics, and truncation state

Readers are local and read-only. JSONL providers stream records and tolerate a
truncated final line. SQLite providers use read-only connections, validate the
schema they consume, and let SQLite read the live WAL rather than copying only
the main database file.

## Phases

### 1. Claude Code and OpenCode

Claude remains the native baseline. The generic `agent_walker_search` MCP tool
merges native Claude JSONL results with OpenCode's
`~/.local/share/opencode/opencode.db` by default. OpenCode support reads
`session`, `message`, and `part`, searches prose plus optional tool inputs and
outputs, and reports its source database on each result.

The OpenCode MVP supports literal matching. Regex requests continue through
Claude's native RE2 implementation and expose an unavailable OpenCode provider
summary; a future OpenCode regex implementation must also be linear-time.

Claude-only recall uses `agent_walker_search` with `providers=["claude"]`.
No native cost/event/beacon command changes in this phase.

### 2. Pi

Read `~/.pi/agent/sessions/--<cwd>--/*.jsonl`. Honor the versioned session
header, reconstruct `id` / `parentId` trees, and retain compaction and branch
summary entries. Search all stored branches, but identify the active branch in
provenance so results are not mistaken for one linear conversation.

### 3. Codex CLI

Treat `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl` as canonical and include
`archived_sessions` on request. Use SQLite/index files only to enrich metadata;
rollout files remain discoverable when indexes drift. Parse incrementally
because individual rollouts can be very large.

### 4. Hermes Agent

Read `$HERMES_HOME/state.db` (default `~/.hermes/state.db`) using its documented
schema and FTS tables when compatible. Preserve parent-session relationships.
Fall back to scanning normalized message rows if the installed FTS schema does
not match the versions covered by fixtures.

### 5. Agy / Antigravity

Keep this provider experimental until its storage stabilizes. Current clients
may use protobuf caches, SQLite conversation stores, or a live language-server
RPC depending on product/version. Begin with explicit-path discovery and
capabilities (`discovery_only`, `live_rpc_export`, `offline_transcript`) rather
than claiming complete offline history.

### 6. Owned index, only if needed

Direct provider reads are the source of truth. Add an agent-walker SQLite FTS
index only after profiling shows repeated multi-provider scans are too slow.
The index must be disposable, provenance-preserving, and incrementally rebuilt
from native stores; it must not become a second authoritative session store.

## Cross-platform test matrix

Each provider needs fixtures from Windows, macOS, and Linux where its native
paths or serialization differ. Path resolution honors provider environment
overrides (`CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `HERMES_HOME`) and agent-walker
source overrides such as `AGENT_WALKER_OPENCODE_DB`. Native Windows and WSL
stores are separate sources rather than assumed aliases.
