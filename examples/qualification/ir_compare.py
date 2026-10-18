"""Compare native object bytes and deferred compiler diagnostics."""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
from typing import TypedDict

from qualification.ir_reuse_report import FAMILY, FAMILY_PHASES, read_snapshot


class CompilerRecord(TypedDict):
    bitcode: str
    integrated: str
    object: str
    flags: list[str]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_diagnostics(cases: list[object]) -> dict[str, dict[str, int]]:
    result = {}
    for case in cases:
        match case:
            case {"name": str(name), "bitcode": str(bitcode), "outcomes": list(outcomes)}:
                pass
            case _:
                raise ValueError(f"Invalid diagnostic result: {case}")
        assert name in {"attribute", "assembly"} and name not in result, case
        phases = {}
        for outcome in outcomes:
            match outcome:
                case {"phase": str(phase), "returncode": int(returncode), "stdout": str(stdout), "stderr": str(stderr)}:
                    pass
                case _:
                    raise ValueError(f"Invalid compiler outcome: {outcome}")
            assert phase in {"integrated", "frontend", "backend"} and phase not in phases, outcome
            # The selected toolchain forces ANSI diagnostic colors even when
            # stderr is captured. Preserve the raw report and match its text.
            diagnostic = re.sub(r"\x1b\[[0-9;]*m", "", stderr)
            if phase == "frontend":
                assert returncode == 0, outcome
            else:
                assert returncode == 1, outcome
                if name == "attribute":
                    assert any("error: call to '" in line and "declared with 'error' attribute: linux-bzl-ir-attribute-error" in line for line in diagnostic.splitlines()), outcome
                else:
                    assert "error: linux-bzl-ir-inline-asm" in diagnostic, outcome
            assert not any(marker in stdout + diagnostic for marker in ["PLEASE submit a bug report", "Stack dump:", "Assertion failed", "LLVM ERROR:", "command failed with exit code", "command failed due to signal"]), outcome
            phases[phase] = returncode
        assert phases.keys() == {"integrated", "frontend", "backend"}, case
        result[name] = phases
    assert result.keys() == {"attribute", "assembly"}, result
    return result


def read_case(path: Path, runfiles: Path, output: Path) -> tuple[dict[str, CompilerRecord], list[object]]:
    descriptor = json.loads(path.read_text())
    root = runfiles / descriptor["root"]
    report = json.loads((root / descriptor["report"]).read_text())
    assert report, f"No native compiler records in {path}"
    result = {}
    for target, record in report.items():
        match record:
            case {"bitcode": str(bitcode), "integrated": str(integrated), "object": str(obj), "flags": list(flags)} if all(isinstance(flag, str) for flag in flags):
                pass
            case _:
                raise ValueError(f"Invalid native compiler record: {record}")
        result[target] = {
            "bitcode": digest(root / bitcode),
            "integrated": digest(root / integrated),
            "object": digest(root / obj),
            "flags": flags,
        }
        for role, artifact in {"bitcode": bitcode, "integrated": integrated, "object": obj}.items():
            destination = output / path.stem / (target + "." + role)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / artifact, destination)
    match descriptor.get("diagnostics"):
        case [str(diagnostics)]:
            pass
        case other:
            raise ValueError(f"Expected one diagnostic report in {path}: {other}")
    cases = json.loads((root / diagnostics).read_text())
    assert isinstance(cases, list), cases
    (output / path.stem).mkdir(parents=True, exist_ok=True)
    shutil.copyfile(root / diagnostics, output / path.stem / "diagnostics.json")
    for case in cases:
        match case:
            case {"name": str(name), "bitcode": str(bitcode)}:
                shutil.copyfile(root / bitcode, output / path.stem / (name + ".bc"))
                assert (root / bitcode).stat().st_size > 0, case
            case _:
                raise ValueError(f"Invalid diagnostic artifact: {case}")
    return result, cases


def compare_family(directory: Path) -> None:
    snapshots = {name: read_snapshot(directory / f"{name}.ir.json") for name in FAMILY_PHASES}
    parity = {
        name: [target for target, record in values.items() if record["digests"]["integrated"] != record["digests"]["object"]]
        for name, values in snapshots.items()
    }
    diagnostics, errors = {}, {}
    for name in FAMILY_PHASES:
        cases = json.loads((directory / "diagnostics" / name / "report.json").read_text())
        try:
            diagnostics[name] = check_diagnostics(cases)
        except (AssertionError, ValueError) as error:
            errors[name] = str(error)
    base, changed = snapshots["base"], snapshots["changed"]
    control = "kernel/panic.o"
    result = {
        "objects": {name: len(records) for name, records in snapshots.items()},
        "object_mismatches": parity,
        "diagnostics": diagnostics,
        "diagnostic_errors": errors,
        "panic_ir_changed": base[control]["digests"]["bitcode"] != changed[control]["digests"]["bitcode"],
        "panic_object_changed": base[control]["digests"]["object"] != changed[control]["digests"]["object"],
    }
    (directory / "ir-comparison.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    assert all(result["objects"][name] >= 4 for name in FAMILY), result["objects"]
    assert all(not mismatches for mismatches in parity.values()), parity
    assert not errors, errors
    assert result["panic_ir_changed"] and result["panic_object_changed"], result
    native = json.loads((directory / "base.compiler.json").read_text())
    flags = native["arch/x86/boot/string.o"]["flags"]
    assert "-g" in flags and "-m16" in flags, flags


def main() -> None:
    if sys.argv[1] == "--family-results":
        compare_family(Path(sys.argv[2]))
        return
    runfiles = Path(os.environ["TEST_SRCDIR"]) / os.environ["TEST_WORKSPACE"]
    output = Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"])
    names = ("base", "lz4", "changed")
    assert len(sys.argv[1:]) == len(names), sys.argv
    cases = {name: read_case(runfiles / arg, runfiles, output / "native") for name, arg in zip(names, sys.argv[1:], strict=True)}
    base, lz4, changed = (cases[name][0] for name in names)
    assert base.keys() == lz4.keys() == changed.keys(), "Compiler inventories differ"
    parity = {
        case: [target for target, record in records.items() if record["integrated"] != record["object"]]
        for case, (records, _) in cases.items()
    }
    equal_ir = [target for target in base if base[target]["bitcode"] == lz4[target]["bitcode"]]
    control = "kernel/panic.o"
    realmode = "arch/x86/boot/string.o"
    result = {
        "objects": len(base),
        "object_mismatches": parity,
        "base_lz4_equal_ir": equal_ir,
        "panic_ir_changed": base[control]["bitcode"] != changed[control]["bitcode"],
        "panic_object_changed": base[control]["object"] != changed[control]["object"],
        "realmode_ir_unchanged": base[realmode]["bitcode"] == changed[realmode]["bitcode"],
        "records": {name: records for name, (records, _) in cases.items()},
    }
    diagnostics, diagnostic_errors = {}, {}
    for name, (_, checks) in cases.items():
        try:
            diagnostics[name] = check_diagnostics(checks)
        except (AssertionError, ValueError) as error:
            diagnostic_errors[name] = str(error)
    result.update(diagnostics=diagnostics, diagnostic_errors=diagnostic_errors)
    (output / "ir-comparison.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({key: value for key, value in result.items() if key != "records"}, indent=2))
    assert "-g" in base[realmode]["flags"] and "-m16" in base[realmode]["flags"], base[realmode]["flags"]
    assert all(not mismatches for mismatches in parity.values()), parity
    assert not diagnostic_errors, diagnostic_errors
    assert len(equal_ir) >= 4, equal_ir
    assert result["realmode_ir_unchanged"], result
    assert result["panic_ir_changed"] and result["panic_object_changed"], result


if __name__ == "__main__":
    main()
