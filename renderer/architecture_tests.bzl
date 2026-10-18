"""Analysis tests for one symbolic export across configured platforms."""

load("@bazel_skylib//lib:unittest.bzl", "analysistest", "asserts", "unittest")
load(":architecture.bzl", "KernelArchitectureInfo")
load(":compiler_capabilities.bzl", "KernelCompilerCapabilityObjectsInfo", "kernel_compiler_capabilities_check")
load(":expressions.bzl", "evaluate", "program")
load(":image.bzl", "kbuild_resolved_build")
load(":multi_platform.bzl", "KernelPlatformsInfo", "SUPPORTED_KERNEL_CPUS", "kernel_multi_platform")
load(":rules.bzl", "KernelConfigInfo", "kernel_config")
load(":toolchain.bzl", "KernelToolchainInfo")

SnapshotInfo = provider(doc = "Snapshot Info.", fields = ["architecture", "values", "flags"])
_ACTIVE = str(Label("//:active_config"))

# Bazel callback signatures include inputs this implementation does not use.
# buildifier: disable=unused-variable
def _select(settings, attr):
    return {_ACTIVE: str(attr.config)}

_transition = transition(implementation = _select, inputs = [], outputs = [_ACTIVE])

def _snapshot_impl(ctx):
    config = ctx.attr.config[KernelConfigInfo]
    setup = ctx.attr._setup[KernelToolchainInfo]
    flags = {name: evaluate([{"op": "reference", "name": [{"op": "literal", "value": name}]}], setup.variables, setup.sources) for name in ["KBUILD_CFLAGS", "KBUILD_CPPFLAGS", "LINUXINCLUDE", "LDFLAGS_vmlinux"]}
    out = ctx.actions.declare_file(ctx.label.name + ".json")
    data = {"arch": config.architecture.arch, "srcarch": config.architecture.srcarch, "bits": config.architecture.bits, "values": config.values, "flags": flags, "config": config.config.path, "report": config.report.path}
    ctx.actions.write(out, json.encode(data))
    return [SnapshotInfo(architecture = config.architecture, values = config.values, flags = flags), DefaultInfo(files = depset([out, config.config, config.report]))]

snapshot = rule(implementation = _snapshot_impl, cfg = _transition, attrs = {
    "config": attr.label(providers = [KernelConfigInfo]),
    "_setup": attr.label(default = "//:toolchain", providers = [KernelToolchainInfo]),
    "_allowlist_function_transition": attr.label(default = "@bazel_tools//tools/allowlists/function_transition_allowlist"),
})

def _check_impl(ctx):
    env = analysistest.begin(ctx)
    target = analysistest.target_under_test(env)
    info = target[SnapshotInfo]
    asserts.equals(env, ctx.attr.arch, info.architecture.arch)
    asserts.equals(env, ctx.attr.srcarch, info.architecture.srcarch)
    asserts.equals(env, ctx.attr.bits, info.architecture.bits)
    asserts.equals(env, "y" if ctx.attr.bits == 64 else "n", info.values.get("64BIT", "n"))
    asserts.equals(env, "y", info.values.get(ctx.attr.symbol))
    asserts.true(env, "--target=" + ctx.attr.triple in info.flags["KBUILD_CPPFLAGS"], str(info.flags))
    asserts.true(env, "/arch/" + ctx.attr.srcarch + "/include" in info.flags["LINUXINCLUDE"], str(info.flags))
    asserts.true(env, ("-z max-page-size=0x200000" if ctx.attr.srcarch == "x86" else "--pic-veneer") in info.flags["LDFLAGS_vmlinux"], str(info.flags))
    for symbol in ["X86", "ARM64", "RISCV"]:
        if symbol != ctx.attr.symbol:
            asserts.equals(env, "n", info.values.get(symbol, "n"))
    return analysistest.end(env)

def _config_check_impl(ctx):
    env = analysistest.begin(ctx)
    config = analysistest.target_under_test(env)[KernelConfigInfo]
    asserts.equals(env, "riscv", config.architecture.srcarch)
    asserts.equals(env, "y", config.values.get("RISCV"))
    asserts.equals(env, "y", config.values.get("64BIT"))
    asserts.equals(env, "n", config.values.get("X86", "n"))
    return analysistest.end(env)

def _architecture_check_impl(ctx):
    env = analysistest.begin(ctx)
    info = analysistest.target_under_test(env)[KernelArchitectureInfo]
    asserts.equals(env, "i386", info.arch)
    asserts.equals(env, "x86", info.srcarch)
    asserts.equals(env, 32, info.bits)
    return analysistest.end(env)

def _failure_impl(ctx):
    env = analysistest.begin(ctx)
    asserts.expect_failure(env, ctx.attr.message)
    return analysistest.end(env)

_expected_failure_test = analysistest.make(_failure_impl, expect_failure = True, config_settings = {"//command_line_option:platforms": [str(Label("//architecture_tests:x86_64"))]}, attrs = {"message": attr.string()})

_check_x86_64_test = analysistest.make(_check_impl, config_settings = {"//command_line_option:platforms": [str(Label("//architecture_tests:x86_64"))]}, attrs = {"arch": attr.string(), "srcarch": attr.string(), "bits": attr.int(), "symbol": attr.string(), "triple": attr.string()})

_check_aarch64_test = analysistest.make(_check_impl, config_settings = {"//command_line_option:platforms": [str(Label("//architecture_tests:aarch64"))]}, attrs = {"arch": attr.string(), "srcarch": attr.string(), "bits": attr.int(), "symbol": attr.string(), "triple": attr.string()})

_check_riscv64_test = analysistest.make(_config_check_impl, config_settings = {"//command_line_option:platforms": [str(Label("//architecture_tests:riscv64"))]}, attrs = {"arch": attr.string(), "srcarch": attr.string(), "bits": attr.int(), "symbol": attr.string(), "triple": attr.string()})

_unsupported_cpu_test = analysistest.make(_failure_impl, expect_failure = True, config_settings = {"//command_line_option:platforms": [str(Label("//architecture_tests:wasm32"))]}, attrs = {"message": attr.string()})
_x86_32_test = analysistest.make(_architecture_check_impl, config_settings = {"//command_line_option:platforms": [str(Label("//architecture_tests:x86_32"))]})

def _recursive_flags_impl(ctx):
    env = unittest.begin(ctx)
    reference = [{"op": "reference", "name": [{"op": "literal", "value": "CFLAGS"}]}]
    statements = [
        {"kind": "assignment", "name_expression": [{"op": "literal", "value": "CFLAGS"}], "value_expression": [{"op": "literal", "value": "-Ilocal"}], "value": "-Ilocal", "flavor": "append"},
        {"kind": "assignment", "name_expression": [{"op": "literal", "value": "captured"}], "value_expression": reference, "value": "$(CFLAGS)", "flavor": "simple"},
    ]
    for command_line, expected in [(False, "-Icaller -Ilocal"), (True, "-Icaller")]:
        result = program(statements, {"CFLAGS": {"value": "-Icaller", "command_line": command_line}}, {}, cells = True).variables
        asserts.equals(env, expected, evaluate(reference, result, {}))
        asserts.equals(env, expected, evaluate([{"op": "reference", "name": [{"op": "literal", "value": "captured"}]}], result, {}))
    return unittest.end(env)

_recursive_flags_test = unittest.make(_recursive_flags_impl)

# Fixed package fixtures or action helpers, not target-naming macros.
# buildifier: disable=unnamed-macro
def architecture_tests():
    _recursive_flags_test(name = "recursive_flags_test")
    kernel_compiler_capabilities_check(name = "compiler_capability_checks", tags = ["manual"])
    kernel_compiler_capabilities_test(name = "selected_compiler_capabilities_test", checks = ":compiler_capability_checks")
    kernel_multi_platform(name = "all_compiler_capability_checks", target = ":compiler_capability_checks", tags = ["manual"])
    kernel_compiler_capabilities_test(name = "compiler_capabilities_test", checks = ":all_compiler_capability_checks")
    kernel_multi_platform(name = "all_configs", target = "//multi_platform:config")
    _multi_platform_test(name = "multi_platform_test", target_under_test = ":all_configs")
    native.platform(name = "x86_32", constraint_values = ["@platforms//os:linux", "@platforms//cpu:x86_32"])
    native.platform(name = "wasm32", constraint_values = ["@platforms//os:linux", "@platforms//cpu:wasm32"])
    _x86_32_test(name = "x86_32_test", target_under_test = "//:architecture")
    _unsupported_cpu_test(name = "unsupported_cpu_test", target_under_test = "//:architecture", message = "Kernel requires one supported CPU constraint")

    kernel_config(name = "config", options = "//options:options", requested = {"EXPERT": "y", "STACKPROTECTOR": "n", "UNWINDER_FRAME_POINTER": "y"})
    snapshot(name = "snapshot", config = ":config")
    kbuild_resolved_build(name = "object", config = ":config", catalog = "//:catalog", roots = ["lib/oid_registry.o"], tags = ["manual"])
    for cpu, arch, srcarch, symbol, triple in [("x86_64", "x86_64", "x86", "X86", "x86_64-linux-gnu"), ("aarch64", "arm64", "arm64", "ARM64", "aarch64-linux-gnu"), ("riscv64", "riscv", "riscv", "RISCV", "riscv64-linux-gnu")]:
        native.platform(name = cpu, constraint_values = ["@platforms//os:linux", "@platforms//cpu:" + cpu])
        check = {"x86_64": _check_x86_64_test, "aarch64": _check_aarch64_test, "riscv64": _check_riscv64_test}[cpu]
        check(name = cpu + "_test", target_under_test = ":config" if cpu == "riscv64" else ":snapshot", arch = arch, srcarch = srcarch, bits = 64, symbol = symbol, triple = triple)
    kernel_config(name = "conflicting_width", options = "//options:options", requested = {"64BIT": "n"}, tags = ["manual"])
    _expected_failure_test(name = "conflicting_width_test", target_under_test = ":conflicting_width", message = "conflicts with platform CPU")

def _multi_platform_impl(ctx):
    env = analysistest.begin(ctx)
    info = analysistest.target_under_test(env)[KernelPlatformsInfo]
    asserts.equals(env, SUPPORTED_KERNEL_CPUS, sorted(info.targets.keys()))
    outputs = {}
    for cpu, target in info.targets.items():
        config = target[KernelConfigInfo]
        asserts.equals(env, cpu, config.architecture.cpu)
        asserts.equals(env, "y" if config.architecture.bits == 64 else "n", config.values.get("64BIT", "n"))
        asserts.true(env, config.config.path not in outputs, "Configurations share an output path")
        outputs[config.config.path] = True
        asserts.true(env, config.config in info.files[cpu].to_list())
    return analysistest.end(env)

_multi_platform_test = analysistest.make(_multi_platform_impl)

def _capability_test_impl(ctx):
    targets = ctx.attr.checks[KernelPlatformsInfo].targets if KernelPlatformsInfo in ctx.attr.checks else {"selected": ctx.attr.checks}
    aliases, files, commands = {}, [], []
    for cpu, target in targets.items():
        info = target[KernelCompilerCapabilityObjectsInfo]
        nm = "compiler-capabilities/%s/nm" % cpu
        aliases[nm] = info.nm
        files.append(info.files)
        for i, check in enumerate(info.checks):
            path = "compiler-capabilities/%s/%d.o" % (cpu, i)
            aliases[path] = check.object
            commands.append('test -s "$root/%s"' % path)
            if check.required or check.excluded:
                commands.append('symbols=$("$root/%s" --undefined-only --just-symbol-name "$root/%s")' % (nm, path))
                for symbol in check.required:
                    commands.append('contains_symbol %s || { echo "Missing instrumentation symbol %s in %s" >&2; exit 1; }' % (symbol, symbol, path))
                for symbol in check.excluded:
                    commands.append('if contains_symbol %s; then echo "Unexpected instrumentation symbol %s in %s" >&2; exit 1; fi' % (symbol, symbol, path))
    script = ctx.actions.declare_file(ctx.label.name + ".sh")
    ctx.actions.write(script, '''#!/bin/sh
set -eu
root="$TEST_SRCDIR/$TEST_WORKSPACE"
contains_symbol() {
    for symbol in $symbols; do
        if [ "$symbol" = "$1" ]; then return 0; fi
    done
    return 1
}
''' + "\n".join(commands) + "\n", is_executable = True)
    return [DefaultInfo(executable = script, runfiles = ctx.runfiles(symlinks = aliases, transitive_files = depset(transitive = files)))]

kernel_compiler_capabilities_test = rule(implementation = _capability_test_impl, test = True, attrs = {
    "checks": attr.label(mandatory = True, providers = [[KernelPlatformsInfo], [KernelCompilerCapabilityObjectsInfo]]),
})
