"""Check that reuse measurements distinguish execution, caches and invalid trials."""

import json
import os
from pathlib import Path
import tempfile
import unittest

from qualification import ir_reuse_report, reuse_report


_OUTPUTS = [f"bazel-out/bin/_objs/kernel.compile/{target}/source/{target}" for target in (
    "init/main.o", "kernel/fork.o", "lib/string.o", "init/do_mounts.o", "init/do_mounts_initrd.o", "usr/initramfs_data.o",
    "init/initramfs.o", ".vmlinux.export.o", reuse_report.PIGGY_OBJECT,
)]


def query(outputs: list[str], mnemonic: str = "CppCompile") -> dict[str, list[dict[str, object]]]:
    fragments, artifacts, actions = [], [], []
    for index, output in enumerate(outputs, 1):
        parent = 0
        for part in output.split("/"):
            fragment = len(fragments) + 1
            fragments.append({"id": fragment, "label": part, "parentId": parent})
            parent = fragment
        artifacts.append({"id": index, "pathFragmentId": parent})
        actions.append({"mnemonic": mnemonic, "outputIds": [index]})
    return {"pathFragments": fragments, "artifacts": artifacts, "actions": actions}


_QUERY = query(_OUTPUTS)


def spawn(index: int, *, cache_hit: bool = False, status: str = "", exit_code: int = 0) -> dict[str, object]:
    # These are jq's projected fields, including protobuf's omitted defaults.
    return {"spawn": {
        "outputs": [{"outputId": index + 1}],
        "mnemonic": "CppCompile",
        "runner": "disk cache hit" if cache_hit else "linux-sandbox",
        "cacheHit": cache_hit,
        "exitCode": exit_code,
        "status": status,
    }}


class ReuseReportTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir=os.environ.get("TEST_TMPDIR"))
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.expected = {(path,) for path in _OUTPUTS}

    def log(self, name: str, spawns: list[dict[str, object]]) -> Path:
        path = self.directory / f"{name}.execution.jsonl"
        entries = [{"invocation": name}]
        entries.extend({"id": index + 1, "path": output} for index, output in enumerate(_OUTPUTS))
        entries.extend(spawns)
        path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
        return path

    def trial(self, edited: list[dict[str, object]]) -> dict[str, object]:
        for name in reuse_report.PHASES:
            (self.directory / f"{name}.aquery.json").write_text(json.dumps(_QUERY))
            (self.directory / f"{name}.digests.json").write_text(json.dumps({
                "kernel/fork.o": "unchanged",
                "init/main.o": "unchanged",
                "usr/initramfs_data.o": "modified" if name in ("incbin", "gzip") else "original",
                "usr/initramfs_inc_data": "modified" if name in ("incbin", "gzip") else "original",
                ".vmlinux.export.c": "unchanged",
                ".vmlinux.export.o": "unchanged",
                reuse_report.PIGGY_SOURCE: "unchanged",
                reuse_report.PIGGY_OBJECT: "gzip-modified" if name == "gzip" else "original",
                reuse_report.GZIP_PAYLOAD: "gzip-modified" if name == "gzip" else "original",
                "decompressed_gzip": "unchanged",
            }))
            (self.directory / f"{name}.sizes.json").write_text(json.dumps({reuse_report.GZIP_PAYLOAD: 1024}))
        self.log("baseline", [spawn(index) for index in range(len(_OUTPUTS))])
        self.log("unchanged", [])
        self.log("edited", edited)
        self.log("header", [spawn(3), spawn(4), spawn(6)])
        self.log("incbin", [spawn(5)])
        self.log("gzip", [spawn(8)])
        return reuse_report.report(self.directory)

    def test_execution_cache_and_omitted_actions_are_distinct(self) -> None:
        result = reuse_report.phase(self.log("baseline", [spawn(0), spawn(1, cache_hit=True)]), self.expected)
        self.assertEqual(result.executed, {(_OUTPUTS[0],)})
        self.assertEqual(result.cached, {(_OUTPUTS[1],)})
        self.assertEqual(result.report()["absent_from_execution_log"], 7)
        self.assertEqual(result.outcome((_OUTPUTS[2],)), "absent_from_execution_log")

    def test_ir_snapshot_uses_the_report_producer_not_its_alias(self) -> None:
        paths = {role: f"bazel-out/cfg/bin/native/{role}" for role in ("frontend", "bitcode", "integrated", "object")}
        report = "bazel-out/cfg/bin/native/kernel.ir.json"
        alias = "bazel-out/cfg/bin/view/kernel.ir.json"
        outputs = [report, alias, *paths.values()]
        graph = query(outputs)
        graph["actions"][0]["mnemonic"] = "FileWrite"
        graph["actions"][1]["mnemonic"] = "Symlink"
        (self.directory / "base.aquery.json").write_text(json.dumps(graph))
        for role, path in paths.items():
            output = self.directory / path
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(role)
        (self.directory / report).write_text(json.dumps({"lib/ctype.o": paths}))
        ir_reuse_report.snapshot(self.directory, "base", self.directory)
        snapshot = ir_reuse_report.read_snapshot(self.directory / "base.ir.json")
        self.assertEqual(snapshot["lib/ctype.o"]["paths"], paths)

    def test_ir_roles_follow_declared_files_and_reject_duplicates(self) -> None:
        roles = {"frontend": "bazel-out/cfg/bin/first", "integrated": "bazel-out/cfg/bin/reference", "object": "bazel-out/cfg/bin/final"}
        artifacts = {"lib/ctype.o": ir_reuse_report.ArtifactRecord(paths=roles, digests={})}
        entries = [{"invocation": "ir", "hashFunctionName": "SHA-256"}]
        entries.extend({"id": index + 1, "path": path} for index, path in enumerate(roles.values()))
        for index in range(len(roles)):
            entry = spawn(index, cache_hit=True)
            native = entry["spawn"]
            assert isinstance(native, dict), entry
            native["digest"] = {"hash": str(index), "sizeBytes": 146}
            entries.append(entry)
        path = self.directory / "ir.execution.jsonl"
        path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
        result = ir_reuse_report.execution(path, artifacts)
        self.assertEqual(result["frontend"][0]["output"], roles["frontend"])
        self.assertEqual(result["outcomes"].keys(), {"integrated:lib/ctype.o", "object:lib/ctype.o"})
        with path.open("a") as output:
            output.write(json.dumps(entries[1 + len(roles)]) + "\n")
        with self.assertRaisesRegex(ValueError, "Duplicate selected compiler frontend:lib/ctype.o"):
            ir_reuse_report.execution(path, artifacts)

    def test_invocation_only_log_is_valid(self) -> None:
        path = self.directory / "unchanged.execution.jsonl"
        path.write_text('{"invocation": "unchanged"}\n')
        self.assertEqual(reuse_report.phase(path, self.expected).executed, set())

    def test_invalid_logs_cannot_look_like_cache_reuse(self) -> None:
        for spawns, message in [
            ([spawn(0, status="EXECUTION_FAILED")], "Failed CppCompile"),
            ([spawn(0, exit_code=1)], "Failed CppCompile"),
            ([spawn(99)], "Unresolved compiler output"),
            ([spawn(0), spawn(0, cache_hit=True)], "Repeated compiler output"),
            ([{"invocation": "another"}], "Unexpected compact-log projection"),
        ]:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    reuse_report.phase(self.log("invalid", spawns), self.expected)
        for contents in ("", '{"invocation":'):
            with self.subTest(contents=contents):
                path = self.directory / "invalid.execution.jsonl"
                path.write_text(contents)
                with self.assertRaises(ValueError):
                    reuse_report.phase(path, self.expected)

    def test_qualification_distinguishes_selective_from_broad_recompilation(self) -> None:
        selective = self.trial([spawn(0)])
        self.assertEqual(selective["phases"]["edited"]["total_kernel_compiles"], 9)
        self.assertEqual(selective["qualification"]["all_unrelated_compiles_reused"], True)
        broad = self.trial([spawn(index) for index in range(len(_OUTPUTS))])
        self.assertEqual(broad["measurement_complete"], True)
        self.assertEqual(broad["qualification"]["unrelated_executed_after_edit"], 8)
        self.assertEqual(broad["qualification"]["all_unrelated_compiles_reused"], False)

    def test_missing_edited_compile_is_inconclusive(self) -> None:
        for edited in ([], [spawn(0, cache_hit=True)], [spawn(1)]):
            with self.subTest(edited=edited):
                with self.assertRaisesRegex(ValueError, "Edited-source compile did not execute"):
                    self.trial(edited)

    def test_changed_inventory_or_unchanged_execution_invalidates_trial(self) -> None:
        self.trial([spawn(0)])
        self.log("unchanged", [spawn(1)])
        with self.assertRaisesRegex(ValueError, "Unchanged build executed"):
            reuse_report.report(self.directory)
        self.log("unchanged", [])
        changed = dict(_QUERY, actions=_QUERY["actions"][:-1])
        (self.directory / "edited.aquery.json").write_text(json.dumps(changed))
        with self.assertRaisesRegex(ValueError, "inventory changed"):
            reuse_report.report(self.directory)

    def test_native_compiles_are_selected_by_kernel_output_ownership(self) -> None:
        path = self.directory / "inventory.json"
        host = "bazel-out/bin/_objs/objtool.objects/main.o"
        for mnemonic in ("CppCompile", "Assemble"):
            with self.subTest(mnemonic=mnemonic):
                path.write_text(json.dumps(query([*_OUTPUTS, host], mnemonic)))
                self.assertEqual(reuse_report.inventory(path), self.expected)
        path.write_text(json.dumps(query(["bazel-out/bin/legacy/init/main.o"], "KbuildCC")))
        self.assertEqual(reuse_report.inventory(path), {("bazel-out/bin/legacy/init/main.o",)})

    def test_stale_header_or_payload_cannot_pass(self) -> None:
        self.trial([spawn(0)])
        self.log("header", [spawn(3)])
        with self.assertRaisesRegex(ValueError, "Included-header consumers did not execute"):
            reuse_report.report(self.directory)
        self.log("header", [spawn(3), spawn(4), spawn(6)])
        self.log("incbin", [])
        with self.assertRaisesRegex(ValueError, "Assembly embedding.*did not execute"):
            reuse_report.report(self.directory)
        self.log("incbin", [spawn(5)])
        (self.directory / "incbin.digests.json").write_bytes((self.directory / "header.digests.json").read_bytes())
        with self.assertRaisesRegex(ValueError, "initramfs edit did not change"):
            reuse_report.report(self.directory)

    def test_unmeasured_baseline_cannot_inflate_reuse(self) -> None:
        self.trial([spawn(0)])
        self.log("baseline", [spawn(0)])
        with self.assertRaisesRegex(ValueError, "Baseline did not observe every compiler"):
            reuse_report.report(self.directory)

    def test_incbin_fanout_cannot_pass_by_preserving_only_named_controls(self) -> None:
        self.trial([spawn(0)])
        self.log("incbin", [spawn(5), spawn(7)])
        result = reuse_report.report(self.directory)
        self.assertEqual(result["qualification"]["incbin_edit_recompiled_only_consumer"], False)

    def test_generated_source_control_requires_unchanged_bytes(self) -> None:
        self.trial([spawn(0)])
        path = self.directory / "incbin.digests.json"
        digests = json.loads(path.read_text())
        digests[".vmlinux.export.c"] = "changed"
        path.write_text(json.dumps(digests))
        with self.assertRaisesRegex(ValueError, "initramfs edit unexpectedly changed"):
            reuse_report.report(self.directory)

    def test_gzip_trial_requires_actual_embedding_execution(self) -> None:
        for spawns in ([], [spawn(8, cache_hit=True)]):
            with self.subTest(spawns=spawns):
                self.trial([spawn(0)])
                self.log("gzip", spawns)
                with self.assertRaisesRegex(ValueError, "changed gzip payload did not execute"):
                    reuse_report.report(self.directory)

    def test_gzip_trial_preserves_source_and_changes_payload(self) -> None:
        for target, value, message in (
            (reuse_report.PIGGY_SOURCE, "modified", "changed generated piggy.S"),
            (reuse_report.GZIP_PAYLOAD, "original", "did not change.*vmlinux.bin.gz"),
            (reuse_report.PIGGY_OBJECT, "original", "did not change.*piggy.o"),
            ("decompressed_gzip", "modified", "changed the decompressed payload"),
            ("init/main.o", "modified", "unexpectedly changed init/main.o"),
        ):
            with self.subTest(target=target):
                self.trial([spawn(0)])
                path = self.directory / "gzip.digests.json"
                digests = json.loads(path.read_text())
                digests[target] = value
                path.write_text(json.dumps(digests))
                with self.assertRaisesRegex(ValueError, message):
                    reuse_report.report(self.directory)
        self.trial([spawn(0)])
        (self.directory / "gzip.sizes.json").write_text(json.dumps({reuse_report.GZIP_PAYLOAD: 2048}))
        with self.assertRaisesRegex(ValueError, "changed the payload size"):
            reuse_report.report(self.directory)

    def test_gzip_trial_rejects_unrelated_compilation(self) -> None:
        self.trial([spawn(0)])
        self.log("gzip", [spawn(2), spawn(8)])
        result = reuse_report.report(self.directory)
        self.assertEqual(result["qualification"]["gzip_edit_recompiled_only_consumer"], False)


if __name__ == "__main__":
    unittest.main()
