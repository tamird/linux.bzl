"""Analysis-time command lowering with explicit inputs and direct LLVM actions."""

load("@linux.bzl//execution_tools:defs.bzl", "execution_tools", "run_recipe")
load("@rules_cc//cc:action_names.bzl", "ACTION_NAMES")
load("@rules_cc//cc/common:cc_common.bzl", "cc_common")
load(":expressions.bzl", "evaluate", "make_words", "match", "program")
load(":flag_program.bzl", "FLAG_PROGRAM")

def value(name, variables, sources):
    return evaluate([{"op": "reference", "name": [{"op": "literal", "value": name}]}], variables, sources)

def shell_quote(text):
    return "'" + text.replace("'", "'\\''") + "'"

def _stage_links(files):
    # Preserve the namespaces of declared inputs, including when this ruleset
    # and its source tree live in an external repository.
    roots = {f.path.split("/")[0]: True for f in files}
    return "\n".join(['if [ ! -e %s ]; then ln -s "$exec_root"/%s %s; fi' % (shell_quote(root), shell_quote(root), shell_quote(root)) for root in sorted(roots)])

def shell_words(text, allow_recipe = False):
    # Split an already-expanded command argument list, retaining quoted C
    # strings. This does not parse Make expressions or execute shell syntax.
    words, current, quote, escaped, active = [], "", "", False, False
    for c in text.elems():
        if escaped:
            current += c
            escaped = False
        elif c == "\\" and quote != "'":
            escaped = True
            active = True
        elif quote:
            if c == quote:
                quote = ""
            else:
                current += c
        elif c in ["'", '"']:
            quote = c
            active = True
        elif c in [" ", "\t", "\n"]:
            if active:
                words.append(current)
                current, active = "", False
        elif c in [";", "|", "&", "<", ">", "`"]:
            if allow_recipe:
                return None
            fail("Shell operation in compiler arguments: " + text)
        else:
            current += c
            active = True
    if quote or escaped:
        fail("Unclosed quote in compiler arguments: " + text)
    if active:
        words.append(current)
    return words

def _scope(setup, directory, target, objtree, source, ancestry = None, graph_variables = {}, native = None, invocation_environment = None):
    variables = dict(setup.variables)
    if invocation_environment != None:
        variables = {name: {"value": cell["value"].replace("__source__", source).replace("__object__", objtree)} for name, cell in invocation_environment.items() if name != "__exports__"}
        variables["__config__"] = setup.variables["__config__"]
        ancestry = [directory]
    for key, val in {
        "objtree": objtree,
        "srctree": source,
        "srcroot": source,
        "building_out_of_srctree": "1",
        "obj": directory,
        "src": source + "/" + directory,
        "@": target,
        "*": native["stem"] if native != None else "",
        "KGZIP": setup.tools["KGZIP"],
        "LEX": setup.tools["LEX"],
        "YACC": setup.tools["YACC"],
        "AWK": setup.tools["AWK"],
    }.items():
        variables[key] = {"value": val}
    if invocation_environment != None:
        variables = program(FLAG_PROGRAM["graph_init"] + FLAG_PROGRAM["image_common"], variables, setup.sources, cells = True).variables
    if native != None and native.get("phase") != None:
        # Final module objects belong to Makefile.modfinal's root invocation,
        # not the source module's directory Kbuild or Makefile.build helpers.
        return program(FLAG_PROGRAM["image_" + native["phase"]], variables, setup.sources, cells = True).variables
    inherited_c, inherited_a = [], []
    target_assignments = []
    parts = [] if directory == "." else directory.split("/")
    for parent in ancestry if ancestry != None else (["."] + ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]):
        for name in ["ccflags-y", "asflags-y", "subdir-ccflags-y", "subdir-asflags-y", "HOST_EXTRACFLAGS", "HOST_EXTRACXXFLAGS"]:
            variables[name] = {"value": ""}
        variables["obj"] = {"value": parent}
        variables["src"] = {"value": source + "/" + parent}
        prefix = "" if parent == "." else parent + "/"
        path = prefix + "Kbuild" if prefix + "Kbuild" in FLAG_PROGRAM["directories"] else prefix + "Makefile"
        variables["MAKEFILE_LIST"] = {"value": source + "/" + path}
        scope = program(FLAG_PROGRAM["directories"].get(path, []), variables, setup.sources, cells = True)
        variables = scope.variables
        target_assignments.extend(scope.target_assignments)
        inherited_c.append(value("subdir-ccflags-y", variables, setup.sources))
        inherited_a.append(value("subdir-asflags-y", variables, setup.sources))
    for name, inherited in [("KBUILD_CFLAGS", inherited_c), ("KBUILD_AFLAGS", inherited_a)]:
        variables[name] = {"value": value(name, variables, setup.sources) + " " + " ".join(inherited)}

    # Keep Kbuild's graph snapshots and recursive helper flavors. Replaying
    # graph transformations here would prefix paths and expand members twice.
    variables.update(graph_variables)
    variables = program(FLAG_PROGRAM["image_helpers"], variables, setup.sources, cells = True).variables
    object_scope = program(FLAG_PROGRAM["image_object_helpers"], variables, setup.sources, cells = True)
    variables = object_scope.variables
    if native != None:
        variables.update(native.get("inherited_variables", {}))
    target_assignments.extend(object_scope.target_assignments)
    for patterns_first in [True, False]:
        for assignment in target_assignments:
            patterns = make_words(evaluate(assignment["targets_expression"], variables, setup.sources))
            if any(["%" in p for p in patterns]) != patterns_first:
                continue
            if any([match(p.removeprefix("./"), target) != None for p in patterns]):
                node = dict(assignment)
                node["kind"] = "assignment"
                variables = program([node], variables, setup.sources, cells = True).variables
    return variables

def _host_link_flags(setup):
    variables = cc_common.create_link_variables(cc_toolchain = setup.host_cc, feature_configuration = setup.host_features)
    return cc_common.get_memory_inefficient_command_line(
        feature_configuration = setup.host_features,
        action_name = ACTION_NAMES.cpp_link_executable,
        variables = variables,
    )

def _rewrite_flags(flags, tree, source, generated):
    rewritten, include_next = [], ""
    for f in flags:
        if f.startswith(("-Wp,-MMD,", "-Wp,-MD,")):
            continue
        f = f.replace("__object__", tree).replace("__source__", source)
        if f in ("-I", "-include", "-imacros", "-iquote", "-isystem"):
            include_next = f
        elif include_next or f.startswith("-I"):
            path = f if include_next else f[2:]

            # Bare forced headers use the compiler's include search path.
            # Only relocate them when they name an actual generated input.
            search_header = include_next in ("-include", "-imacros") and path not in generated
            if not search_header and path not in (source, tree, "external", "bazel-out") and not path.startswith((source + "/", tree + "/", "external/", "bazel-out/", "/")):
                path = tree + "/" + path
            f = path if include_next else "-I" + path
            include_next = ""
        rewritten.append(f)
    return rewritten

def _compile(ctx, target, source_file, flags, dependencies, direct_dependencies, directory_marker, source_tree, source_prerequisites, setup, host):
    # Let Bazel's C++ action discover includes from the compiler's depfile.
    # Candidate source files belong in the prunable header context, including
    # textual includes without a .h suffix. Making them additional_inputs (or
    # supplying the prepared tree) would make every source edit recompile all
    # objects. Kbuild's explicit prerequisites remain mandatory: .incbin and
    # similar assembler reads are not recorded by the C preprocessor.
    if source_file == None:
        fail("No declared compiler source for " + target)
    features = setup.host_features if host else setup.features
    if not host:
        if setup.compiler == "clang" and "-nostdinc" in flags:
            # Clang leaves the toolchain's -nostdlibinc unclaimed when Kbuild
            # passes -nostdinc. Split the equivalent policy without suppressing
            # unused-argument diagnostics or permitting any default includes.
            normalized = []
            for flag in flags:
                normalized.extend(["-nostdlibinc", "-nobuiltininc"] if flag == "-nostdinc" else [flag])
            flags = normalized
        if "-S" in flags or "-E" in flags:
            # compiler_input_flags supplies both -c and the source. Native
            # -S/-E own the mode, so restore only the source argument.
            features = setup.non_object_features
            flags = flags + [source_file.path]

    # Generated ancestors stay available for textual includes, including
    # co-produced files, but only direct Kbuild prerequisites are mandatory.
    # A generated C file's producer may depend on the entire linked kernel;
    # those inputs must not force recompilation when its C output is unchanged.
    generated = cc_common.create_compilation_context(headers = depset(dependencies.values()))

    # Kbuild adds -I $(obj), allowing checked-in sources to include generated
    # headers through ../ paths. That directory must exist even when it has no
    # generated input files. A stable marker preserves it in the sandbox and
    # stays mandatory because an empty search directory is absent from depfiles.
    _, compilation = cc_common.compile(
        actions = ctx.actions,
        name = ctx.label.name + (".host-compile/" if host else ".compile/") + target,
        feature_configuration = features,
        cc_toolchain = setup.host_cc if host else setup.cc,
        srcs = [source_file],
        compilation_contexts = [source_tree.compilation_context, generated],
        additional_inputs = [directory_marker] + direct_dependencies.values() + source_prerequisites.values() + setup.files.to_list(),
        user_compile_flags = flags,
        disallow_pic_outputs = True,
    )
    if len(compilation.objects) != 1:
        fail("Expected one compiler output for %s, got %s" % (target, compilation.objects))
    return compilation.objects[0]

def _compiler_flags(command, compiler, source, target):
    """Recognize a single native compiler invocation without interpreting recipes."""
    args = shell_words(command, allow_recipe = True)
    if not args or args[0] != compiler or not any([flag in args for flag in ["-c", "-S", "-E"]]):
        return None
    flags, output_next, source_count, output_count = [], False, 0, 0
    for arg in args[1:]:
        if output_next:
            if arg != target:
                return None
            output_next = False
            output_count += 1
        elif arg == "-o":
            output_next = True
        elif arg == source:
            source_count += 1
        elif arg != "-c":
            flags.append(arg)
    if output_next or output_count != 1 or source_count != 1:
        return None
    return flags

# Fixed package fixtures or action helpers, not target-naming macros.
# buildifier: disable=unnamed-macro
def register(ctx, target, directory, kind, driver, outputs, dependencies, source_tree, config, setup, compiler_inputs, object_members = [], native = None, ancestry = None, graph_variables = {}, source_prerequisites = {}, invocation_environment = None):
    source = source_tree.source_root
    if kind == "archive":
        variables = dict(setup.variables)
        for key, val in {
            "obj": ".",
            "@": outputs[target].path,
            "AR": setup.tools["AR"],
            "real-prereqs": " ".join([dependencies[p].path for p in object_members]),
        }.items():
            variables[key] = {"value": val}
        expr = FLAG_PROGRAM["image_archives"]["library" if target.endswith("/lib.a") else "builtin"]
        command = evaluate(expr, variables, setup.sources)
        run_recipe(
            ctx,
            execution_tools(ctx),
            command = "set -eu\n" + command,
            inputs = depset(dependencies.values(), transitive = [setup.files]),
            outputs = outputs.values(),
            mnemonic = "KbuildAR",
            progress_message = "Archive %s" % target,
        )
        return

    if kind in ["host", "compile"]:
        # Objtool consumes the finished object in a separate action.
        compiler_dependencies = {p: f for p, f in dependencies.items() if p != "tools/objtool/objtool"}
        direct_dependencies = {p: f for p, f in compiler_inputs.direct.items() if p != "tools/objtool/objtool"}
        generated_root = compiler_inputs.root
        variables = _scope(setup, directory, target, generated_root, source, ancestry, graph_variables, native, invocation_environment)
        if kind == "host":
            link = not target.endswith(".o")
            path = target[:-2] + ".c" if target.endswith(".o") else target + ".c"
            objects = [dependencies[p] for p in object_members]
            if not objects:
                source_file = dependencies.get(path, source_tree.logical.get(path))
                compile_flags = shell_words(value("hostc_flags", variables, setup.sources))
                compile_flags = _rewrite_flags(compile_flags, generated_root, source, dependencies)
                obj = _compile(ctx, target, source_file, compile_flags, compiler_dependencies, direct_dependencies, compiler_inputs.directory_marker, source_tree, source_prerequisites, setup, host = True)
                objects.append(obj)
            if link:
                flags = _host_link_flags(setup) + shell_words(value("KBUILD_HOSTLDFLAGS", variables, setup.sources))
                flags.extend([obj.path for obj in objects])
                flags.extend(shell_words(value("KBUILD_HOSTLDLIBS", variables, setup.sources)))
                flags = _rewrite_flags(flags, generated_root, source, dependencies)
                flags.extend(["-o", outputs[target].path])
                ctx.actions.run(
                    executable = setup.tools["HOSTCC"],
                    arguments = flags,
                    inputs = depset(objects, transitive = [setup.files]),
                    outputs = outputs.values(),
                    mnemonic = "KbuildHostLink",
                    progress_message = "Link host tool %s" % target,
                )
            else:
                ctx.actions.symlink(output = outputs[target], target_file = objects[0])
            return

        path = target[:-2] + ".c"
        if path not in dependencies and path not in source_tree.logical and target.endswith(".o"):
            path = target[:-2] + ".S"
        if native != None and native.get("first"):
            path = native["first"].removeprefix("__source__/")
        source_file = dependencies.get(path, source_tree.logical.get(path))
        if source_file == None:
            fail("No declared compiler source %s for %s" % (path, target))
        compile_variables = program(FLAG_PROGRAM["image_command_helpers"], variables, setup.sources, cells = True).variables
        compile_variables.update(variables)
        compile_variables["<"] = {"value": source_file.path}
        postprocess_variables = dict(compile_variables)
        compile_variables["cmd_objtool"] = {"value": ""}
        compile_variables["cmd_ld_single"] = {"value": ""}
        name = evaluate(native["arguments"][0], variables, setup.sources).strip() if native else ("as_o_S" if path.endswith(".S") else "cc_s_c" if target.endswith(".s") else "cc_o_c")
        command = value("cmd_" + name, compile_variables, setup.sources).strip()
        flags = _compiler_flags(command, setup.tools["CC"], source_file.path, target)
        if flags == None:
            fail("Expected a single native compiler command for %s: %s" % (target, command))
        flags = _rewrite_flags(flags, generated_root, source, dependencies)

        # LLVM's integrated assembler also searches include directories for
        # .incbin, including inline assembly in C. Preserve Kbuild's generated
        # and source namespaces without a private copy of the source tree.
        flags.extend(["-I" + generated_root, "-I" + source])
        raw = _compile(ctx, target, source_file, flags, compiler_dependencies, direct_dependencies, compiler_inputs.directory_marker, source_tree, source_prerequisites, setup, host = False)
        postprocess = ""
        postprocess_inputs = [raw]
        if target.endswith(".o"):
            if config.make_values.get("CONFIG_OBJTOOL") == "y":
                objtool = dependencies["tools/objtool/objtool"]
                postprocess_variables["objtool"] = {"value": objtool.path}
                postprocess_inputs.append(objtool)

            # Membership and target-specific arguments use the logical Kbuild
            # name. Freeze those decisions before substituting the action path.
            for name in ["objtool-enabled", "objtool-args", "is-single-obj-m"]:
                postprocess_variables[name] = {"value": value(name, postprocess_variables, setup.sources)}
            postprocess_variables["@"] = {"value": outputs[target].path}
            postprocess_variables["tmp-target"] = {"value": outputs[target].path + ".tmp"}
            postprocess = (value("cmd_ld_single", postprocess_variables, setup.sources) + value("cmd_objtool", postprocess_variables, setup.sources)).strip()
        if postprocess:
            run_recipe(
                ctx,
                execution_tools(ctx),
                command = "set -eu\ncp " + shell_quote(raw.path) + " " + shell_quote(outputs[target].path) + "\nchmod u+w " + shell_quote(outputs[target].path) + "\n" + postprocess.lstrip("; "),
                inputs = depset(postprocess_inputs, transitive = [setup.files]),
                outputs = outputs.values(),
                mnemonic = "KbuildObjtool",
                progress_message = "Objtool %s" % target,
            )
        else:
            ctx.actions.symlink(output = outputs[target], target_file = raw)
        return
    if driver == "asm_headers":
        ctx.actions.write(outputs[target], "#include <asm-generic/%s>\n" % target.rsplit("/", 1)[-1])
        return
    codegen(ctx, target, directory, outputs, dependencies, source_tree, config, setup, compiler_inputs, native, ancestry, graph_variables, source_prerequisites, invocation_environment)

# Fixed package fixtures or action helpers, not target-naming macros.
# buildifier: disable=unnamed-macro
def codegen(ctx, target, directory, outputs, dependencies, source_tree, config, setup, compiler_inputs, native = None, ancestry = None, graph_variables = {}, source_prerequisites = {}, invocation_environment = None):
    source = source_tree.source_root
    if native != None:
        native_codegen(ctx, target, outputs, dependencies, source_tree, config, setup, compiler_inputs, native, source_prerequisites)
        return
    variables = _scope(setup, directory, target, "__object__", source, ancestry, graph_variables, invocation_environment = invocation_environment)
    key = "Kbuild" if directory == "." else directory + "/Kbuild"
    if key not in FLAG_PROGRAM["commands"]:
        key = "Makefile" if directory == "." else directory + "/Makefile"
    for path in ["scripts/Makefile.lib", "scripts/Makefile.host", "scripts/Makefile.build", key, "arch/" + config.architecture.srcarch + "/Makefile"]:
        variables = program(FLAG_PROGRAM["commands"].get(path, []), variables, setup.sources, cells = True).variables
    variables["src"] = {"value": source + "/" + directory}
    variables["obj"] = {"value": "__object__/" + directory}
    variables["@"] = {"value": "__object__/" + target}
    name, first, header_guard, filechk = "", "", "", False
    if target.endswith(".asn1.c"):
        name, first = "asn1_compiler", source + "/" + target[:-2]
    elif target.endswith(".tab.c"):
        name, first = "bison", source + "/" + target[:-6] + ".y"
    elif target.endswith(".lex.c"):
        name, first = "flex", source + "/" + target[:-6] + ".l"
    elif target.endswith("oid_registry_data.c"):
        name, first = "build_OID_registry", source + "/include/linux/oid_registry.h"
    elif target == "kernel/config_data":
        name, first, filechk = "cat", "__config__", True
    elif target == "kernel/config_data.gz":
        name, first = "gzip", "__object__/kernel/config_data"
    elif target.endswith("/elfconfig.h"):
        name, first = "elfconfig", "__object__/scripts/mod/empty.o"
    elif target.endswith("/timeconst.h"):
        name, first, filechk = "gentimeconst", source + "/kernel/time/timeconst.bc", True
    elif target.endswith("/cpufeaturemasks.h"):
        name, filechk = "gen_featuremasks", True
    elif target.endswith("offsets.h") or target.endswith("/bounds.h"):
        name, filechk = "offsets", True
        stem = target.rsplit("/", 1)[-1][:-2]
        header_guard = {
            "bounds": "__LINUX_BOUNDS_H__",
            "asm-offsets": "__ASM_OFFSETS_H__",
            "rq-offsets": "__RQ_OFFSETS_H__",
            "devicetable-offsets": "__DEVICETABLE_OFFSETS_H__",
        }.get(stem)
        assembly = {
            "bounds": "kernel/bounds.s",
            "asm-offsets": "arch/" + config.architecture.srcarch + "/kernel/asm-offsets.s",
            "rq-offsets": "kernel/sched/rq-offsets.s",
            "devicetable-offsets": "scripts/mod/devicetable-offsets.s",
        }.get(stem)
        if not assembly:
            fail("Unsupported offset generator: " + target)
        first = "__object__/" + assembly
    elif "/unistd_" in target or "/syscalls_" in target:
        name = "syshdr" if "/unistd_" in target else "systbl"
        first = source + "/arch/" + config.architecture.srcarch + "/entry/syscalls/syscall_" + ("32" if "32" in target and "x32" not in target else "64") + ".tbl"
        key = "arch/" + config.architecture.srcarch + "/entry/syscalls/Makefile"
        variables = program(FLAG_PROGRAM["commands"][key], variables, setup.sources, cells = True).variables
        for assignment in FLAG_PROGRAM["target_assignments"][key]:
            patterns = make_words(evaluate(assignment["targets_expression"], variables, setup.sources))
            if any([match(p, target) != None for p in patterns]):
                n = evaluate(assignment["name_expression"], variables, setup.sources)
                variables[n] = {"value": evaluate(assignment["value_expression"], variables, setup.sources)}
    else:
        fail("No direct codegen lowering for " + target)
    variables["<"] = {"value": first}
    variables["^"] = {"value": first}
    variables["KCONFIG_CONFIG"] = {"value": "__config__"}
    expr = [{"op": "function", "name": "call", "arguments": [
        [{"op": "literal", "value": ("filechk_" if filechk else "cmd_") + name}],
        [{"op": "literal", "value": ""}],
        [{"op": "literal", "value": header_guard or ""}],
    ]}]
    command = evaluate(expr, variables, setup.sources).strip()
    if not command:
        fail("Empty native command for " + target)
    all_files = dict(dependencies)
    all_files.update(outputs)
    for path in sorted(all_files, key = len, reverse = True):
        command = command.replace("__object__/" + path, all_files[path].path)
    command = command.replace("__config__", config.config.path)
    if "__object__" in command:
        fail("Undeclared generated dependency in %s: %s" % (target, command))
    if filechk:
        command = "{ " + command + "; } > " + shell_quote(outputs[target].path)

    # Only executable recipes use the interpreter-bound source tree. Keep
    # its namespace out of compiler flags and compiler input declarations.
    command = command.replace(source, source_tree.root.path)
    run_recipe(
        ctx,
        execution_tools(ctx),
        command = "set -eu\n" + command,
        inputs = depset([config.config] + dependencies.values(), transitive = [source_tree.files, setup.files]),
        outputs = outputs.values(),
        mnemonic = "KbuildGenerate",
        progress_message = "Generate %s" % target,
        env = {"KBUILD_VERBOSE": "0", "srctree": source_tree.root.path},
    )

# Fixed package fixtures or action helpers, not target-naming macros.
# buildifier: disable=unnamed-macro
def native_codegen(ctx, target, outputs, dependencies, source_tree, config, setup, compiler_inputs, native, source_prerequisites):
    source = source_tree.source_root
    variables = program(FLAG_PROGRAM["image_command_helpers"], native["variables"], setup.sources, cells = True).variables
    variables.update(native["variables"])
    for name, tool in setup.tools.items():
        variables[name] = {"value": tool}
    pahole = native.get("pahole")
    if pahole != None:
        variables["PAHOLE"] = {"value": pahole.executable.path}
    prerequisites = []
    for path in native["prerequisites"]:
        if path.startswith("__source__/"):
            prerequisites.append(source + "/" + path[len("__source__/"):])
        elif source + "/" + path in setup.sources:
            prerequisites.append(source + "/" + path)
        else:
            prerequisites.append(path)
    first = prerequisites[0] if prerequisites else ""
    if native.get("first"):
        path = native["first"]
        first = source + "/" + path.removeprefix("__source__/") if path.startswith("__source__/") or source + "/" + path in setup.sources else path
    for name, text in {
        "@": target,
        "@F": target.rsplit("/", 1)[-1],
        "@D": target.rpartition("/")[0] or ".",
        "<": first,
        "^": " ".join(prerequisites),
        "*": native["stem"],
        "real-prereqs": " ".join(prerequisites),
        "depfile": ".native-dependencies.d",
        "tmp-target": ".native-temporary",
        "CONFIG_SHELL": setup.tools["CONFIG_SHELL"],
    }.items():
        variables[name] = {"value": text}
    variables.update(native.get("inherited_variables", {}))
    for assignment in native["assignments"]:
        patterns = make_words(evaluate(assignment["targets_expression"], variables, setup.sources))
        if any([match(p.removeprefix("./"), target) != None for p in patterns]):
            node = dict(assignment)
            node["kind"] = "assignment"
            variables = program([node], variables, setup.sources, cells = True).variables
    arguments = [evaluate(a, variables, setup.sources) for a in native["arguments"]]
    name = arguments[0].strip() if arguments else ""
    for i, argument in enumerate(arguments):
        variables[str(i + 1)] = {"value": argument}
    command_sources = dict(setup.sources)
    command_sources["__shell_commands__"] = True
    if native["function"] == "script":
        lines = []
        for expression in native["scripts"]:
            text = evaluate(expression, variables, command_sources).strip()
            body = text.lstrip("@+-")
            modifiers = text[:len(text) - len(body)]
            lines.append(shell_quote(setup.tools["CONFIG_SHELL"]) + " -c " + shell_quote(body) + (" || :" if "-" in modifiers else ""))
        command = "\n".join(lines) + "\ntouch " + shell_quote(target)
    else:
        command = value(("filechk_" if native["function"] == "filechk" else "cmd_") + name, variables, command_sources).strip()
    if native != None and native.get("final_link"):
        command = shell_quote(setup.tools["CONFIG_SHELL"]) + " " + ctx.file._link_script.path + " " + " ".join([shell_quote(setup.tools["LD"]), shell_quote(value("KBUILD_LDFLAGS", variables, setup.sources)), shell_quote(value("LDFLAGS_vmlinux", variables, setup.sources)), shell_quote(target)])
    if native.get("postlink") != None:
        post = native["postlink"]
        post_variables = dict(variables)
        post_variables.update(post["variables"])
        for key in ["@", "@F", "@D", "<", "^", "*", "real-prereqs", "CONFIG_SHELL"] + list(setup.tools):
            post_variables[key] = variables[key]
        if pahole != None:
            post_variables["PAHOLE"] = variables["PAHOLE"]
        post_name = evaluate(post["arguments"][0], post_variables, setup.sources).strip()
        command += "\n" + value("cmd_" + post_name, post_variables, command_sources)
    if not command:
        fail("No exported native command %r for %s" % (name, target))
    mapping = dict(dependencies)
    mapping.update(outputs)
    for path in sorted(mapping, key = len, reverse = True):
        command = command.replace("__object__/" + path, path)
    command = command.replace("__source__/", source + "/")
    command = command.replace("__object__/", "").replace("__object__", ".")
    if native["function"] == "filechk":
        command = "{ " + command + "; } > " + shell_quote(target)
    source_path = first.removeprefix(source + "/")
    source_file = dependencies.get(source_path, source_tree.logical.get(source_path))
    flags = _compiler_flags(command, setup.tools["CC"], first, target)

    # Native preprocessing and compiler-emitted assembly use the same Bazel
    # dependency discovery as ordinary objects. Recognize the command shape,
    # rather than naming linker scripts or architecture-specific targets.
    if flags != None and source_file != None:
        if len(outputs) != 1:
            fail("Native compiler command must have one output: " + target)
        compiler_dependencies = {p: f for p, f in dependencies.items() if p != "tools/objtool/objtool"}
        direct_dependencies = {p: f for p, f in compiler_inputs.direct.items() if p != "tools/objtool/objtool"}
        compiler_dependencies.update(compiler_inputs.config)
        direct_dependencies.update(compiler_inputs.config)
        flags = _rewrite_flags(flags, compiler_inputs.root, source, compiler_dependencies)
        flags.extend(["-I" + compiler_inputs.root, "-I" + source])
        compiled = _compile(ctx, target, source_file, flags, compiler_dependencies, direct_dependencies, compiler_inputs.directory_marker, source_tree, source_prerequisites, setup, host = False)
        ctx.actions.symlink(output = outputs[target], target_file = compiled)
        return
    if native.get("compile"):
        fail("Native compiler recipe needs explicit lowering for dependency discovery: %s: %s" % (target, command))
    stage = ctx.actions.declare_directory(outputs.values()[0].basename + ".native-inputs/" + target)
    manifest = ctx.actions.declare_file(stage.basename + ".mapping/" + target + ".json")
    arranged = {p: f.path for p, f in dependencies.items()}
    arranged["include/config/auto.conf"] = config.config.path
    arranged[".config"] = config.config.path
    ctx.actions.write(manifest, json.encode(arranged))
    ctx.actions.run(
        executable = execution_tools(ctx).python,
        arguments = [ctx.file._materialize.path, manifest.path, stage.path],
        inputs = depset([ctx.file._materialize, manifest, config.config] + dependencies.values(), transitive = [execution_tools(ctx).files]),
        outputs = [stage],
        mnemonic = "KbuildNativeInputs",
    )

    exports = dict(setup.tools)
    if pahole != None:
        exports["PAHOLE"] = pahole.executable.path
    for name in ["KBUILD_VMLINUX_LIBS", "KBUILD_LDS", "KERNELRELEASE", "SRCARCH"] + (FLAG_PROGRAM["image_link_exports"] if native.get("final_link") else []):
        if name not in ("KBUILD_VMLINUX_LIBS", "KBUILD_LDS", "KERNELRELEASE", "SRCARCH") and name not in variables.get("__exports__", {}):
            continue
        exports[name] = value(name, variables, setup.sources).replace("__source__", source).replace("__object__", ".")
    exports.update({"objtree": ".", "CONFIG_SHELL": setup.tools["CONFIG_SHELL"], "KBUILD_BUILD_USER": "bazel", "KBUILD_BUILD_HOST": "bazel"})
    command = "\n".join(["export " + name + "=" + shell_quote(val) for name, val in exports.items()]) + "\n" + command
    command = command.replace(source, source_tree.root.path)

    # Driver-based native links must find the declared linker beside the
    # selected tools, even when the compiler is a multicall-binary symlink.
    linker_directory = setup.driver_tools
    driver_search = shell_quote(linker_directory) if linker_directory.startswith("/") else '"$exec_root"/' + shell_quote(linker_directory)
    command = "export COMPILER_PATH=" + driver_search + "\n" + command
    script = """set -eu
exec_root="$PWD"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
cp -R -L -p %s/. "$work/"
chmod -R u+w "$work"
cd "$work"
%s
%s
%s
""" % (shell_quote(stage.path), _stage_links(source_tree.files.to_list() + setup.files.to_list() + [ctx.file._link_script] + (pahole.files.to_list() if pahole != None else [])), "mkdir -p " + " ".join([shell_quote(p.rpartition("/")[0] or ".") for p in outputs]), command)
    for path, output in outputs.items():
        script += "cp %s %s\n" % (shell_quote(path), shell_quote(output.path))
    run_recipe(
        ctx,
        execution_tools(ctx),
        command = script,
        inputs = depset([stage, config.config, ctx.file._link_script] + dependencies.values(), transitive = [source_tree.files, setup.files] + ([pahole.files] if pahole != None else [])),
        tools = [pahole.files_to_run] if pahole != None else [],
        outputs = outputs.values(),
        mnemonic = "KbuildGenerate",
        progress_message = "Native generator %s" % target,
        env = {"KBUILD_VERBOSE": "0", "srctree": source_tree.root.path},
    )
