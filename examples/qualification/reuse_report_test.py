"""Check that reuse measurements distinguish execution, caches and invalid trials."""

import json
import os
from pathlib import Path
import tempfile
import unittest

from qualification import ir_compare, ir_reuse_report, reuse_report


_OUTPUTS = [f"bazel-out/mapped/bin/_objs/kernel.compile/{target}/source/{target}" for target in (
    "init/main.o", "kernel/fork.o", "lib/string.o", "init/do_mounts.o", "init/do_mounts_initrd.o", "usr/initramfs_data.o",
    "init/initramfs.o", ".vmlinux.export.o", reuse_report.PIGGY_OBJECT,
)]


def reuse_report_target(path: str) -> str:
    return path.split("kernel.compile/", 1)[1].split("/source/", 1)[0]


def query(outputs: list[str], mnemonic: str = "CppCompile") -> dict[str, list[dict[str, object]]]:
    fragments, artifacts, actions = [], [], []
    for index, output in enumerate(outputs, 1):
        parent = 0
        for part in output.split("/"):
            fragment = len(fragments) + 1
            fragments.append({"id": fragment, "label": part, "parentId": parent})
            parent = fragment
        artifacts.append({"id": index, "pathFragmentId": parent})
        actions.append({"mnemonic": mnemonic, "outputIds": [index], "targetId": 1})
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
        self.compilers = {reuse_report_target(path): {"source": reuse_report_target(path).removesuffix(".o") + ".c", "outputs": {"single": [path]}, "bitcode_digest": None, "generated_inputs": {"single": {}}} for path in _OUTPUTS}

    def log(self, name: str, spawns: list[dict[str, object]]) -> Path:
        path = self.directory / f"{name}.execution.jsonl"
        entries = [{"invocation": name}]
        entries.extend({"id": index + 1, "path": output} for index, output in enumerate(_OUTPUTS))
        entries.extend(spawns)
        path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
        return path

    def trial(self, edited: list[dict[str, object]]) -> dict[str, object]:
        for name in reuse_report.PHASES:
            (self.directory / f"{name}.compilers.json").write_text(json.dumps(self.compilers))
            (self.directory / f"{name}.digests.json").write_text(json.dumps({
                "kernel/fork.o": "unchanged",
                "init/main.o": "unchanged",
                "usr/initramfs_data.o": "modified" if name in ("incbin", "gzip") else "original",
                "usr/initramfs_inc_data": "modified" if name in ("incbin", "gzip") else "original",
                ".vmlinux.export.c": "unchanged",
                ".vmlinux.export.o": "unchanged",
                reuse_report.PIGGY_SOURCE: "unchanged",
                reuse_report.PIGGY_OBJECT: "gzip-modified" if name == "gzip" else "incbin-modified" if name == "incbin" else "original",
                reuse_report.GZIP_PAYLOAD: "gzip-modified" if name == "gzip" else "incbin-modified" if name == "incbin" else "original",
                "decompressed_gzip": "unchanged",
                **{header: "unchanged" for header in reuse_report.BOOT_HEADERS},
            }))
            (self.directory / f"{name}.sizes.json").write_text(json.dumps({reuse_report.GZIP_PAYLOAD: 1024}))
        self.log("baseline", [spawn(index) for index in range(len(_OUTPUTS))])
        self.log("unchanged", [])
        self.log("edited", edited)
        self.log("header", [spawn(3), spawn(4), spawn(6)])
        self.log("incbin", [spawn(5), spawn(8)])
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

    def test_native_product_ancestry_excludes_reference_and_unrelated_compiles(self) -> None:
        paths = ["vmlinux", "logical.o", "backend.o", "unit.bc", "frontend.o", "reference.o", "source.c", "unrelated.o"]
        graph = query(paths)
        edges = {1: [2], 2: [3], 3: [4], 4: [5], 5: [7], 6: [7], 8: [7]}
        graph["depSetOfFiles"] = [{"id": identity, "directArtifactIds": inputs} for identity, inputs in edges.items()]
        graph["actions"] = [dict(action, inputDepSetIds=[index]) for index, action in enumerate(graph["actions"], 1) if index != 7]
        path = self.directory / "graph.json"
        path.write_text(json.dumps(graph))
        native = reuse_report.ActionGraph.read(path)
        self.assertEqual(native.ancestry(["vmlinux"]), set(paths) - {"reference.o", "unrelated.o"})
        self.assertTrue(native.consumes("backend.o", "unit.bc"))
        self.assertFalse(native.consumes("backend.o", "source.c"))
        with self.assertRaisesRegex(ValueError, "lack native producers"):
            native.ancestry(["source.c"])
        graph["actions"].append(dict(graph["actions"][0]))
        path.write_text(json.dumps(graph))
        repeated = reuse_report.ActionGraph.read(path)
        self.assertEqual(repeated.actions, native.actions)
        self.assertEqual(repeated.ancestry(["vmlinux"]), native.ancestry(["vmlinux"]))
        graph["depSetOfFiles"].append({"id": 9, "transitiveDepSetIds": [1]})
        graph["actions"][-1]["inputDepSetIds"] = [9]
        path.write_text(json.dumps(graph))
        self.assertEqual(reuse_report.ActionGraph.read(path).actions, native.actions)
        graph["depSetOfFiles"][-1]["transitiveDepSetIds"] = [1, 9]
        path.write_text(json.dumps(graph))
        with self.assertRaisesRegex(ValueError, "Cyclic aquery input set"):
            reuse_report.ActionGraph.read(path)
        graph["depSetOfFiles"][-1]["transitiveDepSetIds"] = [1]
        graph["actions"][-1]["inputDepSetIds"] = [6]
        path.write_text(json.dumps(graph))
        with self.assertRaisesRegex(ValueError, "conflicting aquery producer"):
            reuse_report.ActionGraph.read(path)
        graph["actions"].pop()
        graph["depSetOfFiles"][4]["directArtifactIds"] = [1]
        path.write_text(json.dumps(graph))
        with self.assertRaisesRegex(ValueError, "Cyclic native action graph"):
            reuse_report.ActionGraph.read(path).ancestry(["vmlinux"])

    def test_family_authentication_requires_the_real_compiler_chain(self) -> None:
        paths = ["vmlinux", "logical.o", "backend.o", "unit.bc", "frontend.o", "reference.o", "source.c", "unrelated.o", "kernel.json", "config", "diagnostics.json", "bazel-out/cfg/bin/attribute.bc", "bazel-out/cfg/bin/assembly.bc"]
        graph = query(paths)
        edges = {1: [2], 2: [3], 3: [4], 4: [5], 5: [7], 6: [7], 8: [7]}
        graph["depSetOfFiles"] = [{"id": identity, "directArtifactIds": inputs} for identity, inputs in edges.items()]
        for identity, action in enumerate(graph["actions"], 1):
            action["inputDepSetIds"] = [identity] if identity in edges else []
        graph["actions"][3]["mnemonic"] = "Symlink"
        graph["actions"][8]["mnemonic"] = "FileWrite"
        query_path = self.directory / "base.aquery.json"
        query_path.write_text(json.dumps(graph))
        for path in paths:
            artifact = self.directory / path
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text(path)
        report = {"kernel/panic.o": {
            "source": "source.c", "frontend": "frontend.o", "bitcode": "unit.bc", "integrated": "reference.o", "object": "backend.o", "flags": [],
        }}
        manifest = {
            "roots": ["vmlinux"], "artifacts": {"vmlinux": "vmlinux", "kernel/panic.o": "logical.o"},
            "compilers": {"kernel/panic.o": {"source": "source.c", "object": "backend.o", "kind": "c"}},
            "config": "config", "compiler_diagnostics": [
                "native.ir-diagnostics.json", "bazel-out/cfg/bin/attribute.bc", "bazel-out/cfg/bin/assembly.bc",
            ],
        }
        (self.directory / "native.ir-diagnostics.json").write_text(json.dumps([
            {"name": name, "bitcode": "bazel-out/cfg/bin/" + name + ".bc"} for name in ("attribute", "assembly")
        ]))
        (self.directory / "kernel.json").write_text(json.dumps(manifest))
        ir_reuse_report.authenticate(self.directory, "base", self.directory, "kernel.ir.json", report, reuse_report.ActionGraph.read(query_path))
        authenticated = json.loads((self.directory / "base.products.json").read_text())
        self.assertEqual(authenticated["compiler_counts"]["c"], 1)
        # A reference compile is a real action, but is not a product ancestor.
        manifest["compilers"]["kernel/panic.o"]["object"] = "reference.o"
        (self.directory / "kernel.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(AssertionError, "unreachable"):
            ir_reuse_report.authenticate(self.directory, "base", self.directory, "kernel.ir.json", report, reuse_report.ActionGraph.read(query_path))
        manifest["compilers"]["kernel/panic.o"]["object"] = "backend.o"
        (self.directory / "kernel.json").write_text(json.dumps(manifest))
        # Membership alone cannot authenticate a backend with another input.
        graph["depSetOfFiles"][2]["directArtifactIds"] = [7]
        query_path.write_text(json.dumps(graph))
        with self.assertRaises(AssertionError):
            ir_reuse_report.authenticate(self.directory, "base", self.directory, "kernel.ir.json", report, reuse_report.ActionGraph.read(query_path))

    def family_trial(self, mismatched_action: bool = False) -> dict[str, object]:
        targets = ["kernel/panic.o", "lib/ctype.o", "lib/a.o", "lib/b.o"]
        for phase in ir_reuse_report.FAMILY_PHASES:
            selected = targets + (["debug/only.o"] if phase == "debug" else [])
            if phase == "changed":
                selected = targets[:2]
            snapshot, compilers = {}, {}
            entries = [{"invocation": phase, "hashFunctionName": "SHA-256"}]
            for index, target in enumerate(selected):
                changed = phase == "changed" and target == "kernel/panic.o"
                content = target + ("-changed" if changed else "")
                paths = {role: f"bazel-out/{phase}/bin/{role}/{target}" for role in ("frontend", "bitcode", "integrated", "object")}
                snapshot[target] = {"paths": paths, "digests": {role: content for role in paths}}
                compilers[target] = {"source": "source/" + target.removesuffix(".o") + ".c", "object": paths["object"], "kind": "c"}
                if phase == "unchanged":
                    continue
                entries.append({"id": index + 1, "path": paths["object"]})
                outcome = spawn(index, cache_hit=phase == "lz4")
                outcome["spawn"]["digest"] = {"hash": content + ("-different" if mismatched_action and phase == "lz4" and index == 0 else ""), "sizeBytes": 146}
                entries.append(outcome)
            (self.directory / f"{phase}.ir.json").write_text(json.dumps(snapshot))
            (self.directory / f"{phase}.execution.jsonl").write_text("".join(json.dumps(entry) + "\n" for entry in entries))
            (self.directory / f"{phase}.products.json").write_text(json.dumps({
                "compilers": compilers,
                "products": {"vmlinux": {}, "arch/x86/boot/bzImage": {}},
                "config": '\n'.join(['CONFIG_DEBUG_KERNEL=y', 'CONFIG_LOCALVERSION="-debug"', 'CONFIG_DEBUG_INFO_BTF=y', 'CONFIG_KERNEL_LZ4=y', 'CONFIG_PANIC_TIMEOUT=' + ('37' if phase == "changed" else '0')]),
            }))
        ir_reuse_report.summarize(self.directory, family=True)
        return json.loads((self.directory / "ir-reuse.json").read_text())

    def test_family_allows_distinct_inventories_but_requires_real_paired_hits(self) -> None:
        result = self.family_trial()
        self.assertEqual(result["counts"]["debug"], 5)
        self.assertEqual(result["counts"]["changed"], 2)
        self.assertEqual(result["base_lz4"]["actual_backend_cache_hits"], 4)
        self.assertFalse(result["panic_backend_outcome"]["cache_hit"])
        with self.assertRaises(AssertionError):
            self.family_trial(mismatched_action=True)
        failed = json.loads((self.directory / "ir-reuse.json").read_text())
        self.assertEqual(failed["base_lz4"]["actual_backend_cache_hits"], 3)

    def test_family_comparison_preserves_all_diagnostic_failures(self) -> None:
        self.family_trial()
        for phase in ir_reuse_report.FAMILY_PHASES:
            directory = self.directory / "diagnostics" / phase
            directory.mkdir(parents=True)
            (directory / "report.json").write_text("[]")
        with self.assertRaises(AssertionError):
            ir_compare.compare_family(self.directory)
        result = json.loads((self.directory / "ir-comparison.json").read_text())
        self.assertEqual(set(result["diagnostic_errors"]), set(ir_reuse_report.FAMILY_PHASES))
        self.assertEqual(result["objects"]["debug"], 5)
        self.assertEqual(result["object_mismatches"]["base"], [])

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
        self.assertEqual(selective["phases"]["edited"]["total_compiler_actions"], 9)
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
        changed = {target: record for target, record in self.compilers.items() if target != "init/main.o"}
        (self.directory / "edited.compilers.json").write_text(json.dumps(changed))
        with self.assertRaisesRegex(ValueError, "inventory changed"):
            reuse_report.report(self.directory)

    def test_compiler_inventory_rejects_missing_and_duplicate_phases(self) -> None:
        path = self.directory / "compilers.json"
        for outputs in ({"backend": ["object.o"]}, {"frontend": ["same.o"], "backend": ["same.o"]}):
            path.write_text(json.dumps({"init/main.o": {"source": "source.c", "outputs": outputs, "bitcode_digest": "ir", "generated_inputs": {role: {} for role in outputs}}}))
            with self.assertRaises(ValueError):
                reuse_report.read_compilers(path)

    def test_mutation_snapshot_rejects_unpromoted_clang_c(self) -> None:
        paths = ["vmlinux", "logical.o", "raw.o", "source.c", "kernel.json"]
        graph = query(paths)
        graph["depSetOfFiles"] = [{"id": 1, "directArtifactIds": [2]}, {"id": 2, "directArtifactIds": [3]}]
        graph["actions"][0]["inputDepSetIds"] = [1]
        graph["actions"][1]["inputDepSetIds"] = [2]
        graph["actions"][-1]["mnemonic"] = "FileWrite"
        (self.directory / "baseline.aquery.json").write_text(json.dumps(graph))
        (self.directory / "baseline.manifest-path").write_text("kernel.json\n")
        (self.directory / "kernel.json").write_text(json.dumps({
            "compiler": "clang", "roots": ["vmlinux"],
            "artifacts": {"vmlinux": "vmlinux", "init/main.o": "logical.o", **{header: "source.c" for header in reuse_report.BOOT_HEADERS}},
            "compilers": {"init/main.o": {"kind": "c", "source": "source.c", "object": "raw.o"}},
        }))
        (self.directory / "source.c").write_text("int source;\n")
        with self.assertRaisesRegex(ValueError, "requires split target C"):
            reuse_report.snapshot(self.directory, "baseline", self.directory)

    def test_split_mutation_counts_phases_without_doubling_objects(self) -> None:
        self.trial([spawn(0)])
        split_indices = (0, 3, 4, 6)
        frontends = {index: f"bazel-out/mapped/bin/frontend/{index}.o" for index in split_indices}
        for name in reuse_report.PHASES:
            compilers = json.loads((self.directory / f"{name}.compilers.json").read_text())
            for index, frontend in frontends.items():
                target = reuse_report_target(_OUTPUTS[index])
                compilers[target]["outputs"] = {"frontend": [frontend], "backend": [_OUTPUTS[index]]}
                compilers[target]["generated_inputs"] = {"frontend": {}, "backend": {}}
                compilers[target]["bitcode_digest"] = "changed" if index == 3 and name in ("header", "incbin", "gzip") else "original"
            (self.directory / f"{name}.compilers.json").write_text(json.dumps(compilers))
            path = self.directory / f"{name}.execution.jsonl"
            records = [json.loads(line) for line in path.read_text().splitlines()]
            declarations = [{"id": len(_OUTPUTS) + index + 1, "path": frontend} for index, frontend in frontends.items()]
            new_spawns = [spawn(len(_OUTPUTS) + index) for index in split_indices
                          if name == "baseline" or name == "edited" and index == 0 or name == "header" and index in (3, 4, 6)]
            if name == "header":
                records = [record for record in records if "spawn" not in record] + [spawn(3)]
            path.write_text("".join(json.dumps(record) + "\n" for record in records[:1] + declarations + records[1:] + new_spawns))
        result = reuse_report.report(self.directory)
        self.assertEqual(result["logical_kernel_objects"], 9)
        self.assertEqual(result["compiler_phase_counts"], {"frontend": 4, "backend": 4, "single": 5})
        self.assertTrue(result["qualification"]["header_edit_recompiled_only_consumers"])
        self.assertEqual(result["phases"]["header"]["compiler_phases"]["backend"]["executed"], 1)
        # A changed IR backend must be observed, even when it is already cached.
        path = self.directory / "header.execution.jsonl"
        records = [json.loads(line) for line in path.read_text().splitlines()]
        records = [record for record in records if record != spawn(3)]
        path.write_text("".join(json.dumps(record) + "\n" for record in records))
        with self.assertRaisesRegex(ValueError, "Changed header IR has no observed backend"):
            reuse_report.report(self.directory)
        with path.open("a") as stream:
            stream.write(json.dumps(spawn(3, cache_hit=True)) + "\n")
        self.assertTrue(reuse_report.report(self.directory)["qualification"]["header_edit_recompiled_only_consumers"])

    def test_changed_boot_headers_require_only_authenticated_consumers(self) -> None:
        self.trial([spawn(0)])
        target = "lib/string.o"
        header = reuse_report.BOOT_HEADERS[0]
        for name in reuse_report.PHASES:
            path = self.directory / f"{name}.compilers.json"
            records = json.loads(path.read_text())
            records[target]["generated_inputs"]["single"] = {header: "changed" if name in ("incbin", "gzip") else "original"}
            path.write_text(json.dumps(records))
        with self.assertRaisesRegex(ValueError, "Changed native boot header has no observed compiler"):
            reuse_report.report(self.directory)
        self.log("incbin", [spawn(2), spawn(5), spawn(8)])
        self.assertTrue(reuse_report.report(self.directory)["qualification"]["incbin_edit_recompiled_only_consumer"])
        self.log("incbin", [spawn(2), spawn(5), spawn(7), spawn(8)])
        self.assertFalse(reuse_report.report(self.directory)["qualification"]["incbin_edit_recompiled_only_consumer"])

    def test_stale_header_or_payload_cannot_pass(self) -> None:
        self.trial([spawn(0)])
        self.log("header", [spawn(3)])
        with self.assertRaisesRegex(ValueError, "Included-header consumers did not execute"):
            reuse_report.report(self.directory)
        self.log("header", [spawn(3), spawn(4), spawn(6)])
        self.log("incbin", [])
        with self.assertRaisesRegex(ValueError, "Assembly embedding.*did not execute"):
            reuse_report.report(self.directory)
        self.log("incbin", [spawn(5), spawn(8)])
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
        self.log("incbin", [spawn(5), spawn(7), spawn(8)])
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
            (reuse_report.GZIP_PAYLOAD, "incbin-modified", "did not change.*vmlinux.bin.gz"),
            (reuse_report.PIGGY_OBJECT, "incbin-modified", "did not change.*piggy.o"),
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
