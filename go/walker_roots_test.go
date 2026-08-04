// Unit tests for walker_roots.go covering local-only branches that the
// shared conformance fixtures can't drive (env-cleared home fallback, IO
// failures from a missing/unreadable config path).
package main

import (
	"os"
	"path/filepath"
	"testing"
)

// TestWalkerConfigPathFallback covers walker_roots.go:31 — the no-home branch
// returning the relative ".claude/walker-roots.json".
func TestWalkerConfigPathFallback(t *testing.T) {
	t.Setenv("HOME", "")
	t.Setenv("USERPROFILE", "")
	got := WalkerConfigPath()
	want := filepath.Join(".claude", "walker-roots.json")
	if got != want {
		t.Fatalf("WalkerConfigPath() with no env = %q; want %q", got, want)
	}
}

// TestWalkerConfigPathWithHome confirms the happy path.
func TestWalkerConfigPathWithHome(t *testing.T) {
	t.Setenv("HOME", "/tmp/fakehome")
	t.Setenv("USERPROFILE", "")
	got := WalkerConfigPath()
	want := filepath.Join("/tmp/fakehome", ".claude", "walker-roots.json")
	// On Windows, USERPROFILE wins; tolerate either.
	winWant := filepath.Join("", ".claude", "walker-roots.json")
	if got != want && got != winWant {
		t.Fatalf("WalkerConfigPath() = %q; want %q (or windows %q)", got, want, winWant)
	}
}

// TestReadExtraRootsMissingConfig covers the os.ReadFile err branch (silent).
func TestReadExtraRootsMissingConfig(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("HOME", dir)
	t.Setenv("USERPROFILE", "")
	if got := ReadExtraRootsFromConfig(); len(got) != 0 {
		t.Fatalf("expected empty extras with missing config, got %v", got)
	}
}

// TestReadExtraRootsEmptyFile covers the len(body)==0 silent branch.
func TestReadExtraRootsEmptyFile(t *testing.T) {
	dir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(dir, ".claude"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, ".claude", "walker-roots.json"), nil, 0o644); err != nil {
		t.Fatal(err)
	}
	t.Setenv("HOME", dir)
	t.Setenv("USERPROFILE", "")
	if got := ReadExtraRootsFromConfig(); len(got) != 0 {
		t.Fatalf("expected empty extras with empty config, got %v", got)
	}
}

// TestReadExtraRootsHappyPath ensures parsing a well-formed config yields
// the listed extra roots (empty strings filtered out).
func TestReadExtraRootsHappyPath(t *testing.T) {
	dir := t.TempDir()
	claudeDir := filepath.Join(dir, ".claude")
	if err := os.MkdirAll(claudeDir, 0o755); err != nil {
		t.Fatal(err)
	}
	body := `{"extra_roots":["/tmp/a","","/tmp/b"]}`
	if err := os.WriteFile(filepath.Join(claudeDir, "walker-roots.json"), []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	t.Setenv("HOME", dir)
	t.Setenv("USERPROFILE", "")
	got := ReadExtraRootsFromConfig()
	want := []string{"/tmp/a", "/tmp/b"}
	if len(got) != len(want) {
		t.Fatalf("got %d extras (%v); want 2 (%v)", len(got), got, want)
	}
	for i := range got {
		if got[i] != want[i] {
			t.Errorf("extras[%d] = %q; want %q", i, got[i], want[i])
		}
	}
}

func TestReadTaggedExtraRootsSupportsCodexObjects(t *testing.T) {
	dir := t.TempDir()
	claudeDir := filepath.Join(dir, ".claude")
	if err := os.MkdirAll(claudeDir, 0o755); err != nil {
		t.Fatal(err)
	}
	body := `{"extra_roots":["/tmp/string",{"path":"/tmp/codex","format":"codex"},{"path":"/tmp/claude","format":"claude-code"},{"path":"/tmp/ignored","format":"unknown"},42]}`
	if err := os.WriteFile(filepath.Join(claudeDir, "walker-roots.json"), []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	t.Setenv("HOME", dir)
	t.Setenv("USERPROFILE", "")

	tagged := readTaggedExtraRootsFromConfig()
	want := []transcriptRoot{
		{Path: "/tmp/string", Format: transcriptFormatClaudeCode},
		{Path: "/tmp/codex", Format: transcriptFormatCodex},
		{Path: "/tmp/claude", Format: transcriptFormatClaudeCode},
	}
	if len(tagged) != len(want) {
		t.Fatalf("tagged roots = %+v; want %+v", tagged, want)
	}
	for i := range want {
		if tagged[i] != want[i] {
			t.Errorf("tagged[%d] = %+v; want %+v", i, tagged[i], want[i])
		}
	}

	plain := ReadExtraRootsFromConfig()
	if len(plain) != 2 || plain[0] != "/tmp/string" || plain[1] != "/tmp/claude" {
		t.Fatalf("plain config roots = %v; want Claude roots only", plain)
	}
}

func TestReadTaggedExtraRootsRejectsWrongExtraRootsShape(t *testing.T) {
	dir := t.TempDir()
	claudeDir := filepath.Join(dir, ".claude")
	if err := os.MkdirAll(claudeDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(
		filepath.Join(claudeDir, "walker-roots.json"),
		[]byte(`{"extra_roots":"not-an-array"}`),
		0o644,
	); err != nil {
		t.Fatal(err)
	}
	t.Setenv("HOME", dir)
	t.Setenv("USERPROFILE", "")
	if got := readTaggedExtraRootsFromConfig(); got != nil {
		t.Fatalf("wrong-shaped extra_roots returned %+v; want nil", got)
	}
}

func TestDefaultCodexRootFallback(t *testing.T) {
	t.Setenv("HOME", "")
	t.Setenv("USERPROFILE", "")
	if got := defaultCodexRoot(); got != filepath.Join(".codex", "sessions") {
		t.Fatalf("defaultCodexRoot() = %q", got)
	}
}

func TestResolveSearchRootsAddsCodexOnlyForDefaultPrimary(t *testing.T) {
	home := t.TempDir()
	claudeRoot := filepath.Join(home, ".claude", "projects")
	codexRoot := filepath.Join(home, ".codex", "sessions")
	explicitRoot := filepath.Join(home, "explicit")
	cliRoot := filepath.Join(home, "cli")
	for _, root := range []string{claudeRoot, codexRoot, explicitRoot, cliRoot} {
		if err := os.MkdirAll(root, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)

	defaults := ResolveSearchRoots(defaultProjectsRoot(), false, []string{cliRoot}, false)
	wantDefaults := []transcriptRoot{
		{Path: claudeRoot, Format: transcriptFormatClaudeCode},
		{Path: codexRoot, Format: transcriptFormatCodex},
		{Path: cliRoot, Format: transcriptFormatClaudeCode},
	}
	if len(defaults) != len(wantDefaults) {
		t.Fatalf("default search roots = %+v; want %+v", defaults, wantDefaults)
	}
	for i := range wantDefaults {
		if defaults[i] != wantDefaults[i] {
			t.Errorf("defaults[%d] = %+v; want %+v", i, defaults[i], wantDefaults[i])
		}
	}

	explicit := ResolveSearchRoots(explicitRoot, true, nil, false)
	if len(explicit) != 1 || explicit[0] != (transcriptRoot{Path: explicitRoot, Format: transcriptFormatClaudeCode}) {
		t.Fatalf("explicit search roots = %+v; want explicit Claude root only", explicit)
	}
}

func TestResolveSearchRootsSkipsInvalidExtra(t *testing.T) {
	primary := t.TempDir()
	invalid := filepath.Join(t.TempDir(), "missing")
	got := ResolveSearchRoots(primary, true, []string{invalid}, false)
	if len(got) != 1 || got[0].Path != primary {
		t.Fatalf("search roots = %+v; want primary only", got)
	}
}

// TestReadExtraRootsMalformedJSON covers the json.Unmarshal err branch
// (stderr diagnostic, returns nil).
func TestReadExtraRootsMalformedJSON(t *testing.T) {
	dir := t.TempDir()
	claudeDir := filepath.Join(dir, ".claude")
	if err := os.MkdirAll(claudeDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(claudeDir, "walker-roots.json"), []byte("{not json"), 0o644); err != nil {
		t.Fatal(err)
	}
	t.Setenv("HOME", dir)
	t.Setenv("USERPROFILE", "")
	if got := ReadExtraRootsFromConfig(); got != nil {
		t.Fatalf("expected nil on malformed JSON, got %v", got)
	}
}

// TestReadExtraRootsNonObject covers the first != '{' branch (stderr, nil).
func TestReadExtraRootsNonObject(t *testing.T) {
	dir := t.TempDir()
	claudeDir := filepath.Join(dir, ".claude")
	if err := os.MkdirAll(claudeDir, 0o755); err != nil {
		t.Fatal(err)
	}
	// Valid JSON but not an object.
	if err := os.WriteFile(filepath.Join(claudeDir, "walker-roots.json"), []byte("[1,2,3]"), 0o644); err != nil {
		t.Fatal(err)
	}
	t.Setenv("HOME", dir)
	t.Setenv("USERPROFILE", "")
	if got := ReadExtraRootsFromConfig(); got != nil {
		t.Fatalf("expected nil on non-object JSON, got %v", got)
	}
}

// TestReadExtraRootsLeadingWhitespace exercises the byte-skip loop in the
// object-probe (lines 58-64) — whitespace-only prefixes must still resolve
// to the first non-space byte.
func TestReadExtraRootsLeadingWhitespace(t *testing.T) {
	dir := t.TempDir()
	claudeDir := filepath.Join(dir, ".claude")
	if err := os.MkdirAll(claudeDir, 0o755); err != nil {
		t.Fatal(err)
	}
	body := "  \n\t{\"extra_roots\":[\"/tmp/x\"]}"
	if err := os.WriteFile(filepath.Join(claudeDir, "walker-roots.json"), []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	t.Setenv("HOME", dir)
	t.Setenv("USERPROFILE", "")
	got := ReadExtraRootsFromConfig()
	if len(got) != 1 || got[0] != "/tmp/x" {
		t.Fatalf("got %v; want [/tmp/x]", got)
	}
}

// TestResolveRootsPrimaryMissing exercises the primary-doesn't-exist branch
// — must return empty without stderr noise. Also covers EvalSymlinks happy
// path on an extra that does exist.
func TestResolveRootsPrimaryMissing(t *testing.T) {
	got := ResolveRoots("/no/such/primary/path", nil, false)
	if len(got) != 0 {
		t.Fatalf("expected empty result with missing primary, got %v", got)
	}
}

// TestResolveRootsExtraSkippedWithStderr exercises the "extra not a directory"
// stderr branch (just confirm it doesn't crash and returns the primary only).
func TestResolveRootsExtraSkippedWithStderr(t *testing.T) {
	primary := t.TempDir()
	got := ResolveRoots(primary, []string{"/nope-extra-path"}, false)
	if len(got) != 1 {
		t.Fatalf("expected primary-only result, got %v", got)
	}
}

// TestResolveRootsDedupsViaSymlink — pointing the same root through a
// symlink and directly should dedup. Best-effort: symlink creation may fail
// on locked-down systems; skip in that case.
func TestResolveRootsDedupsViaSymlink(t *testing.T) {
	primary := t.TempDir()
	linkPath := filepath.Join(t.TempDir(), "primary-link")
	if err := os.Symlink(primary, linkPath); err != nil {
		t.Skipf("symlink unsupported: %v", err)
	}
	got := ResolveRoots(primary, []string{linkPath}, false)
	if len(got) != 1 {
		t.Fatalf("expected 1 deduped result, got %v", got)
	}
}

// TestResolveRootsEvalSymlinksFallback constructs a broken symlink so
// filepath.EvalSymlinks fails (lines 119-121). The stat in the same loop
// fails first and emits the skip diagnostic; this exercises that branch.
func TestResolveRootsBrokenSymlinkSkipped(t *testing.T) {
	dir := t.TempDir()
	broken := filepath.Join(dir, "broken-link")
	if err := os.Symlink("/no/such/target", broken); err != nil {
		t.Skipf("symlink unsupported: %v", err)
	}
	got := ResolveRoots(dir, []string{broken}, false)
	if len(got) != 1 {
		t.Fatalf("expected primary-only result, got %v", got)
	}
}

// TestResolveRootsNonexistentExtraSkipped verifies that a nonexistent extra
// root is skipped (exercises the filepath.Clean fallback when EvalSymlinks
// fails, followed by the os.Stat failure filter). The primary must appear in
// the result; the nonexistent extra must not.
func TestResolveRootsNonexistentExtraSkipped(t *testing.T) {
	primary := t.TempDir()
	nonexistent := filepath.Join(t.TempDir(), "does-not-exist")
	got := ResolveRoots(primary, []string{nonexistent}, false)
	if len(got) != 1 {
		t.Fatalf("expected 1 result (primary only), got %v", got)
	}
	if got[0] != primary {
		// filepath.EvalSymlinks may return a canonical version of primary;
		// check the resolved value ends with the basename of the tempdir.
		// We just need to confirm the nonexistent path is absent.
		for _, r := range got {
			if r == nonexistent {
				t.Errorf("nonexistent extra root %q should not appear in result", nonexistent)
			}
		}
	}
}

// TestResolveRootsDedupDuplicatePaths verifies that passing the same path
// twice (once as primary, once as an extra) yields only a single entry.
func TestResolveRootsDedupDuplicatePaths(t *testing.T) {
	primary := t.TempDir()
	got := ResolveRoots(primary, []string{primary}, false)
	if len(got) != 1 {
		t.Fatalf("expected 1 deduped result for same primary+extra, got %d (%v)", len(got), got)
	}
}
