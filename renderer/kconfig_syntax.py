#!/usr/bin/env python3
"""Export file-local Linux Kconfig syntax using the native grammar and lexer."""
import json
import os
from pathlib import Path
import subprocess
import sys

from compiler_capabilities import native_capabilities

HERE = Path(__file__).resolve().parent


def auto_conf_quotes_strings(*, executable: Path | None = None) -> bool:
    """Read the string representation from the source's compiled serializer."""
    executable = Path(sys.executable) if executable is None else executable
    values = ['', 'clang (native) "quoted" \\ path']
    outputs = [subprocess.run(
        [executable, '_kconfig', '--auto-conf-string', value],
        env={'LC_ALL': 'C', 'PATH': ''}, check=True, capture_output=True, text=True,
    ).stdout for value in values]
    if outputs == [f'CONFIG_VALUE={value}\n' for value in values]:
        return False
    if outputs == [f'CONFIG_VALUE={json.dumps(value)}\n' for value in values]:
        return True
    raise ValueError(f'Unsupported native auto.conf string serialization: {outputs!r}')


def export_syntax(linux, output, *, executable: Path | None = None):
    output.mkdir(parents=True, exist_ok=True)
    executable = Path(sys.executable) if executable is None else executable
    env = {'LC_ALL': 'C', 'PATH': ''}
    env.update(srctree=str(linux), SRCARCH='__KCONFIG_SRCARCH', ARCH='__KCONFIG_ARCH',
               SUBARCH='__KCONFIG_SUBARCH', HEADER_ARCH='__KCONFIG_HEADER_ARCH',
               KERNELVERSION='prototype', CC='__selected_cc__', LD='__selected_ld__',
               CC_VERSION_TEXT='__KCONFIG_CC_VERSION_TEXT', PAHOLE='__selected_pahole__',
               PAHOLE_VERSION='__KCONFIG_PAHOLE_VERSION')
    files, warnings = {}, set()
    for path in sorted(linux.rglob('Kconfig*')):
        relative = path.relative_to(linux).as_posix()
        if not path.is_file() or (relative.startswith('Documentation/') and relative != 'Documentation/Kconfig') or relative.startswith(('scripts/kconfig/', 'tools/')) or '.git' in path.parts:
            continue
        result = subprocess.run([executable, "_kconfig", relative], cwd=linux, env=env, capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(f'Native Kconfig syntax export failed for {relative}:\n{result.stderr}')
        warnings.update(line for line in result.stderr.splitlines() if line)
        files[relative] = json.loads(result.stdout)
    for warning in sorted(warnings):
        print(warning, file=sys.stderr)
    return files
