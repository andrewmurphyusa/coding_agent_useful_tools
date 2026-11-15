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

"""Seeded MinHash candidates with exact Jaccard verification."""
import hashlib
import random
from .normalize import norm_for_hash, mask_volatile

_PRIME = (1 << 61) - 1

def shingles(text, k=3, *, mode="word"):
    if k < 1: raise ValueError("k must be positive")
    if mode in ("line", "lines"):
        tokens = [norm_for_hash(line) for line in text.splitlines() if line.strip()]
    elif mode == "word": tokens = norm_for_hash(text).split()
    else: raise ValueError("mode must be word or line")
    if len(tokens) < k: return {tuple(tokens)} if tokens else set()
    return {tuple(tokens[i:i+k]) for i in range(len(tokens)-k+1)}

def line_shingles(text, k=3): return shingles(text, k, mode="line")

def jaccard(a, b):
    a, b = set(a), set(b)
    return len(a & b) / len(a | b) if a or b else 1.0

def containment(a, b):
    a, b = set(a), set(b)
    return len(a & b) / len(a) if a else 1.0

class MinHash:
    def __init__(self, num_perm=128):
        if num_perm < 1: raise ValueError("num_perm must be positive")
        self.num_perm = num_perm
        rng = random.Random(1337)
        self.permutations = [(rng.randrange(1, _PRIME), rng.randrange(_PRIME)) for _ in range(num_perm)]
    def signature(self, tokens):
        hashes = [int.from_bytes(hashlib.sha1(repr(token).encode("utf-8")).digest(), "big") % _PRIME for token in tokens]
        return tuple(min(((a*x+b) % _PRIME for x in hashes), default=_PRIME) for a,b in self.permutations)
    __call__ = signature

def lsh_candidates(signatures, bands=32, rows=4):
    buckets = {}; pairs = set()
    for key, signature in sorted(signatures.items()):
        if len(signature) != bands * rows: raise ValueError("signature length must equal bands * rows")
        for band in range(bands):
            bucket = (band, tuple(signature[band*rows:(band+1)*rows]))
            for other in buckets.get(bucket, ()): pairs.add((other, key))
            buckets.setdefault(bucket, []).append(key)
    return sorted(pairs)

def cluster(items, threshold=0.85, k=3, *, mode="word"):
    if not 0 <= threshold <= 1: raise ValueError("threshold must be between 0 and 1")
    items = dict(items); keys = sorted(items); parent = {key:key for key in keys}
    sets = {key:shingles(items[key], k, mode=mode) for key in keys}
    mh = MinHash(); signatures = {key:mh(sets[key]) for key in keys}
    def find(key):
        while parent[key] != key: parent[key] = parent[parent[key]]; key = parent[key]
        return key
    for a,b in lsh_candidates(signatures):
        if jaccard(sets[a],sets[b]) >= threshold:
            ra,rb=find(a),find(b); parent[max(ra,rb)] = min(ra,rb)
    groups = {}
    for key in keys: groups.setdefault(find(key), []).append(key)
    return groups
