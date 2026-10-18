"""Capabilities declared by the selected compiler provider and verified for products."""

load("@rules_cc//cc:find_cc_toolchain.bzl", "find_cc_toolchain", "use_cc_toolchain")
load(":architecture.bzl", "KernelArchitectureInfo")
load(":compiler_capabilities_data.bzl", "COMPILER_CAPABILITIES", "COMPILER_CAPABILITY_COMMANDS")
load(":compiler_metadata.bzl", "KernelCompilerMetadataInfo")

KernelCompilerCapabilitiesInfo = provider(doc = "Kernel Compiler Capabilities Info.", fields = ["inputs", "checks", "toolchain_id", "make"])
KernelCompilerCapabilityVerificationInfo = provider(doc = "Required verification of declared selected-toolchain capabilities.", fields = ["verified", "toolchain_id"])

def _impl(ctx):
    cc = find_cc_toolchain(ctx)
    compiler = ctx.attr._compiler[config_common.FeatureFlagInfo].value
    architecture = ctx.attr._architecture[KernelArchitectureInfo]
    metadata = ctx.attr._metadata[KernelCompilerMetadataInfo]
    inputs, checks, make = {}, [], {}
    for fact in COMPILER_CAPABILITIES:
        supplied = (compiler in fact["compilers"] if metadata.capabilities == None else fact.get("input", fact["probe"] + ":" + fact["argument"]) in metadata.capabilities)
        applicable = architecture.cpu in fact["cpus"] if "cpus" in fact else architecture.srcarch == fact["srcarch"]
        supported = supplied and applicable and (fact.get("kbuild", False) or fact["input"] in COMPILER_CAPABILITY_COMMANDS)
        if fact.get("kbuild"):
            if supported:
                make[fact["probe"] + ":" + fact["argument"]] = True
        else:
            inputs[fact["input"]] = ["y" if supported else "n", "tristate"]
        if supported:
            checks.append(fact)
        elif applicable and (fact.get("kbuild", False) or fact["input"] in COMPILER_CAPABILITY_COMMANDS):
            # buildifier: disable=print
            print("WARNING: unresolved toolchain capability %s in %s; using fallback n" % (fact["description"], cc.toolchain_id))
    return [KernelCompilerCapabilitiesInfo(inputs = inputs, checks = checks, toolchain_id = cc.toolchain_id, make = make)]

kernel_compiler_capabilities = rule(implementation = _impl, fragments = ["cpp"], toolchains = use_cc_toolchain(), attrs = {
    "_compiler": attr.label(default = "@rules_cc//cc/compiler:compiler", providers = [config_common.FeatureFlagInfo]),
    "_architecture": attr.label(default = "//:architecture", providers = [KernelArchitectureInfo]),
    "_metadata": attr.label(default = "//:compiler_metadata", providers = [KernelCompilerMetadataInfo]),
})
