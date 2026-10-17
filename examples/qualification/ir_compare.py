"""Compare the actual native objects and compiler-owned IR of three kernels."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
from typing import TypedDict


class CompilerRecord(TypedDict):
    bitcode: str
    integrated: str
    object: str
    flags: list[str]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_case(path: Path, runfiles: Path, output: Path) -> dict[str, CompilerRecord]:
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
    return result


def main() -> None:
    runfiles = Path(os.environ["TEST_SRCDIR"]) / os.environ["TEST_WORKSPACE"]
    output = Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"])
    base, lz4, changed = [read_case(runfiles / arg, runfiles, output / "native") for arg in sys.argv[1:]]
    assert base.keys() == lz4.keys() == changed.keys(), "Compiler inventories differ"
    parity = {
        case: [target for target, record in records.items() if record["integrated"] != record["object"]]
        for case, records in {"base": base, "lz4": lz4, "changed": changed}.items()
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
        "records": {"base": base, "lz4": lz4, "changed": changed},
    }
    (output / "ir-comparison.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({key: value for key, value in result.items() if key != "records"}, indent=2))
    assert "-g" in base[realmode]["flags"] and "-m16" in base[realmode]["flags"], base[realmode]["flags"]
    assert all(not mismatches for mismatches in parity.values()), parity
    assert len(equal_ir) >= 4, equal_ir
    assert result["panic_ir_changed"] and result["panic_object_changed"], result
    assert result["realmode_ir_unchanged"], result


if __name__ == "__main__":
    main()
