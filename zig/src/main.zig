// Native pace-walker -- Zig implementation (cross-platform: Linux + Windows + macOS).
// See ../SPEC.md for the contract every implementation must honor.
//
// Linux uses raw syscalls via std.os.linux (no libc). Darwin uses libc (std.c)
// since it has no stable syscall ABI. Windows uses Win32 directly.

const std = @import("std");
const builtin = @import("builtin");
const Allocator = std.mem.Allocator;
const beacons = @import("beacons.zig");
const search = @import("search.zig");
const events = @import("events.zig");
const walker_roots = @import("walker_roots.zig");

pub const VERSION = "zig/0.1.1";
pub const is_windows = builtin.os.tag == .windows;
pub const is_darwin = builtin.os.tag == .macos;

// ─── Platform abstraction ────────────────────────────────────────────────────

const PlatformFd = if (is_windows) *anyopaque else i32;

pub const platform = if (is_windows) struct {
    pub const HANDLE = *anyopaque;
    const DWORD = u32;
    const BOOL = i32;
    const WCHAR = u16;
    pub const LARGE_INTEGER = extern union { parts: extern struct { lo: u32, hi: i32 }, quad: i64 };
    pub const INVALID_HANDLE_VALUE: HANDLE = @ptrFromInt(@as(usize, @bitCast(@as(isize, -1))));
    pub const FILETIME = extern struct {
        lo: u32 = 0,
        hi: u32 = 0,
        pub fn toUnix(ft: FILETIME) f64 {
            const hns: i64 = (@as(i64, ft.hi) << 32) | @as(i64, ft.lo);
            return @as(f64, @floatFromInt(hns - 116_444_736_000_000_000)) / 10_000_000.0;
        }
    };
    pub const WIN32_FILE_ATTRIBUTE_DATA = extern struct {
        dwFileAttributes: u32,
        ftCreationTime: FILETIME,
        ftLastAccessTime: FILETIME,
        ftLastWriteTime: FILETIME,
        nFileSizeHigh: u32,
        nFileSizeLow: u32,
    };
    pub const WIN32_FIND_DATAW = extern struct {
        dwFileAttributes: u32,
        ftCreationTime: FILETIME,
        ftLastAccessTime: FILETIME,
        ftLastWriteTime: FILETIME,
        nFileSizeHigh: u32,
        nFileSizeLow: u32,
        dwReserved0: u32,
        dwReserved1: u32,
        cFileName: [260]u16,
        cAlternateFileName: [14]u16,
    };
    const SECURITY_ATTRIBUTES = extern struct {
        nLength: u32,
        lpSecurityDescriptor: ?*anyopaque,
        bInheritHandle: i32,
    };
    pub const GENERIC_READ = 0x80000000;
    pub const FILE_SHARE_READ = 0x00000001;
    pub const OPEN_EXISTING = 3;
    pub const FILE_ATTRIBUTE_NORMAL = 0x80;
    pub const FILE_ATTRIBUTE_DIRECTORY = 0x10;
    pub const STD_OUTPUT_HANDLE: u32 = @bitCast(@as(i32, -11));

    extern "kernel32" fn GetSystemTimeAsFileTime(*FILETIME) callconv(.winapi) void;
    extern "kernel32" fn QueryPerformanceCounter(*LARGE_INTEGER) callconv(.winapi) BOOL;
    extern "kernel32" fn QueryPerformanceFrequency(*LARGE_INTEGER) callconv(.winapi) BOOL;
    pub extern "kernel32" fn GetFileAttributesExW([*:0]const u16, u32, *WIN32_FILE_ATTRIBUTE_DATA) callconv(.winapi) BOOL;
    extern "kernel32" fn GetCommandLineW() callconv(.winapi) [*:0]const u16;
    extern "kernel32" fn GetEnvironmentVariableW([*:0]const u16, [*]u16, u32) callconv(.winapi) u32;
    extern "kernel32" fn GetStdHandle(nStdHandle: u32) callconv(.winapi) ?HANDLE;
    extern "kernel32" fn WriteFile(hFile: HANDLE, lpBuffer: *const anyopaque, nNumberOfBytesToWrite: u32, lpNumberOfBytesWritten: *u32, lpOverlapped: ?*anyopaque) callconv(.winapi) BOOL;
    extern "kernel32" fn ReadFile(hFile: HANDLE, lpBuffer: *anyopaque, nNumberOfBytesToRead: u32, lpNumberOfBytesRead: *u32, lpOverlapped: ?*anyopaque) callconv(.winapi) BOOL;
    extern "kernel32" fn CreateFileW(lpFileName: [*:0]const u16, dwDesiredAccess: u32, dwShareMode: u32, lpSecurityAttributes: ?*const SECURITY_ATTRIBUTES, dwCreationDisposition: u32, dwFlagsAndAttributes: u32, hTemplateFile: ?HANDLE) callconv(.winapi) ?HANDLE;
    extern "kernel32" fn CloseHandle(hObject: HANDLE) callconv(.winapi) BOOL;
    pub extern "kernel32" fn FindFirstFileW(lpFileName: [*:0]const u16, lpFindFileData: *WIN32_FIND_DATAW) callconv(.winapi) ?HANDLE;
    pub extern "kernel32" fn FindNextFileW(hFindFile: HANDLE, lpFindFileData: *WIN32_FIND_DATAW) callconv(.winapi) BOOL;
    pub extern "kernel32" fn FindClose(hFindFile: HANDLE) callconv(.winapi) BOOL;
} else struct {
    pub const linux = std.os.linux;
};

pub fn nowUnix() f64 {
    if (is_windows) {
        var ft: platform.FILETIME = .{};
        platform.GetSystemTimeAsFileTime(&ft);
        return ft.toUnix();
    } else if (is_darwin) {
        var ts: std.c.timespec = undefined;
        _ = std.c.clock_gettime(std.c.CLOCK.REALTIME, &ts);
        return @as(f64, @floatFromInt(ts.sec)) + @as(f64, @floatFromInt(ts.nsec)) / 1_000_000_000.0;
    } else {
        var ts: platform.linux.timespec = undefined;
        _ = platform.linux.clock_gettime(platform.linux.CLOCK.REALTIME, &ts);
        return @as(f64, @floatFromInt(ts.sec)) + @as(f64, @floatFromInt(ts.nsec)) / 1_000_000_000.0;
    }
}

pub fn perfNow() i64 {
    if (is_windows) {
        var v: platform.LARGE_INTEGER = undefined;
        _ = platform.QueryPerformanceCounter(&v);
        return v.quad;
    } else if (is_darwin) {
        var ts: std.c.timespec = undefined;
        _ = std.c.clock_gettime(std.c.CLOCK.MONOTONIC, &ts);
        return ts.sec * 1_000_000_000 + ts.nsec;
    } else {
        var ts: platform.linux.timespec = undefined;
        _ = platform.linux.clock_gettime(platform.linux.CLOCK.MONOTONIC, &ts);
        return ts.sec * 1_000_000_000 + ts.nsec;
    }
}

pub fn perfFreq() i64 {
    if (is_windows) {
        var v: platform.LARGE_INTEGER = undefined;
        _ = platform.QueryPerformanceFrequency(&v);
        return v.quad;
    } else {
        return 1_000_000_000;
    }
}

pub fn writeStdout(bytes: []const u8) void {
    if (is_windows) {
        const h = platform.GetStdHandle(platform.STD_OUTPUT_HANDLE).?;
        var written: u32 = 0;
        var offset: usize = 0;
        while (offset < bytes.len) {
            const chunk: u32 = @intCast(@min(bytes.len - offset, 65536));
            const ptr: *const anyopaque = @ptrCast(bytes.ptr + offset);
            _ = platform.WriteFile(h, ptr, chunk, &written, null);
            if (written == 0) break;
            offset += written;
        }
    } else if (is_darwin) {
        var offset: usize = 0;
        while (offset < bytes.len) {
            const n = std.c.write(1, bytes.ptr + offset, bytes.len - offset);
            if (n <= 0) break;
            offset += @intCast(n);
        }
    } else {
        var offset: usize = 0;
        while (offset < bytes.len) {
            const n = platform.linux.write(1, bytes.ptr + offset, bytes.len - offset);
            const signed: isize = @bitCast(n);
            if (signed <= 0) break;
            offset += @intCast(signed);
        }
    }
}

pub fn writeStderr(bytes: []const u8) void {
    if (is_windows) {
        const h = platform.GetStdHandle(@bitCast(@as(i32, -12))).?;
        var written: u32 = 0;
        var offset: usize = 0;
        while (offset < bytes.len) {
            const chunk: u32 = @intCast(@min(bytes.len - offset, 65536));
            const ptr: *const anyopaque = @ptrCast(bytes.ptr + offset);
            _ = platform.WriteFile(h, ptr, chunk, &written, null);
            if (written == 0) break;
            offset += written;
        }
    } else if (is_darwin) {
        var offset: usize = 0;
        while (offset < bytes.len) {
            const n = std.c.write(2, bytes.ptr + offset, bytes.len - offset);
            if (n <= 0) break;
            offset += @intCast(n);
        }
    } else {
        var offset: usize = 0;
        while (offset < bytes.len) {
            const n = platform.linux.write(2, bytes.ptr + offset, bytes.len - offset);
            const signed: isize = @bitCast(n);
            if (signed <= 0) break;
            offset += @intCast(signed);
        }
    }
}

// ─── File I/O abstraction ────────────────────────────────────────────────────

fn fileOpen(alloc: Allocator, path: []const u8) !PlatformFd {
    if (is_windows) {
        const wpath = try std.unicode.utf8ToUtf16LeAllocZ(alloc, path);
        const h = platform.CreateFileW(wpath.ptr, platform.GENERIC_READ, platform.FILE_SHARE_READ, null, platform.OPEN_EXISTING, platform.FILE_ATTRIBUTE_NORMAL, null) orelse return error.OpenFailed;
        if (h == platform.INVALID_HANDLE_VALUE) return error.OpenFailed;
        return h;
    } else if (is_darwin) {
        const zpath = try alloc.dupeZ(u8, path);
        defer alloc.free(zpath);
        const fd = std.c.openat(std.c.AT.FDCWD, zpath, .{});
        if (fd < 0) return error.OpenFailed;
        return fd;
    } else {
        const zpath = try alloc.dupeZ(u8, path);
        defer alloc.free(zpath);
        const ret = platform.linux.openat(platform.linux.AT.FDCWD, zpath, .{}, 0);
        const fd: i32 = @bitCast(@as(u32, @truncate(ret)));
        if (fd < 0) return error.OpenFailed;
        return fd;
    }
}

fn fileClose(fd: PlatformFd) void {
    if (is_windows) {
        _ = platform.CloseHandle(fd);
    } else if (is_darwin) {
        _ = std.c.close(fd);
    } else {
        _ = platform.linux.close(fd);
    }
}

fn fileRead(fd: PlatformFd, buf: []u8) !usize {
    if (is_windows) {
        var n: u32 = 0;
        if (platform.ReadFile(fd, buf.ptr, @intCast(buf.len), &n, null) == 0) return error.ReadFailed;
        return n;
    } else if (is_darwin) {
        const n = std.c.read(fd, buf.ptr, buf.len);
        if (n < 0) return error.ReadFailed;
        return @intCast(n);
    } else {
        const n = platform.linux.read(fd, buf.ptr, buf.len);
        const signed: isize = @bitCast(n);
        if (signed < 0) return error.ReadFailed;
        return @intCast(signed);
    }
}

pub fn readEntireFile(alloc: Allocator, path: []const u8) ![]u8 {
    const fd = try fileOpen(alloc, path);
    defer fileClose(fd);
    var buf: std.ArrayList(u8) = .empty;
    var read_buf: [65536]u8 = undefined;
    while (true) {
        const n = fileRead(fd, &read_buf) catch break;
        if (n == 0) break;
        buf.appendSlice(alloc, read_buf[0..n]) catch break;
    }
    return buf.toOwnedSlice(alloc) catch buf.items;
}

// ─── mtime filter ────────────────────────────────────────────────────────────

fn mtimeOk(alloc: Allocator, path: []const u8, earliest: f64) bool {
    if (is_windows) {
        const wpath = std.unicode.utf8ToUtf16LeAllocZ(alloc, path) catch return true;
        var info: platform.WIN32_FILE_ATTRIBUTE_DATA = undefined;
        if (platform.GetFileAttributesExW(wpath.ptr, 0, &info) == 0) return true;
        return info.ftLastWriteTime.toUnix() >= earliest;
    } else if (is_darwin) {
        const zpath = alloc.dupeZ(u8, path) catch return true;
        defer alloc.free(zpath);
        var st: std.c.Stat = undefined;
        if (std.c.fstatat(std.c.AT.FDCWD, zpath, &st, 0) != 0) return true;
        const mt = st.mtime();
        const mtime = @as(f64, @floatFromInt(mt.sec)) +
            @as(f64, @floatFromInt(mt.nsec)) / 1_000_000_000.0;
        return mtime >= earliest;
    } else {
        const zpath = alloc.dupeZ(u8, path) catch return true;
        defer alloc.free(zpath);
        var statx_buf: platform.linux.Statx = std.mem.zeroes(platform.linux.Statx);
        const ret = platform.linux.statx(
            platform.linux.AT.FDCWD,
            zpath,
            0,
            .{ .MTIME = true },
            &statx_buf,
        );
        const signed: isize = @bitCast(ret);
        if (signed < 0) return true;
        const mtime = @as(f64, @floatFromInt(statx_buf.mtime.sec)) +
            @as(f64, @floatFromInt(statx_buf.mtime.nsec)) / 1_000_000_000.0;
        return mtime >= earliest;
    }
}

// ─── Command-line parsing ────────────────────────────────────────────────────

pub fn getArgs(alloc: Allocator) ![][]const u8 {
    if (is_windows) {
        return getArgsWindows(alloc);
    } else if (is_darwin) {
        return getArgsDarwin(alloc);
    } else {
        return getArgsLinux(alloc);
    }
}

// On Darwin libc exposes argv via _NSGetArgv()/_NSGetArgc() (crt_externs.h).
extern "c" fn _NSGetArgv() *[*][*:0]u8;
extern "c" fn _NSGetArgc() *c_int;

fn getArgsDarwin(alloc: Allocator) ![][]const u8 {
    const argc: usize = @intCast(_NSGetArgc().*);
    const argv = _NSGetArgv().*;
    var out: std.ArrayList([]const u8) = .empty;
    var i: usize = 1; // skip argv[0]
    while (i < argc) : (i += 1) {
        const arg = std.mem.span(argv[i]);
        try out.append(alloc, try alloc.dupe(u8, arg));
    }
    return out.toOwnedSlice(alloc);
}

fn getArgsLinux(alloc: Allocator) ![][]const u8 {
    const fd: i32 = @bitCast(@as(u32, @truncate(platform.linux.openat(
        platform.linux.AT.FDCWD,
        "/proc/self/cmdline\x00",
        .{},
        0,
    ))));
    if (fd < 0) return error.OpenFailed;
    defer _ = platform.linux.close(fd);

    var buf: [65536]u8 = undefined;
    var total: usize = 0;
    while (total < buf.len) {
        const n = platform.linux.read(fd, buf[total..].ptr, buf.len - total);
        const signed: isize = @bitCast(n);
        if (signed <= 0) break;
        total += @intCast(signed);
    }

    var out: std.ArrayList([]const u8) = .empty;
    var i: usize = 0;
    var first = true;
    while (i < total) {
        var end = i;
        while (end < total and buf[end] != 0) end += 1;
        if (end > i) {
            if (!first)
                try out.append(alloc, try alloc.dupe(u8, buf[i..end]));
            first = false;
        }
        i = end + 1;
    }
    return out.toOwnedSlice(alloc);
}

fn getArgsWindows(alloc: Allocator) ![][]const u8 {
    const wcmd = platform.GetCommandLineW();
    const wlen = std.mem.len(wcmd);
    const ws = wcmd[0..wlen];

    var out: std.ArrayList([]const u8) = .empty;
    var i: usize = 0;
    var first = true;

    while (i < wlen) {
        while (i < wlen and (ws[i] == ' ' or ws[i] == '\t')) i += 1;
        if (i >= wlen) break;

        var tok: std.ArrayList(u16) = .empty;
        var in_q = false;
        while (i < wlen) {
            const c = ws[i];
            if (c == '"') {
                in_q = !in_q;
                i += 1;
            } else if (!in_q and (c == ' ' or c == '\t')) break else {
                try tok.append(alloc, c);
                i += 1;
            }
        }

        if (tok.items.len > 0 and !first)
            try out.append(alloc, try std.unicode.utf16LeToUtf8Alloc(alloc, tok.items));
        first = false;
        tok.deinit(alloc);
    }
    return out.toOwnedSlice(alloc);
}

pub fn getEnvVar(alloc: Allocator, name: []const u8) ?[]const u8 {
    if (is_windows) {
        var wn: [256:0]u16 = undefined;
        const n = std.unicode.utf8ToUtf16Le(wn[0..255], name) catch return null;
        wn[n] = 0;
        var wv: [32768]u16 = undefined;
        const len = platform.GetEnvironmentVariableW(@ptrCast(&wn), &wv, @intCast(wv.len));
        if (len == 0) return null;
        return std.unicode.utf16LeToUtf8Alloc(alloc, wv[0..len]) catch null;
    } else if (is_darwin) {
        const name_z = alloc.dupeZ(u8, name) catch return null;
        defer alloc.free(name_z);
        const v = std.c.getenv(name_z) orelse return null;
        return alloc.dupe(u8, std.mem.span(v)) catch null;
    } else {
        // Read /proc/self/environ to find the variable without libc
        const fd: i32 = @bitCast(@as(u32, @truncate(platform.linux.openat(
            platform.linux.AT.FDCWD,
            "/proc/self/environ\x00",
            .{},
            0,
        ))));
        if (fd < 0) return null;
        defer _ = platform.linux.close(fd);

        var buf: [65536]u8 = undefined;
        var total: usize = 0;
        while (total < buf.len) {
            const rd = platform.linux.read(fd, buf[total..].ptr, buf.len - total);
            const signed: isize = @bitCast(rd);
            if (signed <= 0) break;
            total += @intCast(signed);
        }

        // Entries are null-separated: NAME=VALUE\0NAME=VALUE\0...
        var i: usize = 0;
        while (i < total) {
            var end = i;
            while (end < total and buf[end] != 0) end += 1;
            const entry = buf[i..end];
            if (entry.len > name.len and entry[name.len] == '=' and
                std.mem.eql(u8, entry[0..name.len], name))
            {
                return alloc.dupe(u8, entry[name.len + 1 ..]) catch null;
            }
            i = end + 1;
        }
        return null;
    }
}

// ─── CLI ─────────────────────────────────────────────────────────────────────

const Cli = struct {
    period: u64 = 0,
    win_start: f64 = 0.0,
    now: ?f64 = null,
    root: ?[]const u8 = null,
    extra_roots: [][]const u8 = &.{},
    read_config: bool = true,
};

fn parseCli(alloc: Allocator, argv: [][]const u8) !Cli {
    var cli = Cli{};
    var extras: std.ArrayList([]const u8) = .empty;
    var win_start_set = false;
    var i: usize = 0;
    while (i < argv.len) {
        const flag = argv[i];
        i += 1;
        if (std.mem.eql(u8, flag, "--period")) {
            cli.period = std.fmt.parseInt(u64, grab(argv, &i, "--period"), 10) catch die("--period: invalid");
        } else if (std.mem.eql(u8, flag, "--win-start")) {
            cli.win_start = std.fmt.parseFloat(f64, grab(argv, &i, "--win-start")) catch die("--win-start: invalid");
            win_start_set = true;
        } else if (std.mem.eql(u8, flag, "--now")) {
            cli.now = std.fmt.parseFloat(f64, grab(argv, &i, "--now")) catch die("--now: invalid");
        } else if (std.mem.eql(u8, flag, "--projects-root")) {
            cli.root = grab(argv, &i, "--projects-root");
        } else if (std.mem.eql(u8, flag, "--extra-projects-root")) {
            try extras.append(alloc, grab(argv, &i, "--extra-projects-root"));
        } else if (std.mem.eql(u8, flag, "--no-config")) {
            cli.read_config = false;
        } else if (std.mem.eql(u8, flag, "--version")) {
            writeStdout(VERSION ++ "\n");
            std.process.exit(0);
        } else {
            std.debug.print("walker: unknown flag: {s}\n", .{flag});
            usagePointer();
            std.process.exit(2);
        }
    }
    if (cli.period == 0) die("--period is required");
    if (!win_start_set) die("--win-start is required");
    cli.extra_roots = try extras.toOwnedSlice(alloc);
    return cli;
}

pub fn grab(argv: [][]const u8, i: *usize, flag: []const u8) []const u8 {
    if (i.* >= argv.len) {
        std.debug.print("walker: {s} needs a value\n", .{flag});
        usagePointer();
        std.process.exit(2);
    }
    defer i.* += 1;
    return argv[i.*];
}

pub fn usagePointer() void {
    std.debug.print("Run 'claude-walker --help' for usage.\n", .{});
}

pub fn die(msg: []const u8) noreturn {
    std.debug.print("walker: {s}\n", .{msg});
    usagePointer();
    std.process.exit(2);
}

const HELP =
    \\claude-walker - fast cost & progress walker over Claude Code transcripts
    \\
    \\USAGE:
    \\    claude-walker [SUBCOMMAND] [OPTIONS]
    \\
    \\With no subcommand it runs `cost` (back-compat for the status line).
    \\
    \\SUBCOMMANDS:
    \\    cost              Trailing + window USD over the transcript fleet (default)
    \\    search <pattern>  Cross-root/-machine content search over transcripts
    \\    events            One NDJSON line per assistant turn (ts, usd, model, session)
    \\    beacons-latest    Most recent <progress-beacon> for a session
    \\    beacons-history   Calibration bias_factor over begin/end beacon pairs
    \\
    \\COST OPTIONS (default mode):
    \\    --period <seconds>            Required. Trailing-window length.
    \\    --win-start <unix>            Required. Cost-window start (unix epoch).
    \\    --projects-root <path>        Transcript root (default: ~/.claude/projects).
    \\    --extra-projects-root <path>  Additional root; repeatable.
    \\    --no-config                   Skip ~/.claude/walker-roots.json extras.
    \\    --now <unix>                  Pin "now" (default: wall clock; for tests).
    \\
    \\GLOBAL:
    \\    -h, --help     Show this help.
    \\    --version      Print <lang>/<version>.
    \\
    \\Full contract: SPEC.md in the source tree.
    \\
;

fn isHelpFlag(s: []const u8) bool {
    return std.mem.eql(u8, s, "-h") or std.mem.eql(u8, s, "--help");
}

// Help is shown when: no args, or the first arg is -h/--help, or the first
// arg is a known subcommand followed by -h/--help. See SPEC.md "Help & usage".
fn wantsHelp(argv: [][]const u8) bool {
    if (argv.len == 0) return true;
    if (isHelpFlag(argv[0])) return true;
    const subs = [_][]const u8{ "cost", "beacons-latest", "beacons-history", "search", "events" };
    for (subs) |s| {
        if (std.mem.eql(u8, argv[0], s)) {
            return argv.len > 1 and isHelpFlag(argv[1]);
        }
    }
    return false;
}

// ─── Pricing ─────────────────────────────────────────────────────────────────

/// Flat charge per server-side web search request (billed $10 / 1,000),
/// added on top of token cost. Matches SPEC.md and the Python reference.
pub const WEB_SEARCH_COST_USD: f64 = 0.01;

/// Sonnet 5 intro->standard boundary. Intro pricing runs through 2026-08-31
/// inclusive; standard applies from 2026-09-01 onward. Selection is a
/// lexicographic compare of the turn's ISO-8601 date prefix ("YYYY-MM-DD")
/// against this string — monotonic for zero-padded dates, no timezone math.
pub const SONNET5_STANDARD_FROM = "2026-09-01";

/// `date_prefix` is the turn's ISO-8601 timestamp (only its leading 10-char
/// "YYYY-MM-DD" is consulted, and only for the sonnet-5 family).
pub fn modelCost(inp: u64, out_: u64, cr: u64, cw: u64, web_searches: u64, model: []const u8, date_prefix: []const u8) f64 {
    var buf: [256]u8 = undefined;
    const n = @min(model.len, buf.len);
    const lo = std.ascii.lowerString(buf[0..n], model[0..n]);
    // Ordered family match; see SPEC.md. fable/mythos first ($10/$50), then
    // opus, haiku, then date-aware sonnet-5, then the generic sonnet default.
    var ir: f64 = 3.0;
    var or_: f64 = 15.0;
    if (std.mem.indexOf(u8, lo, "fable") != null or std.mem.indexOf(u8, lo, "mythos") != null) {
        ir = 10.0;
        or_ = 50.0;
    } else if (std.mem.indexOf(u8, lo, "opus") != null) {
        ir = 5.0;
        or_ = 25.0;
    } else if (std.mem.indexOf(u8, lo, "haiku") != null) {
        ir = 1.0;
        or_ = 5.0;
    } else if (std.mem.indexOf(u8, lo, "sonnet-5") != null) {
        // "sonnet-5" does NOT match "claude-sonnet-4-5" (no such substring),
        // so Sonnet 4.5 keeps the generic sonnet default below.
        const standard = date_prefix.len >= 10 and
            std.mem.order(u8, date_prefix[0..10], SONNET5_STANDARD_FROM) != .lt;
        ir = if (standard) 3.0 else 2.0;
        or_ = if (standard) 15.0 else 10.0;
    }
    const token_cost = (@as(f64, @floatFromInt(inp)) * ir +
        @as(f64, @floatFromInt(cr)) * ir * 0.10 +
        @as(f64, @floatFromInt(cw)) * ir * 1.25 +
        @as(f64, @floatFromInt(out_)) * or_) / 1_000_000.0;
    return token_cost + @as(f64, @floatFromInt(web_searches)) * WEB_SEARCH_COST_USD;
}

// ─── ISO 8601 ────────────────────────────────────────────────────────────────

pub fn parseTs(s: []const u8) !f64 {
    // Accept ISO 8601: "YYYY-MM-DDTHH:MM:SS[.frac][Z|+HH:MM|-HH:MM]".
    // Rust/C++/Go all accept the numeric-offset form; Zig must too.
    if (s.len < 19) return error.Bad;
    if (s[4] != '-' or s[7] != '-' or s[10] != 'T' or s[13] != ':' or s[16] != ':')
        return error.Bad;
    const yr: i32 = try std.fmt.parseInt(i32, s[0..4], 10);
    const mo: u32 = try std.fmt.parseInt(u32, s[5..7], 10);
    const dy: u32 = try std.fmt.parseInt(u32, s[8..10], 10);
    const hr: u32 = try std.fmt.parseInt(u32, s[11..13], 10);
    const mn: u32 = try std.fmt.parseInt(u32, s[14..16], 10);
    const sc: u32 = try std.fmt.parseInt(u32, s[17..19], 10);
    // Per SPEC: malformed ISO 8601 → skip. Range-validate so values like
    // "2026-13-99T99:99:99Z" don't silently roll into a real epoch second.
    if (mo < 1 or mo > 12) return error.Bad;
    const dim_max: u32 = @intCast(dim(mo, yr));
    if (dy < 1 or dy > dim_max) return error.Bad;
    if (hr > 23 or mn > 59 or sc > 60) return error.Bad;
    // Fractional seconds (optional).
    var frac: f64 = 0.0;
    var pos: usize = 19;
    if (pos < s.len and s[pos] == '.') {
        pos += 1;
        const start = pos;
        while (pos < s.len and s[pos] >= '0' and s[pos] <= '9') : (pos += 1) {}
        if (pos == start) return error.Bad;
        const fs = s[start..pos];
        const fn_ = try std.fmt.parseInt(u64, fs, 10);
        frac = @as(f64, @floatFromInt(fn_)) / std.math.pow(f64, 10.0, @as(f64, @floatFromInt(fs.len)));
    }
    // Timezone: optional Z, +HH:MM, -HH:MM, or absent (naive UTC).
    var tz_offset_sec: i64 = 0;
    if (pos < s.len) {
        const c = s[pos];
        if (c == 'Z') {
            pos += 1;
        } else if (c == '+' or c == '-') {
            // Need +HH:MM (5 more chars).
            if (pos + 6 > s.len) return error.Bad;
            if (s[pos + 3] != ':') return error.Bad;
            const tz_h: i64 = try std.fmt.parseInt(i64, s[pos + 1 .. pos + 3], 10);
            const tz_m: i64 = try std.fmt.parseInt(i64, s[pos + 4 .. pos + 6], 10);
            if (tz_h > 23 or tz_m > 59) return error.Bad;
            const sign: i64 = if (c == '-') -1 else 1;
            tz_offset_sec = sign * (tz_h * 3600 + tz_m * 60);
            pos += 6;
        } else {
            return error.Bad;
        }
    }
    if (pos != s.len) return error.Bad;
    const epoch_sec: i64 = calToUnix(yr, mo, dy, hr, mn, sc) - tz_offset_sec;
    return @as(f64, @floatFromInt(epoch_sec)) + frac;
}

fn leap(y: i32) bool {
    return (@rem(y, 4) == 0 and @rem(y, 100) != 0) or @rem(y, 400) == 0;
}
fn dim(m: u32, y: i32) i64 {
    return switch (m) {
        1, 3, 5, 7, 8, 10, 12 => 31,
        4, 6, 9, 11 => 30,
        2 => if (leap(y)) 29 else 28,
        else => 0,
    };
}
fn calToUnix(yr: i32, mo: u32, dy: u32, hr: u32, mn: u32, sc: u32) i64 {
    var d: i64 = 0;
    var y: i32 = 1970;
    if (yr >= 1970) {
        while (y < yr) : (y += 1) d += if (leap(y)) 366 else 365;
    } else {
        while (y > yr) : (y -= 1) d -= if (leap(y - 1)) 366 else 365;
    }
    var m: u32 = 1;
    while (m < mo) : (m += 1) d += dim(m, yr);
    d += @as(i64, dy) - 1;
    return d * 86400 + @as(i64, hr) * 3600 + @as(i64, mn) * 60 + @as(i64, sc);
}

// ─── JSON Scanner helpers ────────────────────────────────────────────────────
//
// Five-field token streaming over the JSONL hot path. Beats
// `std.json.parseFromSlice(Value, ...)` by ~10x because it skips the
// `ObjectMap` allocation and per-string dup that `Value` materialization
// requires. Strings returned with `.alloc_if_needed` are slices into the
// input buffer (zero-copy) when no escape decoding is required, which is
// the typical case for the keys we read (`role`, `id`, `model`,
// `timestamp`, `input_tokens`, etc.).

/// Peek-and-descend into an object. Returns true if the next value was an
/// object (and consumes its `object_begin`); false otherwise (and skips
/// the value entirely). Caller iterates keys until parseObjectKey returns
/// null (i.e. `object_end`).
pub fn enterObject(scanner: *std.json.Scanner) !bool {
    const peek = try scanner.peekNextTokenType();
    if (peek != .object_begin) {
        try scanner.skipValue();
        return false;
    }
    _ = try scanner.next();
    return true;
}

/// Read the next object key, or return null when we've hit `object_end`.
/// Returned slice is valid as long as the input buffer (and arena) is.
pub fn parseObjectKey(scanner: *std.json.Scanner, alloc: Allocator) !?[]const u8 {
    const tok = try scanner.nextAlloc(alloc, .alloc_if_needed);
    return switch (tok) {
        .object_end => null,
        .string => |s| s,
        .allocated_string => |s| s,
        else => error.UnexpectedToken,
    };
}

/// Read a string-typed value, or skip+null when the value is something
/// else (null, number, etc.). Callers use this for "give me the string
/// if it's a string, otherwise treat as missing."
pub fn parseStringValue(scanner: *std.json.Scanner, alloc: Allocator) !?[]const u8 {
    const peek = try scanner.peekNextTokenType();
    if (peek != .string) {
        try scanner.skipValue();
        return null;
    }
    const tok = try scanner.nextAlloc(alloc, .alloc_if_needed);
    return switch (tok) {
        .string, .allocated_string => |s| s,
        else => null,
    };
}

/// Read a non-negative integer value. Numbers come back as their raw
/// byte slice from Scanner; we try integer parse, then float fallback,
/// to match the behavior of the prior `ju64(obj, key)` helper.
pub fn parseU64Value(scanner: *std.json.Scanner, alloc: Allocator) !u64 {
    const peek = try scanner.peekNextTokenType();
    if (peek != .number) {
        try scanner.skipValue();
        return 0;
    }
    const tok = try scanner.nextAlloc(alloc, .alloc_if_needed);
    const slice: []const u8 = switch (tok) {
        .number, .allocated_number => |s| s,
        else => return 0,
    };
    if (std.fmt.parseInt(i64, slice, 10)) |n| {
        return if (n >= 0) @intCast(n) else 0;
    } else |_| {}
    // A scanner `.number` token is always a valid JSON number, which
    // parseFloat accepts; the same-line catch keeps the impossible-failure
    // arm off its own source line. Out-of-range numbers are treated as
    // absent per SPEC "Lenient per-field parsing"; an unguarded
    // @intFromFloat on f >= 2^64 is safety-checked illegal behavior
    // (observed panic on 1e300).
    const f = std.fmt.parseFloat(f64, slice) catch return 0;
    return if (f >= 0.0 and f < 18446744073709551616.0) @intFromFloat(f) else 0;
}

// ─── Group walking ───────────────────────────────────────────────────────────

const Pair = struct { trailing: f64, window: f64 };

fn walkGroup(alloc: Allocator, paths: []const []const u8, pc: f64, ws: f64) Pair {
    const earliest = @min(pc, ws);
    var trailing: f64 = 0.0;
    var window: f64 = 0.0;
    var seen = std.StringHashMap(void).init(alloc);
    defer {
        var it = seen.keyIterator();
        while (it.next()) |k| alloc.free(k.*);
        seen.deinit();
    }

    for (paths) |path| {
        const data = readEntireFile(alloc, path) catch continue;
        defer alloc.free(data);

        var iter = std.mem.splitScalar(u8, data, '\n');
        while (iter.next()) |line| {
            processLine(alloc, line, &seen, earliest, pc, ws, &trailing, &window);
        }
    }
    return .{ .trailing = trailing, .window = window };
}

fn processLine(
    alloc: Allocator,
    raw: []const u8,
    seen: *std.StringHashMap(void),
    earliest: f64,
    pc: f64,
    ws: f64,
    trailing: *f64,
    window: *f64,
) void {
    const line = std.mem.trim(u8, raw, " \t\r\n");
    if (line.len == 0) return;

    var scanner = std.json.Scanner.initCompleteInput(alloc, line);
    defer scanner.deinit();

    if (!(enterObject(&scanner) catch return)) return;

    var role_assistant = false;
    var id_str: ?[]const u8 = null;
    var ts_value: ?f64 = null;
    // Raw timestamp string, retained for date-aware sonnet-5 pricing. The
    // slice points into `line` or the arena; both outlive the modelCost call.
    var ts_date: []const u8 = "";
    var model: []const u8 = "";
    var inp: u64 = 0;
    var out_: u64 = 0;
    var cr: u64 = 0;
    var cw: u64 = 0;
    var web_searches: u64 = 0;

    while (true) {
        const key = (parseObjectKey(&scanner, alloc) catch return) orelse break;
        if (std.mem.eql(u8, key, "message")) {
            if (!(enterObject(&scanner) catch return)) continue;
            while (true) {
                const mkey = (parseObjectKey(&scanner, alloc) catch return) orelse break;
                if (std.mem.eql(u8, mkey, "role")) {
                    const v = parseStringValue(&scanner, alloc) catch return;
                    if (v) |s| role_assistant = std.mem.eql(u8, s, "assistant");
                } else if (std.mem.eql(u8, mkey, "id")) {
                    id_str = parseStringValue(&scanner, alloc) catch return;
                } else if (std.mem.eql(u8, mkey, "model")) {
                    const v = parseStringValue(&scanner, alloc) catch return;
                    if (v) |s| model = s;
                } else if (std.mem.eql(u8, mkey, "usage")) {
                    if (!(enterObject(&scanner) catch return)) continue;
                    while (true) {
                        const ukey = (parseObjectKey(&scanner, alloc) catch return) orelse break;
                        if (std.mem.eql(u8, ukey, "input_tokens")) {
                            inp = parseU64Value(&scanner, alloc) catch return;
                        } else if (std.mem.eql(u8, ukey, "output_tokens")) {
                            out_ = parseU64Value(&scanner, alloc) catch return;
                        } else if (std.mem.eql(u8, ukey, "cache_read_input_tokens")) {
                            cr = parseU64Value(&scanner, alloc) catch return;
                        } else if (std.mem.eql(u8, ukey, "cache_creation_input_tokens")) {
                            cw = parseU64Value(&scanner, alloc) catch return;
                        } else if (std.mem.eql(u8, ukey, "server_tool_use")) {
                            // Nested object: descend for web_search_requests.
                            if (enterObject(&scanner) catch return) {
                                while (true) {
                                    const skey = (parseObjectKey(&scanner, alloc) catch return) orelse break;
                                    if (std.mem.eql(u8, skey, "web_search_requests")) {
                                        web_searches = parseU64Value(&scanner, alloc) catch return;
                                    } else {
                                        scanner.skipValue() catch return;
                                    }
                                }
                            }
                        } else {
                            scanner.skipValue() catch return;
                        }
                    }
                } else {
                    scanner.skipValue() catch return;
                }
            }
        } else if (std.mem.eql(u8, key, "timestamp")) {
            const v = parseStringValue(&scanner, alloc) catch return;
            if (v) |s| {
                ts_value = parseTs(s) catch null;
                ts_date = s;
            }
        } else {
            scanner.skipValue() catch return;
        }
    }

    if (!role_assistant) return;

    if (id_str) |id| {
        if (id.len > 0) {
            if (seen.contains(id)) return;
            // alloc is a worker arena: on a put failure the duped key is
            // reclaimed at arena deinit, so no manual free is needed.
            const k = alloc.dupe(u8, id) catch return;
            seen.put(k, {}) catch return;
        }
    }

    const ts = ts_value orelse return;
    if (ts < earliest) return;

    const c = modelCost(inp, out_, cr, cw, web_searches, model, ts_date);
    if (ts >= pc) trailing.* += c;
    if (ts >= ws) window.* += c;
}

// ─── Directory discovery ─────────────────────────────────────────────────────

pub const FileMap = std.StringHashMap(std.ArrayList([]const u8));
pub const PATH_SEP = if (is_windows) '\\' else '/';

fn addFile(alloc: Allocator, map: *FileMap, slug: []const u8, sid: []const u8, path: []const u8) !void {
    const key = try std.fmt.allocPrint(alloc, "{s}\x00{s}", .{ slug, sid });
    const gop = try map.getOrPut(key);
    if (!gop.found_existing) {
        gop.value_ptr.* = .empty;
    } else alloc.free(key);
    try gop.value_ptr.*.append(alloc, path);
}

/// Resolve the user's home directory. On Windows, USERPROFILE is canonical
/// (HOME is often unset, or a git-bash POSIX path like /c/Users/...), so
/// prefer it and fall back to HOME; elsewhere prefer HOME, fall back to
/// USERPROFILE. Caller frees the returned slice.
pub fn homeDir(alloc: Allocator) ?[]const u8 {
    const primary = if (is_windows) "USERPROFILE" else "HOME";
    const secondary = if (is_windows) "HOME" else "USERPROFILE";
    if (getEnvVar(alloc, primary)) |home| return home;
    return getEnvVar(alloc, secondary);
}

pub fn defaultRoot(alloc: Allocator) ![]const u8 {
    if (homeDir(alloc)) |home| {
        defer alloc.free(home);
        return std.fmt.allocPrint(alloc, "{s}{c}.claude{c}projects", .{ home, PATH_SEP, PATH_SEP });
    }
    return alloc.dupe(u8, ".claude/projects");
}

/// Multi-root discovery: walk every resolved root and merge entries into
/// one (slug,sid) -> [paths] map. Groups from different roots that share a
/// (slug,sid) are merged (seen_ids dedup at walk_group time handles
/// duplicate entries).
pub fn discover(alloc: Allocator, roots: []const []const u8, earliest: f64) !FileMap {
    var map = FileMap.init(alloc);
    for (roots) |root_path| {
        var per_root = if (is_windows)
            discoverWindows(alloc, root_path, earliest) catch continue
        else if (is_darwin)
            discoverDarwin(alloc, root_path, earliest) catch continue
        else
            discoverLinux(alloc, root_path, earliest) catch continue;
        defer per_root.deinit();

        var it = per_root.iterator();
        while (it.next()) |kv| {
            // Need to give the merged map ownership of a fresh key string so
            // it remains valid after per_root deinits its keys.
            const new_key = try alloc.dupe(u8, kv.key_ptr.*);
            // alloc is arena-backed: when the slug already exists the
            // duplicate key is reclaimed at arena deinit, no manual free.
            const gop = try map.getOrPut(new_key);
            if (!gop.found_existing) {
                gop.value_ptr.* = .empty;
            }
            try gop.value_ptr.*.appendSlice(alloc, kv.value_ptr.*.items);
        }
    }
    return map;
}

/// Single-root discovery (used by both the multi-root wrapper above and by
/// callers in beacons.zig that want explicit single-root semantics for
/// some operations).
pub fn discoverOneRoot(alloc: Allocator, root_path: []const u8, earliest: f64) !FileMap {
    if (is_windows) {
        return discoverWindows(alloc, root_path, earliest);
    } else if (is_darwin) {
        return discoverDarwin(alloc, root_path, earliest);
    } else {
        return discoverLinux(alloc, root_path, earliest);
    }
}

// Darwin directory walk uses libc opendir/readdir/closedir.
// Mirrors the structure of discoverLinux below.
fn discoverDarwin(alloc: Allocator, root_path: []const u8, earliest: f64) !FileMap {
    var map = FileMap.init(alloc);
    const root_z = try alloc.dupeZ(u8, root_path);
    defer alloc.free(root_z);
    const root_dir = std.c.opendir(root_z) orelse return map;
    defer _ = std.c.closedir(root_dir);

    while (std.c.readdir(root_dir)) |ent| {
        if (ent.type != std.c.DT.DIR) continue;
        const name_ptr: [*:0]const u8 = @ptrCast(&ent.name);
        const slug = std.mem.span(name_ptr);
        if (slug.len == 0) continue;
        if (slug[0] == '.' and (slug.len == 1 or (slug.len == 2 and slug[1] == '.'))) continue;

        const slug_dir = try std.fmt.allocPrint(alloc, "{s}/{s}", .{ root_path, slug });
        try scanSlugDirDarwin(alloc, &map, slug, slug_dir, earliest);
    }
    return map;
}

fn scanSlugDirDarwin(alloc: Allocator, map: *FileMap, slug: []const u8, slug_dir: []const u8, earliest: f64) !void {
    const slug_z = try alloc.dupeZ(u8, slug_dir);
    defer alloc.free(slug_z);
    const dir = std.c.opendir(slug_z) orelse return;
    defer _ = std.c.closedir(dir);

    while (std.c.readdir(dir)) |ent| {
        const name_ptr: [*:0]const u8 = @ptrCast(&ent.name);
        const name = std.mem.span(name_ptr);
        if (name.len == 0) continue;
        if (name[0] == '.' and (name.len == 1 or (name.len == 2 and name[1] == '.'))) continue;

        if (ent.type == std.c.DT.REG or ent.type == std.c.DT.UNKNOWN) {
            if (std.mem.endsWith(u8, name, ".jsonl")) {
                const sid = name[0 .. name.len - 6];
                const path = try std.fmt.allocPrint(alloc, "{s}/{s}", .{ slug_dir, name });
                if (!mtimeOk(alloc, path, earliest)) {
                    alloc.free(path);
                    continue;
                }
                try addFile(alloc, map, slug, sid, path);
            }
        } else if (ent.type == std.c.DT.DIR) {
            const subagents_dir = try std.fmt.allocPrint(alloc, "{s}/{s}/subagents", .{ slug_dir, name });
            try scanSubagentsDarwin(alloc, map, slug, name, subagents_dir, earliest);
        }
    }
}

fn scanSubagentsDarwin(alloc: Allocator, map: *FileMap, slug: []const u8, sid: []const u8, subagents_dir: []const u8, earliest: f64) !void {
    const sub_z = try alloc.dupeZ(u8, subagents_dir);
    defer alloc.free(sub_z);
    const dir = std.c.opendir(sub_z) orelse return;
    defer _ = std.c.closedir(dir);

    while (std.c.readdir(dir)) |ent| {
        if (ent.type != std.c.DT.REG and ent.type != std.c.DT.UNKNOWN) continue;
        const name_ptr: [*:0]const u8 = @ptrCast(&ent.name);
        const aname = std.mem.span(name_ptr);
        if (!std.mem.startsWith(u8, aname, "agent-")) continue;
        if (!std.mem.endsWith(u8, aname, ".jsonl")) continue;

        const path = try std.fmt.allocPrint(alloc, "{s}/{s}", .{ subagents_dir, aname });
        if (!mtimeOk(alloc, path, earliest)) {
            alloc.free(path);
            continue;
        }
        try addFile(alloc, map, slug, sid, path);
    }
}

fn discoverLinux(alloc: Allocator, root_path: []const u8, earliest: f64) !FileMap {
    const linux = platform.linux;
    var map = FileMap.init(alloc);

    const root_z = try alloc.dupeZ(u8, root_path);
    defer alloc.free(root_z);
    const root_fd_ret = linux.openat(linux.AT.FDCWD, root_z, .{ .DIRECTORY = true }, 0);
    const root_fd: i32 = @bitCast(@as(u32, @truncate(root_fd_ret)));
    if (root_fd < 0) return map;
    defer _ = linux.close(root_fd);

    // Iterate slug directories
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

            const slug_dir = try std.fmt.allocPrint(alloc, "{s}/{s}", .{ root_path, slug });

            // Scan for parent .jsonl files and session dirs
            try scanSlugDir(alloc, &map, slug, slug_dir, earliest);
        }
    }
    return map;
}

fn scanSlugDir(alloc: Allocator, map: *FileMap, slug: []const u8, slug_dir: []const u8, earliest: f64) !void {
    const linux = platform.linux;
    const slug_z = try alloc.dupeZ(u8, slug_dir);
    defer alloc.free(slug_z);
    const slug_fd: i32 = @bitCast(@as(u32, @truncate(linux.openat(linux.AT.FDCWD, slug_z, .{ .DIRECTORY = true }, 0))));
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
            if (name.len == 0) continue;
            if (name[0] == '.' and (name.len == 1 or (name.len == 2 and name[1] == '.'))) continue;

            if (entry.type == linux.DT.REG or entry.type == linux.DT.UNKNOWN) {
                if (std.mem.endsWith(u8, name, ".jsonl")) {
                    const sid = name[0 .. name.len - 6];
                    const path = try std.fmt.allocPrint(alloc, "{s}/{s}", .{ slug_dir, name });
                    if (!mtimeOk(alloc, path, earliest)) {
                        alloc.free(path);
                        continue;
                    }
                    try addFile(alloc, map, slug, sid, path);
                }
            } else if (entry.type == linux.DT.DIR) {
                const sid = name;
                const subagents_dir = try std.fmt.allocPrint(alloc, "{s}/{s}/subagents", .{ slug_dir, sid });
                try scanSubagents(alloc, map, slug, sid, subagents_dir, earliest);
            }
        }
    }
}

fn scanSubagents(alloc: Allocator, map: *FileMap, slug: []const u8, sid: []const u8, subagents_dir: []const u8, earliest: f64) !void {
    const linux = platform.linux;
    const sub_z = try alloc.dupeZ(u8, subagents_dir);
    defer alloc.free(sub_z);
    const sub_fd: i32 = @bitCast(@as(u32, @truncate(linux.openat(linux.AT.FDCWD, sub_z, .{ .DIRECTORY = true }, 0))));
    if (sub_fd < 0) return;
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

            if (entry.type != linux.DT.REG and entry.type != linux.DT.UNKNOWN) {
                continue;
            }
            const name_ptr: [*:0]const u8 = @ptrCast(&entry.name);
            const aname = std.mem.span(name_ptr);
            if (!std.mem.startsWith(u8, aname, "agent-")) continue;
            if (!std.mem.endsWith(u8, aname, ".jsonl")) continue;

            const path = try std.fmt.allocPrint(alloc, "{s}/{s}", .{ subagents_dir, aname });
            if (!mtimeOk(alloc, path, earliest)) {
                alloc.free(path);
                continue;
            }
            try addFile(alloc, map, slug, sid, path);
        }
    }
}

fn discoverWindows(alloc: Allocator, root_path: []const u8, earliest: f64) !FileMap {
    var map = FileMap.init(alloc);

    const slug_pattern = try std.fmt.allocPrint(alloc, "{s}\\*", .{root_path});
    const slug_entries = try findFilesWin(alloc, slug_pattern);

    for (slug_entries.items) |se| {
        if (se.dwFileAttributes & platform.FILE_ATTRIBUTE_DIRECTORY == 0) continue;
        const slug_w = std.mem.span(@as([*:0]const u16, @ptrCast(&se.cFileName)));
        if (slug_w.len == 0) continue;
        if (slug_w.len == 1 and slug_w[0] == '.') continue;
        if (slug_w.len == 2 and slug_w[0] == '.' and slug_w[1] == '.') continue;
        const slug = try std.unicode.utf16LeToUtf8Alloc(alloc, slug_w);
        const slug_dir = try std.fmt.allocPrint(alloc, "{s}\\{s}", .{ root_path, slug });

        {
            const pat = try std.fmt.allocPrint(alloc, "{s}\\*.jsonl", .{slug_dir});
            const entries = try findFilesWin(alloc, pat);
            for (entries.items) |e| {
                if (e.dwFileAttributes & platform.FILE_ATTRIBUTE_DIRECTORY != 0) continue;
                const name_w = std.mem.span(@as([*:0]const u16, @ptrCast(&e.cFileName)));
                const name = try std.unicode.utf16LeToUtf8Alloc(alloc, name_w);
                const sid = name[0 .. name.len - 6];
                const path = try std.fmt.allocPrint(alloc, "{s}\\{s}", .{ slug_dir, name });
                if (!mtimeOk(alloc, path, earliest)) {
                    alloc.free(path);
                    continue;
                }
                try addFile(alloc, &map, slug, sid, path);
            }
        }
        {
            const sess_pat = try std.fmt.allocPrint(alloc, "{s}\\*", .{slug_dir});
            const sess_entries = try findFilesWin(alloc, sess_pat);
            for (sess_entries.items) |se2| {
                if (se2.dwFileAttributes & platform.FILE_ATTRIBUTE_DIRECTORY == 0) continue;
                const sid_w = std.mem.span(@as([*:0]const u16, @ptrCast(&se2.cFileName)));
                if (sid_w.len == 0) continue;
                if (sid_w.len == 1 and sid_w[0] == '.') continue;
                if (sid_w.len == 2 and sid_w[0] == '.' and sid_w[1] == '.') continue;
                const sid = try std.unicode.utf16LeToUtf8Alloc(alloc, sid_w);
                const sub_pat = try std.fmt.allocPrint(alloc, "{s}\\{s}\\subagents\\agent-*.jsonl", .{ slug_dir, sid });
                const sub_entries = try findFilesWin(alloc, sub_pat);
                for (sub_entries.items) |ae| {
                    if (ae.dwFileAttributes & platform.FILE_ATTRIBUTE_DIRECTORY != 0) continue;
                    const aname_w = std.mem.span(@as([*:0]const u16, @ptrCast(&ae.cFileName)));
                    const aname = try std.unicode.utf16LeToUtf8Alloc(alloc, aname_w);
                    const path = try std.fmt.allocPrint(alloc, "{s}\\{s}\\subagents\\{s}", .{ slug_dir, sid, aname });
                    if (!mtimeOk(alloc, path, earliest)) {
                        alloc.free(path);
                        continue;
                    }
                    try addFile(alloc, &map, slug, sid, path);
                }
            }
        }
    }
    return map;
}

fn findFilesWin(alloc: Allocator, pat: []const u8) !std.ArrayList(platform.WIN32_FIND_DATAW) {
    var list: std.ArrayList(platform.WIN32_FIND_DATAW) = .empty;
    if (!is_windows) return list;
    const wpat = try std.unicode.utf8ToUtf16LeAllocZ(alloc, pat);
    var fd: platform.WIN32_FIND_DATAW = undefined;
    const h = platform.FindFirstFileW(wpat.ptr, &fd) orelse return list;
    if (h == platform.INVALID_HANDLE_VALUE) return list;
    defer _ = platform.FindClose(h);
    while (true) {
        try list.append(alloc, fd);
        if (platform.FindNextFileW(h, &fd) == 0) break;
    }
    return list;
}

// ─── Worker pool ─────────────────────────────────────────────────────────────

const Spinlock = struct {
    state: std.atomic.Value(u32) = .init(0),

    fn lock(self: *Spinlock) void {
        while (self.state.cmpxchgWeak(0, 1, .acquire, .monotonic) != null) {
            std.atomic.spinLoopHint();
        }
    }
    fn unlock(self: *Spinlock) void {
        self.state.store(0, .release);
    }
};

const Accum = struct {
    trailing: f64 = 0.0,
    window: f64 = 0.0,
    spin: Spinlock = .{},
    fn add(self: *Accum, p: Pair) void {
        self.spin.lock();
        defer self.spin.unlock();
        self.trailing += p.trailing;
        self.window += p.window;
    }
};

const Queue = struct {
    items: []const []const []const u8,
    cur: std.atomic.Value(usize),
    fn init(items: []const []const []const u8) Queue {
        return .{ .items = items, .cur = .init(0) };
    }
    fn pop(self: *Queue) ?[]const []const u8 {
        const i = self.cur.fetchAdd(1, .seq_cst);
        return if (i < self.items.len) self.items[i] else null;
    }
};

const Wctx = struct { q: *Queue, acc: *Accum, pc: f64, ws: f64 };

fn doWork(ctx: Wctx) void {
    var arena = std.heap.ArenaAllocator.init(std.heap.page_allocator);
    defer arena.deinit();
    while (ctx.q.pop()) |paths| {
        _ = arena.reset(.retain_capacity);
        ctx.acc.add(walkGroup(arena.allocator(), paths, ctx.pc, ctx.ws));
    }
}

// ─── main ────────────────────────────────────────────────────────────────────

pub fn main() !void {
    const gpa = std.heap.smp_allocator;
    var dispatch_arena = std.heap.ArenaAllocator.init(gpa);
    defer dispatch_arena.deinit();
    const dispatch_alloc = dispatch_arena.allocator();

    const argv = try getArgs(dispatch_alloc);
    if (wantsHelp(argv)) {
        writeStdout(HELP);
        std.process.exit(0);
    }
    // Subcommand routing: "cost", "beacons-latest", "beacons-history" route
    // to the matching impl. Bare flag invocation (first arg starts with "-")
    // or no args at all routes to cost mode for back-compat.
    const subcommand: []const u8 = blk: {
        if (argv.len == 0) break :blk "cost";
        const first = argv[0];
        if (std.mem.eql(u8, first, "cost")) break :blk "cost";
        if (std.mem.eql(u8, first, "beacons-latest")) break :blk "beacons-latest";
        if (std.mem.eql(u8, first, "beacons-history")) break :blk "beacons-history";
        if (std.mem.eql(u8, first, "search")) break :blk "search";
        if (std.mem.eql(u8, first, "events")) break :blk "events";
        if (first.len > 0 and first[0] == '-') break :blk "cost";
        std.debug.print("walker: unknown subcommand: {s}\n", .{first});
        usagePointer();
        std.process.exit(2);
    };
    const rest: [][]const u8 = if (std.mem.eql(u8, subcommand, "cost") and (argv.len == 0 or argv[0].len == 0 or argv[0][0] == '-'))
        argv
    else
        argv[1..];

    if (std.mem.eql(u8, subcommand, "beacons-latest")) {
        return beacons.runLatest(gpa, rest);
    }
    if (std.mem.eql(u8, subcommand, "beacons-history")) {
        return beacons.runHistory(gpa, rest);
    }
    if (std.mem.eql(u8, subcommand, "search")) {
        return search.run(gpa, rest);
    }
    if (std.mem.eql(u8, subcommand, "events")) {
        return events.run(gpa, rest);
    }
    return runCost(gpa, rest);
}

fn runCost(gpa: Allocator, args: [][]const u8) !void {
    const t0 = perfNow();
    const frq = perfFreq();

    var arena = std.heap.ArenaAllocator.init(gpa);
    defer arena.deinit();
    const alloc = arena.allocator();

    const cli = try parseCli(alloc, args);
    const now: f64 = cli.now orelse nowUnix();
    const pc = now - @as(f64, @floatFromInt(cli.period));
    const earliest = @min(pc, cli.win_start);

    const primary = if (cli.root) |r| try alloc.dupe(u8, r) else try defaultRoot(alloc);
    const roots = try walker_roots.resolveRoots(alloc, primary, cli.extra_roots, cli.read_config);

    var grp_map = try discover(alloc, roots, earliest);

    const ngroups = grp_map.count();
    var nfiles: usize = 0;
    var grp_list: std.ArrayList([]const []const u8) = .empty;
    var vi = grp_map.valueIterator();
    while (vi.next()) |list| {
        nfiles += list.items.len;
        try grp_list.append(alloc, list.items);
    }

    var queue = Queue.init(grp_list.items);
    var accum = Accum{};

    const ncpu = std.Thread.getCpuCount() catch 4;
    const nw = @min(8, ncpu);
    var thr: std.ArrayList(std.Thread) = .empty;
    const wctx = Wctx{ .q = &queue, .acc = &accum, .pc = pc, .ws = cli.win_start };
    var k: usize = 0;
    while (k < nw) : (k += 1) try thr.append(alloc, try std.Thread.spawn(.{}, doWork, .{wctx}));
    for (thr.items) |th| th.join();

    const elapsed_ms: u64 = @intCast(@divTrunc((perfNow() - t0) * 1000, frq));

    var out_buf: [256]u8 = undefined;
    const out_str = std.fmt.bufPrint(&out_buf, "{{\"trailing_usd\":{d:.6},\"window_usd\":{d:.6},\"files_walked\":{d},\"groups\":{d},\"elapsed_ms\":{d}}}\n", .{ accum.trailing, accum.window, nfiles, ngroups, elapsed_ms }) catch "{}";
    writeStdout(out_str);
}
