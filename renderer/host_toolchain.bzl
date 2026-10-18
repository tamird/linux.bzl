"""C++ toolchain metadata consumed through an execution transition."""

load("@rules_cc//cc:find_cc_toolchain.bzl", "find_cc_toolchain", "use_cc_toolchain")
load("@rules_cc//cc/common:cc_common.bzl", "cc_common")
load(":architecture.bzl", "KernelArchitectureInfo")
load(":compiler_metadata.bzl", "KernelCompilerMetadataInfo")

KernelHostToolchainInfo = provider(doc = "Kernel Host Toolchain Info.", fields = ["cc", "features", "compiler", "compile_files", "architecture"])

def _impl(ctx):
    cc = find_cc_toolchain(ctx)
    metadata = ctx.attr._metadata[KernelCompilerMetadataInfo]
    if metadata.toolchain_id != cc.toolchain_id:
        fail("Host compiler metadata and image actions must use the same selected toolchain")
    features = cc_common.configure_features(ctx = ctx, cc_toolchain = cc, requested_features = ctx.features, unsupported_features = ctx.disabled_features)
    return [KernelHostToolchainInfo(cc = cc, features = features, compiler = metadata.compiler, compile_files = metadata.compile_files, architecture = ctx.attr._architecture[KernelArchitectureInfo])]

kernel_host_toolchain = rule(implementation = _impl, fragments = ["cpp"], toolchains = use_cc_toolchain(), attrs = {
    "_architecture": attr.label(default = "//:architecture", providers = [KernelArchitectureInfo]),
    "_metadata": attr.label(default = "//:compiler_metadata", providers = [KernelCompilerMetadataInfo]),
})
