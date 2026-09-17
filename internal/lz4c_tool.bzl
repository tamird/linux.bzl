"""Expose the configured LZ4 CLI under its legacy command name."""

def _lz4c_tool_impl(ctx):
    executable = ctx.actions.declare_file(ctx.label.name)
    ctx.actions.symlink(
        output = executable,
        target_file = ctx.executable.binary,
        is_executable = True,
    )
    return [DefaultInfo(
        executable = executable,
        runfiles = ctx.runfiles(
            files = [ctx.executable.binary],
            transitive_files = ctx.attr.binary[DefaultInfo].default_runfiles.files,
        ),
    )]

lz4c_tool = rule(
    implementation = _lz4c_tool_impl,
    attrs = {
        "binary": attr.label(
            default = Label("@lz4//programs:lz4"),
            executable = True,
            cfg = "exec",
        ),
    },
    executable = True,
)
