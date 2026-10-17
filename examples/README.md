# linux.bzl prototype examples

These are the maintained examples, updated to consume the static repository
compiler. All five config fragments in `configs/` remain the requested inputs.
The repository rule reads them as data; Kconfig resolution and platform/toolchain
selection happen during Bazel analysis. Debug, BTF and LZ4 requests overlay the
x86 fragment, while ARM64 uses its own fragment and Bazel platform. Unspecified
prompts use the prototype's `allnoconfig` policy.

From the repository root, first build and configure the prototype:

```sh
bazel build //:consumer_support --platforms=@llvm//platforms:linux_x86_64_musl
mkdir -p _tmp/kernel-repository
tar -xzf bazel-bin/consumer-support.tar.gz -C _tmp/kernel-repository
python3 example.py --generated _tmp/kernel-repository/support
```

`example.py` writes only ignored `.generated/` module support and dependency
patches. It preserves this workspace's BUILD file and config fragments. Re-run
it if the source checkout/tool location changes. Fragment edits are tracked by
the repository rule and do not need a separate generation invocation.

Then, from `examples/`:

```sh
bazel test //...
bazel build //:x86_64_kernel //:aarch64_kernel
bazel build //:x86_64_debug_kernel //:x86_64_lz4_kernel
bazel build //:example_initramfs
```

The regular kernel targets produce linked `vmlinux` ELF artifacts. Compression
requests remain part of the resolved configuration; boot-image compression is
not demonstrated by the ELF outputs. `example_initramfs` produces deterministic
`newc` data with `/dev/console`, `/dev/null`, `/proc`, and this README as
`/etc/motd`, using the declared Python execution runtime.

Each kernel target has a corresponding `_config` target exposing its expanded
`.config`, generated header and resolution report. `x86_64_shared_graph_metadata`
now exposes the prototype's action manifest instead of the old graph-repository
metadata format.

The `x86_64_btf_vmlinux` target has an explicit `DEBUG_INFO_BTF=y` requirement
and no declared encoder; its analysis test expects that specific failure.
The manual `x86_64_btf_declared` and `aarch64_btf_declared` targets supply the
generated repository's pahole adapter. Their final link uses that executable,
its source-selected flags, and the kernel's source-built `resolve_btfids`.
`aarch64_btf_declared` also requests the native `arch/arm64/boot/Image` producer;
build it with `--output_groups=kernel,boot` to request both outputs.

The former module-output labels and release-specific graph repositories belong
to the replaced implementation. The prototype currently proves configuration,
individual object actions, native code generation, and kernel build/link.

CI builds the static generator once, then runs independent example jobs:

| CI example | Bazel build target | Analysis test |
| --- | --- | --- |
| x86_64 | `//:x86_64_kernel` | `//:x86_64_config_test` |
| aarch64 | `//:aarch64_kernel` | `//:aarch64_config_test` |
| debug | `//:x86_64_debug_kernel` | `//:debug_config_test` |
| lz4 | `//:x86_64_lz4_kernel` | `//:lz4_config_test` |
| btf-capability-gap | `//:x86_64_btf_vmlinux_config` | `//:btf_capability_test` |
| btf-declared-tool | `//:x86_64_btf_declared_config` | `//:btf_declared_config_test`, `@kernel//:pahole_test` |
| initramfs | `//:example_initramfs` | — |
| graph-metadata | `//:x86_64_shared_graph_metadata` | — |

A separate platform job runs the generated architecture tests, builds the
multi-platform configs, and checks execution tools. Every example build blocks
host tool directories. A separate Linux 6.18 job uses the declared LTS source and
matching generator to build the ARM64 BTF kernel and native boot image, then
runs `aarch64_btf_image_test` to require an ELF `.BTF` section. The encoder-free
analysis test remains a separate check of missing-capability handling.
