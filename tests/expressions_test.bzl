"""Native assignment timing and complete probe context regressions."""

load("@bazel_skylib//lib:unittest.bzl", "asserts", "unittest")
load("//rules:expressions.bzl", "evaluate", "program")
load("//rules:host_tools.bzl", "resolve_subcmd_archive", "target_exports")
load("//rules:image.bzl", "rule_prerequisites")
load("//rules:toolchain.bzl", "make_setup")

def _literal(value):
    return [dict(op = "literal", value = value)]

def _assignment(name, value, flavor = "simple"):
    return dict(kind = "assignment", name_expression = _literal(name), value_expression = value, value = "", flavor = flavor)

def _probe():
    return [dict(op = "function", name = "kbuild_probe", arguments = [_literal("cc-option"), _literal("-fno-common")])]

def _context_test(ctx):
    env = unittest.begin(ctx)

    # The native module writer and its modpost child inherit MODORDER from
    # setup, including its source-selected prefix rather than a fixed path.
    module_variables = [
        dict(_assignment("sub_make_done", _literal("1")), export = 1),
        _assignment("extmod_prefix", [dict(op = "function", name = "if", arguments = [[dict(op = "reference", name = _literal("KBUILD_EXTMOD"))], [dict(op = "reference", name = _literal("KBUILD_EXTMOD"))] + _literal("/")])], flavor = "recursive"),
        dict(_assignment("MODORDER", [dict(op = "reference", name = _literal("extmod_prefix"))] + _literal("modules.order")), export = 1),
    ]
    setup = make_setup(
        ctx,
        struct(arch = "x86_64", architecture = struct(srcarch = "x86", subarch = "x86", header_arch = "x86"), values = {}, make_values = {}, identity = "fixture"),
        struct(root = struct(path = "source"), logical = []),
        struct(commands = {"lz4": struct(path = "declared-lz4")}, environment = {}),
        {},
        "compiler/include",
        [],
        [],
        [],
        {},
        {},
        [],
        {
            "capabilities": {},
            "global": [_assignment("KBUILD_EXTMOD", _literal("external-module")), _assignment("KBUILD_CHECKSRC", _literal("1"))],
            "image_cpp": [],
            "image_exports": [dict(kind = "conditional", branches = [dict(
                condition = dict(directive = "ifneq", operands = [[dict(op = "reference", name = _literal("sub_make_done"))], _literal("1")]),
                body = [dict(kind = "directive", directive = "export", arguments = "KBUILD_CHECKSRC", arguments_expression = _literal("KBUILD_CHECKSRC"))],
            )])],
            "image_flag_programs": {},
            "image_link_global": [],
            "image_link_programs": {},
            "image_module_variables": module_variables,
            "image_setup_programs": {},
            "image_utilities": [_assignment("LZ4", _literal("lz4"))],
            "image_version": [],
        },
    )
    destination = [dict(op = "reference", name = _literal("MODORDER"))]
    writer = _literal("{ echo net/sched/sch_ingress.ko; } > ") + destination
    asserts.equals(env, "{ echo net/sched/sch_ingress.ko; } > external-module/modules.order", evaluate(writer, setup.variables, setup.sources))
    asserts.equals(env, "external-module/modules.order", evaluate(destination, target_exports(setup), {}))
    asserts.equals(env, "1", evaluate([dict(op = "reference", name = _literal("KBUILD_CHECKSRC"))], target_exports(setup), {}))
    in_tree = program(module_variables, {}, {}).variables
    asserts.equals(env, "{ echo net/sched/sch_ingress.ko; } > modules.order", evaluate(writer, in_tree, {}))

    # GNU Make's first prerequisite expansion freezes ordinary operands;
    # escaped references are evaluated later in the selected target scope.
    first = [dict(op = "reference", name = _literal("FIRST"))]
    secondary = program([
        _assignment("FIRST", _literal("original.c")),
        dict(
            kind = "rule",
            targets_expression = _literal("%.o"),
            tail_expression = first + _literal(" $(objtool_dep) $@"),
            separator = ":",
            secondary_inputs = {"__first": first},
            secondary_expression = [dict(op = "reference", name = _literal("__first"))] + _literal(" ") + [dict(op = "reference", name = _literal("objtool_dep"))] + _literal(" ") + [dict(op = "reference", name = _literal("@"))],
        ),
        _assignment("FIRST", _literal("changed.c")),
        _assignment("objtool_dep", _literal("tools/objtool/objtool")),
    ], {}, {}, cells = True)
    asserts.equals(env, "original.c tools/objtool/objtool lib/ctype.o", rule_prerequisites(secondary.rules[0], "lib/ctype.o", "lib/ctype", secondary.variables, {}))
    asserts.equals(env, "plain.h", rule_prerequisites({"prerequisites": "plain.h"}, "lib/ctype.o", "lib/ctype", {}, {}))
    requests = {}
    sources = {"__requests__": requests, "__probe_environment__": {"SELECTED": "yes"}}
    initial = {"__command_line__": {"CC": "compiler"}, "CC": "compiler", "KBUILD_CPPFLAGS": "-D__KERNEL__", "KBUILD_CFLAGS": "", "objtree": ".", "srctree": "source"}
    result = program([
        _assignment("CC", _literal("ignored-makefile-compiler")),
        _assignment("KBUILD_CFLAGS", _literal("-m32")),
        _assignment("FIRST", _probe()),
        _assignment("KBUILD_CFLAGS", _literal("-m64"), flavor = "append"),
        _assignment("SECOND", _probe()),
    ], initial, sources)
    evaluate([dict(op = "reference", name = _literal("FIRST"))], result.variables, sources)
    evaluate([dict(op = "reference", name = _literal("SECOND"))], result.variables, sources)
    asserts.equals(env, 2, len(requests))
    asserts.equals(env, ["-D__KERNEL__ -m32", "-D__KERNEL__ -m32 -m64"], [r["flags"] for r in requests.values()])
    asserts.equals(env, "-m32 -m64", evaluate([dict(op = "reference", name = _literal("KBUILD_CFLAGS"))], result.variables, sources))
    origin = [dict(op = "function", name = "origin", arguments = [_literal("CC")])]
    asserts.equals(env, "command line", evaluate(origin, result.variables, sources))
    for request in requests.values():
        asserts.equals(env, "compiler", request["compiler"])
        asserts.equals(env, {"SELECTED": "yes"}, request["environment"])
        asserts.equals(env, ".", request["directory"])
        asserts.equals(env, "source", request["source"])
    explicit = _probe()[0]
    explicit["probe_context"] = dict(compiler = _literal("explicit-compiler"), flags = _literal("-m16"))
    evaluate([explicit], result.variables, sources)
    asserts.equals(env, "explicit-compiler", requests.values()[-1]["compiler"])
    asserts.equals(env, "-m16", requests.values()[-1]["flags"])

    # Native subcmd's lazy OUTPUT must request its directory measurement
    # before the owner decides that the current probe round has converged.
    directory = "bazel-out/config/bin/kernel.generated/libsubcmd"
    command = "cd __object__; cd " + directory + "; pwd"
    subcmd_program = [
        _assignment("ABSOLUTE_O", [dict(op = "function", name = "kbuild_host_shell", arguments = [_literal(command)])]),
        _assignment("OUTPUT", [dict(op = "reference", name = _literal("ABSOLUTE_O"))] + _literal("/")),
        _assignment("LIBFILE", [dict(op = "reference", name = _literal("OUTPUT"))] + _literal("libsubcmd.a")),
    ]
    request = dict(kind = "host-shell", command = command, environment = {}, directory = ".", source = "source", directories = [directory])
    for capabilities in [{}, {json.encode(request): directory}]:
        pending = {}
        source_context = {"__requests__": pending, "__capabilities__": capabilities, "__probe_environment__": {}, "__source_root__": "source", "__probe_directories__": [directory]}
        subcmd = program(subcmd_program, {}, source_context, cells = True).variables
        asserts.equals(env, {}, pending)
        archive = resolve_subcmd_archive(subcmd, source_context, directory + "/libsubcmd.a")
        if capabilities:
            asserts.equals(env, directory + "/libsubcmd.a", archive)
            asserts.equals(env, {}, pending)
        else:
            asserts.equals(env, None, archive)
            asserts.equals(env, {json.encode(request): request}, pending)

    # ARM64 exports this flag lazily. Its compiler include belongs to the
    # target context, even when a later host Make invocation inherits it.
    target_sources = {"__compiler_include__": "target/compiler/include"}
    target_variables = program([
        _assignment("CC_FLAGS_FPU", _literal("-ffreestanding -isystem ") + [dict(op = "function", name = "kbuild_compiler_include", arguments = [])]),
    ], {}, target_sources, cells = True).variables
    target_variables["__exports__"] = {"CC_FLAGS_FPU": True}
    inherited = target_exports(struct(variables = target_variables, sources = target_sources))
    host_sources = {"__compiler_include__": "host/compiler/include"}
    asserts.equals(env, "-ffreestanding -isystem target/compiler/include", evaluate([dict(op = "reference", name = _literal("CC_FLAGS_FPU"))], inherited, host_sources))

    # Both native include spellings refer to the already loaded auto.conf
    # values. Keep the native Make string representation unchanged.
    native_values = {"CONFIG_NATIVE": "y", "CONFIG_NATIVE_STRING": '\"quoted\\\\value\"'}
    for prefix in ["", "__object__/"]:
        for suffix in ["", ".cmd"]:
            configured = program([
                dict(kind = "directive", directive = "include", arguments_expression = _literal(prefix + "include/./config/auto.conf" + suffix)),
                _assignment("SELECTED", [dict(op = "reference", name = _literal("CONFIG_NATIVE_STRING"))]),
            ], {"__config__": native_values}, {})
            asserts.equals(env, native_values, configured.variables["__config__"])
            asserts.equals(env, native_values["CONFIG_NATIVE_STRING"], evaluate([dict(op = "reference", name = _literal("SELECTED"))], configured.variables))
    other_include = "__object__/include/config/native.mk"
    included = program([
        dict(kind = "directive", directive = "include", arguments_expression = _literal(other_include)),
    ], {}, {"__includes__": {other_include: [_assignment("NATIVE_INCLUDE", _literal("selected"))]}})
    asserts.equals(env, "selected", evaluate([dict(op = "reference", name = _literal("NATIVE_INCLUDE"))], included.variables))

    # A simple assignment captures once in each recipe expansion context.
    # Reusing the cell for another action must not reuse the previous slot.
    captures = {"__shell_commands__": []}
    command = [dict(op = "function", name = "shell", arguments = [_literal("printf captured")])]
    result = program([
        _assignment("SIMPLE", command),
        _assignment("RECURSIVE", command, flavor = "recursive"),
    ], {}, captures)
    simple = [dict(op = "reference", name = _literal("SIMPLE"))]
    recursive = [dict(op = "reference", name = _literal("RECURSIVE"))]
    asserts.equals(env, "__KBUILD_SHELL_RESULT_0__", evaluate(simple, result.variables, captures))
    asserts.equals(env, "__KBUILD_SHELL_RESULT_0__", evaluate(simple, result.variables, captures))
    asserts.equals(env, ["printf captured"], captures["__shell_commands__"])
    other = {"__shell_commands__": []}
    asserts.equals(env, "__KBUILD_SHELL_RESULT_0__", evaluate(simple, result.variables, other))
    asserts.equals(env, "__KBUILD_SHELL_RESULT_0__", evaluate(simple, result.variables, other))
    asserts.equals(env, "__KBUILD_SHELL_RESULT_1__", evaluate(recursive, result.variables, other))
    asserts.equals(env, "__KBUILD_SHELL_RESULT_2__", evaluate(recursive, result.variables, other))
    asserts.equals(env, ["printf captured"] * 3, other["__shell_commands__"])
    asserts.equals(env, ["printf captured"], captures["__shell_commands__"])
    return unittest.end(env)

probe_context_test = unittest.make(_context_test)
