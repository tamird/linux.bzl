"""Verify declared facts with the source's native commands and selected tools."""

import json
import os
import platform
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
from typing import TypedDict

from prepare_source import script_interpreter

class Execution(TypedDict):
    os: str
    cpu: str


class NativeCheck(TypedDict):
    input: str
    commands: list[str]
    execution: Execution | None


class ObjectCheck(TypedDict):
    object: str
    required: list[str]
    excluded: list[str]


class Verification(TypedDict):
    toolchain: str
    target: str
    tools: dict[str, str]
    compile_flags: list[str]
    clang_flags: str
    shell: str
    execution_bin: str
    scripts: dict[str, str]
    checks: list[NativeCheck]
    objects: list[ObjectCheck]


def main() -> None:
    # Written by compiler_capabilities_check.bzl from declared providers.
    spec: Verification = json.loads(Path(sys.argv[1]).read_text())
    stamp, report = (Path(path).absolute() for path in sys.argv[2:])
    execroot = Path.cwd()
    shell = str(execroot / spec['shell'])
    tools = {name: str(execroot / path) for name, path in spec['tools'].items()}
    results = []
    with tempfile.TemporaryDirectory(prefix='capabilities-', dir=stamp.parent) as temporary:
        work = Path(temporary).absolute()
        for name in ('external', 'bazel-out'):
            if (execroot / name).exists():
                (work / name).symlink_to(execroot / name, target_is_directory=True)
        aliases = work / 'tools'
        aliases.mkdir()
        for name, tool in tools.items():
            alias = aliases / ('__selected_' + name.lower() + '__')
            flags = spec['compile_flags'] if name == 'CC' else []
            alias.write_text(f'#!{shell}\nexec {shlex.join([tool, *flags])} "$@"\n')
            alias.chmod(0o755)
        for logical, source in spec['scripts'].items():
            original = execroot / source
            interpreter, arguments = script_interpreter(original.read_bytes().split(b'\n', 1)[0])
            if interpreter != 'sh':
                raise ValueError(f'Unsupported capability script interpreter: {logical}: {interpreter}')
            alias = work / '__selected_srctree__' / logical
            alias.parent.mkdir(parents=True, exist_ok=True)
            alias.write_text(f'#!{shell}\nexec {shlex.join([shell, *arguments, str(original)])} "$@"\n')
            alias.chmod(0o755)
        environment = {
            'PATH': str(aliases) + os.pathsep + str(execroot / spec['execution_bin']),
            'KBUILD_EXEC_ROOT': str(execroot),
            'LC_ALL': 'C', 'LANG': 'C', 'TZ': 'UTC',
            'CLANG_FLAGS': spec['clang_flags'],
            **{name: '__selected_' + name.lower() + '__' for name in tools},
        }
        for check in spec['checks']:
            if required := check['execution']:
                observed = {'os': platform.system(), 'cpu': platform.machine()}
                if observed != required:
                    raise ValueError(f"Capability {check['input']} requires execution platform {required}, got {observed}")
            for command in check['commands']:
                # Context markers occupy native word-list positions. Tool
                # markers remain command names, also valid inside env "CC=...".
                command = command.replace('__selected_clang_flags__', '$CLANG_FLAGS').replace('__selected_m64_flag__', '-m64')
                result = subprocess.run([shell, '-c', command], cwd=work, env=environment, capture_output=True, text=True, check=False)
                results.append({'input': check['input'], 'command': command, 'status': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr})
                if result.returncode:
                    report.write_text(json.dumps({'verification': spec, 'results': results}, indent=2) + '\n')
                    raise RuntimeError(f"Declared capability {check['input']} failed: {command}\n{result.stdout}{result.stderr}")
        for check in spec['objects']:
            obj = execroot / check['object']
            if not obj.stat().st_size:
                raise ValueError(f'Empty capability object: {obj}')
            if check['required'] or check['excluded']:
                symbols = set(subprocess.run([tools['NM'], '--undefined-only', '--just-symbol-name', str(obj)], cwd=work, env=environment, capture_output=True, text=True, check=True).stdout.split())
                if missing := set(check['required']) - symbols:
                    raise ValueError(f'Missing instrumentation symbols in {obj}: {missing}')
                if unexpected := set(check['excluded']) & symbols:
                    raise ValueError(f'Unexpected instrumentation symbols in {obj}: {unexpected}')
    report.write_text(json.dumps({'verification': spec, 'results': results}, indent=2) + '\n')
    # Only this constant result enters production actions. Native commands,
    # config-specific reports and unrelated tool inputs stay in this action.
    stamp.write_text('Verified selected kernel toolchain capabilities\n')


if __name__ == '__main__':
    main()
