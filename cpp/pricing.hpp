// Per-MTok pricing for assistant turns. Shared by cost mode (main.cpp) and
// the events subcommand (events.cpp) so the rate table and cost formula live
// in exactly one place — they must not drift between the two callers.
//
// Keep in lockstep with the canonical rates in
// ~/schoen-claude-status/statusline_lib.py and the other impls (rust/go/zig).

#ifndef WALKER_PRICING_HPP
#define WALKER_PRICING_HPP

#include <algorithm>
#include <cctype>
#include <cstdint>
#include <string_view>

#include <simdjson.h>

namespace walker {

// SPEC.md "Lenient per-field parsing": a token-count field accepts any JSON
// number, truncated toward zero; values outside [0, 2^64) and non-number
// tokens are treated as absent (0). simdjson scalar getters do not consume
// the value on a type mismatch, so the double fallback re-reads it safely.
inline uint64_t lenient_count(simdjson::ondemand::value value) {
    uint64_t whole = 0;
    if (value.get_uint64().get(whole) == simdjson::SUCCESS)
        return whole;
    double numeric = 0.0;
    if (value.get_double().get(numeric) == simdjson::SUCCESS &&
        numeric >= 0.0 && numeric < 18446744073709551616.0)
        return static_cast<uint64_t>(numeric);
    return 0;
}

struct Rates {
    double input;   // per MTok
    double output;  // per MTok
};

// Sonnet 5 introductory pricing runs through 2026-08-31 inclusive; standard
// pricing applies from 2026-09-01 onward. Selection is a lexicographic
// compare of the ISO-8601 timestamp's 10-char date prefix ("YYYY-MM-DD")
// against this string — monotonic for zero-padded dates, no timezone math.
inline constexpr std::string_view SONNET5_STANDARD_FROM = "2026-09-01";

// `date_prefix` is the assistant turn's ISO-8601 timestamp (the whole string
// is fine; only its leading "YYYY-MM-DD" is consulted). Only the sonnet-5
// family reads it; every other family ignores it.
inline Rates rates_for(std::string_view model, std::string_view date_prefix = {}) {
    // Case-insensitive substring scan without copying or lowercasing the
    // model string — tolower-comparing only the needle bytes is fine since
    // the family needles are ASCII.
    auto contains_ci = [&](std::string_view needle) {
        if (needle.size() > model.size()) return false;
        auto eq = [](unsigned char a, unsigned char b) {
            return std::tolower(a) == std::tolower(b);
        };
        return std::search(model.begin(), model.end(),
                           needle.begin(), needle.end(), eq) != model.end();
    };
    // fable/mythos first: the Fable family bills at $10/$50 and must win
    // before any substring that could collide.
    if (contains_ci("fable") || contains_ci("mythos")) return {10.0, 50.0};
    if (contains_ci("opus"))  return {5.0, 25.0};
    if (contains_ci("haiku")) return {1.0, 5.0};
    // sonnet-5 BEFORE the generic sonnet default. "sonnet-5" does NOT match
    // "claude-sonnet-4-5" (no such substring), so Sonnet 4.5 keeps the
    // generic sonnet rates below, date-independent.
    if (contains_ci("sonnet-5")) {
        bool standard = date_prefix.size() >= 10 &&
                        date_prefix.substr(0, 10) >= SONNET5_STANDARD_FROM;
        return standard ? Rates{3.0, 15.0} : Rates{2.0, 10.0};
    }
    return {3.0, 15.0};  // sonnet or unknown -> sonnet rates
}

// Flat charge per server-side web search request (billed $10 / 1,000), added
// on top of token cost. Matches SPEC.md and the Python reference.
inline constexpr double WEB_SEARCH_COST_USD = 0.01;

inline double cost_for(
    uint64_t input_tokens,
    uint64_t output_tokens,
    uint64_t cache_read_tokens,
    uint64_t cache_write_tokens,
    uint64_t web_search_requests,
    std::string_view model,
    std::string_view date_prefix = {})
{
    auto [input_rate, output_rate] = rates_for(model, date_prefix);
    double token_cost = (
        static_cast<double>(input_tokens) * input_rate
        + static_cast<double>(cache_read_tokens) * input_rate * 0.10
        + static_cast<double>(cache_write_tokens) * input_rate * 1.25
        + static_cast<double>(output_tokens) * output_rate
    ) / 1'000'000.0;
    return token_cost + static_cast<double>(web_search_requests) * WEB_SEARCH_COST_USD;
}

}  // namespace walker

#endif  // WALKER_PRICING_HPP
