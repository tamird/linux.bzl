"""Build source-owned objtool objects with the selected host context."""

load(":commands.bzl", "shell_quote", "shell_words", "value")
load(":execution_tools.bzl", "execution_tools", "run_recipe")
load(":expressions.bzl", "evaluate", "make_words", "match", "program", "source_index")

def _normal(path):
    parts = []
    for part in path.split("/"):
        if part == "..":
            parts.pop()
        elif part and part != ".":
            parts.append(part)
    return "/".join(parts)

def _recursive_variables(variables, index):
    result = {name: {"value": value(name, variables, index)} for name in variables.get("__exports__", {}) if name in variables}
    result.update({name: cell for name, cell in variables.items() if type(cell) == "dict" and cell.get("command_line", False)})
    result["__exports__"] = {name: True for name in result}
    for name in ("__command_line__", "__cell_sequence__"):
        if name in variables:
            result[name] = variables[name]
    return result

def _aggregate(ctx, setup, scope, index, inputs, output, mode = ""):
    """Bind actual objects after the native archive/link variable setup."""
    execution = execution_tools(ctx)
    local = program(setup.program["image_host_link_commands"], scope, index, cells = True).variables
    paths = " ".join([file.path for file in inputs])
    local.update({name: {"value": text} for name, text in {
        "@": output.path,
        "^": paths,
        "obj-y": paths,
        "objprefix": "",
        "LD": execution.host_roles["LD"],
        "HOSTLD": execution.host_roles["LD"],
        "AR": execution.host_roles["AR"],
        "HOSTAR": execution.host_roles["AR"],
    }.items()})
    run_recipe(ctx, execution, command = value("cmd_" + mode + "ld_multi", local, index), inputs = inputs, outputs = [output], env = execution.host_link_env, mnemonic = "NativeHostAggregate")

def build_objtool(ctx, prepared, setup, capabilities, requests, package_command):
    architecture = setup.architecture
    flag_program = setup.program
    arch_file = "tools/objtool/arch/" + architecture.srcarch + "/Build"
    if arch_file not in flag_program["image_programs"]:
        return struct(executable = None, architecture = architecture)
    source = prepared.root.path
    index = source_index([source + "/" + path for path in prepared.logical])
    execution = execution_tools(ctx)
    index["__execution_tools__"] = {name: file.path for name, file in execution.commands.items()}
    index["__source_root__"] = source
    index["__capabilities__"] = capabilities
    index["__requests__"] = requests
    environment = dict(execution.host_compile_env, CONFIG_SHELL = execution.shell.path)
    environment["PATH"] = package_command.dirname + ":" + environment.get("PATH", execution.bin_dir)
    index["__probe_environment__"] = environment
    selected = {
        "CC": " ".join([shell_quote(execution.host_cc)] + [shell_quote(flag) for flag in setup.host_compile_flags]),
        "CXX": execution.host_roles["CXX"],
        "LD": execution.host_roles["LD"],
        "AR": execution.host_roles["AR"],
        "HOSTCC": " ".join([shell_quote(execution.host_cc)] + [shell_quote(flag) for flag in setup.host_compile_flags]),
        "HOSTLD": execution.host_roles["LD"],
        "HOSTAR": execution.host_roles["AR"],
        "HOSTPKG_CONFIG": package_command.path,
        "AWK": execution.commands["awk"].path,
        "CONFIG_SHELL": execution.shell.path,
    }
    probe = ctx.actions.declare_file(ctx.label.name + ".generated/.root")

    # Query identity must survive planning rounds. These are the native final
    # output names; provisional action Files still belong to their own round.
    prefix = ctx.output_prefix + "/" + ctx.label.name + ".generated/"
    ctx.actions.write(probe, "")
    initial = _recursive_variables(setup.variables, index)
    initial["__command_line__"] = dict(selected)
    initial.update({name: {"value": text} for name, text in dict(selected, SRCARCH = architecture.srcarch, HOSTARCH = "x86", srctree = source, CURDIR = source + "/tools/objtool", OUTPUT = prefix).items()})
    variables = program(flag_program["image_utilities"], initial, index, cells = True).variables
    variables["AWK"] = {"value": execution.commands["awk"].path}
    variables = program(flag_program["image_host_objtool"], variables, index, cells = True).variables
    invocation = dict(variables)
    invocation["MAKE"] = {"value": "__objtool__"}
    invocation["@"] = variables["OBJTOOL_IN"]
    words = shell_words(evaluate(flag_program["image_host_objtool_call"], invocation, index).lstrip("@+"))
    if words[:3] != ["__objtool__", "-f", source + "/tools/build/Makefile.build"]:
        fail("Unsupported native objtool build invocation: " + str(words))
    build_variables = _recursive_variables(variables, index)
    for word in words[3:]:
        name, separator, text = word.partition("=")
        if not separator:
            fail("Native objtool build argument is not an assignment: " + word)
        build_variables[name] = {"value": text, "command_line": True}
        build_variables["__command_line__"] = dict(build_variables.get("__command_line__", {}), **{name: text})
    if value("obj", build_variables, index) != "objtool" or value("dir", build_variables, index) != ".":
        fail("Unsupported native objtool object namespace: " + str(words))
    build = program(flag_program["image_host_build"], build_variables, index, cells = True)

    # The native aggregate link consumes $^: duplicate prerequisites occur
    # only once, in their first position. Keep each directory's object identity.
    objects = {"tools/objtool/" + p: ("objtool", build.variables) for p in make_words(value("objtool-y", build.variables, index)) if not p.endswith("/")}
    arch_variables = _recursive_variables(build.variables, index)
    arch_variables["dir"] = {"value": "./arch/" + architecture.srcarch, "command_line": True}
    generated = []

    # Native Build rules describe the architecture's two generated C tables.
    architecture_program = program(flag_program["image_programs"]["tools/objtool/arch/" + architecture.srcarch + "/Build"], arch_variables, index, cells = True)
    for rule in architecture_program.rules:
        if rule["recipe"]:
            target = rule["targets"][0]
            output = ctx.actions.declare_file(ctx.label.name + ".generated/" + target.removeprefix(prefix))
            local = dict(architecture_program.variables)
            local["@"] = {"value": output.path}

            # Make consumes recipe control prefixes after variable expansion;
            # Q can expand to @ even when the parsed recipe starts with $(Q).
            command = "\n".join([evaluate(expr, local, index).lstrip("@+") for expr in rule.get("recipes", [])])
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

    # Each architecture Build has its own object namespace.
    objects.update({"tools/objtool/arch/" + architecture.srcarch + "/" + p: ("objtool", architecture_program.variables) for p in make_words(value("objtool-y", architecture_program.variables, index))})
    aliases = {}
    for rule in build.rules:
        tail = make_words(rule["prerequisites"])
        if tail and tail[0].startswith("../"):
            for target in rule["targets"]:
                aliases[_normal("tools/objtool/" + target.removeprefix(prefix))] = _normal("tools/objtool/" + tail[0])
    invocation = dict(variables)
    library = value("LIBSUBCMD", variables, index)
    for name, text in {"MAKE": "__subcmd__", "Q": "", "@": library}.items():
        invocation[name] = {"value": text}
    words = shell_words(evaluate(flag_program["image_host_subcmd_call"], invocation, index).lstrip("@+"))
    if len(words) < 3 or words[:2] != ["__subcmd__", "-C"] or words[2].rstrip("/") != source + "/tools/lib/subcmd":
        fail("Unsupported native objtool subcmd invocation: " + str(words))
    library_variables = _recursive_variables(variables, index)
    library_variables["__command_line__"] = dict(library_variables.get("__command_line__", {}))
    goals = []
    for word in words[3:]:
        if "=" in word:
            name, text = word.split("=", 1)
            library_variables[name] = {"value": text, "command_line": True}
            library_variables["__command_line__"][name] = text
        elif word in (library, "install_headers"):
            goals.append(word)
        else:
            fail("Unsupported native subcmd goal: " + word)
    directory_rules = program(flag_program["image_host_subcmd_directories"], variables, index, cells = True)
    directories = []
    for rule in directory_rules.rules:
        if not rule.get("recipes"):
            continue
        for target in rule["targets"]:
            local = dict(directory_rules.variables)
            local["@"] = {"value": target}
            commands = [shell_words(evaluate(expr, local, index).lstrip("@+")) for expr in rule["recipes"]]
            if commands != [["mkdir", "-p", target]] and commands != [[execution.commands["mkdir"].path, "-p", target]]:
                fail("Unsupported native subcmd directory prerequisite: " + str(commands))
            directories.append(target.rstrip("/"))
    index["__probe_directories__"] = directories
    library_variables["PWD"] = {"value": "__object__"}
    subcmd = program(flag_program["image_host_subcmd"], library_variables, index, cells = True).variables
    if requests:
        return struct(executable = None, architecture = architecture)
    if _normal(value("LIBFILE", subcmd, index)) != _normal(library):
        fail("Native subcmd archive differs from its objtool caller: " + value("LIBFILE", subcmd, index) + " != " + library)

    # Declare public header installation only when the native caller requests
    # it. Individual files let compiler dependency discovery prune headers
    # without depending on an entire generated directory.
    installed_headers = []
    if "install_headers" in goals:
        headers = program(flag_program["image_host_subcmd_headers"], library_variables, index, cells = True).variables
        install_prefix = value("INSTALL_HDRS_PFX", headers, index) + "/"
        targets = make_words(value("INSTALL_HDRS", headers, index))
        if not targets:
            fail("Native subcmd install_headers has no exported header inventory")
        for target in targets:
            if not target.startswith(prefix) or not target.startswith(install_prefix):
                fail("Native subcmd header install escapes the declared objtool output: " + target)
            source_header = "tools/lib/subcmd/" + target.removeprefix(install_prefix)
            header = ctx.actions.declare_file(ctx.label.name + ".generated/" + target.removeprefix(prefix))
            ctx.actions.run(
                executable = execution.commands["cp"],
                arguments = [prepared.logical[source_header].path, header.path],
                inputs = [prepared.logical[source_header]],
                outputs = [header],
                env = execution.host_compile_env,
            )
            installed_headers.append(header)
    objects.update({"tools/lib/subcmd/" + path: ("libsubcmd", subcmd) for path in make_words(value("libsubcmd-y", subcmd, index))})
    outputs = {"objtool": [], "libsubcmd": []}
    for obj, (owner, scope) in objects.items():
        src = aliases.get(obj, obj[:-2] + ".c")
        local = dict(scope)
        local.update({"@": {"value": obj}, "obj": {"value": owner}})
        local = program(flag_program["image_host_cflags"], local, index, cells = True).variables
        flags = [flag for flag in shell_words(value("c_flags", local, index)) if not flag.startswith(("-Wp,-MD,", "-Wp,-MMD,", "-Wp,-MT,"))]
        flags = [flag.replace("../", source + "/tools/") for flag in flags]
        output = ctx.actions.declare_file("objtool/" + obj + ".o")
        ctx.actions.run(
            executable = execution.host_cc,
            arguments = setup.host_compile_flags + flags + ["-c", source + "/" + src, "-o", output.path],
            inputs = depset(generated + installed_headers, transitive = [prepared.files, execution.files]),
            outputs = [output],
            env = execution.host_compile_env,
            mnemonic = "ObjtoolCompile",
        )
        outputs[owner].append(output)

    def generated_output(path):
        path = _normal(path)
        if not path.startswith(prefix):
            fail("Native host output escapes the declared namespace: " + path)
        return ctx.actions.declare_file(ctx.label.name + ".generated/" + path.removeprefix(prefix))

    aggregates = {}
    for owner, scope, name in [("objtool", variables, "OBJTOOL_IN"), ("libsubcmd", subcmd, "SUBCMD_IN")]:
        output = generated_output(value(name, scope, index))
        _aggregate(ctx, setup, scope, index, outputs[owner], output)
        aggregates[owner] = output
    library = generated_output(value("LIBFILE", subcmd, index))
    local = dict(subcmd)
    local.update({name: {"value": text} for name, text in {
        "@": library.path,
        "SUBCMD_IN": aggregates["libsubcmd"].path,
        "AR": execution.host_roles["AR"],
        "QUIET_AR": "",
    }.items()})
    run_recipe(ctx, execution, command = "\n".join([evaluate(expr, local, index).lstrip("@+") for expr in flag_program["image_host_subcmd_final"]]), inputs = [aggregates["libsubcmd"]], outputs = [library], env = execution.host_link_env, mnemonic = "ObjtoolArchive")
    binary = generated_output(value("OBJTOOL", variables, index))
    link_driver = " ".join([shell_quote(execution.host_cc)] + [shell_quote(flag) for flag in setup.host_link_flags])
    local = dict(variables)
    local.update({name: {"value": text} for name, text in {
        "@": binary.path,
        "OBJTOOL_IN": aggregates["objtool"].path,
        "LIBSUBCMD": library.path,
        "CC": link_driver,
        "HOSTCC": link_driver,
        "QUIET_LINK": "",
    }.items()})
    run_recipe(ctx, execution, command = "\n".join([evaluate(expr, local, index).lstrip("@+") for expr in flag_program["image_host_objtool_final"]]), inputs = [aggregates["objtool"], library], outputs = [binary], env = execution.host_link_env, mnemonic = "ObjtoolLink")
    return struct(executable = binary, architecture = architecture)

def build_resolve_btfids(ctx, prepared, setup, capabilities, requests, package_command, libraries, libbpf):
    """Compile the resolver against its own source's installed libbpf headers."""
    execution = execution_tools(ctx)
    source = prepared.root.path
    directory = "tools/bpf/resolve_btfids"
    index = source_index([source + "/" + path for path in prepared.logical])
    index.update(__execution_tools__ = {name: file.path for name, file in execution.commands.items()}, __source_root__ = source, __capabilities__ = capabilities, __requests__ = requests)
    index["__probe_environment__"] = dict(execution.host_compile_env, CONFIG_SHELL = execution.shell.path, PATH = package_command.dirname + ":" + execution.bin_dir)
    anchor = ctx.actions.declare_file("resolve_btfids/.root")
    ctx.actions.write(anchor, "")
    prefix = ctx.output_prefix + "/resolve_btfids/"
    selected = dict(execution.host_roles)
    selected.update(HOSTCC = execution.host_cc, HOSTLD = execution.host_roles["LD"], HOSTAR = execution.host_roles["AR"], HOSTPKG_CONFIG = package_command.path, LIBBPF_INCLUDE = libbpf.include)
    initial = _recursive_variables(setup.variables, index)
    initial["__command_line__"] = selected
    initial.update({name: {"value": text} for name, text in dict(selected, srctree = source, CURDIR = source + "/" + directory, OUTPUT = prefix, HOSTARCH = "x86", SUBCMD_INCLUDE = source + "/tools/lib", LIBELF_FLAGS = " ".join([shell_quote(flag) for flag in libraries.cflags])).items()})
    variables = program(setup.program["image_host_btfids"], initial, index, cells = True).variables
    variables["srctree"] = {"value": source}
    build = program(setup.program["image_host_btfids_build"], variables, index, cells = True)
    variables = build.variables
    variables.update(obj = {"value": "resolve_btfids"}, host = {"value": ""})
    variables = program(setup.program["image_host_compile_mode"], variables, index, cells = True).variables
    mode = value("host", variables, index)
    if mode not in ("", "host_"):
        fail("Unsupported native resolver compiler mode: " + mode)
    library_variables = _recursive_variables(variables, index)
    library_variables["__command_line__"] = dict(library_variables.get("__command_line__", {}))
    for assignment in shell_words(value("HOST_OVERRIDES", variables, index)):
        name, separator, text = assignment.partition("=")
        if not separator:
            fail("Native resolver library override is not an assignment: " + assignment)
        library_variables[name] = {"value": text, "command_line": True}
        library_variables["__command_line__"][name] = text
    library_variables.update(OUTPUT = {"value": prefix + "libsubcmd/"}, CURDIR = {"value": source + "/tools/lib/subcmd"})
    subcmd = program(setup.program["image_host_subcmd"], library_variables, index, cells = True).variables
    if requests:
        return None
    objects = [(directory + "/" + obj, "resolve_btfids", variables, mode + "c_flags") for obj in make_words(value("resolve_btfids-y", variables, index))]
    objects.extend([("tools/lib/subcmd/" + obj, "libsubcmd", subcmd, "c_flags") for obj in make_words(value("libsubcmd-y", subcmd, index))])
    outputs = {"resolve_btfids": [], "libsubcmd": []}
    for obj, owner, scope, flag_name in objects:
        src = obj[:-2] + ".c"
        if src not in prepared.logical:
            for rule in build.rules:
                for pattern in rule["targets"]:
                    stem = match(pattern, value("OUTPUT", scope, index) + obj.removeprefix(directory + "/"))
                    if stem == None:
                        continue
                    prerequisites = make_words(rule["prerequisites"])
                    if prerequisites:
                        candidate = _normal(directory + "/" + prerequisites[0].replace("%", stem, 1))
                        if candidate in prepared.logical:
                            src = candidate
        if src not in prepared.logical:
            fail("No native source for resolver object " + obj)
        local = dict(scope)
        local.update({"@": {"value": obj}, "obj": {"value": owner}})
        local = program(setup.program["image_host_cflags"], local, index, cells = True).variables
        flags = [flag for flag in shell_words(value(flag_name, local, index)) if not flag.startswith(("-Wp,-MD,", "-Wp,-MMD,", "-Wp,-MT,"))]
        output = ctx.actions.declare_file("resolve_btfids/objects/" + obj)
        arguments = setup.host_compile_flags + libraries.cflags + flags + ["-c", prepared.logical[src].path, "-o", output.path]
        ctx.actions.run(executable = execution.host_cc, arguments = arguments, inputs = depset([prepared.logical[src]], transitive = [prepared.files, libbpf.files, libraries.files]), outputs = [output], env = execution.host_compile_env, mnemonic = "ResolveBtfidsCC", progress_message = "Compile resolver host object " + obj)
        outputs[owner].append(output)
    aggregates = {}
    for owner, scope in [("resolve_btfids", variables), ("libsubcmd", subcmd)]:
        output = ctx.actions.declare_file("resolve_btfids/" + owner + "-in.o")
        _aggregate(ctx, setup, scope, index, outputs[owner], output, mode = mode if owner == "resolve_btfids" else "")
        aggregates[owner] = output
    library = ctx.actions.declare_file("resolve_btfids/libsubcmd/libsubcmd.a")
    local = dict(subcmd)
    local.update({"@": {"value": library.path}, "SUBCMD_IN": {"value": aggregates["libsubcmd"].path}, "AR": {"value": execution.host_roles["AR"]}})
    run_recipe(ctx, execution, command = "\n".join([evaluate(expr, local, index).lstrip("@+") for expr in setup.program["image_host_subcmd_final"]]), inputs = [aggregates["libsubcmd"]], outputs = [library], env = execution.host_link_env, mnemonic = "ResolveBtfidsArchive")
    binary = ctx.actions.declare_file("resolve_btfids/resolve_btfids")
    link_driver = " ".join([shell_quote(execution.host_cc)] + [shell_quote(flag) for flag in setup.host_link_flags])
    local = dict(variables)
    local.update({name: {"value": text} for name, text in {"@": binary.path, "BINARY_IN": aggregates["resolve_btfids"].path, "BPFOBJ": libbpf.archive.path, "SUBCMDOBJ": library.path, "LIBS": " ".join([shell_quote(flag) for flag in libraries.libs]), "CC": link_driver, "HOSTCC": link_driver, "msg": "", "Q": ""}.items()})
    run_recipe(ctx, execution, command = "\n".join([evaluate(expr, local, index).lstrip("@+") for expr in setup.program["image_host_btfids_final"]]), inputs = depset([aggregates["resolve_btfids"], library, libbpf.archive], transitive = [libraries.files]), outputs = [binary], env = execution.host_link_env, mnemonic = "ResolveBtfidsLink")
    return binary
