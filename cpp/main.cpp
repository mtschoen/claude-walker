// Native pace-walker -- C++ implementation.
// See ../SPEC.md for the contract every implementation must honor.
//
// Entry point + cost-mode walker. Beacon-mode subcommands live in
// beacons.cpp and shared helpers (ISO 8601, default root) in common.hpp.

#include "beacons.hpp"
#include "common.hpp"
#include "cost_walk.hpp"
#include "discovery.hpp"
#include "events.hpp"
#include "pricing.hpp"
#include "search.hpp"
#include "walker_roots.hpp"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <optional>
#include <sstream>
#include <string>
#include <string_view>
#include <thread>
#include <unordered_map>
#include <unordered_set>
#include <vector>

// simdjson on-demand (built from source by CMake)
#include <simdjson.h>

namespace fs = std::filesystem;
namespace sj = simdjson;

// ---------------------------------------------------------------------------
// Argument parsing
// ---------------------------------------------------------------------------

struct Args {
  uint64_t period_seconds = 0;
  double win_start_unix = 0.0;
  std::optional<double> now_unix;
  std::optional<fs::path> projects_root;
  std::vector<fs::path> extra_projects_roots;
  bool read_config = true;
};

static const char *const HELP =
    R"(agent-walker - fast cost & progress walker over Claude Code transcripts

USAGE:
    agent-walker [SUBCOMMAND] [OPTIONS]

With no subcommand it runs `cost` (back-compat for the status line).

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
)";

static bool is_help_flag(const std::string &arg) {
  return arg == "-h" || arg == "--help";
}

// Help is shown when: no args, or the first arg is -h/--help, or the first
// arg is a known subcommand followed by -h/--help. See SPEC.md "Help & usage".
static bool wants_help(const std::vector<std::string> &raw) {
  if (raw.empty())
    return true;
  const std::string &first = raw.front();
  if (is_help_flag(first))
    return true;
  static const char *const subs[] = {"cost", "beacons-latest",
                                     "beacons-history", "search", "events"};
  for (const char *sub : subs) {
    if (first == sub) {
      return raw.size() > 1 && is_help_flag(raw[1]);
    }
  }
  return false;
}

[[noreturn]] static void die(std::string_view message) {
  std::cerr << "walker: " << message << "\n";
  std::cerr << "Run 'agent-walker --help' for usage.\n";
  std::exit(2);
}

static Args parse_args(const std::vector<std::string> &argv) {
  Args args;
  bool win_start_set = false;
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
        win_start_set = true;
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
    } else if (flag == "--version") {
      std::cout << walker::VERSION << "\n";
      std::exit(0);
    } else if (flag == "--extra-projects-root") {
      args.extra_projects_roots.emplace_back(next());
    } else if (flag == "--no-config") {
      args.read_config = false;
    } else {
      die(std::string("unknown flag: ") + flag);
    }
  }
  if (args.period_seconds == 0) {
    die("--period is required");
  }
  if (!win_start_set) {
    die("--win-start is required");
  }
  return args;
}

// Shared helpers from common.hpp + pricing.hpp. Pricing (rates_for/cost_for),
// group_key, and file_mtime_to_unix are shared verbatim with events.cpp.
using walker::cost_for;
using walker::walk_group;
using walker::GroupResult;
using walker::default_projects_root;
using walker::file_mtime_to_unix;
using walker::group_key;
using walker::parse_iso8601;

// ---------------------------------------------------------------------------
// Group walking
// ---------------------------------------------------------------------------

// GroupResult + walk_group moved to cost_walk.hpp so the native unit
// tests can drive the unreadable-transcript arm (this TU owns main()).

// ---------------------------------------------------------------------------
// Discovery
// ---------------------------------------------------------------------------

// Shared fused walk + grouping (see discovery.hpp; replaces the per-mode
// copy that iterated each slug dir twice and reused one error_code).
using walker::discover_groups;
using walker::GroupMap;

// ---------------------------------------------------------------------------
// Subcommand: cost (the original, default-shape walker behavior)
// ---------------------------------------------------------------------------

static int run_cost(const std::vector<std::string> &argv) {
  auto started = std::chrono::steady_clock::now();

  Args args = parse_args(argv);

  double now_unix = args.now_unix.value_or(walker::current_unix());

  double period_cutoff = now_unix - static_cast<double>(args.period_seconds);
  double earliest = std::min(period_cutoff, args.win_start_unix);

  fs::path primary = args.projects_root.value_or(default_projects_root());
  std::vector<fs::path> roots = walker::resolve_roots(
      primary, args.extra_projects_roots, args.read_config);

  GroupMap groups = discover_groups(roots, earliest);

  size_t total_files = 0;
  for (auto &[key, paths] : groups)
    total_files += paths.size();
  size_t total_groups = groups.size();

  // Collect group paths into a vector for parallel dispatch
  std::vector<std::vector<fs::path>> group_list;
  group_list.reserve(total_groups);
  for (auto &[key, paths] : groups) {
    group_list.push_back(std::move(paths));
  }

  // Parallel walk
  size_t num_workers =
      walker::effective_workers(std::thread::hardware_concurrency());

  std::vector<GroupResult> results(total_groups);
  std::atomic<size_t> task_index(0);

  auto run_tasks = [&]() {
    while (true) {
      size_t idx = task_index.fetch_add(1, std::memory_order_relaxed);
      if (idx >= group_list.size())
        break;
      results[idx] =
          walk_group(group_list[idx], period_cutoff, args.win_start_unix);
    }
  };

  std::vector<std::thread> threads;
  // Use num_workers - 1 background threads; main thread also works
  size_t bg_threads = (num_workers > 1) ? num_workers - 1 : 0;
  threads.reserve(bg_threads);
  for (size_t i = 0; i < bg_threads; ++i) {
    threads.emplace_back(run_tasks);
  }
  run_tasks(); // main thread participates
  for (auto &t : threads)
    t.join();

  // Aggregate results
  double trailing = 0.0, window = 0.0;
  for (const auto &r : results) {
    trailing += r.trailing;
    window += r.window;
  }

  auto elapsed_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                        std::chrono::steady_clock::now() - started)
                        .count();

  // Output one JSON line
  std::cout << "{\"trailing_usd\":" << std::fixed << std::setprecision(6)
            << trailing << ",\"window_usd\":" << window
            << ",\"files_walked\":" << total_files
            << ",\"groups\":" << total_groups
            << ",\"elapsed_ms\":" << elapsed_ms << "}\n";

  return 0;
}

// ---------------------------------------------------------------------------
// main — subcommand dispatch
// ---------------------------------------------------------------------------

int main(int argc, char *argv[]) {
  // Decouple std::cout from C stdio: the default sync makes every operator<<
  // a synchronized stdio call, which dominated the high-volume events emit.
  // The buffered emit paths write via std::fwrite, so they stay consistent.
  std::ios_base::sync_with_stdio(false);

  std::vector<std::string> raw;
  raw.reserve(argc > 1 ? argc - 1 : 0);
  for (int i = 1; i < argc; ++i)
    raw.emplace_back(argv[i]);

  if (wants_help(raw)) {
    std::cout << HELP;
    return 0;
  }

  // Subcommand routing. Bare flag invocations (first arg starts with '-')
  // route to cost mode for back-compat.
  std::string subcommand = "cost";
  std::vector<std::string> rest;
  if (!raw.empty()) {
    const std::string &first = raw.front();
    if (first == "cost" || first == "beacons-latest" ||
        first == "beacons-history" || first == "search" || first == "events") {
      subcommand = first;
      rest.assign(raw.begin() + 1, raw.end());
    } else if (!first.empty() && first.front() == '-') {
      rest = raw; // bare-flag -> cost mode
    } else {
      std::cerr << "walker: unknown subcommand: " << first << "\n";
      std::cerr << "Run 'agent-walker --help' for usage.\n";
      return 2;
    }
  }

  if (subcommand == "cost")
    return run_cost(rest);
  if (subcommand == "beacons-latest")
    return walker::beacons::run_latest(rest);
  if (subcommand == "beacons-history")
    return walker::beacons::run_history(rest);
  if (subcommand == "search")
    return walker::search::run(rest);
  // subcommand is one of {"cost", "beacons-latest", "beacons-history",
  // "search", "events"} by construction above; "events" is the final
  // branch — no trailing fallback needed.
  return walker::events::run(rest);
}
