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

The Python runtime comes from a pinned platform-specific distribution. Its metadata
supplies the core objects, builtin initializers and link dependencies. The
generator includes Python's default bootstrap modules plus the extra modules
listed once in `repositories.bzl`. A deterministic uncompressed ZIP contains
the standard library, renderer and licenses. On Linux, Bazel links the static
musl runtime with both parsers and the launcher, then appends that ZIP; the ELF
has no interpreter or dynamic dependencies.

On macOS, the ZIP is a read-only section included before the final link and its
ad hoc code signature. Apple's [code-signing contract](https://developer.apple.com/library/archive/technotes/tn2206/)
prohibits appending data to a Mach-O executable.
The launcher writes the embedded ZIP to a temporary import archive for the
Python process and removes it on completion. Parser subprocesses bypass this
step. The executable may link only the declared macOS system libraries; no
Python runtime or extension is loaded dynamically. The pinned Python debug
distribution supplies native Mach-O objects, avoiding the optimized archive's
LLVM-version-specific bitcode. It enables `Py_DEBUG`; its own headers carry
the matching debug ABI.

Generator targets use the requested Bazel platform: `linux_x86_64_musl` or
`macos_aarch64` in `@llvm//platforms`. The consumer-support action also requires
an execution platform matching the target binary's OS and CPU. Compiler
cross-compilation support alone does not satisfy that execution requirement.

The root bootstrap targets use the prototype checkout at `_tmp/linux`.
The `//native/linux_6_12` and `//native/linux_6_18` packages expose
`:kernel_repository`, `:consumer_support`, and `:repository_test` for the LTS
source archives pinned in `MODULE.bazel`. These packages share the same
bootstrap and standalone-generation audit. Each
generator embeds the digests of its own kernel parser sources; its consumer
archive must be paired with that kernel source tree.

CI groups the LTS archives in `kernel-repository-consumer-lts-<platform>`.
Its paths are `linux_6_12/consumer-support.tar.gz` and
`linux_6_18/consumer-support.tar.gz`; each tarball remains a separate,
source-specific consumer package.

The lexer adapter resets the scanner when opening each file. This allows the
same process to parse `scripts/Kconfig.include` followed by the requested file,
including kernels whose `file_lookup` signature differs. The audit checks
exported symbols from common, x86, and ARM64 Kconfig files as well as the
empty-PATH and parser-source identity contracts.

The syntax adapter derives the menu entry, finalization and source-location
interfaces from the selected parser sources. It preserves kernels that include
`menu.c` in the parser translation unit and those that compile it separately.
The exporter uses native choice detection and retains the same syntax schema
when older sources have no transitional-symbol flag.

Each bootstrap package exposes `:kconfig_test`, which exports every declared
Kconfig file through its native generator and checks source locations and the
second scanner input. `//native/linux_5_10:kconfig_test` also qualifies this
boundary against the pinned 5.10 source. Its full Kbuild generation and image
build are not yet qualified.
