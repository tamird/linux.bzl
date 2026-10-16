# Prototype qualification

The `Prototype qualification` workflow checks two properties of the static
repository prototype at `0bbb0d3aecc5846a940cc0a9b34fae746566b4ec`. It uses the
matching upstream generator artifact and pinned Linux source. It does not
rebuild the standalone generator.

Dispatch the workflow with `all`, `configuration-parity`, or `source-edit-reuse`
to run the experiments together or independently.

## Configuration parity

`bazel test //qualification:parity` builds pristine Linux `conf` with Bazel,
then supplies the examples' raw requests through `KCONFIG_ALLCONFIG` and
`--allnoconfig`. The native reference uses the selected compiler and linker;
their version output and the effective environment are retained as evidence.
The comparison covers base, debug, LZ4, BTF requests, and ARM64.

Every written symbol is compared. Reports separate missing symbols from
different values. A mismatch can arise from the prototype's compiler-probe
policy or its Kconfig solver; the report does not assume a cause. Native
`conf` uses libc `popen`, so this reference action permits `/bin/sh` while
declaring its source and toolchain inputs. The synthetic `CC_VERSION_TEXT`
label is normalized to the prototype's label; native version scripts still
run the actual executables.

## Source-edit reuse

The workflow builds the x86-64 example, repeats it unchanged, then appends
a unique C comment to `init/main.c` and builds again. All three phases use
the same build options. The source is restored when the step exits.

Compact Bazel execution logs record executed actions and cache hits. A
pinned BuildBuddy decoder projects them to JSONL without expanding each
action's source-tree inputs. `aquery` supplies the complete compiler-output
inventory, including actions absent from the execution log.

`reuse-report.json` distinguishes a valid measurement from successful
selective reuse. The unchanged phase must execute no kernel compiles, and
the edited phase must execute `init/main.o`. The report counts unrelated
compiles and identifies `kernel/fork.o` as a concrete control. Broad
recompilation is a completed measurement with a failed reuse qualification;
it does not make evidence collection fail. This measures source-edit
invalidation, not sharing between configurations or cold-build speed.

## Results on 2026-10-16

The prototype at the revision above does not pass either qualification.

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
