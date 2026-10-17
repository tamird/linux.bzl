"""Generate a kernel repository using one prebuilt, self-contained tool."""

def _kernel_repository_tool_impl(ctx):
    manifest = json.decode(ctx.read(ctx.attr.manifest))
    if type(manifest) != "dict" or manifest.get("format") != 1 or type(manifest.get("platforms")) != "dict":
        fail("Generator manifest must have format 1 and a platforms dictionary")
    os = {"linux": "linux", "mac os x": "macos"}.get(ctx.os.name.lower())
    cpu = {"amd64": "x86_64", "x86_64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}.get(ctx.os.arch)
    if os == None or cpu == None:
        fail("Unsupported generator host: %s/%s" % (ctx.os.name, ctx.os.arch))
    platform = os + "_" + cpu
    artifact = manifest["platforms"].get(platform)
    if type(artifact) != "dict" or type(artifact.get("url")) != "string" or not artifact.get("url") or type(artifact.get("integrity")) != "string" or not artifact.get("integrity"):
        fail("Generator manifest must pin a URL and integrity for " + platform)
    ctx.download_and_extract(
        url = artifact["url"],
        integrity = artifact["integrity"],
    )
    if not ctx.path("kernel-repository").exists:
        fail("Generator archive must contain kernel-repository at its root")
    ctx.file("BUILD.bazel", 'exports_files(["kernel-repository"], visibility = ["//visibility:public"])\n')

kernel_repository_tool = repository_rule(
    implementation = _kernel_repository_tool_impl,
    attrs = {
        "manifest": attr.label(
            mandatory = True,
            allow_single_file = [".json"],
            doc = "Pinned artifact manifest: format 1 and platforms mapping host OS_CPU to {url, integrity}.",
        ),
    },
    doc = "Download the checksummed generator archive for the repository host.",
)

def _kernel_repository_impl(ctx):
    source = ctx.path(ctx.attr.source).realpath.dirname
    tool = ctx.path(ctx.attr.generator)

    # These are the actual inputs to the native parsers and source catalogue.
    # A local checkout changing must invalidate generation, not keep stale BUILD
    # files. Watch the executable too: ctx.execute does not track its contents.
    ctx.watch_tree(source)
    ctx.watch(tool)
    arguments = [tool, "generate", "--linux", source, "--out", ctx.path(".")]
    if ctx.attr.all_backends:
        arguments.append("--all-backends")
    for fragment in ctx.attr.config_fragments:
        path = ctx.path(fragment)
        ctx.watch(path)
        arguments.extend(["--config-fragment", path])
    result = ctx.execute(
        arguments,
        timeout = ctx.attr.timeout,
        environment = {
            "PATH": "",
            "PYTHONPATH": "",
            "PYTHONHOME": "",
            "LC_ALL": "C",
        },
        quiet = False,
    )
    if result.return_code:
        fail("Kernel repository generation failed:\n" + result.stderr)

kernel_repository = repository_rule(
    implementation = _kernel_repository_impl,
    attrs = {
        "source": attr.label(
            mandatory = True,
            allow_single_file = True,
            doc = "The kernel checkout's root Makefile. Its containing tree is watched.",
        ),
        "generator": attr.label(
            mandatory = True,
            allow_single_file = True,
            doc = "Prebuilt kernel-repository executable, not a build-action output.",
        ),
        "config_fragments": attr.label_list(allow_files = [".config"], doc = "Requested config fragments, rendered as data without resolving options."),
        "all_backends": attr.bool(default = False),
        "timeout": attr.int(default = 600),
    },
    doc = "Parse Make/Kconfig and render symbolic BUILD files at repository fetch time.",
)
