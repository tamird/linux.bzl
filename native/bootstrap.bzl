"""Declared native builds and packaging for the standalone repository generator."""

load("@rules_cc//cc:find_cc_toolchain.bzl", "find_cc_toolchain", "use_cc_toolchain")
load("@rules_cc//cc/common:cc_common.bzl", "cc_common")
load("@rules_cc//cc/common:cc_info.bzl", "CcInfo")
load("@rules_python//python:defs.bzl", "py_test")

NativeParserInfo = provider(
    "Namespaced native parser objects and optional source identity metadata.",
    fields = ["compilation_outputs", "identity"],
)

def _features(ctx, toolchain):
    return cc_common.configure_features(
        ctx = ctx,
        cc_toolchain = toolchain,
        requested_features = ctx.features,
        unsupported_features = ctx.disabled_features,
    )

def _namespace(ctx, compilation_outputs, identity = None):
    inputs = depset(compilation_outputs.objects + compilation_outputs.pic_objects).to_list()
    outputs = {source: ctx.actions.declare_file(ctx.label.name + "/" + str(i) + ".o") for i, source in enumerate(inputs)}
    specification = ctx.actions.declare_file(ctx.label.name + ".namespace.json")
    ctx.actions.write(specification, json.encode({
        "nm": ctx.file._nm.path,
        "objcopy": ctx.file._objcopy.path,
        "namespace": ctx.attr.namespace,
        "symbol_prefix": "_" if ctx.target_platform_has_constraint(ctx.attr._macos[platform_common.ConstraintValueInfo]) else "",
        "objects": [[source.path, output.path] for source, output in outputs.items()],
    }))
    ctx.actions.run(
        executable = ctx.executable._builder,
        arguments = ["namespace", specification.path],
        inputs = [specification, ctx.file._nm, ctx.file._objcopy] + inputs,
        tools = [ctx.attr._builder[DefaultInfo].files_to_run],
        outputs = outputs.values(),
        mnemonic = "NamespaceNativeParser",
    )

    # The linker selects PIC or non-PIC objects from its feature configuration.
    # Namespacing must preserve both collections, including an empty collection.
    renamed = cc_common.create_compilation_outputs(
        objects = depset([outputs[source] for source in compilation_outputs.objects]),
        pic_objects = depset([outputs[source] for source in compilation_outputs.pic_objects]),
    )
    return [DefaultInfo(files = depset(outputs.values())), NativeParserInfo(compilation_outputs = renamed, identity = identity)]

def _make_parser_impl(ctx):
    original = ctx.file.original_anchor.dirname + "/"
    prepared = ctx.file.prepared_anchor.dirname + "/"
    replacements = {file.path[len(prepared):]: file for file in ctx.files.prepared}
    sources = []
    headers = list(ctx.files.exports)
    generated_includes = []
    for file in ctx.files.inputs:
        if file.is_source:
            file = replacements[file.path[len(original):]]
        else:
            generated_includes.append(file.dirname)
        if file.extension == "c":
            sources.append(file)
        else:
            headers.append(file)
    toolchain = find_cc_toolchain(ctx)
    _, objects = cc_common.compile(
        actions = ctx.actions,
        name = ctx.label.name,
        cc_toolchain = toolchain,
        feature_configuration = _features(ctx, toolchain),
        srcs = sources,
        public_hdrs = headers,
        includes = [prepared + "src", prepared + "lib", ctx.files.exports[0].dirname] + generated_includes,
        defines = ["HAVE_CONFIG_H", 'LIBDIR="/usr/local/lib"', 'INCLUDEDIR="/usr/local/include"', 'LOCALEDIR="/usr/local/share/locale"'],
        user_compile_flags = ["-std=gnu11", "-w"],
    )
    return _namespace(ctx, objects)

make_parser = rule(
    implementation = _make_parser_impl,
    fragments = ["cpp"],
    toolchains = use_cc_toolchain(),
    attrs = {
        "inputs": attr.label(default = "@make//:posix_parser_inputs"),
        "prepared": attr.label(default = "@bootstrap_make//:sources"),
        "original_anchor": attr.label(default = "@make//:MODULE.bazel", allow_single_file = True),
        "prepared_anchor": attr.label(default = "@bootstrap_make//:COPYING", allow_single_file = True),
        "exports": attr.label_list(allow_files = [".inc"]),
        "namespace": attr.string(default = "kernel_make_"),
        "_builder": attr.label(default = "//:bootstrap_builder", executable = True, cfg = "exec"),
        "_nm": attr.label(default = "@llvm//tools:llvm-nm", allow_single_file = True, cfg = "exec"),
        "_objcopy": attr.label(default = "@llvm//tools:llvm-objcopy", allow_single_file = True, cfg = "exec"),
        "_macos": attr.label(default = "@platforms//os:macos"),
    },
)

def _kconfig_parser_impl(ctx):
    directory = ctx.label.name + ".sources/"
    transformed = {name: ctx.actions.declare_file(directory + name) for name in ["parser.y", "lexer.l", "preprocess.c", "dump.c", "parser-sources.json"]}
    ctx.actions.run(
        executable = ctx.executable._prepare,
        arguments = ["--linux", ctx.file.anchor.dirname, "--out", transformed["parser.y"].dirname],
        inputs = ctx.files.sources,
        tools = [ctx.attr._prepare[DefaultInfo].files_to_run],
        outputs = transformed.values(),
        mnemonic = "PrepareKconfigParser",
    )
    parser = ctx.actions.declare_file(directory + "parser.tab.c")
    header = ctx.actions.declare_file(directory + "parser.tab.h")
    lexer = ctx.actions.declare_file(directory + "lexer.lex.c")
    bison = ctx.toolchains["@rules_bison//bison:toolchain_type"].bison_toolchain
    flex = ctx.toolchains["@rules_flex//flex:toolchain_type"].flex_toolchain
    m4 = ctx.toolchains["@rules_m4//m4:toolchain_type"].m4_toolchain
    ctx.actions.run(
        executable = bison.bison_tool,
        arguments = ["-t", "-l", "-o", parser.path, "--defines=" + header.path, transformed["parser.y"].path],
        inputs = [transformed["parser.y"]],
        tools = [ctx.executable._m4_deny_shell],
        outputs = [parser, header],
        env = dict(bison.bison_env, M4_SYSCMD_SHELL = ctx.executable._m4_deny_shell.path),
        mnemonic = "GenerateKconfigParser",
    )
    flex_env = dict(flex.flex_env)
    if "M4" not in flex_env:
        flex_env["M4"] = m4.m4_tool.executable.path
    ctx.actions.run(
        executable = flex.flex_tool,
        arguments = ["-L", "-o", lexer.path, transformed["lexer.l"].path],
        inputs = [transformed["lexer.l"]],
        tools = [ctx.executable._m4_deny_shell, m4.m4_tool],
        outputs = [lexer],
        env = dict(flex_env, M4_SYSCMD_SHELL = ctx.executable._m4_deny_shell.path),
        mnemonic = "GenerateKconfigLexer",
    )
    original = {file.basename: file for file in ctx.files.sources if file.dirname.endswith("scripts/kconfig")}
    toolchain = find_cc_toolchain(ctx)
    _, objects = cc_common.compile(
        actions = ctx.actions,
        name = ctx.label.name,
        cc_toolchain = toolchain,
        feature_configuration = _features(ctx, toolchain),
        srcs = [original[name + ".c"] for name in ["confdata", "expr", "menu", "symbol", "util"]] + [parser, lexer, transformed["preprocess.c"], transformed["dump.c"]],
        public_hdrs = [file for file in ctx.files.sources if file.extension == "h"] + [header],
        includes = [ctx.file.anchor.dirname + "/scripts/kconfig", ctx.file.anchor.dirname + "/scripts/include", header.dirname],
    )
    return _namespace(ctx, objects, transformed["parser-sources.json"])

kconfig_parser = rule(
    implementation = _kconfig_parser_impl,
    fragments = ["cpp"],
    toolchains = use_cc_toolchain() + ["@rules_bison//bison:toolchain_type", "@rules_flex//flex:toolchain_type", "@rules_m4//m4:toolchain_type"],
    attrs = {
        "sources": attr.label(default = "@bootstrap_linux//:kconfig"),
        "anchor": attr.label(default = "@bootstrap_linux//:Makefile", allow_single_file = True),
        "namespace": attr.string(default = "kernel_kconfig_"),
        "_prepare": attr.label(default = "//native:prepare", executable = True, cfg = "exec"),
        "_builder": attr.label(default = "//:bootstrap_builder", executable = True, cfg = "exec"),
        "_m4_deny_shell": attr.label(default = "@rules_bison//bison/internal:m4_deny_shell", executable = True, cfg = "exec"),
        "_nm": attr.label(default = "@llvm//tools:llvm-nm", allow_single_file = True, cfg = "exec"),
        "_objcopy": attr.label(default = "@llvm//tools:llvm-objcopy", allow_single_file = True, cfg = "exec"),
        "_macos": attr.label(default = "@platforms//os:macos"),
    },
)

def _kernel_repository_impl(ctx):
    toolchain = find_cc_toolchain(ctx)
    features = _features(ctx, toolchain)
    macos = ctx.target_platform_has_constraint(ctx.attr._macos[platform_common.ConstraintValueInfo])
    payload = ctx.actions.declare_file(ctx.label.name + ".zip")
    specification = ctx.actions.declare_file(ctx.label.name + ".payload.json")
    identity = ctx.attr.kconfig[NativeParserInfo].identity
    ctx.actions.write(specification, json.encode({
        "output": payload.path,
        "python_metadata": ctx.file.python_metadata.path,
        "make_module": ctx.file.make_module.path,
        "identity": identity.path,
        "stdlib": [file.path for file in ctx.files.stdlib],
        "renderer": [file.path for file in ctx.files.renderer],
        "license_files": [file.path for file in ctx.files.license_files],
    }))
    ctx.actions.run(
        executable = ctx.executable._builder,
        arguments = ["payload", specification.path],
        inputs = [specification, ctx.file.python_metadata, ctx.file.make_module, identity] + ctx.files.stdlib + ctx.files.renderer + ctx.files.license_files,
        tools = [ctx.attr._builder[DefaultInfo].files_to_run],
        outputs = [payload],
        mnemonic = "PackageGeneratorResources",
    )
    _, launcher = cc_common.compile(
        actions = ctx.actions,
        name = ctx.label.name,
        cc_toolchain = toolchain,
        feature_configuration = features,
        srcs = [ctx.file.launcher],
        compilation_contexts = [ctx.attr.python[CcInfo].compilation_context],
    )
    linked = cc_common.link(
        actions = ctx.actions,
        name = ctx.label.name + "_unbundled",
        cc_toolchain = toolchain,
        feature_configuration = features,
        compilation_outputs = cc_common.merge_compilation_outputs(compilation_outputs = [
            ctx.attr.make[NativeParserInfo].compilation_outputs,
            ctx.attr.kconfig[NativeParserInfo].compilation_outputs,
            launcher,
        ]),
        linking_contexts = [ctx.attr.python[CcInfo].linking_context],
        output_type = "executable",
        link_deps_statically = True,
        # Mach-O signatures cover the linked payload. Appending bytes after the
        # final link would invalidate the executable's code-signing envelope.
        user_link_flags = [
            "-Wl,-sectcreate,__TEXT,__pythonzip," + payload.path,
            # The C++ toolchain adds Foundation and libc++; this C executable
            # must retain only libraries referenced by the embedded runtime.
            "-Wl,-dead_strip_dylibs",
        ] if macos else ["-static"],
        additional_inputs = [payload] if macos else [],
    )
    output = ctx.actions.declare_file("kernel-repository")
    specification = ctx.actions.declare_file(ctx.label.name + ".bundle.json")
    ctx.actions.write(specification, json.encode({
        "binary": linked.executable.path,
        "output": output.path,
        "payload": payload.path,
    }))
    ctx.actions.run(
        executable = ctx.executable._builder,
        arguments = ["bundle", specification.path],
        inputs = [specification, linked.executable, payload],
        tools = [ctx.attr._builder[DefaultInfo].files_to_run],
        outputs = [output],
        mnemonic = "BundleKernelRepository",
    )
    return [DefaultInfo(executable = output, files = depset([output]))]

kernel_repository_binary = rule(
    implementation = _kernel_repository_impl,
    executable = True,
    fragments = ["cpp"],
    toolchains = use_cc_toolchain(),
    attrs = {
        "launcher": attr.label(allow_single_file = True, default = "//:launcher.c"),
        "make": attr.label(providers = [NativeParserInfo], mandatory = True),
        "kconfig": attr.label(providers = [NativeParserInfo], mandatory = True),
        "python": attr.label(default = "@bootstrap_python//:runtime", providers = [CcInfo]),
        "python_metadata": attr.label(default = "@bootstrap_python//:PYTHON.json", allow_single_file = True),
        "make_module": attr.label(default = "@make//:MODULE.bazel", allow_single_file = True),
        "stdlib": attr.label(default = "@bootstrap_python//:stdlib"),
        "renderer": attr.label_list(allow_files = True),
        "license_files": attr.label_list(allow_files = True),
        "_builder": attr.label(default = "//:bootstrap_builder", executable = True, cfg = "exec"),
        "_macos": attr.label(default = "@platforms//os:macos"),
    },
)

def _consumer_support_impl(ctx):
    output = ctx.actions.declare_file("consumer-support.tar.gz")
    specification = ctx.actions.declare_file(ctx.label.name + ".json")
    ctx.actions.write(specification, json.encode({
        "tool": ctx.executable.generator.path,
        "linux_anchor": ctx.file.anchor.path,
        "fragments": [file.path for file in ctx.files.fragments],
        "output": output.path,
    }))
    ctx.actions.run(
        executable = ctx.executable._builder,
        arguments = ["support", specification.path],
        inputs = depset([specification, ctx.file.anchor] + ctx.files.fragments, transitive = [ctx.attr.sources[DefaultInfo].files]),
        tools = [ctx.attr._builder[DefaultInfo].files_to_run, ctx.attr.generator[DefaultInfo].files_to_run],
        outputs = [output],
        mnemonic = "GenerateConsumerSupport",
    )
    return [DefaultInfo(files = depset([output]))]

consumer_support = rule(
    implementation = _consumer_support_impl,
    toolchains = ["//native:generator_execution_toolchain_type"],
    attrs = {
        "generator": attr.label(executable = True, cfg = "target", mandatory = True),
        "sources": attr.label(default = "@bootstrap_linux//:sources"),
        "anchor": attr.label(default = "@bootstrap_linux//:Makefile", allow_single_file = True),
        "fragments": attr.label_list(allow_files = True),
        "_builder": attr.label(default = "//:bootstrap_builder", executable = True, cfg = "exec"),
    },
)

def _generator_execution_impl(_ctx):
    return [platform_common.ToolchainInfo()]

generator_execution = rule(implementation = _generator_execution_impl)

# Each package exposes the same bootstrap targets for one kernel source set.
def _python_input(name):
    return select({
        Label("//native:linux_x86_64_musl"): "@bootstrap_python//:" + name,
        Label("//native:macos_aarch64"): "@bootstrap_python_macos//:" + name,
    }, no_match_error = "Generator builds require @llvm//platforms:linux_x86_64_musl or @llvm//platforms:macos_aarch64")

# buildifier: disable=unnamed-macro
def kernel_repository_bootstrap(source_repository):
    """Build and audit a standalone generator from the selected kernel sources."""
    anchor = source_repository + "//:Makefile"
    sources = source_repository + "//:sources"
    fragments = ["@example_fixtures//configs:" + name + ".config" for name in [
        "x86_64",
        "aarch64",
        "debug",
        "btf",
        "lz4",
    ]]
    kconfig_parser(
        name = "kconfig_parser",
        anchor = anchor,
        sources = source_repository + "//:kconfig",
        tags = ["manual"],
    )
    kernel_repository_binary(
        name = "kernel_repository",
        kconfig = ":kconfig_parser",
        python = _python_input("runtime"),
        python_metadata = _python_input("PYTHON.json"),
        stdlib = _python_input("stdlib"),
        license_files = [
            "@bootstrap_make//:COPYING",
            source_repository + "//:COPYING",
            source_repository + "//:LICENSES/preferred/GPL-2.0",
        ] + select({
            Label("//native:linux_x86_64_musl"): ["@bootstrap_python//:licenses"],
            Label("//native:macos_aarch64"): ["@bootstrap_python_macos//:licenses"],
        }),
        make = "//:make_parser",
        renderer = ["//:renderer"],
        tags = ["manual"],
    )
    consumer_support(
        name = "consumer_support",
        anchor = anchor,
        fragments = fragments,
        generator = ":kernel_repository",
        sources = sources,
        tags = ["manual"],
    )
    py_test(
        name = "repository_test",
        srcs = ["//:build.py", "//:test_repository.py"],
        args = [
            "--tool",
            "$(rootpath :kernel_repository)",
            "--linux",
            "$(rootpath " + anchor + ")",
            "--config-dir",
            "$(rootpath @example_fixtures//configs:x86_64.config)",
        ],
        data = [":kernel_repository", anchor, sources] + fragments,
        main = "//:test_repository.py",
        tags = ["manual"],
    )
