"""Run pristine Linux conf with the prototype's selected compiler and requests."""

import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile


def string_map(value: object, name: str) -> dict[str, str]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise ValueError(f"Expected string mapping for {name}: {value!r}")
    return value


def object_file(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"Expected JSON object in {path}")
    return value


def string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Expected string for {name}: {value!r}")
    return value


def main() -> None:
    spec = object_file(Path(sys.argv[1]))
    output = Path(sys.argv[2]).absolute()
    output.mkdir(parents=True, exist_ok=True)
    execroot = Path.cwd()

    def path(name: str) -> Path:
        return execroot / string(spec[name], name)

    setup = object_file(path("setup"))
    report = object_file(path("report"))
    if report.get("allnoconfig") is not True:
        raise ValueError("Native configuration comparison requires allnoconfig=True")
    requested = string_map(report["requested"], "requested")
    compiler_inputs = report["compiler_inputs"]
    if not isinstance(compiler_inputs, dict):
        raise ValueError("Expected compiler_inputs mapping")
    match compiler_inputs.get("__KCONFIG_CC_VERSION_TEXT"):
        case [str() as compiler_text, "string"]:
            pass
        case other:
            raise ValueError(f"Expected compiler version text: {other!r}")
    flags = setup["compile_flags"]
    if not isinstance(flags, list) or not all(isinstance(flag, str) for flag in flags):
        raise ValueError("Expected compiler flag list")
    tools = {name: str(execroot / value) for name, value in string_map(setup["tools"], "tools").items()}
    if spec.get("pahole") is not None:
        tools["PAHOLE"] = str(path("pahole"))
    seed = output / "requested.config"
    seed.write_text("".join(
        f"# CONFIG_{name} is not set\n" if value == "n" else f"CONFIG_{name}={value}\n"
        for name, value in requested.items()
    ))
    shutil.copyfile(path("prototype_config"), output / "prototype.config")
    shutil.copyfile(path("report"), output / "prototype-report.json")
    shutil.copyfile(path("setup"), output / "prototype-setup.json")

    with tempfile.TemporaryDirectory(prefix="native-", dir=output) as temporary:
        work = Path(temporary)
        # Toolchain flags and script shebangs can contain execroot-relative
        # paths. Preserve those namespaces while conf writes into its own cwd.
        for name in ("external", "bazel-out"):
            if (execroot / name).exists():
                (work / name).symlink_to(execroot / name, target_is_directory=True)
        compiler = work / "cc"
        compiler.write_text(
            f"#!{path('shell')}\nexec {shlex.join([tools['CC'], *flags])} \"$@\"\n"
        )
        compiler.chmod(0o755)
        environment = string_map(spec["environment"], "environment")
        environment.update(tools)
        environment.update({
            "PATH": str(path("execution_bin")),
            "KBUILD_EXEC_ROOT": str(execroot),
            "CONFIG_SHELL": str(path("shell")),
            "LC_ALL": "C",
            "LANG": "C",
            "TZ": "UTC",
            "CC": str(compiler),
            "CC_VERSION_TEXT": compiler_text,
            "srctree": str(path("source")),
            "KCONFIG_ALLCONFIG": str(seed),
            "KCONFIG_CONFIG": str(work / ".config"),
            "KCONFIG_AUTOCONFIG": str(work / "include/config/auto.conf"),
            "KCONFIG_AUTOHEADER": str(work / "include/generated/autoconf.h"),
        })
        # These optional tools are not supplied by the selected prototype
        # toolchain. Use unavailable paths so existence checks see their absence.
        for name in ("PAHOLE", "RUSTC", "RUSTDOC", "BINDGEN"):
            if name not in environment:
                environment[name] = str(work / name.lower())
        # Make exports this before invoking conf. Its $(shell) keeps stdout
        # even when the command fails. Native Kconfig owns its pahole query.
        for name, command in {
            "RUSTC_VERSION_TEXT": [str(path("shell")), "-c", '"$RUSTC" --version 2>/dev/null'],
        }.items():
            version = subprocess.run(command, cwd=work, env=environment, capture_output=True, text=True, check=False)
            environment[name] = version.stdout.strip()
        (output / "native-environment.json").write_text(json.dumps({
            "environment": environment,
            "compiler_command": [tools["CC"], *flags],
            "native_shell": "/bin/sh (libc popen)",
            "compiler_identity": "CC_VERSION_TEXT matches the prototype's synthetic label; native cc-version/as-version/ld-version still probe the real executables",
        }, indent=2) + "\n")
        versions = [("compiler", str(compiler)), ("linker", tools["LD"])]
        if "PAHOLE" in tools:
            versions.append(("pahole", tools["PAHOLE"]))
        for name, executable in versions:
            version = subprocess.run(
                [executable, "--version"],
                cwd=work,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            (output / f"{name}-version.txt").write_text(version.stdout + version.stderr)
            if version.returncode:
                sys.stdout.write(version.stdout)
                sys.stderr.write(version.stderr)
                raise SystemExit(version.returncode)
        process = subprocess.run(
            [str(path("conf")), "--allnoconfig", str(path("source") / "Kconfig")],
            cwd=work,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        (output / "native.stdout").write_text(process.stdout)
        (output / "native.stderr").write_text(process.stderr)
        if process.returncode:
            sys.stdout.write(process.stdout)
            sys.stderr.write(process.stderr)
            raise SystemExit(process.returncode)
        shutil.copyfile(work / ".config", output / "native.config")
        if (header := work / "include/generated/autoconf.h").exists():
            shutil.copyfile(header, output / "native.autoconf.h")


if __name__ == "__main__":
    main()
