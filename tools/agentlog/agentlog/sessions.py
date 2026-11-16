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

"""List session metadata and cached streaming counts."""
from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
from .core.config import add_common_arguments, default_out
from .core.out import dumps, summary, write_outputs
from .core.paths import checked_destination
from .core.tokens import est_tokens
from .readers.base import SessionMeta
from .readers.discover import discover_sessions, read_session
from .core.zst import ZstdUnavailable


def _duration(start, end):
    if not start or not end: return None
    try: return int((datetime.fromisoformat(end.replace("Z", "+00:00"))-datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds())
    except ValueError: return None


def run(args):
    if getattr(args, "indexed", False):
        from .search import run_indexed_sessions
        return run_indexed_sessions(args)
    if args.automation:
        try: from . import automations
        except ImportError: raise ValueError("--automation requires the automations plugin") from None
        if not hasattr(automations, "match"): raise ValueError("--automation requires automations.match")
    paths=list(discover_sessions(args.root)); cache_name=Path("agentlog-out/.cache/sessions.jsonl")
    inputs=paths + ([args.threads_csv] if getattr(args,'threads_csv',None) and args.source in (None,'requestdump') and any(p.is_dir() for p in paths) else [])
    if cache_name.is_symlink(): raise ValueError("Cache must not be a symlink")
    cache_path=checked_destination(cache_name,inputs=inputs)
    cache={}
    if cache_path.is_file():
        for line in cache_path.read_text(encoding="utf-8").splitlines():
            try:
                item=json.loads(line); cache[(item["path"],item["size"],item["mtime_ns"])]=item
            except (ValueError, KeyError, TypeError): continue
    rows=[]; updated=[]
    for path in paths:
        if path.is_dir() and args.source not in (None,'requestdump'): continue
        stat=path.stat(); key=(str(path.resolve()),stat.st_size,stat.st_mtime_ns)
        item=cache.get(key) if not path.is_dir() else None
        if item is None:
            try:
                stream=(read_session(path,threads_csv=args.threads_csv) if path.is_dir() and getattr(args,'threads_csv',None) else read_session(path)); meta=next(stream); events=turns=compactions=errors=0
                for e in stream:
                    events+=1; turns=max(turns,e.turn); compactions+=e.kind=="compaction"; errors+=e.kind=="error"
            except ZstdUnavailable: continue
            item={"path":key[0],"size":key[1],"mtime_ns":key[2],"meta":asdict(meta),"events":events,"turns":turns,"compactions":compactions,"errors":errors}
        if not path.is_dir(): updated.append(item)
        m=item["meta"]
        if args.source and m["source"]!=args.source: continue
        if args.since and (not m["started"] or m["started"] < args.since): continue
        if args.until and (not m["started"] or m["started"][:len(args.until)] > args.until): continue
        if args.cwd and not (m["cwd"] or "").startswith(args.cwd): continue
        if args.thread_source and m["thread_source"]!=args.thread_source: continue
        if args.min_bytes is not None and m["bytes"]<args.min_bytes: continue
        if args.model and m["model"]!=args.model: continue
        if args.cli_version and m["cli_version"]!=args.cli_version: continue
        if args.automation and automations.match(SessionMeta(**m))!=args.automation: continue
        rows.append({"id8":m["session_id"][:8],"source":m["source"],"started":m["started"],"duration":_duration(m["started"],m["ended"]),"bytes":m["bytes"],"est_tokens":(m["bytes"]*2+6)//7,"model":m["model"],"effort":m["reasoning_effort"],"cli_version":m["cli_version"],"thread_source":m["thread_source"],"events":item["events"],"turns":item["turns"],"compactions":item["compactions"],"errors":item["errors"],"title":(m["title"] or "")[:60]})
    rows.sort(key=lambda x:(x["started"] or "",x["id8"]))
    if cache_path.is_symlink() or (cache_path.exists() and cache_path.stat().st_nlink > 1): raise ValueError("Cache must not be a symlink or hard link")
    directory=Path(args.out) if args.out else default_out("sessions")
    for target in (directory, directory/"sessions.md", directory/"sessions.json"):
        checked_destination(target, inputs=inputs)
    cache_path.parent.mkdir(parents=True,exist_ok=True); cache_path.write_text("".join(dumps(i)+"\n" for i in updated),encoding="utf-8",newline="\n")
    lines=["# Sessions"]
    for i,row in enumerate(rows):
        line="- "+" | ".join(str(v) if v is not None else "" for v in row.values())
        if est_tokens("\n".join(lines+[line])) > 3800:
            lines.append(f"\u2026 {len(rows)-i} more (sessions.json)"); break
        lines.append(line)
    meta=SessionMeta("sessions","generic","",sum(r["bytes"] for r in rows))
    write_outputs("sessions",meta,rows,"\n".join(lines),args.out,max_tokens=4000)
    summary({"sessions":len(rows)},json_mode=args.json); return 0


def register(subparsers):
    p=subparsers.add_parser("sessions"); p.add_argument("--root",action="append"); p.add_argument("--source",choices=("codex","claude","requestdump")); p.add_argument("--since"); p.add_argument("--until"); p.add_argument("--cwd"); p.add_argument("--thread-source"); p.add_argument("--automation"); p.add_argument("--min-bytes",type=int); p.add_argument("--model"); p.add_argument("--cli-version"); p.add_argument("--indexed",action="store_true"); p.add_argument("--db",default="agentlog-out/index.sqlite"); p.add_argument("--limit",type=int,default=200); add_common_arguments(p); p.set_defaults(func=run)
