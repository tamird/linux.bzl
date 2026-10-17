# Single-binary kernel repository prototype

This branch replaces the previous implementation with the standalone prototype.
The repository-generation boundary consists of **one executable**: a static
Linux/x86_64 ELF or an ARM64 macOS executable using only OS libraries.
It contains GNU Make 4.4.1's patched native syntax exporter,
the checkout's Linux Kconfig parser, an embedded CPython interpreter, and the
renderer and Starlark templates. It extracts data/templates to a private temporary
directory, but never extracts another executable or a shared library. Native
parser subprocesses execute this same binary, with separate entry points to isolate
the upstream parsers' global state.

Generation exports symbolic Make expressions, Kconfig entries and source edges.
Architecture, requested config, compiler facts, flags and object selection still
resolve during Bazel analysis using the existing platform/toolchain providers.
Kernel build actions do not invoke Make. The accepted conservative compiler-probe
fallback policy and existing architecture/boot limitations are unchanged.

## Build and try it

On Linux x86-64, place the kernel checkout at `_tmp/linux`, then run from
the repository root:

```sh
bazel build //:consumer_support --platforms=@llvm//platforms:linux_x86_64_musl
mkdir -p _tmp/kernel-repository
tar -xzf bazel-bin/consumer-support.tar.gz -C _tmp/kernel-repository
python3 example.py --generated _tmp/kernel-repository/support
cd examples
bazel build //:x86_64_kernel //:aarch64_kernel
bazel build @kernel//multi_platform:configs
# All implemented architectures; this bootstraps the configured LLVM backends:
bazel build @kernel//multi_platform:kernels
```

Bazel compiles the patched GNU Make and kernel parsers with declared toolchains,
links the launcher against pinned CPython objects from
python-build-standalone, and packages the renderer. Dependencies and outputs use
Bazel's normal caches. The package action verifies static ELF linkage on Linux
and checks macOS load commands against the permitted OS libraries.
No system Python, libc installation, Make, Bison, Flex, patch or compiler is
needed at fetch time. Linux uses `/proc/self/exe` to locate the tool; macOS uses
its native executable-path API.

On ARM64 macOS, use `--platforms=@llvm//platforms:macos_aarch64` to build and
audit the native generator. This selects a macOS executable for repository
fetching; the generated kernel actions still need their Linux execution tools.

The Kconfig C parser is compiled from the supplied checkout. Its parser/support
source digests are embedded and checked before generation: rebuild the tool when
those sources change. Ordinary Kconfig/Makefile changes only require refetching.
`kernel_repository` watches the source tree for that invalidation.

The maintained [examples](examples/README.md) retain their original config
fragments and target names. Their BUILD files now use the generated rule API.

## Repository rule

[`repository.bzl`](repository.bzl) exposes:

```starlark
kernel_repository = use_repo_rule("//:repository.bzl", "kernel_repository")
kernel_repository(
    name = "kernel",
    source = "@linux_source//:Makefile",
    generator = "@generator//:kernel-repository",
    all_backends = True,
)
```

`source` anchors the actual kernel checkout; `generator` is a prebuilt file label,
such as an exported local binary or an executable `http_file`. It cannot refer to
a binary that first needs a Bazel build action: repository fetching precedes
analysis and execution. The rule invokes it with `PATH` empty, and the tool uses
fixed environments for native parsing. It does not discover tools on the host.

`kernel_repository_tool` in the same file downloads a native generator archive
from a checked-in JSON manifest. The manifest has `format: 1` and a `platforms`
dictionary keyed by repository host, such as `linux_x86_64` or `macos_aarch64`.
Each entry contains an immutable archive `url` and Bazel `integrity` hash.
The resulting repository exports `:kernel-repository`. The manifest belongs
with the consumer's dependency pins; workflow artifacts are qualification
outputs and are not permanent download locations. This helper chooses the
repository host independently of the target kernel's architecture.

Repository rules cannot add Bzlmod dependencies or register toolchains in the
consumer. `example.py` creates a runnable consumer workspace from the generated
MODULE template, relocating its local labels to `@kernel` and copying the two
accepted dependency patches into the root module. The example includes the
existing BCR dependency/toolchain setup, not another copy of the parser tool.
`//...` in a consumer addresses its own packages; use `@kernel//...` to address
the generated repository.

Generated `kernel_config` rules accept an optional `pahole` label. The
generated `//:pahole` adapter derives the version from the selected encoder's
C compilation definitions; the configuration carries that same tool for BTF
generation. Without a declared encoder, its Kconfig version is zero. The
generated `//:pahole_test` checks those definitions against `pahole --version`.

`kernel_artifact`, loaded from the generated `//rules:image.bzl`, selects one
declared output from a `kbuild_resolved_build` by its Kbuild path. For example:

```starlark
kernel_artifact(name = "kernel_map", build = ":kernel", path = "System.map")
kernel_artifact(name = "kernel_config", build = ":kernel", path = ".config")
```

The selected path must be produced by that build. Selectors share the build's
action graph and report missing outputs during analysis.

## Initramfs archives

The independent `initramfs` rule builds deterministic, root-owned `newc`
archives with a small C tool compiled for the execution platform. It does not
require a generated kernel repository.

```starlark
load("@linux.bzl//:linux.bzl", "initramfs")

initramfs(
    name = "rootfs",
    executables = {"/init": ":init"},
    files = {"/boot/config": ":kernel_config"},
    character_devices = {"/dev/console": "5:1"},
)
```

Archive paths are absolute. Parent directories are added automatically;
`directories` and `symlinks` can add explicit entries. Files and executables
must each provide one regular file.

## Checks

```sh
# Optional host tracing tool, used only by this test runner:
sudo apt-get install strace
bazel test //:repository_test --platforms=@llvm//platforms:linux_x86_64_musl --test_arg=--strace --test_output=errors
```

This checks the executable's linkage, full generation with empty PATH and poisoned
compiler/Make/Python environment variables, and an exec trace containing only
the same binary. It also checks preserved example requests, parser-version and output-directory
failures. An optional `--reference` can compare Kconfig/Make data with a previously
generated workspace. Omit `--test_arg=--strace` when tracing is unavailable.
Root unit/style checks run with `bazel test //...`; the generator integration
test is explicit because it requires the Linux checkout and native execution.

For a build/link check of the repository rule's output, run in the example:

```sh
bazel build //:x86_64_kernel //:aarch64_kernel @kernel//execution_tools:check \
  --sandbox_block_path=/bin --sandbox_block_path=/usr/bin \
  --sandbox_block_path=/usr/local/bin
```

`native/` owns parser integration and bootstrap inputs; `renderer/` is the
independent renderer/runtime snapshot. `launcher.c` owns the executable's
entry points. `native/bootstrap.bzl` owns compilation and linking; `build.py`
contains the declared namespace and packaging action helpers.
Upstream parser global symbols are namespaced before linking; libc references
remain shared. No dynamically loaded Python extension modules are required.

## CI

The `Kernel repository prototype` workflow checks out a pinned Linux commit, builds
the tool for Linux x86-64 and macOS ARM64, audits native generation on both hosts
and Linux parser execution with an exec trace, exercises the
repository rule, runs the example and platform analysis tests, and builds/links
the x86, debug, LZ4 and ARM64 examples with
host tool directories blocked. It runs on this prototype branch, relevant pull
requests, and manual dispatch. The executables and diagnostics are uploaded
as workflow artifacts. Existing root tests remain independent of local checkouts.
