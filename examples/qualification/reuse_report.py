"""Measure compilation reuse from compact execution logs and aquery inventories.

The workflow uses BuildBuddy's raw decoder and jq to retain only invocation,
output-path and spawn fields. Read these projected JSONL records individually;
input sets and source-tree contents are never expanded into individual spawns.
"""

import argparse
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
import gzip
import hashlib
import json
import os
from pathlib import Path
from typing import NotRequired, TypeAlias, TypedDict

Outputs: TypeAlias = tuple[str, ...]
QueryId: TypeAlias = str | int


class PathFragment(TypedDict):
    id: QueryId
    label: str
    parentId: NotRequired[QueryId]


class Artifact(TypedDict):
    id: QueryId
    pathFragmentId: QueryId


class Action(TypedDict):
    mnemonic: str
    outputIds: list[QueryId]
    inputDepSetIds: NotRequired[list[QueryId]]


class InputSet(TypedDict):
    id: QueryId
    directArtifactIds: NotRequired[list[QueryId]]
    transitiveDepSetIds: NotRequired[list[QueryId]]


class ActionQuery(TypedDict):
    pathFragments: list[PathFragment]
    artifacts: list[Artifact]
    actions: list[Action]
    depSetOfFiles: NotRequired[list[InputSet]]


PHASES = ("baseline", "unchanged", "edited", "header", "incbin", "gzip")
PIGGY_OBJECT = "arch/x86/boot/compressed/piggy.o"
PIGGY_SOURCE = "arch/x86/boot/compressed/piggy.S"
GZIP_PAYLOAD = "arch/x86/boot/compressed/vmlinux.bin.gz"


def native_target(path: str) -> str | None:
    """Identify the logical Kbuild target in cc_common's owned output directory."""
    _, marker, rest = path.partition("/_objs/")
    owner, _, target = rest.partition("/")
    return target if marker and owner.endswith(".compile") else None


def kernel_compile(mnemonic: str, outputs: Outputs) -> bool:
    return mnemonic == "KbuildCC" or (
        mnemonic in ("CppCompile", "Assemble")
        and any(native_target(path) is not None for path in outputs)
    )


def target_outputs(expected: set[Outputs], target: str) -> Outputs:
    matches = [outputs for outputs in expected if any(
        (native_target(path) or "").startswith(target + "/")
        or path.endswith("/" + target) and native_target(path) is None
        for path in outputs
    )]
    match matches:
        case [outputs]:
            return outputs
        case _:
            raise ValueError(f"Expected one compile for {target}, found {matches}")


def records(path: Path) -> Iterator[dict[str, object]]:
    with path.open() as stream:
        for number, line in enumerate(stream, 1):
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError(f"Expected an object at {path}:{number}")
            yield record


def artifact_paths(query: ActionQuery) -> dict[str, str]:
    fragments = {str(item["id"]): item for item in query["pathFragments"]}

    def filename(fragment_id: str) -> str:
        parts = []
        visited = set()
        while fragment_id != "0":
            if fragment_id in visited:
                raise ValueError(f"Cyclic aquery path fragment: {fragment_id}")
            visited.add(fragment_id)
            fragment = fragments[fragment_id]
            parts.append(fragment["label"])
            fragment_id = str(fragment.get("parentId", 0))
        return "/".join(reversed(parts))

    return {
        str(item["id"]): filename(str(item["pathFragmentId"]))
        for item in query["artifacts"]
    }


@dataclass
class ActionGraph:
    artifacts: dict[str, str]
    identities: dict[str, str]
    actions: list[Action]
    producers: dict[str, int]
    dep_sets: dict[str, InputSet]

    @classmethod
    def read(cls, path: Path) -> "ActionGraph":
        query: ActionQuery = json.loads(path.read_text())
        artifacts = artifact_paths(query)
        identities = {name: identity for identity, name in artifacts.items()}
        if len(identities) != len(artifacts):
            raise ValueError("Aquery has duplicate artifact paths")
        producers = {}
        for index, action in enumerate(query["actions"]):
            for output in action["outputIds"]:
                identity = str(output)
                if identity not in artifacts or identity in producers:
                    raise ValueError(f"Invalid or conflicting aquery producer: {identity}")
                producers[identity] = index
        return cls(artifacts, identities, query["actions"], producers, {str(item["id"]): item for item in query.get("depSetOfFiles", [])})

    def outputs(self) -> Iterator[tuple[str, Outputs]]:
        for action in self.actions:
            yield action["mnemonic"], tuple(sorted(self.artifacts[str(output)] for output in action["outputIds"]))

    def ancestry(self, roots: list[str]) -> set[str]:
        """Follow actual artifact/action/depset edges without flattening header sets."""
        identities = self.identities
        missing = [path for path in roots if path not in identities or identities[path] not in self.producers]
        if missing:
            raise ValueError(f"Requested outputs lack native producers: {missing}")
        stack = [("artifact", identities[path], False) for path in roots]
        active, visited, reached = set(), set(), set()
        while stack:
            kind, identity, leaving = stack.pop()
            node = kind, identity
            if leaving:
                active.remove(node)
                visited.add(node)
                continue
            if node in active:
                raise ValueError(f"Cyclic native action graph at {node}")
            if node in visited:
                continue
            active.add(node)
            stack.append((kind, identity, True))
            if kind == "artifact":
                reached.add(self.artifacts[identity])
                if identity in self.producers:
                    stack.append(("action", str(self.producers[identity]), False))
            elif kind == "action":
                stack.extend(("depset", str(item), False) for item in self.actions[int(identity)].get("inputDepSetIds", []))
            else:
                inputs = self.dep_sets[identity]
                stack.extend(("artifact", str(item), False) for item in inputs.get("directArtifactIds", []))
                stack.extend(("depset", str(item), False) for item in inputs.get("transitiveDepSetIds", []))
        return reached

    def producer(self, path: str) -> Action:
        return self.actions[self.producers[self.identities[path]]]

    def consumes(self, output: str, required: str) -> bool:
        wanted = self.identities[required]
        pending = list(self.producer(output).get("inputDepSetIds", []))
        visited = set()
        while pending:
            identity = str(pending.pop())
            if identity in visited:
                continue
            visited.add(identity)
            inputs = self.dep_sets[identity]
            if wanted in {str(item) for item in inputs.get("directArtifactIds", [])}:
                return True
            pending.extend(inputs.get("transitiveDepSetIds", []))
        return False


def action_outputs(path: Path) -> Iterator[tuple[str, Outputs]]:
    query = json.loads(path.read_text())
    artifacts = artifact_paths(query)
    for action in query["actions"]:
        yield action["mnemonic"], tuple(sorted(artifacts[str(output)] for output in action["outputIds"]))


def inventory(path: Path) -> set[Outputs]:
    return {outputs for mnemonic, outputs in action_outputs(path) if kernel_compile(mnemonic, outputs)}


def snapshot(directory: Path, name: str, workspace: Path) -> None:
    query = directory / f"{name}.aquery.json"
    expected = inventory(query)
    paths = {}
    for target in ("init/main.o", "kernel/fork.o", "usr/initramfs_data.o", ".vmlinux.export.o", PIGGY_OBJECT):
        match [path for path in target_outputs(expected, target) if path.endswith(".o")]:
            case [path]:
                paths[target] = path
            case other:
                raise ValueError(f"Expected one raw object for {target}, found {other}")
    for target in ("usr/initramfs_inc_data", ".vmlinux.export.c", PIGGY_SOURCE, GZIP_PAYLOAD):
        match [path for _, outputs in action_outputs(query) for path in outputs
               if path.endswith(".artifacts/" + target)]:
            case [path]:
                paths[target] = path
            case other:
                raise ValueError(f"Expected one generated {target}, found {other}")
    digests = {target: hashlib.sha256((workspace / path).read_bytes()).hexdigest() for target, path in paths.items()}
    digests["decompressed_gzip"] = hashlib.sha256(gzip.decompress((workspace / paths[GZIP_PAYLOAD]).read_bytes())).hexdigest()
    (directory / f"{name}.digests.json").write_text(json.dumps(digests, indent=2) + "\n")
    sizes = {target: (workspace / path).stat().st_size for target, path in paths.items()}
    (directory / f"{name}.sizes.json").write_text(json.dumps(sizes, indent=2) + "\n")


@dataclass
class Phase:
    invocation: str
    expected: set[Outputs]
    executed: set[Outputs]
    cached: set[Outputs]
    runners: Counter[str]
    mnemonics: Counter[tuple[str, str]]

    def outcome(self, outputs: Outputs) -> str:
        if outputs in self.executed:
            return "executed"
        if outputs in self.cached:
            return "logged_cache_hit"
        return "absent_from_execution_log"

    def report(self) -> dict[str, object]:
        return {
            "invocation": self.invocation,
            "total_kernel_compiles": len(self.expected),
            "executed": len(self.executed),
            "logged_cache_hits": len(self.cached),
            "absent_from_execution_log": len(self.expected - self.executed - self.cached),
            "runners": dict(self.runners),
            "all_mnemonics": {
                f"{name}/{kind}": count
                for (name, kind), count in sorted(self.mnemonics.items())
            },
            "executed_outputs": sorted(self.executed),
            "logged_cache_hit_outputs": sorted(self.cached),
        }


def phase(path: Path, expected: set[Outputs]) -> Phase:
    entries = records(path)
    match next(entries, None):
        case {"invocation": str(invocation)} if invocation:
            result = Phase(invocation, expected, set(), set(), Counter(), Counter())
        case first:
            raise ValueError(f"Missing initial invocation in {path}: {first}")
    paths: dict[int, str] = {}
    for record in entries:
        match record:
            case {"id": int(entry_id), "path": str(output_path)} if entry_id > 0 and output_path:
                if entry_id in paths:
                    raise ValueError(f"Duplicate output entry ID in {path}: {entry_id}")
                paths[entry_id] = output_path
                continue
            case {"spawn": {
                "outputs": list(outputs), "mnemonic": str(mnemonic),
                "runner": str(runner), "cacheHit": bool(hit),
                "exitCode": int(exit_code), "status": str(status),
            }} if mnemonic:
                pass
            case _:
                raise ValueError(f"Unexpected compact-log projection in {path}: {record}")
        if status or exit_code:
            raise ValueError(f"Failed {mnemonic} spawn in {path}: {status}, exit {exit_code}")
        result.mnemonics[(mnemonic, "cache_hit" if hit else "executed")] += 1
        if mnemonic not in ("KbuildCC", "CppCompile", "Assemble"):
            continue
        resolved = []
        for output in outputs:
            match output:
                case {"outputId": int(output_id)} if output_id in paths:
                    resolved.append(paths[output_id])
                case _:
                    raise ValueError(f"Unresolved compiler output in {path}: {output}")
        identity = tuple(sorted(resolved))
        if not kernel_compile(mnemonic, identity):
            continue
        if identity not in expected:
            raise ValueError(f"Unknown compiler output in {path}: {identity}")
        if identity in result.executed or identity in result.cached:
            raise ValueError(f"Repeated compiler output in {path}: {identity}")
        (result.cached if hit else result.executed).add(identity)
        result.runners[runner] += 1
    return result


def report(directory: Path) -> dict[str, object]:
    inventories = {
        name: inventory(directory / f"{name}.aquery.json")
        for name in PHASES
    }
    expected = inventories["baseline"]
    if not expected or any(value != expected for value in inventories.values()):
        raise ValueError("Kernel compile output inventory changed between builds")
    controls = {target: target_outputs(expected, target) for target in (
        "init/main.o", "kernel/fork.o", "init/do_mounts.o", "init/do_mounts_initrd.o", "init/initramfs.o", "usr/initramfs_data.o", PIGGY_OBJECT,
    )}
    phases = {
        name: phase(directory / f"{name}.execution.jsonl", expected)
        for name in inventories
    }
    if len({value.invocation for value in phases.values()}) != len(phases):
        raise ValueError("Build phases reused an invocation log")
    if expected - phases["baseline"].executed - phases["baseline"].cached:
        raise ValueError("Baseline did not observe every compiler action; inventory includes unmeasured actions")
    if phases["unchanged"].executed:
        raise ValueError("Unchanged build executed kernel compiles; edit comparison is not isolated")
    edited = phases["edited"]
    source_compile = controls["init/main.o"]
    if source_compile not in edited.executed:
        raise ValueError(
            "Edited-source compile did not execute; investigate source invalidation or a preexisting "
            f"cache entry before interpreting reuse: {edited.outcome(source_compile)}"
        )
    unrelated = expected - {source_compile}
    unrelated_executed = unrelated & edited.executed
    # Each of these sources directly includes init/do_mounts.h in the pinned
    # kernel, including initramfs.c's early rootfs setup.
    header_consumers = {controls[target] for target in ("init/do_mounts.o", "init/do_mounts_initrd.o", "init/initramfs.o")}
    if any((native_target(path) or "").startswith("init/do_mounts_rd.o/") or path.endswith("/init/do_mounts_rd.o")
           for outputs in expected for path in outputs):
        header_consumers.add(target_outputs(expected, "init/do_mounts_rd.o"))
    if missing := header_consumers - phases["header"].executed:
        raise ValueError(f"Included-header consumers did not execute: {sorted(missing)}")
    if controls["usr/initramfs_data.o"] not in phases["incbin"].executed:
        raise ValueError("Assembly embedding the changed initramfs payload did not execute")
    digests = {name: json.loads((directory / f"{name}.digests.json").read_text()) for name in PHASES}
    for target in ("usr/initramfs_inc_data", "usr/initramfs_data.o"):
        if digests["header"][target] == digests["incbin"][target]:
            raise ValueError(f"The initramfs edit did not change {target}")
    if len({value["kernel/fork.o"] for value in digests.values()}) != 1:
        raise ValueError("Unrelated kernel/fork.o bytes changed during the trial")
    # The directory-record edit changes vmlinux.o, but not its exported-symbol
    # C source. Rebuilding that source's ancestors must not recompile it.
    for target in (".vmlinux.export.c", ".vmlinux.export.o"):
        if digests["header"][target] != digests["incbin"][target]:
            raise ValueError(f"The initramfs edit unexpectedly changed {target}")
    if controls[PIGGY_OBJECT] not in phases["gzip"].executed:
        raise ValueError(
            "Assembly embedding the changed gzip payload did not execute; "
            f"the trial is inconclusive: {phases['gzip'].outcome(controls[PIGGY_OBJECT])}"
        )
    sizes = {name: json.loads((directory / f"{name}.sizes.json").read_text()) for name in PHASES}
    if sizes["incbin"][GZIP_PAYLOAD] != sizes["gzip"][GZIP_PAYLOAD]:
        raise ValueError("The gzip header edit changed the payload size")
    if digests["incbin"][PIGGY_SOURCE] != digests["gzip"][PIGGY_SOURCE]:
        raise ValueError("The gzip header edit changed generated piggy.S")
    if digests["incbin"]["decompressed_gzip"] != digests["gzip"]["decompressed_gzip"]:
        raise ValueError("The gzip header edit changed the decompressed payload")
    for target in (GZIP_PAYLOAD, PIGGY_OBJECT):
        if digests["incbin"][target] == digests["gzip"][target]:
            raise ValueError(f"The gzip header edit did not change {target}")
    for target in ("init/main.o", "usr/initramfs_data.o", ".vmlinux.export.c", ".vmlinux.export.o"):
        if digests["incbin"][target] != digests["gzip"][target]:
            raise ValueError(f"The gzip header edit unexpectedly changed {target}")
    return {
        "measurement_complete": True,
        "compile_inventory_unchanged": True,
        "qualification": {
            "unrelated_compiles": len(unrelated),
            "unrelated_executed_after_edit": len(unrelated_executed),
            "unrelated_not_executed_after_edit": len(unrelated - edited.executed),
            "unrelated_control_reused": controls["kernel/fork.o"] not in edited.executed,
            "all_unrelated_compiles_reused": not unrelated_executed,
            "header_edit_recompiled_only_consumers": phases["header"].executed == header_consumers,
            "incbin_edit_recompiled_only_consumer": phases["incbin"].executed == {controls["usr/initramfs_data.o"]},
            "gzip_edit_recompiled_only_consumer": phases["gzip"].executed == {controls[PIGGY_OBJECT]},
        },
        "phases": {name: value.report() for name, value in phases.items()},
        "controls": {
            source: {"outputs": outputs, "after_edit": edited.outcome(outputs)}
            for source, outputs in controls.items()
        },
        "digests": digests,
        "sizes": sizes,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("snapshot", "report"))
    parser.add_argument("directory", type=Path)
    parser.add_argument("phase", choices=PHASES, nargs="?")
    args = parser.parse_args()
    if args.command == "snapshot":
        if args.phase is None:
            parser.error("snapshot requires a phase")
        snapshot(args.directory, args.phase, Path(os.environ["BUILD_WORKING_DIRECTORY"]))
        return
    if args.phase is not None:
        parser.error("report does not take a phase")
    result = report(args.directory)
    (args.directory / "reuse-report.json").write_text(json.dumps(result, indent=2) + "\n")
    # Keep every output identity in the artifact, while making CI logs readable.
    result["phases"] = {
        name: {key: value for key, value in measured.items() if not key.endswith("_outputs")}
        for name, measured in result["phases"].items()
    }
    print(json.dumps(result, indent=2))
    if not all(result["qualification"][key] for key in (
        "all_unrelated_compiles_reused", "header_edit_recompiled_only_consumers", "incbin_edit_recompiled_only_consumer", "gzip_edit_recompiled_only_consumer",
    )):
        raise SystemExit("Kernel dependency reuse qualification failed; see reuse-report.json")


if __name__ == "__main__":
    main()
