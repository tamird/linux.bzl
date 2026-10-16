"""Bazel action helpers for the standalone repository generator."""

import argparse
import gzip
import io
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tarfile
import tempfile
from typing import TypedDict
import zipfile


# bootstrap.bzl writes these action specifications.
class NamespaceSpec(TypedDict):
    nm: str
    objcopy: str
    namespace: str
    objects: list[tuple[str, str]]


class BundleSpec(TypedDict):
    binary: str
    output: str
    python_metadata: str
    make_module: str
    identity: str
    stdlib: list[str]
    renderer: list[str]
    license_files: list[str]


class SupportSpec(TypedDict):
    tool: str
    linux_anchor: str
    fragments: list[str]
    output: str


def namespace(spec: NamespaceSpec) -> None:
    """Rename parser-owned globals, preserving references to libc."""
    symbols = set()
    for source, _ in spec['objects']:
        result = subprocess.run(
            [spec['nm'], '--defined-only', '--extern-only', '--format=just-symbols', source],
            check=True, capture_output=True, text=True,
        )
        symbols.update(result.stdout.splitlines())
    mapping = ''.join(f'{name} {spec["namespace"]}{name}\n' for name in sorted(symbols))
    with tempfile.NamedTemporaryFile(mode='w', dir=Path(spec['objects'][0][1]).parent) as stream:
        stream.write(mapping)
        stream.flush()
        for source, output in spec['objects']:
            subprocess.run([spec['objcopy'], '--redefine-syms=' + stream.name, source, output], check=True)


def assert_static(binary: Path) -> None:
    """Check the ELF program headers directly; no ambient readelf is needed."""
    with binary.open('rb') as stream:
        header = stream.read(64)
        if header[:6] != b'\x7fELF\x02\x01':
            raise ValueError('Generator must be a 64-bit little-endian ELF')
        program_offset = struct.unpack_from('<Q', header, 32)[0]
        entry_size, count = struct.unpack_from('<HH', header, 54)
        for index in range(count):
            stream.seek(program_offset + index * entry_size)
            kind = struct.unpack('<I', stream.read(4))[0]
            if kind in (2, 3):
                raise ValueError('Generator must have neither an interpreter nor dynamic dependencies')


def zip_entry(bundle: zipfile.ZipFile, name: str, data: bytes) -> None:
    entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    entry.external_attr = 0o100644 << 16
    bundle.writestr(entry, data)


def bundle(spec: BundleSpec) -> None:
    source, output = Path(spec['binary']), Path(spec['output'])
    assert_static(source)
    shutil.copyfile(source, output)
    metadata_path = Path(spec['python_metadata'])
    metadata = json.loads(metadata_path.read_text())
    stdlib = metadata_path.parent / metadata['python_paths']['stdlib']
    make_module = Path(spec['make_module']).read_text()
    match = re.search(r'\bversion\s*=\s*"([^"]+)"', make_module)
    if match is None:
        raise ValueError('GNU Make module does not declare its version')
    versions = {'python': metadata['python_version'], 'make': match[1].split('.bcr.')[0]}
    with zipfile.ZipFile(output, 'a', compression=zipfile.ZIP_STORED) as archive:
        for path in sorted(map(Path, spec['stdlib'])):
            zip_entry(archive, path.relative_to(stdlib).as_posix(), path.read_bytes())
        for path in sorted(map(Path, spec['renderer'])):
            zip_entry(archive, 'resources/renderer/' + path.name, path.read_bytes())
        zip_entry(archive, 'resources/renderer/parser-sources.json', Path(spec['identity']).read_bytes())
        zip_entry(archive, 'resources/renderer/tool-versions.json', json.dumps(versions, sort_keys=True).encode())
        for index, path in enumerate(sorted(map(Path, spec['license_files']))):
            zip_entry(archive, f'resources/licenses/{index}-{path.name}', path.read_bytes())
    output.chmod(0o755)


def support(spec: SupportSpec) -> None:
    output = Path(spec['output']).resolve()
    tool = Path(spec['tool']).resolve()
    # Bazel may expose source files as individual symlinks inside a real directory.
    linux = Path(spec['linux_anchor']).resolve().parent
    with tempfile.TemporaryDirectory(prefix='consumer-support-', dir=output.parent) as directory:
        repository = Path(directory) / 'repo'
        command = [str(tool), 'generate', '--linux', str(linux), '--out', str(repository), '--all-backends']
        for fragment in spec['fragments']:
            command.extend(['--config-fragment', str(Path(fragment).resolve())])
        subprocess.run(command, env={'PATH': ''}, check=True)
        files = {'kernel-repository': tool}
        for name in ('MODULE.bazel', 'repository-tool.json'):
            files['support/' + name] = repository / name
        for patch in sorted((repository / 'rules').glob('*.patch')):
            files['support/rules/' + patch.name] = patch
        with output.open('wb') as raw, gzip.GzipFile(fileobj=raw, filename='', mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode='w') as archive:
                for name, path in sorted(files.items()):
                    data = path.read_bytes()
                    entry = tarfile.TarInfo(name)
                    entry.size = len(data)
                    entry.mode = 0o755 if name == 'kernel-repository' else 0o644
                    archive.addfile(entry, io.BytesIO(data))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['namespace', 'bundle', 'support'])
    parser.add_argument('specification', type=Path)
    args = parser.parse_args()
    action = {'namespace': namespace, 'bundle': bundle, 'support': support}[args.action]
    action(json.loads(args.specification.read_text()))
