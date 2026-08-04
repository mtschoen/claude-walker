// Search subcommand: substring / minimal-regex match across transcript content.
// Mirrors rust/src/search.rs and go/search.go. Zig port keeps the
// "regex-free stdlib" style of beacons.zig — the matcher here implements only
// the regex surface the conformance fixtures exercise (`foo\d+` plus the
// common escape classes), not full PCRE.

const std = @import("std");
const builtin = @import("builtin");
const Allocator = std.mem.Allocator;
const main = @import("main.zig");
const walker_roots = @import("walker_roots.zig");

const is_windows = main.is_windows;
const is_darwin = main.is_darwin;
const PATH_SEP = main.PATH_SEP;

// ─── Args ────────────────────────────────────────────────────────────────────

const Role = enum { user, assistant, both };
const Format = enum { pretty, jsonl };

const SearchArgs = struct {
    pattern: []const u8,
    regex: bool,
    case_sensitive: bool,
    role: Role,
    since: ?f64,
    until: ?f64,
    cwd: ?[]const u8,
    context: u32,
    limit: u32,
    count_only: bool,
    include_tool_blocks: bool,
    include_queue_ops: bool,
    format: Format,
    snippet_chars: u32,
    projects_root: ?[]const u8,
    extra_roots: [][]const u8,
    read_config: bool,
    now_unix: f64,
};

fn parseArgs(alloc: Allocator, raw: [][]const u8) !SearchArgs {
    var pattern: ?[]const u8 = null;
    var regex = false;
    var case_sensitive = false;
    var role: Role = .both;
    var since_raw: ?[]const u8 = null;
    var until_raw: ?[]const u8 = null;
    var cwd: ?[]const u8 = null;
    var any_cwd_explicit = false;
    var context: u32 = 1;
    var limit: u32 = 50;
    var count_only = false;
    var include_tool_blocks = false;
    var include_queue_ops = false;
    var format: Format = .pretty;
    var snippet_chars: u32 = 240;
    var projects_root: ?[]const u8 = null;
    var extra_roots: std.ArrayList([]const u8) = .empty;
    var read_config = true;
    var now_override: ?f64 = null;

    var i: usize = 0;
    while (i < raw.len) {
        const arg = raw[i];
        i += 1;
        if (std.mem.eql(u8, arg, "--regex")) {
            regex = true;
        } else if (std.mem.eql(u8, arg, "--case-sensitive")) {
            case_sensitive = true;
        } else if (std.mem.eql(u8, arg, "--role")) {
            const v = main.grab(raw, &i, "--role");
            if (std.mem.eql(u8, v, "user")) role = .user else if (std.mem.eql(u8, v, "assistant")) role = .assistant else if (std.mem.eql(u8, v, "both")) role = .both else {
                writeStderrFmt(alloc, "walker: search: --role: invalid value {s}; expected user|assistant|both\n", .{v});
                std.process.exit(2);
            }
        } else if (std.mem.eql(u8, arg, "--since")) {
            since_raw = main.grab(raw, &i, "--since");
        } else if (std.mem.eql(u8, arg, "--until")) {
            until_raw = main.grab(raw, &i, "--until");
        } else if (std.mem.eql(u8, arg, "--cwd")) {
            cwd = main.grab(raw, &i, "--cwd");
        } else if (std.mem.eql(u8, arg, "--any-cwd")) {
            any_cwd_explicit = true;
        } else if (std.mem.eql(u8, arg, "--context")) {
            context = std.fmt.parseInt(u32, main.grab(raw, &i, "--context"), 10) catch {
                writeStderrFmt(alloc, "walker: search: --context: invalid value\n", .{});
                std.process.exit(2);
            };
        } else if (std.mem.eql(u8, arg, "--limit")) {
            limit = std.fmt.parseInt(u32, main.grab(raw, &i, "--limit"), 10) catch {
                writeStderrFmt(alloc, "walker: search: --limit: invalid value\n", .{});
                std.process.exit(2);
            };
        } else if (std.mem.eql(u8, arg, "--count-only")) {
            count_only = true;
        } else if (std.mem.eql(u8, arg, "--include-tool-blocks")) {
            include_tool_blocks = true;
        } else if (std.mem.eql(u8, arg, "--include-queue-ops")) {
            include_queue_ops = true;
        } else if (std.mem.eql(u8, arg, "--format")) {
            const v = main.grab(raw, &i, "--format");
            if (std.mem.eql(u8, v, "pretty")) format = .pretty else if (std.mem.eql(u8, v, "jsonl")) format = .jsonl else {
                writeStderrFmt(alloc, "walker: search: --format: invalid value {s}; expected pretty|jsonl\n", .{v});
                std.process.exit(2);
            }
        } else if (std.mem.eql(u8, arg, "--snippet-chars")) {
            snippet_chars = std.fmt.parseInt(u32, main.grab(raw, &i, "--snippet-chars"), 10) catch {
                writeStderrFmt(alloc, "walker: search: --snippet-chars: invalid value\n", .{});
                std.process.exit(2);
            };
        } else if (std.mem.eql(u8, arg, "--projects-root")) {
            projects_root = main.grab(raw, &i, "--projects-root");
        } else if (std.mem.eql(u8, arg, "--extra-projects-root")) {
            try extra_roots.append(alloc, main.grab(raw, &i, "--extra-projects-root"));
        } else if (std.mem.eql(u8, arg, "--no-config")) {
            read_config = false;
        } else if (std.mem.eql(u8, arg, "--now")) {
            now_override = std.fmt.parseFloat(f64, main.grab(raw, &i, "--now")) catch {
                writeStderrFmt(alloc, "walker: search: --now: invalid value\n", .{});
                std.process.exit(2);
            };
        } else if (std.mem.startsWith(u8, arg, "--")) {
            writeStderrFmt(alloc, "walker: search: unknown flag: {s}\n", .{arg});
            std.process.exit(2);
        } else {
            if (pattern != null) {
                writeStderrFmt(alloc, "walker: search: unexpected positional argument: {s}\n", .{arg});
                std.process.exit(2);
            }
            pattern = arg;
        }
    }

    // Missing and empty positionals share one diagnostic block: the two
    // previously-identical blocks were ICF-merged by the compiler, which
    // attributed all hits to one of them and garbled line coverage.
    const pat = pattern orelse "";
    if (pat.len == 0) {
        writeStderrFmt(alloc, "walker: search: pattern must be non-empty\n", .{});
        std.process.exit(2);
    }
    if (cwd != null and any_cwd_explicit) {
        writeStderrFmt(alloc, "walker: search: --cwd and --any-cwd are mutually exclusive\n", .{});
        std.process.exit(2);
    }

    const now: f64 = now_override orelse main.nowUnix();

    var since: ?f64 = null;
    if (since_raw) |s| {
        since = parseTimeArg(s, now) catch {
            writeStderrFmt(alloc, "walker: search: bad time: --since={s}\n", .{s});
            std.process.exit(2);
        };
    }
    var until: ?f64 = null;
    if (until_raw) |s| {
        until = parseTimeArg(s, now) catch {
            writeStderrFmt(alloc, "walker: search: bad time: --until={s}\n", .{s});
            std.process.exit(2);
        };
    }

    return SearchArgs{
        .pattern = pat,
        .regex = regex,
        .case_sensitive = case_sensitive,
        .role = role,
        .since = since,
        .until = until,
        .cwd = cwd,
        .context = context,
        .limit = limit,
        .count_only = count_only,
        .include_tool_blocks = include_tool_blocks,
        .include_queue_ops = include_queue_ops,
        .format = format,
        .snippet_chars = snippet_chars,
        .projects_root = projects_root,
        .extra_roots = try extra_roots.toOwnedSlice(alloc),
        .read_config = read_config,
        .now_unix = now,
    };
}

fn parseTimeArg(s: []const u8, now: f64) !f64 {
    const trimmed = std.mem.trim(u8, s, " \t\r\n");
    if (trimmed.len == 0) return error.Empty;
    const last = trimmed[trimmed.len - 1];
    const mult: f64 = switch (last) {
        'd' => 86400.0,
        'h' => 3600.0,
        'm' => 60.0,
        's' => 1.0,
        else => 0.0,
    };
    if (mult > 0.0) {
        const head = trimmed[0 .. trimmed.len - 1];
        if (head.len > 0 and isNumeric(head)) {
            const n = try std.fmt.parseFloat(f64, head);
            return now - n * mult;
        }
    }
    return main.parseTs(trimmed);
}

fn isNumeric(s: []const u8) bool {
    for (s) |c| {
        if (c != '.' and (c < '0' or c > '9')) return false;
    }
    return true;
}

fn writeStderrFmt(alloc: Allocator, comptime fmt: []const u8, args: anytype) void {
    const buf = std.fmt.allocPrint(alloc, fmt, args) catch return;
    defer alloc.free(buf);
    main.writeStderr(buf);
}

// ─── Minimal regex / literal matcher ─────────────────────────────────────────
//
// Compile pattern to a flat sequence of Items (Atom + Quantifier). Match is a
// simple backtracking walker. Sufficient for the conformance fixtures
// (`needle`, `foo\d+`) plus common escape classes; does NOT implement groups,
// alternation, anchors, lookaround. That matches the documented scope —
// production callers needing PCRE features should use the Rust or C++ binary.

const Class = enum { digit, non_digit, word, non_word, space, non_space };

const CharClass = struct {
    negated: bool,
    // Bitset of 256 bits indicating which bytes are in the class.
    bits: [32]u8 = [_]u8{0} ** 32,

    fn set(self: *CharClass, b: u8) void {
        self.bits[b / 8] |= @as(u8, 1) << @intCast(b % 8);
    }
    fn contains(self: CharClass, b: u8) bool {
        const inSet = (self.bits[b / 8] >> @intCast(b % 8)) & 1 != 0;
        return if (self.negated) !inSet else inSet;
    }
};

const Atom = union(enum) {
    char: u8,
    dot,
    class_escape: Class,
    class_set: CharClass,
};

const Quant = enum { one, opt, star, plus };

const Item = struct {
    atom: Atom,
    quant: Quant,
};

const Program = struct {
    items: []Item,
    alloc: Allocator,

    fn deinit(self: *Program) void {
        self.alloc.free(self.items);
    }
};

const Pattern = struct {
    // The literal mode keeps the lowercased needle for case-insensitive scan.
    mode: enum { literal, regex },
    literal_needle: []const u8, // owned when case_insensitive (lowercased)
    case_sensitive: bool,
    program: ?Program,
    alloc: Allocator,

    fn deinit(self: *Pattern) void {
        if (self.mode == .literal and !self.case_sensitive) {
            self.alloc.free(self.literal_needle);
        }
        if (self.program) |*p| p.deinit();
    }
};

fn compilePattern(alloc: Allocator, raw_pattern: []const u8, regex_mode: bool, case_sensitive: bool) !Pattern {
    if (!regex_mode) {
        if (case_sensitive) {
            return Pattern{
                .mode = .literal,
                .literal_needle = raw_pattern,
                .case_sensitive = true,
                .program = null,
                .alloc = alloc,
            };
        }
        const lower = try alloc.alloc(u8, raw_pattern.len);
        for (raw_pattern, 0..) |c, k| lower[k] = std.ascii.toLower(c);
        return Pattern{
            .mode = .literal,
            .literal_needle = lower,
            .case_sensitive = false,
            .program = null,
            .alloc = alloc,
        };
    }
    const items = try compileRegex(alloc, raw_pattern, case_sensitive);
    return Pattern{
        .mode = .regex,
        .literal_needle = &.{},
        .case_sensitive = case_sensitive,
        .program = .{ .items = items, .alloc = alloc },
        .alloc = alloc,
    };
}

fn compileRegex(alloc: Allocator, src: []const u8, case_sensitive: bool) ![]Item {
    var items: std.ArrayList(Item) = .empty;
    errdefer items.deinit(alloc);

    var i: usize = 0;
    while (i < src.len) {
        var atom: Atom = undefined;
        const c = src[i];
        if (c == '\\') {
            if (i + 1 >= src.len) return error.BadEscape;
            const esc = src[i + 1];
            switch (esc) {
                'd' => atom = .{ .class_escape = .digit },
                'D' => atom = .{ .class_escape = .non_digit },
                'w' => atom = .{ .class_escape = .word },
                'W' => atom = .{ .class_escape = .non_word },
                's' => atom = .{ .class_escape = .space },
                'S' => atom = .{ .class_escape = .non_space },
                'n' => atom = .{ .char = '\n' },
                't' => atom = .{ .char = '\t' },
                'r' => atom = .{ .char = '\r' },
                else => atom = .{ .char = if (case_sensitive) esc else std.ascii.toLower(esc) },
            }
            i += 2;
        } else if (c == '.') {
            atom = .dot;
            i += 1;
        } else if (c == '(' or c == ')' or c == '|' or c == '{' or c == '}') {
            // Grouping/alternation/bounded-repetition not supported by this
            // hand-rolled engine. Per SPEC.md §"Search subcommand" the regex
            // surface is RE2; rather than silently treat these as literals
            // (parity bug — Rust/C++/Go all reject them), emit a parse error
            // that maps to exit 2 in run().
            return error.UnsupportedMetachar;
        } else if (c == '[') {
            var cls = CharClass{ .negated = false };
            i += 1;
            if (i < src.len and src[i] == '^') {
                cls.negated = true;
                i += 1;
            }
            while (i < src.len and src[i] != ']') {
                var lo: u8 = src[i];
                if (lo == '\\' and i + 1 < src.len) {
                    lo = switch (src[i + 1]) {
                        'n' => '\n',
                        't' => '\t',
                        'r' => '\r',
                        else => src[i + 1],
                    };
                    i += 2;
                } else {
                    i += 1;
                }
                if (i + 1 < src.len and src[i] == '-' and src[i + 1] != ']') {
                    var hi: u8 = src[i + 1];
                    i += 2;
                    if (hi == '\\' and i < src.len) {
                        hi = src[i];
                        i += 1;
                    }
                    var b = lo;
                    while (true) : (b += 1) {
                        if (case_sensitive) {
                            cls.set(b);
                        } else {
                            cls.set(std.ascii.toLower(b));
                            cls.set(std.ascii.toUpper(b));
                        }
                        if (b == hi) break;
                    }
                } else {
                    if (case_sensitive) {
                        cls.set(lo);
                    } else {
                        cls.set(std.ascii.toLower(lo));
                        cls.set(std.ascii.toUpper(lo));
                    }
                }
            }
            if (i >= src.len) return error.UnclosedClass;
            i += 1; // skip ']'
            atom = .{ .class_set = cls };
        } else {
            atom = .{ .char = if (case_sensitive) c else std.ascii.toLower(c) };
            i += 1;
        }

        var q: Quant = .one;
        if (i < src.len) {
            switch (src[i]) {
                '*' => {
                    q = .star;
                    i += 1;
                },
                '+' => {
                    q = .plus;
                    i += 1;
                },
                '?' => {
                    q = .opt;
                    i += 1;
                },
                else => {},
            }
        }
        try items.append(alloc, .{ .atom = atom, .quant = q });
    }
    return items.toOwnedSlice(alloc);
}

fn atomMatch(atom: Atom, b: u8, case_sensitive: bool) bool {
    return switch (atom) {
        .char => |cc| if (case_sensitive) (b == cc) else (std.ascii.toLower(b) == cc),
        .dot => b != '\n',
        .class_escape => |cls| switch (cls) {
            .digit => b >= '0' and b <= '9',
            .non_digit => !(b >= '0' and b <= '9'),
            .word => (b >= 'a' and b <= 'z') or (b >= 'A' and b <= 'Z') or (b >= '0' and b <= '9') or b == '_',
            .non_word => !((b >= 'a' and b <= 'z') or (b >= 'A' and b <= 'Z') or (b >= '0' and b <= '9') or b == '_'),
            .space => b == ' ' or b == '\t' or b == '\n' or b == '\r' or b == 0x0B or b == 0x0C,
            .non_space => !(b == ' ' or b == '\t' or b == '\n' or b == '\r' or b == 0x0B or b == 0x0C),
        },
        .class_set => |cls| cls.contains(b),
    };
}

// Try to match items[idx..] at text[pos..]. Returns end position on success,
// null on failure. Greedy quantifiers backtrack one byte at a time.
fn matchHere(items: []const Item, idx: usize, text: []const u8, pos: usize, case_sensitive: bool) ?usize {
    if (idx == items.len) return pos;
    const it = items[idx];
    switch (it.quant) {
        .one => {
            if (pos >= text.len) return null;
            if (!atomMatch(it.atom, text[pos], case_sensitive)) return null;
            return matchHere(items, idx + 1, text, pos + 1, case_sensitive);
        },
        .opt => {
            if (pos < text.len and atomMatch(it.atom, text[pos], case_sensitive)) {
                if (matchHere(items, idx + 1, text, pos + 1, case_sensitive)) |e| return e;
            }
            return matchHere(items, idx + 1, text, pos, case_sensitive);
        },
        .star => {
            var end = pos;
            while (end < text.len and atomMatch(it.atom, text[end], case_sensitive)) end += 1;
            while (true) {
                if (matchHere(items, idx + 1, text, end, case_sensitive)) |e| return e;
                if (end == pos) return null;
                end -= 1;
            }
        },
        .plus => {
            var end = pos;
            while (end < text.len and atomMatch(it.atom, text[end], case_sensitive)) end += 1;
            if (end == pos) return null;
            while (true) {
                if (matchHere(items, idx + 1, text, end, case_sensitive)) |e| return e;
                if (end == pos + 1) return null;
                end -= 1;
            }
        },
    }
}

// Find all non-overlapping matches of `pattern` in `text`. Empty matches are
// handled by advancing one byte to avoid infinite loops.
fn findAllMatches(alloc: Allocator, pattern: *const Pattern, text: []const u8) ![][2]usize {
    // alloc is arena-backed at every call site; an error path reclaims the
    // list at arena deinit, so no errdefer cleanup is needed.
    var out: std.ArrayList([2]usize) = .empty;

    if (pattern.mode == .literal) {
        const needle = pattern.literal_needle;
        if (needle.len == 0) return out.toOwnedSlice(alloc);
        var i: usize = 0;
        while (i + needle.len <= text.len) {
            const ok = blk: {
                if (pattern.case_sensitive) {
                    break :blk std.mem.eql(u8, text[i .. i + needle.len], needle);
                } else {
                    var k: usize = 0;
                    while (k < needle.len) : (k += 1) {
                        if (std.ascii.toLower(text[i + k]) != needle[k]) break :blk false;
                    }
                    break :blk true;
                }
            };
            if (ok) {
                try out.append(alloc, .{ i, i + needle.len });
                i += needle.len;
            } else {
                i += 1;
            }
        }
        return out.toOwnedSlice(alloc);
    }

    const prog = pattern.program orelse return out.toOwnedSlice(alloc);
    var pos: usize = 0;
    while (pos <= text.len) {
        if (matchHere(prog.items, 0, text, pos, pattern.case_sensitive)) |end_pos| {
            try out.append(alloc, .{ pos, end_pos });
            pos = if (end_pos == pos) end_pos + 1 else end_pos;
        } else {
            pos += 1;
        }
    }
    return out.toOwnedSlice(alloc);
}

// ─── JSON output buffer ──────────────────────────────────────────────────────

const Buf = struct {
    list: std.ArrayList(u8) = .empty,
    alloc: Allocator,

    fn appendStr(self: *Buf, s: []const u8) !void {
        try self.list.appendSlice(self.alloc, s);
    }
    fn appendByte(self: *Buf, b: u8) !void {
        try self.list.append(self.alloc, b);
    }
    fn appendFmt(self: *Buf, comptime fmt: []const u8, args: anytype) !void {
        var tmp: [128]u8 = undefined;
        if (std.fmt.bufPrint(&tmp, fmt, args)) |n| {
            try self.list.appendSlice(self.alloc, n);
        } else |_| {
            // Line exceeds the stack buffer (long pretty context/snippet
            // lines): format on the heap instead of silently dropping it.
            const heap = try std.fmt.allocPrint(self.alloc, fmt, args);
            defer self.alloc.free(heap);
            try self.list.appendSlice(self.alloc, heap);
        }
    }
    fn items(self: *const Buf) []const u8 {
        return self.list.items;
    }
};

fn writeJsonString(w: *Buf, s: []const u8) !void {
    try w.appendStr("\"");
    var i: usize = 0;
    while (i < s.len) : (i += 1) {
        const c = s[i];
        switch (c) {
            '"' => try w.appendStr("\\\""),
            '\\' => try w.appendStr("\\\\"),
            '\n' => try w.appendStr("\\n"),
            '\r' => try w.appendStr("\\r"),
            '\t' => try w.appendStr("\\t"),
            8 => try w.appendStr("\\b"),
            12 => try w.appendStr("\\f"),
            else => {
                if (c < 0x20) {
                    try w.appendFmt("\\u{x:0>4}", .{c});
                } else {
                    try w.appendByte(c);
                }
            },
        }
    }
    try w.appendStr("\"");
}

// Echo a JSON value via Scanner tokens into `buf`. Used for tool_use.input
// which the reference impls dump verbatim with include-tool-blocks. Number
// formatting mirrors the prior `writeJsonValue`: integers raw, floats
// reformatted through `{d}` so `1e3` becomes `1000` (matches the
// parseFromSlice round-trip).
fn dumpValueAsJson(scanner: *std.json.Scanner, alloc: Allocator) ![]u8 {
    var buf: std.ArrayList(u8) = .empty;
    try dumpValueInto(scanner, alloc, &buf);
    return buf.toOwnedSlice(alloc);
}

fn dumpValueInto(scanner: *std.json.Scanner, alloc: Allocator, buf: *std.ArrayList(u8)) !void {
    const peek = try scanner.peekNextTokenType();
    switch (peek) {
        .object_begin => {
            _ = try scanner.next();
            try buf.append(alloc, '{');
            var first = true;
            while (true) {
                const kt = try scanner.nextAlloc(alloc, .alloc_if_needed);
                const key: []const u8 = switch (kt) {
                    .object_end => break,
                    .string => |s| s,
                    .allocated_string => |s| s,
                    else => return error.UnexpectedToken,
                };
                if (!first) try buf.append(alloc, ',');
                first = false;
                try writeJsonStringInto(buf, alloc, key);
                try buf.append(alloc, ':');
                try dumpValueInto(scanner, alloc, buf);
            }
            try buf.append(alloc, '}');
        },
        .array_begin => {
            _ = try scanner.next();
            try buf.append(alloc, '[');
            var first = true;
            while (true) {
                const ep = try scanner.peekNextTokenType();
                if (ep == .array_end) {
                    _ = try scanner.next();
                    break;
                }
                if (!first) try buf.append(alloc, ',');
                first = false;
                try dumpValueInto(scanner, alloc, buf);
            }
            try buf.append(alloc, ']');
        },
        .string => {
            const t = try scanner.nextAlloc(alloc, .alloc_if_needed);
            const s: []const u8 = switch (t) {
                .string => |x| x,
                .allocated_string => |x| x,
                else => return error.UnexpectedToken,
            };
            try writeJsonStringInto(buf, alloc, s);
        },
        .number => {
            const t = try scanner.nextAlloc(alloc, .alloc_if_needed);
            const s: []const u8 = switch (t) {
                .number => |x| x,
                .allocated_number => |x| x,
                else => return error.UnexpectedToken,
            };
            if (std.fmt.parseInt(i64, s, 10)) |_| {
                try buf.appendSlice(alloc, s);
            } else |_| {
                if (std.fmt.parseFloat(f64, s)) |f| {
                    const fmt = try std.fmt.allocPrint(alloc, "{d}", .{f});
                    try buf.appendSlice(alloc, fmt);
                } else |_| {
                    try buf.appendSlice(alloc, s);
                }
            }
        },
        .true => {
            _ = try scanner.next();
            try buf.appendSlice(alloc, "true");
        },
        .false => {
            _ = try scanner.next();
            try buf.appendSlice(alloc, "false");
        },
        .null => {
            _ = try scanner.next();
            try buf.appendSlice(alloc, "null");
        },
        else => return error.UnexpectedToken,
    }
}

fn writeJsonStringInto(buf: *std.ArrayList(u8), alloc: Allocator, s: []const u8) !void {
    try buf.append(alloc, '"');
    var i: usize = 0;
    while (i < s.len) : (i += 1) {
        const c = s[i];
        switch (c) {
            '"' => try buf.appendSlice(alloc, "\\\""),
            '\\' => try buf.appendSlice(alloc, "\\\\"),
            '\n' => try buf.appendSlice(alloc, "\\n"),
            '\r' => try buf.appendSlice(alloc, "\\r"),
            '\t' => try buf.appendSlice(alloc, "\\t"),
            8 => try buf.appendSlice(alloc, "\\b"),
            12 => try buf.appendSlice(alloc, "\\f"),
            else => {
                if (c < 0x20) {
                    const escaped = try std.fmt.allocPrint(alloc, "\\u{x:0>4}", .{c});
                    try buf.appendSlice(alloc, escaped);
                } else {
                    try buf.append(alloc, c);
                }
            },
        }
    }
    try buf.append(alloc, '"');
}

// ─── Per-file scan ───────────────────────────────────────────────────────────

const ScanMessage = struct {
    line_number: u32,
    timestamp: ?f64,
    timestamp_str: []const u8, // owned
    role: []const u8, // owned
    text_default: []const u8, // owned
    text_with_tools: []const u8, // owned
    is_only_tool_blocks: bool,
};

fn scanFile(
    alloc: Allocator,
    path: []const u8,
    transcript_format: walker_roots.TranscriptFormat,
    include_queue_ops: bool,
) ![]ScanMessage {
    const data = main.readEntireFile(alloc, path) catch return alloc.alloc(ScanMessage, 0);
    // Don't free `data`: slices into it (timestamp_str, role) live on in the
    // returned ScanMessages until the arena reclaims them. With an arena
    // allocator `alloc.free` is a no-op anyway, but dropping the defer makes
    // the lifetime story explicit.

    var out: std.ArrayList(ScanMessage) = .empty;

    var idx: u32 = 0;
    var iter = std.mem.splitScalar(u8, data, '\n');
    while (iter.next()) |raw| {
        idx += 1;
        const line = std.mem.trim(u8, raw, " \t\r\n");
        if (line.len == 0) continue;

        // A malformed line is skipped, never the whole file (SPEC: malformed
        // JSONL lines are skipped) -- propagating the parse error here used
        // to abandon every line of the file, hits included.
        const message = if (transcript_format == .codex)
            parseCodexScanMessage(alloc, line, idx) catch null
        else
            parseScanMessage(alloc, line, idx, include_queue_ops) catch null;
        if (message) |m| {
            try out.append(alloc, m);
        }
    }
    return out.toOwnedSlice(alloc);
}

/// Parse the searchable subset of a Codex rollout event. Only event_msg
/// user_message and agent_message payloads with string messages are indexed.
fn parseCodexScanMessage(alloc: Allocator, line: []const u8, idx: u32) !?ScanMessage {
    var parsed = try std.json.parseFromSlice(std.json.Value, alloc, line, .{});
    defer parsed.deinit();
    const object = switch (parsed.value) {
        .object => |value| value,
        else => return null,
    };
    const entry_type = switch (object.get("type") orelse return null) {
        .string => |value| value,
        else => return null,
    };
    if (!std.mem.eql(u8, entry_type, "event_msg")) return null;
    const payload = switch (object.get("payload") orelse return null) {
        .object => |value| value,
        else => return null,
    };
    const event_type = switch (payload.get("type") orelse return null) {
        .string => |value| value,
        else => return null,
    };
    const role = if (std.mem.eql(u8, event_type, "user_message"))
        "user"
    else if (std.mem.eql(u8, event_type, "agent_message"))
        "assistant"
    else
        return null;
    const message = switch (payload.get("message") orelse return null) {
        .string => |value| value,
        else => return null,
    };
    const timestamp_string = if (object.get("timestamp")) |timestamp_value|
        switch (timestamp_value) {
            .string => |value| value,
            else => "",
        }
    else
        "";
    const text = try alloc.dupe(u8, message);
    const timestamp = try alloc.dupe(u8, timestamp_string);
    return .{
        .line_number = idx,
        .timestamp = if (timestamp.len == 0) null else main.parseTs(timestamp) catch null,
        .timestamp_str = timestamp,
        .role = role,
        .text_default = text,
        .text_with_tools = text,
        .is_only_tool_blocks = false,
    };
}

/// Single-pass Scanner walk that produces both `text_default` and
/// `text_with_tools` plus the `is_only_tool_blocks` flag. Returns null when
/// the entry is missing the structural fields a hit requires (role,
/// content). Key order is not assumed -- both role and content are
/// collected eagerly inside the message walk.
fn parseScanMessage(alloc: Allocator, line: []const u8, idx: u32, include_queue_ops: bool) !?ScanMessage {
    var scanner = std.json.Scanner.initCompleteInput(alloc, line);
    defer scanner.deinit();

    if (!(try main.enterObject(&scanner))) return null;

    var ts_str: []const u8 = "";
    var ts: ?f64 = null;
    var role: ?[]const u8 = null;
    // Root-level fields used by queue-operation entries (no `message` object):
    // `type == "queue-operation"` plus a bare-string `content`. Captured during
    // the same key walk; key order is not assumed. Mirrors search.rs.
    var entry_type: ?[]const u8 = null;
    var root_content: ?[]const u8 = null;
    var have_content = false;
    var content_was_array = false;
    var total_blocks: usize = 0;
    var tool_blocks: usize = 0;
    var text_default: std.ArrayList(u8) = .empty;
    var text_with_tools: std.ArrayList(u8) = .empty;
    var text_default_first = true;
    var text_with_tools_first = true;

    while (true) {
        const key = (try main.parseObjectKey(&scanner, alloc)) orelse break;
        if (std.mem.eql(u8, key, "timestamp")) {
            const v = try main.parseStringValue(&scanner, alloc);
            if (v) |s| {
                if (s.len > 0) {
                    ts_str = s;
                    ts = main.parseTs(s) catch null;
                }
            }
        } else if (std.mem.eql(u8, key, "type")) {
            entry_type = try main.parseStringValue(&scanner, alloc);
        } else if (std.mem.eql(u8, key, "content")) {
            // Root-level content (queue-ops). Stored owned the same way role/
            // ts_str are; null when not a bare string (skipped by parseStringValue).
            root_content = try main.parseStringValue(&scanner, alloc);
        } else if (std.mem.eql(u8, key, "message")) {
            if (!(try main.enterObject(&scanner))) continue;
            while (true) {
                const mkey = (try main.parseObjectKey(&scanner, alloc)) orelse break;
                if (std.mem.eql(u8, mkey, "role")) {
                    role = try main.parseStringValue(&scanner, alloc);
                } else if (std.mem.eql(u8, mkey, "content")) {
                    have_content = true;
                    try walkContentSearch(
                        &scanner,
                        alloc,
                        &text_default,
                        &text_with_tools,
                        &text_default_first,
                        &text_with_tools_first,
                        &total_blocks,
                        &tool_blocks,
                        &content_was_array,
                    );
                } else {
                    try scanner.skipValue();
                }
            }
        } else {
            try scanner.skipValue();
        }
    }

    // Queue-operation entries have no `message`/`role`: the text lives in the
    // root-level `content` string. Only surfaced when --include-queue-ops is set;
    // content-bearing enqueue/popAll appear, empty remove/dequeue are dropped.
    // They count as role:user. Mirrors search.rs's scan_file branch.
    if (entry_type) |et| {
        if (std.mem.eql(u8, et, "queue-operation")) {
            if (!include_queue_ops) return null;
            const qtext = root_content orelse return null;
            if (qtext.len == 0) return null;
            return ScanMessage{
                .line_number = idx,
                .timestamp = ts,
                .timestamp_str = ts_str,
                .role = "user",
                .text_default = qtext,
                .text_with_tools = qtext,
                .is_only_tool_blocks = false,
            };
        }
    }

    const r = role orelse return null;
    if (r.len == 0) return null;
    if (!have_content) return null;

    // Matches isOnlyToolBlocks: array, non-empty, every block is tool_use|tool_result.
    const only_tool = content_was_array and total_blocks > 0 and total_blocks == tool_blocks;

    return ScanMessage{
        .line_number = idx,
        .timestamp = ts,
        .timestamp_str = ts_str,
        .role = r,
        .text_default = try text_default.toOwnedSlice(alloc),
        .text_with_tools = try text_with_tools.toOwnedSlice(alloc),
        .is_only_tool_blocks = only_tool,
    };
}

/// One-pass walk of `message.content`. Builds `text_default` (text blocks
/// only) and `text_with_tools` (text + tool_use.input + tool_result.content)
/// simultaneously, while counting blocks for the `is_only_tool_blocks`
/// flag. Each content-derived snippet appended is separated by "\n" from
/// the previous append into the same buf, matching the legacy
/// `extractText` behavior (including separating inner tool_result text
/// items individually rather than as one joined block).
fn walkContentSearch(
    scanner: *std.json.Scanner,
    alloc: Allocator,
    text_default: *std.ArrayList(u8),
    text_with_tools: *std.ArrayList(u8),
    text_default_first: *bool,
    text_with_tools_first: *bool,
    total_blocks: *usize,
    tool_blocks: *usize,
    content_was_array: *bool,
) !void {
    const peek = try scanner.peekNextTokenType();
    switch (peek) {
        .string => {
            // Bare-string content: extractText returned it directly for both
            // default and with-tools modes. Match that.
            if (try main.parseStringValue(scanner, alloc)) |s| {
                try text_default.appendSlice(alloc, s);
                text_default_first.* = false;
                try text_with_tools.appendSlice(alloc, s);
                text_with_tools_first.* = false;
            }
        },
        .array_begin => {
            content_was_array.* = true;
            _ = try scanner.next();
            while (true) {
                const bp = try scanner.peekNextTokenType();
                if (bp == .array_end) {
                    _ = try scanner.next();
                    break;
                }
                if (bp != .object_begin) {
                    try scanner.skipValue();
                    continue;
                }
                _ = try scanner.next();
                total_blocks.* += 1;

                var block_type: ?[]const u8 = null;
                var block_text: ?[]const u8 = null;
                var tool_use_input_serialized: ?[]u8 = null;
                var tool_result_string: ?[]const u8 = null;
                var tool_result_array_lines: std.ArrayList([]const u8) = .empty;

                while (true) {
                    const bkey = (try main.parseObjectKey(scanner, alloc)) orelse break;
                    if (std.mem.eql(u8, bkey, "type")) {
                        block_type = try main.parseStringValue(scanner, alloc);
                    } else if (std.mem.eql(u8, bkey, "text")) {
                        block_text = try main.parseStringValue(scanner, alloc);
                    } else if (std.mem.eql(u8, bkey, "input")) {
                        tool_use_input_serialized = try dumpValueAsJson(scanner, alloc);
                    } else if (std.mem.eql(u8, bkey, "content")) {
                        const cp = try scanner.peekNextTokenType();
                        if (cp == .string) {
                            tool_result_string = try main.parseStringValue(scanner, alloc);
                        } else if (cp == .array_begin) {
                            _ = try scanner.next();
                            while (true) {
                                const ip = try scanner.peekNextTokenType();
                                if (ip == .array_end) {
                                    _ = try scanner.next();
                                    break;
                                }
                                if (ip != .object_begin) {
                                    try scanner.skipValue();
                                    continue;
                                }
                                _ = try scanner.next();
                                var inner_type: ?[]const u8 = null;
                                var inner_text: ?[]const u8 = null;
                                while (true) {
                                    const ikey = (try main.parseObjectKey(scanner, alloc)) orelse break;
                                    if (std.mem.eql(u8, ikey, "type")) {
                                        inner_type = try main.parseStringValue(scanner, alloc);
                                    } else if (std.mem.eql(u8, ikey, "text")) {
                                        inner_text = try main.parseStringValue(scanner, alloc);
                                    } else {
                                        try scanner.skipValue();
                                    }
                                }
                                if (inner_type) |it| {
                                    if (std.mem.eql(u8, it, "text")) {
                                        if (inner_text) |t| {
                                            try tool_result_array_lines.append(alloc, t);
                                        }
                                    }
                                }
                            }
                        } else {
                            try scanner.skipValue();
                        }
                    } else {
                        try scanner.skipValue();
                    }
                }

                if (block_type) |bt| {
                    if (std.mem.eql(u8, bt, "text")) {
                        if (block_text) |txt| {
                            if (!text_default_first.*) try text_default.append(alloc, '\n');
                            text_default_first.* = false;
                            try text_default.appendSlice(alloc, txt);
                            if (!text_with_tools_first.*) try text_with_tools.append(alloc, '\n');
                            text_with_tools_first.* = false;
                            try text_with_tools.appendSlice(alloc, txt);
                        }
                    } else if (std.mem.eql(u8, bt, "tool_use")) {
                        tool_blocks.* += 1;
                        if (tool_use_input_serialized) |inp| {
                            if (!text_with_tools_first.*) try text_with_tools.append(alloc, '\n');
                            text_with_tools_first.* = false;
                            try text_with_tools.appendSlice(alloc, inp);
                        }
                    } else if (std.mem.eql(u8, bt, "tool_result")) {
                        tool_blocks.* += 1;
                        if (tool_result_string) |s| {
                            if (!text_with_tools_first.*) try text_with_tools.append(alloc, '\n');
                            text_with_tools_first.* = false;
                            try text_with_tools.appendSlice(alloc, s);
                        } else if (tool_result_array_lines.items.len > 0) {
                            for (tool_result_array_lines.items) |t| {
                                if (!text_with_tools_first.*) try text_with_tools.append(alloc, '\n');
                                text_with_tools_first.* = false;
                                try text_with_tools.appendSlice(alloc, t);
                            }
                        }
                    }
                }
            }
        },
        else => try scanner.skipValue(),
    }
}

// ─── Discovery ───────────────────────────────────────────────────────────────

const DiscoveredFile = struct {
    path: []const u8,
    slug: []const u8,
    session_id: []const u8,
    host_root: []const u8,
    format: walker_roots.TranscriptFormat = .claude_code,
};

fn discoverFiles(
    alloc: Allocator,
    roots: []const walker_roots.TranscriptRoot,
    since: ?f64,
    cwd_filter: ?[]const u8,
) ![]DiscoveredFile {
    // alloc is arena-backed; an error path reclaims the list at arena
    // deinit, so no errdefer cleanup is needed.
    var out: std.ArrayList(DiscoveredFile) = .empty;

    for (roots) |root| {
        if (root.format == .codex) {
            try discoverCodex(alloc, &out, root.path, since, cwd_filter);
            continue;
        }
        if (is_windows) {
            try discoverWindows(alloc, &out, root.path, since, cwd_filter);
        } else if (is_darwin) {
            try discoverDarwin(alloc, &out, root.path, since, cwd_filter);
        } else {
            try discoverLinux(alloc, &out, root.path, since, cwd_filter);
        }
    }
    return out.toOwnedSlice(alloc);
}

const DateDirectory = struct {
    name: []const u8,
    path: []const u8,
};

fn isNumericComponent(name: []const u8, expected_length: usize) bool {
    if (name.len != expected_length) return false;
    for (name) |character| {
        if (character < '0' or character > '9') return false;
    }
    return true;
}

fn discoverCodex(
    alloc: Allocator,
    out: *std.ArrayList(DiscoveredFile),
    root: []const u8,
    since: ?f64,
    cwd_filter: ?[]const u8,
) !void {
    const years = try listCodexDirectories(alloc, root);
    for (years) |year| {
        if (!isNumericComponent(year.name, 4)) continue;
        const months = try listCodexDirectories(alloc, year.path);
        for (months) |month| {
            if (!isNumericComponent(month.name, 2)) continue;
            const days = try listCodexDirectories(alloc, month.path);
            for (days) |day| {
                if (!isNumericComponent(day.name, 2)) continue;
                const rollouts = try listCodexRollouts(alloc, day.path, since);
                for (rollouts) |path| {
                    try appendCodexFile(alloc, out, root, path, cwd_filter);
                }
            }
        }
    }
}

fn appendCodexFile(
    alloc: Allocator,
    out: *std.ArrayList(DiscoveredFile),
    root: []const u8,
    path: []const u8,
    cwd_filter: ?[]const u8,
) !void {
    const data = main.readFirstLine(alloc, path) catch return;
    const first_line = std.mem.trim(u8, data, " \t\r\n");
    if (first_line.len == 0) return;

    var parsed = std.json.parseFromSlice(std.json.Value, alloc, first_line, .{}) catch return;
    defer parsed.deinit();
    const object = switch (parsed.value) {
        .object => |value| value,
        else => return,
    };
    const entry_type = switch (object.get("type") orelse return) {
        .string => |value| value,
        else => return,
    };
    if (!std.mem.eql(u8, entry_type, "session_meta")) return;
    const payload = switch (object.get("payload") orelse return) {
        .object => |value| value,
        else => return,
    };
    const cwd = switch (payload.get("cwd") orelse return) {
        .string => |value| value,
        else => return,
    };
    if (cwd_filter) |wanted| {
        if (!std.mem.eql(u8, wanted, cwd)) return;
    }
    const session_value = payload.get("session_id") orelse payload.get("id") orelse return;
    const session_id = switch (session_value) {
        .string => |value| value,
        else => return,
    };
    try out.append(alloc, .{
        .path = path,
        .slug = try alloc.dupe(u8, cwd),
        .session_id = try alloc.dupe(u8, session_id),
        .host_root = root,
        .format = .codex,
    });
}

fn listCodexDirectories(alloc: Allocator, path: []const u8) ![]DateDirectory {
    if (is_windows) return listCodexDirectoriesWindows(alloc, path);
    if (is_darwin) return listCodexDirectoriesDarwin(alloc, path);
    return listCodexDirectoriesLinux(alloc, path);
}

fn listCodexRollouts(alloc: Allocator, path: []const u8, since: ?f64) ![][]const u8 {
    if (is_windows) return listCodexRolloutsWindows(alloc, path, since);
    if (is_darwin) return listCodexRolloutsDarwin(alloc, path, since);
    return listCodexRolloutsLinux(alloc, path, since);
}

fn isRolloutName(name: []const u8) bool {
    return std.mem.startsWith(u8, name, "rollout-") and std.mem.endsWith(u8, name, ".jsonl");
}

fn listCodexDirectoriesWindows(alloc: Allocator, path: []const u8) ![]DateDirectory {
    const platform = main.platform;
    const pattern = try std.fmt.allocPrint(alloc, "{s}\\*", .{path});
    defer alloc.free(pattern);
    const wide_pattern = try std.unicode.utf8ToUtf16LeAllocZ(alloc, pattern);
    defer alloc.free(wide_pattern);

    var result: std.ArrayList(DateDirectory) = .empty;
    var find_data: platform.WIN32_FIND_DATAW = undefined;
    const handle = platform.FindFirstFileW(wide_pattern.ptr, &find_data) orelse return &.{};
    if (handle == platform.INVALID_HANDLE_VALUE) return &.{};
    defer _ = platform.FindClose(handle);

    while (true) {
        if ((find_data.dwFileAttributes & platform.FILE_ATTRIBUTE_DIRECTORY) != 0) {
            const wide_name = std.mem.span(@as([*:0]const u16, @ptrCast(&find_data.cFileName)));
            const is_dot = wide_name.len == 1 and wide_name[0] == '.';
            const is_dot_dot = wide_name.len == 2 and wide_name[0] == '.' and wide_name[1] == '.';
            if (wide_name.len > 0 and !is_dot and !is_dot_dot) {
                const name = try std.unicode.utf16LeToUtf8Alloc(alloc, wide_name);
                try result.append(alloc, .{
                    .name = name,
                    .path = try std.fmt.allocPrint(alloc, "{s}\\{s}", .{ path, name }),
                });
            }
        }
        if (platform.FindNextFileW(handle, &find_data) == 0) break;
    }
    return result.toOwnedSlice(alloc);
}

fn listCodexRolloutsWindows(alloc: Allocator, path: []const u8, since: ?f64) ![][]const u8 {
    const platform = main.platform;
    const pattern = try std.fmt.allocPrint(alloc, "{s}\\*", .{path});
    defer alloc.free(pattern);
    const wide_pattern = try std.unicode.utf8ToUtf16LeAllocZ(alloc, pattern);
    defer alloc.free(wide_pattern);

    var result: std.ArrayList([]const u8) = .empty;
    var find_data: platform.WIN32_FIND_DATAW = undefined;
    const handle = platform.FindFirstFileW(wide_pattern.ptr, &find_data) orelse return &.{};
    if (handle == platform.INVALID_HANDLE_VALUE) return &.{};
    defer _ = platform.FindClose(handle);

    while (true) {
        if ((find_data.dwFileAttributes & platform.FILE_ATTRIBUTE_DIRECTORY) == 0) {
            const wide_name = std.mem.span(@as([*:0]const u16, @ptrCast(&find_data.cFileName)));
            const name = try std.unicode.utf16LeToUtf8Alloc(alloc, wide_name);
            defer alloc.free(name);
            const mtime_matches = if (since) |cutoff| find_data.ftLastWriteTime.toUnix() >= cutoff else true;
            if (isRolloutName(name) and mtime_matches) {
                try result.append(alloc, try std.fmt.allocPrint(alloc, "{s}\\{s}", .{ path, name }));
            }
        }
        if (platform.FindNextFileW(handle, &find_data) == 0) break;
    }
    return result.toOwnedSlice(alloc);
}

fn listCodexDirectoriesDarwin(alloc: Allocator, path: []const u8) ![]DateDirectory {
    const zero_path = try alloc.dupeZ(u8, path);
    defer alloc.free(zero_path);
    const directory = std.c.opendir(zero_path) orelse return &.{};
    defer _ = std.c.closedir(directory);

    var result: std.ArrayList(DateDirectory) = .empty;
    while (std.c.readdir(directory)) |entry| {
        if (entry.type != std.c.DT.DIR) continue;
        const name_pointer: [*:0]const u8 = @ptrCast(&entry.name);
        const name = std.mem.span(name_pointer);
        if (name.len == 0) continue;
        if (name[0] == '.' and (name.len == 1 or (name.len == 2 and name[1] == '.'))) continue;
        const owned_name = try alloc.dupe(u8, name);
        try result.append(alloc, .{
            .name = owned_name,
            .path = try std.fmt.allocPrint(alloc, "{s}/{s}", .{ path, owned_name }),
        });
    }
    return result.toOwnedSlice(alloc);
}

fn listCodexRolloutsDarwin(alloc: Allocator, path: []const u8, since: ?f64) ![][]const u8 {
    const zero_path = try alloc.dupeZ(u8, path);
    defer alloc.free(zero_path);
    const directory = std.c.opendir(zero_path) orelse return &.{};
    defer _ = std.c.closedir(directory);

    var result: std.ArrayList([]const u8) = .empty;
    while (std.c.readdir(directory)) |entry| {
        if (entry.type != std.c.DT.REG and entry.type != std.c.DT.UNKNOWN) continue;
        const name_pointer: [*:0]const u8 = @ptrCast(&entry.name);
        const name = std.mem.span(name_pointer);
        if (!isRolloutName(name)) continue;
        const rollout_path = try std.fmt.allocPrint(alloc, "{s}/{s}", .{ path, name });
        if (since) |cutoff| {
            if (!mtimeOkDarwin(alloc, rollout_path, cutoff)) {
                alloc.free(rollout_path);
                continue;
            }
        }
        try result.append(alloc, rollout_path);
    }
    return result.toOwnedSlice(alloc);
}

fn listCodexDirectoriesLinux(alloc: Allocator, path: []const u8) ![]DateDirectory {
    const linux = main.platform.linux;
    const zero_path = try alloc.dupeZ(u8, path);
    defer alloc.free(zero_path);
    const descriptor: i32 = @bitCast(@as(u32, @truncate(linux.openat(linux.AT.FDCWD, zero_path, .{ .DIRECTORY = true }, 0))));
    if (descriptor < 0) return &.{};
    defer _ = linux.close(descriptor);

    var result: std.ArrayList(DateDirectory) = .empty;
    var directory_buffer: [8192]u8 = undefined;
    while (true) {
        const count = linux.getdents64(descriptor, &directory_buffer, directory_buffer.len);
        const signed_count: isize = @bitCast(count);
        if (signed_count <= 0) break;
        var offset: usize = 0;
        while (offset < count) {
            const entry = @as(*align(1) const linux.dirent64, @ptrCast(&directory_buffer[offset]));
            offset += entry.reclen;
            if (entry.type != linux.DT.DIR) continue;
            const name_pointer: [*:0]const u8 = @ptrCast(&entry.name);
            const name = std.mem.span(name_pointer);
            if (name.len == 0) continue;
            if (name[0] == '.' and (name.len == 1 or (name.len == 2 and name[1] == '.'))) continue;
            const owned_name = try alloc.dupe(u8, name);
            try result.append(alloc, .{
                .name = owned_name,
                .path = try std.fmt.allocPrint(alloc, "{s}/{s}", .{ path, owned_name }),
            });
        }
    }
    return result.toOwnedSlice(alloc);
}

fn listCodexRolloutsLinux(alloc: Allocator, path: []const u8, since: ?f64) ![][]const u8 {
    const linux = main.platform.linux;
    const zero_path = try alloc.dupeZ(u8, path);
    defer alloc.free(zero_path);
    const descriptor: i32 = @bitCast(@as(u32, @truncate(linux.openat(linux.AT.FDCWD, zero_path, .{ .DIRECTORY = true }, 0))));
    if (descriptor < 0) return &.{};
    defer _ = linux.close(descriptor);

    var result: std.ArrayList([]const u8) = .empty;
    var directory_buffer: [8192]u8 = undefined;
    while (true) {
        const count = linux.getdents64(descriptor, &directory_buffer, directory_buffer.len);
        const signed_count: isize = @bitCast(count);
        if (signed_count <= 0) break;
        var offset: usize = 0;
        while (offset < count) {
            const entry = @as(*align(1) const linux.dirent64, @ptrCast(&directory_buffer[offset]));
            offset += entry.reclen;
            if (entry.type != linux.DT.REG and entry.type != linux.DT.UNKNOWN) continue;
            const name_pointer: [*:0]const u8 = @ptrCast(&entry.name);
            const name = std.mem.span(name_pointer);
            if (!isRolloutName(name)) continue;
            const rollout_path = try std.fmt.allocPrint(alloc, "{s}/{s}", .{ path, name });
            if (since) |cutoff| {
                if (!mtimeOkLinux(alloc, rollout_path, cutoff)) {
                    alloc.free(rollout_path);
                    continue;
                }
            }
            try result.append(alloc, rollout_path);
        }
    }
    return result.toOwnedSlice(alloc);
}

fn discoverWindows(alloc: Allocator, out: *std.ArrayList(DiscoveredFile), root: []const u8, since: ?f64, cwd_filter: ?[]const u8) !void {
    const platform = main.platform;
    const slug_pattern = try std.fmt.allocPrint(alloc, "{s}\\*", .{root});
    defer alloc.free(slug_pattern);
    const wpat = try std.unicode.utf8ToUtf16LeAllocZ(alloc, slug_pattern);
    defer alloc.free(wpat);

    var fd: platform.WIN32_FIND_DATAW = undefined;
    const h = platform.FindFirstFileW(wpat.ptr, &fd) orelse return;
    if (h == platform.INVALID_HANDLE_VALUE) return;
    defer _ = platform.FindClose(h);

    while (true) {
        const is_dir = (fd.dwFileAttributes & platform.FILE_ATTRIBUTE_DIRECTORY) != 0;
        if (is_dir) {
            const name_w = std.mem.span(@as([*:0]const u16, @ptrCast(&fd.cFileName)));
            const skip = name_w.len == 0 or
                (name_w.len == 1 and name_w[0] == '.') or
                (name_w.len == 2 and name_w[0] == '.' and name_w[1] == '.');
            if (!skip) {
                const slug = try std.unicode.utf16LeToUtf8Alloc(alloc, name_w);
                const passes = if (cwd_filter) |f| std.mem.eql(u8, slug, f) else true;
                if (passes) {
                    try scanSlugJsonlWindows(alloc, out, root, slug, since);
                }
            }
        }
        if (platform.FindNextFileW(h, &fd) == 0) break;
    }
}

fn scanSlugJsonlWindows(alloc: Allocator, out: *std.ArrayList(DiscoveredFile), root: []const u8, slug: []const u8, since: ?f64) !void {
    const platform = main.platform;
    const slug_dir = try std.fmt.allocPrint(alloc, "{s}\\{s}", .{ root, slug });
    // `*` (not `*.jsonl`): one pass classifies parent transcripts AND session
    // dirs, whose subagents/ are probed per SPEC "Discovery" under search.
    const pat = try std.fmt.allocPrint(alloc, "{s}\\*", .{slug_dir});
    defer alloc.free(pat);

    const wpat = try std.unicode.utf8ToUtf16LeAllocZ(alloc, pat);
    defer alloc.free(wpat);

    var fd: platform.WIN32_FIND_DATAW = undefined;
    const h = platform.FindFirstFileW(wpat.ptr, &fd) orelse {
        alloc.free(slug_dir);
        return;
    };
    if (h == platform.INVALID_HANDLE_VALUE) {
        alloc.free(slug_dir);
        return;
    }
    defer _ = platform.FindClose(h);

    while (true) {
        const is_dir = (fd.dwFileAttributes & platform.FILE_ATTRIBUTE_DIRECTORY) != 0;
        const name_w = std.mem.span(@as([*:0]const u16, @ptrCast(&fd.cFileName)));
        if (is_dir) {
            const skip = name_w.len == 0 or
                (name_w.len == 1 and name_w[0] == '.') or
                (name_w.len == 2 and name_w[0] == '.' and name_w[1] == '.');
            if (!skip) {
                const sess = try std.unicode.utf16LeToUtf8Alloc(alloc, name_w);
                try scanSubagentsWindows(alloc, out, slug_dir, slug, sess, root, since);
            }
        } else {
            const name = try std.unicode.utf16LeToUtf8Alloc(alloc, name_w);
            if (std.mem.endsWith(u8, name, ".jsonl")) {
                if (since) |cutoff| {
                    const mtime = fd.ftLastWriteTime.toUnix();
                    if (mtime < cutoff) {
                        alloc.free(name);
                        if (platform.FindNextFileW(h, &fd) == 0) break;
                        continue;
                    }
                }
                const sid = name[0 .. name.len - 6];
                const sid_owned = try alloc.dupe(u8, sid);
                const path = try std.fmt.allocPrint(alloc, "{s}\\{s}", .{ slug_dir, name });
                try out.append(alloc, .{
                    .path = path,
                    .slug = slug,
                    .session_id = sid_owned,
                    .host_root = root,
                });
            }
            alloc.free(name);
        }
        if (platform.FindNextFileW(h, &fd) == 0) break;
    }
}

/// Subagents: <slug>/<session>/subagents/agent-*.jsonl. session_id is the
/// enclosing session dir name (the parent session), so subagent hits group
/// with the parent in sessions_matched.
fn scanSubagentsWindows(alloc: Allocator, out: *std.ArrayList(DiscoveredFile), slug_dir: []const u8, slug: []const u8, sess: []const u8, root: []const u8, since: ?f64) !void {
    const platform = main.platform;
    const pat = try std.fmt.allocPrint(alloc, "{s}\\{s}\\subagents\\agent-*.jsonl", .{ slug_dir, sess });
    defer alloc.free(pat);
    const wpat = try std.unicode.utf8ToUtf16LeAllocZ(alloc, pat);
    defer alloc.free(wpat);

    var fd: platform.WIN32_FIND_DATAW = undefined;
    const h = platform.FindFirstFileW(wpat.ptr, &fd) orelse return;
    if (h == platform.INVALID_HANDLE_VALUE) return;
    defer _ = platform.FindClose(h);

    while (true) {
        const is_dir = (fd.dwFileAttributes & platform.FILE_ATTRIBUTE_DIRECTORY) != 0;
        if (!is_dir) {
            const keep = if (since) |cutoff| fd.ftLastWriteTime.toUnix() >= cutoff else true;
            if (keep) {
                const name_w = std.mem.span(@as([*:0]const u16, @ptrCast(&fd.cFileName)));
                const name = try std.unicode.utf16LeToUtf8Alloc(alloc, name_w);
                defer alloc.free(name);
                const path = try std.fmt.allocPrint(alloc, "{s}\\{s}\\subagents\\{s}", .{ slug_dir, sess, name });
                try out.append(alloc, .{
                    .path = path,
                    .slug = slug,
                    .session_id = try alloc.dupe(u8, sess),
                    .host_root = root,
                });
            }
        }
        if (platform.FindNextFileW(h, &fd) == 0) break;
    }
}

fn discoverDarwin(alloc: Allocator, out: *std.ArrayList(DiscoveredFile), root: []const u8, since: ?f64, cwd_filter: ?[]const u8) !void {
    const root_z = try alloc.dupeZ(u8, root);
    defer alloc.free(root_z);
    const root_dir = std.c.opendir(root_z) orelse return;
    defer _ = std.c.closedir(root_dir);

    while (std.c.readdir(root_dir)) |ent| {
        if (ent.type != std.c.DT.DIR) continue;
        const name_ptr: [*:0]const u8 = @ptrCast(&ent.name);
        const slug = std.mem.span(name_ptr);
        if (slug.len == 0) continue;
        if (slug[0] == '.' and (slug.len == 1 or (slug.len == 2 and slug[1] == '.'))) continue;

        if (cwd_filter) |f| {
            if (!std.mem.eql(u8, slug, f)) continue;
        }
        const slug_owned = try alloc.dupe(u8, slug);
        try scanSlugJsonlDarwin(alloc, out, root, slug_owned, since);
    }
}

fn scanSlugJsonlDarwin(alloc: Allocator, out: *std.ArrayList(DiscoveredFile), root: []const u8, slug: []const u8, since: ?f64) !void {
    const slug_dir = try std.fmt.allocPrint(alloc, "{s}/{s}", .{ root, slug });
    const slug_z = try alloc.dupeZ(u8, slug_dir);
    defer alloc.free(slug_z);
    const dir = std.c.opendir(slug_z) orelse {
        alloc.free(slug_dir);
        return;
    };
    defer _ = std.c.closedir(dir);

    while (std.c.readdir(dir)) |ent| {
        const name_ptr: [*:0]const u8 = @ptrCast(&ent.name);
        const name = std.mem.span(name_ptr);
        if (ent.type == std.c.DT.DIR) {
            if (name.len == 0) continue;
            if (name[0] == '.' and (name.len == 1 or (name.len == 2 and name[1] == '.'))) continue;
            try scanSubagentsDarwin(alloc, out, slug_dir, slug, name, root, since);
            continue;
        }
        if (ent.type != std.c.DT.REG and ent.type != std.c.DT.UNKNOWN) continue;
        if (!std.mem.endsWith(u8, name, ".jsonl")) continue;

        const path = try std.fmt.allocPrint(alloc, "{s}/{s}", .{ slug_dir, name });
        if (since) |cutoff| {
            if (!mtimeOkDarwin(alloc, path, cutoff)) {
                alloc.free(path);
                continue;
            }
        }
        const sid = try alloc.dupe(u8, name[0 .. name.len - 6]);
        try out.append(alloc, .{
            .path = path,
            .slug = slug,
            .session_id = sid,
            .host_root = root,
        });
    }
}

/// Subagents: <slug>/<session>/subagents/agent-*.jsonl. session_id is the
/// enclosing session dir name (the parent session), so subagent hits group
/// with the parent in sessions_matched.
fn scanSubagentsDarwin(alloc: Allocator, out: *std.ArrayList(DiscoveredFile), slug_dir: []const u8, slug: []const u8, sess: []const u8, root: []const u8, since: ?f64) !void {
    const sub_dir = try std.fmt.allocPrint(alloc, "{s}/{s}/subagents", .{ slug_dir, sess });
    const sub_z = try alloc.dupeZ(u8, sub_dir);
    defer alloc.free(sub_z);
    const dir = std.c.opendir(sub_z) orelse {
        alloc.free(sub_dir);
        return;
    };
    defer _ = std.c.closedir(dir);

    while (std.c.readdir(dir)) |ent| {
        if (ent.type != std.c.DT.REG and ent.type != std.c.DT.UNKNOWN) continue;
        const name_ptr: [*:0]const u8 = @ptrCast(&ent.name);
        const name = std.mem.span(name_ptr);
        if (!std.mem.startsWith(u8, name, "agent-")) continue;
        if (!std.mem.endsWith(u8, name, ".jsonl")) continue;

        const path = try std.fmt.allocPrint(alloc, "{s}/{s}", .{ sub_dir, name });
        if (since) |cutoff| {
            if (!mtimeOkDarwin(alloc, path, cutoff)) {
                alloc.free(path);
                continue;
            }
        }
        try out.append(alloc, .{
            .path = path,
            .slug = slug,
            .session_id = try alloc.dupe(u8, sess),
            .host_root = root,
        });
    }
}

fn mtimeOkDarwin(alloc: Allocator, path: []const u8, earliest: f64) bool {
    const zpath = alloc.dupeZ(u8, path) catch return true;
    defer alloc.free(zpath);
    var st: std.c.Stat = undefined;
    if (std.c.fstatat(std.c.AT.FDCWD, zpath, &st, 0) != 0) return true;
    const mt = st.mtime();
    const mtime = @as(f64, @floatFromInt(mt.sec)) +
        @as(f64, @floatFromInt(mt.nsec)) / 1_000_000_000.0;
    return mtime >= earliest;
}

fn discoverLinux(alloc: Allocator, out: *std.ArrayList(DiscoveredFile), root: []const u8, since: ?f64, cwd_filter: ?[]const u8) !void {
    const linux = main.platform.linux;
    const root_z = try alloc.dupeZ(u8, root);
    defer alloc.free(root_z);
    const root_fd_ret = linux.openat(linux.AT.FDCWD, root_z, .{ .DIRECTORY = true }, 0);
    const root_fd: i32 = @bitCast(@as(u32, @truncate(root_fd_ret)));
    if (root_fd < 0) return;
    defer _ = linux.close(root_fd);

    var dent_buf: [8192]u8 = undefined;
    while (true) {
        const n = linux.getdents64(root_fd, &dent_buf, dent_buf.len);
        const signed: isize = @bitCast(n);
        if (signed <= 0) break;

        var offset: usize = 0;
        while (offset < n) {
            const entry = @as(*align(1) const linux.dirent64, @ptrCast(&dent_buf[offset]));
            offset += entry.reclen;

            if (entry.type != linux.DT.DIR) continue;
            const name_ptr: [*:0]const u8 = @ptrCast(&entry.name);
            const slug = std.mem.span(name_ptr);
            if (slug.len == 0) continue;
            if (slug[0] == '.' and (slug.len == 1 or (slug.len == 2 and slug[1] == '.'))) continue;

            if (cwd_filter) |f| {
                if (!std.mem.eql(u8, slug, f)) continue;
            }
            const slug_owned = try alloc.dupe(u8, slug);
            try scanSlugJsonlLinux(alloc, out, root, slug_owned, since);
        }
    }
}

fn scanSlugJsonlLinux(alloc: Allocator, out: *std.ArrayList(DiscoveredFile), root: []const u8, slug: []const u8, since: ?f64) !void {
    const linux = main.platform.linux;
    const slug_dir = try std.fmt.allocPrint(alloc, "{s}/{s}", .{ root, slug });
    const slug_z = try alloc.dupeZ(u8, slug_dir);
    defer alloc.free(slug_z);
    const slug_fd: i32 = @bitCast(@as(u32, @truncate(linux.openat(linux.AT.FDCWD, slug_z, .{ .DIRECTORY = true }, 0))));
    // Non-directory root entries (e.g. a stray file in the slug position)
    // fail the openat; slug_dir is arena-backed, so just return.
    if (slug_fd < 0) return;
    defer _ = linux.close(slug_fd);

    var dent_buf: [8192]u8 = undefined;
    while (true) {
        const n = linux.getdents64(slug_fd, &dent_buf, dent_buf.len);
        const signed: isize = @bitCast(n);
        if (signed <= 0) break;

        var offset: usize = 0;
        while (offset < n) {
            const entry = @as(*align(1) const linux.dirent64, @ptrCast(&dent_buf[offset]));
            offset += entry.reclen;

            const name_ptr: [*:0]const u8 = @ptrCast(&entry.name);
            const name = std.mem.span(name_ptr);
            if (entry.type == linux.DT.DIR) {
                if (name.len == 0) continue;
                if (name[0] == '.' and (name.len == 1 or (name.len == 2 and name[1] == '.'))) continue;
                try scanSubagentsLinux(alloc, out, slug_dir, slug, name, root, since);
                continue;
            }
            if (!std.mem.endsWith(u8, name, ".jsonl")) continue;
            if (entry.type != linux.DT.REG and entry.type != linux.DT.UNKNOWN) continue;

            const path = try std.fmt.allocPrint(alloc, "{s}/{s}", .{ slug_dir, name });
            if (since) |cutoff| {
                if (!mtimeOkLinux(alloc, path, cutoff)) {
                    alloc.free(path);
                    continue;
                }
            }
            const sid = try alloc.dupe(u8, name[0 .. name.len - 6]);
            try out.append(alloc, .{
                .path = path,
                .slug = slug,
                .session_id = sid,
                .host_root = root,
            });
        }
    }
}

/// Subagents: <slug>/<session>/subagents/agent-*.jsonl. session_id is the
/// enclosing session dir name (the parent session), so subagent hits group
/// with the parent in sessions_matched.
fn scanSubagentsLinux(alloc: Allocator, out: *std.ArrayList(DiscoveredFile), slug_dir: []const u8, slug: []const u8, sess: []const u8, root: []const u8, since: ?f64) !void {
    const linux = main.platform.linux;
    const sub_dir = try std.fmt.allocPrint(alloc, "{s}/{s}/subagents", .{ slug_dir, sess });
    const sub_z = try alloc.dupeZ(u8, sub_dir);
    defer alloc.free(sub_z);
    const sub_fd: i32 = @bitCast(@as(u32, @truncate(linux.openat(linux.AT.FDCWD, sub_z, .{ .DIRECTORY = true }, 0))));
    if (sub_fd < 0) {
        alloc.free(sub_dir);
        return;
    }
    defer _ = linux.close(sub_fd);

    var dent_buf: [8192]u8 = undefined;
    while (true) {
        const n = linux.getdents64(sub_fd, &dent_buf, dent_buf.len);
        const signed: isize = @bitCast(n);
        if (signed <= 0) break;

        var offset: usize = 0;
        while (offset < n) {
            const entry = @as(*align(1) const linux.dirent64, @ptrCast(&dent_buf[offset]));
            offset += entry.reclen;

            if (entry.type != linux.DT.REG and entry.type != linux.DT.UNKNOWN) continue;
            const name_ptr: [*:0]const u8 = @ptrCast(&entry.name);
            const name = std.mem.span(name_ptr);
            if (!std.mem.startsWith(u8, name, "agent-")) continue;
            if (!std.mem.endsWith(u8, name, ".jsonl")) continue;

            const path = try std.fmt.allocPrint(alloc, "{s}/{s}", .{ sub_dir, name });
            if (since) |cutoff| {
                if (!mtimeOkLinux(alloc, path, cutoff)) {
                    alloc.free(path);
                    continue;
                }
            }
            try out.append(alloc, .{
                .path = path,
                .slug = slug,
                .session_id = try alloc.dupe(u8, sess),
                .host_root = root,
            });
        }
    }
}

fn mtimeOkLinux(alloc: Allocator, path: []const u8, earliest: f64) bool {
    const linux = main.platform.linux;
    const zpath = alloc.dupeZ(u8, path) catch return true;
    defer alloc.free(zpath);
    var statx_buf: linux.Statx = std.mem.zeroes(linux.Statx);
    const ret = linux.statx(linux.AT.FDCWD, zpath, 0, .{ .MTIME = true }, &statx_buf);
    const signed: isize = @bitCast(ret);
    if (signed < 0) return true;
    const mtime = @as(f64, @floatFromInt(statx_buf.mtime.sec)) +
        @as(f64, @floatFromInt(statx_buf.mtime.nsec)) / 1_000_000_000.0;
    return mtime >= earliest;
}

// ─── Snippet ─────────────────────────────────────────────────────────────────

fn nudgeWhitespace(text: []const u8, cut: usize, direction: i32, max_nudge: usize) usize {
    if (cut == 0 or cut == text.len) return cut;
    if (direction < 0) {
        const lo = if (cut > max_nudge) cut - max_nudge else 0;
        var i = cut;
        while (i > lo) : (i -= 1) {
            const b = text[i - 1];
            if (b == ' ' or b == '\t' or b == '\n' or b == '\r') return i;
        }
    } else {
        const hi = @min(cut + max_nudge, text.len);
        var i = cut;
        while (i < hi) : (i += 1) {
            const b = text[i];
            if (b == ' ' or b == '\t' or b == '\n' or b == '\r') return i;
        }
    }
    return cut;
}

// Nudge idx forward to the next UTF-8 character boundary (mirrors Rust's
// str::is_char_boundary walk). A continuation byte has top bits 10
// (0x80..0xBF); text.len is always a boundary. Prevents a snippet cut from
// splitting a multibyte codepoint. See SPEC.md "Snippet boundaries".
fn nudgeCharBoundary(text: []const u8, idx_in: usize) usize {
    var idx = idx_in;
    while (idx < text.len and (text[idx] & 0xC0) == 0x80) : (idx += 1) {}
    return idx;
}

fn makeSnippet(text: []const u8, first_match: [2]usize, snippet_chars: u32) []const u8 {
    const half: usize = @intCast(snippet_chars / 2);
    const mstart = first_match[0];
    const mend = first_match[1];
    var lo: usize = if (mstart > half) mstart - half else 0;
    var hi: usize = @min(mend + half, text.len);
    lo = nudgeCharBoundary(text, lo);
    hi = nudgeCharBoundary(text, hi);
    if (lo > 0) lo = nudgeWhitespace(text, lo, -1, 20);
    if (hi < text.len) hi = nudgeWhitespace(text, hi, 1, 20);
    lo = nudgeCharBoundary(text, lo);
    hi = nudgeCharBoundary(text, hi);
    return text[lo..hi];
}

// ─── Hit + context ───────────────────────────────────────────────────────────

const ContextTurn = struct {
    role: []const u8,
    text: []const u8,
    timestamp: []const u8,
};

const Hit = struct {
    timestamp: f64,
    timestamp_str: []const u8,
    session_id: []const u8,
    cwd_slug: []const u8,
    host_root: []const u8,
    file_path: []const u8,
    line_number: u32,
    role: []const u8,
    snippet: []const u8,
    match_offsets: [][2]usize,
    context_before: []ContextTurn,
    context_after: []ContextTurn,
};

fn roleMatches(filter: Role, role: []const u8) bool {
    return switch (filter) {
        .both => true,
        .user => std.mem.eql(u8, role, "user"),
        .assistant => std.mem.eql(u8, role, "assistant"),
    };
}

fn buildContext(alloc: Allocator, msgs: []const ScanMessage, hit_idx: usize, n: u32) !struct { before: []ContextTurn, after: []ContextTurn } {
    if (n == 0) return .{ .before = &.{}, .after = &.{} };
    const nn: usize = n;
    const start: usize = if (hit_idx > nn) hit_idx - nn else 0;
    var before: std.ArrayList(ContextTurn) = .empty;
    var i: usize = start;
    while (i < hit_idx) : (i += 1) {
        try before.append(alloc, .{
            .role = msgs[i].role,
            .text = msgs[i].text_default,
            .timestamp = msgs[i].timestamp_str,
        });
    }
    var after: std.ArrayList(ContextTurn) = .empty;
    const end: usize = @min(hit_idx + 1 + nn, msgs.len);
    i = hit_idx + 1;
    while (i < end) : (i += 1) {
        try after.append(alloc, .{
            .role = msgs[i].role,
            .text = msgs[i].text_default,
            .timestamp = msgs[i].timestamp_str,
        });
    }
    return .{
        .before = try before.toOwnedSlice(alloc),
        .after = try after.toOwnedSlice(alloc),
    };
}

fn processFile(
    alloc: Allocator,
    file: DiscoveredFile,
    args: SearchArgs,
    pattern: *const Pattern,
    out: *std.ArrayList(Hit),
) !void {
    const msgs = try scanFile(alloc, file.path, file.format, args.include_queue_ops);
    for (msgs, 0..) |m, idx| {
        if (!roleMatches(args.role, m.role)) continue;
        if (!args.include_tool_blocks and m.is_only_tool_blocks) continue;
        if (m.timestamp) |ts| {
            if (args.since) |s| if (ts < s) continue;
            if (args.until) |u| if (ts > u) continue;
        } else if (args.since != null or args.until != null) {
            continue;
        }
        const searchable = if (args.include_tool_blocks) m.text_with_tools else m.text_default;
        if (searchable.len == 0) continue;

        const matches = try findAllMatches(alloc, pattern, searchable);
        defer alloc.free(matches);
        if (matches.len == 0) continue;

        const snippet_slice = makeSnippet(searchable, matches[0], args.snippet_chars);
        const snippet_owned = try alloc.dupe(u8, snippet_slice);
        const snippet_matches = try findAllMatches(alloc, pattern, snippet_owned);
        const ctx = try buildContext(alloc, msgs, idx, args.context);

        try out.append(alloc, .{
            .timestamp = m.timestamp orelse 0.0,
            .timestamp_str = m.timestamp_str,
            .session_id = file.session_id,
            .cwd_slug = file.slug,
            .host_root = file.host_root,
            .file_path = file.path,
            .line_number = m.line_number,
            .role = m.role,
            .snippet = snippet_owned,
            .match_offsets = snippet_matches,
            .context_before = ctx.before,
            .context_after = ctx.after,
        });
    }
}

// ─── Worker pool ─────────────────────────────────────────────────────────────

const SearchWorker = struct {
    arena: std.heap.ArenaAllocator,
    hits: std.ArrayList(Hit),
};

fn searchDoWork(
    worker: *SearchWorker,
    queue_cur: *std.atomic.Value(usize),
    files: []const DiscoveredFile,
    args: SearchArgs,
    pattern: *const Pattern,
) void {
    const wa = worker.arena.allocator();
    while (true) {
        const i = queue_cur.fetchAdd(1, .seq_cst);
        if (i >= files.len) break;
        const f = files[i];
        processFile(wa, f, args, pattern, &worker.hits) catch {};
    }
}

// ─── Sort + dedup ────────────────────────────────────────────────────────────

fn hitLessThan(_: void, a: Hit, b: Hit) bool {
    if (a.timestamp != b.timestamp) return a.timestamp > b.timestamp;
    const ord = std.mem.order(u8, a.session_id, b.session_id);
    if (ord != .eq) return ord == .lt;
    return a.line_number < b.line_number;
}

// ─── Output ──────────────────────────────────────────────────────────────────

fn writeHitJson(w: *Buf, h: Hit) !void {
    try w.appendStr("{\"type\":\"hit\",\"session_id\":");
    try writeJsonString(w, h.session_id);
    try w.appendStr(",\"cwd_slug\":");
    try writeJsonString(w, h.cwd_slug);
    try w.appendStr(",\"host_root\":");
    try writeJsonString(w, h.host_root);
    try w.appendStr(",\"file_path\":");
    try writeJsonString(w, h.file_path);
    try w.appendFmt(",\"line_number\":{d}", .{h.line_number});
    try w.appendStr(",\"timestamp\":");
    try writeJsonString(w, h.timestamp_str);
    try w.appendStr(",\"role\":");
    try writeJsonString(w, h.role);
    try w.appendStr(",\"snippet\":");
    try writeJsonString(w, h.snippet);
    try w.appendStr(",\"match_offsets\":[");
    for (h.match_offsets, 0..) |m, k| {
        if (k > 0) try w.appendStr(",");
        try w.appendFmt("[{d},{d}]", .{ m[0], m[1] });
    }
    try w.appendStr("],\"context_before\":[");
    for (h.context_before, 0..) |t, k| {
        if (k > 0) try w.appendStr(",");
        try writeCtxTurn(w, t);
    }
    try w.appendStr("],\"context_after\":[");
    for (h.context_after, 0..) |t, k| {
        if (k > 0) try w.appendStr(",");
        try writeCtxTurn(w, t);
    }
    try w.appendStr("]}");
}

fn writeCtxTurn(w: *Buf, t: ContextTurn) !void {
    try w.appendStr("{\"role\":");
    try writeJsonString(w, t.role);
    try w.appendStr(",\"text\":");
    try writeJsonString(w, t.text);
    try w.appendStr(",\"timestamp\":");
    try writeJsonString(w, t.timestamp);
    try w.appendStr("}");
}

fn writeSummaryJson(w: *Buf, hits: u64, sessions: u64, roots: u64, files: u64, truncated: bool, elapsed_ms: u64) !void {
    try w.appendFmt(
        "{{\"type\":\"summary\",\"hits\":{d},\"sessions_matched\":{d},\"roots_walked\":{d},\"files_walked\":{d},\"truncated\":{s},\"elapsed_ms\":{d}}}",
        .{ hits, sessions, roots, files, if (truncated) "true" else "false", elapsed_ms },
    );
}

// ─── Top-level run ───────────────────────────────────────────────────────────

pub fn run(gpa: Allocator, argv: [][]const u8) !void {
    const t0 = main.perfNow();
    const frq = main.perfFreq();

    var arena = std.heap.ArenaAllocator.init(gpa);
    defer arena.deinit();
    const alloc = arena.allocator();

    const args = try parseArgs(alloc, argv);

    var pattern = compilePattern(alloc, args.pattern, args.regex, args.case_sensitive) catch {
        writeStderrFmt(alloc, "walker: search: bad pattern\n", .{});
        std.process.exit(2);
    };
    defer pattern.deinit();

    const roots = try walker_roots.resolveSearchRoots(alloc, args.projects_root, args.extra_roots, args.read_config);
    const files = try discoverFiles(alloc, roots, args.since, args.cwd);
    const files_walked: u64 = files.len;
    const roots_walked: u64 = roots.len;

    // Worker pool: each worker scans a subset of files using its own arena
    // (arena allocators are not thread-safe). Worker arenas hold the Hit
    // string slices (snippets, context turns, etc.) and stay alive via the
    // bottom defer until after the output buffer is written to stdout.
    const ncpu = std.Thread.getCpuCount() catch 4;
    const nw = @min(8, ncpu);
    const workers = try alloc.alloc(SearchWorker, nw);
    for (workers) |*w| w.* = .{
        .arena = std.heap.ArenaAllocator.init(gpa),
        .hits = .empty,
    };
    defer for (workers) |*w| w.arena.deinit();

    var queue_cur = std.atomic.Value(usize).init(0);
    const threads = try alloc.alloc(std.Thread, nw);
    for (workers, 0..) |_, i| {
        threads[i] = try std.Thread.spawn(
            .{},
            searchDoWork,
            .{ &workers[i], &queue_cur, files, args, &pattern },
        );
    }
    for (threads) |th| th.join();

    var hits: std.ArrayList(Hit) = .empty;
    for (workers) |*w| try hits.appendSlice(alloc, w.hits.items);

    std.mem.sort(Hit, hits.items, {}, hitLessThan);

    // sessions_matched counted BEFORE truncation
    var session_set = std.StringHashMap(void).init(alloc);
    defer session_set.deinit();
    for (hits.items) |h| {
        const key = try std.fmt.allocPrint(alloc, "{s}/{s}", .{ h.cwd_slug, h.session_id });
        try session_set.put(key, {});
    }
    const sessions_matched: u64 = session_set.count();

    const total_unfiltered: u64 = hits.items.len;
    const truncated = total_unfiltered > args.limit;
    const limit_us: usize = args.limit;
    if (truncated) {
        hits.shrinkRetainingCapacity(limit_us);
    }

    const elapsed_ms: u64 = @intCast(@divTrunc((main.perfNow() - t0) * 1000, frq));
    const hits_output: u64 = if (args.count_only) total_unfiltered else hits.items.len;

    var w = Buf{ .alloc = alloc };
    switch (args.format) {
        .jsonl => {
            if (!args.count_only) {
                for (hits.items) |h| {
                    try writeHitJson(&w, h);
                    try w.appendStr("\n");
                }
            }
            try writeSummaryJson(&w, hits_output, sessions_matched, roots_walked, files_walked, truncated, elapsed_ms);
            try w.appendStr("\n");
            main.writeStdout(w.items());
        },
        .pretty => {
            if (!args.count_only) {
                for (hits.items) |h| {
                    w.appendFmt("[{s}] cwd={s} role={s} session={s}\n", .{ h.timestamp_str, h.cwd_slug, h.role, h.session_id }) catch {};
                    w.appendFmt("  {s}:{d}\n", .{ h.file_path, h.line_number }) catch {};
                    for (h.context_before) |t| {
                        w.appendFmt("  before: {s}\n", .{truncateForDisplay(t.text)}) catch {};
                    }
                    if (h.match_offsets.len > 0) {
                        const mo = h.match_offsets[0];
                        const ms = @min(mo[0], h.snippet.len);
                        const me = @min(mo[1], h.snippet.len);
                        w.appendFmt("  >>> {s}[{s}]{s} <<<\n", .{ h.snippet[0..ms], h.snippet[ms..me], h.snippet[me..] }) catch {};
                    } else {
                        w.appendFmt("  {s}\n", .{h.snippet}) catch {};
                    }
                    for (h.context_after) |t| {
                        w.appendFmt("  after:  {s}\n", .{truncateForDisplay(t.text)}) catch {};
                    }
                    w.appendStr("\n") catch {};
                }
            }
            w.appendFmt("{d} hits in {d} sessions across {d} roots ({d} files). truncated={s} elapsed {d}ms.\n", .{
                hits_output,                        sessions_matched, roots_walked, files_walked,
                if (truncated) "true" else "false", elapsed_ms,
            }) catch {};
            main.writeStdout(w.items());
        },
    }

    if (truncated) {
        writeStderrFmt(alloc, "walker: search: truncated to --limit={d} (had {d} total); narrow with --since\n", .{ args.limit, total_unfiltered });
    }
}

fn truncateForDisplay(s: []const u8) []const u8 {
    return if (s.len <= 120) s else s[0..120];
}
