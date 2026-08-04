const std = @import("std");

pub fn build(b: *std.Build) void {
    const target = b.standardTargetOptions(.{});
    // Default to ReleaseFast: walker is a perf-comparison tool benched against
    // Release C++/Rust/Go binaries, so a bare `zig build` must produce an
    // optimized binary (a Debug build is 3-9x slower and made Zig look like the
    // slowest impl when it is actually the fastest). We read -Doptimize directly
    // rather than via standardOptimizeOption, whose preferred_optimize_mode only
    // takes effect behind an explicit -Drelease flag and still defaults a bare
    // build to Debug. Override with `-Doptimize=Debug` (shared/coverage.py does
    // this for kcov line mapping).
    const optimize = b.option(std.builtin.OptimizeMode, "optimize", "Optimization mode (default: ReleaseFast)") orelse .ReleaseFast;

    const root_module = b.createModule(.{
        .root_source_file = b.path("src/main.zig"),
        .target = target,
        .optimize = optimize,
        // Darwin has no stable syscall ABI; libSystem is the supported
        // interface, so the macOS code path goes through std.c.
        .link_libc = target.result.os.tag == .macos,
    });
    const search_mod = b.createModule(.{
        .root_source_file = b.path("src/search.zig"),
    });
    root_module.addImport("search", search_mod);

    const exe = b.addExecutable(.{
        .name = "walker",
        .root_module = root_module,
    });

    // Coverage builds force the LLVM backend. Zig 0.16's default self-hosted
    // x86_64 backend emits DWARF that kcov cannot parse (it sees compiler_rt
    // but not our main module); LLVM-emitted DWARF is kcov-readable. Production
    // builds stay on the faster self-hosted backend. Driven by
    // shared/coverage.py via `zig build -Dcoverage=true -Doptimize=Debug`.
    const coverage = b.option(bool, "coverage", "Force LLVM backend so kcov can read DWARF (coverage builds only)") orelse false;
    if (coverage) exe.use_llvm = true;

    b.installArtifact(exe);

    const run_cmd = b.addRunArtifact(exe);
    run_cmd.step.dependOn(b.getInstallStep());
    if (b.args) |args| {
        run_cmd.addArgs(args);
    }

    const run_step = b.step("run", "Run the walker");
    run_step.dependOn(&run_cmd.step);

    const unit_tests = b.addTest(.{
        .root_module = root_module,
    });
    const run_unit_tests = b.addRunArtifact(unit_tests);
    const test_step = b.step("test", "Run unit tests");
    test_step.dependOn(&run_unit_tests.step);
}
