# Copyright (c) 2026 Andrew Murphy
#
# This file is part of coding-agent-useful-tools.
#
# coding-agent-useful-tools is dual-licensed:
# 1. Under the GNU Affero General Public License v3.0 (AGPLv3) for open-source
#    and non-proprietary use. See the LICENSE file in the root directory.
# 2. Under a Commercial License for proprietary use or without AGPLv3
#    obligations. See COMMERCIAL-LICENSE.md or contact andrew_murphy_datapro@outlook.com
#     with subject line: "Commercial Licensing Request for coding-agent-useful-tools".

"""Count tool calls and leading shell commands to help build permission allowlists."""
from collections import Counter
import json
from pathlib import Path
import re
import shlex
from .core.config import add_common_arguments, default_out
from .core.out import summary, write_outputs
from .core.paths import checked_destination
from .core.redact import redact
from .readers.base import SessionMeta
from .readers.discover import discover_sessions, input_paths, read_session
from .core.zst import ZstdUnavailable

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9._:-]{0,40}")
_SUBWORD = re.compile(r"[a-z][a-z0-9-]{0,30}")
_MODULE = re.compile(r"[A-Za-z][A-Za-z0-9._]{0,40}")
_ASSIGN = re.compile(r"[A-Za-z_]\w*=")
_HEREDOC = re.compile(r"(?<!<)<<(?!<)-?\s*(['\"]?)([A-Za-z_]\w*)\1")
_SHELLS = {"bash", "sh", "zsh", "pwsh", "powershell", "cmd"}
_SHELL_FLAGS = {"-c", "-lc", "-command", "/c"}
_KEYWORDS = {"if", "then", "else", "elif", "fi", "for", "while", "do", "done", "case", "esac", "function", "return", "{", "}"}
_CODE_PREFIXES = ("def ", "import ", "from ", "class ")
SUBCOMMAND_TOOLS = frozenset(
    ("git gh npm pnpm yarn bun docker kubectl cargo go pip uv dotnet az aws gcloud brew winget "
     "terraform poetry conda systemctl apt choco scoop code claude codex rtk").split())
_PYTHONS = {"python", "python3", "py"}
_WRAPPERS = {"sudo", "env", "nohup", "time", "command"}


def is_shell_tool(name):
    lowered = (name or "").lower()
    if lowered.startswith("mcp__"): return False
    return lowered in ("bash", "powershell") or any(word in lowered for word in ("shell", "exec", "command"))


def command_text(raw):
    """Extract the command string from a shell tool call's input text."""
    try: value = json.loads(raw)
    except (ValueError, TypeError): value = raw
    if isinstance(value, dict): value = value.get("command", value.get("cmd", ""))
    if isinstance(value, list):
        words = [str(x) for x in value]
        if len(words) >= 3 and _base(words[0]).lower() in _SHELLS and words[1].lower() in _SHELL_FLAGS: return words[-1]
        return " ".join(words)
    return value if isinstance(value, str) else ""


def _base(token):
    name = re.split(r"[\\/]", token)[-1]
    return name[:-4] if name.lower().endswith(".exe") else name


def split_segments(text):
    """Split on && || ; | and newlines outside quotes, skipping heredoc bodies."""
    segments = []; current = []; quote = None; i = 0; n = len(text); line_start = 0
    def flush():
        segments.append("".join(current)); current.clear()
    while i < n:
        ch = text[i]
        if quote:
            current.append(ch)
            if ch == quote: quote = None
            i += 1; continue
        if ch in "'\"": quote = ch; current.append(ch); i += 1; continue
        if ch == "\n":
            markers = [m.group(2) for m in _HEREDOC.finditer(text[line_start:i])]
            flush(); i += 1
            for marker in markers:
                while i < n:
                    end = text.find("\n", i); end = n if end < 0 else end
                    body = text[i:end].strip(); i = end + 1
                    if body == marker: break
            line_start = i
            continue
        if text.startswith(("&&", "||"), i): flush(); i += 2; continue
        if ch in ";|": flush(); i += 1; continue
        current.append(ch); i += 1
    flush()
    return segments


def _strip_wrappers(tokens):
    while tokens:
        head = tokens[0]
        if _ASSIGN.match(head) or head in _WRAPPERS: tokens = tokens[1:]
        elif head == "timeout" and len(tokens) > 1 and re.fullmatch(r"\d+[smhd]?", tokens[1]): tokens = tokens[2:]
        else: break
    return tokens


def _subword(token):
    return _SUBWORD.fullmatch(token) and redact(token)[0] == token


def _key_tail(first, rest, subtools):
    """Return the safe words that follow the first token."""
    lowered = first.lower()
    if lowered in _PYTHONS:
        if len(rest) >= 2 and rest[0] == "-m" and _MODULE.fullmatch(rest[1]) and redact(rest[1])[0] == rest[1]: return ["-m", rest[1]]
        return []
    if lowered not in subtools: return []
    tail = []
    for token in rest[:2 if lowered == "rtk" else 1]:
        if not _subword(token): break
        tail.append(token)
    return tail


def segment_key(segment, subtools=SUBCOMMAND_TOOLS):
    """Return (key, dropped): key is a privacy-safe command key or None."""
    stripped = segment.strip()
    if not stripped or stripped.startswith("#") or stripped.startswith(_CODE_PREFIXES): return None, False
    try: tokens = shlex.split(stripped, posix=False)
    except ValueError: return None, True
    tokens = _strip_wrappers(tokens)
    if not tokens or tokens[0] in _KEYWORDS: return None, False
    first = _base(tokens[0])
    if not _WORD.fullmatch(first) or redact(first)[0] != first: return None, True
    if first.endswith(":") or "=" in tokens[1:2] or "=" in tokens: return None, False
    return " ".join([first, *_key_tail(first, tokens[1:], subtools)]), False


def count_commands(text, counts, tool, subtools=SUBCOMMAND_TOOLS):
    dropped = 0
    for segment in split_segments(text):
        key, was_dropped = segment_key(segment, subtools)
        dropped += was_dropped
        if key: counts[(tool, key)] += 1
    return dropped


def _selected_sessions(args):
    paths = sorted(discover_sessions(args.root, source=args.source), key=lambda p: (-Path(p).stat().st_mtime_ns, str(p)))
    needle = (args.project or "").casefold(); chosen = []
    for path in paths:
        if len(chosen) >= args.last: break
        try: stream = read_session(path, source=args.source); meta = next(stream)
        except ZstdUnavailable: continue
        if meta.source not in ("codex", "claude"): continue
        if needle and needle not in (str(meta.cwd or "") + " " + str(meta.path)).casefold(): continue
        chosen.append((path, meta, stream))
    return chosen


def run(args):
    if args.last < 1 or args.top < 1 or args.min_count < 1: raise ValueError("--last, --top and --min-count must be positive")
    subtools = SUBCOMMAND_TOOLS | {t.lower() for t in (args.subcommand_tool or [])}
    tools = Counter(); commands = Counter(); dropped = 0; sessions = _selected_sessions(args)
    metas = [meta for _, meta, _ in sessions]
    aggregate = SessionMeta("toolcalls", "generic", "", sum(m.bytes for m in metas))
    directory = Path(args.out) if args.out else default_out("toolcalls")
    for target in (directory, directory/"toolcalls.md", directory/"toolcalls.json"):
        checked_destination(target, inputs=input_paths(metas))
    for _, _, stream in sessions:
        for e in stream:
            if e.kind != "tool_call": continue
            name = e.tool or "(unknown)"; tools[name] += 1
            if is_shell_tool(name): dropped += count_commands(command_text(e.text), commands, name, subtools)
    ranked = sorted(((t, k, c) for (t, k), c in commands.items() if c >= args.min_count), key=lambda r: (-r[2], r[1], r[0]))[:args.top]
    tool_rows = sorted(tools.items(), key=lambda r: (-r[1], r[0]))
    data = {"sessions_scanned": len(metas), "tool_calls": sum(tools.values()), "dropped_segments": dropped,
            "min_count": args.min_count, "top": args.top,
            "tools": [{"name": n, "count": c} for n, c in tool_rows],
            "commands": [{"tool": t, "key": k, "count": c} for t, k, c in ranked]}
    lines = [f"Sessions scanned: {len(metas)}", f"Shell segments dropped by privacy filter: {dropped}", "",
             "## Tools", "| tool | count |", "| --- | ---: |"]
    lines += [f"| {n} | {c} |" for n, c in tool_rows]
    lines += ["", f"## Commands (min count {args.min_count}, top {args.top})", "| shell tool | command | count |", "| --- | --- | ---: |"]
    lines += [f"| {t} | {k} | {c} |" for t, k, c in ranked]
    write_outputs("toolcalls", aggregate, data, "\n".join(lines), directory, max_tokens=args.max_tokens)
    summary({"sessions": len(metas), "tool_calls": data["tool_calls"], "commands": len(ranked), "dropped_segments": dropped}, json_mode=args.json)
    return 0


def register(subparsers):
    p = subparsers.add_parser("toolcalls", help="Count tool calls and leading shell commands for permission allowlists")
    p.add_argument("--root", action="append"); p.add_argument("--source", choices=("codex", "claude"))
    p.add_argument("--last", type=int, default=50); p.add_argument("--project")
    p.add_argument("--subcommand-tool", action="append", metavar="NAME", help="also keep the subcommand word for NAME (repeatable)")
    p.add_argument("--min-count", type=int, default=3); p.add_argument("--top", type=int, default=40)
    add_common_arguments(p, view=True); p.set_defaults(func=run)
