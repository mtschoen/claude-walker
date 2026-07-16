// events subcommand: emit one NDJSON record per accepted assistant turn.
// Reuses cost-mode's parse/dedup/filter/pricing logic; only aggregation
// differs (per-turn NDJSON output instead of accumulated totals).
// See ../SPEC.md §events for the full contract.

#include "events.hpp"
#include "common.hpp"
#include "discovery.hpp"
#include "json_writer.hpp"
#include "pricing.hpp"
#include "walker_roots.hpp"

#include <algorithm>
#include <atomic>
#include <cctype>
#include <charconv>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <iomanip>
#include <iostream>
#include <mutex>
#include <optional>
#include <sstream>
#include <string>
#include <string_view>
#include <thread>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#include <simdjson.h>

namespace walker::events {

namespace fs = std::filesystem;
namespace sj = simdjson;

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

struct EventRecord {
  double ts;
  double usd;
  std::string model;
  std::string session_id;
  std::string slug;
};

// Pricing (rates_for / cost_for) lives in pricing.hpp and the JSON string
// writer in json_writer.hpp — the same shared definitions cost mode and the
// search subcommand use. Both resolve unqualified here via the enclosing
// walker namespace.

// ---------------------------------------------------------------------------
// Argument parsing
// ---------------------------------------------------------------------------

struct Args {
  uint64_t period_seconds = 0;
  double win_start_unix = 0.0;
  bool win_start_set = false;
  std::optional<double> now_unix;
  std::optional<fs::path> projects_root;
  std::vector<fs::path> extra_projects_roots;
  bool read_config = true;
};

[[noreturn]] static void die(std::string_view message) {
  std::cerr << "walker: events: " << message << "\n";
  std::exit(2);
}

static Args parse_args(const std::vector<std::string> &argv) {
  Args args;
  for (size_t i = 0; i < argv.size(); ++i) {
    const std::string &flag = argv[i];
    auto next = [&]() -> const std::string & {
      if (i + 1 >= argv.size())
        die(flag + " needs a value");
      return argv[++i];
    };
    if (flag == "--period") {
      try {
        args.period_seconds = std::stoull(next());
      } catch (...) {
        die("--period: invalid integer");
      }
    } else if (flag == "--win-start") {
      try {
        args.win_start_unix = std::stod(next());
        args.win_start_set = true;
      } catch (...) {
        die("--win-start: invalid number");
      }
    } else if (flag == "--now") {
      try {
        args.now_unix = std::stod(next());
      } catch (...) {
        die("--now: invalid number");
      }
    } else if (flag == "--projects-root") {
      args.projects_root = fs::path(next());
    } else if (flag == "--extra-projects-root") {
      args.extra_projects_roots.emplace_back(next());
    } else if (flag == "--no-config") {
      args.read_config = false;
    } else if (flag == "--version") {
      std::cout << walker::VERSION << "\n";
      std::exit(0);
    } else {
      die(std::string("unknown flag: ") + flag);
    }
  }
  if (args.period_seconds == 0) {
    die("--period is required");
  }
  return args;
}

// ---------------------------------------------------------------------------
// Discovery
// ---------------------------------------------------------------------------

// Shared fused walk + grouping (see discovery.hpp; replaces the per-mode
// copy that iterated each slug dir twice and reused one error_code).
using walker::discover_groups;
using walker::GroupMap;

// ---------------------------------------------------------------------------
// Per-group walker
// ---------------------------------------------------------------------------

static std::vector<EventRecord>
walk_group_events(const std::vector<fs::path> &paths, const std::string &slug,
                  const std::string &session_id, double cutoff) {
  std::vector<EventRecord> records;
  std::unordered_set<std::string> seen_ids;

  sj::ondemand::parser parser;

  for (const auto &path : paths) {
    sj::padded_string data;
    if (sj::padded_string::load(path.string()).get(data) != sj::SUCCESS)
      continue;

    std::string_view buffer(data);
    size_t pos = 0;
    while (pos < buffer.size()) {
      size_t newline = buffer.find('\n', pos);
      size_t end =
          (newline == std::string_view::npos) ? buffer.size() : newline;
      size_t line_end = end;
      if (line_end > pos && buffer[line_end - 1] == '\r')
        --line_end;
      std::string_view line = buffer.substr(pos, line_end - pos);
      pos = (newline == std::string_view::npos) ? buffer.size() : newline + 1;

      // Skip blank lines
      bool blank = true;
      for (char c : line) {
        if (!std::isspace(static_cast<unsigned char>(c))) {
          blank = false;
          break;
        }
      }
      if (blank)
        continue;

      size_t line_off = static_cast<size_t>(line.data() - buffer.data());
      sj::padded_string_view view(line.data(), line.size(),
                                  buffer.size() - line_off +
                                      sj::SIMDJSON_PADDING);
      sj::ondemand::document doc;
      if (parser.iterate(view).get(doc) != sj::SUCCESS)
        continue;

      sj::ondemand::object root;
      if (doc.get_object().get(root) != sj::SUCCESS)
        continue;

      std::string_view timestamp_view;
      bool has_timestamp = false;
      bool is_assistant = false;
      std::string_view message_id_view;
      bool has_message_id = false;
      std::string model;
      uint64_t input_tokens = 0, output_tokens = 0, cache_read_tokens = 0,
               cache_write_tokens = 0, web_search_requests = 0;
      bool message_seen = false;

      for (auto root_field : root) {
        std::string_view key;
        if (root_field.unescaped_key().get(key) != sj::SUCCESS)
          continue;

        if (key == "timestamp") {
          if (root_field.value().get_string().get(timestamp_view) ==
              sj::SUCCESS) {
            has_timestamp = !timestamp_view.empty();
          }
        } else if (key == "message") {
          sj::ondemand::object msg_obj;
          if (root_field.value().get_object().get(msg_obj) != sj::SUCCESS)
            continue;
          message_seen = true;

          for (auto msg_field : msg_obj) {
            std::string_view msg_key;
            if (msg_field.unescaped_key().get(msg_key) != sj::SUCCESS)
              continue;

            if (msg_key == "role") {
              std::string_view role_view;
              if (msg_field.value().get_string().get(role_view) ==
                  sj::SUCCESS) {
                is_assistant = (role_view == "assistant");
              }
            } else if (msg_key == "id") {
              std::string_view id_view;
              if (msg_field.value().get_string().get(id_view) == sj::SUCCESS) {
                if (!id_view.empty()) {
                  message_id_view = id_view;
                  has_message_id = true;
                }
              }
            } else if (msg_key == "model") {
              std::string_view model_view;
              if (msg_field.value().get_string().get(model_view) ==
                  sj::SUCCESS) {
                model.assign(model_view.data(), model_view.size());
                // Lowercase the model string (matches rust:
                // to_ascii_lowercase())
                for (char &c : model) {
                  c = static_cast<char>(
                      std::tolower(static_cast<unsigned char>(c)));
                }
              }
            } else if (msg_key == "usage") {
              sj::ondemand::object usage_obj;
              if (msg_field.value().get_object().get(usage_obj) != sj::SUCCESS)
                continue;

              for (auto usage_field : usage_obj) {
                std::string_view usage_key;
                if (usage_field.unescaped_key().get(usage_key) != sj::SUCCESS)
                  continue;

                // server_tool_use is a nested object, not a scalar.
                // Descend for web_search_requests before the scalar
                // get_uint64 below (which would skip a non-uint value).
                if (usage_key == "server_tool_use") {
                  sj::ondemand::object stu_obj;
                  if (usage_field.value().get_object().get(stu_obj) !=
                      sj::SUCCESS)
                    continue;
                  for (auto stu_field : stu_obj) {
                    std::string_view stu_key;
                    if (stu_field.unescaped_key().get(stu_key) != sj::SUCCESS)
                      continue;
                    if (stu_key == "web_search_requests")
                      web_search_requests =
                          walker::lenient_count(stu_field.value());
                  }
                  continue;
                }

                uint64_t value = walker::lenient_count(usage_field.value());

                if (usage_key == "input_tokens")
                  input_tokens = value;
                else if (usage_key == "output_tokens")
                  output_tokens = value;
                else if (usage_key == "cache_read_input_tokens")
                  cache_read_tokens = value;
                else if (usage_key == "cache_creation_input_tokens")
                  cache_write_tokens = value;
              }
            }
          }
        }
      }

      // Filter 1: must have message with assistant role
      if (!message_seen || !is_assistant)
        continue;

      // Filter 2: dedup by message.id within this group
      if (has_message_id) {
        std::string mid(message_id_view);
        if (!seen_ids.insert(std::move(mid)).second)
          continue;
      }

      // Filter 3: timestamp must parse
      if (!has_timestamp)
        continue;
      auto ts_opt = walker::parse_iso8601(timestamp_view);
      if (!ts_opt)
        continue;
      double ts = *ts_opt;

      // Filter 4: window predicate — ts >= cutoff (= min(now-period,
      // win_start))
      if (ts < cutoff)
        continue;

      double usd = cost_for(input_tokens, output_tokens, cache_read_tokens,
                            cache_write_tokens, web_search_requests, model,
                            timestamp_view);

      records.push_back(EventRecord{ts, usd, model, session_id, slug});
    }
  }

  return records;
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

// Phase timers for WALKER_PROFILE-driven profiling: prints per-phase wall-clock
// to stderr (discover / walk / sort / emit) so the events hotspot is visible.
// Off unless the env var is set; near-zero overhead in production.
namespace {
struct PhaseTimer {
  bool enabled = std::getenv("WALKER_PROFILE") != nullptr;
  std::chrono::steady_clock::time_point last = std::chrono::steady_clock::now();
  void mark(const char *phase) {
    if (!enabled)
      return;
    auto now = std::chrono::steady_clock::now();
    double ms = std::chrono::duration<double, std::milli>(now - last).count();
    std::cerr << "walker-profile events " << phase << ": " << ms << " ms\n";
    last = now;
  }
};
} // namespace

int run(const std::vector<std::string> &argv) {
  PhaseTimer timer;
  Args args = parse_args(argv);

  double now_unix = args.now_unix.value_or(walker::current_unix());

  // Effective cutoff = min(now - period, win_start), per SPEC §events.
  double period_cutoff = now_unix - static_cast<double>(args.period_seconds);
  // When --win-start is omitted, default to now - period (simplifies the
  // predicate to ts >= now - period, matching rust behavior).
  double win_start = args.win_start_set ? args.win_start_unix : period_cutoff;
  double cutoff = std::min(period_cutoff, win_start);

  fs::path primary =
      args.projects_root.value_or(walker::default_projects_root());
  std::vector<fs::path> roots = walker::resolve_roots(
      primary, args.extra_projects_roots, args.read_config);

  if (roots.empty()) {
    // Primary root doesn't exist — not a hard error; emit no records.
    return 0;
  }

  GroupMap groups = discover_groups(roots, cutoff);
  timer.mark("discover");

  // Decompose group map into a vector of (slug, session_id, paths) tuples
  // for parallel dispatch.
  struct GroupEntry {
    std::string slug;
    std::string session_id;
    std::vector<fs::path> paths;
  };
  std::vector<GroupEntry> group_list;
  group_list.reserve(groups.size());
  for (auto &[key, paths] : groups) {
    // Key is "slug\0session_id" — split on the null separator.
    size_t sep = key.find('\0');
    std::string slug = (sep != std::string::npos) ? key.substr(0, sep) : key;
    std::string sid = (sep != std::string::npos) ? key.substr(sep + 1) : "";
    group_list.push_back(
        GroupEntry{std::move(slug), std::move(sid), std::move(paths)});
  }

  // Parallel walk — mirrors cost-mode's thread pool pattern from main.cpp.
  size_t num_workers =
      walker::effective_workers(std::thread::hardware_concurrency());

  std::vector<std::vector<EventRecord>> per_thread_records(num_workers);
  std::atomic<size_t> task_index(0);

  auto run_tasks = [&](size_t tid) {
    auto &local = per_thread_records[tid];
    while (true) {
      size_t idx = task_index.fetch_add(1, std::memory_order_relaxed);
      if (idx >= group_list.size())
        break;
      auto &ge = group_list[idx];
      auto recs = walk_group_events(ge.paths, ge.slug, ge.session_id, cutoff);
      local.insert(local.end(), std::make_move_iterator(recs.begin()),
                   std::make_move_iterator(recs.end()));
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

  // Merge per-thread records
  std::vector<EventRecord> all_records;
  {
    size_t total = 0;
    for (auto &v : per_thread_records)
      total += v.size();
    all_records.reserve(total);
    for (auto &v : per_thread_records) {
      all_records.insert(all_records.end(), std::make_move_iterator(v.begin()),
                         std::make_move_iterator(v.end()));
    }
  }
  timer.mark("walk+merge");

  // Sort for deterministic output: (ts, session_id, model) — matches SPEC
  // §events §Ordering and the conformance harness's sort key.
  std::sort(all_records.begin(), all_records.end(),
            [](const EventRecord &a, const EventRecord &b) {
              if (a.ts != b.ts)
                return a.ts < b.ts;
              if (a.session_id != b.session_id)
                return a.session_id < b.session_id;
              return a.model < b.model;
            });
  timer.mark("sort");

  // Emit NDJSON — one line per record. Field order: ts, usd, model, session_id,
  // slug per SPEC §events mandate. Build the whole payload in one buffer and
  // write it with a single fwrite: per-record std::cout/operator<< writes
  // (re-applying the fixed/precision manipulators each field) dominated
  // events-mode wall time.
  std::string out;
  out.reserve(all_records.size() * 128);
  char num[64];
  auto append_fixed6 = [&](double value) {
    // std::to_chars(fixed, 6) is markedly faster than snprintf("%.6f") and
    // produces the identical fixed-6-decimal form for these magnitudes.
    auto [ptr, ec] = std::to_chars(num, num + sizeof(num), value,
                                   std::chars_format::fixed, 6);
    // ts and usd are always finite and small enough that the fixed-6 form
    // fits the 64-byte buffer (token counts are u64-bounded by the lenient
    // parser), so to_chars cannot fail; on the impossible failure the field
    // is simply left empty rather than carrying a dead snprintf fallback.
    if (ec == std::errc())
      out.append(num, static_cast<size_t>(ptr - num));
  };
  for (const auto &r : all_records) {
    out += "{\"ts\":";
    append_fixed6(r.ts);
    out += ",\"usd\":";
    append_fixed6(r.usd);
    out += ",\"model\":";
    write_json_string(out, r.model);
    out += ",\"session_id\":";
    write_json_string(out, r.session_id);
    out += ",\"slug\":";
    write_json_string(out, r.slug);
    out += "}\n";
  }
  std::fwrite(out.data(), 1, out.size(), stdout);
  timer.mark("emit");

  return 0;
}

} // namespace walker::events
