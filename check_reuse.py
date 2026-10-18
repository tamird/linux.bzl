#!/usr/bin/env python3
"""Run native compiler-family or source-mutation reuse qualification."""

import argparse
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import TextIO
import uuid

from qualification import ir_reuse_report, reuse_report


FAMILY_TARGETS = {
    "base": "//examples:x86_64_kernel",
    "unchanged": "//examples:x86_64_kernel",
    "lz4": "//examples:x86_64_lz4_kernel",
    "debug": "//examples:x86_64_debug_kernel",
    "btf": "//examples:x86_64_btf_vmlinux",
    "changed": "//examples:x86_64_changed_kernel",
}


def records(stream: TextIO) -> Iterator[dict[str, object]]:
    """Read the decoder's concatenated JSON without retaining its input trees."""
    decoder = json.JSONDecoder()
    buffer = ""
    read_size = 65536
    while True:
        chunk = stream.read(read_size)
        buffer += chunk
        while buffer.strip():
            buffer = buffer.lstrip()
            try:
                value, end = decoder.raw_decode(buffer)
            except json.JSONDecodeError:
                if not chunk:
                    raise
                read_size = min(read_size * 2, 16 * 1024 * 1024)
                break
            if not isinstance(value, dict):
                raise ValueError("Compact log entry is not an object")
            yield value
            buffer = buffer[end:]
            read_size = 65536
        if not chunk:
            return


def project(entry: dict[str, object]) -> dict[str, object] | None:
    """Keep the original reporter projection, including native cache digests."""
    match entry:
        case {"invocation": {"id": str(invocation), "hashFunctionName": str(algorithm)}}:
            return {"invocation": invocation, "hashFunctionName": algorithm}
        case {"spawn": dict(spawn)}:
            digest = spawn.get("digest", {})
            if not isinstance(digest, dict):
                raise ValueError(f"Invalid compact action digest: {digest}")
            return {"spawn": {
                "outputs": spawn.get("outputs", []),
                "mnemonic": spawn["mnemonic"],
                "runner": spawn.get("runner", ""),
                "cacheHit": spawn.get("cacheHit", False),
                "exitCode": spawn.get("exitCode", 0),
                "status": spawn.get("status", ""),
                "digest": {"hash": digest.get("hash", ""), "sizeBytes": int(digest.get("sizeBytes", 0))},
            }}
    for kind in ("file", "directory", "unresolvedSymlink"):
        match entry.get(kind):
            case {"path": str(path)}:
                return {"id": int(entry["id"]), "path": path}
    if "invocation" in entry:
        raise ValueError(f"Invalid compact invocation: {entry}")
    return None


def decode(decoder: str, log: Path, output: Path) -> None:
    command = [decoder, "print", "--compact_execution_log=" + str(log), "--raw=true", "--sort=false"]
    with output.open("w") as stream, subprocess.Popen(command, stdout=subprocess.PIPE, text=True) as process:
        assert process.stdout is not None
        try:
            for entry in records(process.stdout):
                if (record := project(entry)) is not None:
                    stream.write(json.dumps(record) + "\n")
        except BaseException:
            process.terminate()
            raise
        if process.wait():
            raise RuntimeError(f"Compact execution log decoder failed: {log}")


@contextmanager
def append_edit(path: Path, original: bytes, suffix: bytes) -> Iterator[None]:
    if path.read_bytes() != original:
        raise RuntimeError(f"Source changed concurrently before the reuse edit: {path}")
    path.write_bytes(original + suffix)
    try:
        yield
    finally:
        # Remove only this trial's unique addition, preserving concurrent edits.
        current = path.read_bytes()
        if suffix in current:
            path.write_bytes(current.replace(suffix, b"", 1))


def measure(decoder: str, target: str, options: list[str], directory: Path, name: str, workspace: Path, *, family: bool = False) -> None:
    options = [*options, "--//:validate_native_objects"]
    log = directory / f"{name}.execution.pb.zst"
    subprocess.run([
        "bazel", "build", target, *options,
        "--output_groups=+boot,+qualification_manifest",
        "--sandbox_block_path=/bin", "--sandbox_block_path=/usr/bin", "--sandbox_block_path=/usr/local/bin",
        "--execution_log_compact_file=" + str(log),
        "--profile=" + str(directory / f"{name}.profile.gz"),
    ], cwd=workspace, check=True)
    decode(decoder, log, directory / f"{name}.execution.jsonl")
    with (directory / f"{name}.aquery.json").open("w") as output:
        subprocess.run([
            "bazel", "aquery", f"deps({target})", *options,
            "--noinclude_pruned_inputs", "--output=jsonproto",
        ], cwd=workspace, stdout=output, check=True)
    with (directory / f"{name}.manifest-path").open("w") as output:
        subprocess.run([
            "bazel", "cquery", target, *options,
            "--output=files", "--output_groups=qualification_manifest",
        ], cwd=workspace, stdout=output, check=True)
    execution_root = subprocess.check_output(["bazel", "info", "execution_root"], cwd=workspace, text=True).strip()
    snapshot = ir_reuse_report.snapshot if family else reuse_report.snapshot
    snapshot(directory, name, Path(execution_root))


def family(decoder: str, options: list[str], directory: Path, workspace: Path) -> None:
    for name in ir_reuse_report.FAMILY_PHASES:
        measure(decoder, FAMILY_TARGETS[name], options, directory, name, workspace, family=True)
    # Each comparator must run even when the other rejects the evidence.
    comparison = subprocess.run([
        sys.executable, "-m", "qualification.ir_compare", "--family-results", str(directory),
    ], cwd=workspace)
    reuse = subprocess.run([
        sys.executable, "-m", "qualification.ir_reuse_report", "report", str(directory), "--family",
    ], cwd=workspace)
    if comparison.returncode or reuse.returncode:
        raise SystemExit(f"Compiler-family qualification failed: parity={comparison.returncode}, reuse={reuse.returncode}; see {directory}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--decoder", required=True)
    parser.add_argument("--family", action="store_true", help="Qualify base/unchanged/LZ4/debug/BTF/changed configurations")
    parser.add_argument("--source", type=Path, help="Editable native init/main.c, required for mutation qualification")
    parser.add_argument("--target", help="Mutation target (default: //examples:x86_64_kernel)")
    parser.add_argument("--out", type=Path, help="Empty evidence directory (default: validation/family or validation/reuse)")
    parser.add_argument("--bazel-option", action="append", default=[])
    args = parser.parse_args()
    workspace = Path(os.environ.get("BUILD_WORKSPACE_DIRECTORY", os.getcwd()))
    if args.family:
        if args.source is not None or args.target is not None:
            parser.error("--source and --target apply only to mutation qualification")
    elif args.source is None:
        parser.error("mutation qualification requires --source")
    directory = (workspace / (args.out or Path("validation/family" if args.family else "validation/reuse"))).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if any(directory.iterdir()):
        parser.error("--out already contains evidence; choose a separate empty directory")
    if args.family:
        family(args.decoder, args.bazel_option, directory, workspace)
        return
    source = (workspace / args.source).resolve()
    if source.parts[-2:] != ("init", "main.c"):
        parser.error("--source must identify native init/main.c for the fixed mutation controls")
    root = source.parent.parent
    paths = [source, root / "init/do_mounts.h", root / "usr/default_cpio_list", root / "scripts/Makefile.lib"]
    originals = [path.read_bytes() for path in paths]
    for path, content in zip(paths, originals, strict=True):
        (directory / (path.name + ".original")).write_bytes(content)
    nonce = uuid.uuid4()
    octals = "".join(f"\\{byte:03o}" for byte in nonce.bytes[:4])
    edits = [
        f"\n/* kernel source reuse {nonce} */\n".encode(),
        f"\n/* kernel header reuse {nonce} */\n".encode(),
        f"\ndir /linux-bzl-incbin-{nonce} 0755 0 0\n".encode(),
        f'\n# kernel gzip reuse {nonce}\ncmd_gzip += ; printf "{octals}" | dd of=$@ bs=1 seek=4 conv=notrunc status=none\n'.encode(),
    ]
    # Each later mutation is measured on top of the preceding one, exactly as
    # in the original trial. Restore only our additions on every normal exit.
    with ExitStack() as stack:
        for index, name in enumerate(reuse_report.PHASES):
            if index >= 2:
                edit = index - 2
                stack.enter_context(append_edit(paths[edit], originals[edit], edits[edit]))
            measure(args.decoder, args.target or "//examples:x86_64_kernel", args.bazel_option, directory, name, workspace)
    result = reuse_report.report(directory)
    (directory / "reuse-report.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result["qualification"], indent=2))
    if not all(result["qualification"][key] for key in (
        "all_unrelated_compiles_reused", "header_edit_recompiled_only_consumers",
        "incbin_edit_recompiled_only_consumer", "gzip_edit_recompiled_only_consumer",
    )):
        raise SystemExit("Kernel dependency reuse qualification failed; see reuse-report.json")


if __name__ == "__main__":
    main()
