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

"""Bounded tandem and non-adjacent sequence detection over event symbols.

Indexes in results are half-open alphabet indexes. Bookkeeping events should
be removed by the caller. The fourth symbol field describes the occurrence,
not its identity: a definition and later references match the same body. An
optional fifth field supplies each unit's explicit variant parent. Loops can
match sibling variants through that parent; sequences retain exact identity.
"""
from bisect import bisect_left


def _identity(symbol, *, near=False):
    def freeze(value):
        return tuple(freeze(x) for x in value) if isinstance(value, (list, tuple)) else value
    if isinstance(symbol, (list, tuple)):
        if near and len(symbol) > 4 and symbol[2] is not None and symbol[4] is not None:
            bodies = tuple(("parent", parent) if parent is not None else ("body", ref)
                           for ref, parent in zip(symbol[2], symbol[4]))
            return (symbol[0], symbol[1], bodies)
        return tuple(freeze(x) for x in symbol[:3])
    return symbol


def find_loops(symbols, tokens=None, *, max_period=12, min_reps=3):
    """Choose maximal, non-overlapping tandem repeats by estimated savings."""
    if max_period < 1 or min_reps < 2:
        raise ValueError("max_period must be positive and min_reps at least 2")
    keys = [_identity(s, near=True) for s in symbols]
    count = len(keys)
    weights = list(tokens) if tokens is not None else [1] * count
    if len(weights) != count:
        raise ValueError("tokens must have one weight per symbol")
    sums = [0]
    for weight in weights:
        sums.append(sums[-1] + weight)
    candidates = []
    for period in range(1, min(max_period, count // min_reps) + 1):
        index = 0
        while index + period < count:
            if keys[index] != keys[index + period]:
                index += 1
                continue
            start = index
            while index + period < count and keys[index] == keys[index + period]:
                index += 1
            repetitions = (index - start) // period + 1
            if repetitions >= min_reps:
                end = start + period * repetitions
                saved = sums[end] - sums[start + period]
                candidates.append((saved, period, start, end, repetitions))
    # A sorted interval list avoids an O(length-of-loop) membership operation
    # for every overlapping candidate (notably on a constant alphabet).
    selected = []
    starts = []
    for saved, period, start, end, repetitions in sorted(
            candidates, key=lambda c: (-c[0], c[1], c[2], c[3])):
        position = bisect_left(starts, start)
        if position and selected[position - 1]["end"] > start:
            continue
        if position < len(selected) and selected[position]["start"] < end:
            continue
        starts.insert(position, start)
        selected.insert(position, {"start": start, "end": end,
                                   "period": period, "repetitions": repetitions,
                                   "tokens_saved": saved,
                                   "cycle": list(symbols[start:start + period])})
    for number, loop in enumerate(selected, 1):
        loop["ref"] = f"L{number}"
    return selected


def find_sequences(symbols, loops=(), *, min_reps=3, max_length=8):
    """Find maximal repeated n-grams outside loops using rolling hashes.

    Hash collisions are verified against interned symbol tuples. A pattern is
    non-adjacent when its occurrences do not all form one tandem run. A shorter
    pattern is omitted only when *all* its occurrences extend into an already
    retained longer pattern; extra independent occurrences remain evidence.
    """
    if min_reps < 2 or not 2 <= max_length <= 8:
        raise ValueError("min_reps must be at least 2 and max_length in 2..8")
    identities = [_identity(s) for s in symbols]
    intern = {}
    ids = [intern.setdefault(key, len(intern) + 1) for key in identities]
    count = len(ids)
    excluded = bytearray(count)
    for loop in loops:
        excluded[loop["start"]:loop["end"]] = b"\1" * (loop["end"] - loop["start"])
    blocked = [0]
    for value in excluded:
        blocked.append(blocked[-1] + value)
    mask = (1 << 64) - 1
    base = 1000003
    candidates = []
    for length in range(2, min(max_length, count) + 1):
        power = pow(base, length - 1, 1 << 64)
        rolling = 0
        buckets = {}
        for index, value in enumerate(ids):
            if index >= length:
                rolling = (rolling - ids[index - length] * power) & mask
            rolling = (rolling * base + value) & mask
            start = index - length + 1
            if start >= 0 and blocked[index + 1] == blocked[start]:
                # The length <= 8 verification key is bounded work.
                key = (rolling, tuple(ids[start:index + 1]))
                buckets.setdefault(key, []).append(start)
        for (_, cycle), positions in buckets.items():
            if len(positions) < min_reps:
                continue
            if all(b - a == length for a, b in zip(positions, positions[1:])):
                continue
            candidates.append({"length": length, "occurrences": positions,
                               "cycle": list(symbols[positions[0]:positions[0] + length]),
                               "_key": cycle})
    candidates.sort(key=lambda s: (-s["length"], s["occurrences"][0], s["_key"]))
    kept = []
    # At most 28 bounded subpatterns per retained pattern. Store starts that
    # have already been explained rather than comparing patterns pairwise.
    explained = {}
    for pattern in candidates:
        key = pattern.pop("_key")
        if set(pattern["occurrences"]).issubset(explained.get(key, set())):
            continue
        kept.append(pattern)
        length = pattern["length"]
        for sublength in range(2, length):
            for offset in range(length - sublength + 1):
                subkey = key[offset:offset + sublength]
                explained.setdefault(subkey, set()).update(p + offset for p in pattern["occurrences"])
    kept.sort(key=lambda s: (s["occurrences"][0], -s["length"]))
    for number, pattern in enumerate(kept, 1):
        pattern["ref"] = f"S{number}"
    return kept


def register(subparsers):
    """Sequence analysis is exposed by compress, without a duplicate command."""
