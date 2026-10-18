"""Required execution checks for the selected kernel toolchain declarations."""

load("@linux.bzl//execution_tools:defs.bzl", "EXECUTION_TOOLS_TYPE", "execution_tools")
load("@rules_cc//cc:action_names.bzl", "ACTION_NAMES")
load("@rules_cc//cc:find_cc_toolchain.bzl", "find_cc_toolchain", "use_cc_toolchain")
load("@rules_cc//cc/common:cc_common.bzl", "cc_common")
load(":compiler_capabilities.bzl", "KernelCompilerCapabilitiesInfo", "KernelCompilerCapabilityVerificationInfo")
load(":compiler_capabilities_data.bzl", "COMPILER_CAPABILITY_COMMANDS")
load(":compiler_metadata.bzl", "KernelCompilerMetadataInfo")
load(":expressions.bzl", "evaluate")
load(":toolchain.bzl", "KernelToolchainInfo")

def _check_impl(ctx):
    info = ctx.attr._capabilities[KernelCompilerCapabilitiesInfo]
    metadata = ctx.attr._metadata[KernelCompilerMetadataInfo]
    cc = find_cc_toolchain(ctx)
    if cc.toolchain_id != info.toolchain_id or cc.toolchain_id != metadata.toolchain_id:
        fail("Capability checks must use the same selected C++ toolchain")
    setup = ctx.attr._setup[KernelToolchainInfo]
    features = setup.features
    variables = cc_common.create_compile_variables(cc_toolchain = cc, feature_configuration = features)
    flags = cc_common.get_memory_inefficient_command_line(feature_configuration = features, action_name = ACTION_NAMES.c_compile, variables = variables)
    if setup.cc.toolchain_id != cc.toolchain_id:
        fail("Capability verification and native setup must use the same selected toolchain")
    execution = execution_tools(ctx)
    objects, checks, native = [], [], []
    for i, fact in enumerate(info.checks):
        if not fact.get("kbuild"):
            native.append({"input": fact["input"], "commands": COMPILER_CAPABILITY_COMMANDS[fact["input"]], "execution": fact.get("execution")})
            continue
        source = ctx.actions.declare_file(ctx.label.name + ".checks/%d.S" % i)
        out = ctx.actions.declare_file(ctx.label.name + ".checks/%d.o" % i)
        ctx.actions.write(source, fact.get("source", fact["argument"].replace("\\n", "\n").replace("\\$", "$")))
        ctx.actions.run(
            executable = cc_common.get_tool_for_action(feature_configuration = features, action_name = ACTION_NAMES.c_compile),
            arguments = flags + fact.get("flags", []) + ["-c", "-x", fact.get("language", "assembler"), source.path, "-o", out.path],
            inputs = depset([source], transitive = [cc.all_files, metadata.compile_files]),
            outputs = [out],
            mnemonic = "KernelCapabilityCheck",
        )
        objects.append(out)
        checks.append(struct(object = out, required = fact.get("undefined_symbols", []), excluded = fact.get("excluded_undefined_symbols", [])))
    scripts = {}
    source_root = ctx.file._anchor.dirname + "/"
    source_files = {file.path[len(source_root):]: file for file in ctx.attr._sources[DefaultInfo].files.to_list() if file.path.startswith(source_root)}
    for fact in info.checks:
        for logical in fact.get("scripts", []):
            if logical not in source_files:
                fail("Native capability requires declared source " + logical)
            scripts[logical] = source_files[logical]
    spec = ctx.actions.declare_file(ctx.label.name + ".json")
    verified = ctx.actions.declare_file(ctx.label.name + ".verified")
    report = ctx.actions.declare_file(ctx.label.name + ".report.json")
    tools = {role: metadata.tools[role] for role in ["CC", "LD", "NM", "OBJCOPY"]}
    tools["AR"] = cc_common.get_tool_for_action(feature_configuration = features, action_name = ACTION_NAMES.cpp_link_static_library)
    ctx.actions.write(spec, json.encode({
        "toolchain": cc.toolchain_id,
        "target": setup.architecture.cpu,
        "tools": tools,
        "compile_flags": flags,
        "clang_flags": evaluate([{"op": "reference", "name": [{"op": "literal", "value": "CLANG_FLAGS"}]}], setup.variables, setup.sources),
        "shell": execution.shell.path,
        "execution_bin": execution.bin_dir,
        "scripts": {logical: file.path for logical, file in scripts.items()},
        "checks": native,
        "objects": [{"object": check.object.path, "required": check.required, "excluded": check.excluded} for check in checks],
    }))
    ctx.actions.run(
        executable = execution.python,
        arguments = [ctx.file._verify.path, spec.path, verified.path, report.path],
        inputs = depset([ctx.file._verify, ctx.file._prepare_source, spec] + objects + scripts.values(), transitive = [cc.all_files, metadata.files, metadata.compile_files, execution.files]),
        outputs = [verified, report],
        mnemonic = "KernelCapabilityVerify",
    )
    return [DefaultInfo(files = depset([verified, report])), KernelCompilerCapabilityVerificationInfo(verified = verified, toolchain_id = cc.toolchain_id)]

kernel_compiler_capabilities_check = rule(implementation = _check_impl, fragments = ["cpp"], toolchains = use_cc_toolchain() + [EXECUTION_TOOLS_TYPE], attrs = {
    "_setup": attr.label(default = "//:toolchain", providers = [KernelToolchainInfo]),
    "_sources": attr.label(default = "//:source_tree"),
    "_anchor": attr.label(default = "//:source/Makefile", allow_single_file = True),
    "_verify": attr.label(default = "//rules:verify_capabilities.py", allow_single_file = True, cfg = "exec"),
    "_prepare_source": attr.label(default = "//rules:prepare_source.py", allow_single_file = True, cfg = "exec"),
    "_capabilities": attr.label(default = "//:compiler_capabilities", providers = [KernelCompilerCapabilitiesInfo]),
    "_metadata": attr.label(default = "//:compiler_metadata", providers = [KernelCompilerMetadataInfo]),
})
