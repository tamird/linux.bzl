"""Decode the existing filename transport across chunk boundaries."""

load("@bazel_skylib//lib:unittest.bzl", "asserts", "unittest")
load("//execution_tools:defs.bzl", "EXECUTION_TOOLS_TYPE")
load("//rules:runtime.bzl", "read_payload", "touch_files", "writer")
load("//rules:schedule.bzl", "decode_schedule", "encode_schedule", "write_payload", "write_schedule")

def _payload_test(ctx):
    env = unittest.begin(ctx)

    # The escaped backslash begins at the last character of the first chunk.
    # The reader must join chunks before interpreting either filename or JSON
    # escapes, and sort by the index rather than the directory traversal order.
    encoded = '{"value":"' + "a" * 169 + "%5Cu03bb" + "a" * 20000 + '"}'
    children = []
    for start in range(0, len(encoded), 180):
        number = ("00000000" + str(start // 180))[-8:]
        children.append(struct(basename = number + "=" + encoded[start:start + 180]))
    asserts.true(env, len(children) > 100)
    asserts.true(env, children[0].basename.endswith("%"))
    asserts.equals(env, {"value": "a" * 169 + "λ" + "a" * 20000}, read_payload(struct(children = children[::-1] + [struct(basename = "report.json")])))
    asserts.equals(env, [], read_payload(struct(children = [struct(basename = "00000000=[]")])))

    # Inspect the production writer's filenames and report, including a split
    # slash escape and literal text that must not be recursively unescaped.
    files, runs = [], []
    tools = dict(writer_shell = "sh", writer_mkdir = struct(path = "mkdir"), writer_touch = struct(path = "touch"), writer_files = depset(), python = "python", python_files = depset(), emit = struct(path = "emit"))

    def declare_file(name, directory):
        file = struct(path = directory.path + "/" + name, basename = name.rsplit("/", 1)[-1])
        files.append(file)
        return file

    def run(**kwargs):
        runs.append(kwargs)

    value = {"value": "a" * 169 + "/\\%2F%5C%25\n" + "a" * 20000}
    write_payload(struct(declare_file = declare_file, run = run), tools, struct(path = "payload"), value)
    expected = '{"value":"' + "a" * 169 + "%2F%5C%5C%252F%255C%2525%5Cn" + "a" * 20000 + '"}'
    chunks = [file for file in files if "=" in file.basename]
    asserts.equals(env, expected, "".join([file.basename.split("=", 1)[1] for file in chunks]))
    asserts.equals(env, "payload/00/00/00/00000000=" + expected[:180], chunks[0].path)
    asserts.equals(env, "payload/00/00/01/00000100=" + expected[18000:18180], chunks[100].path)
    asserts.equals(env, ["payload/report.json", json.encode(value)], runs[0]["arguments"][-2:])
    asserts.equals(env, chunks, runs[1]["outputs"])
    asserts.equals(env, value, read_payload(struct(children = files[::-1])))

    # Printable ASCII is retained verbatim except the three reserved bytes;
    # JSON still owns escaping of control characters.
    files.clear()
    runs.clear()
    value = [" !\"#$%&'()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_`abcdefghijklmnopqrstuvwxyz{|}~", "\n\r\t", [], {}]
    write_payload(struct(declare_file = declare_file, run = run), tools, struct(path = "payload"), value)
    asserts.equals(env, value, read_payload(struct(children = files[::-1])))

    # Unequal input lists share strings without losing order or duplicates.
    jobs = [
        dict(inputs = ["source.c", "generated.h", "source.c"], outputs = ["object.o"], scan = {"generated": {"header.h": "generated.h"}}, compiler = {"phase": "single"}),
        dict(inputs = ["generated.h", "object.o"], outputs = ["archive.a"], scan = None, compiler = {}),
        dict(inputs = [], outputs = ["constant"], scan = None, compiler = {}),
    ]
    compact = encode_schedule(jobs)
    asserts.equals(env, ["source.c", "generated.h", "object.o"], compact["paths"])
    asserts.equals(env, [[0, 1, 0], [1, 2], []], [job["inputs"] for job in compact["jobs"]])
    asserts.equals(env, jobs, decode_schedule(compact))
    files.clear()
    runs.clear()
    write_schedule(struct(declare_file = declare_file, run = run), tools, struct(path = "payload"), jobs)
    asserts.equals(env, ["payload/report.json", json.encode(jobs)], runs[0]["arguments"][-2:])
    asserts.equals(env, compact, read_payload(struct(children = files[::-1])))
    asserts.equals(env, jobs, decode_schedule(read_payload(struct(children = files))))

    # The 500-output batch limit belongs to the real payload publisher.
    files.clear()
    runs.clear()
    write_payload(struct(declare_file = declare_file, run = run), tools, struct(path = "payload"), "a" * 100000)
    batches = [call["outputs"] for call in runs if call.get("progress_message")]
    asserts.equals(env, [500, 56], [len(batch) for batch in batches])
    return unittest.end(env)

def _writer_checks(ctx):
    execution = ctx.toolchains[EXECUTION_TOOLS_TYPE].execution
    tools = dict(execution.writer_tools, python = execution.python, python_files = execution.python_files, emit = ctx.file._emit)
    root = ctx.label.name + ".writer"

    # Adapt only the ordinary analysis declaration API; actions execute the
    # same writer commands used for dynamic template children.
    def declare_file(name, directory):
        return ctx.actions.declare_file(directory + "/" + name)

    write = writer(struct(run = ctx.actions.run, declare_file = declare_file), tools, root)
    special = "λ☃ %s %n \\ \\n '$HOME' $(false) `false`; * ? [x] & | < > \n\r\t"
    cases = ["", special, "a" * 59999, "b" * 60000, "c" * 15999 + special + "d" * 240001, "e" * 239999 + "\r\nlast", "u" * 239998 + "💡λtail"]
    inputs, checks = [], []
    for index, content in enumerate(cases):
        output = ctx.actions.declare_file(root + "/nested ' files/%d.actual" % index)
        expected = ctx.actions.declare_file(root + "/%d.expected" % index)
        write(output, content)
        normalized = content
        if len(content) >= 60000:
            normalized = "".join([content[start:start + 240000].replace("\r\n", "\n").replace("\r", "\n") for start in range(0, len(content), 240000)])
        ctx.actions.write(expected, normalized)
        inputs.extend([output, expected])
        checks.append([output.path, expected.path])

    # Execute the production touch argv in a private directory, including an
    # existing nonempty file. No declared input is modified by this check.
    calls = []

    def record(**kwargs):
        calls.append([kwargs["executable"].path] + kwargs["arguments"])

    touched = [struct(path = "nested ' %s/$literal-%d" % ("%", index)) for index in range(500)] + [struct(path = "flat-$literal")]
    for start in range(0, len(touched), 500):
        touch_files(struct(run = record), tools, touched[start:start + 500])
    spec = ctx.actions.declare_file(root + "/checks.json")
    ctx.actions.write(spec, json.encode(dict(checks = checks, commands = calls, paths = [file.path for file in touched], tools = [tools[name].path for name in ["writer_shell", "writer_mkdir", "writer_touch"]])))
    report = ctx.actions.declare_file(root + "/passed")
    ctx.actions.run(
        executable = execution.python,
        arguments = ["-B", "-c", '''import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

spec = json.loads(Path(sys.argv[1]).read_text())
for actual, expected in spec["checks"]:
    assert Path(actual).read_bytes() == Path(expected).read_bytes(), actual
tools = {path: os.path.abspath(path) for path in spec["tools"]}
with tempfile.TemporaryDirectory() as temporary:
    first = Path(temporary, spec["paths"][0])
    first.parent.mkdir(parents=True)
    first.write_bytes(b"preserved content")
    for command in spec["commands"]:
        subprocess.run([tools.get(arg, arg) for arg in command], cwd=temporary, check=True)
    assert first.read_bytes() == b"preserved content"
    for path in spec["paths"][1:]:
        assert Path(temporary, path).read_bytes() == b"", path
Path(sys.argv[2]).write_text("passed\\n")
''', spec.path, report.path],
        inputs = depset(inputs + [spec], transitive = [execution.python_files, tools["writer_files"]]),
        tools = [tool for name, tool in tools.items() if name.startswith("writer_tool_")],
        outputs = [report],
        mnemonic = "KernelWriterCheck",
    )
    return [DefaultInfo(files = depset([report]))]

payload_test = unittest.make(_payload_test)

writer_check = rule(implementation = _writer_checks, attrs = {"_emit": attr.label(default = "//:emit.py", allow_single_file = True)}, toolchains = [EXECUTION_TOOLS_TYPE])
