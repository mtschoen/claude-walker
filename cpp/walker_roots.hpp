// Roots discovery: default root + extras from ~/.claude/walker-roots.json
// + extras from CLI flags. Deduped via fs::canonical, filtered to
// existing directories.
//
// Failure modes follow the SPEC contract:
//   * Missing config file -> no extras (silent).
//   * Malformed JSON -> stderr diagnostic, treat as no extras.
//   * Listed path doesn't exist on disk -> skip silently (stderr).
//   * canonical() fails (broken symlink etc) -> fall back to lexically_normal.

#ifndef WALKER_ROOTS_HPP
#define WALKER_ROOTS_HPP

#include "common.hpp"

#include <filesystem>
#include <fstream>
#include <iostream>
#include <optional>
#include <sstream>
#include <string>
#include <unordered_set>
#include <vector>

#include <simdjson.h>

namespace walker {

namespace sj = simdjson;

enum class TranscriptFormat { ClaudeCode, Codex };

struct TranscriptRoot {
  fs::path path;
  TranscriptFormat format = TranscriptFormat::ClaudeCode;
};

inline fs::path walker_config_path() {
  if (auto home = home_directory())
    return fs::path(*home) / ".claude" / "walker-roots.json";
  return fs::path(".claude/walker-roots.json");
}

// Parse extras from `~/.claude/walker-roots.json`. Returns empty vector on
// any failure (with a stderr diagnostic for malformed JSON specifically).
inline std::vector<TranscriptRoot> read_tagged_extra_roots_from_config() {
  fs::path config = walker_config_path();
  std::error_code ec;
  if (!fs::exists(config, ec))
    return {};

  std::ifstream in(config);
  if (!in)
    return {};
  std::ostringstream buf;
  buf << in.rdbuf();
  std::string body = buf.str();
  if (body.empty())
    return {};

  sj::dom::parser parser;
  sj::padded_string padded(body);
  sj::dom::element doc;
  if (parser.parse(padded).get(doc) != sj::SUCCESS) {
    std::cerr << "walker: malformed " << config.string()
              << " -- ignoring extra roots\n";
    return {};
  }
  sj::dom::object root;
  if (doc.get_object().get(root) != sj::SUCCESS) {
    std::cerr << "walker: " << config.string()
              << " is not a JSON object -- ignoring\n";
    return {};
  }

  sj::dom::array arr;
  if (root["extra_roots"].get_array().get(arr) != sj::SUCCESS)
    return {};

  std::vector<TranscriptRoot> extras;
  for (auto element : arr) {
    std::string_view path_view;
    if (element.get_string().get(path_view) == sj::SUCCESS) {
      if (!path_view.empty())
        extras.push_back(
            {fs::path(std::string(path_view)), TranscriptFormat::ClaudeCode});
      continue;
    }

    sj::dom::object tagged;
    if (element.get_object().get(tagged) != sj::SUCCESS)
      continue;
    if (tagged["path"].get_string().get(path_view) != sj::SUCCESS ||
        path_view.empty())
      continue;
    std::string_view format_view;
    if (tagged["format"].get_string().get(format_view) != sj::SUCCESS)
      continue;
    TranscriptFormat format;
    if (format_view == "claude-code")
      format = TranscriptFormat::ClaudeCode;
    else if (format_view == "codex")
      format = TranscriptFormat::Codex;
    else
      continue;
    extras.push_back({fs::path(std::string(path_view)), format});
  }
  return extras;
}

inline std::vector<fs::path> read_extra_roots_from_config() {
  std::vector<fs::path> extras;
  for (auto &root : read_tagged_extra_roots_from_config()) {
    if (root.format == TranscriptFormat::ClaudeCode)
      extras.push_back(std::move(root.path));
  }
  return extras;
}

inline fs::path default_codex_root() {
  if (auto home = home_directory())
    return fs::path(*home) / ".codex" / "sessions";
  return fs::path(".codex/sessions");
}

inline std::vector<TranscriptRoot>
resolve_search_roots(const std::optional<fs::path> &primary,
                     const std::vector<fs::path> &cli_extras,
                     bool read_config) {
  const bool using_defaults = !primary.has_value();
  std::vector<TranscriptRoot> all;
  all.push_back({primary.value_or(default_projects_root()),
                 TranscriptFormat::ClaudeCode});
  if (using_defaults)
    all.push_back({default_codex_root(), TranscriptFormat::Codex});
  for (const auto &path : cli_extras)
    all.push_back({path, TranscriptFormat::ClaudeCode});
  if (read_config) {
    for (auto &root : read_tagged_extra_roots_from_config())
      all.push_back(std::move(root));
  }

  std::vector<TranscriptRoot> result;
  std::unordered_set<std::string> seen;
  for (size_t index = 0; index < all.size(); ++index) {
    auto &root = all[index];
    std::error_code ec;
    if (!fs::is_directory(root.path, ec)) {
      std::error_code exists_ec;
      if (index > 0 && fs::exists(root.path, exists_ec)) {
        std::cerr << "walker: extra root not a directory, skipping: "
                  << root.path.string() << "\n";
      }
      continue;
    }
    fs::path canonical = fs::canonical(root.path, ec);
    if (ec)
      canonical = root.path.lexically_normal();
    std::string key(1, root.format == TranscriptFormat::Codex ? 'X' : 'C');
    key.push_back('\0');
    key.append(canonical.string());
    if (seen.insert(key).second)
      result.push_back(std::move(root));
  }
  return result;
}

// Resolve the effective root list:
//   [primary] + cli_extras + (config extras if read_config)
//   -> dedup via canonical
//   -> filter to existing directories
inline std::vector<fs::path>
resolve_roots(const fs::path &primary, const std::vector<fs::path> &cli_extras,
              bool read_config) {
  std::vector<fs::path> all;
  all.push_back(primary);
  for (const auto &p : cli_extras)
    all.push_back(p);
  if (read_config) {
    for (const auto &p : read_extra_roots_from_config())
      all.push_back(p);
  }

  std::vector<fs::path> result;
  std::unordered_set<std::string> seen;
  for (const auto &p : all) {
    std::error_code ec;
    if (!fs::exists(p, ec) || !fs::is_directory(p, ec)) {
      if (&p != &all[0]) { // primary is allowed to not exist; that's the
                           // empty-fleet case
        std::cerr << "walker: extra root not a directory, skipping: "
                  << p.string() << "\n";
      }
      continue;
    }
    fs::path canon = fs::canonical(p, ec);
    if (ec)
      canon = p.lexically_normal();
    std::string key = canon.string();
    if (seen.insert(key).second) {
      // Canonical is the dedup key ONLY; walk the original path (SPEC
      // "Roots": canonical form of a mapped network drive can be a
      // \\?\UNC path some walkers cannot enumerate, and it leaks into
      // host_root output).
      result.push_back(p);
    }
  }
  return result;
}

} // namespace walker

#endif // WALKER_ROOTS_HPP
