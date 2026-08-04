# agent-walker (formerly claude-walker)

Fast native walking for Claude Code statusline data, plus provider-neutral
historical session search. The native walker is implemented side-by-side in
**Rust**, **C++**, **Zig**, and **Go** as a language comparison.

## What it does

Walks `~/.claude/projects/**/*.jsonl` (parent transcripts + their
`subagents/agent-*.jsonl` siblings), filters to a time range, dedupes
assistant turns by `message.id`, and prices each turn against the canonical
Anthropic per-MTok rate table. Emits two dollar totals on stdout.

The `agent-walker` MCP server is broader: `agent_walker_search` searches Claude
Code JSONLs and OpenCode's SQLite session store by default, labels every hit by
provider, and merges results newest-first. This aggregation is intentionally
limited to historical recall. OpenCode sessions do not enter Claude statusline
costs, events, progress beacons, or active-session calculations.

OpenCode discovery uses `~/.local/share/opencode/opencode.db`. Override it with
the tool's `opencode_db` parameter or `AGENT_WALKER_OPENCODE_DB`. The provider
opens SQLite read-only and validates the `session`, `message`, and `part`
columns it consumes. See [`docs/provider-roadmap.md`](docs/provider-roadmap.md)
for the Pi, Hermes, Agy, and MCP-level Codex aggregation phases.

OpenCode search currently supports literal substring matching. For `regex=true`,
the generic tool still searches Claude with its native RE2 engine and reports
OpenCode as unavailable rather than substituting Python's potentially
backtracking regex engine.

The walker reads a configurable set of project roots (default
`~/.claude/projects` plus any extras listed in `~/.claude/walker-roots.json`).
Search also includes `~/.codex/sessions` by default and accepts tagged Codex
roots in that config, so the same subscription used from multiple machines can
be aggregated by the host that has filesystem access to all of them. See
"Multi-host setup" below and the `## Roots` section in [`SPEC.md`](SPEC.md).

Drop-in optional speedup for [schoen-claude-status](https://github.com/mtschoen/schoen-claude-status)
(the Python statusline does this in ~250ms with an `orjson` +
`ProcessPoolExecutor` walk; the goal here is **30-80ms**).

## Multi-host setup

The sliding-window pace projection only works correctly if the walker
sees every transcript billed to the subscription. When the same plan is
used from two machines, neither machine's `~/.claude/projects` is a
complete picture on its own.

Make the other machine's transcripts reachable via the filesystem
(SMB/NFS mount, sync, whatever), then point the walker at the mounted
path via `~/.claude/walker-roots.json`:

```json
{
  "extra_roots": [
    "/mnt/other-host/Users/mtsch/.claude/projects",
    {
      "path": "/mnt/other-host/Users/mtsch/.codex/sessions",
      "format": "codex"
    }
  ]
}
```

The file is per-host and lives outside any synced memory — chonkers'
config points at llamabox's mount, llamabox's config points at chonkers'.

**Latency caveat.** Walking JSONLs over a remote mount is slower than
the local case. On a representative Windows-→-Linux SMB mount the
cost-mode walk goes from ~200ms (local-only) to ~3s (with the SMB extra
root). The statusline pace cache (15s TTL) absorbs the cost so the user
sees one slow render per cache window, but if the remote host is asleep
or the mount times out, the walker still exits 0 with primary-only
totals — it never blocks the caller indefinitely.

## Why four languages

The work is small, well-defined, CPU-bound, parallelizable, and
performance-sensitive — a tight benchmark for "what does it actually feel
like to ship a fast CLI in $LANG." Comparison axes are tracked in
[`RESULTS.md`](RESULTS.md): runtime, binary size, lines of code, build
time, distribution friction.

## Layout

```
shared/
  corpus/        JSONL fixtures with expected outputs (the ground truth)
  conformance.py runs each binary, asserts ±$0.01 agreement
  bench.py       times each binary on the live ~/.claude/projects fleet
rust/            cargo, simd-json or sonic-rs
go/              go modules, encoding/json or sonic-go
cpp/             cmake, simdjson
zig/             build.zig, std.json or hand-rolled
```

See [`SPEC.md`](SPEC.md) for the CLI contract every implementation must
honor and the correctness rules they all share.

## Status

The C++ walker is the installed production binary; Rust, Go, and Zig remain
conformance-tested comparison implementations. Native cost, event, beacon, and
search modes are implemented across all four. The MCP server adds
provider-neutral historical recall for Claude Code and OpenCode; remaining
providers are tracked in [`docs/provider-roadmap.md`](docs/provider-roadmap.md).
