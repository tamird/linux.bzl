"""Construct native-config comparisons using this module's Python dependency."""

load("@rules_python//python:defs.bzl", "py_test")

def config_comparison_test(name, checker, reference):
    """Run the generated repository's checker against its native reference."""
    py_test(
        name = name,
        srcs = [checker],
        main = checker,
        args = ["$(rootpath " + reference + ")"],
        data = [reference],
        env = {"PYTHONDONTWRITEBYTECODE": "1"},
    )
