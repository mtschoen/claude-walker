// Search subcommand: substring/regex match across transcript content.
// See ../SPEC.md "Subcommands" for the contract.
//
// Algorithm mirrors rust/src/search.rs:
//   - Parse flags: pattern (positional), --regex, --case-sensitive, --role,
//     --since/--until (time), --cwd/--any-cwd, --context, --limit,
//     --count-only, --include-tool-blocks, --format (pretty|jsonl),
//     --snippet-chars, --projects-root, --now.
//   - For each jsonl file under --projects-root/<slug>/*.jsonl:
//     - Scan each line as an assistant entry.
//     - Extract text from message.content (text blocks; tool_use/tool_result
//       blocks only when --include-tool-blocks).
//     - Skip entries where content is ONLY tool_use/tool_result blocks
//       (unless --include-tool-blocks is set).
//     - Filter by role (--role user|assistant|both).
//     - Filter by time window (--since/--until).
//     - Find pattern matches in the extracted text.
//     - Build snippet around first match with configurable width.
//     - Collect context_before/context_after turns.
//   - Sort hits newest-first by timestamp, tiebreak (session_id, line_number).
//   - Count distinct (slug, session_id) pairs BEFORE truncation.
//   - Truncate to --limit.
//   - Output hits + summary as JSONL or pretty text.

package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"runtime/pprof"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/bytedance/sonic"
)

// searchExtractAll parses array/string content ONCE and returns the default
// text (text blocks only), the tool-inclusive text (computed only when
// includeToolBlocks is set), and whether the array is composed solely of
// tool_use/tool_result blocks. Replaces three separate full parses of the
// same content bytes (default text + with-tools text + only-tool-blocks
// check). Mirrors content.rs's extract_text/is_only_tool_blocks run over a
// single parsed Value.
func searchExtractAll(content json.RawMessage, includeToolBlocks bool) (textDefault, textWithTools string, isOnlyToolBlocks bool) {
	if firstNonSpaceByte(content) != '[' {
		// Bare string content (legacy user-prompt form): default == with-tools,
		// never only-tool-blocks.
		var s string
		if err := sonic.Unmarshal(content, &s); err == nil {
			return s, s, false
		}
		return "", "", false
	}
	type block struct {
		Type    string          `json:"type"`
		Text    string          `json:"text"`
		Content json.RawMessage `json:"content"`
		Input   json.RawMessage `json:"input"`
	}
	var blocks []block
	if err := sonic.Unmarshal(content, &blocks); err != nil {
		return "", "", false
	}
	if len(blocks) == 0 {
		return "", "", false
	}
	isOnlyToolBlocks = true
	var defParts, toolParts []string
	for _, b := range blocks {
		switch b.Type {
		case "text":
			isOnlyToolBlocks = false
			defParts = append(defParts, b.Text)
			if includeToolBlocks {
				toolParts = append(toolParts, b.Text)
			}
		case "tool_use":
			if includeToolBlocks && len(b.Input) > 0 {
				// SPEC "Tool blocks": compact JSON serialization preserving
				// source key order; json.Compact minifies without reordering.
				var compacted bytes.Buffer
				if err := json.Compact(&compacted, b.Input); err == nil {
					toolParts = append(toolParts, compacted.String())
				}
			}
		case "tool_result":
			if includeToolBlocks && len(b.Content) > 0 {
				if firstNonSpaceByte(b.Content) != '[' {
					var s string
					if err := sonic.Unmarshal(b.Content, &s); err == nil {
						toolParts = append(toolParts, s)
					}
				} else {
					var inner []contentBlock
					if err := sonic.Unmarshal(b.Content, &inner); err == nil {
						for _, ib := range inner {
							if ib.Type == "text" {
								toolParts = append(toolParts, ib.Text)
							}
						}
					}
				}
			}
		default:
			isOnlyToolBlocks = false
		}
	}
	textDefault = strings.Join(defParts, "\n")
	if includeToolBlocks {
		textWithTools = strings.Join(toolParts, "\n")
	}
	return textDefault, textWithTools, isOnlyToolBlocks
}

// searchExtractQueueOpText extracts text from a type:"queue-operation" entry.
// Queue-ops have no message object — the text lives in the entry's root-level
// `content` bare string. Returns ("", false) when missing or empty (e.g.
// remove/dequeue ops), so only content-bearing enqueue/popAll surface under
// --include-queue-ops. Mirrors content.rs::extract_queue_op_text.
func searchExtractQueueOpText(content json.RawMessage) (string, bool) {
	if len(content) == 0 {
		return "", false
	}
	var s string
	if err := sonic.Unmarshal(content, &s); err != nil {
		return "", false
	}
	if s == "" {
		return "", false
	}
	return s, true
}

// === Scan ===

type searchMsg struct {
	LineNumber       uint32
	Timestamp        float64
	HasTimestamp     bool
	TimestampStr     string
	Role             string
	TextDefault      string
	TextWithTools    string
	IsOnlyToolBlocks bool
}

func searchScanFile(path string, includeQueueOps, includeToolBlocks bool) []searchMsg {
	file, err := os.Open(path)
	if err != nil {
		return nil
	}
	defer file.Close()
	var out []searchMsg
	scanner := bufio.NewScanner(file)
	buf := make([]byte, 0, 64*1024)
	scanner.Buffer(buf, 4*1024*1024)
	idx := 0
	for scanner.Scan() {
		idx++
		// scanner.Bytes() aliases the scanner's buffer (no allocation); every
		// RawMessage we slice from it is consumed before the next Scan(), so the
		// aliasing is safe. Avoids the Text()+[]byte() double copy per line.
		line := bytes.TrimSpace(scanner.Bytes())
		if len(line) == 0 {
			continue
		}
		// One typed root parse instead of a map + per-field re-parses of
		// type/timestamp. Message/Content stay raw and are decoded lazily.
		var root struct {
			Type      string          `json:"type"`
			Message   json.RawMessage `json:"message"`
			Timestamp string          `json:"timestamp"`
			Content   json.RawMessage `json:"content"`
		}
		if err := sonic.Unmarshal(line, &root); err != nil {
			continue
		}
		// Queue-operation entries have no message object: the text lives in a
		// root-level `content` string. Only indexed when --include-queue-ops is
		// set; content-bearing enqueue/popAll surface, empty remove/dequeue are
		// dropped by searchExtractQueueOpText. They count as role:user. Mirrors search.rs.
		if root.Type == "queue-operation" {
			if !includeQueueOps {
				continue
			}
			qtext, ok := searchExtractQueueOpText(root.Content)
			if !ok {
				continue
			}
			var qts float64
			qhasTs := false
			if root.Timestamp != "" {
				if t, ok := parseISO8601(root.Timestamp); ok {
					qts = t
					qhasTs = true
				}
			}
			out = append(out, searchMsg{
				LineNumber:       uint32(idx),
				Timestamp:        qts,
				HasTimestamp:     qhasTs,
				TimestampStr:     root.Timestamp,
				Role:             "user",
				TextDefault:      qtext,
				TextWithTools:    qtext,
				IsOnlyToolBlocks: false,
			})
			continue
		}
		if len(root.Message) == 0 {
			continue
		}
		var msg struct {
			Role    string          `json:"role"`
			Content json.RawMessage `json:"content"`
		}
		if err := sonic.Unmarshal(root.Message, &msg); err != nil || msg.Role == "" {
			continue
		}
		// Include ALL roles (user, assistant, etc.) — context needs all messages;
		// processFile filters by role as needed.
		if len(msg.Content) == 0 {
			continue
		}
		var ts float64
		hasTs := false
		if root.Timestamp != "" {
			if t, ok := parseISO8601(root.Timestamp); ok {
				ts = t
				hasTs = true
			}
		}
		textDefault, textWithTools, isOnly := searchExtractAll(msg.Content, includeToolBlocks)
		out = append(out, searchMsg{
			LineNumber:       uint32(idx),
			Timestamp:        ts,
			HasTimestamp:     hasTs,
			TimestampStr:     root.Timestamp,
			Role:             msg.Role,
			TextDefault:      textDefault,
			TextWithTools:    textWithTools,
			IsOnlyToolBlocks: isOnly,
		})
	}
	return out
}

// === Discovery ===

type searchFileInfo struct {
	Path      string
	Slug      string
	SessionID string
	HostRoot  string
}

func searchDiscoverFiles(roots []string, since *float64, cwdSlug *string) []searchFileInfo {
	var out []searchFileInfo
	earliestTime := time.Time{}
	if since != nil {
		earliestTime = time.Unix(0, int64(*since*1e9))
	}
	for _, root := range roots {
		entries, err := os.ReadDir(root)
		if err != nil {
			continue
		}
		for _, slugEnt := range entries {
			if !slugEnt.IsDir() {
				continue
			}
			slug := slugEnt.Name()
			if cwdSlug != nil && slug != *cwdSlug {
				continue
			}
			slugPath := filepath.Join(root, slug)
			dirEntries, err := os.ReadDir(slugPath)
			if err != nil {
				continue
			}
			for _, fEnt := range dirEntries {
				if fEnt.IsDir() {
					// Subagents: <slug>/<session>/subagents/agent-*.jsonl, per
					// SPEC "Discovery" under search. session_id is the
					// enclosing session dir name (the parent session), so
					// subagent hits group with the parent in sessions_matched.
					sid := fEnt.Name()
					subDir := filepath.Join(slugPath, sid, "subagents")
					subEntries, err := os.ReadDir(subDir)
					if err != nil {
						continue
					}
					for _, sEnt := range subEntries {
						if sEnt.IsDir() {
							continue
						}
						name := sEnt.Name()
						if !strings.HasPrefix(name, "agent-") || !strings.HasSuffix(name, ".jsonl") {
							continue
						}
						if since != nil {
							info, err := sEnt.Info()
							if err == nil && info.ModTime().Before(earliestTime) {
								continue
							}
						}
						out = append(out, searchFileInfo{
							Path:      filepath.Join(subDir, name),
							Slug:      slug,
							SessionID: sid,
							HostRoot:  root,
						})
					}
					continue
				}
				if !strings.HasSuffix(fEnt.Name(), ".jsonl") {
					continue
				}
				if since != nil {
					info, err := fEnt.Info()
					if err == nil && info.ModTime().Before(earliestTime) {
						continue
					}
				}
				df := searchFileInfo{
					Path:      filepath.Join(slugPath, fEnt.Name()),
					Slug:      slug,
					SessionID: strings.TrimSuffix(fEnt.Name(), ".jsonl"),
					HostRoot:  root,
				}
				out = append(out, df)
			}
		}
	}
	return out
}

// === Snippet ===

func searchNudgeWS(text string, cut int, direction int, maxNudge int) int {
	if cut <= 0 || cut >= len(text) {
		return cut
	}
	if direction < 0 {
		lo := cut - maxNudge
		if lo < 0 {
			lo = 0
		}
		for i := cut; i > lo; i-- {
			if i > 0 && (text[i-1] == ' ' || text[i-1] == '\t' || text[i-1] == '\n' || text[i-1] == '\r') {
				return i
			}
		}
	} else {
		hi := cut + maxNudge
		if hi > len(text) {
			hi = len(text)
		}
		for i := cut; i < hi; i++ {
			if text[i] == ' ' || text[i] == '\t' || text[i] == '\n' || text[i] == '\r' {
				return i
			}
		}
	}
	return cut
}

// searchNudgeCharBoundary nudges idx forward to the next UTF-8 character
// boundary (mirrors Rust's str::is_char_boundary walk). A continuation byte
// has top bits 10 (0x80..0xBF); len(text) is always a boundary. Prevents a
// snippet cut from splitting a multibyte codepoint. See SPEC.md "Snippet
// boundaries".
func searchNudgeCharBoundary(text string, idx int) int {
	for idx < len(text) && text[idx]&0xC0 == 0x80 {
		idx++
	}
	return idx
}

func searchMakeSnippet(text string, firstMatch [2]uint32, snippetChars uint32) string {
	halfInt := int(snippetChars / 2)
	mstart := int(firstMatch[0])
	mend := int(firstMatch[1])
	lo := mstart - halfInt
	if lo < 0 {
		lo = 0
	}
	hi := mend + halfInt
	if hi > len(text) {
		hi = len(text)
	}
	lo = searchNudgeCharBoundary(text, lo)
	hi = searchNudgeCharBoundary(text, hi)
	if lo > 0 {
		lo = searchNudgeWS(text, lo, -1, 20)
	}
	if hi < len(text) {
		hi = searchNudgeWS(text, hi, 1, 20)
	}
	lo = searchNudgeCharBoundary(text, lo)
	hi = searchNudgeCharBoundary(text, hi)
	return text[lo:hi]
}

// === Context ===

type searchCtx struct {
	Role      string `json:"role"`
	Text      string `json:"text"`
	Timestamp string `json:"timestamp"`
}

func searchBuildCtx(msgs []searchMsg, hitIdx int, ctxN uint32) ([]searchCtx, []searchCtx) {
	if ctxN == 0 {
		return nil, nil
	}
	n := int(ctxN)
	var before, after []searchCtx
	start := hitIdx - n
	if start < 0 {
		start = 0
	}
	for i := start; i < hitIdx; i++ {
		before = append(before, searchCtx{msgs[i].Role, msgs[i].TextDefault, msgs[i].TimestampStr})
	}
	end := hitIdx + 1 + n
	if end > len(msgs) {
		end = len(msgs)
	}
	for i := hitIdx + 1; i < end; i++ {
		after = append(after, searchCtx{msgs[i].Role, msgs[i].TextDefault, msgs[i].TimestampStr})
	}
	return before, after
}

// === Hit ===

type searchHit struct {
	Timestamp     float64     `json:"-"`
	TimestampStr  string      `json:"timestamp"`
	SessionID     string      `json:"session_id"`
	CwdSlug       string      `json:"cwd_slug"`
	HostRoot      string      `json:"host_root"`
	FilePath      string      `json:"file_path"`
	LineNumber    uint32      `json:"line_number"`
	Role          string      `json:"role"`
	Snippet       string      `json:"snippet"`
	MatchOffsets  [][2]uint32 `json:"match_offsets"`
	ContextBefore []searchCtx `json:"context_before"`
	ContextAfter  []searchCtx `json:"context_after"`
}

// === Args ===

type searchArgs struct {
	Pattern            string
	Regex              bool
	CaseSensitive      bool
	Role               string
	Since              *float64
	Until              *float64
	Cwd                string
	AnyCwd             bool
	Context            uint32
	Limit              uint32
	CountOnly          bool
	IncludeToolBlocks  bool
	IncludeQueueOps    bool
	Format             string
	SnippetChars       uint32
	ProjectsRoot       string
	ExtraProjectsRoots []string
	ReadConfig         bool
	Now                float64
}

func parseSearchArgs(raw []string) (searchArgs, error) {
	var args searchArgs
	args.Role = "both"
	args.Context = 1
	args.Limit = 50
	args.Format = "pretty"
	args.SnippetChars = 240
	args.ProjectsRoot = defaultProjectsRoot()
	args.ReadConfig = true
	args.Now = float64(time.Now().UnixNano()) / 1e9

	var sinceRaw, untilRaw *string

	for i := 0; i < len(raw); i++ {
		switch raw[i] {
		case "--regex":
			args.Regex = true
		case "--case-sensitive":
			args.CaseSensitive = true
		case "--role":
			if i+1 >= len(raw) {
				return args, fmt.Errorf("--role needs a value")
			}
			i++
			args.Role = raw[i]
			if args.Role != "user" && args.Role != "assistant" && args.Role != "both" {
				return args, fmt.Errorf("--role: invalid value %s; expected user|assistant|both", args.Role)
			}
		case "--since":
			if i+1 >= len(raw) {
				return args, fmt.Errorf("--since needs a value")
			}
			i++
			sinceRaw = &raw[i]
		case "--until":
			if i+1 >= len(raw) {
				return args, fmt.Errorf("--until needs a value")
			}
			i++
			untilRaw = &raw[i]
		case "--cwd":
			if i+1 >= len(raw) {
				return args, fmt.Errorf("--cwd needs a value")
			}
			i++
			args.Cwd = raw[i]
		case "--any-cwd":
			args.AnyCwd = true
		case "--context":
			if i+1 >= len(raw) {
				return args, fmt.Errorf("--context needs a value")
			}
			i++
			v, err := strconv.ParseUint(raw[i], 10, 32)
			if err != nil {
				return args, fmt.Errorf("--context: %v", err)
			}
			args.Context = uint32(v)
		case "--limit":
			if i+1 >= len(raw) {
				return args, fmt.Errorf("--limit needs a value")
			}
			i++
			v, err := strconv.ParseUint(raw[i], 10, 32)
			if err != nil {
				return args, fmt.Errorf("--limit: %v", err)
			}
			args.Limit = uint32(v)
		case "--count-only":
			args.CountOnly = true
		case "--include-tool-blocks":
			args.IncludeToolBlocks = true
		case "--include-queue-ops":
			args.IncludeQueueOps = true
		case "--format":
			if i+1 >= len(raw) {
				return args, fmt.Errorf("--format needs a value")
			}
			i++
			args.Format = raw[i]
			if args.Format != "pretty" && args.Format != "jsonl" {
				return args, fmt.Errorf("--format: invalid value %s; expected pretty|jsonl", args.Format)
			}
		case "--snippet-chars":
			if i+1 >= len(raw) {
				return args, fmt.Errorf("--snippet-chars needs a value")
			}
			i++
			v, err := strconv.ParseUint(raw[i], 10, 32)
			if err != nil {
				return args, fmt.Errorf("--snippet-chars: %v", err)
			}
			args.SnippetChars = uint32(v)
		case "--projects-root":
			if i+1 >= len(raw) {
				return args, fmt.Errorf("--projects-root needs a value")
			}
			i++
			args.ProjectsRoot = raw[i]
		case "--extra-projects-root":
			if i+1 >= len(raw) {
				return args, fmt.Errorf("--extra-projects-root needs a value")
			}
			i++
			args.ExtraProjectsRoots = append(args.ExtraProjectsRoots, raw[i])
		case "--no-config":
			args.ReadConfig = false
		case "--now":
			if i+1 >= len(raw) {
				return args, fmt.Errorf("--now needs a value")
			}
			i++
			v, err := strconv.ParseFloat(raw[i], 64)
			if err != nil {
				return args, fmt.Errorf("--now: %v", err)
			}
			args.Now = v
		default:
			if strings.HasPrefix(raw[i], "--") {
				return args, fmt.Errorf("unknown flag: %s", raw[i])
			}
			if args.Pattern != "" {
				return args, fmt.Errorf("unexpected positional argument: %s", raw[i])
			}
			args.Pattern = raw[i]
		}
	}

	if args.Pattern == "" {
		return args, fmt.Errorf("pattern must be non-empty")
	}
	if args.Cwd != "" && args.AnyCwd {
		return args, fmt.Errorf("--cwd and --any-cwd are mutually exclusive")
	}
	if args.ProjectsRoot == "" {
		args.ProjectsRoot = defaultProjectsRoot()
	}

	if sinceRaw != nil {
		v, err := parseSearchTimeArg(*sinceRaw, args.Now)
		if err != nil {
			return args, fmt.Errorf("bad time: --since=%s (%v)", *sinceRaw, err)
		}
		args.Since = &v
	}
	if untilRaw != nil {
		v, err := parseSearchTimeArg(*untilRaw, args.Now)
		if err != nil {
			return args, fmt.Errorf("bad time: --until=%s (%v)", *untilRaw, err)
		}
		args.Until = &v
	}

	return args, nil
}

func parseSearchTimeArg(s string, now float64) (float64, error) {
	trimmed := strings.TrimSpace(s)
	if trimmed == "" {
		return 0, fmt.Errorf("empty value")
	}
	if len(trimmed) > 0 {
		last := trimmed[len(trimmed)-1]
		if last == 'd' || last == 'h' || last == 'm' || last == 's' {
			head := trimmed[:len(trimmed)-1]
			if head != "" && isSearchNumeric(head) {
				n, err := strconv.ParseFloat(head, 64)
				if err != nil {
					return 0, err
				}
				mult := map[byte]float64{'d': 86400, 'h': 3600, 'm': 60, 's': 1}[last]
				return now - n*mult, nil
			}
		}
	}
	ts, ok := parseISO8601(trimmed)
	if !ok {
		return 0, fmt.Errorf("not RFC3339 or relative: %s", trimmed)
	}
	return ts, nil
}

func isSearchNumeric(s string) bool {
	for _, c := range s {
		if c != '.' && (c < '0' || c > '9') {
			return false
		}
	}
	return true
}

// === Matching ===

// searchMatcher wraps the compiled regex with an optional fast pre-filter for
// plain ASCII literal patterns. Go's regexp runs its backtracking engine (with
// per-rune unicode.SimpleFold for the default case-insensitive search) at every
// position of every message's text - the dominant cost when scanning a fleet
// for a rare token. For a literal pattern the pre-filter answers "could this
// text contain a match?" with a cheap byte scan and skips the regex on the
// common no-match case.
type searchMatcher struct {
	re           *regexp.Regexp
	literalLower []byte // non-nil => ASCII-literal fast path enabled
}

func newSearchMatcher(re *regexp.Regexp, args searchArgs) searchMatcher {
	m := searchMatcher{re: re}
	// Fast path only for a literal (non --regex), non-empty, pure-ASCII pattern.
	if !args.Regex && args.Pattern != "" && isASCIIString(args.Pattern) {
		m.literalLower = asciiLowerBytes(args.Pattern)
	}
	return m
}

// findAll returns every match [start,end) pair in text. When the ASCII-literal
// fast path is active it pre-screens text: pure-ASCII text with no
// case-folded occurrence cannot match (over ASCII text the default (?i)
// behavior reduces to ASCII case-folding), so the regex is skipped. Any text
// containing a non-ASCII byte falls back to the regex, so full Unicode
// case-folding semantics are preserved exactly.
func (m searchMatcher) findAll(text string) [][]int {
	if m.literalLower != nil {
		asciiOnly, found := asciiScanFold(text, m.literalLower)
		if asciiOnly && !found {
			return nil
		}
	}
	return m.re.FindAllStringIndex(text, -1)
}

func isASCIIString(s string) bool {
	for i := 0; i < len(s); i++ {
		if s[i] >= 0x80 {
			return false
		}
	}
	return true
}

func asciiLowerBytes(s string) []byte {
	b := make([]byte, len(s))
	for i := 0; i < len(s); i++ {
		c := s[i]
		if c >= 'A' && c <= 'Z' {
			c += 'a' - 'A'
		}
		b[i] = c
	}
	return b
}

// asciiScanFold scans text once. It returns asciiOnly=false (and found=false)
// the moment it sees a non-ASCII byte, signaling the caller to run the regex.
// For pure-ASCII text it reports whether lowerPat (already ASCII-lowercased)
// occurs under ASCII case-folding.
func asciiScanFold(text string, lowerPat []byte) (asciiOnly, found bool) {
	n, m := len(text), len(lowerPat)
	for i := 0; i < n; i++ {
		if text[i] >= 0x80 {
			return false, false
		}
	}
	// m >= 1 always: parseSearchArgs rejects empty patterns, and the fold
	// scan is only built for a non-empty literal.
	if m > n {
		return true, false
	}
	p0 := lowerPat[0]
	for i := 0; i+m <= n; i++ {
		c := text[i]
		if c >= 'A' && c <= 'Z' {
			c += 'a' - 'A'
		}
		if c != p0 {
			continue
		}
		k := 1
		for ; k < m; k++ {
			d := text[i+k]
			if d >= 'A' && d <= 'Z' {
				d += 'a' - 'A'
			}
			if d != lowerPat[k] {
				break
			}
		}
		if k == m {
			return true, true
		}
	}
	return true, false
}

// === File processing ===

func searchProcessFile(f searchFileInfo, args searchArgs, matcher searchMatcher) []searchHit {
	msgs := searchScanFile(f.Path, args.IncludeQueueOps, args.IncludeToolBlocks)
	var hits []searchHit

	for idx, m := range msgs {
		if !searchRoleMatches(args.Role, m.Role) {
			continue
		}
		if !args.IncludeToolBlocks && m.IsOnlyToolBlocks {
			continue
		}
		if (args.Since != nil || args.Until != nil) && !m.HasTimestamp {
			continue
		}
		if args.Since != nil && m.Timestamp < *args.Since {
			continue
		}
		if args.Until != nil && m.Timestamp > *args.Until {
			continue
		}

		text := m.TextDefault
		if args.IncludeToolBlocks {
			text = m.TextWithTools
		}
		if text == "" {
			continue
		}

		matches := matcher.findAll(text)
		if len(matches) == 0 {
			continue
		}

		firstMatch := matches[0]
		snippet := searchMakeSnippet(text, [2]uint32{uint32(firstMatch[0]), uint32(firstMatch[1])}, args.SnippetChars)

		snippetMatches := matcher.re.FindAllStringIndex(snippet, -1)
		var offsets [][2]uint32
		for _, m2 := range snippetMatches {
			offsets = append(offsets, [2]uint32{uint32(m2[0]), uint32(m2[1])})
		}

		ctxBefore, ctxAfter := searchBuildCtx(msgs, idx, args.Context)
		if ctxBefore == nil {
			ctxBefore = []searchCtx{}
		}
		if ctxAfter == nil {
			ctxAfter = []searchCtx{}
		}

		h := searchHit{
			Timestamp:     m.Timestamp,
			TimestampStr:  m.TimestampStr,
			SessionID:     f.SessionID,
			CwdSlug:       f.Slug,
			HostRoot:      f.HostRoot,
			FilePath:      f.Path,
			LineNumber:    m.LineNumber,
			Role:          m.Role,
			Snippet:       snippet,
			MatchOffsets:  offsets,
			ContextBefore: ctxBefore,
			ContextAfter:  ctxAfter,
		}
		hits = append(hits, h)
	}
	return hits
}

func searchRoleMatches(filter, role string) bool {
	return filter == "both" || filter == role
}

// === Output ===

type searchHitJSON struct {
	Type          string      `json:"type"`
	SessionID     string      `json:"session_id"`
	CwdSlug       string      `json:"cwd_slug"`
	HostRoot      string      `json:"host_root"`
	FilePath      string      `json:"file_path"`
	LineNumber    uint32      `json:"line_number"`
	Timestamp     string      `json:"timestamp"`
	Role          string      `json:"role"`
	Snippet       string      `json:"snippet"`
	MatchOffsets  [][2]uint32 `json:"match_offsets"`
	ContextBefore []searchCtx `json:"context_before"`
	ContextAfter  []searchCtx `json:"context_after"`
}

type searchSummaryJSON struct {
	Type            string `json:"type"`
	Hits            uint64 `json:"hits"`
	SessionsMatched uint64 `json:"sessions_matched"`
	RotsWalked      uint64 `json:"roots_walked"`
	FilesWalked     uint64 `json:"files_walked"`
	Truncated       bool   `json:"truncated"`
	ElapsedMS       uint64 `json:"elapsed_ms"`
}

func searchWriteSummary(out *strings.Builder, hitsCount uint64, sessions uint64, roots uint64, files uint64, truncated bool, elapsedMs uint64) {
	s := searchSummaryJSON{
		Type:            "summary",
		Hits:            hitsCount,
		SessionsMatched: sessions,
		RotsWalked:      roots,
		FilesWalked:     files,
		Truncated:       truncated,
		ElapsedMS:       elapsedMs,
	}
	b, _ := json.Marshal(s)
	out.WriteString(string(b))
}

func searchTruncateStr(s string, max int) string {
	if len(s) <= max {
		return s
	}
	return s[:max] + "…"
}

// === Top-level ===

func runSearch(argv []string) {
	started := time.Now()

	// Opt-in CPU profile for hotspot analysis (mirrors C++'s WALKER_PROFILE
	// convention). Off unless WALKER_CPUPROFILE names an output path.
	if profPath := os.Getenv("WALKER_CPUPROFILE"); profPath != "" {
		if f, err := os.Create(profPath); err == nil {
			_ = pprof.StartCPUProfile(f)
			defer pprof.StopCPUProfile()
		}
	}
	args, err := parseSearchArgs(argv)
	if err != nil {
		fmt.Fprintf(os.Stderr, "walker: search: %v\n", err)
		os.Exit(2)
	}

	// Build regex
	var re *regexp.Regexp
	pattern := args.Pattern
	if !args.Regex {
		pattern = regexp.QuoteMeta(args.Pattern)
	}
	flags := ""
	if !args.CaseSensitive {
		flags = "(?i)"
	}
	re, err = regexp.Compile(flags + pattern)
	if err != nil {
		fmt.Fprintf(os.Stderr, "walker: search: bad regex: %v\n", err)
		os.Exit(2)
	}
	matcher := newSearchMatcher(re, args)

	// Resolve roots (primary + CLI extras + config extras).
	roots := ResolveRoots(args.ProjectsRoot, args.ExtraProjectsRoots, args.ReadConfig)
	rootsWalked := uint64(len(roots))

	// Discover files
	var cwdSlug *string
	if args.Cwd != "" {
		cwdSlug = &args.Cwd
	}
	files := searchDiscoverFiles(roots, args.Since, cwdSlug)
	filesWalked := uint64(len(files))

	// Process files in parallel. Work unit = one file; each worker owns a
	// local hits slice to avoid contention; merge after all workers exit,
	// then sort. searchScanFile/searchProcessFile use only local state; the
	// shared compiled `re` is safe for concurrent use per regexp.Regexp's
	// docs ("safe for concurrent use by multiple goroutines"). sonic's
	// Unmarshal is documented thread-safe. Mirrors the cost-mode pattern
	// in main.go's runCost (channel + sync.WaitGroup + per-worker accumulator).
	numWorkers := effectiveWorkerCount(runtime.NumCPU())

	work := make(chan searchFileInfo, len(files))
	perWorkerHits := make([][]searchHit, numWorkers)

	var wg sync.WaitGroup
	for workerIndex := 0; workerIndex < numWorkers; workerIndex++ {
		wg.Add(1)
		go func(tid int) {
			defer wg.Done()
			local := perWorkerHits[tid][:0]
			for f := range work {
				hs := searchProcessFile(f, args, matcher)
				if len(hs) > 0 {
					local = append(local, hs...)
				}
			}
			perWorkerHits[tid] = local
		}(workerIndex)
	}
	for _, f := range files {
		work <- f
	}
	close(work)
	wg.Wait()

	// Merge per-worker hits
	total := 0
	for _, v := range perWorkerHits {
		total += len(v)
	}
	hits := make([]searchHit, 0, total)
	for _, v := range perWorkerHits {
		hits = append(hits, v...)
	}

	// Sort newest-first
	sort.Slice(hits, func(i, j int) bool {
		if hits[i].Timestamp != hits[j].Timestamp {
			return hits[i].Timestamp > hits[j].Timestamp
		}
		if hits[i].SessionID != hits[j].SessionID {
			return hits[i].SessionID < hits[j].SessionID
		}
		return hits[i].LineNumber < hits[j].LineNumber
	})

	// Count distinct sessions BEFORE truncation
	sessionSet := make(map[string]bool)
	for _, h := range hits {
		sessionSet[h.CwdSlug+"/"+h.SessionID] = true
	}
	sessionsMatched := uint64(len(sessionSet))
	totalUnfiltered := uint64(len(hits))
	truncated := totalUnfiltered > uint64(args.Limit)
	if truncated {
		hits = hits[:args.Limit]
	}

	elapsedMs := uint64(time.Since(started).Milliseconds())
	hitsOutput := totalUnfiltered
	if !args.CountOnly {
		hitsOutput = uint64(len(hits))
	}

	out := &strings.Builder{}

	if args.Format == "jsonl" {
		if !args.CountOnly {
			for _, h := range hits {
				hj := searchHitJSON{
					Type:          "hit",
					SessionID:     h.SessionID,
					CwdSlug:       h.CwdSlug,
					HostRoot:      h.HostRoot,
					FilePath:      h.FilePath,
					LineNumber:    h.LineNumber,
					Timestamp:     h.TimestampStr,
					Role:          h.Role,
					Snippet:       h.Snippet,
					MatchOffsets:  h.MatchOffsets,
					ContextBefore: h.ContextBefore,
					ContextAfter:  h.ContextAfter,
				}
				b, _ := json.Marshal(hj)
				out.Write(b)
				out.WriteByte('\n')
			}
		}
		searchWriteSummary(out, hitsOutput, sessionsMatched, rootsWalked, filesWalked, truncated, elapsedMs)
		out.WriteByte('\n')
		fmt.Print(out.String())
	} else {
		// Pretty format (SPEC "Pretty format"): highlight is
		// `  >>> pre[match]post <<<`; the summary is one human-readable
		// line, not a JSONL record.
		if !args.CountOnly {
			for _, h := range hits {
				fmt.Printf("[%s] cwd=%s role=%s session=%s\n", h.TimestampStr, h.CwdSlug, h.Role, h.SessionID)
				fmt.Printf("  %s:%d\n", h.FilePath, h.LineNumber)
				for _, t := range h.ContextBefore {
					fmt.Printf("  before: %s\n", searchTruncateStr(t.Text, 120))
				}
				// MatchOffsets come from re-running the matcher on a snippet
				// built around the first match, so they are always present
				// and in-bounds; SPEC omits the snippet line otherwise.
				if len(h.MatchOffsets) > 0 {
					mo := h.MatchOffsets[0]
					pm := int(mo[0])
					pe := int(mo[1])
					fmt.Printf("  >>> %s[%s]%s <<<\n", h.Snippet[:pm], h.Snippet[pm:pe], h.Snippet[pe:])
				}
				for _, t := range h.ContextAfter {
					fmt.Printf("  after:  %s\n", searchTruncateStr(t.Text, 120))
				}
				fmt.Println()
			}
		}
		fmt.Printf("%d hits in %d sessions across %d roots (%d files). truncated=%t elapsed %dms.\n",
			hitsOutput, sessionsMatched, rootsWalked, filesWalked, truncated, elapsedMs)
	}

	if truncated {
		fmt.Fprintf(os.Stderr, "walker: search: truncated to --limit=%d (had %d total); narrow with --since\n", args.Limit, totalUnfiltered)
	}
}
