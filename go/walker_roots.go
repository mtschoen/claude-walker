// Roots discovery: primary root + extras from CLI flags + extras from
// ~/.claude/walker-roots.json. Search roots carry a Claude Code or Codex
// format tag; legacy callers continue to receive plain Claude Code paths.
// Roots are deduped via filepath.EvalSymlinks and filtered to directories.
//
// Mirrors cpp/walker_roots.hpp and rust/src/walker_roots.rs. Failure modes
// follow the SPEC.md contract:
//   * Missing config file -> no extras (silent).
//   * Malformed JSON -> stderr diagnostic, treat as no extras (must NOT error).
//   * Listed path doesn't exist on disk -> skip silently with stderr line.
//   * EvalSymlinks() fails (broken symlink etc) -> fall back to filepath.Clean.
//   * Primary is allowed to not exist (empty-fleet case); no stderr for it.
//
// Uses encoding/json (not sonic) because this runs once at startup -- the
// per-MB hot-path parsers don't matter here and stdlib is clearer.

package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
)

// WalkerConfigPath returns the path to ~/.claude/walker-roots.json. Falls
// back to ".claude/walker-roots.json" if neither HOME nor USERPROFILE is set.
func WalkerConfigPath() string {
	if home := homeDirectory(); home != "" {
		return filepath.Join(home, ".claude", "walker-roots.json")
	}
	return filepath.Join(".claude", "walker-roots.json")
}

type transcriptFormat string

const (
	transcriptFormatClaudeCode transcriptFormat = "claude-code"
	transcriptFormatCodex      transcriptFormat = "codex"
)

type transcriptRoot struct {
	Path   string
	Format transcriptFormat
}

type walkerConfig struct {
	ExtraRoots []json.RawMessage `json:"extra_roots"`
}

// ReadExtraRootsFromConfig parses extras from ~/.claude/walker-roots.json.
// Returns nil on any failure; emits a stderr diagnostic for malformed JSON
// or wrong-shape (non-object) bodies specifically.
func ReadExtraRootsFromConfig() []string {
	tagged := readTaggedExtraRootsFromConfig()
	if tagged == nil {
		return nil
	}
	extras := make([]string, 0, len(tagged))
	for _, root := range tagged {
		if root.Format == transcriptFormatClaudeCode {
			extras = append(extras, root.Path)
		}
	}
	return extras
}

func readTaggedExtraRootsFromConfig() []transcriptRoot {
	configPath := WalkerConfigPath()
	body, err := os.ReadFile(configPath)
	if err != nil {
		return nil // missing file or unreadable -- silent
	}
	if len(body) == 0 {
		return nil
	}
	// First check the body is an object (parity with cpp/rust diagnostics).
	var probe json.RawMessage
	if err := json.Unmarshal(body, &probe); err != nil {
		fmt.Fprintf(os.Stderr, "walker: malformed %s -- ignoring extra roots\n", configPath)
		return nil
	}
	// json.Unmarshal into a RawMessage strips leading whitespace, so the
	// first byte of probe is the body's first significant byte.
	if len(probe) == 0 || probe[0] != '{' {
		fmt.Fprintf(os.Stderr, "walker: %s is not a JSON object -- ignoring\n", configPath)
		return nil
	}

	var cfg walkerConfig
	if err := json.Unmarshal(body, &cfg); err != nil {
		fmt.Fprintf(os.Stderr, "walker: malformed %s -- ignoring extra roots\n", configPath)
		return nil
	}
	extras := make([]transcriptRoot, 0, len(cfg.ExtraRoots))
	for _, value := range cfg.ExtraRoots {
		var path string
		if err := json.Unmarshal(value, &path); err == nil {
			if path != "" {
				extras = append(extras, transcriptRoot{Path: path, Format: transcriptFormatClaudeCode})
			}
			continue
		}
		var tagged struct {
			Path   string           `json:"path"`
			Format transcriptFormat `json:"format"`
		}
		if err := json.Unmarshal(value, &tagged); err != nil || tagged.Path == "" {
			continue
		}
		if tagged.Format == transcriptFormatClaudeCode || tagged.Format == transcriptFormatCodex {
			extras = append(extras, transcriptRoot{Path: tagged.Path, Format: tagged.Format})
		}
	}
	return extras
}

func defaultCodexRoot() string {
	if home := homeDirectory(); home != "" {
		return filepath.Join(home, ".codex", "sessions")
	}
	return filepath.Join(".codex", "sessions")
}

// ResolveSearchRoots preserves the format associated with every search root.
// CLI extras and string config entries are Claude Code roots. The default
// Codex sessions root is included only when --projects-root was not explicit.
func ResolveSearchRoots(primary string, primaryExplicit bool, cliExtras []string, readConfig bool) []transcriptRoot {
	type candidate struct {
		root            transcriptRoot
		diagnoseInvalid bool
	}
	combined := []candidate{{
		root: transcriptRoot{Path: primary, Format: transcriptFormatClaudeCode},
	}}
	if !primaryExplicit {
		combined = append(combined, candidate{
			root: transcriptRoot{Path: defaultCodexRoot(), Format: transcriptFormatCodex},
		})
	}
	for _, path := range cliExtras {
		combined = append(combined, candidate{
			root:            transcriptRoot{Path: path, Format: transcriptFormatClaudeCode},
			diagnoseInvalid: true,
		})
	}
	if readConfig {
		for _, root := range readTaggedExtraRootsFromConfig() {
			combined = append(combined, candidate{root: root, diagnoseInvalid: true})
		}
	}

	var result []transcriptRoot
	seen := make(map[string]struct{})
	for _, candidate := range combined {
		canonical, err := filepath.EvalSymlinks(candidate.root.Path)
		if err != nil {
			canonical = filepath.Clean(candidate.root.Path)
		}
		key := string(candidate.root.Format) + "\x00" + canonical
		if _, exists := seen[key]; exists {
			continue
		}
		seen[key] = struct{}{}
		info, err := os.Stat(candidate.root.Path)
		if err != nil || !info.IsDir() {
			if candidate.diagnoseInvalid {
				fmt.Fprintf(os.Stderr,
					"walker: extra root not a directory, skipping: %s\n", candidate.root.Path)
			}
			continue
		}
		result = append(result, candidate.root)
	}
	return result
}

// ResolveRoots assembles the effective root list:
//
//	[primary] + cliExtras + (config extras if readConfig)
//	-> dedup via canonical (EvalSymlinks, fall back to Clean)
//	-> filter to existing directories
//
// Primary is allowed to not exist (empty-fleet case) and emits no diagnostic
// in that scenario. Extras that fail the existence/directory check are
// skipped with a stderr diagnostic matching cpp/rust output.
func ResolveRoots(primary string, cliExtras []string, readConfig bool) []string {
	type candidate struct {
		path      string
		isPrimary bool
	}
	combined := []candidate{{path: primary, isPrimary: true}}
	for _, p := range cliExtras {
		combined = append(combined, candidate{path: p})
	}
	if readConfig {
		for _, p := range ReadExtraRootsFromConfig() {
			combined = append(combined, candidate{path: p})
		}
	}

	var result []string
	seen := make(map[string]struct{})
	for _, c := range combined {
		// Dedup key per SPEC "Resolution": canonical form, falling back to
		// the lexically-normalized path when canonicalization fails (e.g.
		// a nonexistent extra). Canonicalize BEFORE the existence filter,
		// matching rust/cpp, so the fallback is reachable.
		canonical, err := filepath.EvalSymlinks(c.path)
		if err != nil {
			canonical = filepath.Clean(c.path)
		}
		if _, exists := seen[canonical]; exists {
			continue
		}
		seen[canonical] = struct{}{}
		info, err := os.Stat(c.path)
		if err != nil || !info.IsDir() {
			if !c.isPrimary {
				fmt.Fprintf(os.Stderr,
					"walker: extra root not a directory, skipping: %s\n", c.path)
			}
			continue
		}
		// Canonical is the dedup key ONLY; walk the original path (SPEC
		// "Roots": canonicalizing a mapped network drive can yield a
		// \\?\UNC form some walkers cannot enumerate, and it leaks into
		// host_root output).
		result = append(result, c.path)
	}
	return result
}
