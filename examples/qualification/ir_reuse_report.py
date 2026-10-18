"""Measure native IR backend reuse from declared artifacts and compact logs."""

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import TypedDict

from qualification.reuse_report import action_outputs, records

PHASES = ("base", "unchanged", "lz4", "changed")


class ArtifactRecord(TypedDict):
    paths: dict[str, str]
    digests: dict[str, str]


class ActionDigest(TypedDict):
    hash: str
    size_bytes: int


class Outcome(TypedDict):
    cache_hit: bool
    runner: str
    action_digest: ActionDigest


class FrontendOutcome(Outcome):
    output: str


class Execution(TypedDict):
    invocation: str
    hash_function: str
    outcomes: dict[str, Outcome]
    frontend: list[FrontendOutcome]


def string_map(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or any(not isinstance(key, str) or not isinstance(item, str) for key, item in value.items()):
        raise ValueError(f"Expected a string map, got {value}")
    return value


def read_snapshot(path: Path) -> dict[str, ArtifactRecord]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"Expected compiler records in {path}")
    result = {}
    for target, record in value.items():
        match target, record:
            case str(target), {"paths": paths, "digests": digests}:
                result[target] = ArtifactRecord(paths=string_map(paths), digests=string_map(digests))
            case _:
                raise ValueError(f"Invalid compiler record in {path}: {target}, {record}")
    return result


def mapped(path: str) -> str:
    parts = path.split("/", 2)
    return "bazel-out/mapped/" + parts[2] if len(parts) == 3 and parts[0] == "bazel-out" else path


def snapshot(directory: Path, name: str, workspace: Path) -> None:
    actions = list(action_outputs(directory / f"{name}.aquery.json"))
    artifacts = {
        output
        for _, outputs in actions
        for output in outputs
    }
    match [path for mnemonic, outputs in actions if mnemonic == "FileWrite" for path in outputs if path.endswith(".ir.json")]:
        case [report_path]:
            pass
        case reports:
            raise ValueError(f"Expected one native compiler report, got {reports}")
    report = json.loads((workspace / report_path).read_text())
    assert report, "No compiler IR artifacts were requested"
    result = {}
    for target, record in report.items():
        match record:
            case {"frontend": str(frontend), "bitcode": str(bitcode), "integrated": str(integrated), "object": str(obj)}:
                paths = {"frontend": frontend, "bitcode": bitcode, "integrated": integrated, "object": obj}
                missing = set(paths.values()) - artifacts
                assert not missing, (target, missing)
            case _:
                raise ValueError(f"Invalid compiler artifact record: {record}")
        result[target] = {
            "paths": paths,
            "digests": {role: hashlib.sha256((workspace / path).read_bytes()).hexdigest() for role, path in paths.items()},
        }
    (directory / f"{name}.ir.json").write_text(json.dumps(result, indent=2) + "\n")


def execution(path: Path, targets: dict[str, ArtifactRecord]) -> Execution:
    entries = records(path)
    match next(entries, None):
        case {"invocation": str(invocation), "hashFunctionName": str(hash_function)} if invocation and hash_function:
            pass
        case first:
            raise ValueError(f"Missing compact log invocation: {first}")
    selected = {
        mapped(values["paths"][role]): (target, role)
        for target, values in targets.items()
        for role in ("frontend", "integrated", "object")
    }
    paths: dict[int, str] = {}
    seen: set[str] = set()
    outcomes: dict[str, Outcome] = {}
    frontend: list[FrontendOutcome] = []
    for record in entries:
        match record:
            case {"id": int(entry_id), "path": str(path)} if entry_id > 0 and path:
                if entry_id in paths:
                    raise ValueError(f"Duplicate compact output ID {entry_id}")
                paths[entry_id] = path
                continue
            case {"spawn": {
                "outputs": list(outputs), "mnemonic": str(mnemonic),
                "runner": str(runner), "cacheHit": bool(hit),
                "exitCode": int(exit_code), "status": str(status),
                "digest": {"hash": str(action_hash), "sizeBytes": int(action_size)},
            }}:
                pass
            case _:
                raise ValueError(f"Unexpected compact projection {record}")
        if exit_code or status:
            raise ValueError(f"Failed {mnemonic}: {exit_code}, {status}")
        if mnemonic != "CppCompile":
            continue
        digest = ActionDigest(hash=action_hash, size_bytes=action_size)
        resolved = []
        for output in outputs:
            match output:
                case {"outputId": int(output_id)} if output_id in paths:
                    resolved.append(paths[output_id])
                case _:
                    raise ValueError(f"Unknown compiler output {output}")
        for output in resolved:
            identity = selected.get(mapped(output))
            if identity is not None:
                target, role = identity
                key = role + ":" + target
                if key in seen:
                    raise ValueError(f"Duplicate selected compiler {key}")
                seen.add(key)
                if not action_hash or action_size <= 0:
                    raise ValueError(f"Selected compiler {key} has no action-cache digest")
                if role == "frontend":
                    frontend.append({"output": output, "cache_hit": hit, "runner": runner, "action_digest": digest})
                else:
                    outcomes[key] = {"cache_hit": hit, "runner": runner, "action_digest": digest}
    return {"invocation": invocation, "hash_function": hash_function, "outcomes": outcomes, "frontend": frontend}


def summarize(directory: Path) -> None:
    snapshots = {name: read_snapshot(directory / f"{name}.ir.json") for name in PHASES}
    expected = snapshots["base"].keys()
    assert expected and all(values.keys() == expected for values in snapshots.values()), "IR inventories differ"
    phases = {name: execution(directory / f"{name}.execution.jsonl", snapshots[name]) for name in PHASES}
    assert len({value["invocation"] for value in phases.values()}) == len(PHASES), "Repeated invocation IDs"
    assert len({value["hash_function"] for value in phases.values()}) == 1, "Different action-cache digest algorithms"
    parity = {
        name: [target for target, record in values.items() if record["digests"]["integrated"] != record["digests"]["object"]]
        for name, values in snapshots.items()
    }
    equal = [target for target in expected if snapshots["base"][target]["digests"]["bitcode"] == snapshots["lz4"][target]["digests"]["bitcode"]]
    paired = []
    hits = []
    for target in equal:
        base = phases["base"]["outcomes"].get("object:" + target)
        lz4 = phases["lz4"]["outcomes"].get("object:" + target)
        if base is not None and lz4 is not None and base["action_digest"] == lz4["action_digest"]:
            paired.append(target)
            if lz4["cache_hit"]:
                hits.append(target)
    control = "kernel/panic.o"
    changed = all(snapshots["base"][control]["digests"][role] != snapshots["changed"][control]["digests"][role] for role in ("bitcode", "object"))
    control_outcome = phases["changed"]["outcomes"].get("object:" + control)
    base_control = phases["base"]["outcomes"].get("object:" + control)
    control_action_changed = base_control is not None and control_outcome is not None and base_control["action_digest"] != control_outcome["action_digest"]
    unchanged_executions = [
        key for key, outcome in phases["unchanged"]["outcomes"].items() if not outcome["cache_hit"]
    ] + [outcome["output"] for outcome in phases["unchanged"]["frontend"] if not outcome["cache_hit"]]
    unchanged_digests = all(snapshots["base"][target]["digests"] == snapshots["unchanged"][target]["digests"] for target in expected)
    result = {
        "phases": phases, "object_mismatches": parity, "equal_base_lz4_ir": equal,
        "lz4_backend_hits": hits, "panic_changed": changed,
        "paired_backend_actions": paired, "panic_action_changed": control_action_changed,
        "panic_backend_outcome": control_outcome,
        "unchanged_executions": unchanged_executions, "unchanged_digests": unchanged_digests,
    }
    (directory / "ir-reuse.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    assert all(not mismatches for mismatches in parity.values()), parity
    assert len(hits) >= 4, hits
    assert unchanged_digests and not unchanged_executions, result
    # A previous invocation may have cached the changed configuration too.
    # Preserve its actual outcome instead of treating a cache hit as execution.
    assert changed and control_action_changed, result

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("snapshot", "report"))
    parser.add_argument("directory", type=Path)
    parser.add_argument("phase", choices=PHASES, nargs="?")
    args = parser.parse_args()
    if args.command == "snapshot":
        if args.phase is None:
            parser.error("snapshot requires a phase")
        snapshot(args.directory, args.phase, Path(os.environ["BUILD_WORKSPACE_DIRECTORY"]))
    else:
        summarize(args.directory)


if __name__ == "__main__":
    main()
