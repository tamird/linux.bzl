"""Check that reuse measurements distinguish execution, caches and invalid trials."""

import json
import os
from pathlib import Path
import tempfile
import unittest

from qualification import reuse_report


_OUTPUTS = [
    "bazel-out/bin/init/main.o",
    "bazel-out/bin/kernel/fork.o",
    "bazel-out/bin/lib/string.o",
]
_QUERY = {
    "pathFragments": [
        {"id": 1, "label": "bazel-out"},
        {"id": 2, "label": "bin", "parentId": 1},
        {"id": 3, "label": "init", "parentId": 2},
        {"id": 4, "label": "main.o", "parentId": 3},
        {"id": 5, "label": "kernel", "parentId": 2},
        {"id": 6, "label": "fork.o", "parentId": 5},
        {"id": 7, "label": "lib", "parentId": 2},
        {"id": 8, "label": "string.o", "parentId": 7},
    ],
    "artifacts": [
        {"id": "11", "pathFragmentId": 4},
        {"id": "12", "pathFragmentId": 6},
        {"id": "13", "pathFragmentId": 8},
    ],
    "actions": [
        {"mnemonic": "KbuildCC", "outputIds": ["11"]},
        {"mnemonic": "KbuildCC", "outputIds": ["12"]},
        {"mnemonic": "KbuildCC", "outputIds": ["13"]},
    ],
}


def spawn(index: int, *, cache_hit: bool = False, status: str = "", exit_code: int = 0) -> dict[str, object]:
    # These are jq's projected fields, including protobuf's omitted defaults.
    return {"spawn": {
        "outputs": [{"outputId": index + 1}],
        "mnemonic": "KbuildCC",
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
        for name in ("baseline", "unchanged", "edited"):
            (self.directory / f"{name}.aquery.json").write_text(json.dumps(_QUERY))
        self.log("baseline", [spawn(index) for index in range(3)])
        self.log("unchanged", [])
        self.log("edited", edited)
        return reuse_report.report(self.directory)

    def test_execution_cache_and_omitted_actions_are_distinct(self) -> None:
        result = reuse_report.phase(self.log("baseline", [spawn(0), spawn(1, cache_hit=True)]), self.expected)
        self.assertEqual(result.executed, {(_OUTPUTS[0],)})
        self.assertEqual(result.cached, {(_OUTPUTS[1],)})
        self.assertEqual(result.report()["absent_from_execution_log"], 1)
        self.assertEqual(result.outcome((_OUTPUTS[2],)), "absent_from_execution_log")

    def test_invocation_only_log_is_valid(self) -> None:
        path = self.directory / "unchanged.execution.jsonl"
        path.write_text('{"invocation": "unchanged"}\n')
        self.assertEqual(reuse_report.phase(path, self.expected).executed, set())

    def test_invalid_logs_cannot_look_like_cache_reuse(self) -> None:
        for spawns, message in [
            ([spawn(0, status="EXECUTION_FAILED")], "Failed KbuildCC"),
            ([spawn(0, exit_code=1)], "Failed KbuildCC"),
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
        self.assertEqual(selective["phases"]["edited"]["total_kernel_compiles"], 3)
        self.assertEqual(selective["qualification"]["all_unrelated_compiles_reused"], True)
        broad = self.trial([spawn(index) for index in range(3)])
        self.assertEqual(broad["measurement_complete"], True)
        self.assertEqual(broad["qualification"]["unrelated_executed_after_edit"], 2)
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


if __name__ == "__main__":
    unittest.main()
