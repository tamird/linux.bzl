"""Decode the existing filename transport across chunk boundaries."""

load("@bazel_skylib//lib:unittest.bzl", "asserts", "unittest")
load("//rules:runtime.bzl", "read_payload")

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
    return unittest.end(env)

payload_test = unittest.make(_payload_test)
