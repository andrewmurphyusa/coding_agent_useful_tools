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

"""Public normalized event contract and reader utilities."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
from ..core.redact import redact
from ..core.tokens import est_tokens

@dataclass(frozen=True)
class Usage:
    input: int | None = None
    cached_input: int | None = None
    output: int | None = None
    reasoning: int | None = None
    total: int | None = None
    context_used: int | None = None
    cumulative_total: int | None = None
    cache_creation_input: int | None = None

@dataclass(frozen=True)
class SessionMeta:
    session_id: str
    source: str
    path: str
    bytes: int
    compressed: bool = False
    cwd: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    cli_version: str | None = None
    originator: str | None = None
    thread_source: str | None = None
    started: str | None = None
    ended: str | None = None
    title: str | None = None
    git_branch: str | None = None
    git_commit: str | None = None
    context_window: int | None = None
    instructions_sha1: str | None = None
    automation_id: str | None = None
    partial: bool = False
    partial_reason: str | None = None
    extra: dict = field(default_factory=dict, hash=False)

@dataclass(frozen=True)
class Event:
    seq: int
    ts: str | None = None
    kind: str = "other"
    role: str | None = None
    tool: str | None = None
    call_id: str | None = None
    text: str = ""
    exit_code: int | None = None
    is_error: bool = False
    sidechain: bool = False
    turn: int = 0
    raw_type: str = ""
    raw_bytes: int = 0
    est_tokens: int = 0
    usage: Usage | None = None

def as_int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None

def as_str(value):
    return value if isinstance(value, str) else None

def cwd_prefix_matches(value, prefixes):
    if not isinstance(value, str): return False
    value = value.replace('\\', '/').casefold()
    return any(value.startswith(prefix.replace('\\', '/').casefold()) for prefix in prefixes if isinstance(prefix, str))

def utc(value):
    if not isinstance(value, str): return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except ValueError: return None

def text_of(value):
    if value is None: return ""
    if isinstance(value, str): return value
    if isinstance(value, list): return "\n".join(text_of(v) for v in value)
    if isinstance(value, dict):
        for key in ("text", "thinking", "content"):
            if key in value: return text_of(value[key])
    return json.dumps(value, ensure_ascii=False, sort_keys=True)

def safe(value):
    return redact(str(value))[0] if value is not None else None

def event(seq, raw, size, **kw):
    # Text is redacted before constructing the immutable public model.
    text = redact(text_of(kw.pop("text", "")))[0]
    for key in ("role", "tool", "call_id", "raw_type"):
        if key in kw: kw[key] = safe(kw[key])
    return Event(seq=seq, ts=utc(raw.get("timestamp")), raw_bytes=size,
                 text=text, est_tokens=est_tokens(text), **kw)

def fallback_id(path):
    return hashlib.sha1(str(Path(path).resolve()).encode("utf-8")).hexdigest()

def base_meta(path, source, **kw):
    path = Path(path)
    for key, value in list(kw.items()):
        if isinstance(value, str): kw[key] = safe(value)
    return SessionMeta(source=source, path=safe(str(path)), bytes=path.stat().st_size,
                       compressed=path.suffix == ".zst", **kw)
