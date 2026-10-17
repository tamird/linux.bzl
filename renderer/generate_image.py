#!/usr/bin/env python3
"""Whole-kernel extension of the native Make/Kconfig experiment."""
import argparse
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
            active = bool(set(names.split()) & targets)
            if active:
                result.append(node)
        elif node['kind'] == 'recipe' and active:
            result.append(node)
        else:
            active = False
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
    def without_graph(body):
        result = []
        for n in body:
            if n['kind'] == 'conditional':
                branches = [dict(b, body=without_graph(b['body'])) for b in n['branches']]
                if any(b['body'] for b in branches):
                    result.append(dict(n, branches=branches))
            elif n['kind'] not in ('assignment', 'define') or (n['file'], n['line']) not in graph_nodes:
                result.append(n)
        return result
    # Standalone LLVM rules still evaluate the original helper program. Image
    # compilers receive the graph's immutable variable cells instead.
    data['image_helpers'] = without_graph(data['helpers'])
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
    data['image_dtbs'] = bodies['scripts/Makefile.dtbs']
    data['graph_helpers'] = graph
    data['image_arch'] = [{'kind': 'directive', 'directive': 'include', 'arguments_expression': [
        {'op': 'literal', 'value': 'arch/'}, {'op': 'reference', 'name': [{'op': 'literal', 'value': 'SRCARCH'}]}, {'op': 'literal', 'value': '/Makefile'}]}]
    top = bodies['Makefile']
    top_nodes = [n for n, _ in walk(top)]
    first = next(n['line'] for n in top_nodes if n.get('name') == 'core-y')
    last = next(n['line'] for n in top_nodes if n.get('name') == 'libs-y')
    data['image_roots_init'] = backend.bounded(top, first, last)
    data['image_roots'] = backend.slice_program(backend.bounded(top, next(n['line'] for n in top_nodes if n.get('name') == 'ARCH_CORE'), next(n['line'] for n in top_nodes if n.get('name') == 'KBUILD_LDS')), {'ARCH_CORE', 'ARCH_LIB', 'ARCH_DRIVERS', 'KBUILD_LDS', 'KBUILD_VMLINUX_OBJS', 'KBUILD_VMLINUX_LIBS'})
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
    data['image_version'] = backend.slice_program(top, {'VERSION', 'PATCHLEVEL', 'SUBLEVEL', 'EXTRAVERSION'})
    first = next(n['line'] for n in flat if n.get('name') == 'obj-y')
    # Kbuild resets directory variables before reading configuration and local
    # assignments. The reset inventory changes between kernel versions.
    first_include = next(n['line'] for n in flat if n['line'] > first and
                         n['kind'] == 'directive' and n['directive'] in ('include', '-include', 'sinclude'))
    data['graph_init'] = backend.bounded(build, first, first_include - 1)
    data['image_archives'] = {
        'builtin': next(n['value_expression'] for n, _ in walk(bodies['scripts/Makefile.build']) if n.get('name') == 'cmd_ar_builtin'),
        'library': next(n['value_expression'] for n, _ in walk(bodies['scripts/Makefile.lib']) if n.get('name') == 'cmd_ar'),
    }
    data['image_command_helpers'] = without_graph(backend.slice_program(
        bodies['scripts/Kbuild.include'] + bodies['scripts/Makefile.lib'] + bodies['scripts/Makefile.host'] + bodies['scripts/Makefile.build'] + backend.selected(backend.bounded(top, next(n['line'] for n in top_nodes if n.get('name') == 'uts_len'), next(n['line'] for n in top_nodes if n.get('name') == 'filechk_compile.h')), {'filechk_*', 'uts_len'}),
        {'cmd_*', 'filechk_*'}))
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
    data['image_host_objtool'] = host_fallback(backend.slice_program(bodies['tools/objtool/Makefile'], {'OBJTOOL_CFLAGS', 'BUILD_ORC', 'BUILD_KLP'}))
    data['image_host_build'] = backend.slice_program(bodies['tools/objtool/Build'], {'objtool-y'}) + [n for n in bodies['tools/objtool/Build'] if n['kind'] == 'rule']
    data['image_host_arch'] = bodies
    data['image_host_subcmd'] = host_fallback(backend.slice_program(bodies['tools/scripts/Makefile.include'] + bodies['tools/lib/subcmd/Makefile'] + bodies['tools/lib/subcmd/Build'], {'libsubcmd-y', 'CFLAGS'}))
    data['image_host_btfids'] = host_fallback(backend.slice_program(bodies['tools/bpf/resolve_btfids/Makefile'], {'HOSTCFLAGS_resolve_btfids', 'HOST_OVERRIDES'}))
    data['image_host_btfids_build'] = bodies['tools/bpf/resolve_btfids/Build']
    data['image_host_cflags'] = backend.slice_program(bodies['tools/build/Build.include'], {'c_flags', 'host_c_flags'})
    data['image_btf'] = bodies['scripts/Makefile.btf']
    _INCLUDE_DIRECTIVES = True
    data['image_link_global'] = backend.slice_program(backend.bounded(top, min(n['line'] for n in top_nodes if n.get('name') == 'LDFLAGS_vmlinux'), max(n['line'] for n in top_nodes if n.get('name') == 'LDFLAGS_vmlinux')), {'LDFLAGS_vmlinux'})
    data['image_link_programs'] = {p: backend.slice_program(body, {'LDFLAGS_vmlinux'}) for p, body in bodies.items()}
    backend.ROOTS |= {'core-y', 'libs-y', 'drivers-y', 'drivers-m', 'RESOLVE_BTFIDS'}
    global_program, _, helpers, _ = backend.flags_program(tree, linux, defer_arch=True)
    data['global'] = lower_compiler_queries(lower_text_transforms(backend.lower_probes(global_program)))
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
    data['image_setup_programs'] = slice_includes(setup_roots)
    data['image_graph_programs'] = slice_includes({'libs-y', 'core-y', 'drivers-y', 'BITS', 'UTS_MACHINE', 'REALMODE_CFLAGS'})
    _INCLUDE_HEADER_RULES = True
    header_body, active = [], False
    for node in bodies['scripts/Makefile.asm-headers']:
        if node['kind'] == 'rule':
            active = '%.h' in node['targets']
        if node['kind'] not in ('rule', 'recipe') or active:
            header_body.append(node)
    data['image_asm_headers'] = backend.slice_program(header_body, {'generic-y', 'syscall-y', 'generated-y', 'filechk_wrap', 'cmd_syshdr', 'cmd_systbl'})
    _INCLUDE_HEADER_RULES = False
    _INCLUDE_DIRECTIVES = False
    data['image_link'] = {}
    archive = bodies.get('scripts/Makefile.vmlinux_a')
    if archive is None:
        # Older sources own this action in the top Makefile. Its compiler and
        # architecture setup has already run; retain only the archive phase.
        first = next(n['line'] for n in top_nodes if n.get('name') == 'cmd_ar_vmlinux.a')
        last = max(n['line'] for n, _ in walk(target_rules(top, {'vmlinux.a'}))
                   if n['kind'] in ('rule', 'recipe'))
        archive = backend.bounded(top, first, last)
    rules = target_rules(archive, {'built-in-fixup.a', 'vmlinux.a'})
    data['image_archive'] = backend.slice_program(
        bodies['scripts/Makefile.lib'] + archive,
        {'cmd_ar_builtin_fixup', 'cmd_ar_vmlinux.a', 'cmd_copy'} | backend.refs(rules)) + rules
    for driver, roots in {'vmlinux_o': {'cmd_ld_vmlinux.o'}, 'vmlinux': {'cmd_link_vmlinux', 'cmd_strip_relocs', 'LDFLAGS_vmlinux'}}.items():
        data['image_link'][driver] = backend.slice_program(bodies['scripts/Makefile.lib'] + bodies['scripts/Makefile.' + driver], roots)
    modpost = bodies['scripts/Makefile.modpost']
    data['image_link']['modpost'] = backend.slice_program(modpost, {'cmd_modpost', 'modpost-deps', 'output-symdump', 'missing-input'})
    modfinal = bodies['scripts/Makefile.modfinal']
    data['image_modfinal'] = backend.slice_program(
        bodies['scripts/Makefile.lib'] + modfinal,
        {'cmd_cc_o_c', 'cmd_ld_single', 'cmd_objtool', 'objtool-enabled', 'cmd_ld_ko_o', 'cmd_btf_ko',
         'modname', 'part-of-module', 'GCOV_PROFILE', 'ccflags-remove-y'})
    data['image_modfinal'].extend(target_rules(modfinal, {'%.mod.o', '.module-common.o', '$(extmod_prefix).module-common.o', '%.ko'}))
    single_modules = target_rules(top, {'single_modules'})
    order_recipes = [n['value_expression'] for n, _ in walk(single_modules)
                     if n['kind'] == 'recipe' and any(backend.const(expr) == 'modules.order' or
                                                     expr == [{'op': 'reference', 'name': backend.literal('MODORDER')}]
                                                     for expr in output_redirections(n['value_expression']))]
    if len(order_recipes) != 1:
        raise ValueError(f'Expected one native selected-module order recipe, got {order_recipes}')
    data['image_modules_order'] = order_recipes
    data['image_module_variables'] = [n for n in top_nodes if n.get('name') == 'MODORDER']
    # The timestamp object is a separate Bazel action. Remove just its recursive
    # Make dispatch; the native script still performs the native final link.
    link_script = (linux / 'scripts/link-vmlinux.sh').read_text()
    original = '${MAKE} -f "${srctree}/scripts/Makefile.build" obj=init init/version-timestamp.o'
    if link_script.count(original) != 1:
        raise ValueError('Unsupported link-vmlinux.sh: timestamp Make boundary changed')
    (out / 'rules/link-vmlinux.sh').write_text(link_script.replace(
        original, ': # init/version-timestamp.o is a declared Bazel prerequisite'))
    link_script = (out / 'rules/link-vmlinux.sh').read_text()
    exported = {name for n in top_nodes if n['kind'] == 'directive' and n.get('directive') == 'export' for name in n['arguments'].split()}
    referenced = set(re.findall(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)', link_script))
    data['image_common'] = bodies['scripts/Kbuild.include'] + bodies['scripts/Makefile.compiler']
    data['image_link_exports'] = sorted({left or right for left, right in referenced})
    # These helpers run after the local Kbuild file. Do not replay the earlier
    # directory-variable initialization and erase its ccflags/asflags.
    local_include = next(n['line'] for n, _ in walk(build) if n['kind'] == 'directive' and n.get('arguments') == '$(kbuild-file)')
    data['image_object_helpers'] = without_graph(backend.slice_program(bodies['scripts/Makefile.lib'] + backend.bounded(build, local_include + 1, max(n['line'] for n, _ in walk(build))), {'cmd_objtool', 'objtool-enabled'}))
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
    data['image_implicit_rules'] = implicit_rules(bodies['scripts/Makefile.build']) + implicit_rules(bodies['scripts/Makefile.lib'])
    program_file.write_text('FLAG_PROGRAM = json.decode(' + repr(json.dumps(data)) + ')\n')
    shutil.copyfile(HERE / 'host_toolchain.bzl', out / 'rules/host_toolchain.bzl')
    with (out / 'BUILD.bazel').open('a') as stream:
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
