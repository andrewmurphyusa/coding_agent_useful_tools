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

"""Summarize record sizes and paired tool results."""
from glob import glob
import json
import shlex
from pathlib import Path
from .core.config import add_common_arguments, default_out
from .core.out import summary, write_outputs
from .core.paths import checked_destination
from .readers.base import SessionMeta
from .readers.discover import discover_sessions, read_session, resolve_session


def command_head(text):
    try:
        value = json.loads(text)
        if isinstance(value, dict): text = value.get("command", value.get("cmd", ""))
        elif isinstance(value, str): text = value
    except (ValueError, TypeError): pass
    if isinstance(text, list): words = [str(x) for x in text]
    else:
        try: words = shlex.split(str(text), posix=True)
        except ValueError: words = str(text).split()
    if not words: return "(unknown)"
    n = 2 if words[0] in ("git", "dotnet") or words[:2] in (["python", "-m"], ["npm", "run"]) else 1
    return " ".join(words[:n])


def census(meta, events):
    return _census_streams(meta, (events,))


def _census_streams(meta, streams):
    """Aggregate counters while pairing calls only within each source file."""
    groups = {"raw_type": {}, "kind": {}}
    tools = {}; heads = {}
    for events in streams:
        calls = {}
        for e in events:
            for field in groups:
                key = getattr(e, field) or "(unknown)"
                group = groups[field].setdefault(key, {"count":0, "bytes":0, "tokens":0, "largest_seq":None, "largest_bytes":-1})
                group["count"] += 1; group["bytes"] += e.raw_bytes; group["tokens"] += e.est_tokens
                if e.raw_bytes > group["largest_bytes"]: group["largest_seq"], group["largest_bytes"] = e.seq, e.raw_bytes
            if e.kind == "tool_call" and e.call_id: calls[e.call_id] = e
            if e.kind == "tool_result":
                call = calls.pop(e.call_id, None); tool = e.tool or (call.tool if call else None) or "(unknown)"
                targets = [(tools, tool)]
                if call and any(word in tool.lower() for word in ("shell", "exec", "command", "terminal")):
                    targets.append((heads, command_head(call.text)))
                for table, key in targets:
                    row = table.setdefault(key, {"count":0, "total_tokens":0, "max_tokens":0})
                    row["count"] += 1; row["total_tokens"] += e.est_tokens; row["max_tokens"] = max(row["max_tokens"], e.est_tokens)
    for table in groups.values():
        for row in table.values(): row["percent"] = 100 * row["bytes"] / meta.bytes if meta.bytes else 0
    for table in (tools, heads):
        for row in table.values(): row["mean_tokens"] = row["total_tokens"] / row["count"]
    return {**groups, "tools":tools, "command_heads":heads}


def run(args):
    if args.all:
        paths = []
        for ref in args.session:
            matches = sorted(glob(ref, recursive=True))
            paths.extend(Path(p) for p in matches if Path(p).is_file())
        if not args.session: paths = list(discover_sessions(args.root, source=args.source))
    else:
        if len(args.session) != 1: raise ValueError("census requires one session unless --all is specified")
        paths = [resolve_session(args.session[0], args.root, source=args.source)]
    metas=[]; streams=[]
    for path in sorted(set(paths), key=str):
        stream=read_session(path, source=args.source); meta=next(stream)
        if args.since and (not meta.started or meta.started < args.since): continue
        if args.until and (not meta.started or meta.started[:len(args.until)] > args.until): continue
        metas.append(meta); streams.append(stream)
    if not metas: raise ValueError("No matching sessions")
    meta=metas[0] if len(metas)==1 else SessionMeta("aggregate", "generic", "", sum(m.bytes for m in metas))
    directory=Path(args.out) if args.out else default_out(meta.session_id)
    for target in (directory, directory/"census.md", directory/"census.json"):
        checked_destination(target, inputs=[m.path for m in metas])
    data=_census_streams(meta, streams); data["sessions"] = len(metas)
    lines=[]
    for label in ("raw_type", "kind", "tools", "command_heads"):
        lines.append("## " + label)
        for key,row in sorted(data[label].items(), key=lambda item:(-item[1].get("bytes", item[1].get("total_tokens",0)), item[0])): lines.append(f"- {key}: {row}")
    write_outputs("census",meta,data,"\n".join(lines),args.out,max_tokens=1500)
    summary({"sessions":len(metas), "events":sum(x["count"] for x in data["kind"].values()), "bytes":meta.bytes},json_mode=args.json)
    return 0


def register(subparsers):
    p=subparsers.add_parser("census"); p.add_argument("session",nargs="*"); p.add_argument("--all",action="store_true"); p.add_argument("--root",action="append"); p.add_argument("--source",choices=("codex","claude","generic")); p.add_argument("--since"); p.add_argument("--until"); add_common_arguments(p); p.set_defaults(func=run)
