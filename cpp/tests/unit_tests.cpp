// Native unit tests for the C++ walker. Drives the error arms the
// conformance harness cannot reach portably: unreadable files (chmod 000),
// dangling-symlink mtimes, the worker-count seam, and the lenient numeric
// parser. POSIX-only cases are compiled out on Windows and skipped when
// running as root (chmod 000 does not deny root).
//
// The production .cpp files are #included directly so their
// internal-linkage (static / anonymous-namespace) functions are callable;
// the test binary is built from this one TU only (see CMakeLists
// WALKER_BUILD_TESTS) so no duplicate-symbol issues arise.

#include "../beacons.cpp"
#include "../cost_walk.hpp"
#include "../events.cpp"
#include "../search.cpp"

#include <cassert>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <string>

#ifndef _WIN32
#include <sys/stat.h>
#include <unistd.h>
#endif

namespace {

namespace fs = std::filesystem;

int failures = 0;

void expect(bool condition, const char *what) {
  if (!condition) {
    std::fprintf(stderr, "FAIL: %s\n", what);
    ++failures;
  } else {
    std::fprintf(stderr, "  ok: %s\n", what);
  }
}

fs::path make_temp_dir(const char *tag) {
  fs::path dir = fs::temp_directory_path() /
                 (std::string("walker-cpp-tests-") + tag + "-" +
                  std::to_string(::getpid()));
  fs::create_directories(dir);
  return dir;
}

void write_file(const fs::path &path, const std::string &body) {
  fs::create_directories(path.parent_path());
  std::ofstream out(path, std::ios::binary);
  out << body;
}

void test_effective_workers() {
  expect(walker::effective_workers(0) == 4, "effective_workers(0) -> 4");
  expect(walker::effective_workers(3) == 3, "effective_workers(3) -> 3");
  expect(walker::effective_workers(64) == 8, "effective_workers(64) -> 8");
}

void test_lenient_count() {
  simdjson::ondemand::parser parser;
  simdjson::padded_string doc(std::string_view(
      R"({"a":1.5,"b":"x","c":-2,"d":1e300,"e":7,"f":2e2,"g":[1]})"));
  simdjson::ondemand::document parsed;
  expect(parser.iterate(doc).get(parsed) == simdjson::SUCCESS,
         "lenient_count fixture parses");
  simdjson::ondemand::object object;
  assert(parsed.get_object().get(object) == simdjson::SUCCESS);
  uint64_t expected_values[] = {1, 0, 0, 0, 7, 200, 0};
  size_t index = 0;
  for (auto field : object) {
    uint64_t got = walker::lenient_count(field.value());
    expect(got == expected_values[index], "lenient_count field value");
    ++index;
  }
  expect(index == 7, "lenient_count visited all fields");
}

void test_parse_iso8601_separators() {
  expect(walker::parse_iso8601("2026-05-09T11:00:00Z").has_value(),
         "valid ISO accepted");
  // Every separator position must be validated (space-for-T was the
  // 2026-06-10 parity bug; the others guard positional digit parsing).
  const char *bad[] = {
      "2026x05-09T11:00:00Z", "2026-05x09T11:00:00Z",
      "2026-05-09 11:00:00Z", "2026-05-09T11x00:00Z",
      "2026-05-09T11:00x00Z",
  };
  for (const char *ts : bad) {
    expect(!walker::parse_iso8601(ts).has_value(), "bad separator rejected");
  }
}

void test_read_environment_variable() {
#ifndef _WIN32
  ::setenv("WALKER_TEST_ENV_VAR", "hello", 1);
  auto set_value = walker::read_environment_variable("WALKER_TEST_ENV_VAR");
  expect(set_value.has_value() && *set_value == "hello",
         "read_environment_variable set");
  ::unsetenv("WALKER_TEST_ENV_VAR");
#endif
  auto unset_value =
      walker::read_environment_variable("WALKER_TEST_ENV_VAR_UNSET");
  expect(!unset_value.has_value(), "read_environment_variable unset");
}

#ifndef _WIN32

bool running_as_root() { return ::geteuid() == 0; }

void test_entry_mtime_before_dangling_symlink() {
  fs::path dir = make_temp_dir("mtime");
  fs::path link = dir / "dangling.jsonl";
  std::error_code ec;
  fs::create_symlink(dir / "no-such-target.jsonl", link, ec);
  if (ec) {
    std::fprintf(stderr, "  skip: symlink creation failed\n");
    fs::remove_all(dir);
    return;
  }
  bool pruned = false;
  for (const auto &entry : fs::directory_iterator(dir)) {
    pruned = walker::entry_mtime_before(entry, 1e18);
  }
  // last_write_time fails on the dangling target -> err on inclusion.
  expect(!pruned, "dangling symlink mtime errs on inclusion");
  fs::remove_all(dir);
}

void test_discover_groups_unreadable_dirs() {
  if (running_as_root()) {
    std::fprintf(stderr, "  skip: running as root\n");
    return;
  }
  fs::path root = make_temp_dir("disc");
  fs::path slug = root / "slug-locked";
  write_file(slug / "sess.jsonl", "{}\n");
  ::chmod(slug.c_str(), 0000);
  auto groups = walker::discover_groups({root}, -1e308);
  expect(groups.empty(), "unreadable slug dir skipped by discover_groups");
  ::chmod(slug.c_str(), 0755);

  ::chmod(root.c_str(), 0000);
  auto root_groups = walker::discover_groups({root}, -1e308);
  expect(root_groups.empty(), "unreadable root skipped by discover_groups");
  ::chmod(root.c_str(), 0755);
  fs::remove_all(root);
}

void test_unreadable_transcripts() {
  if (running_as_root()) {
    std::fprintf(stderr, "  skip: running as root\n");
    return;
  }
  fs::path dir = make_temp_dir("locked");
  fs::path locked = dir / "locked.jsonl";
  write_file(locked,
             "{\"type\":\"assistant\",\"timestamp\":\"2026-01-01T00:00:00Z\","
             "\"message\":{\"role\":\"assistant\",\"content\":[]}}\n");
  ::chmod(locked.c_str(), 0000);

  // beacons-latest walker: callback must never fire. Generic lambdas keep
  // this robust to the callback parameter shapes.
  bool assistant_seen = false;
  walker::beacons::walk_assistant_entries(
      locked, [&](auto &&...) { assistant_seen = true; });
  expect(!assistant_seen, "beacons latest skips unreadable transcript");

  // beacons-history walker: neither callback fires.
  bool any_event = false;
  walker::beacons::walk_entries_for_history(
      locked, [&](auto &&...) { any_event = true; },
      [&](auto &&...) { any_event = true; });
  expect(!any_event, "beacons history skips unreadable transcript");

  // events walker: no records.
  auto records =
      walker::events::walk_group_events({locked}, "slug", "sess", -1e308);
  expect(records.empty(), "events walker skips unreadable transcript");

  // search scanner: no messages.
  auto messages = walker::search::scanFile(
      locked, walker::TranscriptFormat::ClaudeCode, false, false, nullptr);
  expect(messages.empty(), "search scanner skips unreadable transcript");

  // cost-mode walker: zero sums.
  auto cost_result = walker::walk_group({locked}, -1e308, -1e308);
  expect(cost_result.trailing == 0.0 && cost_result.window == 0.0,
         "cost walker skips unreadable transcript");

  ::chmod(locked.c_str(), 0644);
  fs::remove_all(dir);
}

#endif // !_WIN32

void test_nudge_to_whitespace_bounds() {
  std::string_view text = "alpha beta";
  expect(walker::search::nudgeToWhitespace(text, 0, -1, 5) == 0,
         "nudge at start returns start");
  expect(walker::search::nudgeToWhitespace(text, text.size(), 1, 5) ==
             text.size(),
         "nudge at end returns end");
}

void test_codex_scan_messages() {
  fs::path dir = make_temp_dir("codex-scan");
  fs::path transcript = dir / "rollout-scan.jsonl";
  write_file(transcript,
             "{\"type\":\"session_meta\",\"payload\":{\"id\":\"session-a\","
             "\"cwd\":\"/work/a\"}}\n"
             "{\"timestamp\":\"2026-05-10T11:01:40.000Z\",\"type\":"
             "\"event_msg\",\"payload\":{\"type\":\"user_message\","
             "\"message\":\"needle from user\"}}\n"
             "{\"timestamp\":\"2026-05-10T11:02:00.000Z\",\"type\":"
             "\"event_msg\",\"payload\":{\"type\":\"agent_message\","
             "\"message\":42}}\n"
             "{\"timestamp\":\"2026-05-10T11:03:00.000Z\",\"type\":"
             "\"response_item\",\"payload\":{\"type\":\"agent_message\","
             "\"message\":\"wrong envelope\"}}\n"
             "{\"timestamp\":\"2026-05-10T11:04:00.000Z\",\"type\":"
             "\"event_msg\",\"payload\":{\"type\":\"token_count\","
             "\"message\":\"wrong payload\"}}\n"
             "{\"timestamp\":\"2026-05-10T11:05:00.000Z\",\"type\":"
             "\"event_msg\",\"payload\":{\"type\":\"agent_message\","
             "\"message\":\"needle from agent\"}}\n");

  auto messages = walker::search::scanFile(
      transcript, walker::TranscriptFormat::Codex, false, false, nullptr);
  expect(messages.size() == 2,
         "Codex scanner accepts only string user and agent event messages");
  if (messages.size() == 2) {
    expect(messages[0].line_number == 2 && messages[0].role == "user" &&
               messages[0].text_default == "needle from user",
           "Codex scanner maps user message fields");
    expect(messages[1].line_number == 6 && messages[1].role == "assistant" &&
               messages[1].text_default == "needle from agent",
           "Codex scanner maps agent message fields");
  }
  fs::remove_all(dir);
}

void test_codex_discovery() {
  fs::path root = make_temp_dir("codex-discovery");
  fs::path good = root / "2026" / "05" / "10" / "rollout-good.jsonl";
  write_file(good, "{\"type\":\"session_meta\",\"payload\":{\"id\":"
                   "\"session-from-id\",\"cwd\":\"/work/exact\"}}\n");
  write_file(root / "2026" / "05" / "10" / "not-a-rollout.jsonl",
             "{\"type\":\"session_meta\",\"payload\":{\"id\":\"bad\","
             "\"cwd\":\"/work/exact\"}}\n");
  write_file(root / "2026" / "05" / "rollout-too-shallow.jsonl",
             "{\"type\":\"session_meta\",\"payload\":{\"id\":\"bad\","
             "\"cwd\":\"/work/exact\"}}\n");
  write_file(root / "2026" / "05" / "10" / "rollout-bad-meta.jsonl",
             "{\"type\":\"event_msg\",\"payload\":{\"id\":\"bad\","
             "\"cwd\":\"/work/exact\"}}\n");

  walker::TranscriptRoot tagged{root, walker::TranscriptFormat::Codex};
  std::string exact = "/work/exact";
  auto files = walker::search::discoverFiles(tagged, std::nullopt, &exact);
  expect(files.size() == 1,
         "Codex discovery requires YYYY/MM/DD rollout files and metadata");
  if (files.size() == 1) {
    expect(files[0].path == good && files[0].slug == exact &&
               files[0].session_id == "session-from-id" &&
               files[0].format == walker::TranscriptFormat::Codex,
           "Codex discovery reads id alias and cwd from first metadata line");
  }

  std::string prefix = "/work";
  auto prefix_files =
      walker::search::discoverFiles(tagged, std::nullopt, &prefix);
  expect(prefix_files.empty(), "Codex cwd filtering is exact");
  fs::remove_all(root);
}

} // namespace

int main() {
  test_effective_workers();
  test_lenient_count();
  test_parse_iso8601_separators();
  test_read_environment_variable();
  test_nudge_to_whitespace_bounds();
  test_codex_scan_messages();
  test_codex_discovery();
#ifndef _WIN32
  test_entry_mtime_before_dangling_symlink();
  test_discover_groups_unreadable_dirs();
  test_unreadable_transcripts();
#endif
  if (failures != 0) {
    std::fprintf(stderr, "%d test(s) FAILED\n", failures);
    return 1;
  }
  std::fprintf(stderr, "all cpp unit tests passed\n");
  return 0;
}
