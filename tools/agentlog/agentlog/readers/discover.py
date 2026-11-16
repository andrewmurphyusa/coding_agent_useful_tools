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

"""Discover, identify and resolve read-only local session files."""
import os
from pathlib import Path
from . import codex, claude, generic, requestdump
from ..core.jsonl import iter_records
from ..core.zst import ZstdUnavailable

READERS = {"codex":codex.read, "claude":claude.read, "generic":generic.read, "requestdump":requestdump.read}

def default_roots():
    return {"codex":Path(os.environ.get("AGENTLOG_CODEX_HOME",str(Path.home()/".codex"))),
            "claude":Path(os.environ.get("AGENTLOG_CLAUDE_HOME",str(Path.home()/".claude")))}

def discover_sessions(roots=None, *, include_archived=True, source=None):
    if isinstance(roots,(str,Path)):roots=[roots]
    if roots is None:
        homes=default_roots();roots=[p for key,p in homes.items() if source in (None,key)]
        mode="default"
    else:mode="explicit"
    paths=set()
    for root in roots:
        root=Path(root).expanduser()
        if root.is_file():paths.add(root);continue
        if not root.exists():continue
        if mode=="default":
            if root==default_roots()["codex"]:
                paths.update((root/"sessions").rglob("rollout-*.jsonl"))
                if include_archived:
                    paths.update((root/"archived_sessions").glob("*.jsonl"));paths.update((root/"archived_sessions").glob("*.jsonl.zst"))
            else:
                paths.update((root/"projects").glob("*/*.jsonl"));paths.update((root/"projects").glob("*/*/subagents/*.jsonl"))
        else:
            paths.update(root.rglob("*.jsonl"));paths.update(root.rglob("*.jsonl.zst"))
            if (root/"request-001.txt").is_file(): paths.add(root)
            paths.update(p.parent for p in root.rglob("request-001.txt") if p.is_file())
    for path in sorted(paths,key=lambda p:str(p)):
        if not include_archived and "archived_sessions" in path.parts:continue
        if source:
            try:
                if detect_source(path)!=source:continue
            except ZstdUnavailable:continue
        yield path

def detect_source(path):
    if Path(path).is_dir() and (Path(path)/"request-001.txt").is_file(): return "requestdump"
    for index,(_,raw,_) in enumerate(iter_records(path)):
        if raw.get("type")=="session_meta" or (raw.get("type") in ("turn_context","response_item","event_msg") and "payload" in raw):return "codex"
        if "sessionId" in raw or (raw.get("type") in ("user","assistant") and isinstance(raw.get("message"),dict)):return "claude"
        if index>=19:break
    return "generic"

def read_session(path, *, source=None, threads_csv=None, cwd_prefixes=()):
    path=Path(path)
    reader=READERS[source or detect_source(path)]
    if reader is requestdump.read: yield from reader(path, threads_csv=threads_csv)
    elif cwd_prefixes: yield from reader(path, cwd_prefixes=cwd_prefixes)
    else: yield from reader(path)

def iter_sessions(roots=None, **kwargs):
    for path in discover_sessions(roots,**kwargs):
        try:yield next(read_session(path))
        except ZstdUnavailable:continue

def resolve_session(ref, roots=None, *, source=None, include_archived=True):
    path=Path(ref).expanduser()
    if path.is_file() or (path.is_dir() and (path/"request-001.txt").is_file()):return path
    allow_prefix=len(str(ref))>=8
    matches=[]
    for candidate in discover_sessions(roots,source=source,include_archived=include_archived):
        try:meta=next(read_session(candidate,source=source))
        except ZstdUnavailable:continue
        if meta.session_id==str(ref) or (allow_prefix and meta.session_id.startswith(str(ref))):matches.append(candidate)
    if not matches:
        if not allow_prefix:raise ValueError("Session ID prefixes must have at least 8 characters")
        raise FileNotFoundError("No session matches the supplied reference")
    if len(matches)>1:raise ValueError("Ambiguous session ID prefix; use a longer ID or a file path")
    return matches[0]

def load_session(ref, roots=None, *, source=None, threads_csv=None):
    records=read_session(resolve_session(ref,roots,source=source),source=source,threads_csv=threads_csv)
    meta=next(records)
    return meta,list(records)

def input_paths(metas, threads_csv=None):
    paths = [meta.path for meta in metas]
    if threads_csv and any(meta.source == 'requestdump' for meta in metas): paths.append(threads_csv)
    return paths

discover=discover_sessions
resolve=resolve_session
