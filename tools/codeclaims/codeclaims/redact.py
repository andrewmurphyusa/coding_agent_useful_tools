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

"""Canonical secret patterns, shared by all session readers."""
from collections import Counter
import math
import re

ACCOUNT_KEYS = ("creator_user_id", "creator_account_id", "ownerAccountUuid", "ownerOrganizationUuid", "account_id", "user_id", "accountUuid", "organizationUuid")
_ACCOUNT_KEYS_FOLDED = frozenset(key.casefold() for key in ACCOUNT_KEYS)
PATTERNS = (
    ("private_key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    ("anthropic_key", r"\bsk-ant-[A-Za-z0-9_\-]{20,}"),
    ("openai_key", r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}"),
    ("github_token", r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"),
    ("aws_key", r"\bAKIA[0-9A-Z]{16}\b"),
    ("azure_conn", r"(?i)(?:AccountKey=[^;\s]+|SharedAccessSignature=[^;\s]+|[?&]sig=[A-Za-z0-9%]{20,})"),
    ("stripe_key", r"\b(?:(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}|whsec_[A-Za-z0-9]{16,})"),
    ("jwt", r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    ("conn_password", r"(?i)\b(?:password|pwd)\s*=\s*[^;\s]+"),
    ("bearer", r"(?i)\bbearer\s+[A-Za-z0-9\-._~+/]{20,}=*"),
    ("account_id", r"\buser-[A-Za-z0-9]{20,}\b"),
    ("email", r"(?i)\b[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9](?:[A-Z0-9.-]*[A-Z0-9])?\.[A-Z]{2,}\b"),
)
# Quote delimiters include their backslashes so JSON embedded in JSON is handled
# without decoding or rewriting surrounding text. _keyed scans the closing
# delimiter separately, including escaped quotes and literal trailing slashes.
_ACCOUNT = re.compile(r"""(?i)((?:\\*["'])?\b(?:%s)\b(?:\\*["'])?\s*[:=]\s*)(?:(\\*["'])|([^\s,;}]+))""" % "|".join(ACCOUNT_KEYS))
_KV = re.compile(r"""(?i)((?:\\*["'])?\b(?:api[_-]?key|secret|token|passwd|password|client[_-]?secret)\b(?:\\*["'])?\s*[:=]\s*)(?:(\\*["'])|([^\s"',;}]{8,}))""")
_COMPILED = tuple((kind, re.compile(pattern)) for kind, pattern in PATTERNS)
def _placeholder(value, kind):
    return not value or value == f"[REDACTED:{kind}]"

def _keyed(text, pattern, kind, counts):
    pieces = []
    position = 0
    while match := pattern.search(text, position):
        end = match.end()
        quote = match[2]
        if quote is None:
            value = match[3]
        else:
            # With d backslashes on the opening quote, a closing quote has
            # d + k*2*(d+1) preceding backslashes. The extra pairs belong to
            # trailing literal backslashes; escaped content quotes have the
            # opposite parity. This works at every JSON embedding depth.
            depth = len(quote)-1
            slashes = 0
            index = end
            while index < len(text) and text[index] not in '\r\n':
                char = text[index]
                if char == quote[-1] and slashes >= depth and (slashes-depth) % (2*(depth+1)) == 0:
                    value = text[end:index-depth]
                    end = index+1
                    break
                slashes = slashes+1 if char == '\\' else 0
                index += 1
            else:
                value = text[end:index]
                end = index
        unquoted_null = quote is None and value.lower() in {"none", "null"}
        if _placeholder(value, kind) or unquoted_null or (kind == "kv_secret" and len(value) < 8):
            replacement = text[match.start():end]
        else:
            counts[kind] += 1
            delimiter = quote if quote is not None else '"'
            replacement = match[1]+delimiter+'[REDACTED:'+kind+']'+delimiter
        pieces.extend((text[position:match.start()], replacement))
        position = end
    pieces.append(text[position:])
    return ''.join(pieces)

def redact(text, *, redact_emails=True, extra_patterns=()):
    counts = Counter()
    def replacement(kind):
        def sub(match): counts[kind] += 1; return f"[REDACTED:{kind}]"
        return sub
    text = _keyed(text, _ACCOUNT, "account_id", counts)
    for kind, pattern in _COMPILED:
        if kind == "email" and not redact_emails: continue
        text = pattern.sub(replacement(kind), text)
    text = _keyed(text, _KV, "kv_secret", counts)
    # Entropy is contextual; ordinary SHAs and IDs are never secret candidates.
    lines = []
    for line in text.splitlines(keepends=True):
        if re.search(r"(?i)key|secret|token|password|sig|auth", line):
            def entropy_sub(match):
                value = match.group()
                if re.fullmatch(r"[0-9a-fA-F]{32,}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value): return value
                freqs = Counter(value)
                entropy = -sum((n/len(value))*math.log2(n/len(value)) for n in freqs.values())
                if entropy < 4: return value
                counts["high_entropy"] += 1
                return "[REDACTED:high_entropy]"
            line = re.sub(r"(?<![\w-])[A-Za-z0-9+/=_\-]{32,}(?![\w-])", entropy_sub, line)
        lines.append(line)
    text = "".join(lines)
    for kind, pattern in extra_patterns:
        text = re.sub(pattern, replacement(kind), text)
    return text, sorted(counts.items())

def redact_object(value):
    if isinstance(value, dict):
        # Reserve every sanitized key before adding suffixes. A literal key such
        # as "[REDACTED:email]#2" must not be overwritten by a collision suffix.
        entries = [(str(k), redact(str(k))[0], v) for k, v in value.items()]
        reserved = {key for _, key, _ in entries}
        result = {}
        # Existing safe keys retain their spelling, even when a secret key
        # redacts to the same spelling. Sorting also stabilizes collision labels.
        for original, key, item in sorted(entries, key=lambda entry: (entry[0] != entry[1], entry[0])):
            candidate = key
            number = 2
            while candidate in result or (candidate != key and candidate in reserved):
                candidate = f"{key}#{number}"
                number += 1
            result[candidate] = ("[REDACTED:account_id]" if original.casefold() in _ACCOUNT_KEYS_FOLDED and item is not None else redact_object(item))
        return result
    if isinstance(value, list): return [redact_object(v) for v in value]
    if isinstance(value, str): return redact(value)[0]
    return value
