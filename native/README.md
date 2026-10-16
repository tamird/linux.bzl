GNU Make integration is the original prototype patch and its three native export
includes. Linux Kconfig integration is prepared by `prepare.py` from the kernel
checkout at tool-build time. It preserves source directives rather than following
them, exports file-local entries, and keeps the existing symbolic compiler facts
and declared fallback policy. These are generation inputs, not build-action tools.

The launcher invokes each parser in a fresh instance of the same executable.
Bazel compiles the parsers separately. Its `build.py` action helper collects each
parser's defined global symbols and applies the same `objcopy --redefine-syms`
mapping to all its objects. References between those objects remain consistent;
undefined libc references are untouched. This avoids collisions between Linux,
GNU Make and CPython helpers without changing their parser logic.

The Python runtime comes from a pinned static musl distribution. Its metadata
supplies the core objects, builtin initializers and link dependencies. The
generator includes Python's default bootstrap modules plus the extra modules
listed once in `MODULE.bazel`. Bazel links that runtime with both parsers and
the launcher, then appends a deterministic uncompressed ZIP containing the
standard library, renderer and licenses. The resulting ELF has no interpreter
or dynamic dependencies.
