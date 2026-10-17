"""Source and embedded Python inputs for the standalone generator build."""

_PYTHON_RUNTIMES = {
    "linux_x86_64_musl": struct(
        archive = "x86_64-unknown-linux-musl-noopt%2Bstatic",
        sha256 = "4523333b3f83997216fe873cdb69b1e183a58cd66da4fe2cfdf22a4f257e61af",
        target = "x86_64-unknown-linux-musl",
        format = "elf",
        crt = ["static"],
        constraints = ["@platforms//os:linux", "@platforms//cpu:x86_64", "@llvm//constraints/libc:musl"],
    ),
    "macos_aarch64": struct(
        # PBS's optimized archive contains LLVM 20 bitcode. Use its native
        # objects so embedding does not depend on that compiler's LTO ABI.
        archive = "aarch64-apple-darwin-debug",
        sha256 = "1616613344c1e194c14578df52fe8b3b9ece2622d17648c8f201d6726da9329e",
        target = "aarch64-apple-darwin",
        format = "mach-o",
        crt = ["libSystem"],
        constraints = ["@platforms//os:macos", "@platforms//cpu:aarch64"],
    ),
}

# Additional imports used by the renderer and its standard-library dependencies.
# Core and default bootstrap modules still come from the runtime's metadata.
_PYTHON_MODULES = [
    "array",
    "_bisect",
    "_contextvars",
    "_json",
    "_random",
    "_struct",
    "math",
    "binascii",
    "_posixsubprocess",
    "select",
    "fcntl",
    "grp",
    "_md5",
    "_sha1",
    "_sha2",
    "_sha3",
    "_blake2",
]

def _python_runtime_impl(ctx):
    runtime = _PYTHON_RUNTIMES[ctx.attr.platform]
    ctx.download_and_extract(
        url = "https://github.com/astral-sh/python-build-standalone/releases/download/20251014/cpython-3.13.9%2B20251014-" + runtime.archive + "-full.tar.zst",
        sha256 = runtime.sha256,
        stripPrefix = "python",
    )
    metadata = json.decode(ctx.read("PYTHON.json"))
    build = metadata["build_info"]
    if metadata["target_triple"] != runtime.target or build["object_file_format"] != runtime.format or metadata["crt_features"] != runtime.crt:
        fail("Unexpected embedded runtime metadata for " + ctx.attr.platform)
    modules = {name: True for name in _PYTHON_MODULES}
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
        elif link.get("framework"):
            linkopts["-framework " + link["name"]] = True
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
    target_compatible_with = %s,
)
filegroup(name = "stdlib", srcs = glob([%r], exclude = %s))
filegroup(name = "licenses", srcs = %s)
""" % (
        repr(sorted(objects) + sorted(libraries) + ["inittab.c"]),
        metadata["python_paths"]["include"] + "/**/*.h",
        metadata["python_paths"]["include"],
        repr(sorted(linkopts)),
        repr(runtime.constraints),
        stdlib + "/**/*.py",
        repr([stdlib + "/**/" + name + "/**" for name in ["test", "tests", "idlelib", "tkinter", "ensurepip", "site-packages", "__pycache__"]]),
        repr(sorted(licenses)),
    ))

python_runtime_repository = repository_rule(
    implementation = _python_runtime_impl,
    attrs = {"platform": attr.string(mandatory = True, values = _PYTHON_RUNTIMES.keys())},
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
