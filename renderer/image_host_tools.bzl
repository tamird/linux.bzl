"""Host objtool built with the selected C++ toolchain and Bazel libelf."""

load("@linux.bzl//execution_tools:defs.bzl", "EXECUTION_TOOLS_TYPE", "execution_tools", "run_recipe")
load("@rules_cc//cc:find_cc_toolchain.bzl", "use_cc_toolchain")
load("@rules_cc//cc/common:cc_common.bzl", "cc_common")
load("@rules_cc//cc/common:cc_info.bzl", "CcInfo")
load(":architecture.bzl", "KernelArchitectureInfo")
load(":commands.bzl", "shell_quote", "shell_words", "value")
load(":expressions.bzl", "evaluate", "make_words", "match", "program", "source_index")
load(":flag_program.bzl", "FLAG_PROGRAM")
load(":host_libraries.bzl", "KernelHostLibraryInfo")
load(":host_toolchain.bzl", "KernelHostToolchainInfo")
load(":source_tree.bzl", "KernelSourceTreeInfo")
load(":toolchain.bzl", "KernelToolchainInfo")

KernelObjtoolInfo = provider(doc = "Kernel Objtool Info.", fields = ["executable", "architecture"])
KernelResolveBtfidsInfo = provider(doc = "Source-built BTF ID resolver.", fields = ["executable"])

def _normal(path):
    parts = []
    for p in path.split("/"):
        if p == "..":
            parts.pop()
        elif p and p != ".":
            parts.append(p)
    return "/".join(parts)

def _host_capabilities(host, libraries):
    answers = {}
    for library in libraries:
        answers.update(library[KernelHostLibraryInfo].probe_answers)

    # This source query asks for compiler identity, available from the selected
    # host toolchain; it does not require guessing a compiler capability.
    answers["$(CC) -dM -E -x c /dev/null | grep -Fq \"__clang__\"; echo $$?"] = "0" if host.compiler == "clang" else "1"
    return answers

def _impl(ctx):
    architecture = ctx.attr._architecture[KernelArchitectureInfo]
    if "tools/objtool/arch/" + architecture.srcarch + "/Build" not in FLAG_PROGRAM["image_host_arch"]:
        return [KernelObjtoolInfo(executable = None, architecture = architecture), DefaultInfo(files = depset())]
    host = ctx.attr._host_toolchain[KernelHostToolchainInfo]
    host_architecture = host.architecture
    cc, features = host.cc, host.features
    prepared = ctx.attr._prepared_source[KernelSourceTreeInfo]
    source = prepared.source_root
    sources = {source + "/" + p: f for p, f in prepared.logical.items()}
    index = source_index(sources.keys())
    execution = execution_tools(ctx)
    index["__execution_tools__"] = {n: f.path for n, f in execution.commands.items()}

    # The BCR elfutils dependency supplies the modern libelf API. This is a
    # library contract, rather than an execution-time compiler/header probe.
    index["__libelf_modern_api__"] = "elf_getshdr" in ctx.attr.libelf[KernelHostLibraryInfo].apis
    libraries = [ctx.attr.libelf] + ctx.attr.optional_libraries
    index["__host_capabilities__"] = _host_capabilities(host, libraries)
    variables = program(FLAG_PROGRAM["image_utilities"], {"SRCARCH": architecture.srcarch, "HOSTARCH": host_architecture.srcarch, "srctree": source, "OUTPUT": ""}, index).variables
    variables["AWK"] = {"value": execution.commands["awk"].path}
    variables = program(FLAG_PROGRAM["image_host_objtool"], variables, index, cells = True).variables
    generated = []

    # Native Build rules describe the architecture's two generated C tables.
    probe = ctx.actions.declare_file(ctx.label.name + ".generated/.root")
    prefix = probe.dirname + "/"
    ctx.actions.write(probe, "")
    variables["OUTPUT"] = {"value": prefix}
    architecture_program = program(FLAG_PROGRAM["image_host_arch"]["tools/objtool/arch/" + architecture.srcarch + "/Build"], variables, index, cells = True)
    variables = architecture_program.variables
    for rule in architecture_program.rules:
        if rule["recipe"]:
            target = rule["targets"][0]
            output = ctx.actions.declare_file(ctx.label.name + ".generated/" + target.removeprefix(prefix))
            local = dict(variables)
            local["@"] = {"value": output.path}
            command = "\n".join([evaluate(expr, local, index) for expr in rule.get("recipes", [])])
            command = command.replace("../", source + "/tools/")
            command = command.replace(source, prepared.root.path)
            run_recipe(
                ctx,
                execution,
                command = "set -eu\nmkdir -p " + shell_quote(output.dirname) + "\n" + command,
                inputs = prepared.files,
                outputs = [output],
                mnemonic = "ObjtoolTable",
            )
            generated.append(output)
    variables["objtool-y"] = {"value": ""}
    build = program(FLAG_PROGRAM["image_host_build"], variables, index, cells = True)
    objects = [("tools/objtool/" + p, "objtool") for p in make_words(value("objtool-y", build.variables, index)) if not p.endswith("/")]

    # Each architecture Build has its own object namespace.
    arch = program(FLAG_PROGRAM["image_host_arch"]["tools/objtool/arch/" + architecture.srcarch + "/Build"], dict(variables, **{"objtool-y": {"value": ""}}), index, cells = True)
    objects.extend([("tools/objtool/arch/" + architecture.srcarch + "/" + p, "objtool") for p in make_words(value("objtool-y", arch.variables, index))])
    aliases = {}
    for rule in build.rules:
        tail = make_words(rule["prerequisites"])
        if tail and tail[0].startswith("../"):
            for target in rule["targets"]:
                aliases[_normal("tools/objtool/" + target.removeprefix(prefix))] = _normal("tools/objtool/" + tail[0])
    subcmd = program(FLAG_PROGRAM["image_host_subcmd"], variables, index, cells = True).variables
    objects.extend([("tools/lib/subcmd/" + p, "subcmd") for p in make_words(value("libsubcmd-y", subcmd, index))])
    outputs = []
    for obj, group in objects:
        src = aliases.get(obj, obj[:-2] + ".c")
        flags = shell_words(value("OBJTOOL_CFLAGS", variables, index) if group == "objtool" else value("CFLAGS", subcmd, index))
        if group == "objtool":
            flags += shell_words(value("CFLAGS_" + obj.rsplit("/", 1)[-1], arch.variables, index))

        # Preserve the toolchain's object variants for cc_common.link to select.
        _, compilation = cc_common.compile(
            actions = ctx.actions,
            name = ctx.label.name + ".objects/" + obj,
            feature_configuration = features,
            cc_toolchain = cc,
            srcs = [sources[source + "/" + src]],
            additional_inputs = generated,
            compilation_contexts = [prepared.compilation_context] + [library[CcInfo].compilation_context for library in libraries],
            user_compile_flags = flags,
        )
        outputs.append(compilation)
    linked = cc_common.link(
        actions = ctx.actions,
        name = ctx.label.name,
        feature_configuration = features,
        cc_toolchain = cc,
        compilation_outputs = cc_common.merge_compilation_outputs(compilation_outputs = outputs),
        linking_contexts = [library[CcInfo].linking_context for library in libraries],
        output_type = "executable",
        link_deps_statically = True,
        stamp = 0,
    )
    return [KernelObjtoolInfo(executable = linked.executable, architecture = architecture), DefaultInfo(files = depset([linked.executable]))]

kernel_objtool = rule(implementation = _impl, fragments = ["cpp"], toolchains = use_cc_toolchain() + [EXECUTION_TOOLS_TYPE], attrs = {
    "_architecture": attr.label(default = "//:architecture", providers = [KernelArchitectureInfo]),
    "_host_toolchain": attr.label(default = "//:host_toolchain", providers = [KernelHostToolchainInfo], cfg = "exec"),
    "libelf": attr.label(default = "//host_libraries:libelf", providers = [CcInfo, KernelHostLibraryInfo], cfg = "exec"),
    "optional_libraries": attr.label_list(providers = [CcInfo, KernelHostLibraryInfo], cfg = "exec"),
    "_prepared_source": attr.label(default = "//:prepared_source", providers = [KernelSourceTreeInfo], cfg = "exec"),
})

def _resolve_btfids_impl(ctx):
    prepared = ctx.attr._prepared_source[KernelSourceTreeInfo]
    setup = ctx.attr._setup[KernelToolchainInfo]
    host = ctx.attr._host_toolchain[KernelHostToolchainInfo]
    source = prepared.source_root
    index = source_index([source + "/" + p for p in prepared.logical])
    index["__execution_tools__"] = {n: f.path for n, f in execution_tools(ctx).commands.items()}
    index["__host_capabilities__"] = _host_capabilities(host, [ctx.attr.libelf])
    directory = "tools/bpf/resolve_btfids"
    initial = dict(setup.variables)
    for name, text in {
        "srctree": source,
        "CURDIR": source + "/" + directory,
        "OUTPUT": "",
        "HOSTARCH": setup.host_architecture.srcarch,
    }.items():
        initial[name] = {"value": text}
    variables = program(FLAG_PROGRAM["image_host_btfids"], initial, index, cells = True).variables

    # The declared libbpf exports its installed public header namespace.
    # Recover that root from its actual virtual header, not a repository path.
    libbpf = ctx.attr.libbpf[CcInfo]
    headers = [f for f in libbpf.compilation_context.headers.to_list() if f.path.endswith("/bpf/btf.h")]
    if len(headers) != 1:
        fail("Declared libbpf must expose exactly one public bpf/btf.h, got %s" % headers)
    variables["LIBBPF_INCLUDE"] = {"value": headers[0].dirname.rpartition("/")[0]}
    variables["SUBCMD_INCLUDE"] = {"value": source + "/tools/lib"}
    variables["srctree"] = {"value": source}
    build = program(FLAG_PROGRAM["image_host_btfids_build"], variables, index, cells = True)
    variables = build.variables

    # The library's recursive Make invocation forwards HOSTCFLAGS as
    # EXTRA_CFLAGS; resolve_btfids-specific flags belong to its own objects.
    library_variables = dict(variables)
    for assignment in shell_words(value("HOST_OVERRIDES", variables, index)):
        name, separator, text = assignment.partition("=")
        if not separator:
            fail("Native resolver library override is not an assignment: " + assignment)
        library_variables[name] = {"value": text}
    subcmd = program(FLAG_PROGRAM["image_host_subcmd"], library_variables, index, cells = True).variables
    objects = [(directory + "/" + obj, "resolve_btfids", variables, "host_c_flags") for obj in make_words(value("resolve_btfids-y", variables, index))]
    objects.extend([("tools/lib/subcmd/" + obj, "libsubcmd", subcmd, "c_flags") for obj in make_words(value("libsubcmd-y", subcmd, index))])
    libraries = [ctx.attr.libelf[CcInfo], libbpf]
    compilations = []
    for obj, owner, scope, flag_name in objects:
        src = obj[:-2] + ".c"
        if src not in prepared.logical:
            for rule in build.rules:
                for pattern in rule["targets"]:
                    stem = match(pattern, obj.removeprefix(directory + "/"))
                    if stem == None:
                        continue
                    prerequisites = make_words(rule["prerequisites"])
                    if prerequisites:
                        candidate = _normal(directory + "/" + prerequisites[0].replace("%", stem, 1))
                        if candidate in prepared.logical:
                            src = candidate
        if src not in prepared.logical:
            fail("No native source for resolve_btfids object " + obj)
        local = dict(scope)
        local.update({"@": {"value": obj}, "obj": {"value": owner}})
        local = program(FLAG_PROGRAM["image_host_cflags"], local, index, cells = True).variables

        # Bazel owns depfile generation and include discovery for these actions.
        flags = [f for f in shell_words(value(flag_name, local, index)) if not f.startswith(("-Wp,-MD,", "-Wp,-MMD,", "-Wp,-MT,"))]
        _, compiled = cc_common.compile(
            actions = ctx.actions,
            name = ctx.label.name + ".objects/" + obj,
            feature_configuration = setup.host_features,
            cc_toolchain = setup.host_cc,
            srcs = [prepared.logical[src]],
            compilation_contexts = [prepared.compilation_context] + [library.compilation_context for library in libraries],
            user_compile_flags = flags,
        )
        compilations.append(compiled)
    linked = cc_common.link(
        actions = ctx.actions,
        name = ctx.label.name,
        feature_configuration = setup.host_features,
        cc_toolchain = setup.host_cc,
        compilation_outputs = cc_common.merge_compilation_outputs(compilation_outputs = compilations),
        linking_contexts = [library.linking_context for library in libraries],
        user_link_flags = shell_words(value("KBUILD_HOSTLDFLAGS", variables, index)),
        output_type = "executable",
        link_deps_statically = True,
        stamp = 0,
    )
    return [KernelResolveBtfidsInfo(executable = linked.executable), DefaultInfo(files = depset([linked.executable]))]

kernel_resolve_btfids = rule(implementation = _resolve_btfids_impl, fragments = ["cpp"], toolchains = use_cc_toolchain() + [EXECUTION_TOOLS_TYPE], attrs = {
    "libelf": attr.label(default = "//host_libraries:libelf", providers = [CcInfo, KernelHostLibraryInfo], cfg = "exec"),
    "libbpf": attr.label(default = "@libbpf//:bpf", providers = [CcInfo], cfg = "exec"),
    "_setup": attr.label(default = "//:toolchain", providers = [KernelToolchainInfo]),
    "_host_toolchain": attr.label(default = "//:host_toolchain", providers = [KernelHostToolchainInfo], cfg = "exec"),
    "_prepared_source": attr.label(default = "//:prepared_source", providers = [KernelSourceTreeInfo], cfg = "exec"),
})
