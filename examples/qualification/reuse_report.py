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
    targetId: NotRequired[QueryId]


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
BOOT_HEADERS = ("arch/x86/boot/voffset.h", "arch/x86/boot/zoffset.h")
HEADER_TARGETS = ("init/do_mounts.o", "init/do_mounts_initrd.o", "init/initramfs.o", "init/do_mounts_rd.o")


def mapped(path: str) -> str:
    parts = path.split("/", 2)
    return "bazel-out/mapped/" + parts[2] if len(parts) == 3 and parts[0] == "bazel-out" else path


@dataclass
class Compiler:
    source: str
    outputs: dict[str, Outputs]
    bitcode_digest: str | None
    generated_inputs: dict[str, dict[str, str]]


def read_compilers(path: Path) -> dict[str, Compiler]:
    records = json.loads(path.read_text())
    if not isinstance(records, dict) or not records:
        raise ValueError(f"Missing compiler inventory: {path}")
    result = {}
    owned = set()
    for target, record in records.items():
        match target, record:
            case str(target), {"source": str(source), "outputs": dict(outputs), "bitcode_digest": digest, "generated_inputs": dict(generated_inputs)} if digest is None or isinstance(digest, str):
                pass
            case _:
                raise ValueError(f"Invalid compiler inventory record: {target}, {record}")
        if set(outputs) not in ({"single"}, {"frontend", "backend"}):
            raise ValueError(f"Invalid compiler phases for {target}: {outputs}")
        phases = {}
        for role, paths in outputs.items():
            if not isinstance(paths, list) or not paths or any(not isinstance(item, str) for item in paths):
                raise ValueError(f"Invalid {target} {role} outputs: {paths}")
            identity = tuple(sorted(paths))
            if owned.intersection(identity):
                raise ValueError(f"Duplicate compiler phase outputs: {identity}")
            owned.update(identity)
            phases[role] = identity
        if ("backend" in phases) != bool(digest):
            raise ValueError(f"Missing or unexpected bitcode digest for {target}")
        if set(generated_inputs) != set(phases):
            raise ValueError(f"Missing generated input phase inventory for {target}")
        for role, inputs in generated_inputs.items():
            if not isinstance(inputs, dict) or any(name not in BOOT_HEADERS or not isinstance(value, str) for name, value in inputs.items()):
                raise ValueError(f"Invalid generated inputs for {target} {role}: {inputs}")
        result[target] = Compiler(source, phases, digest, generated_inputs)
    return result


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
        dep_sets = {str(item["id"]): item for item in query.get("depSetOfFiles", [])}

        def input_ids(action: Action) -> set[str]:
            pending = [(str(item), False) for item in action.get("inputDepSetIds", [])]
            active, visited, result = set(), set(), set()
            while pending:
                identity, leaving = pending.pop()
                if leaving:
                    active.remove(identity)
                    visited.add(identity)
                    continue
                if identity in active:
                    raise ValueError(f"Cyclic aquery input set: {identity}")
                if identity in visited:
                    continue
                active.add(identity)
                pending.append((identity, True))
                inputs = dep_sets[identity]
                result.update(map(str, inputs.get("directArtifactIds", [])))
                pending.extend((str(item), False) for item in inputs.get("transitiveDepSetIds", []))
            return result

        producers = {}
        actions = []
        for action in query["actions"]:
            outputs = [str(output) for output in action["outputIds"]]
            previous = producers.get(outputs[0]) if outputs else None
            # Aquery can serialize one shared native action more than once,
            # using distinct nested-set IDs for the same input artifacts.
            # Compare every other field before expanding this duplicate's
            # inputs; ordinary actions never need per-action input closures.
            if previous is not None:
                original = actions[previous]
                if original == action or (
                    {k: v for k, v in original.items() if k != "inputDepSetIds"}
                    == {k: v for k, v in action.items() if k != "inputDepSetIds"}
                    and input_ids(original) == input_ids(action)
                ):
                    continue
            index = len(actions)
            for output in action["outputIds"]:
                identity = str(output)
                if identity not in artifacts or identity in producers:
                    raise ValueError(f"Invalid or conflicting aquery producer: {identity}")
                producers[identity] = index
            actions.append(action)
        return cls(artifacts, identities, actions, producers, dep_sets)

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


def snapshot(directory: Path, name: str, workspace: Path) -> None:
    graph = ActionGraph.read(directory / f"{name}.aquery.json")
    match (directory / f"{name}.manifest-path").read_text().splitlines():
        case [manifest_path]:
            pass
        case other:
            raise ValueError(f"Expected one declared kernel manifest, found {other}")
    manifest_action = graph.producer(manifest_path)
    assert manifest_action["mnemonic"] == "FileWrite", manifest_path
    owner = manifest_action["targetId"]
    manifest_text = (workspace / manifest_path).read_text()
    manifest = json.loads(manifest_text)
    (directory / f"{name}.manifest.json").write_text(manifest_text)
    match manifest:
        case {"compiler": "clang", "roots": list(roots), "artifacts": dict(artifacts), "compilers": dict(compilers)} if roots:
            pass
        case _:
            raise ValueError(f"Invalid kernel manifest: {manifest_path}")
    reached = graph.ancestry([artifacts[root] for root in roots])
    inventory = {}
    paths = {}
    boot_headers = {header: hashlib.sha256((workspace / artifacts[header]).read_bytes()).hexdigest() for header in BOOT_HEADERS}
    for target, compiler in compilers.items():
        match compiler:
            case {"source": str(source), "object": str(obj), "kind": str(kind)} if kind in ("c", "assembly", "codegen", "host"):
                pass
            case _:
                raise ValueError(f"Invalid compiler record: {compiler}")
        if kind == "host" or obj not in reached:
            continue
        assert artifacts[target] in reached, target
        assert artifacts[target] == obj or graph.consumes(artifacts[target], obj), target
        if kind == "c" and not {"frontend", "bitcode"} <= compiler.keys():
            raise ValueError(f"Clang mutation qualification requires split target C: {target}")
        roles = {"single": obj}
        bitcode_digest = None
        if "frontend" in compiler or "bitcode" in compiler:
            match compiler:
                case {"kind": "c", "frontend": str(frontend), "bitcode": str(bitcode)}:
                    pass
                case _:
                    raise ValueError(f"Invalid split compiler record: {compiler}")
            assert graph.producer(bitcode)["mnemonic"] == "Symlink", target
            assert graph.consumes(obj, bitcode) and graph.consumes(bitcode, frontend) and graph.consumes(frontend, source), target
            roles = {"frontend": frontend, "backend": obj}
            bitcode_digest = hashlib.sha256((workspace / bitcode).read_bytes()).hexdigest()
            # This trial edits this exact include. Its only backend effect is
            # through IR; native explicit non-preprocessor reads stay mandatory.
            if target in HEADER_TARGETS:
                header = compilers["init/do_mounts.o"]["source"].removesuffix(".c") + ".h"
                assert not graph.consumes(obj, header), (target, header)
        outputs = {}
        generated_inputs = {}
        for role, path in roles.items():
            action = graph.producer(path)
            assert action["mnemonic"] in ("CppCompile", "Assemble", "KbuildCC"), (target, role)
            outputs[role] = sorted(mapped(graph.artifacts[str(item)]) for item in action["outputIds"])
            generated_inputs[role] = {header: digest for header, digest in boot_headers.items() if graph.consumes(path, artifacts[header])}
        inventory[target] = {"source": source, "outputs": outputs, "bitcode_digest": bitcode_digest, "generated_inputs": generated_inputs}
        paths[target] = obj
    assert inventory, "No product compiler actions"
    # A missing manifest record must not silently shrink the denominator.
    host_objects = {record["object"] for record in compilers.values() if record["kind"] == "host"}
    actual = set()
    for action in graph.actions:
        outputs = {graph.artifacts[str(item)] for item in action["outputIds"]}
        if action.get("targetId") == owner and action["mnemonic"] in ("CppCompile", "Assemble", "KbuildCC") and outputs & reached and not outputs & host_objects:
            actual.add(tuple(sorted(mapped(path) for path in outputs)))
    recorded = {tuple(paths) for record in inventory.values() for paths in record["outputs"].values()}
    assert actual == recorded, {"unrecorded": sorted(actual - recorded), "unreachable": sorted(recorded - actual)}
    for target in ("usr/initramfs_inc_data", ".vmlinux.export.c", PIGGY_SOURCE, GZIP_PAYLOAD):
        paths[target] = artifacts[target]
    # Authenticate the extra boot compiler reached by a changed initramfs.
    assert graph.consumes(compilers["usr/initramfs_data.o"]["object"], artifacts["usr/initramfs_inc_data"]), "usr/initramfs_data.o"
    assert graph.consumes(compilers[PIGGY_OBJECT]["object"], artifacts[GZIP_PAYLOAD]), PIGGY_OBJECT
    assert artifacts["usr/initramfs_data.o"] in graph.ancestry([artifacts[GZIP_PAYLOAD]]), GZIP_PAYLOAD
    controls = ("init/main.o", "kernel/fork.o", "usr/initramfs_data.o", ".vmlinux.export.o", PIGGY_OBJECT,
                "usr/initramfs_inc_data", ".vmlinux.export.c", PIGGY_SOURCE, GZIP_PAYLOAD)
    digests = {target: hashlib.sha256((workspace / paths[target]).read_bytes()).hexdigest() for target in controls}
    digests.update(boot_headers)
    digests["decompressed_gzip"] = hashlib.sha256(gzip.decompress((workspace / paths[GZIP_PAYLOAD]).read_bytes())).hexdigest()
    (directory / f"{name}.compilers.json").write_text(json.dumps(inventory, indent=2) + "\n")
    (directory / f"{name}.digests.json").write_text(json.dumps(digests, indent=2) + "\n")
    sizes = {target: (workspace / paths[target]).stat().st_size for target in controls}
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
            "total_compiler_actions": len(self.expected),
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
    expected_paths = {item for outputs in expected for item in outputs}
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
                    resolved.append(mapped(paths[output_id]))
                case _:
                    raise ValueError(f"Unresolved compiler output in {path}: {output}")
        identity = tuple(sorted(resolved))
        if identity not in expected:
            if expected_paths.intersection(identity):
                raise ValueError(f"Unknown compiler output in {path}: {identity}")
            continue
        if identity in result.executed or identity in result.cached:
            raise ValueError(f"Repeated compiler output in {path}: {identity}")
        (result.cached if hit else result.executed).add(identity)
        result.runners[runner] += 1
    return result


def report(directory: Path) -> dict[str, object]:
    inventories = {name: read_compilers(directory / f"{name}.compilers.json") for name in PHASES}
    compilers = inventories["baseline"]
    identities = {target: (record.source, record.outputs, {role: sorted(inputs) for role, inputs in record.generated_inputs.items()}) for target, record in compilers.items()}
    if any({target: (record.source, record.outputs, {role: sorted(inputs) for role, inputs in record.generated_inputs.items()}) for target, record in value.items()} != identities for value in inventories.values()):
        raise ValueError("Kernel compile output inventory changed between builds")
    expected = {outputs for record in compilers.values() for outputs in record.outputs.values()}
    controls = {target: compilers[target].outputs for target in (
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
    source_compiles = set(controls["init/main.o"].values())
    if missing := source_compiles - edited.executed:
        raise ValueError(f"Edited-source compile did not execute; trial is inconclusive: {sorted(missing)}")
    unrelated = expected - source_compiles
    unrelated_executed = unrelated & edited.executed
    header_required, header_allowed = set(), set()
    for target in HEADER_TARGETS:
        if target not in compilers:
            continue
        roles = compilers[target].outputs
        frontend = roles["frontend"] if "frontend" in roles else roles["single"]
        header_required.add(frontend)
        header_allowed.add(frontend)
        if "backend" in roles and inventories["edited"][target].bitcode_digest != inventories["header"][target].bitcode_digest:
            backend = roles["backend"]
            header_allowed.add(backend)
            if backend not in phases["header"].executed | phases["header"].cached:
                raise ValueError(f"Changed header IR has no observed backend: {target}")
    if missing := header_required - phases["header"].executed:
        raise ValueError(f"Included-header consumers did not execute: {sorted(missing)}")
    incbin_consumers = set(controls["usr/initramfs_data.o"].values()) | set(controls[PIGGY_OBJECT].values())
    if missing := incbin_consumers - phases["incbin"].executed:
        raise ValueError(f"Assembly embedding changed initramfs or boot payload did not execute: {sorted(missing)}")
    incbin_allowed = set(incbin_consumers)
    for target, previous in inventories["header"].items():
        current = inventories["incbin"][target]
        changed_roles = {role for role, inputs in current.generated_inputs.items() if inputs != previous.generated_inputs[role]}
        if not changed_roles:
            continue
        if "backend" in current.outputs and previous.bitcode_digest != current.bitcode_digest:
            changed_roles.add("backend")
        for role in changed_roles:
            outputs = current.outputs[role]
            incbin_allowed.add(outputs)
            if outputs not in phases["incbin"].executed | phases["incbin"].cached:
                raise ValueError(f"Changed native boot header has no observed compiler: {target} {role}")
    digests = {name: json.loads((directory / f"{name}.digests.json").read_text()) for name in PHASES}
    for target in ("usr/initramfs_inc_data", "usr/initramfs_data.o", GZIP_PAYLOAD, PIGGY_OBJECT):
        if digests["header"][target] == digests["incbin"][target]:
            raise ValueError(f"The initramfs edit did not change {target}")
    if len({value["kernel/fork.o"] for value in digests.values()}) != 1:
        raise ValueError("Unrelated kernel/fork.o bytes changed during the trial")
    # The directory-record edit changes vmlinux.o, but not its exported-symbol
    # C source. Rebuilding that source's ancestors must not recompile it.
    for target in (".vmlinux.export.c", ".vmlinux.export.o"):
        if digests["header"][target] != digests["incbin"][target]:
            raise ValueError(f"The initramfs edit unexpectedly changed {target}")
    gzip_consumers = set(controls[PIGGY_OBJECT].values())
    if not gzip_consumers <= phases["gzip"].executed:
        raise ValueError(
            "Assembly embedding the changed gzip payload did not execute; "
            f"the trial is inconclusive: {sorted(gzip_consumers - phases['gzip'].executed)}"
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
    for target in ("init/main.o", "usr/initramfs_data.o", ".vmlinux.export.c", ".vmlinux.export.o", *BOOT_HEADERS):
        if digests["incbin"][target] != digests["gzip"][target]:
            raise ValueError(f"The gzip header edit unexpectedly changed {target}")
    return {
        "measurement_complete": True,
        "compile_inventory_unchanged": True,
        "qualification": {
            "unrelated_logical_objects": len(compilers) - 1,
            "unrelated_compiler_actions": len(unrelated),
            "unrelated_executed_after_edit": len(unrelated_executed),
            "unrelated_not_executed_after_edit": len(unrelated - edited.executed),
            "unrelated_control_reused": not set(controls["kernel/fork.o"].values()) & edited.executed,
            "all_unrelated_compiles_reused": not unrelated_executed,
            "header_edit_recompiled_only_consumers": header_required <= phases["header"].executed <= header_allowed,
            "incbin_edit_recompiled_only_consumer": incbin_consumers <= phases["incbin"].executed <= incbin_allowed,
            "gzip_edit_recompiled_only_consumer": phases["gzip"].executed == gzip_consumers,
        },
        "logical_kernel_objects": len(compilers),
        "compiler_phase_counts": dict(Counter(role for record in compilers.values() for role in record.outputs)),
        "phases": {name: dict(value.report(), compiler_phases={
            role: {outcome: sum(value.outcome(record.outputs[role]) == outcome for record in compilers.values() if role in record.outputs)
                   for outcome in ("executed", "logged_cache_hit", "absent_from_execution_log")}
            for role in ("frontend", "backend", "single")
        }) for name, value in phases.items()},
        "controls": {
            source: {role: {"outputs": outputs, "after_edit": edited.outcome(outputs)} for role, outputs in roles.items()}
            for source, roles in controls.items()
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
