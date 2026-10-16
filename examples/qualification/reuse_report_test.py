"""Check that reuse measurements distinguish execution, caches and invalid trials."""

import json
import os
from pathlib import Path
import tempfile
import unittest

from qualification import reuse_report


_OUTPUTS = [f"bazel-out/bin/_objs/kernel.compile/{target}/source/{target}" for target in (
    "init/main.o", "kernel/fork.o", "lib/string.o", "init/do_mounts.o", "init/do_mounts_initrd.o", "usr/initramfs_data.o",
    "init/initramfs.o", ".vmlinux.export.o",
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
                "usr/initramfs_data.o": "modified" if name == "incbin" else "original",
                "usr/initramfs_inc_data": "modified" if name == "incbin" else "original",
                ".vmlinux.export.c": "unchanged",
                ".vmlinux.export.o": "unchanged",
            }))
        self.log("baseline", [spawn(index) for index in range(len(_OUTPUTS))])
        self.log("unchanged", [])
        self.log("edited", edited)
        self.log("header", [spawn(3), spawn(4), spawn(6)])
        self.log("incbin", [spawn(5)])
        return reuse_report.report(self.directory)

    def test_execution_cache_and_omitted_actions_are_distinct(self) -> None:
        result = reuse_report.phase(self.log("baseline", [spawn(0), spawn(1, cache_hit=True)]), self.expected)
        self.assertEqual(result.executed, {(_OUTPUTS[0],)})
        self.assertEqual(result.cached, {(_OUTPUTS[1],)})
        self.assertEqual(result.report()["absent_from_execution_log"], 6)
        self.assertEqual(result.outcome((_OUTPUTS[2],)), "absent_from_execution_log")

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
        self.assertEqual(selective["phases"]["edited"]["total_kernel_compiles"], 8)
        self.assertEqual(selective["qualification"]["all_unrelated_compiles_reused"], True)
        broad = self.trial([spawn(index) for index in range(len(_OUTPUTS))])
        self.assertEqual(broad["measurement_complete"], True)
        self.assertEqual(broad["qualification"]["unrelated_executed_after_edit"], 7)
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


if __name__ == "__main__":
    unittest.main()
