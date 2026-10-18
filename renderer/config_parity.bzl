"""Build pristine native conf and compare its output with the prototype."""

load("@linux.bzl//execution_tools:defs.bzl", "EXECUTION_TOOLS_TYPE", "execution_tools", "run_recipe")
load("@rules_cc//cc:find_cc_toolchain.bzl", "find_cc_toolchain", "use_cc_toolchain")
load("@rules_cc//cc/common:cc_common.bzl", "cc_common")
load("@rules_python//python:defs.bzl", "py_test")
load(":expressions.bzl", "evaluate", "make_words", "program")
load(":flag_program.bzl", "FLAG_PROGRAM")
load(":rules.bzl", "KernelConfigInfo")
load(":toolchain.bzl", "KernelToolchainInfo")

def _quote(value):
    return "'" + value.replace("'", "'\\''") + "'"

def _native_conf_impl(ctx):
    execution = execution_tools(ctx)
    prefix = ctx.file._anchor.dirname + "/"
    sources = {file.path.removeprefix(prefix): file for file in ctx.files._source_tree}
    kconfig = "scripts/kconfig/"
    variables = program(FLAG_PROGRAM["image_native_conf"], {}, {}).variables
    objects = make_words(evaluate([{"op": "reference", "name": [{"op": "literal", "value": "conf-objs"}]}], variables, {}))
    if not objects or len(objects) != len({obj: True for obj in objects}):
        fail("Expected a nonempty, unique native conf object inventory: %s" % objects)
    parser = ctx.actions.declare_file(ctx.label.name + ".parsers/parser.tab.c")
    header = ctx.actions.declare_file(ctx.label.name + ".parsers/parser.tab.h")
    lexer = ctx.actions.declare_file(ctx.label.name + ".parsers/lexer.lex.c")
    run_recipe(
        ctx,
        execution,
        command = "bison -o %s --defines=%s -t -l %s\nflex -o%s -L %s" % tuple([
            _quote(file.path)
            for file in [parser, header, sources[kconfig + "parser.y"], lexer, sources[kconfig + "lexer.l"]]
        ]),
        inputs = [sources[kconfig + "parser.y"], sources[kconfig + "lexer.l"]],
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
    generated = {"parser.tab.o": parser, "lexer.lex.o": lexer}
    if not all([obj in objects for obj in generated]):
        fail("Native conf must own its generated parser and lexer: %s" % objects)
    source_files = []
    for obj in objects:
        if not obj.endswith(".o"):
            fail("Unsupported native conf object: " + obj)
        source_files.append(generated[obj] if obj in generated else sources[kconfig + obj.removesuffix(".o") + ".c"])
    included = [sources[kconfig + path] for path in FLAG_PROGRAM["image_native_conf_includes"]]
    if any([file in source_files for file in included]):
        fail("Native conf includes a source that is also a separate translation unit")
    headers = [file for path, file in sources.items() if path.endswith(".h") and path.startswith((kconfig, "scripts/include/"))]
    includes = [prefix + kconfig, header.dirname]
    if any([path.startswith("scripts/include/") for path in sources]):
        includes.append(prefix + "scripts/include")
    _, objects = cc_common.compile(
        actions = ctx.actions,
        name = ctx.label.name,
        cc_toolchain = toolchain,
        feature_configuration = features,
        srcs = source_files,
        public_hdrs = headers + included + [header],
        includes = includes,
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
        "_source_tree": attr.label(default = "//:source_tree"),
        "_anchor": attr.label(default = "//:source/Makefile", allow_single_file = True),
    },
)

_ACTIVE_CONFIG = str(Label("//:active_config"))

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
    if not config.allnoconfig:
        fail("Native configuration comparison currently requires allnoconfig=True")
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
        "conf": ctx.executable._conf.path,
        "source": ctx.file._anchor.dirname,
        "setup": setup.file.path,
        "report": config.report.path,
        "prototype_config": config.config.path,
        "environment": environment,
        "execution_bin": execution.bin_dir,
        "shell": execution.shell.path,
        "pahole": config.pahole.executable.path if config.pahole else None,
    }))

    # Native conf uses libc popen(), whose /bin/sh is independent of
    # CONFIG_SHELL. This reference action deliberately permits that host shell;
    # PATH, compiler tools, source files and output directories are explicit.
    ctx.actions.run(
        executable = execution.python,
        arguments = [ctx.file._driver.path, spec.path, output.path],
        inputs = depset(
            [ctx.file._driver, spec, setup.file, config.report, config.config],
            transitive = [setup.files, execution.files, ctx.attr._source_tree[DefaultInfo].files] + ([config.pahole.files] if config.pahole else []),
        ),
        tools = execution.tools + [ctx.attr._conf[DefaultInfo].files_to_run] + ([config.pahole.files_to_run] if config.pahole else []),
        outputs = [output],
        env = {"PYTHONNOUSERSITE": "1", "LC_ALL": "C", "LANG": "C"},
        use_default_shell_env = False,
        mnemonic = "NativeKconfigReference",
        progress_message = "Resolve native Kconfig for %s" % ctx.label.name,
    )
    return [DefaultInfo(files = depset([output]))]

_native_config_reference = rule(
    implementation = _native_reference_impl,
    cfg = _transition,
    toolchains = [EXECUTION_TOOLS_TYPE],
    attrs = {
        "config": attr.label(mandatory = True, providers = [KernelConfigInfo]),
        "platform": attr.label(mandatory = True),
        "_conf": attr.label(default = "//:native_conf", executable = True, cfg = "exec"),
        "_setup": attr.label(default = "//:toolchain", providers = [KernelToolchainInfo]),
        "_source_tree": attr.label(default = "//:source_tree"),
        "_anchor": attr.label(default = "//:source/Makefile", allow_single_file = True),
        "_driver": attr.label(default = "//rules:native_reference.py", allow_single_file = True),
        "_allowlist_function_transition": attr.label(default = "@bazel_tools//tools/allowlists/function_transition_allowlist"),
    },
)

def kernel_config_parity_test(name, config, platform):
    """Compare an existing requested config with pristine native conf."""
    reference = name + "_native"
    _native_config_reference(name = reference, config = config, platform = platform, tags = ["manual"])
    py_test(
        name = name,
        srcs = [Label(":parity_test.py")],
        main = Label(":parity_test.py"),
        args = ["$(rootpath :" + reference + ")"],
        data = [":" + reference],
        env = {"PYTHONDONTWRITEBYTECODE": "1"},
    )
