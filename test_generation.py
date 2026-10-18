"""Native syntax transport keeps conditions and compiler query context symbolic."""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from export import native_events, parse_events
from programs import lower_probes
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
