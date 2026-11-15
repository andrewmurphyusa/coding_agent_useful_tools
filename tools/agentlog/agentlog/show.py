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

"""Expand event and analysis references from local reports."""
import json
from dataclasses import replace
from pathlib import Path
import re
from .core.config import add_common_arguments, default_out, validate_max_tokens
from .core.out import dumps, view_header
from .core.redact import redact
from .core.refs import parse_ref
from .core.paths import checked_destination
from .core.tokens import est_tokens
from .readers.discover import load_session


def _entries(data, key):
    """Accept report maps/lists, rejecting explicit malformed containers."""
    if not isinstance(data, dict):
        raise ValueError("invalid compressor report: expected an object")
    items = data.get(key, {})
    if not isinstance(items, (dict, list)):
        raise ValueError(f"invalid compressor {key}: expected a map or list")
    result = {}
    for name, item in (items.items() if isinstance(items, dict) else
                       ((None, value) for value in items)):
        if not isinstance(item, dict):
            raise ValueError(f"invalid compressor {key} entry: expected an object")
        ref = item.get("ref", name)
        prefix = {"blocks": "B", "variants": "V", "loops": "L", "findings": "F"}[key]
        if (not isinstance(ref, str) or not re.fullmatch(prefix + r"\d+", ref)
                or (name is not None and name != ref) or ref in result):
            raise ValueError(f"invalid compressor {key} reference")
        result[ref] = item
    return result


def _find(data, key, ref):
    return _entries(data, key).get(ref)


def _unit_metadata(data):
    """Validate and flatten compressor units in their original source order."""
    entries = {**_entries(data, "blocks"), **_entries(data, "variants")}
    occurrences = data.get("occurrences", [])
    if not isinstance(occurrences, list):
        raise ValueError("invalid compressor occurrences: expected a list")
    flat = []
    actual = {ref: [] for ref in entries}
    previous_seq = -1
    for occurrence in occurrences:
        if not isinstance(occurrence, dict):
            raise ValueError("invalid compressor occurrence: expected an object")
        seq = occurrence.get("seq")
        if type(seq) is not int or seq <= previous_seq:
            raise ValueError("invalid compressor occurrence event sequences")
        previous_seq = seq
        units = occurrence.get("units", [])
        if not isinstance(units, list):
            raise ValueError("invalid compressor occurrence units: expected a list")
        for chunk, unit in enumerate(units):
            if not isinstance(unit, dict):
                raise ValueError("invalid compressor unit: expected an object")
            ref = unit.get("block")
            if not isinstance(ref, str) or ref not in entries:
                raise ValueError("invalid compressor unit block reference")
            if type(unit.get("chunk")) is not int or unit["chunk"] != chunk:
                raise ValueError("invalid compressor unit chunk index")
            if ref.startswith("V") and unit.get("parent") != entries[ref].get("parent"):
                raise ValueError("invalid compressor variant unit parent")
            actual[ref].append(len(flat))
            flat.append((occurrence, unit))
    for ref, item in entries.items():
        indexes = item.get("occurrences")
        if (not isinstance(indexes, list) or not indexes
                or any(type(index) is not int or index < 0 for index in indexes)
                or indexes != actual[ref]):
            raise ValueError(f"{ref}: invalid compressor occurrence indexes or multiplicity")
        if "first_seq" in item and (type(item["first_seq"]) is not int
                or item["first_seq"] != flat[indexes[0]][0]["seq"]):
            raise ValueError(f"{ref}: inconsistent first occurrence in compress.json")
    return entries, occurrences, flat


def _loop_events(data, item, by_seq, ref):
    """Resolve the first cycle using the compressor's occurrence indexes."""
    period = item.get("period")
    indexes = item.get("occurrence_indexes")
    occurrences = data.get("occurrences")
    if type(period) is not int or period < 1:
        raise ValueError(f"{ref}: invalid loop period in compress.json")
    if not isinstance(indexes, list) or len(indexes) < period:
        raise ValueError(f"{ref}: missing or incomplete occurrence_indexes in compress.json")
    if not isinstance(occurrences, list):
        raise ValueError(f"{ref}: missing occurrences in compress.json")
    selected = []
    previous_index = previous_seq = -1
    for position, index in enumerate(indexes):
        if type(index) is not int or not 0 <= index < len(occurrences):
            raise ValueError(f"{ref}: invalid occurrence index in compress.json")
        if index <= previous_index:
            raise ValueError(f"{ref}: occurrence indexes must be strictly increasing in compress.json")
        occurrence = occurrences[index]
        seq = occurrence.get("seq") if isinstance(occurrence, dict) else None
        if type(seq) is not int or seq < 0:
            raise ValueError(f"{ref}: invalid occurrence event sequence in compress.json")
        if seq <= previous_seq:
            raise ValueError(f"{ref}: occurrence event sequences must be strictly increasing in compress.json")
        if seq not in by_seq:
            raise ValueError(f"{ref}: event E{seq} from compress.json is missing from session")
        if position < period:
            selected.append(by_seq[seq])
        previous_index, previous_seq = index, seq
    if "repetitions" in item:
        reps = item["repetitions"]
        if type(reps) is not int or reps < 1 or len(indexes) != period * reps:
            raise ValueError(f"{ref}: invalid loop repetitions or occurrence count")
    return selected


def _source_view(data, item, by_seq, ref):
    """Resolve flattened unit indexes and recover the first source chunk."""
    entries, occurrences, units = _unit_metadata(data)
    parent = None
    if ref.startswith("V"):
        parent = item.get("parent")
        parent_item = entries.get(parent) if isinstance(parent, str) else None
        if (not isinstance(parent_item, dict) or type(item.get("creation_order")) is not int
                or type(parent_item.get("creation_order")) is not int
                or parent_item["creation_order"] < 1
                or parent_item["creation_order"] >= item["creation_order"]):
            raise ValueError(f"{ref}: invalid variant parent in compress.json")
    indexes = item.get("occurrences")
    options = data.get("options")
    if (not isinstance(options, dict) or type(options.get("chunk_lines")) is not int
            or options["chunk_lines"] < 1 or options.get("unit") not in ("event", "chunk")):
        raise ValueError(f"{ref}: invalid chunk settings in compress.json")
    resolved = []
    source_chunks = {}
    for index in indexes:
        occurrence, unit = units[index]
        seq = occurrence["seq"]
        if seq not in by_seq:
            raise ValueError(f"{ref}: missing session event E{seq}")
        if seq not in source_chunks:
            event = by_seq[seq]
            lines = event.text.splitlines(keepends=True)
            chunk_lines = options["chunk_lines"]
            chunks = (["".join(lines[start:start + chunk_lines])
                       for start in range(0, len(lines), chunk_lines)]
                      if len(lines) > chunk_lines and (event.kind == "tool_result" or options["unit"] == "chunk")
                      else [event.text])
            if len(occurrence["units"]) != len(chunks) or not event.text:
                raise ValueError(f"{ref}: invalid source chunk in session event")
            source_chunks[seq] = chunks
        resolved.append((seq, unit["chunk"]))
    if type(item.get("first_seq")) is not int or item["first_seq"] != resolved[0][0]:
        raise ValueError(f"{ref}: inconsistent first occurrence in compress.json")
    seq, chunk = resolved[0]
    event = by_seq[seq]
    info = {"ref": ref, "occurrences": [
        {"event": f"E{seq}", "chunk": chunk} for seq, chunk in resolved]}
    if parent is not None:
        info["parent"] = parent
    return info, replace(event, text=source_chunks[seq][chunk])


def _bounded_view(body, references, meta, max_tokens, json_mode):
    """Fit the actual redacted serialized stdout and the complete Markdown."""
    validate_max_tokens(max_tokens)
    body = redact(body)[0]
    references = json.loads(dumps(references))
    raw_tokens = (meta.bytes * 2 + 6) // 7

    def render(candidate):
        # Both fields were redacted before truncation. Serialize directly so
        # trial prefixes cannot change redaction boundaries or be rescanned.
        stdout = (json.dumps({"references": references, "view": candidate}, ensure_ascii=False, sort_keys=True)
                  if json_mode else candidate) + "\n"
        header = redact(view_header("show", meta.session_id, raw_tokens, 0))[0]
        report = header + "\n" + candidate
        while True:
            updated = redact(view_header("show", meta.session_id, raw_tokens, est_tokens(report)))[0] + "\n" + candidate
            if updated == report:
                break
            report = updated
        return stdout, report

    def fits(rendered):
        return all(est_tokens(text) <= max_tokens for text in rendered)

    complete = render(body)
    if fits(complete):
        return complete

    def truncated(keep):
        omitted = est_tokens(body[keep:])
        return body[:keep] + f"\n\u2026 truncated (+{omitted} tokens)"

    if not fits(render(truncated(0))):
        raise ValueError("--max-tokens is too small for the required show wrapper, header and full truncation marker")
    low, high = 0, len(body)
    while low < high:
        middle = (low + high + 1) // 2
        if fits(render(truncated(middle))):
            low = middle
        else:
            high = middle - 1
    return render(truncated(low))


def run(args):
    if args.context<0: raise ValueError("--context must be nonnegative")
    sessions={}; sections=[]; found=[]; missing=False; meta=None
    def session(ref):
        nonlocal meta
        if not ref: raise ValueError("--session is required for unqualified references")
        if ref not in sessions: sessions[ref]=load_session(ref,args.root)
        meta,events=sessions[ref]; return meta,events
    for requested in args.refs:
        range_match=re.fullmatch(r"(?:(?P<session>[A-Za-z0-9-]{8,}):)?E(?P<start>\d+)-E(?P<end>\d+)",requested)
        if range_match:
            start=int(range_match["start"]); end=int(range_match["end"])
            if start>end: raise ValueError("Event range start exceeds end")
            ref_session=range_match["session"] or args.session; kind="E"; numbers=list(range(start,end+1))
        else:
            ref=parse_ref(requested); ref_session=ref.session or args.session; kind=ref.kind; numbers=[ref.number]
        current,events=session(ref_session); by_seq={e.seq:e for e in events}; payload=[]
        if kind=="E":
            absent=[n for n in numbers if n not in by_seq]
            if absent:
                missing=True; sections.append(", ".join(f"E{n}" for n in absent)+": NOT FOUND")
            selected=set()
            for n in numbers:
                if n in by_seq: selected.update(range(n-args.context,n+args.context+1))
            payload=[by_seq[n] for n in sorted(selected) if n in by_seq]
        else:
            directory=Path(args.out) if args.out else default_out(current.session_id)
            filename="loops.json" if kind=="F" else "compress.json"; path=directory/filename
            data=json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
            if not isinstance(data, dict): raise ValueError(f"Invalid {filename}: expected an object")
            item=_find(data,{"B":"blocks","V":"variants","L":"loops","F":"findings"}[kind],kind+str(numbers[0]))
            if item is not None:
                payload=[item]
                if kind in ("B", "V"): payload=list(_source_view(data,item,by_seq,kind+str(numbers[0])))
                if kind=="L": payload.extend(_loop_events(data,item,by_seq,requested))
                if kind=="F":
                    evidence_list = item.get("evidence", [])
                    if not isinstance(evidence_list, list):
                        raise ValueError("invalid findings evidence: expected a list")
                    for index, evidence in enumerate(evidence_list):
                        value = evidence.get("ref") if isinstance(evidence, dict) else evidence
                        context = f"{requested} evidence[{index}]"
                        if not isinstance(value, str):
                            raise ValueError(f"{context}: invalid findings evidence reference: expected a string")
                        try:
                            evref = parse_ref(value)
                        except ValueError as exc:
                            raise ValueError(f"{context}: invalid findings evidence reference: {exc}") from exc
                        if evref.kind=="E" and evref.number in by_seq: payload.append(by_seq[evref.number])
            else: missing=True
        if not payload: sections.append(f"{requested}: NOT FOUND")
        else:
            found.extend(payload); sections.append("## "+requested+"\n"+"\n".join((f"E{x.seq} {x.kind} {x.tool or ''}\n{x.text}" if hasattr(x,"seq") else json.dumps(x,ensure_ascii=False,sort_keys=True)) for x in payload))
    stdout, markdown = _bounded_view("\n\n".join(sections), args.refs, meta, args.max_tokens, args.json)
    json_text = dumps({"references": args.refs, "results": found}) + "\n"
    directory=Path(args.out) if args.out else default_out(meta.session_id)
    for target in (directory, directory/"show.md", directory/"show.json"):
        checked_destination(target, inputs=[m.path for m, _ in sessions.values()])
        if target != directory and (target.is_symlink() or (target.exists() and target.stat().st_nlink > 1)):
            raise ValueError("Report files must not be symlinks or hard links")
    directory.mkdir(parents=True, exist_ok=True)
    (directory/"show.md").write_text(markdown, encoding="utf-8", newline="\n")
    (directory/"show.json").write_text(json_text, encoding="utf-8", newline="\n")
    print(stdout, end="")
    return 1 if missing else 0


def register(subparsers):
    p=subparsers.add_parser("show"); p.add_argument("refs",nargs="+"); p.add_argument("--session"); p.add_argument("--root",action="append"); p.add_argument("--context",type=int,default=1); add_common_arguments(p,view=True); p.set_defaults(func=run)
