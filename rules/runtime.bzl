"""Adapt the artifact-oriented kernel backend to action-template callbacks."""

def action_tools(tools):
    """Keep provider-owned runfiles on ordinary native actions."""
    return [tool for name, tool in tools.items() if name.startswith("compiler_tool_") or name.startswith("execution_tool_")]

def read_payload(expanded):
    chunks = sorted([f.basename for f in expanded.children if "=" in f.basename])

    # Join before unescaping: an escape may straddle publisher chunks. Decode
    # the marker last so literal strings such as "%2F" survive unchanged.
    encoded = "".join([chunk.split("=", 1)[1] for chunk in chunks])
    return json.decode(encoded.replace("%2F", "/").replace("%5C", "\\").replace("%25", "%"))

def _write_text(tctx, tools, output, pieces):
    tctx.run(
        executable = tools["writer_shell"],
        inputs = tools["writer_files"],
        tools = [tool for name, tool in tools.items() if name.startswith("writer_tool_")],
        outputs = [output],
        arguments = ["-eu", "-c", '''mkdir=$1
output=$2
shift 2
case "$output" in */*) "$mkdir" -p -- "${output%/*}";; esac
printf '%s' "$@" > "$output"
''', "kernel-write", tools["writer_mkdir"].path, output.path] + pieces,
    )

def touch_files(tctx, tools, outputs):
    """Publish empty payload chunks without truncating existing files."""
    tctx.run(
        executable = tools["writer_shell"],
        inputs = tools["writer_files"],
        tools = [tool for name, tool in tools.items() if name.startswith("writer_tool_")],
        outputs = outputs,
        arguments = ["-eu", "-c", '''mkdir=$1
touch=$2
shift 2
parents=()
for output do
    case "$output" in */*) parents+=("${output%/*}");; esac
done
if ((${#parents[@]})); then
    "$mkdir" -p -- "${parents[@]}"
fi
"$touch" -- "$@"
''', "kernel-touch", tools["writer_mkdir"].path, tools["writer_touch"].path] + [file.path for file in outputs],
        progress_message = "Write kernel schedule metadata",
    )

def _text_chunks(content, limit):
    # Bazel Starlark indexes UTF-8 bytes, but spawn arguments are Unicode.
    # Back up at most three continuation bytes rather than split a code point.
    chunks, start = [], 0
    for _ in range(0, len(content), limit - 3):
        if start == len(content):
            break
        end = min(start + limit, len(content))
        for _ in range(3):
            if end < len(content) and "\200" <= content[end] and content[end] < "\300":
                end -= 1
            else:
                break
        chunks.append((start, content[start:end]))
        start = end
    return chunks

def writer(tctx, tools, root):
    count = []

    # buildifier: disable=unused-variable
    def write(output, content, **kwargs):
        count.append(0)
        if len(content) < 60000:
            _write_text(tctx, tools, output, [content])
            return
        parts = []
        for start, piece in _text_chunks(content, 240000):
            part = tctx.declare_file("write/%d-%d" % (len(count), start), directory = root)
            _write_text(tctx, tools, part, [chunk for _, chunk in _text_chunks(piece, 16000)])
            parts.append(part)

        # Preserve the existing text concatenation's universal-newline reads.
        # Literal/chunk writes above preserve the supplied UTF-8 bytes.
        script = tools["emit"]
        tctx.run(executable = tools["python"], inputs = depset(parts + [script], transitive = [tools["python_files"]]), outputs = [output], arguments = [script.path, "--concat", output.path] + [p.path for p in parts])

    return write

def context(tctx, roots, tools, params, planning = False, recorded = None, kernel_roots = ["vmlinux"], required_options = {}, validate_native_objects = False):
    runtime_tools = tools
    virtual = {}
    sequence = []
    write_file = writer(tctx, tools, roots["scratch"])
    common_paths, common_manifest = {}, None
    if recorded != None:
        common = depset(transitive = [tools["compiler_files"], tools["execution_files"]]).to_list()
        common_paths = {f.path: True for f in common}
        common_manifest = tctx.declare_file("actions/tools.json", directory = roots["kernel"])
        write_file(common_manifest, json.encode(common_paths.keys()))

    # buildifier: disable=unused-variable
    def declare_file(name, **kwargs):
        return tctx.declare_file(name, directory = roots["kernel"])

    def declare_directory(name):
        file = declare_file(name)
        virtual[file.path] = True
        return file

    # buildifier: disable=unused-variable
    def write(output, content, **kwargs):
        if not planning:
            write_file(output, content)

    def args():
        values = []

        # Adapter accepts the actions.args interface.
        # buildifier: disable=unused-variable
        def add_all(items, **kwargs):
            values.extend([x.path if type(x) == "File" else x for x in items])

        def add(item):
            values.append(item.path if type(item) == "File" else item)

        return struct(values = values, add_all = add_all, add = add)

    # buildifier: disable=unused-variable
    def run(executable, outputs, inputs = [], arguments = [], tools = [], env = None, input_aliases = {}, empty_directories = [], **kwargs):
        compiler = kwargs.get("compiler")
        if kwargs.get("mnemonic") in ["KbuildCC", "KbuildAS", "KbuildHostCC", "KbuildLLVMBackend"] and compiler == None:
            fail("Native compiler action requires explicit compiler identity: " + kwargs["mnemonic"])
        if planning:
            return
        sequence.append(0)
        files = inputs.to_list() if type(inputs) == "depset" else list(inputs)
        flat = []
        for argument in arguments:
            flat.extend(argument.values if type(argument) == "struct" else [argument])
        if compiler != None and compiler["phase"] == "backend":
            if recorded == None:
                fail("LLVM backend actions require the measured scheduler")
            recorded.append(dict(
                compiler = compiler,
                inputs = [file.path for file in files],
                outputs = [file.path for file in outputs],
                scan = None,
                message = kwargs.get("progress_message", "Compile native LLVM IR"),
            ))
            return
        all_files = files + input_aliases.values() + [x for x in tools if type(x) == "File"] + runtime_tools["compiler_files"].to_list()
        if type(executable) == "File":
            all_files.append(executable)
            executable = executable.path
        environment = dict(runtime_tools.get("environment", {}) if env == None else env)
        spec = dict(
            executable = executable,
            arguments = flat,
            inputs = [f.path for f in all_files],
            outputs = [f.path for f in outputs],
            directory_inputs = [f.path for f in all_files if f.path in virtual],
            directory_outputs = [f.path for f in outputs if f.path in virtual],
            environment = environment,
            path_environment = runtime_tools.get("path_environment", {}),
            tool_runfiles = {tool.executable.path + ".runfiles": True for tool in action_tools(runtime_tools) if tool.runfiles_manifest != None}.keys(),
            aliases = {path: file.path for path, file in input_aliases.items()},
            empty_directories = empty_directories,
        )
        if recorded != None:
            alias_files = {file.path: True for file in input_aliases.values()}
            spec["inputs"] = [path for path in spec["inputs"] if path not in common_paths or path in alias_files] + [common_manifest.path]
            spec["common_inputs"] = common_manifest.path
        else:
            spec["inputs"].extend(input_aliases.keys())
        manifest = tctx.declare_file("actions/%d.json" % len(sequence), directory = roots["kernel"])
        write_file(manifest, json.encode(spec))
        if recorded != None:
            scan = kwargs.get("scan")
            if scan == None and kwargs.get("mnemonic") in ["KbuildCC", "KbuildAS"]:
                scan = dict(argument_start = 3)
            elif scan == None and executable in [runtime_tools["host_cc"], runtime_tools.get("host_cxx", ""), runtime_tools.get("target_cc", "")] and ("-c" in flat or "-S" in flat):
                scan = dict(argument_start = 0)
            recorded.append(dict(
                manifest = manifest.path,
                inputs = spec["inputs"],
                outputs = spec["outputs"],
                scan = scan,
                message = kwargs.get("progress_message", kwargs.get("mnemonic", "Kernel action")),
                compiler = compiler or {},
            ))
            return
        tctx.run(
            executable = params.python,
            inputs = depset([manifest, params.executor] + all_files, transitive = [runtime_tools["python_files"]]),
            outputs = outputs,
            tools = action_tools(runtime_tools),
            arguments = [params.executor.path, manifest.path],
            progress_message = kwargs.get("progress_message", "%s %s" % (kwargs.get("mnemonic", "Kernel action"), outputs[0].path)),
        )

    execution = struct(
        commands = tools["commands"],
        host_cc = tools["host_cc"],
        host_roles = tools["host_roles"],
        host_spec = tools["host_spec"],
        backend_environment = tools["backend_environment"],
        target_compiler_files = tools["target_compiler_files"],
        host_compile_env = tools.get("host_compile_env", {}),
        host_link_env = tools.get("host_link_env", {}),
        files = tools["execution_files"],
        tools = action_tools(runtime_tools),
        bin_dir = tools["commands"]["sh"].dirname,
        shell = tools["commands"]["sh"],
        python = tools["python"],
        environment = runtime_tools.get("environment", {}),
        path_environment = runtime_tools.get("path_environment", {}),
    )
    return struct(
        actions = struct(declare_file = declare_file, declare_directory = declare_directory, write = write, args = args, run = run),
        execution = execution,
        output_prefix = getattr(params, "output_prefix", roots["kernel"].path),
        native_link_script = params.link_script_text,
        file = struct(_materialize = params.materialize, _link_script = params.link_script, _native_recipe = params.native_recipe, _recipe_check = getattr(params, "recipe_check", None), _diagnostic_driver = getattr(params, "diagnostic_driver", None), _diagnostic_source = getattr(params, "diagnostic_source", None)),
        label = struct(name = "kernel"),
        attr = struct(roots = kernel_roots, required_options = required_options, validate_native_objects = validate_native_objects),
    )
