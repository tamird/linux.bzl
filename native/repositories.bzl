"""Source and static Python inputs for the standalone generator build."""

def _python_runtime_impl(ctx):
    ctx.download_and_extract(
        url = "https://github.com/astral-sh/python-build-standalone/releases/download/20251014/cpython-3.13.9%2B20251014-x86_64-unknown-linux-musl-noopt%2Bstatic-full.tar.zst",
        sha256 = "4523333b3f83997216fe873cdb69b1e183a58cd66da4fe2cfdf22a4f257e61af",
        stripPrefix = "python",
    )
    metadata = json.decode(ctx.read("PYTHON.json"))
    build = metadata["build_info"]
    if metadata["target_triple"] != "x86_64-unknown-linux-musl" or build["object_file_format"] != "elf" or metadata["crt_features"] != ["static"]:
        fail("Generator runtime must contain static x86_64 musl ELF objects")
    modules = {name: True for name in ctx.attr.modules}
    for line in ctx.read("build/Modules/Setup.bootstrap").splitlines():
        words = [word for word in line.split("#", 1)[0].replace("\t", " ").split(" ") if word]
        if words and not words[0].startswith("*") and "=" not in line:
            modules[words[0]] = True
    extensions = {}
    for name, variants in build["extensions"].items():
        if len(variants) != 1:
            fail("Expected one static runtime variant for " + name)
        extension = variants[0]
        extensions[name] = extension
        if extension["in_core"] or extension["required"]:
            modules[name] = True
    objects = {path: True for path in build["core"]["objs"] if path != build["inittab_object"]}
    links = list(build["core"]["links"])
    licenses = {metadata["license_path"]: True}
    declarations = []
    entries = []
    for name in sorted(modules):
        if name not in extensions:
            fail("Static runtime does not provide requested module " + name)
        extension = extensions[name]
        for path in extension["objs"]:
            objects[path] = True
        links.extend(extension["links"])
        for path in extension.get("license_paths", []):
            licenses[path] = True
        initializer = extension["init_fn"]
        if initializer != "NULL":
            declarations.append("extern PyObject *%s(void);" % initializer)
        entries.append('{"%s", %s},' % (name, initializer))
    libraries = {}
    linkopts = {}
    for link in links:
        if link.get("system"):
            linkopts["-l" + link["name"]] = True
        elif link.get("path_static"):
            libraries[link["path_static"]] = True
        else:
            fail("Runtime link input has no static implementation: " + repr(link))
    ctx.file("inittab.c", '#include "Python.h"\n' + "\n".join(declarations) + "\nstruct _inittab _PyImport_Inittab[] = {\n" + "\n".join(entries) + "\n{0, 0}\n};\n")
    stdlib = metadata["python_paths"]["stdlib"]
    ctx.file("BUILD.bazel", """load("@rules_cc//cc:cc_library.bzl", "cc_library")
package(default_visibility = ["//visibility:public"])
exports_files(["PYTHON.json"])
cc_library(
    name = "runtime",
    srcs = %s,
    hdrs = glob([%r]),
    includes = [%r],
    linkopts = %s,
    target_compatible_with = ["@platforms//os:linux", "@platforms//cpu:x86_64"],
)
filegroup(name = "stdlib", srcs = glob([%r], exclude = %s))
filegroup(name = "licenses", srcs = %s)
""" % (
        repr(sorted(objects) + sorted(libraries) + ["inittab.c"]),
        metadata["python_paths"]["include"] + "/**/*.h",
        metadata["python_paths"]["include"],
        repr(sorted(linkopts)),
        stdlib + "/**/*.py",
        repr([stdlib + "/**/" + name + "/**" for name in ["test", "tests", "idlelib", "tkinter", "ensurepip", "site-packages", "__pycache__"]]),
        repr(sorted(licenses)),
    ))

python_runtime_repository = repository_rule(
    implementation = _python_runtime_impl,
    attrs = {"modules": attr.string_list(mandatory = True)},
)

def _make_sources_impl(ctx):
    root = ctx.path(ctx.attr.source).dirname
    changed = {}
    for line in ctx.read(ctx.attr.patch).splitlines():
        if line.startswith("+++ b/"):
            path = line[len("+++ b/"):]
            if not path.startswith("src/") or "/../" in path:
                fail("Unexpected Make parser patch path: " + path)
            changed[path] = True
    for directory in ["src", "lib"]:
        for source in root.get_child(directory).readdir():
            path = directory + "/" + source.basename
            if path in changed:
                # The native patcher must never write through into @make.
                ctx.file(path, ctx.read(source), executable = False)
            else:
                ctx.symlink(source, path)
    ctx.patch(ctx.attr.patch, strip = 1)
    ctx.symlink(root.get_child("COPYING"), "COPYING")
    ctx.file("BUILD.bazel", """package(default_visibility = ["//visibility:public"])
exports_files(["COPYING"])
filegroup(name = "sources", srcs = glob(["src/**", "lib/**"]))
""")

make_parser_repository = repository_rule(
    implementation = _make_sources_impl,
    attrs = {
        "source": attr.label(default = "@make//:MODULE.bazel"),
        "patch": attr.label(default = "//native:gnu-make-4.4.1.patch"),
    },
)
