"""Native assignment timing and complete probe context regressions."""

load("@bazel_skylib//lib:unittest.bzl", "asserts", "unittest")
load("//rules:expressions.bzl", "evaluate", "program")

def _literal(value):
    return [dict(op = "literal", value = value)]

def _assignment(name, value, flavor = "simple"):
    return dict(kind = "assignment", name_expression = _literal(name), value_expression = value, value = "", flavor = flavor)

def _probe():
    return [dict(op = "function", name = "kbuild_probe", arguments = [_literal("cc-option"), _literal("-fno-common")])]

def _context_test(ctx):
    env = unittest.begin(ctx)
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
