"""Build pristine native conf and compare its output with the prototype."""

load("@kernel//rules:expressions.bzl", "evaluate")
load("@kernel//rules:rules.bzl", "KernelConfigInfo")
load("@kernel//rules:toolchain.bzl", "KernelToolchainInfo")
load("@linux.bzl//execution_tools:defs.bzl", "EXECUTION_TOOLS_TYPE", "execution_tools", "run_recipe")
load("@rules_cc//cc:find_cc_toolchain.bzl", "find_cc_toolchain", "use_cc_toolchain")
load("@rules_cc//cc/common:cc_common.bzl", "cc_common")

def _quote(value):
    return "'" + value.replace("'", "'\\''") + "'"

def _native_conf_impl(ctx):
    execution = execution_tools(ctx)
    parser = ctx.actions.declare_file(ctx.label.name + ".parsers/parser.tab.c")
    header = ctx.actions.declare_file(ctx.label.name + ".parsers/parser.tab.h")
    lexer = ctx.actions.declare_file(ctx.label.name + ".parsers/lexer.lex.c")
    run_recipe(
        ctx,
        execution,
        command = "bison -o %s --defines=%s -t -l %s\nflex -o%s -L %s" % tuple([
            _quote(file.path)
            for file in [parser, header, ctx.file.parser, lexer, ctx.file.lexer]
        ]),
        inputs = [ctx.file.parser, ctx.file.lexer],
        outputs = [parser, header, lexer],
        mnemonic = "NativeKconfigParsers",
    )
    toolchain = find_cc_toolchain(ctx)
    features = cc_common.configure_features(
        ctx = ctx,
        cc_toolchain = toolchain,
        requested_features = ctx.features,
        unsupported_features = ctx.disabled_features,
    )
    _, objects = cc_common.compile(
        actions = ctx.actions,
        name = ctx.label.name,
        cc_toolchain = toolchain,
        feature_configuration = features,
        srcs = ctx.files.srcs + [parser, lexer],
        public_hdrs = ctx.files.hdrs + [header],
        includes = [ctx.file.parser.dirname, ctx.file.include_anchor.dirname, header.dirname],
    )
    linked = cc_common.link(
        actions = ctx.actions,
        name = ctx.label.name,
        cc_toolchain = toolchain,
        feature_configuration = features,
        compilation_outputs = objects,
        output_type = "executable",
        link_deps_statically = True,
    )
    return [DefaultInfo(executable = linked.executable, files = depset([linked.executable]))]

native_conf = rule(
    implementation = _native_conf_impl,
    executable = True,
    fragments = ["cpp"],
    toolchains = use_cc_toolchain() + [EXECUTION_TOOLS_TYPE],
    attrs = {
        "srcs": attr.label_list(allow_files = [".c"]),
        "hdrs": attr.label_list(allow_files = [".h"]),
        "parser": attr.label(allow_single_file = [".y"], mandatory = True),
        "lexer": attr.label(allow_single_file = [".l"], mandatory = True),
        "include_anchor": attr.label(allow_single_file = True, mandatory = True),
    },
)

_ACTIVE_CONFIG = str(Label("@kernel//:active_config"))

# Bazel supplies the transition callback signature.
# buildifier: disable=unused-variable
def _select(settings, attr):
    return {
        _ACTIVE_CONFIG: str(attr.config),
        "//command_line_option:platforms": str(attr.platform),
    }

_transition = transition(
    implementation = _select,
    inputs = [],
    outputs = [_ACTIVE_CONFIG, "//command_line_option:platforms"],
)

def _native_reference_impl(ctx):
    config = ctx.attr.config[KernelConfigInfo]
    setup = ctx.attr._setup[KernelToolchainInfo]
    if config.identity != setup.identity:
        fail("Native reference and prototype must use the same configuration/toolchain")
    execution = execution_tools(ctx)
    output = ctx.actions.declare_directory(ctx.label.name)
    spec = ctx.actions.declare_file(ctx.label.name + ".json")
    environment = {
        name: evaluate([{"op": "reference", "name": [{"op": "literal", "value": name}]}], setup.variables, setup.sources)
        for name in ["CLANG_FLAGS", "LLVM", "LLVM_IAS", "KERNELVERSION"]
    }
    environment.update({
        "ARCH": config.architecture.arch,
        "SRCARCH": config.architecture.srcarch,
        "SUBARCH": config.architecture.subarch,
    })
    ctx.actions.write(spec, json.encode({
        "conf": ctx.executable.conf.path,
        "source": ctx.file._anchor.dirname,
        "setup": setup.file.path,
        "report": config.report.path,
        "prototype_config": config.config.path,
        "environment": environment,
        "execution_bin": execution.bin_dir,
        "shell": execution.shell.path,
    }))

    # Native conf uses libc popen(), whose /bin/sh is independent of
    # CONFIG_SHELL. This reference action deliberately permits that host shell;
    # PATH, compiler tools, source files and output directories are explicit.
    ctx.actions.run(
        executable = execution.python,
        arguments = [ctx.file._driver.path, spec.path, output.path],
        inputs = depset(
            [ctx.file._driver, spec, setup.file, config.report, config.config],
            transitive = [setup.files, execution.files, ctx.attr._source_tree[DefaultInfo].files],
        ),
        tools = execution.tools + [ctx.attr.conf[DefaultInfo].files_to_run],
        outputs = [output],
        env = {"PYTHONNOUSERSITE": "1", "LC_ALL": "C", "LANG": "C"},
        use_default_shell_env = False,
        mnemonic = "NativeKconfigReference",
        progress_message = "Resolve native Kconfig for %s" % ctx.label.name,
    )
    return [DefaultInfo(files = depset([output]))]

native_config_reference = rule(
    implementation = _native_reference_impl,
    cfg = _transition,
    toolchains = [EXECUTION_TOOLS_TYPE],
    attrs = {
        "config": attr.label(mandatory = True, providers = [KernelConfigInfo]),
        "platform": attr.label(mandatory = True),
        "conf": attr.label(mandatory = True, executable = True, cfg = "exec"),
        "_setup": attr.label(default = "@kernel//:toolchain", providers = [KernelToolchainInfo]),
        "_source_tree": attr.label(default = "@kernel//:source_tree"),
        "_anchor": attr.label(default = "@kernel//:source/Makefile", allow_single_file = True),
        "_driver": attr.label(default = "//qualification:native_reference.py", allow_single_file = True),
        "_allowlist_function_transition": attr.label(default = "@bazel_tools//tools/allowlists/function_transition_allowlist"),
    },
)
