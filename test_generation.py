"""Native syntax transport keeps conditions and compiler query context symbolic."""
import argparse
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from compiler_queries import lower_compiler_queries, lower_initramfs_inputs
from export import native_events, parse_events
from programs import export_directives, lower_probes, slice_program
from syntax import runtime_program


class SymbolicExportTest(unittest.TestCase):
    def test_native_export_does_not_evaluate_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary) / 'Makefile'
            fixture.write_text('VALUE := $(error export evaluated a Make expression)\nall:\n\t$(error export executed a recipe)\n')
            events = native_events([MAKE], [fixture], expressions=True)
            self.assertTrue(any(event.get('name') == 'VALUE' for event in events), events)
            self.assertTrue(any(event.get('kind') == 'recipe' for event in events), events)
            projected = runtime_program(parse_events(events)['files'][0]['body'], fixture.parent.resolve())
            assignment = next(node for node in projected if node.get('name') == 'VALUE')
            original = next(node for node in events if node.get('name') == 'VALUE')
            self.assertEqual(assignment['value'], original['value'])
            self.assertEqual(assignment['value_expression'], original['value_expression'])
            self.assertEqual((assignment['file'], assignment['line']), ('Makefile', 1))
            self.assertNotIn('text', assignment)
            self.assertNotIn('words', assignment)

    def test_native_setup_rejects_missing_required_include(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary) / 'Makefile'
            setup = Path(temporary) / 'setup.json'
            for directive, succeeds in [('include', False), ('-include', True)]:
                with self.subTest(directive=directive):
                    fixture.write_text(f'{directive} {temporary}/absent.mk\nexport VALUE := native\n')
                    result = subprocess.run([MAKE, '-rR', '-f', fixture], capture_output=True, text=True,
                                            env={'PATH': '', 'LINUX_BZL_MAKE_SETUP': str(setup)})
                    self.assertEqual(result.returncode == 0, succeeds, result.stderr)
                    if succeeds:
                        self.assertEqual(json.loads(setup.read_text())['VALUE'], 'native')
                    else:
                        self.assertFalse(setup.exists(), 'Failed include published a partial native setup')

    def test_probe_keeps_explicit_compiler_and_accumulated_flags(self):
        literal = lambda text: [{'op': 'literal', 'value': text}]
        compiler = [{'op': 'reference', 'name': literal('HOSTCC')}]
        flags = [{'op': 'reference', 'name': literal('HOSTCFLAGS')}]
        result = lower_probes({'op': 'function', 'name': 'call', 'arguments': [
            literal('__cc-option'), compiler, flags, literal('-Werror'), literal('fallback')]})
        self.assertEqual(result['name'], 'kbuild_probe')
        self.assertEqual(result['probe_context'], {'compiler': compiler, 'flags': flags})
        self.assertEqual(result['arguments'], [literal('cc-option'), literal('-Werror'), literal('fallback')])

    def test_native_ifdef_keeps_its_literal_variable_dependency(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary) / 'Makefile'
            fixture.write_text('NATIVE_MODE := selected\nifdef NATIVE_MODE\nRESULT := selected\nelse\nRESULT := absent\nendif\nUNRELATED := omit\n')
            body = parse_events(native_events([MAKE], [fixture], expressions=True))['files'][0]['body']
            selected = slice_program(body, {'RESULT'})
            self.assertEqual([node.get('name') for node in selected], ['NATIVE_MODE', None])
            self.assertEqual([branch['body'][0]['value'] for branch in selected[1]['branches']], ['selected', 'absent'])

    def test_native_compiler_banner_guard_remains_a_measured_command(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary) / 'Makefile'
            fixture.write_text('ifneq ($(shell $(CC) --version 2>&1 | head -n 1 | grep clang),)\nCLANG_FLAGS += -fintegrated-as\nendif\nOTHER = $(shell printf recipe-output)\n')
            body = parse_events(native_events([MAKE], [fixture], expressions=True))['files'][0]['body']
            lowered = lower_compiler_queries(body)
            original = body[0]['branches'][0]
            measured = lowered[0]['branches'][0]
            query = original['condition']['operands'][0][0]
            self.assertEqual(measured['condition'], dict(original['condition'], operands=[[dict(query, name='kbuild_host_shell')], []]))
            self.assertEqual(measured['body'], original['body'])
            self.assertEqual(lowered[1], body[1], 'Unrelated shell expansion changed owners')

    def test_initramfs_input_lowering_preserves_native_expression_and_guards(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary) / 'Makefile'
            fixture.write_text(
                'ifdef CONFIG_BLK_DEV_INITRD\n'
                'ramfs-input := $(strip $(shell echo $(CONFIG_INITRAMFS_SOURCE)))\n'
                'ifeq ($(ramfs-input),)\n'
                'ramfs-input := $(srctree)/$(src)/default_cpio_list\n'
                'endif\n'
                'endif\n'
                'OTHER := $(shell echo $(CONFIG_INITRAMFS_SOURCE))\n')
            body = lower_probes(parse_events(native_events([MAKE], [fixture], expressions=True))['files'][0]['body'])
            expected = copy.deepcopy(body)
            original = body[0]['branches'][0]['body'][0]
            selected = expected[0]['branches'][0]['body'][0]
            shell = selected['value_expression'][0]['arguments'][0][0]
            self.assertEqual((shell['op'], shell['name'], shell['arguments']), ('function', 'shell', [[
                dict(op='literal', value='echo '), dict(op='config', name='CONFIG_INITRAMFS_SOURCE')]]))
            shell['name'] = 'kbuild_host_shell'
            lowered = lower_initramfs_inputs(body)
            self.assertEqual(lowered, expected)
            self.assertEqual(original['value_expression'][0]['arguments'][0][0]['name'], 'shell')

    def test_export_slice_keeps_only_guards_with_exported_names(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary) / 'Makefile'
            fixture.write_text(
                'ifdef building_out_of_srctree\n'
                'KBUILD_CPPFLAGS += -fmacro-prefix-map=$(srcroot)/=\n'
                'ifeq ($(call rustc-option-yn, --remap-path-scope=macro),y)\n'
                'KBUILD_RUSTFLAGS += --remap-path-prefix=$(srcroot)/= --remap-path-scope=macro\n'
                'endif\nendif\n'
                'ifdef SELECTED\nIGNORED := value\nelse\nexport RETAINED\nendif\n'
                'export INLINE := $(error assignment belongs to another phase)\n'
                'unexport REMOVED\n')
            body = parse_events(native_events([MAKE], [fixture], expressions=True))['files'][0]['body']
            selected = export_directives(body)
            self.assertEqual(len(selected), 3, selected)
            branches = selected[0]['branches']
            self.assertEqual(branches[0]['condition'], body[1]['branches'][0]['condition'])
            self.assertEqual(branches[0]['body'], [])
            self.assertEqual(branches[1], body[1]['branches'][1])
            self.assertEqual((selected[1]['directive'], selected[1]['arguments']), ('export', 'INLINE'))
            self.assertEqual(selected[2], body[3])
            self.assertNotIn('rustc-option-yn', json.dumps(selected))

    def test_native_condition_preserves_both_config_dependent_branches(self):
        events = [dict(kind='file_begin', file='Kbuild'),
                  dict(kind='directive', directive='ifdef', arguments='CONFIG_SMP', file='Kbuild', line=1),
                  dict(kind='assignment', name='obj-y', value='smp.o'),
                  dict(kind='directive', directive='else', arguments='', file='Kbuild', line=3),
                  dict(kind='assignment', name='obj-y', value='up.o'),
                  dict(kind='directive', directive='endif', arguments='', file='Kbuild', line=5),
                  dict(kind='file_end')]
        branches = parse_events(events)['files'][0]['body'][0]['branches']
        self.assertEqual(branches[0]['arguments'], 'CONFIG_SMP')
        self.assertEqual([b['body'][0]['value'] for b in branches], ['smp.o', 'up.o'])
        with self.assertRaisesRegex(ValueError, 'Unclosed conditional'):
            parse_events(events[:-2] + [dict(kind='file_end')])


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--make', type=Path, required=True)
    arguments, remaining = parser.parse_known_args()
    MAKE = arguments.make.resolve()
    unittest.main(argv=[__file__, *remaining])
