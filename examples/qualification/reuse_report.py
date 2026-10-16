"""Measure compilation reuse from compact execution logs and aquery inventories.

The workflow uses BuildBuddy's raw decoder and jq to retain only invocation,
output-path and spawn fields. Read these projected JSONL records individually;
input sets and source-tree contents are never expanded into individual spawns.
"""

import argparse
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
import json
from pathlib import Path
from typing import TypeAlias

Outputs: TypeAlias = tuple[str, ...]


def records(path: Path) -> Iterator[dict[str, object]]:
    with path.open() as stream:
        for number, line in enumerate(stream, 1):
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError(f"Expected an object at {path}:{number}")
            yield record


def inventory(path: Path) -> set[Outputs]:
    query = json.loads(path.read_text())
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

    artifacts = {
        str(item["id"]): filename(str(item["pathFragmentId"]))
        for item in query["artifacts"]
    }
    return {
        tuple(sorted(artifacts[str(output)] for output in action["outputIds"]))
        for action in query["actions"]
        if action["mnemonic"] == "KbuildCC"
    }


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
        if mnemonic != "KbuildCC":
            continue
        resolved = []
        for output in outputs:
            match output:
                case {"outputId": int(output_id)} if output_id in paths:
                    resolved.append(paths[output_id])
                case _:
                    raise ValueError(f"Unresolved compiler output in {path}: {output}")
        identity = tuple(sorted(resolved))
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
        for name in ("baseline", "unchanged", "edited")
    }
    expected = inventories["baseline"]
    if not expected or any(value != expected for value in inventories.values()):
        raise ValueError("Kernel compile output inventory changed between builds")
    controls = {}
    for source in ("init/main.o", "kernel/fork.o"):
        matches = [outputs for outputs in expected if any(path.endswith("/" + source) for path in outputs)]
        match matches:
            case [outputs]:
                controls[source] = outputs
            case _:
                raise ValueError(f"Expected one compile for {source}, found {matches}")
    phases = {
        name: phase(directory / f"{name}.execution.jsonl", expected)
        for name in inventories
    }
    if len({value.invocation for value in phases.values()}) != len(phases):
        raise ValueError("Build phases reused an invocation log")
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
    return {
        "measurement_complete": True,
        "compile_inventory_unchanged": True,
        "qualification": {
            "unrelated_compiles": len(unrelated),
            "unrelated_executed_after_edit": len(unrelated_executed),
            "unrelated_not_executed_after_edit": len(unrelated - edited.executed),
            "unrelated_control_reused": controls["kernel/fork.o"] not in edited.executed,
            "all_unrelated_compiles_reused": not unrelated_executed,
        },
        "phases": {name: value.report() for name, value in phases.items()},
        "controls": {
            source: {"outputs": outputs, "after_edit": edited.outcome(outputs)}
            for source, outputs in controls.items()
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    result = report(args.directory)
    (args.directory / "reuse-report.json").write_text(json.dumps(result, indent=2) + "\n")
    # Keep every output identity in the artifact, while making CI logs readable.
    result["phases"] = {
        name: {key: value for key, value in measured.items() if not key.endswith("_outputs")}
        for name, measured in result["phases"].items()
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
