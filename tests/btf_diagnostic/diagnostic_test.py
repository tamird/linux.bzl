import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile


def main() -> None:
    match sys.argv[1:]:
        case [compiler_arg, bitcode_arg, report_arg, source_arg, regression_arg, expected_host]:
            compiler, bitcode, report, source, regression = map(
                Path, (compiler_arg, bitcode_arg, report_arg, source_arg, regression_arg),
            )
        case arguments:
            raise ValueError(f"Expected five input paths and a native host, got {arguments!r}")
    native_host = {"Linux": "linux", "Darwin": "macos"}[platform.system()] + "_" + platform.machine().replace("arm64", "aarch64")
    assert native_host == expected_host, (native_host, expected_host)
    result = {
        "inputs": {
            str(path): {"bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for path in (compiler, bitcode, report, source, regression)
        },
        "host": native_host,
        "outcomes": [],
    }
    destination = Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"])

    def run(name: str, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        outcome = subprocess.run([str(compiler), *arguments], capture_output=True, text=True, check=False)
        result["outcomes"].append({
            "name": name,
            "argv": outcome.args,
            "returncode": outcome.returncode,
            "stdout": outcome.stdout,
            "stderr": outcome.stderr,
        })
        print(name, "returncode", outcome.returncode, flush=True)
        (destination / "outcomes.json").write_text(json.dumps(result, indent=2) + "\n")
        return outcome

    version = run("version", ["--version"])
    assert version.returncode == 0, version
    assert "clang version 23.1.2" in version.stdout, version
    resources = run("resource-directory", ["-print-resource-dir"])
    assert resources.returncode == 0, resources
    resource_directory = Path(resources.stdout.strip()).resolve()
    expected_directory = compiler.resolve().parent.parent / "lib" / "clang" / "23"
    assert resource_directory == expected_directory, (resource_directory, expected_directory)
    for header in ("stddef.h", "stdarg.h"):
        assert (resource_directory / "include" / header).is_file(), header
    original = json.loads(report.read_text())[0]["outcomes"][2]
    arguments = original["argv"][1:]
    arguments[arguments.index("-c") + 1] = str(bitcode)
    captured = run("captured-chain", arguments)
    control = run("captured-without-chain", [arg for arg in arguments if arg != "-fdiagnostics-show-inlining-chain"])
    flags = ["-target", "x86_64-linux-gnu", "-O2", "-g", "-gdwarf-5", "-fdiagnostics-show-inlining-chain"]
    with tempfile.TemporaryDirectory() as temporary:
        reduced = str(Path(temporary) / "minimal.bc")
        integrated = run("minimal-integrated", [*flags, "-c", str(source), "-o", "/dev/null"])
        frontend = run("minimal-frontend", [*flags, "-emit-llvm", "-Xclang", "-disable-llvm-passes", "-c", str(source), "-o", reduced])
        backend = run("minimal-backend", [*flags, "-c", reduced, "-o", "/dev/null"])
        disassembled = run("captured-ir", ["-target", "x86_64-linux-gnu", "-S", "-emit-llvm", "-Xclang", "-disable-llvm-passes", "-x", "ir", str(bitcode), "-o", str(destination / "attribute.ll")])
    chain = run("ir-inlining-chain", ["-cc1", "-triple", "x86_64-unknown-linux-gnu", "-x", "ir", "-emit-obj", "-O2", "-fdiagnostics-show-inlining-chain", str(regression), "-o", "/dev/null"])
    assert chain.returncode == 1, chain
    assert "dontcall IR error" in chain.stderr, chain
    assert "called by function 'inner'" in chain.stderr, chain
    assert "inlined by function 'outer'" in chain.stderr, chain
    assert "Stack dump:" not in chain.stderr, chain
    assert captured.returncode == 1, captured
    assert control.returncode == 1, control
    assert integrated.returncode == 1, integrated
    assert frontend.returncode == 0, frontend
    assert backend.returncode == 1, backend
    assert disassembled.returncode == 0, disassembled
    for outcome in (captured, control, integrated, backend):
        assert "linux-bzl-ir-attribute-error" in outcome.stderr, outcome
        assert "Stack dump:" not in outcome.stderr, outcome
        assert "PLEASE submit a bug report" not in outcome.stderr, outcome
    for outcome in (captured, integrated, backend):
        assert "called by function 'linux_bzl_ir_diagnostic'" in outcome.stderr, outcome


if __name__ == "__main__":
    main()
