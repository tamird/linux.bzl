# Prototype qualification

The `Prototype qualification` workflow checks configuration parity and compiler
dependency tracking. It builds and tests the current standalone generator with
Bazel before running consumers against the pinned Linux source.

Dispatch the workflow with `configuration-parity`, `source-edit-reuse`, or
`examples` to select an experiment or the maintained example matrix. `all` runs
them together. The example matrix reuses the `Kernel repository prototype`
workflow.

## Configuration parity

`bazel test //qualification:parity` builds pristine Linux `conf` with Bazel,
then supplies the examples' raw requests through `KCONFIG_ALLCONFIG` and
`--allnoconfig`. The native reference uses the selected compiler and linker;
their version output and the effective environment are retained as evidence.
The comparison covers base, debug, LZ4, BTF requests, and ARM64.

Compiler probes currently use conservative fallbacks during repository generation.
A compiler built by Bazel may be unavailable at that stage; running those probes
requires a separate execution mechanism. This comparison records the resulting
differences. Source dependency tracking preserves that policy.

Every written symbol is compared. Reports separate missing symbols from
different values. A mismatch can arise from the prototype's compiler-probe
policy or its Kconfig solver; the report does not assume a cause. Native
`conf` uses libc `popen`, so this reference action permits `/bin/sh` while
declaring its source and toolchain inputs. The synthetic `CC_VERSION_TEXT`
label is normalized to the prototype's label; native version scripts still
run the actual executables.

## Source-edit reuse

The workflow builds the x86-64 example, repeats it unchanged, then makes three
cumulative edits, rebuilding after each: a unique comment in `init/main.c`, a
comment in `init/do_mounts.h`, and a directory entry in `usr/default_cpio_list`.
All five phases use the same build options. The files are restored on exit.

Compact Bazel execution logs record executed actions and cache hits. A
pinned BuildBuddy decoder projects them to JSONL without expanding each
action's source-tree inputs. `aquery` supplies the complete compiler-output
inventory, including actions absent from the execution log.

`reuse-report.json` distinguishes a valid measurement from successful
selective reuse. The baseline must observe the entire compiler inventory, the
unchanged phase must execute no kernel compiles, and the source edit must
execute only `init/main.o`. The header edit must execute its selected
`do_mounts` consumers. The initramfs edit must change the payload and the raw
object that embeds it through `.incbin`, while independent controls stay cached.
The generated C source describing exported symbols and its object must remain
byte-identical after this payload edit, and the compilation must stay cached even
though the kernel object used to generate that source changes.
The inventory includes native Bazel C and assembly actions and the original
`KbuildCC` actions, excluding host-tool compilation. Broad recompilation produces
a report and fails the qualification. This measures dependency invalidation;
it does not qualify sharing between configurations or cold-build speed.

## Original prototype results on 2026-10-16

These measurements cover the original prototype at
`0bbb0d3aecc5846a940cc0a9b34fae746566b4ec`, using its matching upstream generator
artifact. It did not pass either qualification. They predate the fixes and
expanded regression checks described above.

### Native configuration comparison

[The native comparison run](https://github.com/tamird/linux.bzl/actions/runs/38922114009)
successfully ran pristine `conf` for every case, with empty diagnostic output.
The strict comparisons found these differences:

| Configuration | Different written values | Present on only one side | Total |
| --- | ---: | ---: | ---: |
| Base | 2 | 46 | 48 |
| Debug | 2 | 48 | 50 |
| LZ4 | 2 | 46 | 48 |
| BTF request | 2 | 51 | 53 |
| ARM64 | 2 | 50 | 52 |

Both sides used Clang/LLD 23.1.2. Native `conf` selected
`INIT_STACK_ALL_ZERO=y`; the prototype selected `INIT_STACK_NONE=y` in every
case. Native compiler probing also selected
`CC_MS_EXTENSIONS="-fms-anonymous-structs"`, while the prototype used
`"-fms-extensions"`. All x86 requests included `LD_ORPHAN_WARN=y`; native
`conf` accepted it, but the prototype reported it unsatisfied.

These traced differences follow from the prototype's unresolved compiler
capability fallbacks and dependent defaults or visibility. They do not
establish an independent Kconfig solver defect. Symbol presence differences
include enabled capabilities, not merely alternate spelling of disabled
symbols. BTF remained unavailable on both sides because pahole was absent;
this was a BTF request comparison, not a successful BTF-enabled build.

### Source edit comparison

[The source edit run](https://github.com/tamird/linux.bzl/actions/runs/38921726164/job/117043062719)
successfully built the x86-64 kernel target in every phase:

| Phase | Executed kernel compiles | Logged compiler cache hits | Build elapsed time |
| --- | ---: | ---: | ---: |
| Baseline | 984 | 0 | 34m 35s |
| Unchanged | 0 | 0 | 48s |
| Comment added to `init/main.c` | 984 | 0 | 57m 31s |

The unchanged build's compact log contained only its invocation record:
the 984 compiler actions were already up to date. The baseline executed the
entire compiler inventory, so there are no unobserved actions in this
denominator. After the edit, all 983 unrelated compiles executed again.
Every observed compiler runner was `processwrapper-sandbox`, and an
independent raw-log count agreed with the report.

The compiler output for `kernel/fork.o` was identical before and after the
edit: SHA-256 `16421fde396beb631bf37cfd05a47612881a75d7d26dc4984925f48bbe8d499f`,
44,512 bytes. This is the raw compiler output before objtool processing.

All three `aquery` inventories had the same output identities and action
keys. Every compiler action declared the shared `prepared_source.tree`
input. The renderer supplies the complete source tree through both
`prepared.files` and `setup.files`, without unused-input pruning. The
comment changes this tree's content digest, invalidating unrelated actions
without changing their action keys. The unchanged `kernel/fork.o` bytes
demonstrate recompilation with an identical result.

These elapsed times describe this sequential run; they are not a controlled
performance comparison with the dynamic planner. This experiment does not
qualify cross-configuration sharing, Aya's kernel-version matrix, booting,
or 32-bit guests.

## Updated dependency tracking results on 2026-10-17

The following checks used
`d9538487b91773a4884ada9002a00171898853a5`, with its generator built and tested
by Bazel. Compiler-probe fallbacks remain unchanged.

[The five-phase reuse run](https://github.com/tamird/linux.bzl/actions/runs/38972452725/job/117215551322)
passed every measurement and selective-reuse check:

| Phase | Executed kernel compiles | Logged compiler cache hits | Build elapsed time |
| --- | ---: | ---: | ---: |
| Baseline | 987 | 0 | 21m 40s |
| Unchanged | 0 | 0 | 2m 16s |
| Comment added to `init/main.c` | 1 | 0 | 6m 18s |
| Comment added to `init/do_mounts.h` | 3 | 0 | 6m 4s |
| Directory added to `usr/default_cpio_list` | 1 | 0 | 6m 9s |

All five phases had the same 987-action compiler inventory. The baseline
executed every action, and an independent count from the projected execution
logs agreed with the report. Every observed compiler runner was
`processwrapper-sandbox`. Actions already up to date were absent from the logs;
the zero logged cache-hit counts do not mean those actions executed again.

The source edit recompiled only `init/main.o`, reusing all 986 unrelated
compiler actions. The header edit recompiled exactly `init/do_mounts.o`,
`init/do_mounts_initrd.o`, and `init/initramfs.o`. The payload edit recompiled
only `usr/initramfs_data.o`; both the payload and this raw embedding object
changed. The independent `kernel/fork.o` control, generated `.vmlinux.export.c`,
and raw `.vmlinux.export.o` remained byte-identical throughout the run. In
particular, changing an input to the export-source generator did not recompile
its unchanged output.

These elapsed times describe this sequential run, not a controlled performance
comparison. The experiment does not qualify sharing between configurations.

[The maintained example matrix](https://github.com/tamird/linux.bzl/actions/runs/38972448852)
passed all eight cases: x86-64, ARM64, debug, LZ4, initramfs, graph metadata,
platform checks, and the BTF capability-gap check. The first four built kernel
images. The BTF case checks the expected missing capability; it does not build
a BTF-enabled kernel. These checks do not exercise booting, Aya's kernel-version
matrix, or 32-bit guests.

Changes after that tested revision document these results and remove the script
materializer's unused handling of extra directory arguments. Its only caller
passes a manifest and destination; the removed loop was never entered.
