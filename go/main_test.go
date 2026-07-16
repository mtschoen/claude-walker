// Unit tests for main.go covering local-only branches that conformance
// fixtures can't drive (no-home fallback, IO failure paths in walkGroup
// and discoverGroups, helper-function edge cases).
package main

import (
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

// TestHomeDirectoryNoEnv covers main.go:196 (returning "" when both env vars
// are unset). t.Setenv with "" effectively unsets HOME/USERPROFILE for the
// purpose of homeDirectory()'s lookup; t.Setenv auto-restores the original
// pre-test value on cleanup.
func TestHomeDirectoryNoEnv(t *testing.T) {
	t.Setenv("HOME", "")
	t.Setenv("USERPROFILE", "")
	if got := homeDirectory(); got != "" {
		t.Fatalf("homeDirectory() with no env = %q; want empty", got)
	}
}

// TestHomeDirectoryHomeUnixWins on non-Windows confirms HOME is preferred.
func TestHomeDirectoryHomeUnixWins(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("Unix-only behavior")
	}
	t.Setenv("HOME", "/tmp/myhome")
	t.Setenv("USERPROFILE", "/tmp/profile")
	if got := homeDirectory(); got != "/tmp/myhome" {
		t.Fatalf("homeDirectory() = %q; want /tmp/myhome", got)
	}
}

// TestHomeDirectoryFallback covers the secondary-env branch (line 193).
func TestHomeDirectoryFallback(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("Unix-only path")
	}
	t.Setenv("HOME", "")
	t.Setenv("USERPROFILE", "/tmp/profile-only")
	if got := homeDirectory(); got != "/tmp/profile-only" {
		t.Fatalf("homeDirectory() fallback = %q; want /tmp/profile-only", got)
	}
}

// TestDefaultProjectsRootNoHome covers main.go:203 no-home fallback.
func TestDefaultProjectsRootNoHome(t *testing.T) {
	t.Setenv("HOME", "")
	t.Setenv("USERPROFILE", "")
	got := defaultProjectsRoot()
	want := filepath.Join(".claude", "projects")
	if got != want {
		t.Fatalf("defaultProjectsRoot() = %q; want %q", got, want)
	}
}

// TestDefaultProjectsRootWithHome confirms happy path.
func TestDefaultProjectsRootWithHome(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("Unix-only path")
	}
	t.Setenv("HOME", "/tmp/h")
	t.Setenv("USERPROFILE", "")
	got := defaultProjectsRoot()
	want := filepath.Join("/tmp/h", ".claude", "projects")
	if got != want {
		t.Fatalf("defaultProjectsRoot() = %q; want %q", got, want)
	}
}

// TestParseFloat64 — both happy + error path.
func TestParseFloat64(t *testing.T) {
	v, err := parseFloat64("1.5")
	if err != nil || v != 1.5 {
		t.Fatalf("parseFloat64(1.5) = (%v, %v); want (1.5, nil)", v, err)
	}
	if _, err := parseFloat64("garbage"); err == nil {
		t.Fatalf("parseFloat64(garbage) want error, got nil")
	}
}

// TestRatesForModel covers every rate bucket, the fable/mythos family, the
// date-aware sonnet-5 branch, and the sonnet-4-5 non-collision guard.
func TestRatesForModel(t *testing.T) {
	if in, out := ratesForModel("claude-3-opus", ""); in != 5.0 || out != 25.0 {
		t.Errorf("opus rates = (%v,%v); want (5,25)", in, out)
	}
	if in, out := ratesForModel("HAIKU", ""); in != 1.0 || out != 5.0 {
		t.Errorf("haiku rates = (%v,%v); want (1,5)", in, out)
	}
	if in, out := ratesForModel("claude-3-5-sonnet", ""); in != 3.0 || out != 15.0 {
		t.Errorf("sonnet rates = (%v,%v); want (3,15)", in, out)
	}
	if in, out := ratesForModel("unknown-model", ""); in != 3.0 || out != 15.0 {
		t.Errorf("unknown rates = (%v,%v); want (3,15)", in, out)
	}
	// Fable family ($10/$50), date-independent.
	if in, out := ratesForModel("claude-fable-5", "2026-05-10"); in != 10.0 || out != 50.0 {
		t.Errorf("fable rates = (%v,%v); want (10,50)", in, out)
	}
	if in, out := ratesForModel("claude-mythos-preview", ""); in != 10.0 || out != 50.0 {
		t.Errorf("mythos rates = (%v,%v); want (10,50)", in, out)
	}
	// Sonnet 5: intro through 2026-08-31, standard from 2026-09-01.
	if in, out := ratesForModel("claude-sonnet-5", "2026-05-10"); in != 2.0 || out != 10.0 {
		t.Errorf("sonnet-5 intro rates = (%v,%v); want (2,10)", in, out)
	}
	if in, out := ratesForModel("claude-sonnet-5", "2026-08-31"); in != 2.0 || out != 10.0 {
		t.Errorf("sonnet-5 2026-08-31 rates = (%v,%v); want (2,10)", in, out)
	}
	if in, out := ratesForModel("claude-sonnet-5", "2026-09-01"); in != 3.0 || out != 15.0 {
		t.Errorf("sonnet-5 2026-09-01 rates = (%v,%v); want (3,15)", in, out)
	}
	if in, out := ratesForModel("claude-sonnet-5", ""); in != 2.0 || out != 10.0 {
		t.Errorf("sonnet-5 no-date rates = (%v,%v); want intro (2,10)", in, out)
	}
	// Non-collision: sonnet-4-5 must NOT hit the sonnet-5 branch, stays $3/$15
	// regardless of date.
	if in, out := ratesForModel("claude-sonnet-4-5", "2026-09-15"); in != 3.0 || out != 15.0 {
		t.Errorf("sonnet-4-5 rates = (%v,%v); want (3,15)", in, out)
	}
}

// TestCostForTurnNilUsage — covers main.go:252-254 nil-usage early return.
func TestCostForTurnNilUsage(t *testing.T) {
	if c := costForTurn(nil, "sonnet", ""); c != 0 {
		t.Fatalf("costForTurn(nil) = %v; want 0", c)
	}
}

// TestCostForTurnWebSearchFee covers the web-search flat-fee path.
func TestCostForTurnWebSearchFee(t *testing.T) {
	u := &usage{ServerToolUse: &serverToolUse{WebSearchRequests: 5}}
	got := costForTurn(u, "sonnet", "")
	if got != 0.05 {
		t.Fatalf("costForTurn(web=5) = %v; want 0.05", got)
	}
}

// TestCostForTurnTokenBreakdown — input/output/cache_read/cache_creation.
func TestCostForTurnTokenBreakdown(t *testing.T) {
	u := &usage{
		InputTokens:              1_000_000,  // $3
		OutputTokens:             1_000_000,  // $15
		CacheReadInputTokens:     10_000_000, // 10M * $3 * 0.10 = $3
		CacheCreationInputTokens: 800_000,    // 800k * $3 * 1.25 = $3
	}
	got := costForTurn(u, "sonnet", "")
	want := 3.0 + 15.0 + 3.0 + 3.0
	if got < want-1e-6 || got > want+1e-6 {
		t.Fatalf("costForTurn breakdown = %v; want %v", got, want)
	}
}

// TestParseISO8601Variants — Z, fractional, offset, fail cases.
func TestParseISO8601Variants(t *testing.T) {
	cases := []struct {
		in   string
		want bool // true if parse should succeed
	}{
		{"", false},
		{"garbage", false},
		{"2025-01-01T00:00:00Z", true},
		{"2025-01-01T00:00:00.123Z", true},
		{"2025-01-01T00:00:00+05:30", true},
	}
	for _, c := range cases {
		_, ok := parseISO8601(c.in)
		if ok != c.want {
			t.Errorf("parseISO8601(%q) ok=%v; want %v", c.in, ok, c.want)
		}
	}
}

// TestWantsHelp covers wantsHelp() in main.go (no args / -h / subcommand+help).
func TestWantsHelp(t *testing.T) {
	if !wantsHelp(nil) {
		t.Error("wantsHelp(nil) should be true")
	}
	if !wantsHelp([]string{"-h"}) {
		t.Error("wantsHelp(-h) should be true")
	}
	if !wantsHelp([]string{"--help"}) {
		t.Error("wantsHelp(--help) should be true")
	}
	if !wantsHelp([]string{"search", "-h"}) {
		t.Error("wantsHelp(search -h) should be true")
	}
	if wantsHelp([]string{"--period", "60"}) {
		t.Error("wantsHelp(--period 60) should be false")
	}
}

// TestWalkGroupOpenError — covers main.go:301-303 file open error continue.
func TestWalkGroupOpenError(t *testing.T) {
	r := walkGroup([]string{"/no/such/file.jsonl"}, 0, 0)
	if r.trailing != 0 || r.window != 0 {
		t.Fatalf("walkGroup of missing file = %+v; want zero", r)
	}
}

// TestWalkGroupSkipLadder exercises the per-line skip branches in walkGroup
// (blank line / bad JSON / missing message / non-assistant / dup id /
// missing-ts / unparseable-ts / before-earliest).
func TestWalkGroupSkipLadder(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "session.jsonl")
	body := "\n" +
		"   \n" +
		"{garbage\n" +
		"{}\n" +
		`{"timestamp":"2025-01-01T00:00:00Z","message":{"role":"user"}}` + "\n" +
		`{"timestamp":"2025-01-01T00:00:01Z","message":{"role":"assistant","id":"m1","model":"sonnet","usage":{"input_tokens":1000000,"output_tokens":1000000}}}` + "\n" +
		`{"timestamp":"2025-01-01T00:00:02Z","message":{"role":"assistant","id":"m1","model":"sonnet"}}` + "\n" +
		`{"message":{"role":"assistant","id":"m2","model":"sonnet"}}` + "\n" +
		`{"timestamp":"garbage","message":{"role":"assistant","id":"m3","model":"sonnet"}}` + "\n"
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	r := walkGroup([]string{path}, 0, 0)
	// Only one valid turn (m1 first instance) contributed cost.
	if r.trailing == 0 {
		t.Fatalf("walkGroup trailing = 0; want positive (the m1 turn should count)")
	}
}

// TestDiscoverGroupsParentAndSubagents — verifies both glob layouts get
// discovered. Implicitly exercises the read-dir paths.
func TestDiscoverGroupsParentAndSubagents(t *testing.T) {
	root := t.TempDir()
	slug := filepath.Join(root, "slug")
	if err := os.MkdirAll(slug, 0o755); err != nil {
		t.Fatal(err)
	}
	parentFile := filepath.Join(slug, "sid-1.jsonl")
	if err := os.WriteFile(parentFile, []byte(""), 0o644); err != nil {
		t.Fatal(err)
	}
	subDir := filepath.Join(slug, "sid-2", "subagents")
	if err := os.MkdirAll(subDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(subDir, "agent-a.jsonl"), []byte(""), 0o644); err != nil {
		t.Fatal(err)
	}
	groups := discoverGroups([]string{root}, 0)
	if len(groups) != 2 {
		t.Fatalf("expected 2 groups; got %d (%v)", len(groups), groups)
	}
	if _, ok := groups[groupKey{slug: "slug", sessionID: "sid-1"}]; !ok {
		t.Errorf("missing parent group")
	}
	if _, ok := groups[groupKey{slug: "slug", sessionID: "sid-2"}]; !ok {
		t.Errorf("missing subagent group")
	}
}

// TestDiscoverGroupsMissingRoot — exercises the ReadDir err branch (root
// doesn't exist) plus the Glob no-match path.
func TestDiscoverGroupsMissingRoot(t *testing.T) {
	groups := discoverGroups([]string{"/no/such/root"}, 0)
	if len(groups) != 0 {
		t.Fatalf("expected no groups for missing root; got %v", groups)
	}
}

// TestParseArgumentsMissingValues covers the "flag as last arg" error path for
// every flag that requires a following value.
func TestParseArgumentsMissingValues(t *testing.T) {
	for _, flag := range []string{
		"--period", "--win-start", "--now", "--projects-root", "--extra-projects-root",
	} {
		if _, err := parseArguments([]string{flag}); err == nil {
			t.Errorf("%s as last arg should return an error, got nil", flag)
		}
	}
}

// TestHomeEnvVarsOrdering confirms homeEnvVars returns the correct primary and
// secondary env var names for "windows", "linux", and "darwin".
func TestHomeEnvVarsOrdering(t *testing.T) {
	primary, secondary := homeEnvVars("windows")
	if primary != "USERPROFILE" || secondary != "HOME" {
		t.Errorf("windows: got (%q,%q); want (USERPROFILE, HOME)", primary, secondary)
	}
	primary, secondary = homeEnvVars("linux")
	if primary != "HOME" || secondary != "USERPROFILE" {
		t.Errorf("linux: got (%q,%q); want (HOME, USERPROFILE)", primary, secondary)
	}
	primary, secondary = homeEnvVars("darwin")
	if primary != "HOME" || secondary != "USERPROFILE" {
		t.Errorf("darwin: got (%q,%q); want (HOME, USERPROFILE)", primary, secondary)
	}
}

// TestDiscoverGroupsUnreadableSlugDir verifies that a slug directory that
// cannot be listed (chmod 000) is silently skipped for the subagent walk.
// The parent glob also silently fails on unlistable dirs.
func TestDiscoverGroupsUnreadableSlugDir(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("chmod 000 not supported on Windows")
	}
	if os.Geteuid() == 0 {
		t.Skip("chmod 000 ineffective for root")
	}
	root := t.TempDir()
	slugPath := filepath.Join(root, "locked-slug")
	if err := os.MkdirAll(slugPath, 0o755); err != nil {
		t.Fatal(err)
	}
	// Make the slug dir unreadable so ReadDir inside discoverGroups fails.
	if err := os.Chmod(slugPath, 0o000); err != nil {
		t.Fatal(err)
	}
	defer os.Chmod(slugPath, 0o755) //nolint — best effort cleanup
	groups := discoverGroups([]string{root}, 0)
	if len(groups) != 0 {
		t.Fatalf("expected 0 groups with unreadable slug dir, got %d", len(groups))
	}
}

// TestDiscoverGroupsDanglingSymlinks verifies that dangling symlinks for both
// the parent-glob path (os.Stat fails on the .jsonl symlink) and the subagent
// path are silently skipped via the err != nil continue.
func TestDiscoverGroupsDanglingSymlinks(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("symlinks require elevated privileges on Windows")
	}
	root := t.TempDir()
	slugPath := filepath.Join(root, "myslug")
	if err := os.MkdirAll(slugPath, 0o755); err != nil {
		t.Fatal(err)
	}

	// Dangling symlink at the parent position: <slug>/x.jsonl -> /nonexistent
	danglingParent := filepath.Join(slugPath, "x.jsonl")
	if err := os.Symlink("/no/such/target/x.jsonl", danglingParent); err != nil {
		t.Skipf("symlink unsupported: %v", err)
	}

	// Dangling symlink at the subagent position:
	// <slug>/<sess>/subagents/agent-y.jsonl -> /nonexistent
	sessDir := filepath.Join(slugPath, "mysess")
	subagentsDir := filepath.Join(sessDir, "subagents")
	if err := os.MkdirAll(subagentsDir, 0o755); err != nil {
		t.Fatal(err)
	}
	danglingAgent := filepath.Join(subagentsDir, "agent-y.jsonl")
	if err := os.Symlink("/no/such/target/agent-y.jsonl", danglingAgent); err != nil {
		t.Skipf("symlink unsupported: %v", err)
	}

	// Both should be skipped silently; no groups discovered.
	groups := discoverGroups([]string{root}, 0)
	if len(groups) != 0 {
		t.Fatalf("expected 0 groups with all-dangling symlinks, got %d (%v)", len(groups), groups)
	}
}

// TestDiscoverGroupsPrunesOldFiles — far-future cutoff causes Before() to be
// true for every just-created file, exercising the mtime-prune continue.
func TestDiscoverGroupsPrunesOldFiles(t *testing.T) {
	root := t.TempDir()
	slug := filepath.Join(root, "p")
	if err := os.MkdirAll(slug, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(slug, "sid-1.jsonl"), []byte(""), 0o644); err != nil {
		t.Fatal(err)
	}
	subDir := filepath.Join(slug, "sid-2", "subagents")
	if err := os.MkdirAll(subDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(subDir, "agent-a.jsonl"), []byte(""), 0o644); err != nil {
		t.Fatal(err)
	}
	// Earliest far in the future (but within int64 nanos) → everything is
	// "before" earliest. 4e9 seconds ≈ year 2096; 4e9 * 1e9 < int64 max.
	groups := discoverGroups([]string{root}, 4e9)
	if len(groups) != 0 {
		t.Fatalf("expected pruning to drop everything; got %v", groups)
	}
}
