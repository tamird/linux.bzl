"""Generate explicit utility registrations; no host utility discovery."""
from pathlib import Path
import shutil

HERE = Path(__file__).resolve().parent

MODULE = '''
bazel_dep(name="linux.bzl", version="0.0.0")
single_version_override(
    module_name="rules_bison",
    version="0.4.bcr.1",
    patches=["//rules:rules_bison_cstdlib.patch"],
    patch_strip=1,
)
single_version_override(
    module_name="rules_cc_autoconf",
    version="0.27.0",
    patches=["//rules:rules_cc_autoconf_no_host_shell.patch"],
    patch_strip=1,
)

register_toolchains("@linux.bzl//execution_tools:linux_x86_64")
'''

def generate(repo):
    for name in ['source_tree.bzl', 'prepare_source.py', 'host_libraries.bzl', 'rules_cc_autoconf_no_host_shell.patch', 'rules_bison_cstdlib.patch']:
        shutil.copyfile(HERE / name, repo / 'rules' / name)
    (repo / 'host_libraries').mkdir(exist_ok=True)
    (repo / 'host_libraries/BUILD.bazel').write_text('''load("//rules:host_libraries.bzl", "kernel_host_library")
kernel_host_library(name="libelf", library="@elfutils//:libelf", apis=["elf_getshdr"], visibility=["//visibility:public"])
''')
    with (repo / 'BUILD.bazel').open('a') as stream:
        stream.write('load("//rules:source_tree.bzl", "kernel_source_tree")\nkernel_source_tree(name="prepared_source", sources=":source_tree", anchor="source/Makefile")\n')
