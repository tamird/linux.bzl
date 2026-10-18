"""Check the native syntax exporter against every Kconfig file of its source."""

import argparse
import json
import os
from pathlib import Path

from kconfig_syntax import auto_conf_quotes_strings, export_syntax


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tool", required=True, type=Path)
    parser.add_argument("--linux", required=True, type=Path)
    args = parser.parse_args()
    # Resolve the runfile executable, but keep the source's declared runfile
    # namespace: the tests intentionally contain only the Kconfig inputs.
    linux = args.linux.absolute().parent
    print(f'Native auto.conf quotes strings: {auto_conf_quotes_strings(executable=args.tool.resolve())}')
    syntax = export_syntax(linux, Path(os.environ["TEST_TMPDIR"]), executable=args.tool.resolve())
    commands = json.loads((Path(os.environ["TEST_TMPDIR"]) / "capability-commands.json").read_text())
    arch_config = (linux / "arch/Kconfig").read_text()
    for tool in ("NM", "AR"):
        query = f"$({tool}) --help | head -n 1 | grep -qi llvm"
        if query in arch_config:
            expected = f"__selected_{tool.lower()}__ --help | head -n 1 | grep -qi llvm"
            assert commands[f"__KCONFIG_{tool}_LLVM_HELP"] == [expected], (tool, commands)
    if "$(as-instr64,wrussq" in (linux / "arch/x86/Kconfig.assembler").read_text():
        assert all("__selected_m64_flag__" in command for command in commands["__KCONFIG_AS_WRUSSQ"]), commands
    for path, symbol in (
        ("init/Kconfig", "CC_VERSION_TEXT"),
        ("arch/x86/Kconfig", "X86_64"),
        ("arch/arm64/Kconfig", "ARM64"),
    ):
        assert any(entry["symbol"] == symbol for entry in syntax[path]), (path, symbol, syntax[path])
    for path, entries in syntax.items():
        for entry in entries:
            assert entry["source"] == path, (path, entry)
            assert isinstance(entry["transitional"], bool), (path, entry)
    inputs = {
        "CC_IS_GCC": {"__KCONFIG_CC_IS_GCC"},
        "CC_IS_CLANG": {"__KCONFIG_CC_IS_CLANG"},
        "AS_IS_GNU": {"__KCONFIG_AS_IS_GNU"},
        "AS_IS_LLVM": {"__KCONFIG_AS_IS_LLVM"},
        "LD_IS_LLD": {"__KCONFIG_LD_IS_LLD"},
        "GCC_VERSION": {"__KCONFIG_CC_VERSION", "__KCONFIG_GCC_VERSION"},
        "CLANG_VERSION": {"__KCONFIG_CC_VERSION", "__KCONFIG_CLANG_VERSION"},
        "LD_VERSION": {"__KCONFIG_LD_VERSION", "__KCONFIG_LD_LEGACY_VERSION"},
        "LLD_VERSION": {"__KCONFIG_LD_VERSION", "__KCONFIG_LLD_VERSION"},
    }
    symbols = {entry["symbol"]: entry for entry in syntax["init/Kconfig"]}
    for symbol, expected in inputs.items():
        defaults = [prop["value"] for prop in symbols[symbol]["properties"] if prop["kind"] == "default"]
        actual = {token[1] for code in defaults for token in code if token[0] in ("sym", "lit")}
        assert actual & expected, (symbol, defaults, expected)
    assert any(entry["kind"] == "source" for entries in syntax.values() for entry in entries), "No source entries exported"
    assert any(entry["kind"] == "choice" for entries in syntax.values() for entry in entries), "No choice entries exported"
    print(f"Exported {len(syntax)} Kconfig files with native source locations, choices and source entries")


if __name__ == "__main__":
    main()
