"""Shared capability queries for native export and analysis providers."""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def native_capabilities():
    lines = []
    for fact in json.loads((HERE / 'compiler_capabilities.json').read_text()):
        if fact.get('kbuild'):
            continue
        arguments = fact.get('arguments', [fact['argument']])
        matches = ['!strcmp(name, ' + json.dumps(fact['probe']) + ')', 'argc == ' + str(len(arguments))]
        for i, value in enumerate(arguments):
            alternatives = [value] + (fact.get('argument_aliases', []) if i == 0 else [])
            matches.append('(' + ' || '.join('prototype_query_equal(argv[%d], %s)' % (i, json.dumps(arg)) for arg in alternatives) + ')')
        lines.append('if (%s) return prototype_capability(%s, name, argc, argv);' % (' && '.join(matches), json.dumps(fact['input'])))
    return '\n'.join(lines)
