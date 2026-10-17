"""Pahole version facts taken from the executable's compilation metadata."""

load("@rules_cc//cc/common:cc_info.bzl", "CcInfo")

KernelPaholeInfo = provider(
    "One pahole executable and the version used to compile it.",
    fields = ["executable", "files_to_run", "files", "runfiles", "version", "version_text"],
)

def _impl(ctx):
    binary = ctx.attr.binary
    definitions = binary[CcInfo].compilation_context.local_defines.to_list()
    versions = {}
    for name in ["DWARVES_MAJOR_VERSION", "DWARVES_MINOR_VERSION"]:
        values = [value.removeprefix(name + "=") for value in definitions if value.startswith(name + "=")]
        if len(values) != 1:
            fail("Pahole %s must define exactly one %s in its C compilation metadata, got %s" % (binary.label, name, values))
        versions[name] = int(values[0])
    major = versions["DWARVES_MAJOR_VERSION"]
    minor = versions["DWARVES_MINOR_VERSION"]
    if major < 0 or minor < 0 or minor > 99:
        fail("Pahole version cannot be represented by Kconfig: %s.%s" % (major, minor))
    info = binary[DefaultInfo]
    return [KernelPaholeInfo(
        executable = ctx.executable.binary,
        files_to_run = info.files_to_run,
        files = depset(transitive = [info.files, info.default_runfiles.files]),
        runfiles = info.default_runfiles,
        version = major * 100 + minor,
        version_text = "v%s.%s" % (major, minor),
    )]

kernel_pahole = rule(
    implementation = _impl,
    attrs = {
        "binary": attr.label(mandatory = True, providers = [CcInfo], executable = True, cfg = "exec"),
    },
)

def _test_impl(ctx):
    tool = ctx.attr.pahole[KernelPaholeInfo]
    script = ctx.actions.declare_file(ctx.label.name + ".sh")
    ctx.actions.write(script, """#!/bin/sh
set -eu
actual=$("$TEST_SRCDIR/$TEST_WORKSPACE/pahole" --version)
if test "$actual" != "%s"; then
    printf 'Pahole version differs from compilation metadata: expected %s, got %%s\\n' "$actual" >&2
    exit 1
fi
""" % (tool.version_text, tool.version_text), is_executable = True)
    return [DefaultInfo(executable = script, runfiles = ctx.runfiles(symlinks = {"pahole": tool.executable}).merge(tool.runfiles))]

kernel_pahole_test = rule(
    implementation = _test_impl,
    test = True,
    attrs = {"pahole": attr.label(mandatory = True, providers = [KernelPaholeInfo])},
)
