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

The root bootstrap targets use the prototype checkout at `_tmp/linux`.
`//native/linux_6_18:kernel_repository`, `:consumer_support`, and
`:repository_test` use the LTS source archive pinned in `MODULE.bazel`. Both
packages share the same bootstrap and standalone-generation audit. Each
generator embeds the digests of its own kernel parser sources; its consumer
archive must be paired with that kernel source tree.

The lexer adapter resets the scanner when opening each file. This allows the
same process to parse `scripts/Kconfig.include` followed by the requested file,
including kernels whose `file_lookup` signature differs. The audit checks
exported symbols from common, x86, and ARM64 Kconfig files as well as the
empty-PATH and parser-source identity contracts.
