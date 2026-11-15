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

"""Codex rollout reader, including modern per-response usage records.

The token-ledger format reference establishes that modern records are complete
within a file, including requests missing from legacy token counts. Therefore
any modern record selects modern usage for the entire file, regardless of record
order or whether usage figures happen to match. Artificially spliced or partial
mixed-format files do not satisfy that premise and cannot recover missing usage.

Legacy-only files skip repeated cumulative totals and derive usage from successive
totals. The first count (including continuation files), or a counter reset, uses
last_token_usage. Reads remain streaming: the metadata pass also selects format.
"""
from collections import OrderedDict
import hashlib
import json
import re
from .base import Usage, as_int, as_str, base_meta, event, fallback_id, text_of, utc
from ..core.jsonl import iter_records

_CONTEXT = re.compile(r"^\s*(?:# AGENTS\.md instructions|<environment_context>|<user_instructions>|<permissions\b|<INSTRUCTIONS>|<system-reminder>)", re.I)
_ERROR = re.compile(r"rate limit|usage limit|quota|context length|context window", re.I)

def _dict(value): return value if isinstance(value, dict) else {}

def _usage(value, cumulative=None):
    if not isinstance(value, dict): return None
    return Usage(input=as_int(value.get("input_tokens")), cached_input=as_int(value.get("cached_input_tokens")),
                 output=as_int(value.get("output_tokens")), reasoning=as_int(value.get("reasoning_output_tokens")),
                 total=as_int(value.get("total_tokens")), context_used=as_int(value.get("input_tokens")),
                 cumulative_total=as_int(_dict(cumulative).get("total_tokens")))

def _inspect(path):
    meta = {}; settings = {}; started = ended = None; context = None; modern = False
    for _, raw, _ in iter_records(path):
        typ = raw.get("type"); p = _dict(raw.get("payload")); ts = utc(raw.get("timestamp"))
        if typ == "token_usage_record": modern = True
        if ts: started = started or ts; ended = ts
        if typ == "session_meta" and not meta: meta = p
        if typ == "turn_context" and not settings: settings = p
        if p.get("type") == "token_count": context = as_int(_dict(p.get("info")).get("model_context_window")) or context
    instructions = meta.get("base_instructions")
    if isinstance(instructions, dict): instructions = instructions.get("text")
    git = _dict(meta.get("git"))
    source = meta.get("thread_source") or meta.get("source")
    if isinstance(source, dict): source = "subagent" if "subagent" in source else json.dumps(source, sort_keys=True)
    model = settings.get("model") or meta.get("model")
    effort = settings.get("effort") or settings.get("reasoning_effort")
    if not effort: effort = _dict(_dict(settings.get("collaboration_mode")).get("settings")).get("reasoning_effort")
    result = base_meta(path, "codex", session_id=str(meta.get("id") or fallback_id(path)),
        cwd=meta.get("cwd") or settings.get("cwd"), model=model, reasoning_effort=effort,
        cli_version=meta.get("cli_version"), originator=meta.get("originator"), thread_source=source,
        started=utc(meta.get("timestamp")) or started, ended=ended, title=meta.get("title"),
        git_branch=git.get("branch"), git_commit=git.get("commit_hash"), context_window=context,
        instructions_sha1=hashlib.sha1(instructions.encode("utf-8")).hexdigest() if isinstance(instructions,str) else None)
    return result, source == "subagent" or bool(meta.get("parent_thread_id")), modern

_TOTAL_FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
                 "output_tokens", "reasoning_output_tokens", "total_tokens")

def _legacy_usage(info, previous):
    if not isinstance(info, dict): return None, previous
    total = info.get("total_token_usage")
    current = ({field: as_int(total.get(field)) for field in _TOTAL_FIELDS}
               if isinstance(total, dict) else None)
    if current is not None and current == previous: return None, previous
    value = info.get("last_token_usage")
    if current is not None and previous is not None:
        difference = {field: (current[field] - previous[field]
                             if current[field] is not None and previous[field] is not None else None)
                      for field in _TOTAL_FIELDS}
        if not any(number is not None and number < 0 for number in difference.values()):
            value = difference
    return _usage(value, total), current if current is not None else previous

def read(path):
    meta, sidechain, modern = _inspect(path)
    yield meta
    turn = 0; seq = 0; calls = OrderedDict(); previous_total = None
    for _, raw, size in iter_records(path):
        typ = str(raw.get("type", "")); p = _dict(raw.get("payload")); sub = str(p.get("type", ""))
        raw_type = typ + ("/" + sub if sub else "")
        kw = dict(kind="other", text="", raw_type=raw_type, turn=turn, sidechain=sidechain)
        if "compact" in (typ + "/" + sub + "/" + str(raw.get("event", ""))).lower():
            kw.update(kind="compaction", text=p.get("summary") or p.get("message") or p.get("replacement_history") or p.get("compaction_response_id") or "")
        elif typ == "response_item":
            role = as_str(p.get("role"))
            if sub in ("message", "agent_message"):
                text = text_of(p.get("content") or p.get("text") or p.get("message"))
                kind = "context" if role in ("system", "developer") or (role == "user" and _CONTEXT.search(text)) else {"user":"user_msg", "assistant":"assistant_msg"}.get(role,"other")
                if kind == "user_msg":
                    turn += 1
                kw.update(kind=kind, role=role, text=text, turn=turn)
            elif sub == "reasoning":
                text = text_of(p.get("summary"))
                if not text and p.get("encrypted_content") is not None: text = f"[encrypted {len(str(p['encrypted_content']).encode('utf-8'))} bytes]"
                kw.update(kind="reasoning", role="assistant", text=text)
            elif sub in ("function_call", "custom_tool_call"):
                call_id = as_str(p.get("call_id")); tool = as_str(p.get("name"))
                if call_id:
                    calls[call_id] = tool
                    if len(calls)>1024: calls.popitem(last=False)
                kw.update(kind="tool_call", role="assistant", tool=tool, call_id=call_id, text=p.get("arguments",p.get("input","")))
            elif sub in ("function_call_output", "custom_tool_call_output"):
                call_id = as_str(p.get("call_id")); text = text_of(p.get("output"))
                exit_code = as_int(p.get("exit_code")); match = re.search(r"(?:Process exited with code|exit(?:[_ ]code)?\s*[:=])\s*(-?\d+)",text,re.I)
                if exit_code is None and match: exit_code = int(match[1])
                kw.update(kind="tool_result", role="tool", tool=calls.pop(call_id,None), call_id=call_id, text=text, exit_code=exit_code, is_error=bool(p.get("is_error")) or (exit_code is not None and exit_code!=0))
            elif sub == "additional_tools": kw.update(kind="settings", text=text_of(p))
        elif typ == "event_msg":
            text = text_of(p.get("message") or p.get("text"))
            if sub == "token_count":
                if not modern:
                    usage, previous_total = _legacy_usage(p.get("info"), previous_total)
                    if usage is not None: kw.update(kind="token_usage", usage=usage)
            elif sub in ("task_started", "task_complete"):
                kw.update(kind="task_start" if sub=="task_started" else "task_end", text=p.get("last_agent_message") or text)
            elif sub in ("thread_settings_applied", "additional_tools"): kw.update(kind="settings",text=text_of(p))
            elif "error" in sub.lower() or _ERROR.search(text): kw.update(kind="error",text=text,is_error=True)
        elif typ == "token_usage_record":
            usage = _usage(p.get("usage"),p.get("thread_token_usage"))
            kw.update(kind="token_usage",usage=usage)
        elif typ == "turn_context":
            kw.update(kind="settings",text=text_of(p))
        yield event(seq, raw, size, **kw); seq += 1

read_session = read
