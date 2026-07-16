// Events subcommand: emit one NDJSON record per accepted assistant turn.
// Reuses cost-mode's parse/dedup/filter/pricing verbatim (entry/message/usage,
// costForTurn, parseISO8601, discoverGroups, ResolveRoots from main.go and
// walker_roots.go); only aggregation differs (per-turn output instead of
// accumulated totals).
//
// Mirrors rust/src/events.rs and cpp's events handling.
// See ../SPEC.md "events" for the full contract.

package main

import (
	"bufio"
	"encoding/json"
	"fmt"
	"math"
	"os"
	"runtime"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/bytedance/sonic"
)

// eventsArguments mirrors cost-mode arguments, but --win-start is OPTIONAL here
// (defaults to now - period, per SPEC §events) and --extra-projects-root /
// --no-config carry the same semantics as cost mode.
type eventsArguments struct {
	periodSeconds      uint64
	winStartUnix       float64
	nowUnix            float64
	projectsRoot       string
	extraProjectsRoots []string
	readConfig         bool
}

func parseEventsArguments(rawArgs []string) (eventsArguments, error) {
	out := eventsArguments{readConfig: true}
	periodSet := false
	winStartSet := false
	nowSet := false

	for i := 0; i < len(rawArgs); i++ {
		switch rawArgs[i] {
		case "--period":
			if i+1 >= len(rawArgs) {
				return eventsArguments{}, fmt.Errorf("--period needs a value")
			}
			i++
			value, err := parseUint64(rawArgs[i])
			if err != nil {
				return eventsArguments{}, fmt.Errorf("--period: %v", err)
			}
			out.periodSeconds = value
			periodSet = true
		case "--win-start":
			if i+1 >= len(rawArgs) {
				return eventsArguments{}, fmt.Errorf("--win-start needs a value")
			}
			i++
			value, err := parseFloat64(rawArgs[i])
			if err != nil {
				return eventsArguments{}, fmt.Errorf("--win-start: %v", err)
			}
			out.winStartUnix = value
			winStartSet = true
		case "--now":
			if i+1 >= len(rawArgs) {
				return eventsArguments{}, fmt.Errorf("--now needs a value")
			}
			i++
			value, err := parseFloat64(rawArgs[i])
			if err != nil {
				return eventsArguments{}, fmt.Errorf("--now: %v", err)
			}
			out.nowUnix = value
			nowSet = true
		case "--projects-root":
			if i+1 >= len(rawArgs) {
				return eventsArguments{}, fmt.Errorf("--projects-root needs a value")
			}
			i++
			out.projectsRoot = rawArgs[i]
		case "--extra-projects-root":
			if i+1 >= len(rawArgs) {
				return eventsArguments{}, fmt.Errorf("--extra-projects-root needs a value")
			}
			i++
			out.extraProjectsRoots = append(out.extraProjectsRoots, rawArgs[i])
		case "--no-config":
			out.readConfig = false
		case "--version":
			fmt.Println(version)
			os.Exit(0)
		default:
			return eventsArguments{}, fmt.Errorf("unknown flag: %s", rawArgs[i])
		}
	}

	if !periodSet || out.periodSeconds == 0 {
		return eventsArguments{}, fmt.Errorf("--period is required and must be > 0")
	}

	if !nowSet {
		out.nowUnix = float64(time.Now().UnixNano()) / 1e9
	}
	// When --win-start is omitted, default to now - period (simplifies the
	// predicate to ts >= now - period, per SPEC §events).
	if !winStartSet {
		out.winStartUnix = out.nowUnix - float64(out.periodSeconds)
	}
	if out.projectsRoot == "" {
		out.projectsRoot = defaultProjectsRoot()
	}

	return out, nil
}

// eventRecord is one emitted line per accepted assistant turn. Field order
// (ts, usd, model, session_id, slug) matches SPEC §events for line-equality.
type eventRecord struct {
	TS        float64 `json:"ts"`
	USD       float64 `json:"usd"`
	Model     string  `json:"model"`
	SessionID string  `json:"session_id"`
	Slug      string  `json:"slug"`
}

// walkGroupEvents walks one (slug, session) group and collects an eventRecord
// for every accepted assistant turn. Dedup is per-group via a local seen-IDs
// set, exactly matching cost-mode's walkGroup contract.
func walkGroupEvents(paths []string, slug, sessionID string, cutoff float64) []eventRecord {
	var records []eventRecord
	seenIDs := make(map[string]struct{})

	for _, path := range paths {
		file, err := os.Open(path)
		if err != nil {
			continue
		}
		scanner := bufio.NewScanner(file)
		buf := make([]byte, 0, 64*1024)
		scanner.Buffer(buf, 4*1024*1024)

		for scanner.Scan() {
			line := strings.TrimSpace(scanner.Text())
			if line == "" {
				continue
			}

			var e entry
			if err := sonic.Unmarshal([]byte(line), &e); err != nil {
				continue
			}

			msg := e.Message
			if msg == nil || msg.Role != "assistant" {
				continue
			}

			// Dedup by message ID (if present).
			if msg.ID != "" {
				if _, already := seenIDs[string(msg.ID)]; already {
					continue
				}
				seenIDs[string(msg.ID)] = struct{}{}
			}

			// Timestamp must be parseable and in range.
			if e.Timestamp == "" {
				continue
			}
			ts, ok := parseISO8601(string(e.Timestamp))
			if !ok || ts < cutoff {
				continue
			}

			model := strings.ToLower(string(msg.Model))
			usd := costForTurn(msg.Usage, model, string(e.Timestamp))

			records = append(records, eventRecord{
				TS:        ts,
				USD:       usd,
				Model:     model,
				SessionID: sessionID,
				Slug:      slug,
			})
		}
		file.Close()
	}
	return records
}

// runEvents is the entry point for the `events` subcommand. Emits NDJSON on
// stdout (one line per accepted assistant turn) and exits 0 even when empty.
func runEvents(rawArgs []string) {
	args, err := parseEventsArguments(rawArgs)
	if err != nil {
		fmt.Fprintf(os.Stderr, "walker: events: %v\n", err)
		os.Exit(2)
	}

	// Effective cutoff = min(now - period, win_start), per SPEC §events.
	periodCutoff := args.nowUnix - float64(args.periodSeconds)
	cutoff := math.Min(periodCutoff, args.winStartUnix)

	roots := ResolveRoots(args.projectsRoot, args.extraProjectsRoots, args.readConfig)
	if len(roots) == 0 {
		// Primary root doesn't exist — emit nothing, exit 0 (matches cost-mode
		// empty-fleet behavior).
		return
	}

	groups := discoverGroups(roots, cutoff)

	// Collect groups (key + paths) for concurrent processing.
	type groupWork struct {
		slug      string
		sessionID string
		paths     []string
	}
	work := make(chan groupWork, len(groups))

	// runtime.NumCPU is documented to return a value >= 1, so no lower
	// clamp is needed; cap at 8 to avoid runaway parallelism on big hosts.
	// Also shrink to group count when smaller so we don't spawn idle workers.
	numWorkers := runtime.NumCPU()
	if numWorkers > 8 {
		numWorkers = 8
	}
	if numWorkers > len(groups) && len(groups) > 0 {
		numWorkers = len(groups)
	}

	perWorker := make([][]eventRecord, numWorkers)
	var wg sync.WaitGroup
	for workerIndex := 0; workerIndex < numWorkers; workerIndex++ {
		wg.Add(1)
		go func(tid int) {
			defer wg.Done()
			var local []eventRecord
			for gw := range work {
				recs := walkGroupEvents(gw.paths, gw.slug, gw.sessionID, cutoff)
				local = append(local, recs...)
			}
			perWorker[tid] = local
		}(workerIndex)
	}
	for key, paths := range groups {
		work <- groupWork{slug: key.slug, sessionID: key.sessionID, paths: paths}
	}
	close(work)
	wg.Wait()

	// Merge.
	var records []eventRecord
	for _, v := range perWorker {
		records = append(records, v...)
	}

	// Sort for deterministic output: (ts, session_id, model) — matches the
	// multiset tiebreaker in SPEC §events §Ordering.
	sort.Slice(records, func(i, j int) bool {
		if records[i].TS != records[j].TS {
			return records[i].TS < records[j].TS
		}
		if records[i].SessionID != records[j].SessionID {
			return records[i].SessionID < records[j].SessionID
		}
		return records[i].Model < records[j].Model
	})

	// A json.Encoder reuses one internal buffer across records and appends the
	// record terminator itself, so the hot loop avoids the per-record []byte
	// allocation that json.Marshal incurs. Output is byte-identical to
	// `Marshal(record) + '\n'` (same HTML escaping, same float formatting).
	// Encode cannot fail on eventRecord (fixed primitive/string fields, no
	// MarshalJSON, write target is the bufio buffer), so the error is dropped.
	out := bufio.NewWriter(os.Stdout)
	enc := json.NewEncoder(out)
	for i := range records {
		_ = enc.Encode(&records[i])
	}
	out.Flush()
}
