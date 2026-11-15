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

"""Claude Code content blocks and per-request token usage."""
from collections import OrderedDict
from contextlib import closing
import json
import re
import sqlite3
from .base import Usage, as_int, as_str, base_meta, event, fallback_id, text_of, utc
from ..core.jsonl import iter_records
from ..core.paths import private_temp_directory

def _dict(value): return value if isinstance(value,dict) else {}
_CONTEXT = re.compile(r"^\s*(?:<system-reminder>[\s\S]*?</system-reminder>\s*)+$")
_COMMAND = re.compile(r"^\s*<(?:command-name|local-command|local-command-stdout|command-message)>")

def _meta(path):
    values = {}; started=ended=None
    for _, raw, _ in iter_records(path):
        ts=utc(raw.get("timestamp"))
        if ts: started=started or ts;ended=ts
        for key in ("sessionId","cwd","version","gitBranch","title"):
            if raw.get(key) and key not in values: values[key]=raw[key]
        if raw.get("type")=="custom-title": values["title"]=raw.get("customTitle") or raw.get("title")
        model=_dict(raw.get("message")).get("model")
        if model and "model" not in values: values["model"]=model
    return base_meta(path,"claude",session_id=str(values.get("sessionId") or fallback_id(path)),cwd=values.get("cwd"),cli_version=values.get("version"),model=values.get("model"),git_branch=values.get("gitBranch"),title=values.get("title"),started=started,ended=ended)

def _usage(value):
    inp=as_int(value.get("input_tokens")); cached=as_int(value.get("cache_read_input_tokens")); created=as_int(value.get("cache_creation_input_tokens")); out=as_int(value.get("output_tokens"))
    context=sum(v or 0 for v in (inp,cached,created))
    return Usage(input=inp,cached_input=cached,output=out,reasoning=as_int(_dict(value.get("output_tokens_details")).get("thinking_tokens")),total=context+(out or 0),context_used=context,cache_creation_input=created)

def read(path):
    yield _meta(path)
    # Keep exact IDs on disk, not an evicting cache or a collision-prone digest.
    # The fixed page cache bounds resident memory as request cardinality grows.
    # Only identities are stored; transcript text is never copied to scratch.
    with private_temp_directory() as scratch, closing(sqlite3.connect(
            str(scratch/"identities.sqlite"), isolation_level=None)) as seen_usage:
        seen_usage.execute("PRAGMA cache_size=-1024")
        seen_usage.execute("PRAGMA journal_mode=OFF")
        seen_usage.execute("PRAGMA synchronous=OFF")
        seen_usage.execute("PRAGMA cache_spill=OFF")
        seen_usage.execute("PRAGMA temp_store=MEMORY")
        seen_usage.execute("CREATE TABLE identities (identity TEXT PRIMARY KEY) WITHOUT ROWID")
        yield from _events(path, seen_usage)

def _first_usage(connection, identity):
    if not any(identity):
        # Missing IDs cannot establish that records describe one request.
        return True
    # JSON preserves the pair boundary, None, and arbitrary Unicode exactly.
    key = json.dumps(identity, ensure_ascii=True)
    return connection.execute("INSERT OR IGNORE INTO identities VALUES (?)", (key,)).rowcount == 1

def _events(path, seen_usage):
    turn=0;seq=0;calls=OrderedDict()
    for _,raw,size in iter_records(path):
        typ=str(raw.get("type",""));message=_dict(raw.get("message"));content=message.get("content",raw.get("text",""))
        sidechain=bool(raw.get("isSidechain")) or "subagents" in str(path).replace("\\","/").split("/")
        blocks=content if isinstance(content,list) else [{"type":"text","text":text_of(content)}]
        compact=raw.get("isCompactSummary") or "compact" in (typ+str(raw.get("subtype",""))).lower()
        if compact:
            yield event(seq,raw,size,kind="compaction",role=message.get("role"),text=text_of(content),sidechain=sidechain,turn=turn,raw_type=typ+"/"+str(raw.get("subtype","")));seq+=1
            continue
        if typ not in ("user","assistant") or raw.get("isApiErrorMessage"):
            is_error=bool(raw.get("isApiErrorMessage")) or "error" in typ.lower()
            yield event(seq,raw,size,kind="error" if is_error else "other",text=text_of(content) if is_error else "",is_error=is_error,sidechain=sidechain,turn=turn,raw_type=typ);seq+=1
            continue
        has_real_user=typ=="user" and not raw.get("isMeta") and any(isinstance(b,dict) and b.get("type")=="text" and not (_CONTEXT.fullmatch(text_of(b)) or _COMMAND.search(text_of(b))) for b in blocks)
        if has_real_user:turn+=1
        emitted=False
        for block in blocks:
            if not isinstance(block,dict):continue
            bt=str(block.get("type",""));kw=dict(kind="other",role=typ,text="",sidechain=sidechain,turn=turn,raw_type=typ+"/"+bt)
            if bt=="text":
                text=text_of(block)
                context=bool(raw.get("isMeta")) or bool(_CONTEXT.fullmatch(text)) or bool(_COMMAND.search(text))
                kw.update(kind="context" if context else "user_msg" if typ=="user" else "assistant_msg",text=text)
            elif bt=="thinking":kw.update(kind="reasoning",text=block.get("thinking",""))
            elif bt=="redacted_thinking":kw.update(kind="reasoning",text=f"[encrypted {len(str(block.get('data','')).encode('utf-8'))} bytes]")
            elif bt=="tool_use":
                call_id=as_str(block.get("id"));tool=as_str(block.get("name"))
                if call_id:
                    calls[call_id]=tool
                    if len(calls)>1024:calls.popitem(last=False)
                kw.update(kind="tool_call",tool=tool,call_id=call_id,text=block.get("input",{}))
            elif bt=="tool_result":
                call_id=as_str(block.get("tool_use_id"))
                kw.update(kind="tool_result",role="tool",tool=calls.pop(call_id,None),call_id=call_id,text=block.get("content",""),is_error=bool(block.get("is_error")))
            yield event(seq,raw,size if not emitted else 0,**kw);seq+=1;emitted=True
        usage=message.get("usage");usage_id=(as_str(message.get("id")) or as_str(raw.get("uuid")),as_str(raw.get("requestId")))
        if typ=="assistant" and isinstance(usage,dict) and message.get("model")!="<synthetic>" and _first_usage(seen_usage, usage_id):
            yield event(seq,raw,size if not emitted else 0,kind="token_usage",usage=_usage(usage),sidechain=sidechain,turn=turn,raw_type=typ+"/usage");seq+=1;emitted=True
        if not emitted:
            yield event(seq,raw,size,sidechain=sidechain,turn=turn,raw_type=typ);seq+=1

read_session=read
