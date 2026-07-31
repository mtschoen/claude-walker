// Native pace-walker -- Go implementation.
// See ../SPEC.md for the contract every implementation must honor.
package main

import (
	"bufio"
	"bytes"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"time"

	"github.com/bytedance/sonic"
)

const version = "go/0.1.1"

const helpText = `claude-walker - fast cost & progress walker over Claude Code transcripts

USAGE:
    claude-walker [SUBCOMMAND] [OPTIONS]

With no subcommand it runs ` + "`cost`" + ` (back-compat for the status line).

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
`

func isHelpFlag(s string) bool {
	return s == "-h" || s == "--help"
}

// wantsHelp reports whether to show the overview: no args, or first arg is
// -h/--help, or first arg is a known subcommand followed by -h/--help.
// See SPEC.md "Help & usage".
func wantsHelp(raw []string) bool {
	if len(raw) == 0 {
		return true
	}
	if isHelpFlag(raw[0]) {
		return true
	}
	switch raw[0] {
	case "cost", "beacons-latest", "beacons-history", "search", "events":
		return len(raw) > 1 && isHelpFlag(raw[1])
	}
	return false
}

// CLI arguments.
type arguments struct {
	periodSeconds      uint64
	winStartUnix       float64
	nowUnix            float64
	projectsRoot       string
	extraProjectsRoots []string
	readConfig         bool
}

func parseArguments(rawArgs []string) (arguments, error) {
	// Manual parse loop so we can support repeatable --extra-projects-root
	// and the boolean --no-config flag alongside the standard ones. Go's
	// stdlib `flag` package doesn't have a repeatable-string flavor without
	// a custom Value, so a hand-rolled loop is cleaner.
	out := arguments{readConfig: true}
	periodSet := false
	winStartSet := false
	nowSet := false

	for i := 0; i < len(rawArgs); i++ {
		switch rawArgs[i] {
		case "--period":
			if i+1 >= len(rawArgs) {
				return arguments{}, fmt.Errorf("--period needs a value")
			}
			i++
			value, err := parseUint64(rawArgs[i])
			if err != nil {
				return arguments{}, fmt.Errorf("--period: %v", err)
			}
			out.periodSeconds = value
			periodSet = true
		case "--win-start":
			if i+1 >= len(rawArgs) {
				return arguments{}, fmt.Errorf("--win-start needs a value")
			}
			i++
			value, err := parseFloat64(rawArgs[i])
			if err != nil {
				return arguments{}, fmt.Errorf("--win-start: %v", err)
			}
			out.winStartUnix = value
			winStartSet = true
		case "--now":
			if i+1 >= len(rawArgs) {
				return arguments{}, fmt.Errorf("--now needs a value")
			}
			i++
			value, err := parseFloat64(rawArgs[i])
			if err != nil {
				return arguments{}, fmt.Errorf("--now: %v", err)
			}
			out.nowUnix = value
			nowSet = true
		case "--projects-root":
			if i+1 >= len(rawArgs) {
				return arguments{}, fmt.Errorf("--projects-root needs a value")
			}
			i++
			out.projectsRoot = rawArgs[i]
		case "--extra-projects-root":
			if i+1 >= len(rawArgs) {
				return arguments{}, fmt.Errorf("--extra-projects-root needs a value")
			}
			i++
			out.extraProjectsRoots = append(out.extraProjectsRoots, rawArgs[i])
		case "--no-config":
			out.readConfig = false
		case "--version":
			fmt.Println(version)
			os.Exit(0)
		default:
			return arguments{}, fmt.Errorf("unknown flag: %s", rawArgs[i])
		}
	}

	if !periodSet || out.periodSeconds == 0 {
		return arguments{}, fmt.Errorf("--period is required and must be > 0")
	}
	if !winStartSet {
		return arguments{}, fmt.Errorf("--win-start is required")
	}

	if !nowSet {
		out.nowUnix = float64(time.Now().UnixNano()) / 1e9
	}
	if out.projectsRoot == "" {
		out.projectsRoot = defaultProjectsRoot()
	}

	return out, nil
}

func parseUint64(s string) (uint64, error) {
	var v uint64
	if _, err := fmt.Sscanf(s, "%d", &v); err != nil {
		return 0, fmt.Errorf("invalid integer: %s", s)
	}
	return v, nil
}

func parseFloat64(s string) (float64, error) {
	var v float64
	if _, err := fmt.Sscanf(s, "%g", &v); err != nil {
		return 0, fmt.Errorf("invalid number: %s", s)
	}
	return v, nil
}

// homeDirectory resolves the user's home dir. On Windows, USERPROFILE is
// canonical (HOME is often unset, or a git-bash POSIX path like /c/Users/...
// that isn't a valid native path), so prefer it; elsewhere HOME is canonical.
// The fallback covers the rarer inverse case. Returns "" if neither is set.
func homeDirectory() string {
	primary, secondary := homeEnvVars(runtime.GOOS)
	if v, ok := os.LookupEnv(primary); ok && v != "" {
		return v
	}
	if v, ok := os.LookupEnv(secondary); ok && v != "" {
		return v
	}
	return ""
}

// homeEnvVars is the platform seam for home resolution: pure so a test can
// drive the windows ordering from any OS (COVERAGE-PLAN section 5 option 1).
func homeEnvVars(goos string) (primary, secondary string) {
	if goos == "windows" {
		return "USERPROFILE", "HOME"
	}
	return "HOME", "USERPROFILE"
}

func defaultProjectsRoot() string {
	if home := homeDirectory(); home != "" {
		return filepath.Join(home, ".claude", "projects")
	}
	return filepath.Join(".claude", "projects")
}

// JSON structures for a JSONL line.
//
// Wrong-typed fields are treated as absent rather than poisoning the line,
// per SPEC.md "Lenient per-field parsing": the lenient* wrapper types absorb
// type mismatches instead of failing the whole-entry unmarshal.

type entry struct {
	Timestamp lenientString `json:"timestamp"`
	Message   *message      `json:"message"`
}

type message struct {
	Role  lenientString `json:"role"`
	ID    lenientString `json:"id"`
	Model lenientString `json:"model"`
	Usage *usage        `json:"usage"`
}

type usage struct {
	InputTokens              lenientCount   `json:"input_tokens"`
	OutputTokens             lenientCount   `json:"output_tokens"`
	CacheReadInputTokens     lenientCount   `json:"cache_read_input_tokens"`
	CacheCreationInputTokens lenientCount   `json:"cache_creation_input_tokens"`
	ServerToolUse            *serverToolUse `json:"server_tool_use"`
}

// serverToolUse is the nested usage.server_tool_use object; nil when absent.
type serverToolUse struct {
	WebSearchRequests lenientCount `json:"web_search_requests"`
}

// lenientString unmarshals a JSON string; any other type is an absent field.
type lenientString string

func (s *lenientString) UnmarshalJSON(b []byte) error {
	var v string
	if err := sonic.Unmarshal(b, &v); err == nil {
		*s = lenientString(v)
	}
	return nil
}

// lenientCount accepts any JSON number, truncated toward zero; values outside
// [0, math.MaxUint64] and non-number tokens are treated as absent (0).
type lenientCount uint64

func (c *lenientCount) UnmarshalJSON(b []byte) error {
	var u uint64
	if err := sonic.Unmarshal(b, &u); err == nil {
		*c = lenientCount(u)
		return nil
	}
	var f float64
	if err := sonic.Unmarshal(b, &f); err == nil && f >= 0 && f < 18446744073709551616.0 {
		*c = lenientCount(uint64(f))
	}
	return nil
}

// UnmarshalJSON on usage absorbs a non-object value as an absent subtree.
func (u *usage) UnmarshalJSON(b []byte) error {
	type usageAlias usage // drops the method set, avoiding recursion
	var a usageAlias
	if err := sonic.Unmarshal(b, &a); err == nil {
		*u = usage(a)
	}
	return nil
}

// UnmarshalJSON on serverToolUse absorbs a non-object value the same way.
func (s *serverToolUse) UnmarshalJSON(b []byte) error {
	type stuAlias serverToolUse
	var a stuAlias
	if err := sonic.Unmarshal(b, &a); err == nil {
		*s = serverToolUse(a)
	}
	return nil
}

// sonnet5StandardFrom is the intro->standard boundary for Sonnet 5. Intro
// pricing runs through 2026-08-31 inclusive; standard applies from 2026-09-01
// onward. Selection is a lexicographic compare of the turn's ISO-8601 date
// prefix ("YYYY-MM-DD") against this string — monotonic for zero-padded dates,
// no timezone math.
const sonnet5StandardFrom = "2026-09-01"

// ratesForModel returns (inputPerMTok, outputPerMTok) for a model string.
// datePrefix is the turn's ISO-8601 timestamp (only its leading 10-char
// "YYYY-MM-DD" is consulted, and only for the sonnet-5 family).
func ratesForModel(model, datePrefix string) (float64, float64) {
	lower := strings.ToLower(model)
	switch {
	// fable/mythos first: the Fable family bills at $10/$50 and must win
	// before any substring that could collide.
	case strings.Contains(lower, "fable"), strings.Contains(lower, "mythos"):
		return 10.0, 50.0
	case strings.Contains(lower, "opus"):
		return 5.0, 25.0
	case strings.Contains(lower, "haiku"):
		return 1.0, 5.0
	// sonnet-5 BEFORE the generic sonnet default. "sonnet-5" does NOT match
	// "claude-sonnet-4-5" (no such substring), so Sonnet 4.5 keeps the
	// generic sonnet rates below, date-independent.
	case strings.Contains(lower, "sonnet-5"):
		if len(datePrefix) >= 10 && datePrefix[:10] >= sonnet5StandardFrom {
			return 3.0, 15.0 // standard
		}
		return 2.0, 10.0 // intro
	default: // sonnet or unknown -> sonnet
		return 3.0, 15.0
	}
}

// webSearchCostUSD is the flat charge per server-side web search request
// (billed $10 / 1,000), added on top of token cost. Matches SPEC.md.
const webSearchCostUSD = 0.01

// costForTurn computes the dollar cost of one assistant turn. datePrefix is
// the turn's ISO-8601 timestamp, threaded through for date-aware sonnet-5.
func costForTurn(u *usage, model, datePrefix string) float64 {
	if u == nil {
		return 0
	}
	inputRate, outputRate := ratesForModel(model, datePrefix)
	tokenCost := (float64(u.InputTokens)*inputRate +
		float64(u.CacheReadInputTokens)*inputRate*0.10 +
		float64(u.CacheCreationInputTokens)*inputRate*1.25 +
		float64(u.OutputTokens)*outputRate) / 1_000_000.0
	var webSearches uint64
	if u.ServerToolUse != nil {
		webSearches = uint64(u.ServerToolUse.WebSearchRequests)
	}
	return tokenCost + float64(webSearches)*webSearchCostUSD
}

// parseISO8601 parses an ISO 8601 timestamp (with optional Z suffix) to a
// Unix epoch float64. Returns (0, false) on failure.
func parseISO8601(timestamp string) (float64, bool) {
	if timestamp == "" {
		return 0, false
	}
	// Replace trailing Z with +00:00 so time.Parse handles it.
	normalized := strings.TrimSuffix(timestamp, "Z")
	if len(normalized) < len(timestamp) {
		normalized += "+00:00"
	}

	// Try RFC3339Nano first (most common), then RFC3339.
	for _, layout := range []string{time.RFC3339Nano, time.RFC3339} {
		if t, err := time.Parse(layout, normalized); err == nil {
			return float64(t.UnixNano()) / 1e9, true
		}
	}
	return 0, false
}

// groupResult holds the cost sums for one (slug, session) group.
type groupResult struct {
	trailing float64
	window   float64
}

// walkGroup processes all JSONL files in a session group with shared dedup.
func walkGroup(paths []string, periodCutoff, winStart float64) groupResult {
	earliest := math.Min(periodCutoff, winStart)
	var result groupResult
	seenIDs := make(map[string]struct{})

	for _, path := range paths {
		file, err := os.Open(path)
		if err != nil {
			continue
		}
		scanner := bufio.NewScanner(file)
		// Increase token buffer for long lines (some JSONL entries can be large).
		buf := make([]byte, 0, 64*1024)
		scanner.Buffer(buf, 4*1024*1024)

		for scanner.Scan() {
			// scanner.Bytes() aliases the scanner buffer (no allocation); the
			// parse consumes it before the next Scan(), so aliasing is safe.
			// Avoids the Text()+[]byte() double copy per line.
			line := bytes.TrimSpace(scanner.Bytes())
			if len(line) == 0 {
				continue
			}

			var e entry
			if err := sonic.Unmarshal(line, &e); err != nil {
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
			if !ok || ts < earliest {
				continue
			}

			cost := costForTurn(msg.Usage, string(msg.Model), string(e.Timestamp))
			if ts >= periodCutoff {
				result.trailing += cost
			}
			if ts >= winStart {
				result.window += cost
			}
		}
		file.Close()
	}
	return result
}

// groupKey identifies a unique (slug, session) pair.
type groupKey struct {
	slug      string
	sessionID string
}

// discoverGroups finds all JSONL files under every root, applies the mtime
// filter, and groups them by (slug, session_id). Groups merge naturally
// across roots: same (slug, session_id) on two roots concatenates the path
// lists, and dedup happens later in walkGroup via seenIDs on message.id.
func discoverGroups(roots []string, earliest float64) map[groupKey][]string {
	groups := make(map[groupKey][]string)
	earliestTime := time.Unix(0, int64(earliest*1e9))

	for _, root := range roots {
		// Parents: <root>/<slug>/<session_id>.jsonl
		parentGlob := filepath.Join(root, "*", "*.jsonl")
		parentMatches, err := filepath.Glob(parentGlob)
		if err == nil {
			for _, path := range parentMatches {
				info, err := os.Stat(path)
				if err != nil {
					continue
				}
				// Glob matches directories named *.jsonl too; parents must
				// be regular files (SPEC Discovery).
				if !info.Mode().IsRegular() {
					continue
				}
				if info.ModTime().Before(earliestTime) {
					continue
				}
				slug := filepath.Base(filepath.Dir(path))
				sessionID := strings.TrimSuffix(filepath.Base(path), ".jsonl")
				key := groupKey{slug: slug, sessionID: sessionID}
				groups[key] = append(groups[key], path)
			}
		}

		// Subagents: <root>/<slug>/<session_id>/subagents/agent-*.jsonl
		// filepath.Glob doesn't support **, so we walk two levels.
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
					continue // no subagents dir is normal
				}
				for _, subEntry := range subEntries {
					name := subEntry.Name()
					if subEntry.IsDir() || !strings.HasPrefix(name, "agent-") || !strings.HasSuffix(name, ".jsonl") {
						continue
					}
					path := filepath.Join(subagentsDir, name)
					info, err := os.Stat(path)
					if err != nil {
						continue
					}
					if info.ModTime().Before(earliestTime) {
						continue
					}
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

// output is the JSON shape we print to stdout.
type output struct {
	TrailingUSD float64 `json:"trailing_usd"`
	WindowUSD   float64 `json:"window_usd"`
	FilesWalked uint64  `json:"files_walked"`
	Groups      uint64  `json:"groups"`
	ElapsedMS   uint64  `json:"elapsed_ms"`
}

func main() {
	raw := os.Args[1:]
	if wantsHelp(raw) {
		fmt.Print(helpText)
		os.Exit(0)
	}
	// Subcommand dispatch. Bare flag invocation (or no args) routes to cost
	// mode for back-compat with the original CLI shape.
	if len(raw) > 0 {
		switch raw[0] {
		case "cost":
			runCost(raw[1:])
			return
		case "beacons-latest":
			runBeaconsLatest(raw[1:])
			return
		case "beacons-history":
			runBeaconsHistory(raw[1:])
			return
		case "search":
			runSearch(raw[1:])
			return
		case "events":
			runEvents(raw[1:])
			return
		}
		first := raw[0]
		// Any non-flag first positional that didn't match a subcommand is
		// an error; bare flag invocation falls through to cost.
		if !strings.HasPrefix(first, "-") {
			fmt.Fprintf(os.Stderr, "walker: unknown subcommand: %s\n", first)
			fmt.Fprintln(os.Stderr, "Run 'claude-walker --help' for usage.")
			os.Exit(2)
		}
	}
	runCost(raw)
}

func runCost(rawArgs []string) {
	started := time.Now()

	args, err := parseArguments(rawArgs)
	if err != nil {
		fmt.Fprintf(os.Stderr, "walker: %v\n", err)
		fmt.Fprintln(os.Stderr, "Run 'claude-walker --help' for usage.")
		os.Exit(2)
	}

	periodCutoff := args.nowUnix - float64(args.periodSeconds)
	earliest := math.Min(periodCutoff, args.winStartUnix)

	roots := ResolveRoots(args.projectsRoot, args.extraProjectsRoots, args.readConfig)
	groups := discoverGroups(roots, earliest)

	// Count totals before we consume the map.
	totalGroups := uint64(len(groups))
	var totalFiles uint64
	for _, paths := range groups {
		totalFiles += uint64(len(paths))
	}

	// Collect groups as slices for concurrent processing.
	groupSlices := make([][]string, 0, len(groups))
	for _, paths := range groups {
		groupSlices = append(groupSlices, paths)
	}

	numWorkers := effectiveWorkerCount(runtime.NumCPU())

	type result struct {
		trailing float64
		window   float64
	}

	work := make(chan []string, len(groupSlices))
	results := make(chan result, len(groupSlices))

	var workerGroup sync.WaitGroup
	for workerIndex := 0; workerIndex < numWorkers; workerIndex++ {
		workerGroup.Add(1)
		go func() {
			defer workerGroup.Done()
			for paths := range work {
				r := walkGroup(paths, periodCutoff, args.winStartUnix)
				results <- result(r)
			}
		}()
	}

	// Feed work.
	for _, paths := range groupSlices {
		work <- paths
	}
	close(work)

	// Close results once all workers are done.
	go func() {
		workerGroup.Wait()
		close(results)
	}()

	// Collect results.
	var totalTrailing, totalWindow float64
	for r := range results {
		totalTrailing += r.trailing
		totalWindow += r.window
	}

	elapsedMS := uint64(time.Since(started).Milliseconds())

	out := output{
		TrailingUSD: totalTrailing,
		WindowUSD:   totalWindow,
		FilesWalked: totalFiles,
		Groups:      totalGroups,
		ElapsedMS:   elapsedMS,
	}

	// Marshal with 6-decimal precision matching the Rust reference output.
	// encoding/json will use shortest representation, so we format manually.
	fmt.Printf(
		"{\"trailing_usd\":%.6f,\"window_usd\":%.6f,\"files_walked\":%d,\"groups\":%d,\"elapsed_ms\":%d}\n",
		out.TrailingUSD, out.WindowUSD, out.FilesWalked, out.Groups, out.ElapsedMS,
	)

	_ = out // fields printed above via fmt.Printf
}
