"""Behavioral checks for artifact transport used by the kernel action adapter."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

from payload import chunk_path, publish
from scheduler import DependencyScan

EXECUTOR=Path(__file__).absolute().parent/'execute.py'
SCHEDULER = Path(__file__).absolute().parent / 'scheduler.py'

class ExecuteTest(unittest.TestCase):
 def run_action(self,root,inputs,outputs,program,directories=(),directory_outputs=()):
  manifest=root/'action.json'
  manifest.write_text(json.dumps(dict(executable=sys.executable,arguments=['-c',program],inputs=inputs,outputs=outputs,
      directory_inputs=list(directories),directory_outputs=list(directory_outputs),environment={})))
  return subprocess.run([sys.executable,str(EXECUTOR),str(manifest)],cwd=root,capture_output=True,text=True)

 def test_regular_declared_input_and_output(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);(root/'input').write_text('kernel')
   result=self.run_action(root,['input'],['out/result'],"from pathlib import Path; Path('out/result').write_text(Path('input').read_text()+'-built')")
   self.assertEqual(result.returncode,0,result.stderr)
   self.assertEqual((root/'out/result').read_text(),'kernel-built')

 def test_directory_intermediate_round_trip(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp)
   result=self.run_action(root,[],['headers'],"from pathlib import Path; Path('headers').mkdir(); Path('headers/config.h').write_text('#define CONFIG_TEST 1')",directory_outputs=['headers'])
   self.assertEqual(result.returncode,0,result.stderr)
   result=self.run_action(root,['headers'],['result'],"from pathlib import Path; Path('result').write_text(Path('headers/config.h').read_text())",directories=['headers'])
   self.assertEqual(result.returncode,0,result.stderr)
   self.assertEqual((root/'result').read_text(),'#define CONFIG_TEST 1')

 def test_directory_transport_ignores_timestamps(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp)
   outputs=[]
   for stamp in [100,200]:
    result=self.run_action(root,[],['headers'],"from pathlib import Path; import os; Path('headers').mkdir(); Path('headers/config.h').write_text('constant'); os.utime('headers/config.h', (%d,%d))"%(stamp,stamp),directory_outputs=['headers'])
    self.assertEqual(result.returncode,0,result.stderr)
    outputs.append((root/'headers').read_bytes())
   self.assertEqual(outputs[0],outputs[1])

 def test_failed_command_is_a_build_failure(self):
  with tempfile.TemporaryDirectory() as tmp:
   result=self.run_action(Path(tmp),[],['result'],'raise SystemExit(42)')
   self.assertNotEqual(result.returncode,0)


class PayloadTest(unittest.TestCase):
    def test_nested_chunks_preserve_full_payload_and_bound_directory_fanout(self):
        value = dict(
            capabilities={'native': True, 'empty': '', 'unicode': 'λ'},
            history=[[dict(argv=['compiler', 'x' * 12_000], status=1, stdout='first\nsecond\n', stderr='warning')]],
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            publish(root, value)
            chunks = []
            for name, directories, files in os.walk(root):
                directory = Path(name)
                self.assertLessEqual(len(directories) + len(files), 101 if directory == root else 100)
                chunks.extend(directory / name for name in files if '=' in name)
            self.assertGreater(len(chunks), 100)
            self.assertTrue(all(path.parent != root for path in chunks))
            encoded = ''.join(path.name.split('=', 1)[1] for path in sorted(chunks, key=lambda path: path.name))
            self.assertEqual(json.loads(bytes.fromhex(encoded)), value)
            self.assertEqual(json.loads((root / 'report.json').read_text()), value)

    def test_chunk_index_boundary_rejects_overflow(self):
        self.assertEqual(chunk_path(99_999_999, 'ab'), Path('99/99/99/99999999=ab'))
        for index in [-1, 100_000_000]:
            with self.subTest(index=index), self.assertRaisesRegex(ValueError, 'eight-digit'):
                chunk_path(index, 'ab')


class CompilerDependenciesTest(unittest.TestCase):
    def test_source_and_generated_dependencies_keep_distinct_names(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            scan = DependencyScan(dict(
                source_prefix='prepared',
                original_prefix='original',
                aliases={'plan/generated.c': 'stage/artifacts/generated.c'},
                scan=dict(
                    argument_start=0,
                    header_tree='plan/headers',
                    generated={
                        'include/shared.h': 'plan/shared.h',
                        'generated.c': 'plan/generated.c',
                        'include/quoted name.h': 'plan/quoted.h',
                        'vmlinux.o': 'plan/vmlinux.o',
                    },
                ),
            ))
            scan.depfile = work / 'dependencies.d'
            scan.depfile.write_text(
                'object: original/source.c prepared/include/shared.h \\\n'
                f' {work}/plan/headers/include/shared.h '
                'stage/artifacts/generated.c include/quoted\\ name.h\n'
            )
            self.assertEqual(scan.dependencies(work), dict(
                sources=['include/shared.h', 'source.c'],
                generated=['generated.c', 'include/quoted name.h', 'include/shared.h'],
            ))

    def test_undeclared_generated_header_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            scan = DependencyScan(dict(
                source_prefix='prepared',
                original_prefix='original',
                aliases={},
                scan=dict(argument_start=0, header_tree='plan/headers', generated={}),
            ))
            scan.depfile = work / 'dependencies.d'
            scan.depfile.write_text('object: plan/headers/include/missing.h\n')
            with self.assertRaisesRegex(ValueError, 'undeclared generated dependency: include/missing.h'):
                scan.dependencies(work)

    def test_compile_reconstructs_only_measured_and_mandatory_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'selected.h').write_text('selected header')
            (root / 'generated.c').write_text('generated source')
            (root / 'initramfs').write_bytes(b'native payload')
            (root / 'tools.json').write_text('[]')
            # Neither the scan's aggregate archive nor the unused vmlinux.o
            # exists during execution. The original manifest still names both.
            manifest = root / 'compile.json'
            program = '\n'.join([
                'from pathlib import Path',
                'import shutil',
                "assert not Path('plan/vmlinux.o').exists()",
                "assert not Path('plan/headers/vmlinux.o').exists()",
                "assert Path('plan/headers/arch/x86').is_dir()",
                "shutil.copytree('plan/headers', 'native-work', symlinks=True)",
                "header = Path('native-work/include/generated/selected.h').read_text()",
                "payload = Path('native-work/usr/initramfs_data.cpio').read_bytes()",
                "source = Path('plan/generated.c').read_text()",
                "Path('plan/result').write_bytes((header + ':' + source).encode() + b':' + payload)",
            ])
            manifest.write_text(json.dumps(dict(
                executable=sys.executable,
                arguments=['-c', program],
                inputs=['plan/headers', 'plan/vmlinux.o', 'plan/generated.c'],
                outputs=['plan/result'],
                directory_inputs=['plan/headers'],
                directory_outputs=[],
                common_inputs='plan/tools.json',
                environment={},
            )))
            mapping = root / 'mapping.json'
            mapping.write_text(json.dumps(dict(
                manifest=str(manifest),
                aliases={
                    'plan/tools.json': 'tools.json',
                    'plan/generated.c': 'generated.c',
                    'plan/headers/include/generated/selected.h': 'selected.h',
                    'plan/headers/usr/initramfs_data.cpio': 'initramfs',
                },
                source_inputs=[],
                outputs={'plan/result': 'result'},
                empty_directories=['plan/headers', 'plan/headers/arch/x86'],
            )))
            result = subprocess.run(
                [sys.executable, str(SCHEDULER), 'execute', str(mapping)],
                cwd=root, capture_output=True, text=True,
                env=dict(os.environ, PYTHONPATH=str(SCHEDULER.parent)),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / 'result').read_bytes(), b'selected header:generated source:native payload')


if __name__=='__main__':unittest.main()
