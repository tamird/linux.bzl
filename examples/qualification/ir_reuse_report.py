"""Measure native IR backend reuse from declared artifacts and compact logs."""

import argparse
import hashlib
import itertools
import json
import os
import shutil
from pathlib import Path
from typing import TypedDict

from qualification.reuse_report import ActionGraph, action_outputs, records

PHASES = ("base", "unchanged", "lz4", "changed")
FAMILY = ("base", "debug", "btf", "lz4")
FAMILY_PHASES = ("base", "unchanged", "lz4", "debug", "btf", "changed")


class ArtifactRecord(TypedDict):
    paths: dict[str, str]
    digests: dict[str, str]


class CompilerRecord(TypedDict):
    source: str
    frontend: str
    bitcode: str
    integrated: str
    object: str
    flags: list[str]


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


def snapshot(directory: Path, name: str, workspace: Path, family: bool = False) -> None:
    query_path = directory / f"{name}.aquery.json"
    graph = ActionGraph.read(query_path) if family else None
    actions = list(graph.outputs() if graph is not None else action_outputs(query_path))
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
    report: dict[str, CompilerRecord] = json.loads((workspace / report_path).read_text())
    assert report, "No compiler IR artifacts were requested"
    if graph is not None:
        authenticate(directory, name, workspace, report_path, report, graph)
        (directory / f"{name}.compiler.json").write_text(json.dumps(report, indent=2) + "\n")
    del graph
    result = {}
    for target, record in report.items():
        match record:
            case {"frontend": str(frontend), "bitcode": str(bitcode), "integrated": str(integrated), "object": str(obj)}:
                paths = {"frontend": frontend, "bitcode": bitcode, "integrated": integrated, "object": obj}
                missing = set(paths.values()) - artifacts
                assert not missing, (target, missing)
            case _:
                raise ValueError(f"Invalid compiler artifact record: {record}")
        digests = {role: hashlib.sha256((workspace / path).read_bytes()).hexdigest() for role, path in paths.items()}
        result[target] = {"paths": paths, "digests": digests}
        if family and digests["integrated"] != digests["object"]:
            for role, path in paths.items():
                destination = directory / "mismatches" / name / (target + "." + role)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(workspace / path, destination)
    (directory / f"{name}.ir.json").write_text(json.dumps(result, indent=2) + "\n")


def authenticate(directory: Path, name: str, workspace: Path, report_path: str, report: dict[str, CompilerRecord], graph: ActionGraph) -> None:
    manifest_path = report_path.removesuffix(".ir.json") + ".json"
    assert graph.producer(manifest_path)["mnemonic"] == "FileWrite", manifest_path
    manifest = json.loads((workspace / manifest_path).read_text())
    match manifest:
        case {"roots": list(roots), "artifacts": artifacts, "compilers": dict(compilers), "config": str(config)} if roots and all(isinstance(root, str) for root in roots):
            artifacts = string_map(artifacts)
        case _:
            raise ValueError(f"Invalid native product manifest: {manifest_path}")
    products = {root: artifacts[root] for root in roots}
    reached = graph.ancestry(list(products.values()))
    inventory: dict[str, dict[str, str]] = {}
    for target, compiler in compilers.items():
        match compiler:
            case {"source": str(source), "object": str(obj), "kind": str(kind)} if kind in ("c", "host", "assembly", "codegen"):
                pass
            case _:
                raise ValueError(f"Invalid native compiler record: {compiler}")
        if obj in reached:
            assert graph.producer(obj)["mnemonic"] in ("CppCompile", "Assemble"), (target, obj)
            inventory[target] = {"source": source, "object": obj, "kind": kind}
    selected_c = {target for target, compiler in inventory.items() if compiler["kind"] == "c"}
    assert selected_c and selected_c == report.keys(), {"missing": sorted(selected_c - report.keys()), "unreachable": sorted(report.keys() - selected_c)}
    for target, record in report.items():
        match record:
            case {"source": str(source), "frontend": str(frontend), "bitcode": str(bitcode), "integrated": str(integrated), "object": str(obj)}:
                pass
            case _:
                raise ValueError(f"Invalid native C compiler record: {record}")
        assert source.endswith(".c") and inventory[target]["source"] == source, (target, source)
        assert artifacts[target] in reached and obj in reached, (target, obj)
        assert artifacts[target] == obj or graph.consumes(artifacts[target], obj), (target, obj)
        assert all(graph.producer(path)["mnemonic"] == "CppCompile" for path in (frontend, integrated, obj)), target
        assert graph.producer(bitcode)["mnemonic"] == "Symlink", target
        assert graph.consumes(obj, bitcode) and graph.consumes(bitcode, frontend) and graph.consumes(frontend, source), target
    match manifest["compiler_diagnostics"]:
        case list(diagnostics) if all(isinstance(path, str) for path in diagnostics):
            pass
        case other:
            raise ValueError(f"Invalid diagnostic artifact inventory: {other}")
    match [path for path in diagnostics if path.endswith(".ir-diagnostics.json")]:
        case [diagnostic_report]:
            pass
        case other:
            raise ValueError(f"Expected one compiler diagnostic report: {other}")
    destination = directory / "diagnostics" / name
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(workspace / diagnostic_report, destination / "report.json")
    cases = json.loads((workspace / diagnostic_report).read_text())
    assert isinstance(cases, list), cases
    for case in cases:
        match case:
            case {"name": str(case_name), "bitcode": str(bitcode)} if case_name in ("attribute", "assembly"):
                assert bitcode in diagnostics, (bitcode, diagnostics)
                source = workspace / bitcode
                assert source.stat().st_size > 0, bitcode
                shutil.copyfile(source, destination / (case_name + ".bc"))
            case _:
                raise ValueError(f"Invalid diagnostic artifact: {case}")
    result = {
        "products": {root: {"path": path, "sha256": hashlib.sha256((workspace / path).read_bytes()).hexdigest()} for root, path in products.items()},
        "compilers": inventory,
        "compiler_counts": {kind: sum(item["kind"] == kind for item in inventory.values()) for kind in ("c", "host", "assembly", "codegen")},
        "config": (workspace / config).read_text(),
        "reached_artifacts": len(reached),
    }
    (directory / f"{name}.products.json").write_text(json.dumps(result, indent=2) + "\n")


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


def summarize(directory: Path, family: bool = False) -> None:
    names = FAMILY_PHASES if family else PHASES
    snapshots = {name: read_snapshot(directory / f"{name}.ir.json") for name in names}
    expected = snapshots["base"].keys()
    assert expected and all(snapshots.values()), "Empty native compiler inventory"
    if not family:
        assert all(values.keys() == expected for values in snapshots.values()), "IR inventories differ"
    assert snapshots["unchanged"].keys() == expected, "Unchanged compiler inventory differs"
    phases = {name: execution(directory / f"{name}.execution.jsonl", snapshots[name]) for name in names}
    assert len({value["invocation"] for value in phases.values()}) == len(names), "Repeated invocation IDs"
    assert len({value["hash_function"] for value in phases.values()}) == 1, "Different action-cache digest algorithms"
    parity = {
        name: [target for target, record in values.items() if record["digests"]["integrated"] != record["digests"]["object"]]
        for name, values in snapshots.items()
    }
    products = {name: json.loads((directory / f"{name}.products.json").read_text()) for name in names} if family else {}
    common = expected & snapshots["lz4"].keys()
    if family:
        common = {target for target in common if mapped(products["base"]["compilers"][target]["source"]) == mapped(products["lz4"]["compilers"][target]["source"])}
    equal = [target for target in sorted(common) if snapshots["base"][target]["digests"]["bitcode"] == snapshots["lz4"][target]["digests"]["bitcode"]]
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
        "counts": {name: len(values) for name, values in snapshots.items()},
        "base_lz4": {"common_c_compilers": len(common), "equal_ir": len(equal), "equal_backend_actions": len(paired), "actual_backend_cache_hits": len(hits), "hit_fraction_of_lz4_c": len(hits) / len(snapshots["lz4"])},
    }
    if family:
        memberships = {}
        for size in range(2, len(FAMILY) + 1):
            for members in itertools.combinations(FAMILY, size):
                shared = set.intersection(*(set(snapshots[member]) for member in members))
                identities = []
                for target in sorted(shared):
                    outcomes = [phases[member]["outcomes"].get("object:" + target) for member in members]
                    if all(outcome is not None for outcome in outcomes):
                        digests = [outcome["action_digest"] for outcome in outcomes if outcome is not None]
                    else:
                        continue
                    if all(digest == digests[0] for digest in digests) and len({mapped(products[member]["compilers"][target]["source"]) for member in members}) == 1:
                        assert len({snapshots[member][target]["digests"]["bitcode"] for member in members}) == 1, (members, target)
                        identities.append(target)
                memberships["/".join(members)] = {"common_c_compilers": len(shared), "equal_native_backend_actions": identities}
        result["memberships"] = memberships
        result["products"] = products
        result["family_checks"] = {
            "all_vmlinux": all("vmlinux" in products[name]["products"] for name in FAMILY),
            "both_boot_images": all("arch/x86/boot/bzImage" in products[name]["products"] for name in ("base", "lz4")),
            "per_variant_c_floor": all(len(snapshots[name]) >= 4 for name in FAMILY),
            "debug_config": all(line in products["debug"]["config"].splitlines() for line in ('CONFIG_DEBUG_KERNEL=y', 'CONFIG_LOCALVERSION="-debug"')),
            "btf_config": "CONFIG_DEBUG_INFO_BTF=y" in products["btf"]["config"].splitlines(),
            "lz4_config": "CONFIG_KERNEL_LZ4=y" in products["lz4"]["config"].splitlines(),
            "base_panic": "CONFIG_PANIC_TIMEOUT=0" in products["base"]["config"].splitlines(),
            "changed_panic": "CONFIG_PANIC_TIMEOUT=37" in products["changed"]["config"].splitlines(),
        }
    (directory / "ir-reuse.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({key: value for key, value in result.items() if key not in ("phases", "products")}, indent=2))
    if family:
        assert all(result["family_checks"].values()), result["family_checks"]
    else:
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
    parser.add_argument("phase", choices=FAMILY_PHASES, nargs="?")
    parser.add_argument("--family", action="store_true")
    args = parser.parse_args()
    if args.command == "snapshot":
        if args.phase is None:
            parser.error("snapshot requires a phase")
        snapshot(args.directory, args.phase, Path(os.environ["BUILD_WORKSPACE_DIRECTORY"]), args.family)
    else:
        summarize(args.directory, args.family)


if __name__ == "__main__":
    main()
