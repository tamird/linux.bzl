"""Resolve configured prerequisite programs and register one action per artifact."""

load("@linux.bzl//execution_tools:defs.bzl", "EXECUTION_TOOLS_TYPE")
load("@rules_cc//cc:find_cc_toolchain.bzl", "use_cc_toolchain")
load(":commands.bzl", "register", "shell_words")
load(":expressions.bzl", "evaluate", "make_words", "match", "program", "source_index")
load(":flag_program.bzl", "FLAG_PROGRAM")
load(":host_tools.bzl", "KernelObjtoolInfo", "KernelResolveBtfidsInfo")
load(":kbuild.bzl", "KbuildFileInfo")
load(":resolved.bzl", "KbuildCatalogInfo")
load(":rules.bzl", "KernelConfigInfo")
load(":source_tree.bzl", "KernelSourceTreeInfo")
load(":toolchain.bzl", "KernelToolchainInfo", "image_evaluation_setup")

KernelBuildInfo = provider(
    "Declared artifacts of one configured kernel build.",
    fields = ["artifacts", "config", "architecture"],
)

KernelBuildRequestInfo = provider(
    "Configured native products requested from the shared image action owner.",
    fields = ["config", "catalog", "roots", "architecture_roots", "modules", "required_options", "qualification"],
)

KernelBuildQualificationInfo = provider(
    "Declared inputs for opt-in compiler parity and diagnostic qualification.",
    fields = ["diagnostic_target", "diagnostic_source", "diagnostic_driver"],
)

def _catalog_impl(ctx):
    programs = {}
    for target in ctx.attr.makefiles:
        info = target[KbuildFileInfo]
        parent, _, name = info.path.rpartition("/")
        if name == "Kbuild" or (name == "Makefile" and parent not in programs):
            programs[parent if parent else "."] = info.statements
    actions = json.decode(ctx.attr.actions)
    return [KbuildCatalogInfo(actions = actions, programs = programs, aliases = {out: p for p, d in actions.items() for out in d["outs"]})]

_catalog = rule(implementation = _catalog_impl, attrs = {
    "actions": attr.string(mandatory = True),
    "makefiles": attr.label_list(providers = [KbuildFileInfo]),
})

def kbuild_catalog(name, actions, makefiles):
    _catalog(name = name, actions = json.encode(actions), makefiles = makefiles)

def _value(name, variables, sources):
    return evaluate([{"op": "reference", "name": [{"op": "literal", "value": name}]}], variables, sources)

def _normalize(path):
    parts = []
    for p in path.split("/"):
        if p == "..":
            if not parts:
                fail("Kbuild dependency escapes its tree: " + path)
            parts.pop()
        elif p and p != ".":
            parts.append(p)
    return "/".join(parts)

def _exported_environment(variables, sources):
    environment = {name: {"value": _value(name, variables, sources)} for name in variables.get("__exports__", {}) if name in variables}
    environment["__exports__"] = {name: True for name in environment}
    return environment

def _build_dispatch(words):
    if len(words) < 3 or words[0] != "__make_dispatch__" or not words[1].startswith("__build_dispatch__="):
        return None
    if any(["=" in word or word.startswith("-") for word in words[2:]]):
        fail("Unsupported recursive Kbuild arguments: " + str(words))
    return struct(directory = _normalize(words[1].split("=", 1)[1]), goals = words[2:])

def _enter_invocation(directory, variables, visibility, state):
    invocation = struct(environment = _exported_environment(variables, state["sources"]), visibility = {p: True for p in visibility}.keys())
    if (directory in state["contexts"] or directory in state["invocations"]) and state["invocations"].get(directory) != invocation:
        fail("Kbuild producer reached %s with a different invocation context" % directory)
    state["invocations"][directory] = invocation

def _context(directory, state):
    if directory.startswith("arch/") and directory.split("/")[1] not in (state["arch"], state["setup"].architecture.header_arch):
        fail("Kbuild architecture mismatch: " + directory)
    if directory not in state["contexts"]:
        prefix = "" if directory == "." else directory + "/"
        programs = FLAG_PROGRAM["image_programs"]
        statements = programs.get(prefix + "Kbuild", programs.get(prefix + "Makefile"))
        if statements == None:
            fail("No exported Make program for " + directory)
        invocation = state["invocations"].get(directory)
        initial = dict(state["setup"].variables if directory == "." else invocation.environment if invocation != None else state["environment"])
        if directory == ".":
            for name in ["core-y", "libs-y", "drivers-y", "drivers-m"]:
                logical = _value(name, state["setup"].variables, state["setup"].sources).replace("__object__/", "").replace("__object__", ".")
                initial[name] = {"value": logical}
        builtin, modorder = state["directory_modes"].get(directory, (True, bool(state["modules"])))
        for k, v in {
            "obj": directory,
            "objtree": ".",
            "srctree": "__source__",
            "VPATH": "__source__",
            "building_out_of_srctree": "1",
            "srcroot": "__source__",
            "SRCARCH": state["arch"],
            "ARCH": state["setup"].architecture.arch,
            "KCONFIG_CONFIG": ".config",
            "need-builtin": "1" if builtin else "",
            "need-modorder": "1" if modorder else "",
            "KBUILD_BUILTIN": "1",
            "KBUILD_MODULES": "y" if state["modules"] else "",
        }.items():
            initial[k] = {"value": v}
        initial["__config__"] = state["config"]
        if directory in state["dtbs_lists"]:
            initial["need-dtbslist"] = {"value": "1"}
        initial = program(FLAG_PROGRAM["image_build_init"], initial, state["sources"], cells = True).variables
        local_context = None
        graph_variables = {}

        # Setup already evaluated the architecture include. Replaying it here
        # repeats += assignments (including platform load addresses).
        bodies = [FLAG_PROGRAM["image_roots"], FLAG_PROGRAM["graph_init"], statements, FLAG_PROGRAM["graph_helpers"]] if directory == "." else [FLAG_PROGRAM["image_common"], FLAG_PROGRAM["graph_init"], statements, FLAG_PROGRAM["graph_helpers"], FLAG_PROGRAM["image_helpers"]]
        for body in bodies:
            sources = dict(state["sources"])
            if body == FLAG_PROGRAM["image_arch"]:
                sources["__includes__"] = FLAG_PROGRAM["image_graph_programs"]
            result = program(body, initial, sources, cells = True)
            initial = result.variables
            if body == statements:
                local_context = result
            if body == FLAG_PROGRAM["graph_helpers"]:
                for pattern in FLAG_PROGRAM["graph_variables"]:
                    if pattern.endswith("*"):
                        graph_variables.update({name: cell for name, cell in initial.items() if name.startswith(pattern[:-1])})
                    elif pattern in initial:
                        graph_variables[pattern] = initial[pattern]

        # Preserve rules from the single local evaluation; reevaluating the
        # Makefile repeats += and path-prefix assignments.
        context = local_context
        dtb_rules = []
        if directory in state["dtbs_lists"] or any([p.endswith((".dtb.o", ".dtbo.o")) for p in make_words(_value("real-obj-y", initial, state["sources"]))]):
            dtbs = program(FLAG_PROGRAM["image_dtbs"], initial, state["sources"], cells = True)
            initial = dtbs.variables
            dtb_rules = dtbs.rules
        implicit = program(FLAG_PROGRAM["image_implicit_rules"], initial, state["sources"], cells = True)
        state["contexts"][directory] = struct(variables = initial, graph_variables = graph_variables, rules = context.rules + dtb_rules + implicit.rules, target_assignments = context.target_assignments)
        builtins = make_words(_value("subdir-builtin", initial, state["sources"]))
        modorders = make_words(_value("subdir-modorder", initial, state["sources"]))
        for child in make_words(_value("subdir-ym", initial, state["sources"])):
            child = _normalize(child)
            if child in state["parents"] and state["parents"][child] != directory:
                fail("Kbuild directory %s has conflicting parents %s and %s" % (child, state["parents"][child], directory))
            state["parents"][child] = directory
            state["directory_modes"][child] = (child + "/built-in.a" in builtins, child + "/modules.order" in modorders)
    return state["contexts"][directory]

def _valid_rule(rule, stem, context, state):
    if not rule.get("implicit") and not any(["%" in p for p in rule["targets"]]):
        return True
    prerequisites = [p for p in make_words(rule["prerequisites"]) if p not in ("FORCE", "|")]
    if not prerequisites:
        return True
    pending = [(_normalize(prerequisites[0].replace("%", stem, 1)), [])]
    seen = {}

    # Native pattern producers may chain, e.g. %.pi.o -> %.o -> %.S.
    for _ in range(10000):
        if not pending:
            return False
        path, chain = pending.pop()
        source_path = path.startswith("__source__/")
        path = path.removeprefix("__source__/")
        key = path + str(chain)
        if key in seen:
            continue
        seen[key] = True
        if path in state["source_files"] or path in state["catalog"].aliases or path in state["known_products"]:
            return True
        for index, candidate in enumerate(context.rules):
            if index in chain or not candidate.get("recipes"):
                continue
            for pattern in candidate["targets"]:
                candidate_stem = match(_normalize(pattern), path)
                if candidate_stem == None:
                    continue
                if "%" not in pattern:
                    return True
                if source_path:
                    continue
                inputs = [p for p in make_words(candidate["prerequisites"]) if p not in ("FORCE", "|")]
                if not inputs:
                    return True
                pending.append((_normalize(inputs[0].replace("%", candidate_stem, 1)), chain + [index]))
    fail("Native pattern search exceeded 10000 prerequisites")

def _rules_for_path(path, context, state):
    explicit, candidate, shortest = [], None, None
    for rule in context.rules:
        for pattern in rule["targets"]:
            stem = match(_normalize(pattern), path)
            if stem == None:
                continue
            if "%" not in pattern:
                explicit.append(rule)
            elif rule.get("recipes") and _valid_rule(rule, stem, context, state) and (shortest == None or len(stem) < shortest):
                candidate, shortest = rule, len(stem)

    # GNU Make merges explicit prerequisites but selects one pattern recipe,
    # preferring the most specific applicable pattern (shortest stem).
    return explicit + ([candidate] if candidate != None and not any([r.get("recipes") for r in explicit]) else [])

def _target_scope(path, directory, state):
    """Keep the first-reached scope within its native Make invocation."""
    context = _context(directory, state)
    owner, inherited = state["target_scopes"].get(path, (None, {}))
    inherited = inherited if owner == directory else {}
    variables = dict(context.variables)
    variables.update(inherited)
    descendants = dict(inherited)
    for patterns_first in [True, False]:
        for assignment in context.target_assignments:
            patterns = make_words(evaluate(assignment["targets_expression"], context.variables, state["sources"]))
            if any(["%" in p for p in patterns]) != patterns_first:
                continue
            if any([match(_normalize(p), path) != None for p in patterns]):
                node = dict(assignment)
                node["kind"] = "assignment"
                variables = program([node], variables, state["sources"], cells = True).variables
                name = evaluate(node["name_expression"], variables, state["sources"])
                if not node.get("private"):
                    descendants[name] = variables[name]
                    if node.get("export"):
                        descendants["__exports__"] = variables["__exports__"]
    return struct(variables = variables, inherited = inherited, descendants = descendants)

def _local_recipe(path, directory, state):
    context = _context(directory, state)
    scope = _target_scope(path, directory, state)
    prerequisites = []
    for rule in _rules_for_path(path, context, state):
        for pattern in rule["targets"]:
            stem = match(_normalize(pattern), path)
            if stem == None or not _valid_rule(rule, stem, context, state):
                continue
            tail = rule["prerequisites"].split(";", 1)[0]
            if ":" in tail:
                target_pattern, tail = tail.split(":", 1)
                stem = match(_normalize(target_pattern.strip()), path)
                if stem == None:
                    continue
            prerequisites.extend([_normalize(p.replace("%", stem, 1)) for p in make_words(tail) if p not in ("FORCE", "|")])
    prerequisites = {p: True for p in prerequisites}.keys()
    for rule in _rules_for_path(path, context, state):
        for pattern in rule["targets"]:
            stem = match(_normalize(pattern), path)
            if stem == None or not rule.get("recipes") or not _valid_rule(rule, stem, context, state):
                continue
            for terms in rule["recipes"]:
                for term in terms:
                    if term.get("op") != "function" or term.get("name") != "call":
                        continue
                    function = evaluate(term["arguments"][0], context.variables, state["sources"]).strip()
                    if function in ("if_changed", "if_changed_dep", "if_changed_rule", "if_changed_except", "cmd", "filechk"):
                        tail = rule["prerequisites"].split(";", 1)[0]
                        if ":" in tail:
                            target_pattern, tail = tail.split(":", 1)
                            stem = match(_normalize(target_pattern.strip()), path)
                            if stem == None:
                                continue
                        own_prerequisites = [_normalize(p.replace("%", stem, 1)) for p in make_words(tail) if p not in ("FORCE", "|")]
                        return {
                            "directory": directory,
                            "first": own_prerequisites[0] if own_prerequisites else "",
                            "function": function,
                            "arguments": term["arguments"][1:],
                            "variables": context.variables,
                            "prerequisites": prerequisites,
                            "stem": stem,
                            "assignments": context.target_assignments,
                            "inherited_variables": scope.inherited,
                        }
            variables = dict(scope.variables)
            for name, text in {"MAKE": "__make_dispatch__", "build": "__build_dispatch__", "Q": "", "@": path}.items():
                variables[name] = {"value": text}
            for terms in rule["recipes"]:
                text = evaluate(terms, variables, state["sources"]).lstrip("@+- ")
                if not text.startswith("__make_dispatch__"):
                    continue
                dispatch = _build_dispatch(shell_words(text))
                if dispatch == None or dispatch.goals != [path]:
                    fail("Unsupported recursive producer for %s: %s" % (path, text))
                return {"dispatch": dispatch.directory, "prerequisites": prerequisites, "variables": scope.variables}
    return None

def _recipe(path, directory, state):
    """Follow recursive producers while retaining one artifact identity."""
    prerequisites, visited = [], {}
    for _ in range(10000):
        if directory in visited:
            fail("Recursive Kbuild producer cycle for %s: %s" % (path, visited.keys()))
        visited[directory] = True
        native = _local_recipe(path, directory, state)
        if native == None:
            return None
        prerequisites.extend(native["prerequisites"])
        if "dispatch" not in native:
            native["prerequisites"] = {p: True for p in prerequisites}.keys()
            return native
        child = native["dispatch"]
        parent = state["invocations"].get(directory)
        visibility = (parent.visibility if parent != None else []) + native["prerequisites"]
        _enter_invocation(child, native["variables"], visibility, state)
        directory = child
    fail("Recursive Kbuild producer exceeded 10000 invocations: " + path)

def _architecture_invocations(roots, state):
    """Retain the completed parent phase visible to a selected boot invocation."""
    if not roots:
        return
    context = state["contexts"]["@architecture"]
    selected = {}
    for rule in context.rules:
        if not any([term.get("op") == "reference" and term.get("name") == [{"op": "literal", "value": "MAKE"}] for terms in rule.get("recipes", []) for term in terms]):
            continue
        for goal in rule["targets"]:
            if "%" in goal:
                continue
            variables = dict(_target_scope(goal, "@architecture", state).variables)
            variables["@"] = {"value": goal}
            preceding = []
            for terms in rule["recipes"]:
                text = evaluate(terms, variables, state["sources"]).lstrip("@+- ")
                words = shell_words(text, allow_recipe = True) if text.startswith("__make_dispatch__") else None
                if words != None and any([path in roots for path in words[2:]]):
                    dispatch = _build_dispatch(words)
                    if dispatch == None:
                        fail("Unsupported architecture boot dispatch %s: %s" % (goal, text))
                    if preceding:
                        fail("Unsupported commands before architecture boot dispatch %s: %s" % (goal, preceding))
                    prerequisites = [p for r in _rules_for_path(goal, context, state) for p in make_words(r["prerequisites"]) if p not in ("FORCE", "|")]
                    _enter_invocation(dispatch.directory, variables, prerequisites, state)
                    for path in dispatch.goals:
                        if path in selected and selected[path] != dispatch.directory:
                            fail("Multiple architecture invocations produce " + path)
                        selected[path] = dispatch.directory
                    break
                if text and text != ":":
                    preceding.append(text)

def _postlink(path, state):
    post = state["contexts"].get("@postlink")
    if post == None:
        return struct(commands = [], outputs = [], prerequisites = [])
    native = _local_recipe(path, "@postlink", state)
    prerequisites = native["prerequisites"] if native != None else []
    variables = dict(_target_scope(path, "@postlink", state).variables)
    for name, text in {
        "@": path,
        "@F": path.rsplit("/", 1)[-1],
        "@D": path.rpartition("/")[0] or ".",
        "<": native["first"] if native != None else "",
        "^": " ".join(prerequisites),
        "*": native["stem"] if native != None else "",
        "real-prereqs": " ".join(prerequisites),
    }.items():
        variables[name] = {"value": text}
    commands, outputs = [], []
    for rule in _rules_for_path(path, post, state):
        for terms in rule.get("recipes", []):
            significant = [term for term in terms if term.get("op") != "literal" or term["value"].strip() not in ("", "@", "+")]
            if len(significant) == 1 and significant[0].get("op") == "literal" and significant[0]["value"].lstrip("@+").strip() == "true":
                continue
            if len(significant) != 1 or significant[0].get("name") != "call":
                fail("Unsupported native postlink recipe: " + str(terms))
            arguments = significant[0]["arguments"]
            function = evaluate(arguments[0], variables, state["sources"]).strip()
            if function not in ("cmd", "if_changed", "if_changed_dep") or len(arguments) < 2:
                fail("Unsupported native postlink command: " + str(terms))
            name = evaluate(arguments[1], variables, state["sources"]).strip()
            commands.append({"arguments": arguments[1:], "variables": variables, "own_automatic_variables": True})
            outputs.extend([_normalize(evaluate(expr, variables, state["sources"])) for expr in FLAG_PROGRAM["image_postlink_outputs"][state["postlink_file"]].get(name, [])])
    return struct(commands = commands, outputs = outputs, prerequisites = prerequisites)

def _link_descriptor(path, state):
    if path in ("built-in-fixup.a", "vmlinux.a"):
        native = _recipe(path, "@archive", state)
        if native == None:
            fail("No native kernel archive producer for " + path)
        dependencies = [p for p in native["prerequisites"] if p not in state["empty_archive_controls"]]
        return {"target": path, "directory": ".", "outs": [path], "deps": dependencies, "kind": "generate", "driver": "link", "native": native}
    variables = dict(_context(".", state).variables)
    for name, val in {"objtree": ".", "srctree": "__source__", "srcroot": "__source__"}.items():
        variables[name] = {"value": val}
    libs = make_words(_value("KBUILD_VMLINUX_LIBS", variables, state["sources"]))
    symdump = "Module.symvers" if state["modules"] else "vmlinux.symvers"
    modpost_outputs = [symdump, ".vmlinux.export.c"] + [p[:-3] + ".mod.c" for p in state["modules"]]
    modpost_deps = ["vmlinux.o", "scripts/mod/modpost"]
    if state["modules"]:
        modpost_deps += ["modules.order"] + [p[:-3] + suffix for p in state["modules"] for suffix in [".o", ".mod"]]
    stages = {
        "vmlinux.o": ("vmlinux_o", "ld_vmlinux.o", ["vmlinux.a"] + libs, [path]),
        symdump: ("modpost", "modpost", modpost_deps, modpost_outputs),
        ".vmlinux.export.c": ("modpost", "modpost", modpost_deps, modpost_outputs),
    }
    final_link = path == state["final_link"]
    if path in ["vmlinux", state["final_link"]]:
        recipe = _recipe(path, "@vmlinux", state)
        if recipe == None:
            fail("No native final-image producer for " + path)
        command = evaluate(recipe["arguments"][0], recipe["variables"], state["sources"]).strip()
        deps = list(recipe["prerequisites"])
        if final_link:
            deps.extend([symdump, "init/version-timestamp.o"])
            if state["pahole"] != None:
                deps.extend(make_words(_value("RESOLVE_BTFIDS", variables, state["sources"])))
        stages[path] = ("vmlinux", command, deps, [path, "System.map"] if final_link else [path])
    for module in state["modules"]:
        stages[module[:-3] + ".mod.c"] = ("modpost", "modpost", modpost_deps, modpost_outputs)
    if path not in stages:
        return None
    driver, command, deps, outs = stages[path]
    if final_link:
        post = state["postlinks"][path]
        outs = outs + post.outputs
        deps.extend(post.prerequisites)
        scripts = _context("scripts", state)
        deps.extend(["scripts/" + name for name in make_words(_value("hostprogs-always-y", scripts.variables, state["sources"]))])
    variables["KBUILD_MODULES"] = {"value": "y" if state["modules"] else ""}
    variables = program(FLAG_PROGRAM["image_link"][driver], variables, state["sources"], cells = True).variables
    if final_link and state["pahole"] != None:
        # Bazel schedules the action; keep pahole's internal worker count
        # explicit instead of passing the native Makefile's empty -j argument.
        variables["MAKEFLAGS"] = {"value": "-j1"}
        variables = program(FLAG_PROGRAM["image_common"] + FLAG_PROGRAM["image_btf"], variables, state["sources"], cells = True).variables
    if driver == "modpost":
        if _value("missing-input", variables, state["sources"]):
            fail("Native modpost has a missing declared input: " + _value("missing-input", variables, state["sources"]))
        if _value("output-symdump", variables, state["sources"]).strip() != symdump:
            fail("Unexpected native modpost output: " + _value("output-symdump", variables, state["sources"]))
        deps.extend(make_words(_value("modpost-deps", variables, state["sources"])))
    native = {
        "function": "cmd",
        "arguments": [[{"op": "literal", "value": command}]],
        "variables": variables,
        "prerequisites": deps,
        "stem": "",
        "assignments": [],
        "final_link": final_link,
        "postlink": state["postlinks"][path].commands if final_link else [],
        "pahole": state["pahole"] if final_link else None,
    }
    return {"target": outs[0], "directory": ".", "outs": outs, "deps": deps, "kind": "generate", "driver": "link", "native": native}

def _prepare_contexts(state, arch_context):
    """Resolve architecture preparation producers from the same native programs."""
    root = _context(".", state)
    state["bootstrap"] = make_words(_value("targets", root.variables, state["sources"]))
    state["preparation"] = []
    state["preparation_checks"] = {}
    state["early_preparation"] = []
    state["preparation_assembly"] = {}
    state["late_preparation"] = {}
    state["late_directories"] = {}
    for name in ["bounds-file", "timeconst-file", "offsets-file", "rq-offsets-file"]:
        state["preparation"].extend(make_words(_value(name, root.variables, state["sources"])))
    state["header_owners"] = {p: "." for p in state["preparation"]}
    for arch in {a: True for a in [state["arch"], state["setup"].architecture.header_arch]}:
        for uapi in ["", "uapi/"]:
            directory = "arch/" + arch + "/include/generated/" + uapi + "asm"
            variables = dict(state["setup"].variables)
            for name, text in {"obj": directory, "SRCARCH": arch, "srctree": "__source__", "srcroot": "__source__", "generic": "include/" + uapi + "asm-generic"}.items():
                variables[name] = {"value": text}
            header = program(FLAG_PROGRAM["image_asm_headers"], variables, state["sources"], cells = True)
            state["contexts"][directory] = header
            for path in make_words(_value("generic-y", header.variables, state["sources"]) + " " + _value("syscall-y", header.variables, state["sources"])):
                state["header_owners"][path] = directory
                state["preparation"].append(path)

    # Architecture Makefiles can declare generated headers outside their own
    # directory. Preserve their rule scope, rather than guessing path owners.
    state["contexts"]["@architecture"] = arch_context
    for rule in arch_context.rules:
        for path in rule["targets"]:
            if "/include/generated/" in path and "%" not in path and rule.get("recipes"):
                state["header_owners"][path] = "@architecture"
                state["preparation"].append(path)

    # Lower native architecture preparation dispatches and expand phony goals.
    work = [(arch_context, "@architecture", phase, phase == "prepare") for phase in ["archscripts", "archheaders", "archprepare", "prepare"]]
    seen = {}
    for _ in range(10000):
        if not work:
            break
        context, directory, goal, late = work.pop()
        if goal == "prepare0":
            continue
        key = directory + ":" + goal
        if key in seen:
            continue
        seen[key] = True
        matching = [r for r in context.rules if goal in r["targets"]]
        if ("/" in goal or "." in goal) and goal not in ("scripts_basic",) and goal not in state["source_files"]:
            for rule in matching:
                for prerequisite in make_words(rule["prerequisites"]):
                    if prerequisite.endswith(".s") and prerequisite not in state["source_files"]:
                        state["bootstrap"].append(prerequisite)
                        state["preparation_assembly"][prerequisite] = True
                        state["header_owners"][prerequisite] = directory
            state["header_owners"][goal] = directory
            state["preparation"].append(goal)
            if late:
                state["late_preparation"][goal] = True
            else:
                state["early_preparation"].append(goal)
            continue
        for r in matching:
            scripts = [expression for expression in r.get("recipes", []) if evaluate(expression, context.variables, state["sources"]).strip() not in ("", "@:", ":")]
            if scripts and all([not evaluate(expression, context.variables, state["sources"]).lstrip("@+- ").startswith("__make_dispatch__") for expression in scripts]):
                # Native phony preparation checks are real actions. Their
                # success marker gives subsequent compile actions an ordering
                # edge without trying to interpret shell syntax in analysis.
                state["preparation_checks"][goal] = {
                    "target": goal,
                    "directory": directory,
                    "outs": [goal],
                    "deps": [d for d in make_words(r["prerequisites"]) if d not in ("FORCE", "scripts_basic")],
                    "kind": "generate",
                    "driver": "build",
                    "native": {
                        "function": "script",
                        "scripts": scripts,
                        "arguments": [],
                        "variables": context.variables,
                        "prerequisites": [],
                        "stem": "",
                        "assignments": [],
                    },
                }
                state["preparation"].append(goal)
                if not late:
                    state["early_preparation"].append(goal)
                continue
            for dependency in make_words(r["prerequisites"]):
                if dependency not in ("FORCE", "scripts_basic"):
                    work.append((context, directory, dependency, late))
            for expression in r.get("recipes", []):
                text = evaluate(expression, context.variables, state["sources"]).strip()
                if not text or text in ("@:", ":"):
                    continue
                words = shell_words(text)
                if len(words) >= 4 and words[:3] == ["__make_dispatch__", "-f", "__source__/Makefile"]:
                    overrides = {word.split("=", 1)[0]: word.split("=", 1)[1] for word in words[3:] if "=" in word}
                    if overrides.keys() != ["ARCH"]:
                        fail("Unsupported native top-level preparation overrides: " + text)
                    arch = overrides["ARCH"]
                    variables = dict(context.variables)
                    variables.update({"ARCH": {"value": arch}, "SRCARCH": {"value": arch}})
                    child = program(FLAG_PROGRAM["image_programs"]["arch/" + arch + "/Makefile"], variables, state["sources"], cells = True)
                    child_directory = "@architecture:" + arch
                    state["contexts"][child_directory] = child
                    for child_goal in words[3:]:
                        if "=" not in child_goal and child_goal != "asm-generic":
                            work.append((child, child_directory, child_goal, late))
                    continue
                dispatch = _build_dispatch(words)
                if dispatch == None:
                    fail("Unsupported native architecture preparation dispatch: " + text)
                child = dispatch.directory
                child_context = _context(child, state)
                if late:
                    state["late_directories"][child] = True
                for child_goal in dispatch.goals:
                    hostnames = make_words(_value("hostprogs", child_context.variables, state["sources"]) + " " + _value("hostprogs-always-y", child_context.variables, state["sources"]))
                    if child_goal in hostnames:
                        state["preparation"].append(child + "/" + child_goal)
                    else:
                        work.append((child_context, child, child_goal, late))
    if work:
        fail("Architecture preparation graph exceeded 10000 goals")
    state["preparation"] = {p: True for p in state["preparation"]}.keys()

def _module_order_variables(state):
    variables = dict(state["setup"].variables)
    variables["single-ko"] = {"value": " ".join(state["modules"])}
    variables["Q"] = {"value": ""}
    return variables

def _describe(path, state, owner = None):
    path = state["postlink_outputs"].get(path, path)
    if path in state["preparation_checks"]:
        return state["preparation_checks"][path]
    if path == "System.map":
        return _link_descriptor(state["final_link"], state)
    link = _link_descriptor(path, state) if path in ["built-in-fixup.a", "vmlinux.a", "vmlinux.o", "vmlinux.symvers", "Module.symvers", ".vmlinux.export.c", "vmlinux.unstripped", "vmlinux"] or path in [m[:-3] + ".mod.c" for m in state["modules"]] else None
    if link:
        return link
    if state["modules"] and path == "modules.order":
        variables = _module_order_variables(state)
        return {
            "target": path,
            "directory": ".",
            "outs": [path],
            "deps": [],
            "kind": "generate",
            "driver": "link",
            "native": {"function": "script", "scripts": FLAG_PROGRAM["image_modules_order"], "arguments": [], "variables": variables, "prerequisites": [], "stem": "", "assignments": []},
        }
    if path in state["module_final"]:
        native = _recipe(path, "@modfinal", state)
        if native == None:
            fail("No native module finalization producer for " + path)
        compiling = path.endswith(".o")
        native["phase"] = "modfinal"
        dependencies = list(native["prerequisites"])
        outputs = [path]
        if compiling:
            dependencies.extend(state["preparation"])
            dependencies.extend(["include/generated/uapi/linux/version.h", "include/generated/utsrelease.h", "include/generated/compile.h", "include/generated/utsversion.h"])
        else:
            post = state["postlinks"][path]
            native["postlink_lowered"] = True
            native["postlink"] = list(post.commands)
            outputs.extend(post.outputs)
            dependencies.extend(post.prerequisites)
            if state["config"].get("CONFIG_DEBUG_INFO_BTF_MODULES") == "y":
                native["pahole"] = state["pahole"]
                native["postlink"].append({
                    "arguments": [[{"op": "literal", "value": "btf_ko"}]],
                    "variables": native["variables"],
                })
                dependencies.append("tools/bpf/resolve_btfids/resolve_btfids")
        return {"target": path, "directory": ".", "outs": outputs, "deps": dependencies, "kind": "compile" if compiling else "generate", "driver": "link", "native": native}
    if path == "include/generated/utsversion.h":
        return {"target": path, "directory": "init", "outs": [path], "deps": [], "kind": "generate", "driver": "build", "native": _recipe(path, "init", state)}
    header_commands = {"include/generated/uapi/linux/version.h": "version.h", "include/generated/utsrelease.h": "utsrelease.h", "include/generated/compile.h": "compile.h"}
    if path in header_commands:
        command = header_commands[path]
        return {
            "target": path,
            "directory": ".",
            "outs": [path],
            "deps": [],
            "kind": "generate",
            "driver": "build",
            "native": {"function": "filechk", "arguments": [[{"op": "literal", "value": command}]], "variables": state["setup"].variables, "prerequisites": [], "stem": "", "assignments": []},
        }
    if path.endswith("/built-in.a") or path == "built-in.a" or path.endswith("/lib.a"):
        directory = path.rpartition("/")[0] or "."
        variables = _context(directory, state).variables
        members = make_words(_value("lib-y" if path.endswith("/lib.a") else "real-obj-y", variables, state["sources"]))

        # Make automatic $^ (and therefore real-prereqs) keeps the first
        # occurrence of a prerequisite, even when multiple options add it.
        members = {_normalize(p): True for p in members}.keys()
        extra = make_words(_value("always-y", variables, state["sources"]) + " " + _value("extra-y", variables, state["sources"]))
        return {
            "target": path,
            "directory": directory,
            "outs": [path],
            "kind": "archive",
            "driver": "build",
            "deps": members + [_normalize(p) for p in extra],
            "members": members,
        }
    static = state["catalog"].actions.get(state["catalog"].aliases.get(path, path))
    if static != None and (static["kind"] == "compile" or static.get("driver") == "asm_headers" or path in state["header_owners"] or path in state["preparation"]):
        static = None
    directory = path.rpartition("/")[0]
    if owner != None and any([r.get("recipes") and path in [_normalize(p) for p in r["targets"] if "%" not in p] for r in _context(owner, state).rules]):
        # A Makefile can own outputs outside its directory (for example a
        # sibling generated header). Only an explicit producer establishes
        # that ownership; generic compiler patterns belong to each directory.
        directory, static = owner, None
    module_owner = state["module_owners"].get(path)
    if module_owner != None:
        directory = module_owner

    # Existing explicit base/preparation edges remain available as anchors.
    if static != None:
        result = dict(static)
        result["deps"] = list(static["deps"])
        if static["kind"] == "compile":
            result["deps"].extend(header_commands)
        if path.endswith(".lds"):
            result["deps"].extend(state["preparation"])
        if path.endswith(".o"):
            generated = path[:-2] + ".c"
            if generated in state["catalog"].aliases:
                result["deps"].append(generated)
        if static["kind"] == "generate" and static.get("driver") != "asm_headers" and static["directory"] in state["catalog"].programs:
            result["native"] = _recipe(path, static["directory"], state)
            if result["native"] != None:
                result["directory"] = result["native"]["directory"]
                result["deps"].extend(result["native"]["prerequisites"])
        return result
    if path == "include/generated/utsversion.h":
        directory = "init"
    if path in state["link_owners"]:
        directory = "@vmlinux"
    elif path in state["header_owners"]:
        directory = state["header_owners"][path]
    elif path in state["bootstrap"]:
        directory = "."
    if directory not in state["contexts"] and directory not in state["catalog"].programs:
        parts = directory.split("/")
        for i in reversed(range(len(parts))):
            parent = "/".join(parts[:i]) if i else "."
            if parent in state["catalog"].programs:
                directory = parent
                break
    if path.endswith("/dtbs-list") and directory not in state["dtbs_lists"]:
        state["dtbs_lists"][directory] = True
        state["contexts"].pop(directory, None)
    context = _context(directory, state)
    variables = context.variables
    hosts = make_words(_value("hostprogs", variables, state["sources"]) + " " +
                       _value("hostprogs-always-y", variables, state["sources"]) + " " +
                       _value("hostprogs-always-m", variables, state["sources"]))
    host_members = {}
    for host in hosts:
        objects = make_words(_value(host + "-objs", variables, state["sources"]) + " " +
                             _value(host + "-cxxobjs", variables, state["sources"]))
        host_members[host] = [_normalize(directory + "/" + obj) for obj in objects]
    host_name = path.removeprefix(directory + "/") if directory != "." else path
    kind = "host" if host_name in hosts or path in [p for members in host_members.values() for p in members] else "generate"
    dependencies = host_members.get(host_name, [])
    outs = [path]
    has_recipe = host_name in hosts
    source = path[:-2] + ".c" if path.endswith((".o", ".s")) else path + ".c"
    if path.endswith((".o", ".s")):
        # Native suffix patterns consume generated lexers/parsers too.
        generated_source = source == ".vmlinux.export.c" or any([match(_normalize(pattern), source) != None and rule["recipe"] and _valid_rule(rule, match(_normalize(pattern), source), context, state) for rule in context.rules for pattern in rule["targets"]])
        if generated_source:
            dependencies.append(source)
            has_recipe = True
        if source in state["source_files"] or source in state["catalog"].actions:
            has_recipe = True
        elif source.endswith(".lex.c") and source[:-6] + ".l" in state["source_files"]:
            dependencies.append(source)
            has_recipe = True
        elif source.endswith(".tab.c") and source[:-6] + ".y" in state["source_files"]:
            dependencies.append(source)
            has_recipe = True
        elif path[:-2] + ".S" in state["source_files"]:
            has_recipe = True
        if source in state["catalog"].actions:
            dependencies.append(source)
        if kind != "host":
            kind = "compile" if has_recipe else "generate"
    for rule in _rules_for_path(path, context, state):
        for pattern in rule["targets"]:
            stem = match(_normalize(pattern), path)
            if stem == None or not _valid_rule(rule, stem, context, state):
                continue
            has_recipe = has_recipe or rule["recipe"]
            if rule["grouped"]:
                outs = [_normalize(p.replace("%", stem, 1)) for p in rule["targets"]]
            tail = rule["prerequisites"].split(";", 1)[0]
            if ":" in tail:
                target_pattern, tail = tail.split(":", 1)
                stem = match(_normalize(target_pattern.strip()), path)
                if stem == None:
                    continue
            dependencies.extend([_normalize(d.replace("%", stem, 1)) for d in make_words(tail) if d not in ("FORCE", "|")])
    native = _recipe(path, directory, state)
    if native != None:
        if native["directory"] != directory:
            directory = native["directory"]
            dependencies.extend(native["prerequisites"])
        command = evaluate(native["arguments"][0], native["variables"], state["sources"]).strip()
        if kind != "host" and command in FLAG_PROGRAM["image_compile_commands"]:
            native["compile"] = True
        if kind != "host" and command in ("cc_o_c", "as_o_S"):
            kind = "compile"
    if not has_recipe:
        fail("No native action producer for " + path)
    compiler_prerequisites = list(dependencies)
    if kind == "compile" or (native != None and native.get("compile")) or path.endswith(".lds"):
        # Root Kbuild already declares the ordering among bootstrap assembly
        # generators. Other objects consume the configured preparation outputs.
        if path not in state["bootstrap"]:
            dependencies.extend([p for p in state["preparation"] if directory not in state["late_directories"] or p not in state["late_preparation"]])
        else:
            dependencies.extend([p for p in state["preparation"] if "/include/generated/" in p and p not in state["late_preparation"]])
            if path not in state["preparation_assembly"]:
                dependencies.extend(state["early_preparation"])
        dependencies.extend(header_commands)

        # The final version header is also consumed by native boot wrappers.
        dependencies.append("include/generated/utsversion.h")
    return {"target": path, "directory": "." if directory == "@vmlinux" else directory, "outs": outs, "deps": dependencies, "compiler_prerequisites": compiler_prerequisites, "kind": kind, "driver": "build", "native": native if kind in ("generate", "compile") else None}

def _select_modules(state):
    """Follow the configured recursive Kbuild graph to each requested owner."""
    for module in state["modules"]:
        object_path = module[:-3] + ".o"
        work = ["."]
        seen = {}
        selected = False
        for _ in range(10000):
            if not work:
                fail("Requested module is not selected by Kbuild: " + module)
            directory = work.pop()
            if directory in seen:
                continue
            seen[directory] = True
            variables = _context(directory, state).variables
            if object_path in make_words(_value("obj-m", variables, state["sources"])):
                state["module_owners"][object_path] = directory
                state["module_owners"][module[:-3] + ".mod"] = directory
                selected = True
                break
            work.extend([child for child in make_words(_value("subdir-ym", variables, state["sources"])) if object_path.startswith(child + "/")])
        if not selected:
            fail("Module selection exceeded 10000 directories: " + module)

def _build(ctx, request):
    config = request.config
    modules = sorted({path: True for path in request.modules})
    for path in modules:
        if not path.endswith(".ko") or _normalize(path) != path or path.startswith("/"):
            fail("Expected a source-relative Kbuild module path ending in .ko: " + path)
    if modules:
        if config.values.get("MODULES") != "y":
            fail("Requested modules require CONFIG_MODULES=y")
        for option in ["MODVERSIONS", "MODULE_SRCVERSION_ALL"]:
            if config.values.get(option) == "y":
                fail("Requested modules with CONFIG_%s need native .cmd metadata, which is not yet exported" % option)
    for name, expected in request.required_options.items():
        if config.values.get(name, "n") != expected:
            fail("Required kernel option %s=%s resolved to %s" % (name, expected, config.values.get(name, "n")))
    setup = image_evaluation_setup(ctx.attr._setup[KernelToolchainInfo])
    if setup.identity != config.identity:
        fail("Mixed native setup and kernel configuration")
    prepared = ctx.attr._prepared_source[KernelSourceTreeInfo]
    source_files = prepared.logical

    # This in-tree build declares vmlinux.o before modpost; native Make's
    # existence test must see the planned artifact, not the host filesystem.
    index = source_index(["__source__/" + p for p in source_files] + ["vmlinux.o"])
    index["__includes__"] = FLAG_PROGRAM["image_programs"]
    index["__capabilities__"] = setup.sources["__capabilities__"]
    index["__host_machine__"] = setup.sources["__host_machine__"]
    index["__compiler_include__"] = setup.sources.get("__compiler_include__", "")
    index["__execution_tools__"] = setup.sources["__execution_tools__"]
    index["__reported_probes__"] = setup.sources["__reported_probes__"]
    state = {
        "catalog": request.catalog,
        "contexts": {},
        "invocations": {},
        "target_scopes": {},
        "parents": {},
        "directory_modes": {},
        "modules": modules,
        "module_owners": {},
        "module_final": {p: True for module in modules for p in [module, module[:-3] + ".mod.o"]},
        "dtbs_lists": {},
        "arch": config.architecture.srcarch,
        "config": config.make_values,
        "source_files": source_files,
        "sources": index,
        "setup": setup,
        "pahole": config.pahole if config.values.get("DEBUG_INFO_BTF") == "y" else None,
    }
    external_tools = {}
    if config.values.get("DEBUG_INFO_BTF") == "y":
        if config.pahole == None:
            fail("CONFIG_DEBUG_INFO_BTF requires the declared pahole used to configure the kernel")
        external_tools["tools/bpf/resolve_btfids/resolve_btfids"] = ctx.attr._resolve_btfids[KernelResolveBtfidsInfo].executable

    # Recursive Make expands only exported parent variables into a child's
    # environment. Unexported names such as core-y must not affect core.o.
    state["environment"] = {name: {"expr": [{"op": "reference", "name": [{"op": "literal", "value": name}]}], "scope": setup.variables, "raw": ""} for name in setup.variables.get("__exports__", {}) if name in setup.variables}
    state["environment"]["__exports__"] = dict(setup.variables.get("__exports__", {}))
    dispatch = dict(setup.variables)
    for name, text in {"MAKE": "__make_dispatch__", "build": "__build_dispatch__", "Q": "", "KCONFIG_CONFIG": ".config", "srctree": "__source__", "srcroot": "__source__", "objtree": "."}.items():
        dispatch[name] = {"value": text}
    arch_context = program(FLAG_PROGRAM["image_programs"]["arch/" + config.architecture.srcarch + "/Makefile"], dispatch, state["sources"], cells = True)
    state["known_products"] = {}
    _prepare_contexts(state, arch_context)
    _select_modules(state)
    if modules:
        state["module_final"][".module-common.o"] = True
        variables = dict(setup.variables)
        for name, text in {"objtree": ".", "srctree": "__source__", "srcroot": "__source__", "KBUILD_BUILTIN": "1", "MAKEFLAGS": "-j1"}.items():
            variables[name] = {"value": text}
        if FLAG_PROGRAM["image_module_order_contents"] != None:
            variables["__selected_module_order"] = {"value": evaluate(FLAG_PROGRAM["image_module_order_contents"], _module_order_variables(state), state["sources"])}
        state["contexts"]["@modfinal"] = program(FLAG_PROGRAM["image_common"] + FLAG_PROGRAM["image_btf"] + FLAG_PROGRAM["image_modfinal"], variables, state["sources"], cells = True)
    state["contexts"]["@archive"] = program(FLAG_PROGRAM["image_archive"], _context(".", state).variables, state["sources"], cells = True)
    controls = program(FLAG_PROGRAM["image_archive_controls"], dict(state["contexts"]["@archive"].variables, PHONY = {"value": ""}), state["sources"], cells = True)
    phony = {p: True for rule in controls.rules if ".PHONY" in rule["targets"] for p in make_words(rule["prerequisites"])}
    state["empty_archive_controls"] = {}
    for path in phony:
        # A phony target with no selected rule is also empty. Inspect the
        # records directly: an unsupported active recipe must never disappear.
        rules = [rule for rule in controls.rules if path in rule["targets"]]
        if not any([rule.get("recipes") or make_words(rule["prerequisites"]) for rule in rules]):
            state["empty_archive_controls"][path] = True
    link_context = program(FLAG_PROGRAM["image_programs"]["scripts/Makefile.vmlinux"], _context(".", state).variables, state["sources"], cells = True)
    state["contexts"]["@vmlinux"] = link_context
    state["link_owners"] = {p: True for p in make_words(_value("targets", link_context.variables, state["sources"]))}
    final_targets = {}
    for rule in link_context.rules:
        for terms in rule.get("recipes", []):
            for term in terms:
                if term.get("op") == "function" and term.get("name") == "call" and len(term["arguments"]) > 1 and evaluate(term["arguments"][1], link_context.variables, state["sources"]).strip() == "link_vmlinux":
                    final_targets.update({p: True for p in rule["targets"]})
    if len(final_targets) != 1:
        fail("Expected one native link_vmlinux target, got " + str(final_targets.keys()))
    state["final_link"] = final_targets.keys()[0]
    postlink = "arch/" + config.architecture.srcarch + "/Makefile.postlink"
    state["postlink_file"] = postlink
    if postlink in FLAG_PROGRAM["image_programs"]:
        state["contexts"]["@postlink"] = program(FLAG_PROGRAM["image_programs"][postlink], _context(".", state).variables, state["sources"], cells = True)
    state["postlinks"] = {path: _postlink(path, state) for path in [state["final_link"]] + modules}
    state["postlink_outputs"] = {}
    for path, post in state["postlinks"].items():
        for output in post.outputs:
            if output == path or output in state["postlink_outputs"]:
                fail("Multiple native postlink producers for " + output)
            state["postlink_outputs"][output] = path
    state["known_products"] = {p: True for p in ["built-in-fixup.a", "vmlinux.a", "vmlinux.o", "vmlinux.symvers", ".vmlinux.export.c", "vmlinux.unstripped", "vmlinux", "System.map", ".config", "include/config/auto.conf", "include/generated/autoconf.h"] + state["postlink_outputs"].keys() + state["module_final"].keys() + [m[:-3] + suffix for m in modules for suffix in [".o", ".mod", ".mod.c"]]}
    _architecture_invocations(request.architecture_roots.get(config.architecture.srcarch, []), state)
    pending = [(path, None, {}) for path in reversed(list(request.roots) + request.architecture_roots.get(config.architecture.srcarch, []) + modules)]
    descriptors, producers = {}, {}
    for _ in range(100000):
        if not pending:
            break
        path, owner, inherited = pending.pop()
        path = _normalize(path)
        if path in (".config", "include/config/auto.conf", "include/generated/autoconf.h") or path.startswith("__source__/") or path == "tools/objtool/objtool" or path in external_tools or path in source_files or path in producers:
            continue
        state["target_scopes"][path] = (owner, inherited)
        descriptor = _describe(path, state, owner)
        descriptor["deps"] = [_normalize(p) for p in descriptor["deps"]]
        invocation = state["invocations"].get(descriptor["directory"])
        descriptor["visibility"] = [_normalize(p) for p in invocation.visibility] if invocation != None else []
        descriptors[descriptor["target"]] = descriptor
        for out in descriptor["outs"]:
            if out in producers and producers[out] != descriptor["target"]:
                fail("Multiple configured producers for " + out)
            producers[out] = descriptor["target"]
        descendants = _target_scope(path, descriptor["directory"], state).descendants
        pending.extend([(dependency, None, {}) for dependency in reversed(descriptor["visibility"])])
        pending.extend([(dependency, descriptor["directory"], descendants) for dependency in reversed(descriptor["deps"])])
    if pending:
        fail("Kbuild graph exceeded 100000 dependency expansions")

    # Declare every output before registering jobs. This permits a direct,
    # configured topological check and avoids label cycles between alternatives.
    artifacts = {out: ctx.actions.declare_file(ctx.label.name + ".artifacts/" + out) for out in producers}

    # Compilers resolve generated includes through individual artifacts in one
    # namespace. Configuration outputs come from another rule, so alias them
    # here once instead of copying them into each compiler's input tree.
    config_files = {}
    for path, source in {"include/generated/autoconf.h": config.header, "include/config/auto.conf": config.config, ".config": config.config}.items():
        output = ctx.actions.declare_file(ctx.label.name + ".artifacts/" + path)
        ctx.actions.symlink(output = output, target_file = source)
        config_files[path] = output
    generated_root = config_files[".config"].dirname
    compiler_directories = {}
    for d in descriptors.values():
        directory = d["directory"]
        if directory not in compiler_directories:
            path = "" if directory == "." else directory + "/"
            marker = ctx.actions.declare_file(ctx.label.name + ".artifacts/" + path + ".kbuild-directory")
            ctx.actions.write(marker, "")
            compiler_directories[directory] = marker
    common = {"include/generated/autoconf.h": config_files["include/generated/autoconf.h"]}
    if config.values.get("OBJTOOL") == "y":
        objtool = ctx.attr._objtool[KernelObjtoolInfo]
        if objtool.executable == None:
            fail("CONFIG_OBJTOOL requires a native objtool decoder for SRCARCH=" + config.architecture.srcarch)
        common["tools/objtool/objtool"] = objtool.executable
    finished = {}
    members = []
    qualification = request.qualification
    experiment = struct(
        enabled = qualification != None,
        records = {},
        diagnostic_target = qualification.diagnostic_target if qualification != None else "",
        diagnostic_source = qualification.diagnostic_source if qualification != None else None,
        diagnostic_driver = qualification.diagnostic_driver if qualification != None else None,
        diagnostics = [],
    )
    if experiment.diagnostic_target and (experiment.diagnostic_source == None or experiment.diagnostic_driver == None):
        fail("IR diagnostics require their declared source and capture driver")
    for _ in range(len(descriptors) + 1):
        progress = False
        for target, d in descriptors.items():
            if target in finished:
                continue
            deps = {producers[p]: True for p in d["deps"] + d["visibility"] if p in producers and producers[p] != target}
            if any([p not in finished for p in deps]):
                continue
            closure = dict(common)
            for dep in deps:
                closure.update(finished[dep])
            closure.update({p: external_tools[p] for p in d["deps"] if p in external_tools})

            # Earlier invocation phases supply candidate generated includes.
            # Only the target's own prerequisites are mandatory compiler inputs.
            direct = dict(common)
            direct.update({p: artifacts[p] for p in d["deps"] if p in producers and producers[p] != target})
            direct.update({p: external_tools[p] for p in d["deps"] if p in external_tools})
            native_prerequisites = d.get("compiler_prerequisites", d["deps"])
            native_direct = {p: artifacts[p] for p in native_prerequisites if p in producers and producers[p] != target}
            native_direct.update({p: external_tools[p] for p in native_prerequisites if p in external_tools and p != "tools/objtool/objtool"})

            # Source prerequisites are mandatory inputs of this action, e.g.
            # assembler .incbin payloads which C preprocessor depfiles omit.
            # Descendants consume its outputs, not every ancestor's sources.
            source_prerequisites = {
                p.removeprefix("__source__/"): source_files[p.removeprefix("__source__/")]
                for p in d["deps"]
                if p.removeprefix("__source__/") in source_files
            }
            outputs = {out: artifacts[out] for out in d["outs"]}
            ancestry, parent = [d["directory"]], d["directory"]
            for _parent in range(len(state["parents"]) + 1):
                parent = state["parents"].get(parent, ".")
                if parent == ".":
                    break
                if parent in ancestry:
                    fail("Native directory recursion cycle: " + str(ancestry + [parent]))
                ancestry.append(parent)
            if ancestry[-1] != ".":
                ancestry.append(".")
            ancestry = list(reversed(ancestry))
            graph_variables = {}
            if not d["directory"].startswith("@") and (d["kind"] in ["host", "compile"] or (d["kind"] == "generate" and d.get("native") == None and d["driver"] != "asm_headers")):
                graph_variables = _context(d["directory"], state).graph_variables
            register(
                ctx,
                target,
                d["directory"],
                d["kind"],
                d["driver"],
                outputs,
                closure,
                prepared,
                config,
                setup,
                struct(root = generated_root, directory_marker = compiler_directories[d["directory"]], direct = direct, native_direct = native_direct, config = config_files, experiment = experiment),
                d.get("members", [p for p in d["deps"] if p.endswith(".o")] if d["kind"] == "host" and not target.endswith(".o") else []),
                native = d.get("native"),
                ancestry = ancestry,
                graph_variables = graph_variables,
                source_prerequisites = source_prerequisites,
                invocation_environment = state["invocations"][d["directory"]].environment if d["directory"] in state["invocations"] else None,
            )
            members.append({"target": target, "dependencies": sorted(deps), "outputs": {p: f.path for p, f in outputs.items()}})
            closure.update(outputs)
            finished[target] = closure
            progress = True
        if len(finished) == len(descriptors):
            break
        if not progress:
            blocked = {
                t: [producers[p] for p in d["deps"] + d["visibility"] if p in producers and producers[p] != t and producers[p] not in finished]
                for t, d in descriptors.items()
                if t not in finished
            }
            trail, positions, current = [], {}, blocked.keys()[0]
            for _edge in range(len(blocked) + 1):
                if current in positions:
                    fail("Configured Kbuild dependency cycle: " + str(trail[positions[current]:] + [current]))
                positions[current] = len(trail)
                trail.append(current)
                current = blocked[current][0]
            fail("Unable to trace configured Kbuild dependency cycle")
    manifest = ctx.actions.declare_file(ctx.label.name + ".json")
    ctx.actions.write(manifest, json.encode({
        "identity": config.identity,
        "config": config.config.path,
        "config_report": config.report.path,
        "setup": setup.file.path,
        "members": members,
        "roots": list(request.roots) + request.architecture_roots.get(config.architecture.srcarch, []) + modules,
        "artifacts": {p: f.path for p, f in artifacts.items()},
    }))
    experiment_files = []
    if experiment.enabled:
        report = ctx.actions.declare_file(ctx.label.name + ".ir.json")
        ctx.actions.write(report, json.encode({
            target: {
                "source": record.source.path,
                "frontend": record.frontend.path,
                "bitcode": record.bitcode.path,
                "integrated": record.integrated.path,
                "object": record.object.path,
                "flags": record.flags,
            }
            for target, record in experiment.records.items()
        }))
        experiment_files.append(report)
        for record in experiment.records.values():
            experiment_files.extend([record.frontend, record.bitcode, record.integrated, record.object])
        if experiment.diagnostic_target and not experiment.diagnostics:
            fail("IR diagnostic target was not compiled: " + experiment.diagnostic_target)
        experiment_files.extend(experiment.diagnostics)
    return [
        KernelBuildInfo(
            artifacts = dict(artifacts, **{".config": config.config}),
            config = config,
            architecture = config.architecture,
        ),
        DefaultInfo(files = depset([manifest, config.report, setup.file] + artifacts.values())),
        OutputGroupInfo(boot = depset([artifacts[p] for p in request.architecture_roots.get(config.architecture.srcarch, [])]), kernel = depset([artifacts["vmlinux"]] if "vmlinux" in artifacts else []), modules = depset([artifacts[p] for p in modules]), manifest = depset([manifest]), ir_experiment = depset(experiment_files)),
    ]

def _artifact_impl(ctx):
    build = ctx.attr.build[KernelBuildInfo]
    output = build.artifacts.get(ctx.attr.path)
    if output == None:
        fail("Kernel build %s does not produce %s; request its Kbuild root or module" % (ctx.attr.build.label, ctx.attr.path))
    return [DefaultInfo(files = depset([output]))]

kernel_artifact = rule(
    implementation = _artifact_impl,
    attrs = {
        "build": attr.label(mandatory = True, providers = [KernelBuildInfo]),
        "path": attr.string(mandatory = True),
    },
    doc = "Select one declared kernel artifact by its Kbuild path, or .config.",
)

_ACTIVE_CONFIG = str(Label("//:active_config"))

# Bazel callback signatures include inputs this implementation does not use.
# buildifier: disable=unused-variable
def _select(settings, attr):
    return {_ACTIVE_CONFIG: str(attr.config)}

_transition = transition(implementation = _select, inputs = [], outputs = [_ACTIVE_CONFIG])

def _setup_impl(ctx):
    config = ctx.attr.config[KernelConfigInfo]
    setup = ctx.attr._setup[KernelToolchainInfo]
    if config.identity != setup.identity:
        fail("Setup snapshot and configuration must match")
    return [config, setup, DefaultInfo(files = depset([config.config, config.report, setup.file]))]

kbuild_resolved_setup = rule(implementation = _setup_impl, cfg = _transition, attrs = {
    "config": attr.label(mandatory = True, providers = [KernelConfigInfo]),
    "_setup": attr.label(default = "//:toolchain", providers = [KernelToolchainInfo]),
    "_allowlist_function_transition": attr.label(default = "@bazel_tools//tools/allowlists/function_transition_allowlist"),
})

_REQUEST_ATTRS = {
    "config": attr.label(mandatory = True, providers = [KernelConfigInfo]),
    "catalog": attr.label(mandatory = True, providers = [KbuildCatalogInfo]),
    "required_options": attr.string_dict(),
    "roots": attr.string_list(mandatory = True),
    "architecture_roots": attr.string_list_dict(),
    "modules": attr.string_list(doc = "Source-relative .ko paths selected as modules by the configured Kbuild graph."),
}

_CORE_ATTRS = {
    "_link_script": attr.label(default = "//rules:link-vmlinux.sh", allow_single_file = True),
    "_objtool": attr.label(default = "//:objtool", providers = [KernelObjtoolInfo]),
    "_resolve_btfids": attr.label(default = "//:resolve_btfids", providers = [KernelResolveBtfidsInfo]),
    "_setup": attr.label(default = "//:toolchain", providers = [KernelToolchainInfo]),
    "_materialize": attr.label(default = "//rules:materialize.py", allow_single_file = True, cfg = "exec"),
    "_prepared_source": attr.label(default = "//:prepared_source", providers = [KernelSourceTreeInfo], cfg = "exec"),
    "_anchor": attr.label(default = "//:source/Makefile", allow_single_file = True),
}

def _request_info(ctx, qualification = None):
    return KernelBuildRequestInfo(
        config = ctx.attr.config[KernelConfigInfo],
        catalog = ctx.attr.catalog[KbuildCatalogInfo],
        roots = ctx.attr.roots,
        architecture_roots = ctx.attr.architecture_roots,
        modules = ctx.attr.modules,
        required_options = ctx.attr.required_options,
        qualification = qualification,
    )

def _request_impl(ctx):
    request = _request_info(ctx)

    # Existing setup rules consume the same configuration through active_config.
    return [request, request.config, DefaultInfo()]

_build_request = rule(implementation = _request_impl, attrs = _REQUEST_ATTRS)

def _qualification_impl(ctx):
    if not ctx.attr.diagnostic_target:
        fail("Compiler qualification requires a native diagnostic target")
    return [KernelBuildQualificationInfo(
        diagnostic_target = ctx.attr.diagnostic_target,
        diagnostic_source = ctx.file.diagnostic_source,
        diagnostic_driver = ctx.file.diagnostic_driver,
    )]

kernel_build_qualification = rule(implementation = _qualification_impl, doc = "Declare compiler checks for a build's optional qualification view.", attrs = {
    "diagnostic_target": attr.string(mandatory = True),
    "diagnostic_source": attr.label(mandatory = True, allow_single_file = [".c"]),
    "diagnostic_driver": attr.label(mandatory = True, allow_single_file = [".py"], cfg = "exec"),
})

def _qualification_request_impl(ctx):
    base = ctx.attr.base[KernelBuildRequestInfo]
    request = KernelBuildRequestInfo(
        config = base.config,
        catalog = base.catalog,
        roots = base.roots,
        architecture_roots = base.architecture_roots,
        modules = base.modules,
        required_options = base.required_options,
        qualification = ctx.attr.qualification[KernelBuildQualificationInfo],
    )
    return [request, request.config, DefaultInfo()]

_qualification_request = rule(implementation = _qualification_request_impl, attrs = {
    "base": attr.label(mandatory = True, providers = [KernelBuildRequestInfo]),
    "qualification": attr.label(mandatory = True, providers = [KernelBuildQualificationInfo]),
})

def _core_impl(ctx):
    return _build(ctx, ctx.attr._request[KernelBuildRequestInfo])

kbuild_resolved_core = rule(implementation = _core_impl, fragments = ["cpp"], toolchains = use_cc_toolchain() + [EXECUTION_TOOLS_TYPE], attrs = dict(
    _CORE_ATTRS,
    _request = attr.label(default = "//:active_config", providers = [KernelBuildRequestInfo, KernelConfigInfo]),
))

# Bazel supplies both transition callback arguments.
# buildifier: disable=unused-variable
def _select_request(settings, attr):
    return {_ACTIVE_CONFIG: str(attr.request)}

_request_transition = transition(implementation = _select_request, inputs = [], outputs = [_ACTIVE_CONFIG])

def _view_impl(ctx):
    target = ctx.attr._core[0]
    return [target[KernelBuildInfo], target[DefaultInfo], target[OutputGroupInfo]]

_build_view = rule(implementation = _view_impl, attrs = {
    "request": attr.label(mandatory = True, cfg = _request_transition, providers = [KernelBuildRequestInfo]),
    "_core": attr.label(default = "//:_kernel_build", cfg = _request_transition, providers = [KernelBuildInfo]),
    "_allowlist_function_transition": attr.label(default = "@bazel_tools//tools/allowlists/function_transition_allowlist"),
})

def kbuild_resolved_build(name, config, catalog, roots, architecture_roots = {}, modules = [], required_options = {}, tags = [], visibility = None, qualification = None):
    """Request native products from this source repository's shared action owner.

    A qualification spec also declares a manual `<name>_qualification` view
    of the same requested products with compiler parity and diagnostic checks.
    """
    request = name + "_request"
    _build_request(
        name = request,
        config = config,
        catalog = catalog,
        roots = roots,
        architecture_roots = architecture_roots,
        modules = modules,
        required_options = required_options,
        tags = ["manual"],
        visibility = ["//visibility:private"],
    )
    _build_view(name = name, request = ":" + request, tags = tags, visibility = visibility)
    if qualification != None:
        qualified = name + "_qualification"
        _qualification_request(
            name = qualified + "_request",
            base = ":" + request,
            qualification = qualification,
            tags = ["manual"],
            visibility = ["//visibility:private"],
        )
        _build_view(name = qualified, request = ":" + qualified + "_request", tags = tags + ["manual"], visibility = visibility)

def _experiment_impl(ctx):
    qualification = KernelBuildQualificationInfo(
        diagnostic_target = ctx.attr.ir_diagnostic_target,
        diagnostic_source = ctx.file.ir_diagnostic_source,
        diagnostic_driver = ctx.file.ir_diagnostic_driver,
    ) if ctx.attr.ir_pipeline else None
    return _build(ctx, _request_info(ctx, qualification))

# Temporary qualification core: outgoing transitions can select several
# configurations of this same label, preserving paths after Bazel mapping.
kbuild_ir_experiment = rule(implementation = _experiment_impl, fragments = ["cpp"], toolchains = use_cc_toolchain() + [EXECUTION_TOOLS_TYPE], attrs = dict(
    _CORE_ATTRS,
    **dict(
        _REQUEST_ATTRS,
        config = attr.label(default = "//:active_config", providers = [KernelConfigInfo]),
        ir_pipeline = attr.bool(default = True),
        ir_diagnostic_target = attr.string(),
        ir_diagnostic_source = attr.label(allow_single_file = [".c"]),
        ir_diagnostic_driver = attr.label(allow_single_file = [".py"], cfg = "exec"),
    )
))
