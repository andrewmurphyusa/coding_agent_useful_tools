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

"""Compact event timeline with salient-turn sampling."""
from collections import defaultdict
import json
from pathlib import Path
import re
from .core.config import add_common_arguments, default_out
from .core.normalize import mask_volatile
from .core.out import dumps, summary, view_header, write_outputs
from .core.redact import redact
from .core.tokens import est_tokens
from .readers.discover import load_session, input_paths
from .show import _entries, _unit_metadata


def patch_summary(text):
    files=[]; current=None
    for line in text.splitlines():
        match=re.match(r"\*\*\* (?:Update|Add|Delete) File: (.+)",line)
        if match:
            current=[match.group(1),0,0]; files.append(current)
        elif current and line.startswith("+") and not line.startswith("+++"): current[1]+=1
        elif current and line.startswith("-") and not line.startswith("---"): current[2]+=1
    return ", ".join(f"{name} (+{add} \u2212{remove})" for name,add,remove in files)


def annotations(data):
    result=defaultdict(list)
    loop_notes=defaultdict(list)
    entries, occurrences, units = _unit_metadata(data)
    for ref, loop in _entries(data, "loops").items():
        period=loop.get("period"); reps=loop.get("repetitions")
        indexes=loop.get("occurrence_indexes")
        if (type(period) is not int or period <= 0 or
                type(reps) is not int or reps <= 0):
            raise ValueError("invalid compressor loop period or repetitions")
        if not isinstance(indexes,list) or len(indexes) != period * reps:
            raise ValueError("invalid compressor loop occurrence_indexes count")
        if not isinstance(occurrences,list):
            raise ValueError("invalid compressor occurrences")
        previous_index=-1; previous_seq=-1
        for position,index in enumerate(indexes):
            if type(index) is not int or index <= previous_index or index >= len(occurrences):
                raise ValueError("invalid compressor loop occurrence indexes")
            occ=occurrences[index]
            seq=occ.get("seq") if isinstance(occ,dict) else None
            if type(seq) is not int or seq <= previous_seq:
                raise ValueError("invalid compressor loop event sequences")
            loop_notes[seq].append(f"[{ref} iter {position//period+1}/{reps}]")
            previous_index=index; previous_seq=seq
    for occ, unit in units:
        ref=unit["block"]
        count=len(entries[ref]["occurrences"])
        result[occ["seq"]].append(f"[{ref}"+(f" \u00d7{count}" if count>1 else "")+"]")
    for seq, notes in loop_notes.items(): result[seq].extend(notes)
    return result


def line_for(e, meta, raw_values, notes):
    label=f"E{e.seq} {e.kind}"
    if e.tool: label += " " + e.tool
    if e.kind == "context":
        label += " ("+("AGENTS.md" if "AGENTS.md" in e.text else "context")+f", {e.est_tokens:,}t)"
    elif e.kind != "token_usage": label += f" {e.est_tokens:,}t"
    if e.kind in ("tool_call","user_msg"):
        text=patch_summary(e.text) if e.tool and "patch" in e.tool.lower() else ""
        text=text or " ".join(e.text.split())
        if not raw_values: text=mask_volatile(text)[0]
        if len(text)>100: text=text[:99]+"\u2026"
        if text: label += ' "'+text+'"'
    elif e.kind == "tool_result" and e.exit_code is not None: label += f" exit={e.exit_code}"
    elif e.kind == "token_usage" and e.usage:
        u=e.usage
        used=f"{u.context_used:,}" if u.context_used is not None else "?"
        window=f"{meta.context_window:,}" if meta.context_window is not None else "?"
        percent=(f" ({u.context_used / meta.context_window:.0%})"
                 if u.context_used is not None and meta.context_window is not None and meta.context_window>0 else "")
        total=("?" if u.cumulative_total is None else
               f"{u.cumulative_total / 1_000_000:.1f}M" if u.cumulative_total>=1_000_000 else
               f"{u.cumulative_total:,}")
        label += f" ctx {used} / {window}{percent} \u00b7 \u03a3 {total}"
    return label + (" " + " ".join(notes.get(e.seq,[])) if notes.get(e.seq) else "")


def render(meta, events, max_tokens, raw_values=False, notes=None):
    notes=notes or {}; groups={}; peak=None
    for e in events:
        if e.usage and e.usage.context_used is not None and (peak is None or e.usage.context_used>peak.usage.context_used): peak=e
        group=groups.setdefault(e.turn,[])
        if e.kind=="token_usage" and group and group[-1].kind=="token_usage": group[-1]=e
        else: group.append(e)
    keys=sorted(groups); mandatory={e.turn for e in events if e.kind in ("compaction","error")}
    if peak: mandatory.add(peak.turn)
    if keys: mandatory.update((keys[0],keys[-1]))
    rendered={turn:redact("\n".join([f"\u2500\u2500 turn {turn} \u00b7 {groups[turn][0].ts or '?'} \u2500\u2500"] + [line_for(e,meta,raw_values,notes) for e in groups[turn]]))[0] for turn in keys}
    def body_for(selected):
        lines=[rendered[k] for k in keys if k in selected]
        omitted=len(keys)-len(selected)
        if omitted: lines.append(f"\u2026 {omitted} turns omitted (use --turns)")
        return "\n".join(lines)

    def fits(selected, exact=False):
        body=body_for(selected)
        if exact: body=redact(body)[0]
        raw_tokens=(meta.bytes*2+6)//7
        header=view_header("skeleton",meta.session_id,raw_tokens,max_tokens)
        report=header+"\n"+body
        for _ in range(4):
            header=view_header("skeleton",meta.session_id,raw_tokens,est_tokens(report))
            report=header+"\n"+body
        # Account for both text stdout and JSON escaping, including print's newline.
        serialized=dumps({"view":body}) if exact else json.dumps({"view":body},ensure_ascii=False,sort_keys=True)
        return (est_tokens(report)<=max_tokens and
                est_tokens(body)<=max_tokens-est_tokens(header+"\n") and
                est_tokens(body+"\n")<=max_tokens and
                est_tokens(serialized+"\n")<=max_tokens)

    selected=set(keys)
    if not fits(selected):
        selected=set(mandatory)
        if not fits(selected):
            raise ValueError("--max-tokens is too small to include all mandatory skeleton turns")
        positions={key:i for i,key in enumerate(keys)}
        remaining=set(keys)-selected
        distance={k:min(abs(positions[k]-positions[s]) for s in selected) for k in remaining}
        while remaining:
            candidate=max(remaining,key=lambda k:(distance[k],-positions[k])); remaining.remove(candidate)
            proposed=selected | {candidate}
            # Avoid repeatedly redacting and serializing obviously oversized views.
            header=view_header("skeleton",meta.session_id,(meta.bytes*2+6)//7,max_tokens)
            if est_tokens(header+"\n"+body_for(proposed))<=max_tokens and fits(proposed):
                selected.add(candidate)
                for key in remaining: distance[key]=min(distance[key],abs(positions[key]-positions[candidate]))
    if not fits(selected,exact=True):
        raise ValueError("--max-tokens is too small to include all mandatory skeleton turns")
    return body_for(selected)


def run(args):
    meta,events=load_session(args.session,args.root,threads_csv=getattr(args,'threads_csv',None))
    if args.range:
        match=re.fullmatch(r"E(\d+)-E(\d+)",args.range)
        if not match or int(match[1])>int(match[2]): raise ValueError("--range must be E<start>-E<end>")
        events=[e for e in events if int(match[1])<=e.seq<=int(match[2])]
    if args.turns:
        match=re.fullmatch(r"(\d+)-(\d+)",args.turns)
        if not match or int(match[1])>int(match[2]): raise ValueError("--turns must be <start>-<end>")
        events=[e for e in events if int(match[1])<=e.turn<=int(match[2])]
    directory=Path(args.out) if args.out else default_out(meta.session_id); notes={}; path=directory/"compress.json"
    from .core.paths import checked_destination
    for target in (directory, directory/'skeleton.md', directory/'skeleton.json'):
        checked_destination(target, inputs=input_paths([meta],getattr(args,'threads_csv',None)))
    if path.is_file(): notes=annotations(json.loads(path.read_text(encoding="utf-8")))
    body=render(meta,events,args.max_tokens,args.raw_values,notes)
    write_outputs("skeleton",meta,{"view":body},body,args.out,max_tokens=args.max_tokens)
    body=redact(body)[0]
    if args.json: summary({"view":body},json_mode=True)
    else: print(body)
    return 0


def register(subparsers):
    p=subparsers.add_parser("skeleton"); p.add_argument("session"); p.add_argument("--range"); p.add_argument("--turns"); p.add_argument("--root",action="append"); p.add_argument("--raw-values",action="store_true"); add_common_arguments(p,view=True); p.set_defaults(func=run)
