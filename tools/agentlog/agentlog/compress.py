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

"""Deterministic repetition compression over normalized, redacted events.

Schema v1 stores ordered occurrences referencing distinct B blocks or V
variants. Variants have explicit earlier parents; their normalized bodies are
shared by all occurrences. Lossless reconstruction uses these bodies and each
occurrence's volatile values/spans. Non-lossless bodies are previews; first_seq
points back to the source event. Loop indexes exclude bookkeeping events.
"""
from collections import Counter, defaultdict
from dataclasses import asdict
import difflib
import hashlib
import json
from pathlib import Path
import sys

from .core.config import DEFAULT_MAX_TOKENS, add_common_arguments, default_out, hard_cap, validate_max_tokens
from .core.minhash import MinHash, jaccard, shingles
from .core.normalize import mask_volatile
from .core.out import dumps, summary, view_header
from .core.paths import checked_destination
from .core.redact import redact
from .core.tokens import est_tokens
from .readers.discover import read_session, resolve_session, input_paths
from .sequences import find_loops, find_sequences

_MARKERS = {"token_usage", "settings", "other"}
_EXCLUDED = {"token_usage", "settings"}


def preview(text, cap):
    """A bounded start/end preview, with the omitted character count."""
    if len(text) <= cap:
        return text
    marker = f"…[+{len(text)} chars]…"
    for _ in range(4):
        remaining = max(0, cap - len(marker))
        marker = f"…[+{len(text) - remaining} chars]…"
    remaining = max(0, cap - len(marker))
    if not remaining:
        return marker[:cap]
    head = (remaining + 1) // 2
    tail = remaining - head
    return text[:head] + marker + (text[-tail:] if tail else "")


class _Blocks:
    """Incremental exact dictionary and seeded LSH candidate index."""
    def __init__(self, near, minimum, body_cap, lossless):
        self.near = near
        self.minimum = minimum
        self.body_cap = body_cap
        self.lossless = lossless
        self.blocks = {}
        self.variants = {}
        self.entries = {}
        self.exact = {}
        self.full = {}
        self.sets = {}
        self.buckets = defaultdict(list)
        self.hasher = MinHash()

    def add(self, event, text, chunk, unit_index):
        masked, values = mask_volatile(text)
        key = (event.kind, event.tool, hashlib.sha1(masked.encode("utf-8")).digest())
        # Digests only select a bucket. Equality establishes exact identity,
        # including in the unlikely event of a digest collision.
        base = next((ref for ref in self.exact.get(key, ()) if self.full[ref] == masked), None)
        repeat_kind = "exact" if base is not None else "new"
        similarity, diff, parent = 1.0, [], None
        if base is not None and base in self.variants:
            entry = self.variants[base]
            parent = entry["parent"]
        token_count = est_tokens(text)
        signature = None
        features = None
        if base is None and token_count >= self.minimum:
            features = shingles(masked, mode="line" if event.kind == "tool_result" else "word")
            signature = self.hasher(features)
            candidates = set()
            for band in range(32):
                bucket = (event.kind, event.tool, band, signature[band * 4:(band + 1) * 4])
                candidates.update(self.buckets.get(bucket, ()))
            best = None
            for candidate in sorted(candidates, key=lambda ref: self.entries[ref]["creation_order"]):
                score = jaccard(features, self.sets[candidate])
                if score < self.near:
                    continue
                difference = list(difflib.unified_diff(
                    self.full[candidate].splitlines(), masked.splitlines(), n=0, lineterm=""))
                # Include hunk/file headers in the storage cost. A giant
                # one-line replacement is not a useful near reference.
                if sum(len(line) + 1 for line in difference) > len(masked) * 0.4:
                    continue
                if best is None or score > best[0]:
                    best = (score, candidate, difference)
            if best is not None:
                similarity, parent, difference = best
                repeat_kind = "near"
                diff = difference[:30]
                if len(difference) > 30:
                    diff[-1] = "…[diff capped at 30 lines; show source event for full]"
        if base is None:
            base = f"V{len(self.variants) + 1}" if parent else f"B{len(self.blocks) + 1}"
            self.full[base] = masked
            entry = {
                "ref": base, "kind": event.kind, "tool": event.tool,
                "creation_order": len(self.entries) + 1,
                "first_seq": event.seq, "masked_text": masked if self.lossless else preview(masked, self.body_cap),
                "body": preview(text, self.body_cap), "tokens": token_count,
                "occurrences": [], "exact_repeats": 0, "near_repeats": 0,
                "total_tokens": 0, "first_ref": f"E{event.seq}", "last_ref": f"E{event.seq}",
            }
            if parent:
                entry.update(parent=parent, similarity=similarity, diff=diff)
                self.variants[base] = entry
            else:
                self.blocks[base] = entry
            self.entries[base] = entry
            self.exact.setdefault(key, []).append(base)
            if signature is not None:
                self.sets[base] = features
                for band in range(32):
                    self.buckets[(event.kind, event.tool, band, signature[band * 4:(band + 1) * 4])].append(base)
        block = self.entries[base]
        block["occurrences"].append(unit_index)
        block["total_tokens"] += token_count
        block["last_ref"] = f"E{event.seq}"
        if repeat_kind != "new":
            block[repeat_kind + "_repeats"] += 1
        unit = {"block": base, "repeat_kind": repeat_kind, "values": list(values),
                "spans": values.spans, "diff": diff, "similarity": similarity,
                "placeholders": [masked[start:end] for start, end in values.spans],
                "chunk": chunk, "tokens": token_count}
        if parent:
            unit["parent"] = parent
        return unit


def _chunks(event, chunk_lines, unit):
    if not event.text:
        return []
    lines = event.text.splitlines(keepends=True)
    if (event.kind == "tool_result" or unit == "chunk") and len(lines) > chunk_lines:
        return ["".join(lines[start:start + chunk_lines]) for start in range(0, len(lines), chunk_lines)]
    return [event.text]


def _changed_values(occurrences, indexes, period):
    """Return only volatile slots whose values vary across iterations."""
    rows = []
    for start in range(0, len(indexes), period):
        slots = {}
        for offset, index in enumerate(indexes[start:start + period]):
            for chunk, unit in enumerate(occurrences[index]["units"]):
                for number, value in enumerate(unit["values"]):
                    placeholder = unit["placeholders"][number]
                    slots[f"event {offset + 1} chunk {chunk + 1} {placeholder}#{number + 1}"] = value
        rows.append(slots)
    keys = sorted({key for row in rows for key in row})
    changed = [key for key in keys if len({row.get(key) for row in rows}) > 1]
    return [{key: row.get(key) for key in changed} for row in rows]


def compress_session(meta, events, *, unit="event", chunk_lines=40, near=0.85,
                     min_block_tokens=30, body_cap=1200, max_period=12,
                     min_reps=3, lossless=False):
    """Consume a normalized event iterable once; return schema-v1 report data.

    Full text is retained only once per distinct block or variant. The
    required occurrence table and alphabet
    are linear compact metadata; there is no second list of full Events.
    """
    if unit not in ("event", "chunk") or chunk_lines < 1 or body_cap < 0:
        raise ValueError("unit must be event or chunk; chunk_lines positive and body_cap nonnegative")
    if not 0 <= near <= 1 or min_block_tokens < 0 or max_period < 1 or min_reps < 2:
        raise ValueError("near must be in 0..1; minimum tokens nonnegative; max_period positive; min_reps >= 2")
    dictionary = _Blocks(near, min_block_tokens, body_cap, lossless)
    occurrences = []
    alphabet = []
    indexes = []
    weights = []
    novelty = defaultdict(lambda: [0, 0])
    counts = Counter()
    saved = Counter()
    unit_index = 0
    previous_seq = -1
    context_used = None
    pending_compactions = []
    for event in events:
        if event.seq <= previous_seq:
            raise ValueError("Events must be in strictly increasing seq order")
        previous_seq = event.seq
        occurrence = {name: value for name, value in asdict(event).items() if name != "text"}
        occurrence["event"] = f"E{event.seq}"
        occurrence["units"] = []
        novelty[event.turn]  # Include turns containing only empty markers.
        if event.usage and event.usage.context_used is not None:
            context_used = event.usage.context_used
            for pending in pending_compactions:
                occurrences[pending]["context_after"] = context_used
            pending_compactions.clear()
        if event.kind == "compaction":
            occurrence["context_before"] = context_used
            occurrence["context_after"] = None
            pending_compactions.append(len(occurrences))
        if event.kind in _MARKERS:
            novelty[event.turn][1] += event.est_tokens or est_tokens(event.text)
            # Markers never enter the block dictionary, but preserve their
            # complete text in lossless mode, including literal placeholders.
            if lossless:
                occurrence["marker_text"] = event.text
        else:
            for number, text in enumerate(_chunks(event, chunk_lines, unit)):
                item = dictionary.add(event, text, number, unit_index)
                unit_index += 1
                occurrence["units"].append(item)
                counts[item["repeat_kind"]] += 1
                novelty[event.turn][1] += item["tokens"]
                if item["repeat_kind"] == "new":
                    novelty[event.turn][0] += item["tokens"]
                else:
                    saved[item["repeat_kind"]] += max(0, item["tokens"] - est_tokens(" ".join(item["diff"])) - 8)
        occurrences.append(occurrence)
        if event.kind not in _EXCLUDED:
            blocks = tuple(item["block"] for item in occurrence["units"]) or None
            repeat = tuple(item["repeat_kind"] for item in occurrence["units"]) or None
            parents = tuple(item.get("parent") for item in occurrence["units"]) or None
            alphabet.append((event.kind, event.tool if blocks else None, blocks, repeat, parents))
            indexes.append(len(occurrences) - 1)
            weights.append(event.est_tokens or est_tokens(event.text))
    loops = find_loops(alphabet, weights, max_period=max_period, min_reps=min_reps)
    for loop in loops:
        selected = indexes[loop["start"]:loop["end"]]
        loop.update(start_seq=occurrences[selected[0]]["seq"], end_seq=occurrences[selected[-1]]["seq"],
                    start_ref=occurrences[selected[0]]["event"], end_ref=occurrences[selected[-1]]["event"],
                    occurrence_indexes=selected,
                    variations=_changed_values(occurrences, selected, loop["period"]))
        outcomes = []
        for start in range(0, len(selected), loop["period"]):
            outcomes.append(tuple((occurrences[i]["exit_code"],
                                   tuple(u["block"] for u in occurrences[i]["units"])
                                   if occurrences[i]["is_error"] or occurrences[i]["exit_code"] not in (0, None) else None)
                                  for i in selected[start:start + loop["period"]]
                                  if occurrences[i]["kind"] == "tool_result"))
        loop["no_change_in_outcome"] = bool(outcomes[0]) and all(outcome == outcomes[0] for outcome in outcomes)
    patterns = find_sequences(alphabet, loops, min_reps=3)
    for pattern in patterns:
        pattern["event_refs"] = [occurrences[indexes[index]]["event"] for index in pattern["occurrences"]]
    top = sorted(dictionary.entries.values(), key=lambda b: (-b["total_tokens"], b["creation_order"]))[:15]
    stats = {"events": len(occurrences), "blocks": len(dictionary.blocks),
             "variants": len(dictionary.variants),
             "exact_repeats": counts["exact"], "near_repeats": counts["near"],
             "loops": len(loops), "sequences": len(patterns),
             "raw_tokens": (meta.bytes * 2 + 6) // 7,
             "tokens_saved": {"exact": saved["exact"], "near": saved["near"],
                              "loops": sum(loop["tokens_saved"] for loop in loops)},
             "novelty_per_turn": [{"turn": turn, "new_block_tokens": values[0], "all_tokens": values[1],
                                   "novelty_ratio": values[0] / values[1] if values[1] else 0}
                                  for turn, values in sorted(novelty.items())],
             "top_blocks": [block["ref"] for block in top]}
    return {"schema_version": 1, "session": asdict(meta), "lossless": lossless,
            "options": {"unit": unit, "chunk_lines": chunk_lines, "near": near,
                        "min_block_tokens": min_block_tokens, "body_cap": body_cap,
                        "max_period": max_period, "min_reps": min_reps},
            "blocks": dictionary.blocks, "variants": dictionary.variants, "occurrences": occurrences,
            "alphabet_occurrence_indexes": indexes,
            "loops": loops, "sequences": patterns, "stats": stats}


def _safe_inline(value):
    return str(value).replace("\n", " ").replace("\r", " ").replace("|", "\\|")


def _definition(data, ref):
    """Resolve a body reference without inferring ancestry from its number."""
    return data["variants"][ref] if ref.startswith("V") else data["blocks"][ref]


def _event_ranges(data, indexes):
    """Encode exact loop positions, coalescing only consecutive E numbers."""
    ranges = []
    for index in indexes:
        seq = data["occurrences"][index]["seq"]
        if ranges and ranges[-1][1] + 1 == seq:
            ranges[-1][1] = seq
        else:
            ranges.append([seq, seq])
    return ", ".join(f"E{first}" if first == last else f"E{first}–E{last}" for first, last in ranges)


def _event_parts(data, occurrence, seen, body_cap):
    """Return mandatory reference lines and optional text previews separately."""
    ref = occurrence["event"]
    kind = occurrence["kind"]
    units = occurrence["units"]
    label = occurrence["tool"] or occurrence["role"] or kind
    suffix = f" exit={occurrence['exit_code']}" if occurrence["exit_code"] is not None else ""
    if kind == "compaction":
        before = occurrence.get("context_before")
        after = occurrence.get("context_after")
        suffix += f" context {before if before is not None else '?'} → {after if after is not None else '?'}"
    if occurrence["is_error"] or occurrence["exit_code"] not in (None, 0):
        suffix += " ERROR"
    if not units:
        return [(f"{ref} {kind}{suffix}\n", False)]
    lines = []
    for item in units:
        block = item["block"]
        prefix = f"{ref}" + (f" chunk {item['chunk'] + 1}" if len(units) > 1 else "")
        if block not in seen:
            seen.add(block)
            definition = _definition(data, block)
            ancestry = (f" ≈ {definition['parent']} {definition['similarity']:.0%} (similarity parent)"
                        if "parent" in definition else "")
            lines.append((f"{prefix} ⟦{block}⟧ {label} {kind}{suffix} ({item['tokens']}t){ancestry}\n", False))
            body = preview(definition["body"], body_cap)
            detail = ["    " + line for line in body.splitlines()]
            if len(definition["body"]) >= body_cap and body_cap:
                detail.append(f"    [show {ref} for full]")
            if detail:
                lines.append(("\n".join(detail) + "\n", True))
            if "parent" in definition and definition["diff"]:
                lines.append(("\n".join("    " + line for line in definition["diff"]) + "\n", True))
        elif item["repeat_kind"] == "near":
            lines.append((f"{prefix} ≈ {block} {item['similarity']:.0%}{suffix} (masked; show {ref} for full diff)\n", False))
            if item["diff"]:
                lines.append(("\n".join("    " + line for line in item["diff"]) + "\n", True))
        else:
            block_data = _definition(data, block)
            lines.append((f"{prefix} ↻ {block} exact{suffix} ({len(block_data['occurrences'])} occurrences)\n", False))
    return lines


def render_compress(data, *, max_tokens=8000):
    """Preserve structure, then spend remaining target budget on detail."""
    validate_max_tokens(max_tokens)
    lines = ["## Repetition cost (top 15)",
             "| ref | what | occurrences | tokens each | tokens total | first → last |",
             "| --- | --- | --- | --- | --- | --- |"]
    for ref in data["stats"]["top_blocks"]:
        block = _definition(data, ref)
        lines.append(f"| {ref} | {_safe_inline(block['tool'] or block['kind'])} | "
                     f"{block['exact_repeats']} exact + {block['near_repeats']} near | {block['tokens']} | "
                     f"{block['total_tokens']} | {block['first_ref']} → {block['last_ref']} |")
    for loop in sorted(data["loops"], key=lambda item: (-item["tokens_saved"], item["start"]))[:15]:
        lines.append(f"| {loop['ref']} | loop period {loop['period']} | ×{loop['repetitions']} | "
                     f"{loop['tokens_saved'] // max(1, loop['repetitions'] - 1)} / iter | "
                     f"{loop['tokens_saved']} saved | {loop['start_ref']} → {loop['end_ref']} |")
    parts = [("\n".join(lines) + "\n\n", True), ("## Transcript\n", False)]
    lines = []
    loop_starts = {loop["occurrence_indexes"][0]: loop for loop in data["loops"]}
    hidden = set()
    for loop in data["loops"]:
        hidden.update(loop["occurrence_indexes"][loop["period"]:])
    annotations = defaultdict(list)
    indexes = data["alphabet_occurrence_indexes"]
    for pattern in data["sequences"]:
        for number, position in enumerate(pattern["occurrences"], 1):
            annotations[indexes[position]].append(f"[{pattern['ref']} occ {number}/{len(pattern['occurrences'])}]")
    seen = set()
    turn = None
    for index, occurrence in enumerate(data["occurrences"]):
        if turn != occurrence["turn"]:
            turn = occurrence["turn"]
            lines.append(f"── turn {turn} ── ({occurrence['event']})")
        loop = loop_starts.get(index)
        if loop:
            lines.append(f"{loop['start_ref']} ⟦{loop['ref']}⟧ LOOP period {loop['period']} × {loop['repetitions']} "
                         f"({loop['start_ref']}–{loop['end_ref']}, {loop['tokens_saved']}t saved)")
            lines.append("    cycle: " + " → ".join(_safe_inline(symbol[1] or symbol[0]) for symbol in loop["cycle"]))
            lines.append("    events: " + _event_ranges(data, loop["occurrence_indexes"]))
            parts.append(("\n".join(lines) + "\n", False))
            lines = []
            variation_lines = ["    | iter | events | ts | varied values |"]
            groups = []
            for iteration, variation in enumerate(loop["variations"]):
                if groups and groups[-1][2] == variation:
                    groups[-1] = (groups[-1][0], iteration, variation)
                else:
                    groups.append((iteration, iteration, variation))
            for first, last, variation in groups:
                selected = loop["occurrence_indexes"]
                start = data["occurrences"][selected[first * loop["period"]]]
                label = str(first + 1) if first == last else f"iters {first + 1}–{last + 1}: identical"
                values = "; ".join(f"{key}={_safe_inline(value)}" for key, value in variation.items()) or "identical"
                positions = _event_ranges(data, selected[first * loop["period"]:(last + 1) * loop["period"]])
                variation_lines.append(f"    | {label} | {positions} | {start['ts'] or ''} | {values} |")
            # Event positions stay mandatory, but changing values and detailed
            # iteration rows are optional. Prefer the complete table when it
            # fits; otherwise offer a compact pointer to the complete JSON.
            parts.append(("\n".join(variation_lines) + "\n",
                          f"    [{loop['ref']} variations: complete data in compress.json]\n"))
            if loop["no_change_in_outcome"]:
                lines.append(f"    ↳ no change in outcome across {loop['repetitions']} iterations")
        # Errors, compactions, bookkeeping and turn boundaries remain visible
        # even inside a collapsed loop. Bodies of ordinary iterations do not.
        if (index in hidden and occurrence["kind"] not in (_MARKERS | {"compaction", "error", "user_msg"})
                and not occurrence["is_error"] and occurrence["exit_code"] in (None, 0)):
            if lines:
                parts.append(("\n".join(lines) + "\n", False))
                lines = []
            continue
        if annotations[index]:
            lines.append(" ".join(annotations[index]))
        if lines:
            parts.append(("\n".join(lines) + "\n", False))
            lines = []
        parts.extend(_event_parts(data, occurrence, seen, data["options"]["body_cap"]))
    # Redact before measuring: redaction may change character counts.
    parts = [(redact(text)[0], redact(optional)[0] if isinstance(optional, str) else optional)
             for text, optional in parts]
    mandatory = "".join(text for text, optional in parts if not optional)
    minimum = _compress_report(data, mandatory, max_tokens)
    if est_tokens(minimum) > max_tokens:
        return minimum.split("\n", 1)[1]
    body_length = len(mandatory)
    selected = []
    for text, optional in parts:
        if optional:
            if _report_size("compress", data, body_length + len(text)) > max_tokens:
                if not isinstance(optional, str):
                    continue
                text = optional
                if _report_size("compress", data, body_length + len(text)) > max_tokens:
                    continue
            body_length += len(text)
        selected.append(text)
    return _compress_report(data, "".join(selected), max_tokens).split("\n", 1)[1]


def render_stats(data):
    stats = data["stats"]
    lines = ["## Compression statistics"]
    for name in ("raw_tokens", "view_tokens", "ratio", "events", "blocks", "variants", "exact_repeats", "near_repeats", "loops", "sequences"):
        if name in stats:
            lines.append(f"{name}: {stats[name]}")
    lines.append("Tokens saved per mechanism (estimates overlap): " + json.dumps(stats["tokens_saved"], sort_keys=True))
    lines.extend(["", "| turn | new-block tokens | all tokens | novelty ratio |", "| --- | --- | --- | --- |"])
    for row in stats["novelty_per_turn"]:
        lines.append(f"| {row['turn']} | {row['new_block_tokens']} | {row['all_tokens']} | {row['novelty_ratio']:.6f} |")
    lines.extend(["", "## Top 15 blocks by total tokens"])
    for ref in stats["top_blocks"]:
        block = _definition(data, ref)
        lines.append(f"{ref}: {block['total_tokens']}t, {len(block['occurrences'])} occurrences")
    return "\n".join(lines) + "\n"


def _bounded_report(command, data, body, max_tokens):
    """Budget including the required header, preserving its exact token count."""
    if command == "compress":
        return _compress_report(data, body, max_tokens)
    raw = data["stats"]["raw_tokens"]
    session = data["session"]["session_id"]
    # Compute a conservative header reserve; digit counts converge quickly.
    header = view_header(command, session, raw, max_tokens)
    available = (max_tokens * 7) // 2 - len(header) - 1
    if available < 1:
        raise ValueError("--max-tokens is too small for the required view header")
    if len(body) > available:
        marker = "\n[view truncated; compress.json retains all event positions and refs]\n"
        body = body[:max(0, available - len(marker))] + marker[:available]
    text = header + "\n" + body
    for _ in range(4):
        header = view_header(command, session, raw, est_tokens(text))
        text = header + "\n" + body
    return text


_OVERFLOW_PREFIX = "[Mandatory structure exceeds --max-tokens target: "


def _report_size(command, data, body_length):
    """Compute the fixed-point size of a header and a body without copies."""
    tokens = 0
    while True:
        header = view_header(command, data["session"]["session_id"], data["stats"]["raw_tokens"], tokens)
        updated = ((len(header) + 1 + body_length) * 2 + 6) // 7
        if updated == tokens:
            return tokens
        tokens = updated


def _compress_report(data, body, target):
    """Attach an exact header and required overflow notice; never truncate."""
    validate_max_tokens(target)
    # render_compress returns the view body, including an overflow notice. A
    # subsequent header attachment must count that notice exactly once.
    if body.startswith(_OVERFLOW_PREFIX):
        body = body.split("\n", 1)[1]
    tokens = _report_size("compress", data, len(body))
    notice = ""
    if tokens > target:
        while True:
            notice = f"{_OVERFLOW_PREFIX}target {target}, actual {tokens} estimated tokens; optional detail omitted.]\n"
            updated = _report_size("compress", data, len(notice) + len(body))
            if updated == tokens:
                break
            tokens = updated
    cap = hard_cap()
    if tokens > cap:
        raise ValueError(f"compress mandatory structure requires an estimated minimum of {tokens} tokens "
                         f"including header and notice; AGENTLOG_HARD_CAP is {cap}. "
                         "Raise AGENTLOG_HARD_CAP if appropriate; no partial view was emitted.")
    return view_header("compress", data["session"]["session_id"], data["stats"]["raw_tokens"], tokens) + "\n" + notice + body


def _write_reports(directory, meta, data, rendered, stats_rendered):
    """Apply core destination safeguards without its strict-target truncator."""
    targets = [directory / name for name in ("compress.md", "compress.json", "compress-stats.md")]
    for target in targets:
        checked_destination(target, inputs=(meta.path,))
        if target.is_symlink() or (target.exists() and target.stat().st_nlink > 1):
            raise ValueError("Report files must not be symlinks or hard links")
    contents = (rendered, dumps(data) + "\n", stats_rendered)
    # All views, serialization and validation complete before the first write.
    directory.mkdir(parents=True, exist_ok=True)
    for target, content in zip(targets, contents):
        target.write_text(content, encoding="utf-8", newline="\n")


def _run(args):
    path = resolve_session(args.session, args.root, source=args.source)
    stream = read_session(path, source=args.source, threads_csv=getattr(args, 'threads_csv', None))
    meta = next(stream)
    data = compress_session(meta, stream, unit=args.unit, chunk_lines=args.chunk_lines,
                            near=args.near, min_block_tokens=args.min_block_tokens,
                            body_cap=args.body_cap, max_period=args.max_period,
                            min_reps=args.min_reps, lossless=args.lossless)
    inputs = input_paths([meta], getattr(args, 'threads_csv', None))
    directory = checked_destination(args.out or default_out(meta.session_id), inputs=inputs)
    for name in ('compress.md', 'compress.json', 'compress-stats.md'):
        checked_destination(directory / name, inputs=inputs)
    stats_candidate = directory / "compress-stats.md"
    stats_path = checked_destination(stats_candidate, inputs=(path,))
    if stats_candidate.is_symlink() or (stats_path.exists() and stats_path.stat().st_nlink > 1):
        raise ValueError("Report files must not be symlinks or hard links")
    body = render_compress(data, max_tokens=args.max_tokens)
    rendered = _bounded_report("compress", data, redact(body)[0], args.max_tokens)
    data["stats"]["view_tokens"] = est_tokens(rendered)
    raw = data["stats"]["raw_tokens"]
    data["stats"]["ratio"] = est_tokens(rendered) / raw if raw else 0
    if data["stats"]["view_tokens"] > args.max_tokens:
        data["stats"]["target_overflow"] = (f"Mandatory structure exceeds --max-tokens target: target {args.max_tokens}, "
                                             f"actual {data['stats']['view_tokens']} estimated tokens; optional detail omitted.")
    stats_rendered = _bounded_report("compress-stats", data, render_stats(data), min(8000, hard_cap()))
    _write_reports(directory, meta, data, rendered, stats_rendered)
    if args.print_view:
        print(rendered, end="")
        if "target_overflow" in data["stats"]:
            print(data["stats"]["target_overflow"], file=sys.stderr)
    else:
        summary({key: value for key, value in data["stats"].items() if key not in ("novelty_per_turn", "top_blocks")}, json_mode=args.json)
    return 0


def register(subparsers):
    parser = subparsers.add_parser("compress", help="Compress repetition while preserving its evidence",
        description="Compress repetition while preserving mandatory transcript structure. The token target "
                    "covers the complete view, including its header. Mandatory structure may exceed the target; "
                    "optional detail cannot cause overflow. AGENTLOG_HARD_CAP (default 20000) is an absolute "
                    "hard cap including header and overflow notice. Refuses with exit 2 and no partial view "
                    "if mandatory structure cannot fit; raise AGENTLOG_HARD_CAP if appropriate.")
    parser.add_argument("session")
    parser.add_argument("--root", action="append", type=Path)
    parser.add_argument("--source", choices=("codex", "claude", "generic", "requestdump"))
    parser.add_argument("--unit", choices=("event", "chunk"), default="event")
    parser.add_argument("--chunk-lines", type=int, default=40)
    parser.add_argument("--near", type=float, default=0.85)
    parser.add_argument("--min-block-tokens", type=int, default=30)
    parser.add_argument("--body-cap", type=int, default=1200)
    parser.add_argument("--max-period", type=int, default=12)
    parser.add_argument("--min-reps", type=int, default=3)
    parser.add_argument("--lossless", action="store_true")
    parser.add_argument("--print", dest="print_view", action="store_true")
    add_common_arguments(parser)
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS,
                        help="Target for the complete view; mandatory structure may exceed it up to AGENTLOG_HARD_CAP")
    parser.set_defaults(func=_run)
