"""Declared tools for native Kconfig bootstrap and probes."""

EXECUTION_TOOLS_TYPE = Label("//execution_tools:toolchain_type")

def _impl(ctx):
    commands, files, tools, transitive = {}, [], [], []
    for target, names in ctx.attr.commands.items():
        info = target[DefaultInfo]
        executable = info.files_to_run.executable
        if executable == None:
            candidates = info.files.to_list()
            if len(candidates) != 1:
                fail("Execution command must supply one executable: %s" % target.label)
            executable = candidates[0]
        else:
            tools.append(info.files_to_run)
        transitive.extend([info.files, info.default_runfiles.files])
        for name in names.split(" "):
            if not name or "/" in name or name in commands:
                fail("Invalid or duplicate execution command: " + name)
            alias = ctx.actions.declare_file(ctx.label.name + "/bin/" + name)
            ctx.actions.symlink(output = alias, target_file = executable, is_executable = True)
            commands[name] = alias
            files.append(alias)
    coreutils = ctx.toolchains["@bazel_lib//lib:coreutils_toolchain_type"].coreutils_info.bin
    files.append(coreutils)
    for name in ctx.attr.coreutils:
        if name in commands:
            fail("Duplicate execution command: " + name)
        alias = ctx.actions.declare_file(ctx.label.name + "/bin/" + name)
        ctx.actions.symlink(output = alias, target_file = coreutils, is_executable = True)
        commands[name] = alias
        files.append(alias)
    environment, path_environment = {}, {}
    for type_name, field, command, tool_field, env_field in [
        ("@rules_m4//m4:toolchain_type", "m4_toolchain", "m4", "m4_tool", "m4_env"),
        ("@rules_bison//bison:toolchain_type", "bison_toolchain", "bison", "bison_tool", "bison_env"),
        ("@rules_flex//flex:toolchain_type", "flex_toolchain", "flex", "flex_tool", "flex_env"),
    ]:
        provider = getattr(ctx.toolchains[type_name], field)
        tool = getattr(provider, tool_field)
        tools.append(tool)
        transitive.append(provider.all_files)
        alias = ctx.actions.declare_file(ctx.label.name + "/bin/" + command)
        ctx.actions.symlink(output = alias, target_file = tool.executable, is_executable = True)
        commands[command] = alias
        files.append(alias)
        for name, value in getattr(provider, env_field).items():
            if name in ["M4", "BISON_PKGDATADIR", "M4_SYSCMD_SHELL"]:
                path_environment[name] = value
            else:
                environment[name] = value
    runtime = ctx.attr._python_runtime[platform_common.ToolchainInfo].py3_runtime
    if runtime.interpreter == None:
        fail("Native configuration requires a declared Python interpreter")
    files.append(runtime.interpreter)
    transitive.append(runtime.files)
    alias = ctx.actions.declare_file(ctx.label.name + "/bin/python3")
    ctx.actions.symlink(output = alias, target_file = runtime.interpreter, is_executable = True)
    commands["python3"] = alias
    files.append(alias)
    path_environment.update(M4 = commands["m4"].path, M4_SYSCMD_SHELL = commands["sh"].path)
    closure = depset(files, transitive = transitive)
    return [platform_common.ToolchainInfo(execution = struct(
        commands = commands,
        files = closure,
        tools = tools,
        bin_dir = commands["sh"].dirname,
        shell = commands["sh"],
        python = runtime.interpreter,
        environment = environment,
        path_environment = path_environment,
    )), DefaultInfo(files = closure)]

kernel_execution_tools = rule(
    implementation = _impl,
    attrs = {
        "commands": attr.label_keyed_string_dict(cfg = "exec", allow_files = True, mandatory = True),
        "coreutils": attr.string_list(),
        "_python_runtime": attr.label(default = "@rules_python//python:current_py_toolchain", cfg = "exec"),
    },
    toolchains = [
        "@bazel_lib//lib:coreutils_toolchain_type",
        "@rules_m4//m4:toolchain_type",
        "@rules_bison//bison:toolchain_type",
        "@rules_flex//flex:toolchain_type",
    ],
)
