claude-walker test report — 2026-07-16T18:44:30+00:00
============================================================

Status:       PASS (100% all impls)
Conformance:  PASS (995 checks across measured impls)
Cumulative:   100.00% line/statement coverage (8039/8039 pooled across 4 impls)
Git:          61e4d1d (feat/cross-agent-session-search)
Target:       100% line/statement coverage in all four implementations

Per-implementation coverage
------------------------------------------------------------
impl   metric         covered/total    cover   conformance
rust   lines              1660/1660  100.00%   PASS (249 ok)
cpp    lines              2869/2869  100.00%   PASS (249 ok)
go     statements         1330/1330  100.00%   PASS (249 ok)
zig    lines              2180/2180  100.00%   PASS (248 ok)

### rust — 100.00% (0 lines uncovered)
    beacons.rs               375/375   100.00%
    content.rs                66/66    100.00%
    events.rs                166/166   100.00%
    main.rs                  208/208   100.00%
    search.rs                666/666   100.00%
    transcript.rs            107/107   100.00%
    walker_roots.rs           72/72    100.00%

### cpp — 100.00% (0 lines uncovered)
    beacons.cpp              879/879   100.00%
    common.hpp               120/120   100.00%
    cost_walk.hpp            174/174   100.00%
    discovery.hpp             72/72    100.00%
    events.cpp               377/377   100.00%
    json_writer.hpp           46/46    100.00%
    main.cpp                 197/197   100.00%
    pricing.hpp               36/36    100.00%
    search.cpp               874/874   100.00%
    walker_roots.hpp          94/94    100.00%

### go — 100.00% (0 statements uncovered)
    beacons.go               380/380   100.00%
    events.go                130/130   100.00%
    main.go                  274/274   100.00%
    search.go                498/498   100.00%
    walker_roots.go           48/48    100.00%

### zig — 100.00% (0 lines uncovered)
    beacons.zig              496/496   100.00%
    events.zig               197/197   100.00%
    main.zig                 548/548   100.00%
    search.zig               864/864   100.00%
    walker_roots.zig          75/75    100.00%

Regenerate: `python shared/coverage.py`  (see CLAUDE.md)
