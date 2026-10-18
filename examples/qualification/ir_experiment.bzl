"""Bounded qualification of native frontend/backend actions."""

load("@kernel//rules:image.bzl", "KernelBuildInfo")

_ACTIVE_CONFIG = str(Label("@kernel//:active_config"))

# Bazel supplies both transition callback arguments.
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

def _variant_impl(ctx):
    target = ctx.attr.target[0]
    values = target[KernelBuildInfo].config.values
    for name, expected in ctx.attr.required_options.items():
        if values.get(name) != expected:
            fail("IR qualification requires %s=%s, got %s" % (name, expected, values.get(name)))
    files = target[OutputGroupInfo].ir_experiment.to_list()
    reports = [file for file in files if file.basename.endswith(".ir.json")]
    if len(reports) != 1:
        fail("Expected one IR qualification report, got " + str(reports))
    aliases = []
    for file in files:
        output = ctx.actions.declare_file(ctx.label.name + "/" + file.short_path)
        ctx.actions.symlink(output = output, target_file = file)
        aliases.append(output)
    manifest = ctx.actions.declare_file(ctx.label.name + ".json")
    ctx.actions.write(manifest, json.encode({
        "root": ctx.label.package + "/" + ctx.label.name,
        "report": reports[0].short_path,
        "diagnostics": [file.short_path for file in files if file.basename.endswith(".ir-diagnostics.json")],
    }))
    return [
        DefaultInfo(files = depset([manifest]), runfiles = ctx.runfiles(files = aliases + [manifest])),
        OutputGroupInfo(ir_experiment = depset(aliases + [manifest])),
    ]

ir_variant = rule(implementation = _variant_impl, attrs = {
    "target": attr.label(mandatory = True, cfg = _transition),
    "config": attr.label(mandatory = True),
    "platform": attr.label(default = "@llvm//platforms:linux_x86_64"),
    "required_options": attr.string_dict(),
    "_allowlist_function_transition": attr.label(default = "@bazel_tools//tools/allowlists/function_transition_allowlist"),
})
