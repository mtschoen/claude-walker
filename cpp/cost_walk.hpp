// Cost-mode group walker, shared between main.cpp and the native unit
// tests (tests/unit_tests.cpp cannot #include main.cpp because that TU owns
// main()). Header-only, matching the rest of the cpp impl.

#ifndef WALKER_COST_WALK_HPP
#define WALKER_COST_WALK_HPP

#include "common.hpp"
#include "pricing.hpp"

#include <algorithm>
#include <cctype>
#include <filesystem>
#include <string>
#include <string_view>
#include <unordered_set>
#include <vector>

#include <simdjson.h>

namespace walker {

struct GroupResult {
  double trailing = 0.0;
  double window = 0.0;
};

// Per-line walk via simdjson on-demand. We iterate the top-level object
// once and dispatch on key name — the on-demand API rewards forward-only
// access, so we extract every needed field in one pass without backing up.
//
// Why per-line iterate() and not iterate_many(): iterate_many bails the
// entire document_stream on the first malformed line and can't resume,
// so we'd lose every line after a bad one. Per-line iterate skips bad
// lines naturally. Zero per-line allocation: we hand simdjson a
// padded_string_view that points into the whole-file `data` buffer
// (padded_string::load guarantees SIMDJSON_PADDING bytes of tail zero
// padding), so the parser never sees a fresh heap allocation.
inline GroupResult walk_group(const std::vector<fs::path> &paths,
                              double period_cutoff, double win_start_unix) {
  double earliest = std::min(period_cutoff, win_start_unix);
  GroupResult result;
  std::unordered_set<std::string> seen_ids;

  simdjson::ondemand::parser parser;

  for (const auto &path : paths) {
    simdjson::padded_string data;
    if (simdjson::padded_string::load(path.string()).get(data) != simdjson::SUCCESS)
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

      // Skip empty / whitespace-only lines
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
      simdjson::padded_string_view view(line.data(), line.size(),
                                  buffer.size() - line_off +
                                      simdjson::SIMDJSON_PADDING);
      simdjson::ondemand::document doc;
      if (parser.iterate(view).get(doc) != simdjson::SUCCESS)
        continue;

      simdjson::ondemand::object root;
      if (doc.get_object().get(root) != simdjson::SUCCESS)
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
        if (root_field.unescaped_key().get(key) != simdjson::SUCCESS)
          continue;

        if (key == "timestamp") {
          if (root_field.value().get_string().get(timestamp_view) ==
              simdjson::SUCCESS) {
            has_timestamp = !timestamp_view.empty();
          }
        } else if (key == "message") {
          simdjson::ondemand::object msg_obj;
          if (root_field.value().get_object().get(msg_obj) != simdjson::SUCCESS)
            continue;
          message_seen = true;

          for (auto msg_field : msg_obj) {
            std::string_view msg_key;
            if (msg_field.unescaped_key().get(msg_key) != simdjson::SUCCESS)
              continue;

            if (msg_key == "role") {
              std::string_view role_view;
              if (msg_field.value().get_string().get(role_view) ==
                  simdjson::SUCCESS) {
                is_assistant = (role_view == "assistant");
              }
            } else if (msg_key == "id") {
              std::string_view id_view;
              if (msg_field.value().get_string().get(id_view) == simdjson::SUCCESS) {
                if (!id_view.empty()) {
                  message_id_view = id_view;
                  has_message_id = true;
                }
              }
            } else if (msg_key == "model") {
              std::string_view model_view;
              if (msg_field.value().get_string().get(model_view) ==
                  simdjson::SUCCESS) {
                model.assign(model_view.data(), model_view.size());
              }
            } else if (msg_key == "usage") {
              simdjson::ondemand::object usage_obj;
              if (msg_field.value().get_object().get(usage_obj) != simdjson::SUCCESS)
                continue;

              for (auto usage_field : usage_obj) {
                std::string_view usage_key;
                if (usage_field.unescaped_key().get(usage_key) != simdjson::SUCCESS)
                  continue;

                // server_tool_use is a nested object, not a scalar.
                // Descend for web_search_requests before the scalar
                // get_uint64 below (which would skip a non-uint value).
                if (usage_key == "server_tool_use") {
                  simdjson::ondemand::object stu_obj;
                  if (usage_field.value().get_object().get(stu_obj) !=
                      simdjson::SUCCESS)
                    continue;
                  for (auto stu_field : stu_obj) {
                    std::string_view stu_key;
                    if (stu_field.unescaped_key().get(stu_key) != simdjson::SUCCESS)
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

      if (!message_seen || !is_assistant)
        continue;

      if (has_message_id) {
        std::string mid(message_id_view);
        if (!seen_ids.insert(std::move(mid)).second)
          continue;
      }

      if (!has_timestamp)
        continue;
      auto ts_opt = parse_iso8601(timestamp_view);
      if (!ts_opt)
        continue;
      double ts = *ts_opt;
      if (ts < earliest)
        continue;

      double cost = cost_for(input_tokens, output_tokens, cache_read_tokens,
                             cache_write_tokens, web_search_requests, model,
                             timestamp_view);

      if (ts >= period_cutoff)
        result.trailing += cost;
      if (ts >= win_start_unix)
        result.window += cost;
    }
  }

  return result;
}

}  // namespace walker

#endif  // WALKER_COST_WALK_HPP
