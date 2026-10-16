"""Require complete native/prototype config equality; retain every difference."""

import argparse
import json
import os
from pathlib import Path
import shutil


def config_values(path: Path) -> dict[str, str]:
    values = {}
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if line.startswith("CONFIG_") and "=" in line:
            name, value = line.removeprefix("CONFIG_").split("=", 1)
        elif line.startswith("# CONFIG_") and line.endswith(" is not set"):
            name, value = line.removeprefix("# CONFIG_").removesuffix(" is not set"), "n"
        elif not line or line.startswith("#"):
            continue
        else:
            raise ValueError(f"Unrecognized config line at {path}:{number}: {line!r}")
        if name in values:
            raise ValueError(f"Duplicate config symbol at {path}:{number}: {name}")
        values[name] = value
    return values


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference", type=Path)
    args = parser.parse_args()
    native = config_values(args.reference / "native.config")
    prototype = config_values(args.reference / "prototype.config")
    differences = {
        name: {"native": native.get(name), "prototype": prototype.get(name)}
        for name in sorted(native.keys() | prototype.keys())
        if native.get(name) != prototype.get(name)
    }
    report = {
        "case": args.reference.name,
        "native_symbols": len(native),
        "prototype_symbols": len(prototype),
        "differences": differences,
        "presence_differences": [name for name in differences if name not in native or name not in prototype],
        "value_differences": [name for name in differences if name in native and name in prototype],
        "comparison": "All written symbols and values, including explicit n and absent entries",
        "interpretation": "Differences may reflect compiler-probe policy or Kconfig semantics; this check does not ignore either",
    }
    evidence = Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"]) / args.reference.name
    shutil.copytree(args.reference, evidence)
    (evidence / "parity.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    if differences:
        raise SystemExit(f"Native/prototype configuration mismatch: {len(differences)} symbols")


if __name__ == "__main__":
    main()
