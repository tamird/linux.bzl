# Prototype qualification

The `Prototype qualification` workflow checks configuration parity and compiler
dependency tracking. It builds and tests the current standalone generator with
Bazel before running consumers against the pinned Linux source.

Dispatch the workflow with `configuration-parity`, `source-edit-reuse`,
`compiler-family`, or `examples` to select a gate or the maintained example
matrix. `all` runs them together. The example matrix reuses the
`Kernel repository prototype` workflow.

The separate `compiler-ir` experiment qualifies a possible compiler boundary;
it is not included in `all` while that mechanism is experimental.

## Configuration parity

`bazel test //qualification:parity` builds pristine Linux `conf` with Bazel,
then supplies the examples' raw requests through `KCONFIG_ALLCONFIG` and
`--allnoconfig`. The native reference uses the selected compiler and linker;
their version output and the effective environment are retained as evidence.
The comparison covers base, debug, LZ4, BTF requests, and ARM64.

Compiler probes currently use source-selected fallbacks during analysis.
A compiler built by Bazel is unavailable until execution; running those probes
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

The workflow builds the x86-64 example and its compressed boot image, repeats
it unchanged, then makes four cumulative edits, rebuilding after each:
a unique comment in `init/main.c`, a
comment in `init/do_mounts.h`, a directory entry in `usr/default_cpio_list`, and
a native gzip recipe change that writes four run-specific MTIME header bytes.
All six phases use the same build options. The files are restored on exit.

Compact Bazel execution logs record executed actions and cache hits. A
pinned BuildBuddy decoder projects them to JSONL without expanding each
action's source-tree inputs. `cquery` selects the declared kernel manifest, and
a post-build `aquery` with
`--noinclude_pruned_inputs` authenticates its compiler phases through the actual
product dependencies. The inventory includes actions absent from the execution
log. All phases use the production path-mapping options and the canonical cache.

`reuse-report.json` distinguishes a valid measurement from successful
selective reuse. The baseline must observe the entire compiler inventory, the
unchanged phase must execute no kernel compiles, and the source edit must
execute only the frontend and backend of `init/main.o`. The header edit must
execute the selected `do_mounts` frontends. A backend may reuse an unchanged IR
input; changed IR must have a recorded backend execution or cache hit. The
query also verifies the edited header is not a direct backend input. The
initramfs edit must change the payload and the raw
object that embeds it through `.incbin`, plus the compressed boot payload and
its embedding `piggy.o`, while independent controls stay cached. The native
payload dependency edges authenticate these two assembly consumers. Changed
native `voffset.h`/`zoffset.h` bytes may additionally invalidate only their
authenticated direct compiler consumers, including the declared backend inputs.
The generated C source describing exported symbols and its object must remain
byte-identical after this payload edit, and the compilation must stay cached even
though the kernel object used to generate that source changes.
The gzip header edit must preserve the compressed size, decompressed contents
and generated `piggy.S`, while changing the compressed payload and raw `piggy.o`.
Only the embedding compiler may execute in that phase. A cached embedding action
makes the trial inconclusive, rather than proving input invalidation.
Logical object counts and frontend/backend/single-action counts are reported
separately. This Clang trial requires every reached target C compiler to use the
promoted split path; host-tool compilation cannot satisfy its inventory. Broad
recompilation produces
a report and fails the qualification. This measures dependency invalidation;
it does not qualify sharing between configurations or cold-build speed.

## Compiler IR experiment

`compiler-ir` builds seven real kernel C objects through both the integrated
compiler and a frontend-to-bitcode-to-backend pipeline. The objects include
ordinary kernel code and real-mode boot code with native `-g -m16` flags.
The distributed rules_cc patch accepts LLVM IR inputs through native C++
compilation actions without requesting preprocessor depfiles.

The experiment selects base, LZ4, and `PANIC_TIMEOUT=37` configurations of
one build label. Sequential builds enable Bazel output-path mapping and use
the workflow's disk cache. The unchanged build must execute no selected
compiler actions. At least four LZ4 backends must have bitcode and canonical
action-cache digests identical to the base case, with actual logged cache
hits. Changing `PANIC_TIMEOUT` must change its bitcode, object, and backend
action-cache digest; the report distinguishes backend execution
from a previously cached result for that configuration.

Every split object must match its integrated reference byte for byte.
Reports, native bitcode and objects, and original compact execution logs are
retained even when comparison fails. These checks establish semantic parity
and cache reuse for the selected objects, not a whole-build speedup or the
required full base/debug/BTF/LZ4 family qualification.

## Full compiler family

`compiler-family` builds the maintained base, debug, declared-pahole BTF and
LZ4 qualification companions, including their real kernel and boot products.
Each companion retains its normal request and adds integrated references and
backend diagnostic checks. Post-build precise action graphs authenticate the
compiler phases from the requested products; host tools, references and assembly
do not count toward the four C backend reuse requirement between base and LZ4.
The report requires equal bitcode, equal canonical action-cache digests and
actual LZ4 cache hits, reports counts and fractions, and allows each maintained
configuration to have a different compiler inventory. The unchanged and bounded
PANIC_TIMEOUT control builds remain separate checks.

The earlier seven-object trial established two actual cache hits and matching
integrated/split objects, but failed its four-object threshold. It remains a
separate recorded experiment. Full-family success and the promoted mutation
trial are both required before adoption; neither is implied by that bounded
result or by an image build.

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
