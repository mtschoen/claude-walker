// Beacon-mode subcommands: beacons-latest and beacons-history.
// See ../SPEC.md "Subcommands" for the contract.
package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/bytedance/sonic"
)

var beaconRegex = regexp.MustCompile(`(?s)<progress-beacon>\s*(\{.*?\})\s*</progress-beacon>`)

type beaconEntry struct {
	EntryType string         `json:"type"`
	Timestamp string         `json:"timestamp"`
	Message   *beaconMessage `json:"message"`
}

// beaconMessage uses json.RawMessage for Content because real-world
// transcripts have message.content as either an array of content-blocks
// OR a bare string. Strictly-typed []contentBlock silently fails to
// deserialize the bare-string variants (sonic errors -> entry skipped),
// which drops real user prompts from the idle calculation.
type beaconMessage struct {
	Role    string          `json:"role"`
	Content json.RawMessage `json:"content"`
}

type contentBlock struct {
	Type string `json:"type"`
	Text string `json:"text"`
}

type beacon struct {
	Kind       string  `json:"kind"`
	EtaSeconds float64 `json:"eta_seconds"`
	Summary    string  `json:"summary"`
	// Optional per SPEC: nil (and so omitted) when the source beacon lacked
	// drift; the pointer keeps presence distinct from an empty value, matching
	// rust's Option<String> / cpp's has_drift.
	Drift     *string `json:"drift,omitempty"`
	BeatsLeft *int64  `json:"beats_left,omitempty"`
}

type rawBeacon struct {
	Kind       *string  `json:"kind"`
	EtaSeconds *float64 `json:"eta_seconds"`
	Summary    *string  `json:"summary"`
	Drift      *string  `json:"drift"`
	BeatsLeft  *int64   `json:"beats_left"`
}

func (rb *rawBeacon) toBeacon() (*beacon, bool) {
	if rb.Kind == nil || rb.Summary == nil {
		return nil, false
	}
	// eta_seconds is required for begin/report but optional for end
	// (defaults to 0): agents routinely omit it on end beacons, and
	// rejecting those left lifecycles permanently open. SPEC beacons-latest.
	eta := 0.0
	switch {
	case rb.EtaSeconds != nil:
		eta = *rb.EtaSeconds
	case *rb.Kind != "end":
		return nil, false
	}
	return &beacon{
		Kind:       *rb.Kind,
		EtaSeconds: eta,
		Summary:    *rb.Summary,
		Drift:      rb.Drift, // nil when absent -> omitted on output
		BeatsLeft:  rb.BeatsLeft,
	}, true
}

// firstNonSpaceByte returns the first non-whitespace byte of raw or 0
// when the payload is empty/whitespace. Used to dispatch on content shape.
func firstNonSpaceByte(raw json.RawMessage) byte {
	for _, b := range raw {
		switch b {
		case ' ', '\t', '\n', '\r':
			continue
		default:
			return b
		}
	}
	return 0
}

// extractText concatenates text blocks from an array-shaped content.
// Returns empty string if content is a bare string or non-array.
func extractText(content json.RawMessage) string {
	if firstNonSpaceByte(content) != '[' {
		return ""
	}
	var blocks []contentBlock
	if err := sonic.Unmarshal(content, &blocks); err != nil {
		return ""
	}
	parts := make([]string, 0, len(blocks))
	for _, b := range blocks {
		if b.Type == "text" {
			parts = append(parts, b.Text)
		}
	}
	return strings.Join(parts, "\n")
}

// userContentIsToolResult returns true iff content is a JSON array and
// any block has type == "tool_result". Tool-result entries are tagged
// type: "user" in JSONL but represent agent-active time, not user idle.
func userContentIsToolResult(content json.RawMessage) bool {
	if firstNonSpaceByte(content) != '[' {
		return false
	}
	var blocks []struct {
		Type string `json:"type"`
	}
	if err := sonic.Unmarshal(content, &blocks); err != nil {
		return false
	}
	for _, b := range blocks {
		if b.Type == "tool_result" {
			return true
		}
	}
	return false
}

type beaconWithTimestamp struct {
	beacon    beacon
	timestamp float64
}

// event is one entry in a session timeline. isRealUser is true only when
// the entry has type: "user" AND content is NOT a tool_result array.
type event struct {
	timestamp  float64
	isRealUser bool
}

func findLatestInPath(path string) (*beaconWithTimestamp, bool) {
	file, err := os.Open(path)
	if err != nil {
		return nil, false
	}
	defer file.Close()

	scanner := bufio.NewScanner(file)
	buf := make([]byte, 0, 64*1024)
	scanner.Buffer(buf, 4*1024*1024)

	var latest *beaconWithTimestamp
	for scanner.Scan() {
		// scanner.Bytes() aliases the buffer; entry.Message.Content (the only
		// RawMessage) is consumed before the next Scan(), so aliasing is safe.
		line := bytes.TrimSpace(scanner.Bytes())
		if len(line) == 0 {
			continue
		}
		var entry beaconEntry
		if err := sonic.Unmarshal(line, &entry); err != nil {
			continue
		}
		if entry.Message == nil || entry.Message.Role != "assistant" {
			continue
		}
		if len(entry.Message.Content) == 0 {
			continue
		}
		if entry.Timestamp == "" {
			continue
		}
		ts, ok := parseISO8601(entry.Timestamp)
		if !ok {
			continue
		}
		combined := extractText(entry.Message.Content)
		matches := beaconRegex.FindAllStringSubmatch(combined, -1)
		var entryBeacon *beacon
		for _, m := range matches {
			// beaconRegex has exactly one capture group, so every match
			// row has len 2; index m[1] directly.
			var rb rawBeacon
			if err := sonic.Unmarshal([]byte(m[1]), &rb); err != nil {
				continue
			}
			if b, ok := rb.toBeacon(); ok {
				entryBeacon = b
			}
		}
		if entryBeacon == nil {
			continue
		}
		if latest == nil || ts >= latest.timestamp {
			latest = &beaconWithTimestamp{beacon: *entryBeacon, timestamp: ts}
		}
	}
	if latest == nil {
		return nil, false
	}
	return latest, true
}

type sessionEvents struct {
	beacons []beaconWithTimestamp
	events  []event
}

// collectSessionEventsInPath walks one transcript and collects beacons +
// per-entry events for the idle-gap calc. Events are NOT sorted; callers
// concatenate across the session group and sort once.
func collectSessionEventsInPath(path string) sessionEvents {
	var out sessionEvents
	file, err := os.Open(path)
	if err != nil {
		return out
	}
	defer file.Close()

	scanner := bufio.NewScanner(file)
	buf := make([]byte, 0, 64*1024)
	scanner.Buffer(buf, 4*1024*1024)

	for scanner.Scan() {
		// scanner.Bytes() aliases the buffer; entry.Message.Content (the only
		// RawMessage) is consumed before the next Scan(), so aliasing is safe.
		line := bytes.TrimSpace(scanner.Bytes())
		if len(line) == 0 {
			continue
		}
		var entry beaconEntry
		if err := sonic.Unmarshal(line, &entry); err != nil {
			continue
		}
		if entry.Timestamp == "" {
			continue
		}
		ts, ok := parseISO8601(entry.Timestamp)
		if !ok {
			continue
		}
		if entry.EntryType == "user" {
			var contentRaw json.RawMessage
			if entry.Message != nil {
				contentRaw = entry.Message.Content
			}
			isRealUser := !userContentIsToolResult(contentRaw)
			out.events = append(out.events, event{timestamp: ts, isRealUser: isRealUser})
			continue
		}
		if entry.Message == nil || entry.Message.Role != "assistant" {
			continue
		}
		if len(entry.Message.Content) == 0 {
			continue
		}
		out.events = append(out.events, event{timestamp: ts, isRealUser: false})
		combined := extractText(entry.Message.Content)
		matches := beaconRegex.FindAllStringSubmatch(combined, -1)
		for _, m := range matches {
			// beaconRegex has exactly one capture group, so every match
			// row has len 2; index m[1] directly.
			var rb rawBeacon
			if err := sonic.Unmarshal([]byte(m[1]), &rb); err != nil {
				continue
			}
			if b, ok := rb.toBeacon(); ok {
				out.beacons = append(out.beacons, beaconWithTimestamp{
					beacon: *b, timestamp: ts,
				})
			}
		}
	}
	return out
}

// computeIdleInWindow sums the portion of [lo, hi] occupied by gaps that
// immediately precede a real-user event. events MUST be sorted ascending.
func computeIdleInWindow(events []event, lo, hi float64) float64 {
	if len(events) < 2 {
		return 0
	}
	var idle float64
	for i := 1; i < len(events); i++ {
		if !events[i].isRealUser {
			continue
		}
		gapLo := math.Max(events[i-1].timestamp, lo)
		gapHi := math.Min(events[i].timestamp, hi)
		if gapHi > gapLo {
			idle += gapHi - gapLo
		}
	}
	return idle
}

// === beacons-latest ===

type latestArguments struct {
	sessionID          string
	projectsRoot       string
	extraProjectsRoots []string
	readConfig         bool
	nowUnix            float64
	nowSet             bool
}

func parseLatestArguments(args []string) (latestArguments, error) {
	parsed := latestArguments{readConfig: true}
	i := 0
	for i < len(args) {
		flag := args[i]
		switch flag {
		case "--session-id":
			if i+1 >= len(args) {
				return parsed, fmt.Errorf("--session-id needs a value")
			}
			parsed.sessionID = args[i+1]
			i += 2
		case "--projects-root":
			if i+1 >= len(args) {
				return parsed, fmt.Errorf("--projects-root needs a value")
			}
			parsed.projectsRoot = args[i+1]
			i += 2
		case "--extra-projects-root":
			if i+1 >= len(args) {
				return parsed, fmt.Errorf("--extra-projects-root needs a value")
			}
			parsed.extraProjectsRoots = append(parsed.extraProjectsRoots, args[i+1])
			i += 2
		case "--no-config":
			parsed.readConfig = false
			i++
		case "--now":
			if i+1 >= len(args) {
				return parsed, fmt.Errorf("--now needs a value")
			}
			value, err := strconv.ParseFloat(args[i+1], 64)
			if err != nil {
				return parsed, fmt.Errorf("--now: %v", err)
			}
			parsed.nowUnix = value
			parsed.nowSet = true
			i += 2
		default:
			return parsed, fmt.Errorf("unknown flag: %s", flag)
		}
	}
	if parsed.sessionID == "" {
		return parsed, fmt.Errorf("--session-id is required")
	}
	return parsed, nil
}

// discoverLatestPaths locates the transcript(s) for a single session id by
// walking the directory tree directly instead of compiling two filepath.Glob
// patterns. Mirrors the glob semantics it replaces:
//
//	parent:   <root>/*/<session_id>.jsonl
//	subagent: <root>/*/*/subagents/agent-<session_id>.jsonl
//
// The parent form is probed with a single os.Stat per slug dir rather than
// listing each slug dir's contents the way filepath.Glob does, which is the
// bulk of the win on a large fleet. Result order is irrelevant: the caller
// keeps the highest-timestamp beacon.
func discoverLatestPaths(roots []string, sessionID string) []string {
	parentName := sessionID + ".jsonl"
	subName := "agent-" + sessionID + ".jsonl"
	var paths []string
	for _, root := range roots {
		slugEntries, err := os.ReadDir(root)
		if err != nil {
			continue
		}
		for _, slugEntry := range slugEntries {
			if !slugEntry.IsDir() {
				continue
			}
			slugPath := filepath.Join(root, slugEntry.Name())
			// Parent transcript: <root>/<slug>/<session_id>.jsonl
			parent := filepath.Join(slugPath, parentName)
			if info, err := os.Stat(parent); err == nil && !info.IsDir() {
				paths = append(paths, parent)
			}
			// Subagent transcripts live one level deeper, under each session
			// directory's subagents/ folder.
			sessionEntries, err := os.ReadDir(slugPath)
			if err != nil {
				continue
			}
			for _, sessionEntry := range sessionEntries {
				if !sessionEntry.IsDir() {
					continue
				}
				sub := filepath.Join(slugPath, sessionEntry.Name(), "subagents", subName)
				if info, err := os.Stat(sub); err == nil && !info.IsDir() {
					paths = append(paths, sub)
				}
			}
		}
	}
	return paths
}

func runBeaconsLatest(args []string) {
	started := time.Now()
	parsed, err := parseLatestArguments(args)
	if err != nil {
		fmt.Fprintf(os.Stderr, "walker: beacons-latest: %v\n", err)
		os.Exit(2)
	}
	primary := parsed.projectsRoot
	if primary == "" {
		primary = defaultProjectsRoot()
	}
	nowUnix := parsed.nowUnix
	if !parsed.nowSet {
		nowUnix = float64(time.Now().UnixNano()) / 1e9
	}

	roots := ResolveRoots(primary, parsed.extraProjectsRoots, parsed.readConfig)
	paths := discoverLatestPaths(roots, parsed.sessionID)

	var best *beaconWithTimestamp
	for _, path := range paths {
		if found, ok := findLatestInPath(path); ok {
			if best == nil || found.timestamp > best.timestamp {
				best = found
			}
		}
	}

	elapsedMS := uint64(time.Since(started).Milliseconds())

	if best == nil {
		fmt.Printf(
			"{\"beacon\":null,\"emitted_at\":null,\"age_seconds\":null,\"elapsed_ms\":%d}\n",
			elapsedMS,
		)
		return
	}
	// beacon is a fixed struct of strings/numbers; Marshal cannot fail.
	beaconJSON, _ := sonic.Marshal(best.beacon)
	age := nowUnix - best.timestamp
	fmt.Printf(
		"{\"beacon\":%s,\"emitted_at\":%s,\"age_seconds\":%s,\"elapsed_ms\":%d}\n",
		string(beaconJSON),
		formatFloat(best.timestamp),
		formatFloat(age),
		elapsedMS,
	)
}

func formatFloat(value float64) string {
	return strconv.FormatFloat(value, 'f', -1, 64)
}

// === beacons-history ===

type historyArguments struct {
	periodSeconds      uint64
	winStartUnix       float64
	projectsRoot       string
	extraProjectsRoots []string
	readConfig         bool
	nowUnix            float64
	nowSet             bool
}

func parseHistoryArguments(args []string) (historyArguments, error) {
	parsed := historyArguments{readConfig: true}
	periodSet := false
	i := 0
	for i < len(args) {
		flag := args[i]
		switch flag {
		case "--period":
			if i+1 >= len(args) {
				return parsed, fmt.Errorf("--period needs a value")
			}
			value, err := strconv.ParseUint(args[i+1], 10, 64)
			if err != nil {
				return parsed, fmt.Errorf("--period: %v", err)
			}
			parsed.periodSeconds = value
			periodSet = true
			i += 2
		case "--win-start":
			if i+1 >= len(args) {
				return parsed, fmt.Errorf("--win-start needs a value")
			}
			value, err := strconv.ParseFloat(args[i+1], 64)
			if err != nil {
				return parsed, fmt.Errorf("--win-start: %v", err)
			}
			parsed.winStartUnix = value
			i += 2
		case "--projects-root":
			if i+1 >= len(args) {
				return parsed, fmt.Errorf("--projects-root needs a value")
			}
			parsed.projectsRoot = args[i+1]
			i += 2
		case "--extra-projects-root":
			if i+1 >= len(args) {
				return parsed, fmt.Errorf("--extra-projects-root needs a value")
			}
			parsed.extraProjectsRoots = append(parsed.extraProjectsRoots, args[i+1])
			i += 2
		case "--no-config":
			parsed.readConfig = false
			i++
		case "--now":
			if i+1 >= len(args) {
				return parsed, fmt.Errorf("--now needs a value")
			}
			value, err := strconv.ParseFloat(args[i+1], 64)
			if err != nil {
				return parsed, fmt.Errorf("--now: %v", err)
			}
			parsed.nowUnix = value
			parsed.nowSet = true
			i += 2
		default:
			return parsed, fmt.Errorf("unknown flag: %s", flag)
		}
	}
	if !periodSet {
		return parsed, fmt.Errorf("--period is required")
	}
	return parsed, nil
}

// discoverHistoryGroups groups transcripts by (slug, session_id) across all
// roots without the mtime filter -- beacon entries can sit deep in a long
// transcript. Same (slug, session_id) on two roots merges into one group.
func discoverHistoryGroups(roots []string) map[groupKey][]string {
	groups := make(map[groupKey][]string)

	for _, root := range roots {
		parentGlob := filepath.Join(root, "*", "*.jsonl")
		parentMatches, err := filepath.Glob(parentGlob)
		if err == nil {
			for _, path := range parentMatches {
				// Glob matches directories named *.jsonl too; parents must
				// be regular files (SPEC Discovery).
				info, statErr := os.Stat(path)
				if statErr != nil || !info.Mode().IsRegular() {
					continue
				}
				slug := filepath.Base(filepath.Dir(path))
				sessionID := strings.TrimSuffix(filepath.Base(path), ".jsonl")
				key := groupKey{slug: slug, sessionID: sessionID}
				groups[key] = append(groups[key], path)
			}
		}

		slugEntries, err := os.ReadDir(root)
		if err != nil {
			continue
		}
		for _, slugEntry := range slugEntries {
			if !slugEntry.IsDir() {
				continue
			}
			slugPath := filepath.Join(root, slugEntry.Name())
			sessionEntries, err := os.ReadDir(slugPath)
			if err != nil {
				continue
			}
			for _, sessionEntry := range sessionEntries {
				if !sessionEntry.IsDir() {
					continue
				}
				subagentsDir := filepath.Join(slugPath, sessionEntry.Name(), "subagents")
				subEntries, err := os.ReadDir(subagentsDir)
				if err != nil {
					continue
				}
				for _, subEntry := range subEntries {
					name := subEntry.Name()
					if subEntry.IsDir() || !strings.HasPrefix(name, "agent-") || !strings.HasSuffix(name, ".jsonl") {
						continue
					}
					path := filepath.Join(subagentsDir, name)
					key := groupKey{
						slug:      slugEntry.Name(),
						sessionID: sessionEntry.Name(),
					}
					groups[key] = append(groups[key], path)
				}
			}
		}
	}

	return groups
}

// historyPair carries the four elapsed values per begin/end pair.
type historyPair struct {
	beginEta      float64
	actualElapsed float64
	idleExcluded  float64
	activeElapsed float64
}

// biasFactor returns the median of (active/eta) ratios. Pairs with eta<=0
// are excluded. Returns (0, false) when no usable pairs exist.
func biasFactor(pairs []historyPair) (float64, bool) {
	if len(pairs) == 0 {
		return 0, false
	}
	ratios := make([]float64, 0, len(pairs))
	for _, p := range pairs {
		if p.beginEta > 0 {
			ratios = append(ratios, p.activeElapsed/p.beginEta)
		}
	}
	if len(ratios) == 0 {
		return 0, false
	}
	sort.Float64s(ratios)
	n := len(ratios)
	if n%2 == 1 {
		return ratios[n/2], true
	}
	return (ratios[n/2-1] + ratios[n/2]) / 2.0, true
}

func runBeaconsHistory(args []string) {
	started := time.Now()
	parsed, err := parseHistoryArguments(args)
	if err != nil {
		fmt.Fprintf(os.Stderr, "walker: beacons-history: %v\n", err)
		os.Exit(2)
	}
	nowUnix := parsed.nowUnix
	if !parsed.nowSet {
		nowUnix = float64(time.Now().UnixNano()) / 1e9
	}
	periodCutoff := nowUnix - float64(parsed.periodSeconds)
	windowLo := periodCutoff
	if parsed.winStartUnix > windowLo {
		windowLo = parsed.winStartUnix
	}
	primary := parsed.projectsRoot
	if primary == "" {
		primary = defaultProjectsRoot()
	}
	roots := ResolveRoots(primary, parsed.extraProjectsRoots, parsed.readConfig)

	groups := discoverHistoryGroups(roots)
	sessionCount := uint64(len(groups))

	// Flatten group paths for the worker pool. Group identity does not
	// matter past this point: pair order doesn't matter (conformance
	// sorts pairs before comparing), so we don't need a stable key.
	groupSlices := make([][]string, 0, len(groups))
	for _, paths := range groups {
		groupSlices = append(groupSlices, paths)
	}

	// Parallel per-group walk. Work unit = one session group; each worker
	// owns a local pairs slice merged after all workers exit. The shared
	// beaconRegex is safe for concurrent use (regexp.Regexp is documented
	// thread-safe for its Find/Match methods). sonic.Unmarshal is also
	// documented thread-safe. Mirrors the cost-mode pattern in main.go.
	numWorkers := effectiveWorkerCount(runtime.NumCPU())

	work := make(chan []string, len(groupSlices))
	perWorkerPairs := make([][]historyPair, numWorkers)

	var wg sync.WaitGroup
	for workerIndex := 0; workerIndex < numWorkers; workerIndex++ {
		wg.Add(1)
		go func(tid int) {
			defer wg.Done()
			local := perWorkerPairs[tid][:0]
			for paths := range work {
				var beaconsAll []beaconWithTimestamp
				var eventsAll []event
				for _, path := range paths {
					se := collectSessionEventsInPath(path)
					beaconsAll = append(beaconsAll, se.beacons...)
					eventsAll = append(eventsAll, se.events...)
				}
				sort.Slice(eventsAll, func(i, j int) bool {
					return eventsAll[i].timestamp < eventsAll[j].timestamp
				})
				var inside []beaconWithTimestamp
				for _, b := range beaconsAll {
					if b.timestamp >= windowLo {
						inside = append(inside, b)
					}
				}

				// Iterate in timestamp order (stable), tracking one in-flight
				// pending begin: emit one pair per properly-closed begin->end
				// lifecycle. Replaces the old earliest-begin/latest-end rule.
				sort.SliceStable(inside, func(i, j int) bool {
					return inside[i].timestamp < inside[j].timestamp
				})
				var pendingBegin *beaconWithTimestamp
				for index := range inside {
					b := &inside[index]
					switch b.beacon.Kind {
					case "begin":
						pendingBegin = b // orphans any prior pending begin
					case "end":
						if pendingBegin != nil && b.timestamp > pendingBegin.timestamp {
							wall := b.timestamp - pendingBegin.timestamp
							idle := computeIdleInWindow(eventsAll, pendingBegin.timestamp, b.timestamp)
							// idle sums gaps clipped to [begin,end], so it
							// can never exceed wall.
							active := wall - idle
							local = append(local, historyPair{
								beginEta:      pendingBegin.beacon.EtaSeconds,
								actualElapsed: wall,
								idleExcluded:  idle,
								activeElapsed: active,
							})
							pendingBegin = nil
						}
					}
				}
			}
			perWorkerPairs[tid] = local
		}(workerIndex)
	}
	for _, paths := range groupSlices {
		work <- paths
	}
	close(work)
	wg.Wait()

	// Merge per-worker pairs
	total := 0
	for _, v := range perWorkerPairs {
		total += len(v)
	}
	pairs := make([]historyPair, 0, total)
	for _, v := range perWorkerPairs {
		pairs = append(pairs, v...)
	}

	bias, biasOK := biasFactor(pairs)
	elapsedMS := uint64(time.Since(started).Milliseconds())

	var pairsBuilder strings.Builder
	pairsBuilder.WriteString("[")
	for index, p := range pairs {
		if index > 0 {
			pairsBuilder.WriteString(",")
		}
		fmt.Fprintf(&pairsBuilder,
			"{\"begin_eta\":%s,\"actual_elapsed\":%s,\"idle_excluded\":%s,\"active_elapsed\":%s}",
			formatFloat(p.beginEta),
			formatFloat(p.actualElapsed),
			formatFloat(p.idleExcluded),
			formatFloat(p.activeElapsed),
		)
	}
	pairsBuilder.WriteString("]")

	biasJSON := "null"
	if biasOK {
		biasJSON = formatFloat(bias)
	}

	fmt.Printf(
		"{\"pairs\":%s,\"session_count\":%d,\"n_pairs\":%d,\"bias_factor\":%s,\"elapsed_ms\":%d}\n",
		pairsBuilder.String(),
		sessionCount,
		len(pairs),
		biasJSON,
		elapsedMS,
	)
}
