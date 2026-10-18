from pathlib import Path
import argparse
import hashlib
import json
import re
from compiler_capabilities import native_capabilities
HERE = Path(__file__).resolve().parent

def source_block(text: str, start: str, end: str, name: str) -> str:
    _, opening, rest = text.partition(start)
    body, closing, _ = rest.partition(end)
    if not opening or not closing:
        raise ValueError(f'Expected native {name}')
    return body

def prepare_sources(linux, output):
    output.mkdir(parents=True, exist_ok=True)
    src = linux / 'scripts/kconfig'
    parser = (src / 'parser.y').read_text()
    comment = source_block(parser, 'comment: T_COMMENT T_WORD_QUOTE T_EOL\n{', '\n};', 'comment grammar production')
    match re.findall(r'\t(menu_add_entry\([^;\n]*\);)', comment):
        case [comment_entry]:
            pass
        case entries:
            raise ValueError(f'Expected one native comment entry call, got {entries}')
    source_hook = 'zconf_nextfile($2);'
    assert parser.count(source_hook) == 1
    parser = parser.replace(source_hook, comment_entry + ' current_entry->help = xstrdup($2);')
    match re.findall(r'\tmenu_finalize\([^;\n]*\);', parser):
        case [finalize]:
            pass
        case calls:
            raise ValueError(f'Expected one native menu finalization call, got {calls}')
    parser = parser.replace(finalize, '\treturn; /* Link and finalize the syntax in Bazel analysis. */')
    parser = parser.replace('\tyyparse();', '\t/* Preprocessor definitions shared by the kernel Kconfig files. */\n\tif (strcmp(name, "scripts/Kconfig.include")) {\n\t\tzconf_initscan("scripts/Kconfig.include");\n\t\tyyparse();\n\t\tzconf_initscan(name);\n\t}\n\tyyparse();', 1)
    (output / 'parser.y').write_text(parser)
    # Older grammars include menu.c directly because some parser state is
    # private to that translation unit. Preserve the native ownership.
    (output / 'menu_translation.c').write_text('' if '#include "menu.c"' in parser else '#include "menu.c"\n')
    lexer = (src / 'lexer.l').read_text()
    # Each process parses Kconfig.include and then one file. Reset Flex's EOF
    # buffer and token history when opening the second file. file_lookup's
    # signature changed after 6.18; the scanner entry point owns this state.
    scanner = 'void zconf_initscan(const char *name)\n{'
    assert lexer.count(scanner) == 1, 'Expected one zconf_initscan definition'
    before, scanner_body = lexer.split(scanner)
    scanner_body, after = scanner_body.split('\n}', 1)
    reset = '\tyylineno = 1;'
    assert scanner_body.count(reset) == 1, 'Expected one scanner line reset'
    if 'const char *cur_filename;' in lexer:
        position_reset = '\tcur_lineno = 1;'
        filename = 'cur_filename'
    elif 'current_pos.file = current_file;' in lexer:
        position_reset = '\tcurrent_pos.file = current_file;\n\tcurrent_pos.lineno = 1;'
        filename = 'zconf_curname()'
    else:
        raise ValueError('Unrecognized native scanner source-location state')
    scanner_body = scanner_body.replace(reset, reset + '\n\tyyrestart(yyin);\n\tBEGIN(INITIAL);\n\tprev_token = prev_prev_token = T_EOL;\n' + position_reset)
    lexer = before + scanner + scanner_body + '\n}' + after
    (output / 'lexer.l').write_text(lexer)
    preprocess = (src / 'preprocess.c').read_text()
    marker = 'static LIST_HEAD(env_list);'
    assert preprocess.count(marker) == 1
    fallback = (HERE / 'kconfig_fallback.inc').read_text().replace('cur_filename', filename)
    preprocess = preprocess.replace(marker, fallback.replace('/* PROTOTYPE_COMPILER_CAPABILITIES */', native_capabilities()) + '\n' + marker)
    start = preprocess.index('static char *variable_expand(')
    before, after = preprocess[:start], preprocess[start:]
    after = after.replace('\tv = variable_lookup(name);', '\tres = prototype_probe_fallback(name, argc, argv);\n\tif (res) return res;\n\tv = variable_lookup(name);', 1)
    preprocess = (before + after).replace('\tcmd = argv[0];', '\treturn prototype_shell_fallback(argc, argv);\n\tcmd = argv[0];', 1)
    (output / 'preprocess.c').write_text(preprocess)
    confdata = (src / 'confdata.c').read_text()
    autoconf = source_block(confdata, 'int conf_write_autoconf(', '\n}\n', 'auto.conf writer')
    modern = re.findall(r'__conf_write_autoconf\(\s*conf_get_autoconfig_name\(\),\s*print_symbol_for_autoconf,\s*&comment_style_pound\s*\)', autoconf)
    legacy = re.findall(r'conf_write_symbol\(\s*out,\s*sym,\s*&kconfig_printer_cb,\s*\(void\s*\*\)\s*1\s*\)', autoconf)
    match modern, legacy:
        case [_], []:
            printer = 'print_symbol_for_autoconf(stdout, &sym);'
        case [], [_]:
            printer = 'conf_write_symbol(stdout, &sym, &kconfig_printer_cb, (void *)1);'
        case _:
            raise ValueError('Unrecognized native auto.conf symbol printer')
    # Keep escaping and quoting in the compiled native serializer. Its string
    # printer reads curr.val without requiring configuration resolution.
    confdata += '''
void prototype_auto_conf_string(const char *value)
{
    struct symbol sym = { .name = "VALUE", .type = S_STRING,
                          .curr = { .val = (void *)value } };
    ''' + printer + '''
}
'''
    (output / 'confdata.c').write_text(confdata)
    dump = (HERE / 'dump.c').read_text()
    menu = source_block((src / 'expr.h').read_text(), 'struct menu {', '\n};', 'menu structure declaration')
    if re.search(r'\bconst char \*filename;', menu):
        filename = '(menu)->filename'
    elif re.search(r'\bstruct file \*file;', menu):
        filename = '(menu)->file->name'
    else:
        raise ValueError('Unrecognized native menu source-location field')
    dump += '#define prototype_menu_filename(menu) (' + filename + ')\n'
    dump += (HERE / 'kconfig_syntax.inc').read_text()
    (output / 'dump.c').write_text(dump)
    identity = {str(path.relative_to(linux)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(src.iterdir()) if path.suffix in ('.c', '.h', '.l', '.y')}
    identity.update({str(path.relative_to(linux)): hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in sorted((linux / 'scripts/include').rglob('*.h'))})
    (output / 'parser-sources.json').write_text(json.dumps(identity, sort_keys=True))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Prepare native Kconfig syntax exporter sources')
    parser.add_argument('--linux', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    prepare_sources(args.linux, args.out)
