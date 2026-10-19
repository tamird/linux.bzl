"""Decode the existing filename transport across chunk boundaries."""

load("@bazel_skylib//lib:unittest.bzl", "asserts", "unittest")
load("//rules:runtime.bzl", "read_payload")
load("//rules:schedule.bzl", "decode_schedule", "encode_schedule", "write_payload", "write_schedule")

def _payload_test(ctx):
    env = unittest.begin(ctx)

    # {"value":" followed by 79 'a' bytes places the Unicode escape's
    # backslash at byte 90, the end of the first publisher chunk.
    encoded = "7b2276616c7565223a22" + "61" * 79 + "5c7530336262" + "61" * 10000 + "227d"
    children = []
    for start in range(0, len(encoded), 180):
        number = ("00000000" + str(start // 180))[-8:]
        children.append(struct(basename = number + "=" + encoded[start:start + 180]))
    asserts.true(env, len(children) > 100)
    asserts.true(env, children[0].basename.endswith("5c"))
    asserts.equals(env, {"value": "a" * 79 + "λ" + "a" * 10000}, read_payload(struct(children = children[::-1] + [struct(basename = "report.json")])))
    asserts.equals(env, [], read_payload(struct(children = [struct(basename = "00000000=5b5d")])))

    # Inspect the production writer's declared filenames and report. A JSON
    # newline escape crosses the same boundary as the decoder case above.
    files, runs = [], []

    def declare_file(name, directory):
        file = struct(path = directory.path + "/" + name, basename = name.rsplit("/", 1)[-1])
        files.append(file)
        return file

    def run(**kwargs):
        runs.append(kwargs)

    value = {"value": "a" * 79 + "\n" + "a" * 10000}
    write_payload(struct(declare_file = declare_file, run = run), "python", struct(path = "emit"), struct(path = "payload"), value, depset())
    expected = "7b2276616c7565223a22" + "61" * 79 + "5c6e" + "61" * 10000 + "227d"
    chunks = [file for file in files if "=" in file.basename]
    asserts.equals(env, expected, "".join([file.basename.split("=", 1)[1] for file in chunks]))
    asserts.equals(env, "payload/00/00/00/00000000=" + expected[:180], chunks[0].path)
    asserts.equals(env, "payload/00/00/01/00000100=" + expected[18000:18180], chunks[100].path)
    asserts.equals(env, ["emit", "payload/report.json", json.encode(value)], runs[0]["arguments"])
    asserts.equals(env, chunks, runs[1]["outputs"])
    asserts.equals(env, value, read_payload(struct(children = files[::-1])))

    # Both publishers emit printable ASCII JSON, including escapes for
    # control characters. Hexadecimal letter case does not change a byte.
    files.clear()
    runs.clear()
    value = [" !\"#$%&'()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_`abcdefghijklmnopqrstuvwxyz{|}~", "\n\r\t", [], {}]
    write_payload(struct(declare_file = declare_file, run = run), "python", struct(path = "emit"), struct(path = "payload"), value, depset())
    asserts.equals(env, value, read_payload(struct(children = files[::-1])))
    asserts.equals(env, value, read_payload(struct(children = [struct(basename = file.basename.upper()) for file in files[::-1]])))

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
    write_schedule(struct(declare_file = declare_file, run = run), "python", struct(path = "emit"), struct(path = "payload"), jobs, depset())
    asserts.equals(env, ["emit", "payload/report.json", json.encode(jobs)], runs[0]["arguments"])
    asserts.equals(env, compact, read_payload(struct(children = files[::-1])))
    asserts.equals(env, jobs, decode_schedule(read_payload(struct(children = files))))
    return unittest.end(env)

payload_test = unittest.make(_payload_test)
