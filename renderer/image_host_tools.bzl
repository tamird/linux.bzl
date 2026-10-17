"""Host objtool built with the selected C++ toolchain and Bazel libelf."""

load("@linux.bzl//execution_tools:defs.bzl", "EXECUTION_TOOLS_TYPE", "execution_tools", "run_recipe")
load("@rules_cc//cc:find_cc_toolchain.bzl", "use_cc_toolchain")
load("@rules_cc//cc/common:cc_common.bzl", "cc_common")
load("@rules_cc//cc/common:cc_info.bzl", "CcInfo")
load(":architecture.bzl", "KernelArchitectureInfo")
load(":commands.bzl", "shell_quote", "shell_words", "value")
load(":expressions.bzl", "evaluate", "make_words", "program", "source_index")
load(":flag_program.bzl", "FLAG_PROGRAM")
load(":host_libraries.bzl", "KernelHostLibraryInfo")
load(":host_toolchain.bzl", "KernelHostToolchainInfo")
load(":source_tree.bzl", "KernelSourceTreeInfo")

KernelObjtoolInfo = provider(doc = "Kernel Objtool Info.", fields = ["executable", "architecture"])

def _normal(path):
    parts = []
    for p in path.split("/"):
        if p == "..":
            parts.pop()
        elif p and p != ".":
            parts.append(p)
    return "/".join(parts)

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
    index["__host_capabilities__"] = {}
    for library in libraries:
        index["__host_capabilities__"].update(library[KernelHostLibraryInfo].probe_answers)
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
