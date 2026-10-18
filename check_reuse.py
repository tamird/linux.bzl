#!/usr/bin/env python3
"""Run the six native source/header/payload reuse phases and retain their evidence."""

import argparse
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
import json
import os
from pathlib import Path
import subprocess
from typing import TextIO
import uuid

from qualification import reuse_report


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


def measure(decoder: str, target: str, options: list[str], directory: Path, name: str, workspace: Path) -> None:
    log = directory / f"{name}.execution.pb.zst"
    subprocess.run([
        "bazel", "build", target, *options,
        "--output_groups=+boot,+qualification_manifest",
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
    execution_root = subprocess.check_output(["bazel", "info", "execution_root", *options], cwd=workspace, text=True).strip()
    reuse_report.snapshot(directory, name, Path(execution_root))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--decoder", required=True)
    parser.add_argument("--source", type=Path, required=True, help="Editable native init/main.c")
    parser.add_argument("--target", default="//examples:x86_64_kernel")
    parser.add_argument("--out", type=Path, default=Path("validation/reuse"))
    parser.add_argument("--bazel-option", action="append", default=[])
    args = parser.parse_args()
    workspace = Path(os.environ.get("BUILD_WORKSPACE_DIRECTORY", os.getcwd()))
    source = (workspace / args.source).resolve()
    if source.parts[-2:] != ("init", "main.c"):
        parser.error("--source must identify native init/main.c for the fixed mutation controls")
    root = source.parent.parent
    directory = (workspace / args.out).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if any((directory / f"{name}.execution.pb.zst").exists() for name in reuse_report.PHASES):
        parser.error("--out already contains a trial; choose a separate evidence directory")
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
            measure(args.decoder, args.target, args.bazel_option, directory, name, workspace)
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
