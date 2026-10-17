"""Source tree with interpreter paths supplied by the execution toolchain."""

load("@linux.bzl//execution_tools:defs.bzl", "EXECUTION_TOOLS_TYPE", "execution_tools")
load("@rules_cc//cc/common:cc_common.bzl", "cc_common")

KernelSourceTreeInfo = provider(
    doc = "Original source artifacts and the separate interpreter-bound tree for executing scripts.",
    fields = {
        "source_root": "Execroot-relative directory containing the original source Files.",
        "logical": "Logical source paths mapped to individual original Files.",
        "compilation_context": "Shared original source candidates for compiler dependency discovery.",
        "root": "Prepared script tree. Compilers must use the original Files instead.",
        "files": "Prepared script tree and the interpreters needed to execute it.",
    },
)

def _impl(ctx):
    execution = execution_tools(ctx)
    originals = ctx.attr.sources[DefaultInfo].files
    prefix = ctx.file.anchor.dirname + "/"
    logical = {f.path[len(prefix):]: f for f in originals.to_list() if f.path.startswith(prefix)}
    manifest = ctx.actions.declare_file(ctx.label.name + ".json")
    output = ctx.actions.declare_directory(ctx.label.name + ".tree")
    ctx.actions.write(manifest, json.encode({
        "sources": {p: f.path for p, f in logical.items()},
        "commands": {n: f.path for n, f in execution.commands.items()},
    }))
    ctx.actions.run(
        executable = execution.python,
        arguments = [ctx.file._prepare.path, manifest.path, output.path],
        inputs = depset([manifest, ctx.file._prepare], transitive = [originals, execution.files]),
        outputs = [output],
        mnemonic = "KbuildSourceTree",
    )
    files = depset([output], transitive = [execution.files])

    # Creating a compilation context copies its direct header inventory. Share
    # this context as a dependency so each object retains the source candidates
    # transitively instead of copying the entire kernel's files during analysis.
    compilation_context = cc_common.create_compilation_context(headers = originals)
    return [KernelSourceTreeInfo(source_root = ctx.file.anchor.dirname, root = output, logical = logical, files = files, compilation_context = compilation_context), DefaultInfo(files = files)]

kernel_source_tree = rule(implementation = _impl, toolchains = [EXECUTION_TOOLS_TYPE], attrs = {
    "sources": attr.label(mandatory = True),
    "anchor": attr.label(allow_single_file = True, mandatory = True),
    "_prepare": attr.label(default = "//rules:prepare_source.py", allow_single_file = True, cfg = "exec"),
})
