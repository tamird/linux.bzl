#!/usr/bin/env python3
"""Whole-kernel extension of the native Make/Kconfig experiment."""
import argparse
import copy
import json
import re
from pathlib import Path
import shutil
import tempfile

import generate_llvm as backend
from export import native_events, parse_events
from generate import walk, relative
from kconfig_syntax import export_syntax
from text_transforms import lower_text_transforms
from compiler_queries import lower_compiler_queries
from shell_outputs import output_redirections
from execution_support import generate as generate_execution_tools
from qemu_support import generate as generate_qemu_tools
from metadata_support import generate as generate_compiler_metadata
from workspace_support import write_module
from execution_paths import lower_execution_paths
from prepare_source import script_interpreter

HERE = Path(__file__).resolve().parent


def make_text(terms):
    result = []
    for term in terms:
        if term['op'] == 'literal':
            result.append(term['value'].replace('$', '$$'))
        elif term['op'] == 'config':
            result.append('$(' + term['name'] + ')')
        elif term['op'] == 'reference':
            result.append('$(' + make_text(term['name']) + ')')
        elif term['op'] == 'function':
            result.append('$(' + term['name'] + ' ' + ','.join(make_text(a) for a in term['arguments']) + ')')
        else:
            raise ValueError(term)
    return ''.join(result)


def fragment_provenance(value, filename):
    if isinstance(value, dict):
        return {k: filename if k == 'file' else fragment_provenance(v, filename)
                for k, v in value.items()}
    if isinstance(value, list):
        return [fragment_provenance(v, filename) for v in value]
    return value


def without_assignments(body, owned):
    """Remove statements evaluated by a later phase, retaining their guards."""
    result = []
    for node in body:
        if node['kind'] == 'conditional':
            branches = [dict(branch, body=without_assignments(branch['body'], owned))
                        for branch in node['branches']]
            if any(branch['body'] for branch in branches):
                result.append(dict(node, branches=branches))
        elif node['kind'] not in ('assignment', 'define') or (node['file'], node['line']) not in owned:
            result.append(node)
    return result


def lower_eval_template(terms, make, definitions):
    """Parse fixed eval template syntax natively, retaining its symbolic loops."""
    significant = [t for t in terms if t['op'] != 'literal' or t['value'].strip()]
    if len(significant) == 1 and significant[0].get('name') == 'foreach':
        args = significant[0]['arguments']
        return [{'kind': 'eval_foreach', 'name_expression': args[0], 'words_expression': args[1],
                 'body': lower_eval_template(args[2], make, definitions)}]
    # eval removes one dollar-escaping layer before the native parser sees the
    # template. Runtime references remain references; no config is expanded.
    inputs, pieces = {}, []
    for term in terms:
        if term['op'] == 'literal':
            pieces.append(term['value'])
        else:
            name = '__KBUILD_EVAL_INPUT_' + str(len(inputs))
            inputs[name] = [term]
            pieces.append('$(' + name + ')')
    text = ''.join(pieces)
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / 'eval-template.mk'
        source.write_text(text + '\n')
        nodes = parse_events(native_events(make, [source], expressions=True))['files'][0]['body']
        nodes = fragment_provenance(nodes, '<eval-template>')
    entries = [(n, 'text' if n['kind'] == 'expression' else 'value')
               for n, _ in walk(nodes) if n['kind'] in ('expression', 'recipe') and 'value_expression' not in n]
    if entries:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'eval-expressions.mk'
            source.write_text(''.join('define __eval_expr_%d =\n%s\nendef\n' % (i, n[field])
                                      for i, (n, field) in enumerate(entries)))
            expressions = {n['name']: n['value_expression'] for n in native_events(make, [source], expressions=True)
                           if n['kind'] == 'define'}
        for i, (node, _) in enumerate(entries):
            node['value_expression'] = expressions['__eval_expr_' + str(i)]
    nodes = lower_body(nodes, make, definitions)
    return [{'kind': 'eval_template', 'inputs': inputs, 'body': nodes}]


def lower_statement(terms, make, definitions):
    terms = [t for t in terms if t['op'] != 'literal' or t['value'].strip()]
    if len(terms) != 1 or terms[0]['op'] != 'function':
        return None
    term = terms[0]
    args = term['arguments']
    if term['name'] == 'shell' and make_text(args[0]).strip().startswith('mkdir -p '):
        return {'kind': 'declared_directories', 'expression': args[0]}
    if term['name'] == 'foreach':
        body = lower_statement(args[2], make, definitions)
        return {'kind': 'foreach_eval', 'name_expression': args[0], 'words_expression': args[1], 'body': [body]} if body else None
    if term['name'] == 'eval':
        inner = args[0]
        if len(inner) == 1 and inner[0].get('name') == 'call':
            call = inner[0]['arguments']
            name = make_text(call[0]).strip()
            definition = definitions.get(name)
            if definition:
                return {'kind': 'eval_call', 'arguments': call[1:],
                        'body': lower_eval_template(definition['value_expression'], make, definitions)}
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'eval.mk'
            source.write_text(make_text(args[0]) + '\n')
            nodes = [n for n in native_events(make, [source], expressions=True) if not n['kind'].startswith('file_')]
        if len(nodes) == 1 and nodes[0]['kind'] == 'assignment':
            return dict(nodes[0], kind='eval_assignment', file='<eval>')
    return None


def lower_body(body, make, definitions=None):
    definitions = definitions or {n["name"]: n for n, _ in walk(body) if n["kind"] == "define"}
    result = []
    for node in body:
        if node['kind'] == 'conditional':
            node = dict(node, branches=[dict(b, body=lower_body(b['body'], make, definitions)) for b in node['branches']])
        elif node['kind'] == 'expression':
            lowered = lower_statement(node['value_expression'], make, definitions)
            if lowered:
                node = dict(node, **lowered)
        result.append(node)
    return result


_original_selected = backend.selected
_INCLUDE_DIRECTIVES = False
_INCLUDE_HEADER_RULES = False

def image_selected(body, wanted):
    result = []
    for node in body:
        if node['kind'] == 'foreach_eval':
            children = image_selected(node['body'], wanted)
            if children:
                result.append(dict(node, body=children))
        elif _INCLUDE_HEADER_RULES and node['kind'] in ('rule', 'recipe'):
            result.append(node)
        elif node['kind'] == 'directive' and (node['directive'] in ('export', 'unexport') or (_INCLUDE_DIRECTIVES and node['directive'] in ('include', '-include', 'sinclude'))):
            result.append(node)
        elif node['kind'] == 'target_assignment':
            name = backend.const(node['name_expression'])
            if name and backend.matches(name, wanted):
                result.append(node)
        elif node['kind'] == 'eval_assignment':
            result.extend([dict(n, kind='eval_assignment') for n in _original_selected([dict(node, kind='assignment')], wanted)])
        else:
            result.extend(_original_selected([node], wanted))
    return result


def target_rules(body, targets):
    """Retain native recipes and conditionals for selected target expressions."""
    result = []
    active = False
    for node in body:
        if node['kind'] == 'conditional':
            branches = [dict(branch, body=target_rules(branch['body'], targets))
                        for branch in node['branches']]
            if any(branch['body'] for branch in branches):
                result.append(dict(node, branches=branches))
            active = False
        elif node['kind'] == 'rule':
            names = make_text(node['targets_expression'])
            pattern, separator, _ = make_text(node['tail_expression']).partition(':')
            active = bool(set(names.split()) & targets or
                          (separator and pattern.strip() in targets))
            if active:
                result.append(node)
        elif node['kind'] == 'recipe' and active:
            result.append(node)
        else:
            active = False
    return result


def module_order_input(body, recipe):
    """Bind a selected list writer's words at its native modfinal read."""
    match output_redirections(recipe):
        case [destination]:
            pass
        case outputs:
            raise ValueError(f'Expected one selected-module order output, got {outputs}')
    match recipe:
        case [qualifier, {'op': 'literal', 'value': opening},
              {'op': 'function', 'name': 'foreach', 'arguments': [variable, words, emission]} as loop, *tail]:
            if (qualifier != {'op': 'reference', 'name': backend.literal('Q')} or
                    opening.strip() != '{' or
                    make_text(tail).strip() != '} > ' + make_text(destination)):
                raise ValueError(f'Unsupported selected-module order writer: {recipe}')
        case _:
            raise ValueError(f'Unsupported selected-module order writer: {recipe}')
    match emission:
        case [{'op': 'literal', 'value': before}, *word, {'op': 'literal', 'value': after}] if before.strip() == 'echo' and after.strip() == ';':
            contents = [dict(loop, arguments=[variable, words, word])]
        case _:
            raise ValueError(f'Unsupported selected-module order entry: {emission}')
    assignments = [n for n, _ in walk(body) if n.get('name') == 'modules']
    match assignments:
        case [assignment]:
            pass
        case _:
            raise ValueError(f'Expected one native module list assignment, got {assignments}')
    match assignment['value_expression']:
        case [{'op': 'function', 'name': 'sort', 'arguments': [[{'op': 'function', 'name': 'shell', 'arguments': [query]}]]} as sort]:
            if make_text(query).strip() != 'cat ' + make_text(destination):
                raise ValueError(f'Module list reads a different selected output: {query}')
        case expression:
            raise ValueError(f'Unsupported native module list read: {expression}')
    # Preserve the native sort and the writer's word expression. The only
    # substituted operation is reading this already-known generated list.
    assignment['value_expression'] = [dict(sort, arguments=[[
        {'op': 'reference', 'name': backend.literal('__selected_module_order')}]])]
    assignment['value'] = make_text(assignment['value_expression'])
    return contents


def archive_controls(body, rules):
    """Retain source-declared phony prerequisites without replaying setup."""
    prerequisites = {word for node, _ in walk(rules) if node['kind'] == 'rule'
                     for term in node['tail_expression'] if term['op'] == 'literal'
                     for word in term['value'].split()}
    nodes = list(walk(body))
    guards = {(node['file'], node['line']): branch for node, branch in nodes}
    archive_guards = [guards[(node['file'], node['line'])] for node, _ in walk(rules) if node['kind'] == 'rule']
    assignments = [node for node, _ in nodes if node.get('name') == 'PHONY']
    selected = [(node, set(value.split()) & prerequisites) for node in assignments
                if (value := backend.const(node['value_expression'])) is not None and
                set(value.split()) & prerequisites]
    if not selected:
        return []
    for index, node in enumerate(assignments):
        if node['flavor'] != 'append' and not (index == 0 and node['flavor'] in ('simple', 'recursive') and backend.const(node['value_expression']) is not None):
            raise ValueError(f'Cannot prove monotonic native PHONY membership: {node}')
    declarations = target_rules(body, {'.PHONY'})
    match [node for node, _ in walk(declarations) if node['kind'] == 'rule']:
        case [declaration] if declaration['tail_expression'] == [{'op': 'reference', 'name': backend.literal('PHONY')}]:
            pass
        case _:
            raise ValueError(f'Unsupported native phony declaration: {declarations}')
    result = []
    for assignment, names in selected:
        membership_guards = guards[(assignment['file'], assignment['line'])]
        if any(branch[:len(membership_guards)] != membership_guards for branch in archive_guards):
            raise ValueError(f'Phony membership has a separate native selection guard: {assignment}')
        controls = target_rules(body, names)
        control_lines = [node['line'] for node, _ in walk(controls)
                         if node['kind'] in ('rule', 'recipe')]
        if any(line < assignment['line'] for line in control_lines):
            raise ValueError(f'Native phony control precedes its membership declaration: {assignment}')
        last = max([assignment['line']] + control_lines)
        # Strip outer invocation orchestration as for the archive itself,
        # retaining CONFIG guards within this source-selected control phase.
        phase = backend.bounded(body, assignment['line'], last)
        result.extend(backend.selected(phase, {'PHONY'}))
        result.extend(target_rules(phase, names))
    result.extend(declarations)
    return result


def main(argv=None):
    global _INCLUDE_DIRECTIVES, _INCLUDE_HEADER_RULES
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--ast', type=Path, required=True)
    p.add_argument('--make', type=Path, required=True)
    p.add_argument('--linux', type=Path, default=Path('_tmp/linux'))
    p.add_argument('--out', type=Path, default=Path('_tmp/kernel-image/repo'))
    a = p.parse_args(argv)
    linux, out = a.linux.resolve(), a.out.resolve()
    syntax = export_syntax(linux, out.parent / 'kconfig-syntax')
    syntax_path = out.parent / 'kconfig-syntax/syntax.json'
    syntax_path.write_text(json.dumps(syntax))
    backend.main(['--ast', str(a.ast), '--make', str(a.make),
                  '--linux', str(linux), '--syntax', str(syntax_path), '--out', str(out),
                  '--default-config', '//configs:minimal', '--config-scope', 'target'])
    tree = json.loads(a.ast.read_text())
    extras = [p for pattern in ('Makefile*', 'Kbuild.*', 'Platform')
              for p in sorted(linux.rglob(pattern)) if p.is_file()]
    extras += [linux / 'scripts/Kbuild.include', linux / 'tools/objtool/Build', *sorted(linux.glob('tools/objtool/arch/*/Build')), linux / 'tools/lib/subcmd/Build', linux / 'tools/bpf/resolve_btfids/Build', linux / 'tools/build/Build.include']
    existing = {Path(f['file']).resolve() for f in tree['files']}
    tree['files'].extend(parse_events(native_events(a.make, [p for p in extras if p.resolve() not in existing], expressions=True))['files'])
    entries = []
    for f in tree['files']:
        for n, _ in walk(f['body']):
            if n['kind'] == 'directive' and n['directive'] in ('include', '-include', 'sinclude', 'export', 'unexport'):
                entries.append((n, 'arguments', 'arguments_expression'))
            elif n['kind'] in ('expression', 'recipe'):
                entries.append((n, 'value' if n['kind'] == 'recipe' else 'text', 'value_expression'))
            elif n['kind'] == 'target_assignment':
                entries.extend((n, key, key + '_expression') for key in ('targets', 'name', 'value'))
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / 'expressions.mk'
        source.write_text(''.join(f'x{i} = {n[field]}\n' for i, (n, field, _) in enumerate(entries)))
        expressions = {n['name']: n['value_expression'] for n in native_events(a.make, [source], expressions=True) if n['kind'] == 'assignment'}
    for i, (n, _, field) in enumerate(entries):
        n[field] = expressions['x' + str(i)]
    secondary = []
    for file in tree['files']:
        nodes = [node for node, _ in walk(file['body'])]
        declarations = []
        for node, guards in walk(file['body']):
            if node['kind'] == 'rule' and node.get('targets') == '.SECONDEXPANSION':
                if guards:
                    raise ValueError(f'Conditional secondary expansion needs explicit lowering: {node}')
                declarations.append(node['line'])
        if not declarations:
            continue
        for node in nodes:
            if (node['kind'] != 'rule' or node['line'] < min(declarations) or
                    not any(term['op'] == 'literal' and '$' in term['value'] for term in node['tail_expression'])):
                continue
            inputs, pieces = {}, []
            for term in node['tail_expression']:
                if term['op'] == 'literal':
                    pieces.append(term['value'])
                else:
                    name = '__kbuild_first_prerequisite_' + str(len(inputs))
                    inputs[name] = [term]
                    pieces.append('$(' + name + ')')
            node['secondary_inputs'] = inputs
            secondary.append((node, ''.join(pieces)))
    if secondary:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'secondary-prerequisites.mk'
            source.write_text(''.join(f'x{i} = {text}\n' for i, (_, text) in enumerate(secondary)))
            expressions = {node['name']: node['value_expression'] for node in native_events(a.make, [source], expressions=True)
                           if node['kind'] == 'assignment'}
        for i, (node, _) in enumerate(secondary):
            node['secondary_expression'] = expressions['x' + str(i)]
    bodies = {relative(f['file'], linux): lower_execution_paths(lower_compiler_queries(lower_text_transforms(backend.lower_probes(lower_body(f['body'], a.make))))) for f in tree['files']}
    program_file = out / 'rules/flag_program.bzl'
    import ast
    raw = program_file.read_text()
    data = json.loads(ast.literal_eval(raw[len('FLAG_PROGRAM = json.decode('):-2]))
    # Kbuild moved the directory graph transformations from Makefile.lib into
    # Makefile.build. Evaluate their native phase once, including helper inputs
    # such as the older pre-prefix multi-used snapshot used by modname-multi.
    build = bodies['scripts/Makefile.build']
    flat = [n for n, _ in walk(build)]
    graph_roots = {'obj-m', 'real-obj-y', 'real-obj-m', 'lib-y', 'always-y', 'extra-y',
                   'subdir-ym', 'subdir-builtin', 'subdir-modorder',
                   'multi-obj-y', 'multi-obj-m', 'multi-obj-ym'}
    owners = [(path, n['line']) for path in ('scripts/Makefile.lib', 'scripts/Makefile.build')
              for n, _ in walk(bodies[path])
              if n.get('name') == 'obj-m' and 'filter-out' in n.get('value', '') and
              'obj-y' in backend.refs(n['value_expression'])]
    match owners:
        case [(owner, low)]:
            pass
        case _:
            raise ValueError(f'Expected one Kbuild object graph owner, got {owners}')
    high = max(n['line'] for n, _ in walk(bodies[owner]) if n.get('name') in graph_roots)
    graph_phase = backend.bounded(bodies[owner], low, high)
    if owner != 'scripts/Makefile.build':
        includes = [n['line'] for n in flat if n['kind'] == 'directive' and
                    n.get('arguments', '').removeprefix('$(srctree)/') == owner]
        match includes:
            case [include]:
                pass
            case _:
                raise ValueError(f'Expected one native include of {owner}, got {includes}')
        tail = next(n['line'] for n in flat if n.get('name') == 'subdir-modorder')
        graph_phase += backend.bounded(build, include + 1, tail)
    graph = backend.slice_program(graph_phase, graph_roots | backend.refs(data['helpers']))
    graph_nodes = {(n['file'], n['line']) for n, _ in walk(graph)
                   if n['kind'] in ('assignment', 'define')}
    # Standalone LLVM rules still evaluate the original helper program. Image
    # compilers receive the graph's immutable variable cells instead.
    data['image_helpers'] = without_assignments(data['helpers'], graph_nodes)
    data['graph_variables'] = sorted({backend.const(n['name_expression']) or
                                      n['name_expression'][0].get('value', '') + '*'
                                      for n, _ in walk(graph) if n['kind'] in ('assignment', 'define')})
    backend.selected = image_selected
    def flag_body(body):
        result = []
        for n in body:
            if n['kind'] == 'conditional':
                result.append(dict(n, branches=[dict(b, body=flag_body(b['body'])) for b in n['branches']]))
            elif n['kind'] not in ('rule', 'recipe'):
                result.append(n)
        return result
    data['image_flag_programs'] = {p: flag_body(body) for p, body in bodies.items()}
    data['directories'] = {p: body for p, body in data['image_flag_programs'].items() if p.rsplit('/', 1)[-1] in ('Makefile', 'Kbuild') and p != 'Makefile'}
    data['image_programs'] = bodies
    data['image_compile_commands'] = sorted({n['name'][4:] for body in bodies.values() for n, _ in walk(body) if n['kind'] in ('assignment', 'define') and n.get('name', '').startswith('cmd_') and backend.refs(n['value_expression']) & {'c_flags', 'a_flags'}})
    data['image_postlink_outputs'] = {p: {n['name'][4:]: output_redirections(n['value_expression']) for n, _ in walk(body) if n['kind'] in ('assignment', 'define') and n.get('name', '').startswith('cmd_')} for p, body in bodies.items() if p.endswith('/Makefile.postlink')}
    # Older sources keep DTB recipes in Makefile.lib, already included in the
    # native implicit-rule program. Only newer sources add a separate phase.
    data['image_dtbs'] = bodies.get('scripts/Makefile.dtbs', [])
    data['graph_helpers'] = graph
    data['image_arch'] = [{'kind': 'directive', 'directive': 'include', 'arguments_expression': [
        {'op': 'literal', 'value': 'arch/'}, {'op': 'reference', 'name': [{'op': 'literal', 'value': 'SRCARCH'}]}, {'op': 'literal', 'value': '/Makefile'}]}]
    top = bodies['Makefile']
    top_nodes = [n for n, _ in walk(top)]
    first = next(n['line'] for n in top_nodes if n.get('name') == 'core-y')
    last = next(n['line'] for n in top_nodes if n.get('name') == 'libs-y')
    data['image_roots_init'] = backend.bounded(top, first, last)
    root_names = {'ARCH_CORE', 'ARCH_LIB', 'ARCH_DRIVERS', 'KBUILD_LDS', 'KBUILD_VMLINUX_OBJS', 'KBUILD_VMLINUX_LIBS'}
    modern_roots = [n['line'] for n in top_nodes if n.get('name') == 'ARCH_CORE']
    data['image_root_descent'] = []
    if modern_roots:
        root_first = min(modern_roots)
    else:
        # The ordinary architecture include follows core/libs initialization;
        # the earlier config-only invocation includes the same file separately.
        includes = [n['line'] for n in top_nodes if n['kind'] == 'directive' and
                    n.get('arguments', '').removeprefix('$(srctree)/') == 'arch/$(SRCARCH)/Makefile' and n['line'] > last]
        match includes:
            case [include]:
                root_first = include + 1
            case _:
                raise ValueError(f'Expected one build architecture include, got {includes}')
        root_names |= {'build-dirs', 'subdir-modorder'}
        data['image_root_descent'] = target_rules(top, {'descend', '$(build-dirs)'})
    root_last = next(n['line'] for n in top_nodes if n.get('name') == 'KBUILD_LDS')
    data['image_roots'] = backend.slice_program(backend.bounded(top, root_first, root_last), root_names)
    root_nodes = {(n['file'], n['line']) for n, _ in walk(data['image_roots'])
                  if n['kind'] in ('assignment', 'define')}
    def export_directives(body):
        result = []
        for node in body:
            if node['kind'] == 'conditional':
                result.append(dict(node, branches=[dict(b, body=export_directives(b['body'])) for b in node['branches']]))
            elif node['kind'] == 'directive' and node.get('directive') in ('export', 'unexport'):
                result.append(node)
            elif node['kind'] in ('assignment', 'define') and node.get('export') in (1, 2):
                result.append({'kind': 'directive', 'directive': 'export' if node['export'] == 1 else 'unexport', 'arguments': node['name'], 'arguments_expression': node['name_expression']})
        return result
    data['image_exports'] = export_directives(top)
    version_names = {'VERSION', 'PATCHLEVEL', 'SUBLEVEL', 'EXTRAVERSION'}
    versions = [(node, guards) for node, guards in walk(top)
                if node['kind'] in ('assignment', 'define') and node.get('name') in version_names]
    if (len(versions) != len(version_names) or {node['name'] for node, _ in versions} != version_names or
            any(guards or node['kind'] != 'assignment' or backend.const(node['value_expression']) is None
                for node, guards in versions)):
        raise ValueError(f'Expected one unguarded literal assignment per kernel version field: {versions}')
    # Export directives belong to the native invocation, not this version
    # phase. Their dependency closure would replay top-level orchestration.
    data['image_version'] = [node for node, _ in versions]
    first = next(n['line'] for n in flat if n.get('name') == 'obj-y')
    # Makefile.build owns whether src is a logical directory found via VPATH
    # or an explicitly rooted source path. Preserve that distinction for
    # generated files which share its implicit source patterns.
    data['image_build_init'] = backend.slice_program(backend.bounded(build, 0, first - 1), {'src'})
    match [n for n, _ in walk(data['image_build_init']) if n.get('name') == 'src']:
        case [_]:
            pass
        case assignments:
            raise ValueError(f'Expected one native Makefile.build src initialization, got {assignments}')
    # Kbuild resets directory variables before reading configuration and local
    # assignments. The reset inventory changes between kernel versions.
    first_include = next(n['line'] for n in flat if n['line'] > first and
                         n['kind'] == 'directive' and n['directive'] in ('include', '-include', 'sinclude'))
    data['graph_init'] = backend.bounded(build, first, first_include - 1)
    data['image_archives'] = {
        'builtin': next(n['value_expression'] for n, _ in walk(bodies['scripts/Makefile.build']) if n.get('name') == 'cmd_ar_builtin'),
        'library': next(n['value_expression'] for n, _ in walk(bodies['scripts/Makefile.lib']) if n.get('name') == 'cmd_ar'),
    }
    header_last = max(n['line'] for n in top_nodes if n.get('name') in
                      ('filechk_version.h', 'filechk_utsrelease.h', 'filechk_compile.h'))
    data['image_header_commands'] = {name.removeprefix('filechk_'): '.' for n in top_nodes
                                     if (name := n.get('name', '')) in
                                     ('filechk_version.h', 'filechk_utsrelease.h', 'filechk_compile.h')}
    data['image_command_helpers'] = without_assignments(backend.slice_program(
        bodies['scripts/Kbuild.include'] + bodies['scripts/Makefile.lib'] + bodies['scripts/Makefile.host'] + bodies['scripts/Makefile.build'] + backend.selected(backend.bounded(top, next(n['line'] for n in top_nodes if n.get('name') == 'uts_len'), header_last), {'filechk_*', 'uts_len'}),
        {'cmd_*', 'filechk_*'}), graph_nodes)
    data['image_compile_postprocess'] = {}
    compile_nodes = [node for node, _ in walk(bodies['scripts/Makefile.lib'] + build)]
    for node in compile_nodes:
        if node.get('name') not in ('rule_cc_o_c', 'rule_as_o_S'):
            continue
        calls = []
        for term in node['value_expression']:
            if term['op'] == 'literal' and not term['value'].strip():
                continue
            if term.get('op') != 'function' or term.get('name') != 'call' or len(term['arguments']) != 2:
                raise ValueError(f'Unsupported native compiler rule: {node}')
            calls.append(tuple(backend.const(argument) for argument in term['arguments']))
        command = node['name'].removeprefix('rule_')
        if calls[:1] != [('cmd_and_fixdep', command)] or any(function != 'cmd' or name is None for function, name in calls[1:]):
            raise ValueError(f'Unsupported native compiler stages: {calls}')
        data['image_compile_postprocess'][command] = [name for _, name in calls[1:]]
    # Split only command references following the compiler's source operand.
    # The source keeps their order, including older cmd_ld_single_m spelling.
    data['image_compile_tails'] = {}
    for node in compile_nodes:
        if node.get('name') not in {'cmd_' + name for name in data['image_compile_postprocess']}:
            continue
        terms = node['value_expression']
        source_operand = [i for i, term in enumerate(terms)
                          if term == {'op': 'reference', 'name': backend.literal('<')}]
        if len(source_operand) != 1:
            raise ValueError(f'Expected one compiler source operand: {node}')
        tails = []
        for term in terms[source_operand[0] + 1:]:
            if term['op'] == 'literal' and not term['value'].strip():
                continue
            name = backend.const(term['name']) if term['op'] == 'reference' else None
            if name is None or not name.startswith('cmd_'):
                raise ValueError(f'Unsupported compiler command suffix: {node}')
            tails.append(name)
        data['image_compile_tails'][node['name'][4:]] = tails
    # A directly invoked source script needs its declared interpreter, not a
    # host shebang or the entire prepared source tree in every object action.
    data['image_compile_scripts'] = {}
    post_commands = {'cmd_' + name for names in data['image_compile_postprocess'].values() for name in names}
    for node in compile_nodes:
        if node.get('name') not in post_commands:
            continue
        script = re.match(r'^\$\(srctree\)/([\w./-]+)(?:\s|$)', make_text(node['value_expression']).strip())
        if script is None:
            continue
        path = script[1]
        if '..' in Path(path).parts:
            raise ValueError(f'Compiler postprocessor escapes the source tree: {node}')
        with (linux / path).open('rb') as stream:
            interpreter, arguments = script_interpreter(stream.readline(512))
        data['image_compile_scripts'][node['name']] = {'path': path, 'interpreter': interpreter, 'arguments': arguments}
    data['image_recordmcount'] = None
    for node in flat:
        if node.get('name') != 'sub_cmd_record_mcount' or '$(objtree)' not in node.get('value', ''):
            continue
        shape = re.fullmatch(r'\s*if\s+\[\s+\$\(@\)\s+!=\s+"([\w./-]+)"\s+\];\s+then\s+'
                             r'\$\(objtree\)/([\w./-]+)\s+\$\(RECORDMCOUNT_FLAGS\)\s+"\$\(@\)";\s+fi;\s*',
                             make_text(node['value_expression']))
        if shape is None or data['image_recordmcount'] is not None:
            raise ValueError(f'Unsupported native recordmcount invocation: {node}')
        excluded, tool = shape.groups()
        if '..' in Path(tool).parts or '..' in Path(excluded).parts:
            raise ValueError(f'Native recordmcount path escapes the output tree: {node}')
        data['image_recordmcount'] = {'excluded': excluded, 'tool': tool,
                                    'expression': node['value_expression'],
                                    'program': backend.slice_program(build, {'sub_cmd_record_mcount'})}
    data['image_timestamp_header'] = bool(target_rules(bodies['init/Makefile'], {'include/generated/utsversion.h'}))
    data['image_utilities'] = [n for n in top_nodes if n.get('name') in {'AWK', 'PERL', 'PYTHON3', 'BASH', 'KGZIP', 'KBZIP2', 'KLZMA', 'LZOP', 'LZ4', 'XZ', 'ZSTD', 'LEX', 'YACC', 'CONFIG_SHELL'}]
    def host_fallback(value):
        if isinstance(value, list):
            return [host_fallback(v) for v in value]
        if isinstance(value, dict):
            if value.get('op') == 'function' and value.get('name') == 'shell':
                if 'libelf --cflags' in make_text(value['arguments'][0]):
                    return {'op': 'literal', 'value': ''}  # Supplied by the CcInfo dependency.
                if 'grep elf_getshdr' in make_text(value['arguments'][0]):
                    return {'op': 'function', 'name': 'kbuild_libelf_api', 'arguments': []}
                return {'op': 'function', 'name': 'kbuild_host_fallback', 'arguments': [[{'op': 'literal', 'value': make_text(value['arguments'][0])}]]}
            return {k: host_fallback(v) for k, v in value.items()}
        return value
    objtool = bodies['tools/objtool/Makefile']
    subcmd_calls = [n['value_expression'] for n, _ in walk(target_rules(objtool, {'$(LIBSUBCMD)'}))
                    if n['kind'] == 'recipe']
    match subcmd_calls:
        case [call]:
            data['image_host_subcmd_call'] = call
        case _:
            raise ValueError(f'Expected one native objtool subcmd invocation, got {subcmd_calls}')
    build_calls = [n['value_expression'] for n, _ in walk(target_rules(objtool, {'$(OBJTOOL_IN)'}))
                   if n['kind'] == 'recipe' and 'MAKE' in backend.refs(n['value_expression'])]
    match build_calls:
        case [build_call]:
            data['image_host_objtool_call'] = build_call
        case _:
            raise ValueError(f'Expected one native objtool build invocation, got {build_calls}')
    host_setup = bodies['tools/scripts/Makefile.include'] + objtool + bodies['tools/build/Makefile.include']
    data['image_host_build'] = backend.slice_program(bodies['tools/objtool/Build'], {'objtool-y', 'CFLAGS', 'CFLAGS_*'}) + [n for n in bodies['tools/objtool/Build'] if n['kind'] == 'rule']
    child_inputs = backend.refs(data['image_host_build']) | backend.refs([
        body for path, body in bodies.items() if path.startswith('tools/objtool/arch/') and path.endswith('/Build')])
    data['image_host_objtool'] = host_fallback(backend.slice_program(host_setup, {'CFLAGS', 'LIBSUBCMD', 'OBJTOOL_IN'} | backend.refs(call) | backend.refs(build_call) | child_inputs))
    data['image_host_arch'] = bodies
    subcmd = bodies['tools/scripts/Makefile.include'] + bodies['tools/lib/subcmd/Makefile'] + bodies['tools/lib/subcmd/Build']
    data['image_host_subcmd'] = host_fallback(backend.slice_program(subcmd, {'libsubcmd-y', 'CFLAGS'}))
    data['image_host_subcmd_headers'] = backend.slice_program(subcmd, {'INSTALL_HDRS'})
    data['image_host_btfids'] = host_fallback(backend.slice_program(bodies['tools/bpf/resolve_btfids/Makefile'], {'HOSTCFLAGS_resolve_btfids', 'HOST_OVERRIDES'}))
    data['image_host_btfids_build'] = bodies['tools/bpf/resolve_btfids/Build']
    data['image_host_cflags'] = backend.slice_program(bodies['tools/build/Build.include'], {'c_flags', 'host_c_flags'})
    data['image_btf'] = bodies.get('scripts/Makefile.btf', [])
    data['image_btf_shell'] = {}
    if 'scripts/Makefile.btf' not in bodies:
        # Older sources calculate these flags with the selected pahole at
        # execution time. Keep that native script as the policy owner.
        assignments = [n['value_expression'] for n in top_nodes if n.get('name') == 'PAHOLE_FLAGS']
        match assignments:
            case [[{'op': 'function', 'name': 'shell', 'arguments': [command]}]]:
                data['image_btf_shell']['PAHOLE_FLAGS'] = command
            case _:
                raise ValueError(f'Unsupported native pahole flag computation: {assignments}')
    _INCLUDE_DIRECTIVES = True
    data['image_link_global'] = backend.slice_program(backend.bounded(top, min(n['line'] for n in top_nodes if n.get('name') == 'LDFLAGS_vmlinux'), max(n['line'] for n in top_nodes if n.get('name') == 'LDFLAGS_vmlinux')), {'LDFLAGS_vmlinux'})
    data['image_link_programs'] = {p: backend.slice_program(body, {'LDFLAGS_vmlinux'}) for p, body in bodies.items()}
    def export_names(body):
        names = set()
        for node, _ in walk(body):
            if node['kind'] == 'directive' and node.get('directive') == 'export':
                text = backend.const(node.get('arguments_expression', backend.literal(node['arguments'])))
                if text is not None:
                    names.update(text.split())
            elif node['kind'] in ('assignment', 'define') and node.get('export') == 1:
                name = backend.const(node['name_expression'])
                if name is not None:
                    names.add(name)
        return names
    backend.ROOTS |= {'core-y', 'libs-y', 'drivers-y', 'drivers-m', 'RESOLVE_BTFIDS'}
    if data['image_recordmcount'] is not None:
        # The native compiler phase inherits its selected implementation from
        # top-level exports. Preserve that selector in the original setup.
        backend.ROOTS |= backend.refs(data['image_recordmcount']['program']) & export_names(top)
    global_program, _, helpers, _ = backend.flags_program(tree, linux, defer_arch=True)
    data['global'] = without_assignments(lower_compiler_queries(lower_text_transforms(backend.lower_probes(global_program))), root_nodes)
    setup_roots = backend.ROOTS | backend.refs(data["image_command_helpers"]) | backend.refs(global_program) | backend.refs(helpers) | {'BITS', 'UTS_MACHINE', 'REALMODE_CFLAGS', 'CLANG_FLAGS', 'libs-y', 'core-y', 'drivers-y', 'LDFLAGS_vmlinux'}
    def slice_includes(roots):
        # An included file may define a variable referenced by its caller.
        # Compute the closure across files before pruning individual bodies.
        wanted = set(roots) | set().union(*(export_names(body) for body in bodies.values()))
        for _ in range(100):
            selected = {p: backend.selected(body, wanted) for p, body in bodies.items()}
            extra = {name for body in selected.values() for name in backend.refs(body)
                     if not name.startswith('CONFIG_')} - wanted
            if not extra:
                return selected
            wanted.update(extra)
        raise ValueError('Cross-file Make dependency slice did not converge')
    data['image_setup_programs'] = {path: without_assignments(body, root_nodes)
                                    for path, body in slice_includes(setup_roots).items()}
    data['image_graph_programs'] = slice_includes({'libs-y', 'core-y', 'drivers-y', 'BITS', 'UTS_MACHINE', 'REALMODE_CFLAGS'})
    _INCLUDE_HEADER_RULES = True
    header_dispatch = [make_text(n['value_expression']).split() for n in top_nodes
                       if n.get('name') == 'asm-generic']
    match header_dispatch:
        case [['-f', header_makefile, 'obj']] if header_makefile.startswith('$(srctree)/'):
            header_program = bodies[header_makefile.removeprefix('$(srctree)/')]
        case _:
            raise ValueError(f'Unsupported native asm-generic dispatch: {header_dispatch}')
    header_body, active = [], False
    for node in header_program:
        if node['kind'] == 'rule':
            active = '%.h' in node['targets']
        if node['kind'] not in ('rule', 'recipe') or active:
            header_body.append(node)
    data['image_asm_headers'] = backend.slice_program(header_body, {'generic-y', 'syscall-y', 'generated-y', 'cmd_wrap', 'cmd_syshdr', 'cmd_systbl'})
    _INCLUDE_HEADER_RULES = False
    _INCLUDE_DIRECTIVES = False
    data['image_link'] = {}
    data['image_script_link'] = 'scripts/Makefile.vmlinux' not in bodies
    archive = bodies.get('scripts/Makefile.vmlinux_a')
    archive_owner = archive if archive is not None else top
    if archive is None:
        # Older sources own this action in the top Makefile. Its compiler and
        # architecture setup has already run; retain only the archive phase.
        starts = [n['line'] for n in top_nodes if n.get('name') == 'cmd_ar_vmlinux.a']
        if starts:
            last = max(n['line'] for n, _ in walk(target_rules(top, {'vmlinux.a'}))
                       if n['kind'] in ('rule', 'recipe'))
            archive = backend.bounded(top, min(starts), last)
        else:
            archive = []
    rules = target_rules(archive, {'built-in-fixup.a', 'vmlinux.a'})
    data['image_archive_controls'] = archive_controls(archive_owner, rules)
    data['image_archive'] = backend.slice_program(
        bodies['scripts/Makefile.lib'] + archive,
        {'cmd_ar_builtin_fixup', 'cmd_ar_vmlinux.a', 'cmd_copy'} | backend.refs(rules)) + rules
    if data['image_script_link']:
        link_rules = target_rules(top, {'vmlinux'})
        first = next(n['line'] for n in top_nodes if n.get('name') == 'vmlinux-deps')
        last = max(n['line'] for n, _ in walk(link_rules))
        last = next(n['line'] for n in top_nodes if n.get('name') == 'targets' and n['line'] > last)
        link_phase = backend.bounded(top, first, last)
        data['image_final_command'] = 'link-vmlinux'
        data['image_link']['vmlinux'] = backend.slice_program(link_phase, {'cmd_link-vmlinux', 'targets'} | backend.refs(link_rules))
        data['image_link_context'] = data['image_link']['vmlinux'] + link_rules
        data['image_archive_controls'] += archive_controls(top, link_rules)
    else:
        data['image_final_command'] = 'link_vmlinux'
        data['image_link_context'] = bodies['scripts/Makefile.vmlinux']
        for driver, roots in {'vmlinux_o': {'cmd_ld_vmlinux.o'}, 'vmlinux': {'cmd_link_vmlinux', 'cmd_strip_relocs', 'LDFLAGS_vmlinux'}}.items():
            data['image_link'][driver] = backend.slice_program(bodies['scripts/Makefile.lib'] + bodies['scripts/Makefile.' + driver], roots)
    modpost = bodies['scripts/Makefile.modpost']
    modpost_rules = target_rules(modpost, {'__modpost', 'vmlinux.symvers', 'Module.symvers', '$(output-symdump)'}) if data['image_script_link'] else []
    data['image_link']['modpost'] = backend.slice_program(modpost, {'cmd_modpost', 'cmd_cat', 'modpost-deps', 'output-symdump', 'missing-input'} | backend.refs(modpost_rules)) + modpost_rules
    modfinal = bodies['scripts/Makefile.modfinal']
    modfinal_rules = target_rules(modfinal, {'%.mod.o', '.module-common.o', '$(extmod_prefix).module-common.o', '%.ko'})
    data['image_modfinal'] = backend.slice_program(
        bodies['scripts/Makefile.lib'] + modfinal,
        {'cmd_cc_o_c', 'cmd_ld_single', 'cmd_objtool', 'objtool-enabled', 'cmd_ld_ko_o', 'cmd_btf_ko',
         'modname', 'part-of-module', 'GCOV_PROFILE', 'ccflags-remove-y'} | backend.refs(modfinal_rules))
    data['image_modfinal'].extend(modfinal_rules)
    single_modules = target_rules(top, {'single_modules', 'single_modpost'})
    order_recipes = [n['value_expression'] for n, _ in walk(single_modules)
                     if n['kind'] == 'recipe' and any(backend.const(expr) == 'modules.order' or
                                                     expr == [{'op': 'reference', 'name': backend.literal('MODORDER')}]
                                                     for expr in output_redirections(n['value_expression']))]
    if len(order_recipes) != 1:
        raise ValueError(f'Expected one native selected-module order recipe, got {order_recipes}')
    data['image_modules_order'] = order_recipes
    # The selected-module caller builds these targets before writing its list.
    # Older modpost reads the listed objects without declaring them itself.
    order_refs = backend.refs([n['tail_expression'] for n, _ in walk(single_modules)
                               if n['kind'] == 'rule'])
    data['image_module_order_prerequisites'] = [n['value_expression'] for n in top_nodes
        if n.get('name') in order_refs and 'MAKECMDGOALS' in backend.refs(n.get('value_expression', []))]
    if data['image_script_link'] and len(data['image_module_order_prerequisites']) != 1:
        raise ValueError('Expected one native selected-module prerequisite expression')
    data['image_module_order_contents'] = None
    if 'modules' in backend.refs(modfinal_rules):
        data['image_modfinal'] = copy.deepcopy(data['image_modfinal'])
        data['image_module_order_contents'] = module_order_input(data['image_modfinal'], order_recipes[0])
    if data['image_script_link'] and 'modules' in backend.refs(modpost_rules):
        data['image_link']['modpost'] = copy.deepcopy(data['image_link']['modpost'])
        contents = module_order_input(data['image_link']['modpost'], order_recipes[0])
        if data['image_module_order_contents'] not in (None, contents):
            raise ValueError('Native modpost and modfinal read different selected module lists')
        data['image_module_order_contents'] = contents
    data['image_module_variables'] = [n for n in top_nodes if n.get('name') == 'MODORDER']
    # Keep the source's link implementation. Replace only recursive Make
    # boundaries whose producers are represented by the configured graph.
    link_script = (linux / 'scripts/link-vmlinux.sh').read_text()
    init_goal = 'need-builtin=1' if data['image_script_link'] else 'init/version-timestamp.o'
    original = '${MAKE} -f "${srctree}/scripts/Makefile.build" obj=init ' + init_goal
    if link_script.count(original) != 1:
        raise ValueError('Unsupported link-vmlinux.sh: init Make boundary changed')
    link_script = link_script.replace(original, ': # init inputs are declared Bazel prerequisites')
    if data['image_script_link']:
        original = '${MAKE} -f "${srctree}/scripts/Makefile.modpost" MODPOST_VMLINUX=1'
        if link_script.count(original) != 1:
            raise ValueError('Unsupported link-vmlinux.sh: vmlinux modpost boundary changed')
        link_script = link_script.replace(original, '__KBUILD_VMLINUX_MODPOST__')
    (out / 'rules/link-vmlinux.sh').write_text(link_script)
    exported = {name for n in top_nodes if n['kind'] == 'directive' and n.get('directive') == 'export' for name in n['arguments'].split()}
    referenced = set(re.findall(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)', link_script))
    data['image_common'] = bodies['scripts/Kbuild.include'] + bodies.get('scripts/Makefile.compiler', [])
    data['image_link_exports'] = sorted({left or right for left, right in referenced})
    # These helpers run after the local Kbuild file. Do not replay the earlier
    # directory-variable initialization and erase its ccflags/asflags.
    local_includes = [n['line'] for n, _ in walk(build) if n['kind'] == 'directive' and
                      n['directive'] in ('include', '-include', 'sinclude') and
                      backend.refs(n['arguments_expression']) & {'kbuild-file', 'kbuild-dir'}]
    match local_includes:
        case [local_include]:
            pass
        case _:
            raise ValueError(f'Expected one local Kbuild include, got {local_includes}')
    data['image_object_helpers'] = without_assignments(backend.slice_program(bodies['scripts/Makefile.lib'] + backend.bounded(build, local_include + 1, max(n['line'] for n, _ in walk(build))), {'cmd_objtool', 'objtool-enabled'}), graph_nodes)
    data['image_objtool_variables'] = sorted({n['name']
        for n, _ in walk(bodies['scripts/Makefile.lib'] + build)
        if n['kind'] == 'assignment' and n.get('value') == '$(objtree)/tools/objtool/objtool'})
    data['image_cpp'] = [n for n in top_nodes if n.get('name') == 'CPP']
    def implicit_rules(body):
        result = []
        active = False
        for n in body:
            if n['kind'] == 'conditional':
                branches = [dict(b, body=implicit_rules(b['body'])) for b in n['branches']]
                if any(b['body'] for b in branches):
                    result.append(dict(n, branches=branches))
            elif n['kind'] == 'rule':
                active = '%' in n['targets']
                if active:
                    result.append(dict(n, implicit=True))
            elif n['kind'] == 'recipe' and active:
                result.append(n)
            else:
                active = False
        return result
    rules = implicit_rules(bodies['scripts/Makefile.build']) + implicit_rules(bodies['scripts/Makefile.lib'])
    secondary_roots = backend.refs([node['secondary_expression'] for node, _ in walk(rules)
                                    if 'secondary_expression' in node])
    secondary_helpers = without_assignments(backend.slice_program(
        bodies['scripts/Makefile.lib'] + backend.bounded(build, local_include + 1, max(n['line'] for n, _ in walk(build))),
        secondary_roots), graph_nodes) if secondary_roots else []
    unsupported = backend.refs(secondary_helpers) & {'<', '^', '+', '?', '|'}
    if unsupported:
        raise ValueError(f'Secondary prerequisites require unsupported automatic variables: {unsupported}')
    data['image_implicit_rules'] = secondary_helpers + rules
    program_file.write_text('FLAG_PROGRAM = json.decode(' + repr(json.dumps(data)) + ')\n')
    shutil.copyfile(HERE / 'host_toolchain.bzl', out / 'rules/host_toolchain.bzl')
    with (out / 'BUILD.bazel').open('a') as stream:
        stream.write('load("//rules:image.bzl", "kbuild_resolved_core")\nkbuild_resolved_core(name="_kernel_build", tags=["manual"])\n')
        stream.write('load("//rules:host_toolchain.bzl","kernel_host_toolchain")\nkernel_host_toolchain(name="host_toolchain")\n')
        stream.write('load("//rules:host_tools.bzl","kernel_objtool","kernel_resolve_btfids")\nkernel_objtool(name="objtool")\nkernel_resolve_btfids(name="resolve_btfids")\n')
        stream.write('''load("//rules:pahole.bzl", "kernel_pahole", "kernel_pahole_test")
kernel_pahole(name="pahole", binary="@pahole//:pahole", exec_compatible_with=["@platforms//os:linux", "@platforms//cpu:x86_64"])
kernel_pahole_test(name="pahole_test", pahole=":pahole")
''')
    shutil.copyfile(HERE / 'text_transforms.bzl', out / 'rules/text_transforms.bzl')
    shutil.copyfile(HERE / 'multi_platform_test.py', out / 'rules/multi_platform_test.py')
    shutil.copyfile(HERE / 'multi_platform.bzl', out / 'rules/multi_platform.bzl')
    (out / 'rules/kernel_platform_data.bzl').write_text('''load("@llvm//platforms:common.bzl", "SUPPORTED_TARGETS")
load(":architecture_data.bzl", "CPU_ARCHITECTURES")
SUPPORTED_KERNEL_CPUS = sorted({cpu: True for os, cpu in SUPPORTED_TARGETS if os == "linux" and cpu in CPU_ARCHITECTURES}.keys())
KERNEL_CONSTRAINTS = []
''')
    shutil.copyfile(HERE / 'usermode.bzl', out / 'rules/usermode.bzl')
    shutil.copyfile(HERE / 'usermode_test.py', out / 'rules/usermode_test.py')
    (out / 'usermode').mkdir(exist_ok=True)
    (out / 'usermode/BUILD.bazel').write_text('load("//rules:usermode.bzl", "usermode_targets")\nusermode_targets(platform_parent="@llvm//platforms:linux_x86_64_musl", requested={"STATIC_LINK":"y"})\n')
    (out / 'kernel_platforms').mkdir(exist_ok=True)
    (out / 'kernel_platforms/BUILD.bazel').write_text('load("//rules:multi_platform.bzl", "kernel_platforms")\nkernel_platforms()\n')
    (out / 'multi_platform').mkdir(exist_ok=True)
    (out / 'multi_platform/BUILD.bazel').write_text('''load("//rules:rules.bzl", "kernel_config")
load("//rules:image.bzl", "kbuild_resolved_build", "kbuild_resolved_setup")
load("//rules:multi_platform.bzl", "kernel_multi_platform", "kernel_multi_platform_test")
kernel_config(name="config", options="//options:options", allnoconfig=True, visibility=["//visibility:public"])
kbuild_resolved_build(name="kernel", config=":config", catalog="//:catalog", roots=["vmlinux"], architecture_roots={"s390": ["arch/s390/boot/bzImage"]}, tags=["manual"])
kernel_multi_platform(name="configs", target=":config")
kbuild_resolved_setup(name="setup", config=":config")
kernel_multi_platform(name="setups", target=":setup")
kernel_multi_platform(name="kernels", target=":kernel")
kernel_multi_platform_test(name="kernels_test", kernels=":kernels", size="large", tags=["manual"])
''')
    shutil.copyfile(HERE / 'architecture_tests.bzl', out / 'rules/architecture_tests.bzl')
    (out / 'architecture_tests').mkdir(exist_ok=True)
    (out / 'architecture_tests/BUILD.bazel').write_text('load("//rules:architecture_tests.bzl","architecture_tests")\narchitecture_tests()\n')
    shutil.copyfile(HERE / 'image_boot_init.S', out / 'examples/boot_init.S')
    shutil.copyfile(HERE / 'image_boot_test.py', out / 'rules/boot_test.py')
    shutil.copyfile(HERE / 'image_boot_test.bzl', out / 'rules/boot_test.bzl')
    shutil.copyfile(HERE / 'image_host_tools.bzl', out / 'rules/host_tools.bzl')
    shutil.copyfile(HERE / 'image_resolved.bzl', out / 'rules/image.bzl')
    shutil.copyfile(HERE / 'image_commands.bzl', out / 'rules/commands.bzl')
    shutil.copyfile(HERE / 'image_toolchain.bzl', out / 'rules/toolchain.bzl')
    shutil.copyfile(HERE / 'image_materialize.py', out / 'rules/materialize.py')
    shutil.copyfile(HERE / 'native_recipe.py', out / 'rules/native_recipe.py')
    shutil.copyfile(HERE / 'image_expressions.bzl', out / 'rules/expressions.bzl')
    base = {n['symbol']: 'n' for body in syntax.values() for n in body if n['symbol'] and n['type'] in ('bool', 'boolean', 'tristate') and any(p['kind'] in ('prompt', 'menu') and p.get('text') for p in n['properties'])}
    requested = {'EXPERT': 'y', '64BIT': 'y', 'PRINTK': 'y', 'TTY': 'y', 'SERIAL_8250': 'y',
                 'SERIAL_8250_CONSOLE': 'y', 'BLK_DEV_INITRD': 'y', 'RD_GZIP': 'y', 'BINFMT_ELF': 'y',
                 'HYPERVISOR_GUEST': 'y', 'PVH': 'y', 'UNWINDER_FRAME_POINTER': 'y', 'CC_OPTIMIZE_FOR_SIZE': 'y'}
    (out / 'configs/BUILD.bazel').write_text('load("//rules:rules.bzl", "kernel_config")\npackage(default_visibility=["//visibility:public"])\nkernel_config(name="minimal", options="//options:options", strict=False, base=json.decode(' + repr(json.dumps(base)) + '), requested=' + repr(requested) + ')\n')
    module = out / 'MODULE.bazel'
    module.write_text(module.read_text() + '\nbazel_dep(name = \"elfutils\", version = \"0.195\")\nbazel_dep(name=\"bazel_skylib\",version=\"1.9.0\")\n')
    generate_execution_tools(out)
    generate_qemu_tools(out)
    generate_compiler_metadata(out)
    write_module(out)
    (out / 'examples/BUILD.bazel').write_text('load("//rules:image.bzl", "kbuild_resolved_build")\nkbuild_resolved_build(name="kernel", config="//configs:minimal", catalog="//:catalog", roots=["vmlinux"], required_options=' + repr(requested) + ')\n')
    with (out / 'examples/BUILD.bazel').open('a') as f:
        f.write('alias(name="objtool", actual="//:objtool")\n')
    with (out / 'examples/BUILD.bazel').open('a') as f:
        f.write('load("//rules:boot_test.bzl", "kernel_boot_test", "boot_init_binary")\n')
        f.write('boot_init_binary(name="boot_init", src="boot_init.S")\n')
        f.write('filegroup(name="kernel_elf", srcs=[":kernel"], output_group="kernel")\n')
        f.write('kernel_boot_test(name="boot_test", kernel=":kernel_elf", init=":boot_init", tags=["local", "external", "manual"], timeout="moderate")\n')
    print(out)


if __name__ == '__main__':
    main()
