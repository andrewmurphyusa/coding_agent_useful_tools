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

"""Basic fidelity for unknown role/content or type/text JSONL records."""
from .base import as_str, base_meta, cwd_prefix_matches, event, fallback_id, text_of, utc
from ..core.jsonl import iter_records

def read(path, cwd_prefixes=()):
    started=ended=None;session_id=None;cwd=None;model=None;cwd_match=False
    for _,raw,_ in iter_records(path):
        if cwd_prefixes: cwd_match = cwd_match or cwd_prefix_matches(raw.get('cwd'), cwd_prefixes)
        ts=utc(raw.get("timestamp"))
        if ts:started=started or ts;ended=ts
        session_id=session_id or raw.get("session_id") or raw.get("sessionId")
        cwd=cwd or raw.get("cwd");model=model or raw.get("model")
    yield base_meta(path,"generic",session_id=str(session_id or fallback_id(path)),cwd=cwd,model=model,started=started,ended=ended,extra={'cwd_prefix_match':cwd_match} if cwd_prefixes else {})
    turn=0
    for seq,(_,raw,size) in enumerate(iter_records(path)):
        role=as_str(raw.get("role"));typ=str(raw.get("type",""));text=text_of(raw.get("content",raw.get("text","")))
        kind={"user":"user_msg","assistant":"assistant_msg","system":"context","developer":"context","tool":"tool_result"}.get(role,"other")
        if kind=="other" and "text" in raw:kind={"user":"user_msg","assistant":"assistant_msg","error":"error"}.get(typ,"other")
        if kind=="user_msg":turn+=1
        yield event(seq,raw,size,kind=kind,role=role,text=text,turn=turn,raw_type=typ,is_error=kind=="error")

read_session=read
