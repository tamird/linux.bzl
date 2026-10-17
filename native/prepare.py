from pathlib import Path
import argparse
import hashlib
import json
from compiler_capabilities import native_capabilities
HERE = Path(__file__).resolve().parent

def prepare_sources(linux, output):
    output.mkdir(parents=True, exist_ok=True)
    src = linux / 'scripts/kconfig'
    parser = (src / 'parser.y').read_text()
    source_hook = 'zconf_nextfile($2);'
    assert parser.count(source_hook) == 1
    parser = parser.replace(source_hook, 'menu_add_entry(NULL, M_COMMENT); current_entry->help = xstrdup($2);')
    finalize = '\tmenu_finalize();'
    assert parser.count(finalize) == 1
    parser = parser.replace(finalize, '\treturn; /* Link and finalize the syntax in Bazel analysis. */')
    parser = parser.replace('\tyyparse();', '\t/* Preprocessor definitions shared by the kernel Kconfig files. */\n\tif (strcmp(name, "scripts/Kconfig.include")) {\n\t\tzconf_initscan("scripts/Kconfig.include");\n\t\tyyparse();\n\t\tzconf_initscan(name);\n\t}\n\tyyparse();', 1)
    (output / 'parser.y').write_text(parser)
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
    scanner_body = scanner_body.replace(reset, reset + '\n\tyyrestart(yyin);\n\tBEGIN(INITIAL);\n\tprev_token = prev_prev_token = T_EOL;\n\tcur_lineno = 1;')
    lexer = before + scanner + scanner_body + '\n}' + after
    (output / 'lexer.l').write_text(lexer)
    preprocess = (src / 'preprocess.c').read_text()
    marker = 'static LIST_HEAD(env_list);'
    assert preprocess.count(marker) == 1
    preprocess = preprocess.replace(marker, (HERE / 'kconfig_fallback.inc').read_text().replace('/* PROTOTYPE_COMPILER_CAPABILITIES */', native_capabilities()) + '\n' + marker)
    start = preprocess.index('static char *variable_expand(')
    before, after = preprocess[:start], preprocess[start:]
    after = after.replace('\tv = variable_lookup(name);', '\tres = prototype_probe_fallback(name, argc, argv);\n\tif (res) return res;\n\tv = variable_lookup(name);', 1)
    preprocess = (before + after).replace('\tcmd = argv[0];', '\treturn prototype_shell_fallback(argc, argv);\n\tcmd = argv[0];', 1)
    (output / 'preprocess.c').write_text(preprocess)
    dump = (HERE / 'dump.c').read_text()
    dump = dump[:dump.index('int main(')] + (HERE / 'kconfig_syntax.inc').read_text()
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
