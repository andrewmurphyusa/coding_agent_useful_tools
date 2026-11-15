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

"""Lossless volatile masking, suitable for repeat comparison.

Specific shapes (timestamps, UUIDs, paths, PIDs, locations) take precedence.
Seven or more hexadecimal digits, including all-digit integers, use HEX before
the general NUM rule. Decimal numbers remain NUM, including long digit parts.
"""
import re

_RULES = (
 ("TS", r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b|\b\d{2}:\d{2}:\d{2}(?:\.\d+)?\b"),
 ("DATE", r"\b\d{4}-\d{2}-\d{2}\b"),
 ("UUID", r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"),
 ("TMP", r"(?:[A-Za-z]:[\\/](?:[^\s\"<>]*?[\\/])?(?:Temp|tmp)[\\/][^\s\"<>]+|/(?:tmp|var/tmp)/[^\s\"<>]+)"),
 ("PID", r"(?i:\bpid\s*[:=]?\s*)\d+\b"),
 ("LC", r":\d+:\d+\b"),
 ("HEX", r"\b(?:0x[0-9a-fA-F]{7,}|(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{7,}|(?<!\.)\d{7,}(?!\.\d))\b"),
 ("DUR", r"\b\d+(?:\.\d+)?\s?(?:ms|sec|min|s|m)\b"),
 ("NUM", r"\b(?:\d+\.\d+|\d{3,})\b"),
)
_PATTERN = re.compile("|".join(f"(?P<{kind}>{pattern})" for kind,pattern in _RULES))
_PLACEHOLDERS = re.compile(r"<(?:TS|DATE|UUID|HEX|DUR|NUM|TMP|PID|LC)>")

class VolatileValues(list):
    """Original values plus offsets, preserving literal pre-existing placeholders."""
    def __init__(self):
        super().__init__()
        self.spans = []

def mask_volatile(text):
    values = VolatileValues()
    offset = 0
    def sub(match):
        nonlocal offset
        placeholder = "<" + match.lastgroup + ">"
        start = match.start() + offset
        values.spans.append((start, start+len(placeholder)))
        values.append(match.group())
        offset += len(placeholder)-len(match.group())
        return placeholder
    return _PATTERN.sub(sub, text), values

def restore_volatile(masked, values):
    if isinstance(values, VolatileValues):
        result = masked
        for (start, end), value in reversed(list(zip(values.spans, values))):
            result = result[:start] + value + result[end:]
        return result
    values = iter(values)
    def sub(match):
        try: return next(values)
        except StopIteration: raise ValueError("Too few volatile values") from None
    result = _PLACEHOLDERS.sub(sub, masked)
    try: next(values)
    except StopIteration: return result
    raise ValueError("Too many volatile values")

unmask_volatile = restore_volatile

def norm_for_hash(text):
    text = text.lower()
    text = re.sub(r"\[redacted:[^\]]+\]|\b(?:s[0-9a-f]{10}|d[0-9a-f]{8}|f\d{4,5}|t[0-9a-f]{8}#\d+)\b", " ", text)
    text = mask_volatile(text)[0].lower()
    text = re.sub(r"[#*_`>|-]", " ", text)
    return " ".join(text.split())
