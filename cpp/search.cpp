// Search subcommand: substring/regex match across transcript content.
// See ../SPEC.md "Subcommands" for the contract.
// Uses DOM API for reliable JSON parsing.

#include "search.hpp"
#include "common.hpp"
#include "discovery.hpp"
#include "json_writer.hpp"
#include "walker_roots.hpp"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <optional>
#include <regex>
#include <string>
#include <string_view>
#include <thread>
#include <unordered_set>
#include <vector>

#include <simdjson.h>

namespace walker::search {
namespace fs = std::filesystem;

// === Content extraction ===

static std::string extractText(const simdjson::dom::element &content,
                               bool include_tools) {
  std::string_view sv;
  if (content.get_string().get(sv) == simdjson::SUCCESS) {
    return std::string(sv);
  }
  simdjson::dom::array arr;
  if (content.get_array().get(arr) != simdjson::SUCCESS)
    return "";
  std::string out;
  for (auto block : arr) {
    simdjson::dom::object obj;
    if (block.get_object().get(obj) != simdjson::SUCCESS)
      continue;
    std::string_view btype;
    if (obj["type"].get_string().get(btype) != simdjson::SUCCESS)
      btype = "";
    if (btype == "text") {
      std::string_view t;
      if (obj["text"].get_string().get(t) == simdjson::SUCCESS) {
        if (!out.empty())
          out.push_back('\n');
        out.append(t);
      }
    } else if (include_tools && btype == "tool_use") {
      auto input = obj["input"];
      if (!input.error()) {
        if (!out.empty())
          out.push_back('\n');
        // SPEC "Tool blocks": the input value is indexed as its compact
        // JSON serialization (strings keep their JSON quotes, object key
        // order follows the source). simdjson::to_string minifies.
        out.append(simdjson::to_string(input.value()));
      }
    } else if (include_tools && btype == "tool_result") {
      auto tc = obj["content"];
      if (tc.error())
        continue;
      std::string_view ts;
      if (tc.get_string().get(ts) == simdjson::SUCCESS) {
        if (!out.empty())
          out.push_back('\n');
        out.append(ts);
      } else {
        simdjson::dom::array inner;
        if (tc.get_array().get(inner) == simdjson::SUCCESS) {
          for (auto ib : inner) {
            simdjson::dom::object io;
            if (ib.get_object().get(io) != simdjson::SUCCESS)
              continue;
            std::string_view it;
            if (io["type"].get_string().get(it) == simdjson::SUCCESS &&
                it == "text") {
              std::string_view txt;
              if (io["text"].get_string().get(txt) == simdjson::SUCCESS) {
                if (!out.empty())
                  out.push_back('\n');
                out.append(txt);
              }
            }
          }
        }
      }
    }
  }
  return out;
}

// Extract text from a `type: "queue-operation"` entry. Queue-ops have no
// `message` object — the text lives in the entry's root-level `content` string
// (a bare string). Returns std::nullopt when the field is missing or empty
// (e.g. remove/dequeue ops), so only content-bearing entries (enqueue/popAll)
// surface under --include-queue-ops. Mirrors content.rs::extract_queue_op_text.
static std::optional<std::string>
extractQueueOpText(const simdjson::dom::element &doc) {
  std::string_view sv;
  if (doc["content"].get_string().get(sv) != simdjson::SUCCESS)
    return std::nullopt;
  if (sv.empty())
    return std::nullopt;
  return std::string(sv);
}

static bool isOnlyToolBlocks(const simdjson::dom::element &content) {
  simdjson::dom::array arr;
  if (content.get_array().get(arr) != simdjson::SUCCESS)
    return false;
  bool any = false;
  for (auto block : arr) {
    any = true;
    simdjson::dom::object obj;
    if (block.get_object().get(obj) != simdjson::SUCCESS)
      return false;
    std::string_view t;
    if (obj["type"].get_string().get(t) != simdjson::SUCCESS)
      return false;
    if (t != "tool_use" && t != "tool_result")
      return false;
  }
  return any;
}

// === Scan ===

struct ScanMessage {
  uint32_t line_number;
  double timestamp = 0.0;
  bool has_timestamp = false;
  std::string timestamp_str;
  std::string role;
  std::string text_default;
  std::string text_with_tools;
  bool is_only_tool_blocks = false;
};

// Allocation-free ASCII case-insensitive substring scan: memchr on the
// first byte (both cases), then a per-candidate window compare. Bytes >=
// 0x80 compare as themselves, matching the std::tolower("C" locale)
// comparator this replaces.
static bool containsAsciiCI(std::string_view hay, const std::string &lower) {
  size_t n = lower.size();
  if (n == 0 || hay.size() < n)
    return false;
  unsigned char b0 = static_cast<unsigned char>(lower[0]);
  unsigned char b0_upper =
      static_cast<unsigned char>(std::toupper(static_cast<int>(b0)));
  const char *base = hay.data();
  size_t last_start = hay.size() - n;
  size_t pos = 0;
  while (pos <= last_start) {
    size_t remaining = hay.size() - pos;
    const char *c1 =
        static_cast<const char *>(memchr(base + pos, b0, remaining));
    const char *c2 =
        (b0_upper != b0)
            ? static_cast<const char *>(memchr(base + pos, b0_upper, remaining))
            : nullptr;
    const char *cand = (c1 && c2) ? (c1 < c2 ? c1 : c2) : (c1 ? c1 : c2);
    if (!cand)
      return false;
    size_t i = static_cast<size_t>(cand - base);
    if (i > last_start)
      return false;
    bool match = true;
    for (size_t j = 0; j < n; ++j) {
      unsigned char hb = static_cast<unsigned char>(base[i + j]);
      if (static_cast<char>(std::tolower(static_cast<int>(hb))) != lower[j]) {
        match = false;
        break;
      }
    }
    if (match)
      return true;
    pos = i + 1;
  }
  return false;
}

// Raw-byte necessary-condition check for literal (non --regex) patterns,
// applied to a whole file's bytes before any JSON parsing. Sound because the
// pattern is restricted to ASCII without `"`, `\`, or control bytes, so JSON
// string-escaping never alters an occurrence: if the extracted text contains
// the literal, the raw line bytes do too. False positives only cost a normal
// parse, never an output change. Mirrors rust search.rs PreFilter.
struct LiteralPrefilter {
  bool eligible = false;
  bool sensitive = false;
  std::string original; // pattern as-is (case-sensitive path)
  std::string lower;    // pattern lowercased (insensitive path)
  // Pattern contains k/s: under Unicode simple case folding those also match
  // U+212A KELVIN SIGN / U+017F LONG S (UTF-8 lead bytes 0xE2/0xC5) - when
  // the haystack has either lead byte, pass the file through rather than
  // risk a false negative.
  bool fold_hazard = false;

  static LiteralPrefilter build(const std::string &pattern, bool is_regex,
                                bool case_sensitive) {
    LiteralPrefilter pf;
    if (is_regex || pattern.empty())
      return pf;
    for (unsigned char b : pattern) {
      if (b >= 0x80 || b == '"' || b == '\\' || b < 0x20 || b == 0x7F)
        return pf;
    }
    pf.eligible = true;
    pf.sensitive = case_sensitive;
    pf.original = pattern;
    pf.lower.reserve(pattern.size());
    for (unsigned char b : pattern) {
      pf.lower.push_back(static_cast<char>(std::tolower(static_cast<int>(b))));
      if (b == 'k' || b == 'K' || b == 's' || b == 'S')
        pf.fold_hazard = true;
    }
    return pf;
  }

  bool mightContain(std::string_view hay) const {
    if (!eligible)
      return true;
    if (sensitive)
      return hay.find(original) != std::string_view::npos;
    if (containsAsciiCI(hay, lower))
      return true;
    return fold_hazard && (memchr(hay.data(), 0xE2, hay.size()) != nullptr ||
                           memchr(hay.data(), 0xC5, hay.size()) != nullptr);
  }
};

// `extract_with_tools` controls whether we materialize text_with_tools.
// When the caller asks for the default (no tool blocks), context turns
// also use text_default — so text_with_tools is never read and we skip
// the extra content-array traversal it would cost.
static std::vector<ScanMessage> scanFile(const fs::path &path,
                                         bool extract_with_tools,
                                         bool include_queue_ops,
                                         const LiteralPrefilter *prefilter) {
  std::vector<ScanMessage> out;

  // Memory-map (via padded_string::load) instead of std::ifstream::getline,
  // and feed simdjson a padded_string_view that points into the whole-file
  // buffer instead of per-line std::string copies. Mirrors the I/O shape
  // used by cost mode and the beacons walkers. DOM parser reuses its
  // internal buffers across parse() calls, so this is allocation-light.
  simdjson::padded_string data;
  if (simdjson::padded_string::load(path.string()).get(data) !=
      simdjson::SUCCESS)
    return out;

  std::string_view whole(data);
  if (prefilter && !prefilter->mightContain(whole))
    return out;

  // thread_local: scanFile runs once per file per worker, and a fresh
  // dom::parser per call re-allocates its internal tape/string buffers
  // (one construction per file per fleet walk). Reuse keeps the buffers
  // warm without parser-state sharing across threads - same pattern as
  // parse_beacon_body in beacons.cpp.
  static thread_local simdjson::dom::parser parser;
  std::string_view buffer(data);
  size_t pos = 0;
  uint32_t idx = 0;
  while (pos < buffer.size()) {
    size_t newline = buffer.find('\n', pos);
    size_t end = (newline == std::string_view::npos) ? buffer.size() : newline;
    size_t line_end = end;
    if (line_end > pos && buffer[line_end - 1] == '\r')
      --line_end;
    std::string_view line = buffer.substr(pos, line_end - pos);
    pos = (newline == std::string_view::npos) ? buffer.size() : newline + 1;

    ++idx;
    if (line.empty())
      continue;

    size_t line_off = static_cast<size_t>(line.data() - buffer.data());
    simdjson::padded_string_view view(line.data(), line.size(),
                                      buffer.size() - line_off +
                                          simdjson::SIMDJSON_PADDING);
    simdjson::dom::element doc;
    if (parser.parse(view).get(doc) != simdjson::SUCCESS)
      continue;
    // Queue-operation entries have no `message` object: the text lives in a
    // root-level `content` string. Only indexed when --include-queue-ops is
    // set; content-bearing enqueue/popAll surface, empty remove/dequeue are
    // dropped by extractQueueOpText. They count as role:user. Mirrors
    // search.rs.
    std::string_view doc_type;
    if (doc["type"].get_string().get(doc_type) == simdjson::SUCCESS &&
        doc_type == "queue-operation") {
      if (!include_queue_ops)
        continue;
      auto qtext = extractQueueOpText(doc);
      if (!qtext)
        continue;
      std::string_view qts;
      if (doc["timestamp"].get_string().get(qts) != simdjson::SUCCESS)
        qts = "";
      ScanMessage qm;
      qm.line_number = idx;
      qm.role = "user";
      qm.text_default = *qtext;
      qm.text_with_tools = *qtext;
      qm.is_only_tool_blocks = false;
      if (!qts.empty()) {
        qm.timestamp_str = std::string(qts);
        auto ts_opt = parse_iso8601(qm.timestamp_str);
        if (ts_opt) {
          qm.timestamp = *ts_opt;
          qm.has_timestamp = true;
        }
      }
      out.push_back(std::move(qm));
      continue;
    }
    simdjson::dom::object obj;
    if (doc["message"].get_object().get(obj) != simdjson::SUCCESS)
      continue;
    std::string_view role;
    if (obj["role"].get_string().get(role) != simdjson::SUCCESS || role.empty())
      continue;
    // Include ALL roles (user, assistant) — processFile handles role filtering
    auto content_res = obj["content"];
    if (content_res.error())
      continue;
    auto content = content_res.value();
    std::string_view ts_str;
    if (doc["timestamp"].get_string().get(ts_str) != simdjson::SUCCESS)
      ts_str = "";
    ScanMessage m;
    m.line_number = idx;
    m.role = std::string(role);
    m.text_default = extractText(content, false);
    if (extract_with_tools) {
      m.text_with_tools = extractText(content, true);
    }
    m.is_only_tool_blocks = isOnlyToolBlocks(content);
    if (!ts_str.empty()) {
      m.timestamp_str = std::string(ts_str);
      auto ts_opt = parse_iso8601(m.timestamp_str);
      if (ts_opt) {
        m.timestamp = *ts_opt;
        m.has_timestamp = true;
      }
    }
    out.push_back(std::move(m));
  }
  return out;
}

// === Discovery ===

struct DiscoveredFile {
  fs::path path;
  std::string slug;
  std::string session_id;
  std::string host_root; // the effective root this file was discovered under
};

// Shared fused walk (discovery.hpp): parents + subagents in one pass per
// slug dir, per-call error_codes, and the same mtime conversion as every
// other mode (the old local mtimeAtOrAfter used fractional seconds while
// common.hpp truncates - a latent cross-mode divergence, now gone).
static std::vector<DiscoveredFile> discoverFiles(const fs::path &root,
                                                 std::optional<double> since,
                                                 const std::string *cwd_slug) {
  std::vector<DiscoveredFile> out;
  walker::for_each_transcript(
      std::vector<fs::path>{root}, cwd_slug,
      [&](const fs::path &, const std::string &slug, const std::string &sid,
          const fs::directory_entry &entry) {
        if (since && walker::entry_mtime_before(entry, *since))
          return;
        DiscoveredFile df;
        df.path = entry.path();
        df.slug = slug;
        df.session_id = sid;
        df.host_root = root.string();
        out.push_back(std::move(df));
      });
  return out;
}

// === Pattern matching ===

static bool roleMatches(const std::string &filter, const std::string &role) {
  return filter == "both" || filter == role;
}

// === Snippet generation ===

// Nudge `idx` forward to the next UTF-8 character boundary (mirrors Rust's
// str::is_char_boundary walk in search.rs). A byte is a continuation byte iff
// its top two bits are 10 (0x80..0xBF); idx == size() is always a boundary.
// Without this, a snippet cut can split a multibyte codepoint and emit invalid
// UTF-8 into the JSON `snippet` field. See SPEC.md "Snippet boundaries".
static size_t nudgeCharBoundary(std::string_view text, size_t idx) {
  while (idx < text.size() &&
         (static_cast<unsigned char>(text[idx]) & 0xC0) == 0x80) {
    ++idx;
  }
  return idx;
}

static size_t nudgeToWhitespace(std::string_view text, size_t cut,
                                int direction, size_t max_nudge) {
  if (cut == 0 || cut >= text.size())
    return cut;
  if (direction < 0) {
    size_t lo = cut > max_nudge ? cut - max_nudge : 0;
    for (size_t i = cut; i > lo; --i) {
      if (i > 0 && std::isspace((unsigned char)text[i - 1]))
        return i;
    }
  } else {
    size_t hi = (cut + max_nudge) < text.size() ? cut + max_nudge : text.size();
    for (size_t i = cut; i < hi; ++i) {
      if (std::isspace((unsigned char)text[i]))
        return i;
    }
  }
  return cut;
}

static std::string makeSnippet(std::string_view text,
                               std::pair<size_t, size_t> first_match,
                               uint32_t snippet_chars) {
  size_t half = snippet_chars / 2;
  size_t mstart = first_match.first;
  size_t mend = first_match.second;
  size_t raw_lo = mstart > half ? mstart - half : 0;
  size_t raw_hi = std::min(mend + half, text.size());
  size_t lo = nudgeCharBoundary(text, raw_lo);
  size_t hi = nudgeCharBoundary(text, raw_hi);
  if (lo > 0)
    lo = nudgeToWhitespace(text, lo, -1, 20);
  if (hi < text.size())
    hi = nudgeToWhitespace(text, hi, 1, 20);
  lo = nudgeCharBoundary(text, lo);
  hi = nudgeCharBoundary(text, hi);
  return std::string(text.substr(lo, hi - lo));
}

// === Context ===

struct Ctx {
  std::string role, text, ts;
};

static std::pair<std::vector<Ctx>, std::vector<Ctx>>
buildContextTurns(const std::vector<ScanMessage> &messages, size_t hit_idx,
                  uint32_t context_n) {
  if (context_n == 0)
    return {{}, {}};
  size_t n = context_n;
  std::vector<Ctx> before, after;
  size_t start = hit_idx > n ? hit_idx - n : 0;
  for (size_t i = start; i < hit_idx; ++i) {
    before.push_back({messages[i].role, messages[i].text_default,
                      messages[i].timestamp_str});
  }
  size_t end = std::min(hit_idx + 1 + n, messages.size());
  for (size_t i = hit_idx + 1; i < end; ++i) {
    after.push_back({messages[i].role, messages[i].text_default,
                     messages[i].timestamp_str});
  }
  return {before, after};
}

// === Hit ===

struct Hit {
  double timestamp;
  std::string timestamp_str;
  std::string session_id;
  std::string cwd_slug;
  std::string host_root;
  std::string file_path;
  uint32_t line_number;
  std::string role;
  std::string snippet;
  std::vector<std::pair<size_t, size_t>> match_offsets;
  std::vector<Ctx> context_before;
  std::vector<Ctx> context_after;
};

// === JSON escaping ===
// The shared escaper lives in json_writer.hpp (walker::write_json_string);
// calls below resolve to it unqualified via the enclosing walker namespace.

// === Args ===

struct Args {
  std::string pattern;
  bool regex = false;
  bool case_sensitive = false;
  std::string role = "both";
  std::optional<double> since;
  std::optional<double> until;
  std::string cwd;
  bool any_cwd = false;
  uint32_t context = 1;
  uint32_t limit = 50;
  bool count_only = false;
  bool include_tool_blocks = false;
  bool include_queue_ops = false;
  std::string format = "pretty";
  uint32_t snippet_chars = 240;
  fs::path projects_root;
  std::vector<fs::path> extra_projects_roots;
  bool read_config = true;
  double now = 0;
};

static double parseTimeArg(const std::string &s, double now) {
  std::string trimmed = s;
  while (!trimmed.empty() && std::isspace((unsigned char)trimmed.front()))
    trimmed.erase(trimmed.begin());
  while (!trimmed.empty() && std::isspace((unsigned char)trimmed.back()))
    trimmed.pop_back();
  if (trimmed.empty())
    throw std::runtime_error("empty value");
  char last = trimmed.back();
  if (last == 'd' || last == 'h' || last == 'm' || last == 's') {
    std::string head = trimmed.substr(0, trimmed.size() - 1);
    if (!head.empty() &&
        head.find_first_not_of("0123456789.") == std::string::npos) {
      double n = std::stod(head);
      double mult = last == 'd'   ? 86400.0
                    : last == 'h' ? 3600.0
                    : last == 'm' ? 60.0
                                  : 1.0;
      return now - n * mult;
    }
  }
  auto ts = parse_iso8601(trimmed);
  if (!ts)
    throw std::runtime_error("not RFC3339 or relative: " + trimmed);
  return *ts;
}

static Args parseArgs(const std::vector<std::string> &raw) {
  Args args;
  std::optional<double> now_override;
  std::optional<std::string> since_raw, until_raw;

  for (size_t i = 0; i < raw.size(); ++i) {
    const auto &s = raw[i];
    if (s == "--regex")
      args.regex = true;
    else if (s == "--case-sensitive")
      args.case_sensitive = true;
    else if (s == "--role") {
      if (++i >= raw.size())
        throw std::runtime_error("--role needs a value");
      args.role = raw[i];
      if (args.role != "user" && args.role != "assistant" &&
          args.role != "both")
        throw std::runtime_error("--role: invalid value " + args.role +
                                 "; expected user|assistant|both");
    } else if (s == "--since") {
      if (++i >= raw.size())
        throw std::runtime_error("--since needs a value");
      since_raw = raw[i];
    } else if (s == "--until") {
      if (++i >= raw.size())
        throw std::runtime_error("--until needs a value");
      until_raw = raw[i];
    } else if (s == "--cwd") {
      if (++i >= raw.size())
        throw std::runtime_error("--cwd needs a value");
      args.cwd = raw[i];
    } else if (s == "--any-cwd")
      args.any_cwd = true;
    else if (s == "--context") {
      if (++i >= raw.size())
        throw std::runtime_error("--context needs a value");
      try {
        args.context = (uint32_t)std::stoul(raw[i]);
      } catch (...) {
        throw std::runtime_error("--context: invalid value");
      }
    } else if (s == "--limit") {
      if (++i >= raw.size())
        throw std::runtime_error("--limit needs a value");
      try {
        args.limit = (uint32_t)std::stoul(raw[i]);
      } catch (...) {
        throw std::runtime_error("--limit: invalid value");
      }
    } else if (s == "--count-only")
      args.count_only = true;
    else if (s == "--include-tool-blocks")
      args.include_tool_blocks = true;
    else if (s == "--include-queue-ops")
      args.include_queue_ops = true;
    else if (s == "--format") {
      if (++i >= raw.size())
        throw std::runtime_error("--format needs a value");
      args.format = raw[i];
      if (args.format != "pretty" && args.format != "jsonl")
        throw std::runtime_error("--format: invalid value " + args.format +
                                 "; expected pretty|jsonl");
    } else if (s == "--snippet-chars") {
      if (++i >= raw.size())
        throw std::runtime_error("--snippet-chars needs a value");
      try {
        args.snippet_chars = (uint32_t)std::stoul(raw[i]);
      } catch (...) {
        throw std::runtime_error("--snippet-chars: invalid value");
      }
    } else if (s == "--projects-root") {
      if (++i >= raw.size())
        throw std::runtime_error("--projects-root needs a value");
      args.projects_root = raw[i];
    } else if (s == "--now") {
      if (++i >= raw.size())
        throw std::runtime_error("--now needs a value");
      try {
        now_override = std::stod(raw[i]);
      } catch (...) {
        throw std::runtime_error("--now: invalid value");
      }
    } else if (s == "--extra-projects-root") {
      if (++i >= raw.size())
        throw std::runtime_error("--extra-projects-root needs a value");
      args.extra_projects_roots.push_back(fs::path(raw[i]));
    } else if (s == "--no-config") {
      args.read_config = false;
    } else if (s.rfind("--", 0) == 0)
      throw std::runtime_error("unknown flag: " + s);
    else {
      if (!args.pattern.empty())
        throw std::runtime_error("unexpected positional argument: " + s);
      args.pattern = s;
    }
  }

  if (args.pattern.empty())
    throw std::runtime_error("pattern must be non-empty");
  // cwd empty + any_cwd set -> match all cwds (the default; nothing to do
  // here).
  if (!args.cwd.empty() && args.any_cwd)
    throw std::runtime_error("--cwd and --any-cwd are mutually exclusive");
  args.projects_root =
      args.projects_root.empty() ? default_projects_root() : args.projects_root;
  args.now = now_override.value_or(current_unix());
  if (since_raw)
    args.since = parseTimeArg(*since_raw, args.now);
  if (until_raw)
    args.until = parseTimeArg(*until_raw, args.now);
  return args;
}

// === Matcher ===
//
// MSVC's std::regex is materially slow even for trivial literal patterns,
// and the default search invocation is a literal substring (regex_replace
// escapes regex meta-chars at search.cpp:run). Use a small Matcher that
// branches: literal substring fast path (std::string_view::find for
// case-sensitive, std::search + tolower comparator for case-insensitive)
// and std::regex only when --regex was passed.
//
// Non-overlapping match semantics match std::sregex_iterator (advance by
// match length after each hit).
struct Matcher {
  bool is_regex = false;
  bool case_sensitive = false;
  std::string pattern; // literal pattern when !is_regex
  std::regex re;       // compiled regex when is_regex
  // Literal-pattern accelerators (set by run() for !is_regex):
  bool ascii_literal = false; // pattern is pure ASCII -> memchr fast path
  std::string pattern_lower;  // ASCII-lowercased pattern
  LiteralPrefilter prefilter; // whole-file raw-byte skip (see scanFile)

  std::vector<std::pair<size_t, size_t>> find_all(std::string_view text) const {
    std::vector<std::pair<size_t, size_t>> out;
    if (is_regex) {
      // std::regex_iterator requires a contiguous string with iterators;
      // construct a string view-backed string only here (rare path).
      std::string txt(text);
      for (auto it = std::sregex_iterator(txt.begin(), txt.end(), re);
           it != std::sregex_iterator(); ++it) {
        out.emplace_back((size_t)it->position(),
                         (size_t)(it->position() + it->length()));
      }
      return out;
    }
    if (pattern.empty() || text.size() < pattern.size())
      return out;
    if (case_sensitive) {
      size_t pos = 0;
      while (true) {
        size_t hit = text.find(pattern, pos);
        if (hit == std::string_view::npos)
          break;
        out.emplace_back(hit, hit + pattern.size());
        pos = hit + pattern.size();
      }
    } else if (ascii_literal) {
      // memchr candidate scan on the first byte (both cases), window compare
      // per candidate. Replaces std::search's per-position tolower comparator
      // (O(n*m) with two tolower calls per byte) for the dominant ASCII case;
      // byte-identical match semantics (bytes >= 0x80 compare raw both ways).
      size_t n = pattern_lower.size();
      const char *base = text.data();
      unsigned char b0 = static_cast<unsigned char>(pattern_lower[0]);
      unsigned char b0_upper =
          static_cast<unsigned char>(std::toupper(static_cast<int>(b0)));
      size_t last_start = text.size() - n;
      size_t pos = 0;
      while (pos <= last_start) {
        size_t remaining = text.size() - pos;
        const char *c1 =
            static_cast<const char *>(memchr(base + pos, b0, remaining));
        const char *c2 = (b0_upper != b0)
                             ? static_cast<const char *>(
                                   memchr(base + pos, b0_upper, remaining))
                             : nullptr;
        const char *cand = (c1 && c2) ? (c1 < c2 ? c1 : c2) : (c1 ? c1 : c2);
        if (!cand)
          break;
        size_t i = static_cast<size_t>(cand - base);
        if (i > last_start)
          break;
        bool match = true;
        for (size_t j = 0; j < n; ++j) {
          unsigned char hb = static_cast<unsigned char>(base[i + j]);
          if (static_cast<char>(std::tolower(static_cast<int>(hb))) !=
              pattern_lower[j]) {
            match = false;
            break;
          }
        }
        if (match) {
          out.emplace_back(i, i + n);
          pos = i + n; // non-overlapping, like std::sregex_iterator
        } else {
          pos = i + 1;
        }
      }
    } else {
      auto eq = [](unsigned char a, unsigned char b) {
        return std::tolower(a) == std::tolower(b);
      };
      auto begin = text.begin();
      auto cursor = begin;
      while (true) {
        auto it =
            std::search(cursor, text.end(), pattern.begin(), pattern.end(), eq);
        if (it == text.end())
          break;
        size_t hit = static_cast<size_t>(it - begin);
        out.emplace_back(hit, hit + pattern.size());
        cursor = it + pattern.size();
      }
    }
    return out;
  }
};

// === File processing ===

static std::vector<Hit> processFile(const DiscoveredFile &f, const Args &args,
                                    const Matcher &matcher) {
  auto messages = scanFile(f.path, args.include_tool_blocks,
                           args.include_queue_ops, &matcher.prefilter);
  std::vector<Hit> hits;
  for (size_t idx = 0; idx < messages.size(); ++idx) {
    auto &m = messages[idx];
    if (!roleMatches(args.role, m.role))
      continue;
    if (!args.include_tool_blocks && m.is_only_tool_blocks)
      continue;
    if ((args.since || args.until) && !m.has_timestamp)
      continue;
    if (args.since && m.timestamp < *args.since)
      continue;
    if (args.until && m.timestamp > *args.until)
      continue;
    std::string_view text =
        args.include_tool_blocks
            ? std::string_view(m.text_with_tools.data(),
                               m.text_with_tools.size())
            : std::string_view(m.text_default.data(), m.text_default.size());
    if (text.empty())
      continue;

    std::vector<std::pair<size_t, size_t>> matches = matcher.find_all(text);
    if (matches.empty())
      continue;

    std::string snip = makeSnippet(text, matches[0], args.snippet_chars);
    std::vector<std::pair<size_t, size_t>> snippet_matches =
        matcher.find_all(snip);

    auto [ctx_before, ctx_after] =
        buildContextTurns(messages, idx, args.context);

    Hit h;
    h.timestamp = m.has_timestamp ? m.timestamp : 0.0;
    h.timestamp_str = m.timestamp_str;
    h.session_id = f.session_id;
    h.cwd_slug = f.slug;
    h.host_root = f.host_root;
    h.file_path = f.path.string();
    h.line_number = m.line_number;
    h.role = m.role;
    h.snippet = snip;
    h.match_offsets = snippet_matches;
    h.context_before = ctx_before;
    h.context_after = ctx_after;
    hits.push_back(std::move(h));
  }
  return hits;
}

// === Output ===

static void writeCtxArray(std::ostream &os, const std::vector<Ctx> &ctx) {
  os.put('[');
  for (size_t i = 0; i < ctx.size(); ++i) {
    if (i > 0)
      os << ',';
    os << "{\"role\":";
    write_json_string(os, ctx[i].role);
    os << ",\"text\":";
    write_json_string(os, ctx[i].text);
    os << ",\"timestamp\":";
    write_json_string(os, ctx[i].ts);
    os << '}';
  }
  os << ']';
}

static void writeHit(std::ostream &os, const Hit &h) {
  os << "{\"type\":\"hit\",";
  os << "\"session_id\":";
  write_json_string(os, h.session_id);
  os << ",\"cwd_slug\":";
  write_json_string(os, h.cwd_slug);
  os << ",\"host_root\":";
  write_json_string(os, h.host_root);
  os << ",\"file_path\":";
  write_json_string(os, h.file_path);
  os << ",\"line_number\":" << h.line_number;
  os << ",\"timestamp\":";
  write_json_string(os, h.timestamp_str);
  os << ",\"role\":";
  write_json_string(os, h.role);
  os << ",\"snippet\":";
  write_json_string(os, h.snippet);
  os << ",\"match_offsets\":[";
  for (size_t i = 0; i < h.match_offsets.size(); ++i) {
    if (i > 0)
      os << ',';
    os << '[' << h.match_offsets[i].first << ',' << h.match_offsets[i].second
       << ']';
  }
  os << "],\"context_before\":";
  writeCtxArray(os, h.context_before);
  os << ",\"context_after\":";
  writeCtxArray(os, h.context_after);
  os << "}";
}

static void writeSummary(std::ostream &os, size_t hits_count, size_t sessions,
                         size_t roots, size_t files, bool truncated,
                         long long elapsed_ms) {
  os << "{\"type\":\"summary\",";
  os << "\"hits\":" << hits_count;
  os << ",\"sessions_matched\":" << sessions;
  os << ",\"roots_walked\":" << roots;
  os << ",\"files_walked\":" << files;
  os << ",\"truncated\":" << (truncated ? "true" : "false");
  os << ",\"elapsed_ms\":" << elapsed_ms;
  os << "}";
}

// === Top-level ===

int run(const std::vector<std::string> &argv) {
  auto started = std::chrono::steady_clock::now();

  Args args;
  try {
    args = parseArgs(argv);
  } catch (const std::exception &e) {
    std::cerr << "walker: search: " << e.what() << "\n";
    return 2;
  }

  // Build matcher. Literal patterns get the fast substring path; --regex
  // compiles std::regex with the requested case-sensitivity. We compile
  // regex up-front (rather than per-file) so a bad pattern fails once.
  Matcher matcher;
  matcher.is_regex = args.regex;
  matcher.case_sensitive = args.case_sensitive;
  matcher.prefilter =
      LiteralPrefilter::build(args.pattern, args.regex, args.case_sensitive);
  if (!args.regex) {
    matcher.pattern = args.pattern;
    matcher.ascii_literal =
        !args.case_sensitive &&
        std::all_of(args.pattern.begin(), args.pattern.end(),
                    [](unsigned char b) { return b < 0x80; }) &&
        !args.pattern.empty();
    if (matcher.ascii_literal) {
      matcher.pattern_lower.reserve(args.pattern.size());
      for (unsigned char b : args.pattern)
        matcher.pattern_lower.push_back(
            static_cast<char>(std::tolower(static_cast<int>(b))));
    }
  } else {
    try {
      matcher.re = std::regex(
          args.pattern, args.case_sensitive
                            ? std::regex::ECMAScript
                            : (std::regex::ECMAScript | std::regex::icase));
    } catch (const std::exception &e) {
      std::cerr << "walker: search: bad regex: " << e.what() << "\n";
      return 2;
    }
  }

  // Discover files across all effective roots (primary + CLI extras + config
  // extras from ~/.claude/walker-roots.json). Each DiscoveredFile carries the
  // root it came from as host_root. Mirrors events.cpp / search.rs; the
  // perf-pass-2 search rewrite dropped this multi-root resolution.
  std::string *cwd_slug_ptr = args.cwd.empty() ? nullptr : &args.cwd;
  std::vector<fs::path> roots = walker::resolve_roots(
      args.projects_root, args.extra_projects_roots, args.read_config);
  std::vector<DiscoveredFile> files;
  for (const auto &root : roots) {
    auto root_files = discoverFiles(root, args.since, cwd_slug_ptr);
    files.insert(files.end(), std::make_move_iterator(root_files.begin()),
                 std::make_move_iterator(root_files.end()));
  }
  size_t files_walked = files.size();
  size_t roots_walked = roots.size();

  // Process files in parallel. Work unit = one file; each worker owns a
  // local hits list to avoid contention; merge after join, then sort.
  // scanFile's dom::parser is thread_local (reused across that worker's
  // files, never shared across threads). Matcher::find_all
  // is const and does not mutate the regex/pattern state, so one shared
  // matcher is safe across threads. Mirrors the cost-mode pattern in
  // main.cpp::run_cost.
  size_t num_workers =
      walker::effective_workers(std::thread::hardware_concurrency());

  std::vector<std::vector<Hit>> per_thread_hits(num_workers);
  std::atomic<size_t> task_index(0);

  auto run_tasks = [&](size_t tid) {
    auto &local = per_thread_hits[tid];
    while (true) {
      size_t idx = task_index.fetch_add(1, std::memory_order_relaxed);
      if (idx >= files.size())
        break;
      auto hs = processFile(files[idx], args, matcher);
      local.insert(local.end(), std::make_move_iterator(hs.begin()),
                   std::make_move_iterator(hs.end()));
    }
  };

  std::vector<std::thread> threads;
  size_t bg_threads = (num_workers > 1) ? num_workers - 1 : 0;
  threads.reserve(bg_threads);
  for (size_t i = 0; i < bg_threads; ++i) {
    threads.emplace_back(run_tasks, i + 1);
  }
  run_tasks(0); // main thread participates as worker 0
  for (auto &t : threads)
    t.join();

  // Merge per-thread hits
  std::vector<Hit> hits;
  size_t total = 0;
  for (auto &v : per_thread_hits)
    total += v.size();
  hits.reserve(total);
  for (auto &v : per_thread_hits) {
    hits.insert(hits.end(), std::make_move_iterator(v.begin()),
                std::make_move_iterator(v.end()));
  }

  // Sort newest-first by timestamp; tiebreak (session_id, line_number)
  std::sort(hits.begin(), hits.end(), [](const Hit &a, const Hit &b) {
    if (a.timestamp != b.timestamp)
      return a.timestamp > b.timestamp;
    if (a.session_id != b.session_id)
      return a.session_id < b.session_id;
    return a.line_number < b.line_number;
  });

  // Count distinct sessions BEFORE truncation
  std::unordered_set<std::string> session_set;
  for (auto &h : hits)
    session_set.insert(h.cwd_slug + "/" + h.session_id);
  size_t sessions_matched = session_set.size();
  size_t total_unfiltered = hits.size();
  bool truncated = total_unfiltered > args.limit;
  if (truncated)
    hits.resize(args.limit);

  auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(
                     std::chrono::steady_clock::now() - started)
                     .count();

  size_t hits_output = args.count_only ? total_unfiltered : hits.size();

  if (args.format == "jsonl") {
    if (!args.count_only) {
      for (auto &h : hits) {
        writeHit(std::cout, h);
        std::cout << '\n';
      }
    }
    writeSummary(std::cout, hits_output, sessions_matched, roots_walked,
                 files_walked, truncated, elapsed);
    std::cout << '\n';
  } else {
    // pretty format
    if (!args.count_only) {
      for (auto &h : hits) {
        std::cout << "[" << h.timestamp_str << "] cwd=" << h.cwd_slug
                  << " role=" << h.role << " session=" << h.session_id << "\n";
        std::cout << "  " << h.file_path << ":" << h.line_number << "\n";
        for (auto &t : h.context_before) {
          std::cout << "  before: ";
          if (t.text.size() > 120)
            std::cout << t.text.substr(0, 120) << "…";
          else
            std::cout << t.text;
          std::cout << "\n";
        }
        // match_offsets come from re-running the matcher on a snippet built
        // around the first match, so they are always present and in-bounds;
        // SPEC "Pretty format" omits the snippet line otherwise.
        if (!h.match_offsets.empty()) {
          auto [ms, me] = h.match_offsets[0];
          size_t pm = std::min(ms, h.snippet.size());
          size_t pe = std::min(me, h.snippet.size());
          std::cout << "  >>> " << h.snippet.substr(0, pm) << "["
                    << h.snippet.substr(pm, pe - pm) << "]"
                    << h.snippet.substr(pe) << " <<<\n";
        }
        for (auto &t : h.context_after) {
          std::cout << "  after:  ";
          if (t.text.size() > 120)
            std::cout << t.text.substr(0, 120) << "…";
          else
            std::cout << t.text;
          std::cout << "\n";
        }
        std::cout << "\n";
      }
    }
    // SPEC "Pretty format": the summary is one human-readable line, not a
    // JSONL record (which belongs to --format jsonl only).
    std::cout << hits_output << " hits in " << sessions_matched
              << " sessions across " << roots_walked << " roots ("
              << files_walked << " files). truncated="
              << (truncated ? "true" : "false") << " elapsed " << elapsed
              << "ms.\n";
  }

  if (truncated) {
    std::cerr << "walker: search: truncated to --limit=" << args.limit
              << " (had " << total_unfiltered
              << " total); narrow with --since\n";
  }

  return 0;
}

} // namespace walker::search
