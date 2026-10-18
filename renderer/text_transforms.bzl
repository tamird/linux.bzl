"""Evaluate bounded literal arguments and text programs without a process."""

def sed_substitute(text, encoded):
    spec = json.decode(encoded)
    code = spec["code"]

    # Ordered backtracking implements greedy ERE repetition and captures.
    for start in range(len(text) + 1):
        pending = [(0, start, {})]
        seen = {}
        for _ in range(100000):
            if not pending:
                break
            pc, pos, captures = pending.pop()
            key = (pc, pos, str(captures))
            if key in seen:
                continue
            seen[key] = True
            op = code[pc]
            if op[0] == "accept":
                result, escaped = "", False
                for char in spec["replacement"].elems():
                    if escaped:
                        if char in "123456789":
                            group = int(char) * 2
                            result += text[captures[group]:captures[group + 1]] if group in captures and group + 1 in captures else ""
                        else:
                            result += char
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == "&":
                        result += text[start:pos]
                    else:
                        result += char
                return text[:start] + result + text[pos:]
            if op[0] == "split":
                pending.append((op[2], pos, captures))
                pending.append((op[1], pos, captures))
            elif op[0] == "jump":
                pending.append((op[1], pos, captures))
            elif op[0] == "save":
                captures = dict(captures)
                captures[op[1]] = pos
                pending.append((pc + 1, pos, captures))
            elif pos < len(text) and ((op[0] == "char" and text[pos] == op[1]) or (op[0] == "class" and (text[pos] in op[2]) != op[1])):
                pending.append((pc + 1, pos + 1, captures))
        if pending:
            fail("Text transformation exceeded 100000 matching steps")
    return text

def shell_words(text, allow_recipe = False):
    # Split an already-expanded command argument list, retaining quoted C
    # strings. This does not parse Make expressions or execute shell syntax.
    words, current, quote, escaped, active = [], "", "", False, False
    for c in text.elems():
        if escaped:
            current += c
            escaped = False
        elif c == "\\" and quote != "'":
            escaped = True
            active = True
        elif quote:
            if c == quote:
                quote = ""
            else:
                current += c
        elif c in ["'", '"']:
            quote = c
            active = True
        elif c in [" ", "\t", "\n"]:
            if active:
                words.append(current)
                current, active = "", False
        elif c in [";", "|", "&", "<", ">", "`"]:
            if allow_recipe:
                return None
            fail("Shell operation in literal arguments: " + text)
        else:
            current += c
            active = True
    if quote or escaped:
        fail("Unclosed quote in literal arguments: " + text)
    if active:
        words.append(current)
    return words

def literal_echo(text):
    # Echo options, escapes, expansions and globbing vary with the shell or
    # consult external state. Only literal arguments have a text-only result.
    if any([c in text for c in "$`\\*?[]{}~#;|&<>()\n\r".elems()]):
        fail("Native echo requires literal arguments: " + text)
    words = shell_words(text)
    if words and words[0].startswith("-"):
        fail("Native echo options are unsupported: " + text)
    return " ".join(words)
