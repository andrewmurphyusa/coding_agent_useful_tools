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

"""Deterministic output files, budgets and small stdout summaries."""
from dataclasses import asdict, is_dataclass
import json
from pathlib import Path
import re
from .config import default_out, validate_max_tokens
from .tokens import est_tokens
from .redact import redact, redact_object
from .paths import checked_destination

def json_default(value):
    if is_dataclass(value): return asdict(value)
    if isinstance(value, Path): return str(value)
    if isinstance(value, set): return sorted(value)
    raise TypeError(type(value).__name__)

def dumps(value):
    primitive = json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, default=json_default))
    return json.dumps(redact_object(primitive), ensure_ascii=False, sort_keys=True)

def summary(data, *, json_mode=False):
    if json_mode: print(dumps(data))
    elif isinstance(data, dict):
        for key, value in list(data.items())[:20]: print(redact(f"{key}: {value}")[0].replace("\n", " "))
    else:
        for line in str(data).splitlines()[:20]: print(redact(line)[0])

print_summary = summary

def cap_text(text, max_tokens=8000):
    validate_max_tokens(max_tokens)
    if est_tokens(text) <= max_tokens: return text
    marker = "\n[view truncated; detailed JSON contains complete results]\n"
    chars = (max_tokens * 7) // 2
    return text[:max(0, chars-len(marker))] + marker[:chars]

def view_header(command, session_id, raw_tokens, view_tokens):
    ratio = view_tokens/raw_tokens if raw_tokens else 0
    return f"<!-- agentlog {command} | session {session_id[:8]} | raw_tokens {raw_tokens} | this_view_tokens {view_tokens} | ratio {ratio:.6f} -->"

def write_outputs(command, meta, data, markdown, out=None, *, max_tokens=8000):
    validate_max_tokens(max_tokens)
    if not isinstance(command, str) or not re.fullmatch(r"[a-z][a-z0-9_-]*", command):
        raise ValueError("Report command must be a simple filename component")
    directory = checked_destination(Path(out) if out else default_out(meta.session_id), inputs=(meta.path,))
    targets = [directory/f"{command}.{suffix}" for suffix in ("md", "json")]
    # Check both outputs before mkdir or either write. Existing symlinks and
    # hard links may alias another log, even when it is not the selected input.
    for target in targets:
        checked_destination(target, inputs=(meta.path,))
        if target.is_symlink() or (target.exists() and target.stat().st_nlink > 1):
            raise ValueError("Report files must not be symlinks or hard links")
    raw_tokens = (meta.bytes*2+6)//7
    body = redact(markdown)[0]
    header = view_header(command, meta.session_id, raw_tokens, max_tokens)
    available = max_tokens - est_tokens(header+"\n")
    if available < 1: raise ValueError("--max-tokens is too small for the required view header")
    body = cap_text(body, available)
    text = header + "\n" + body
    for _ in range(4):
        header = view_header(command, meta.session_id, raw_tokens, est_tokens(text))
        text = header + "\n" + body
    json_text = dumps(data)+"\n"
    directory.mkdir(parents=True, exist_ok=True)
    targets[0].write_text(text, encoding="utf-8", newline="\n")
    targets[1].write_text(json_text, encoding="utf-8", newline="\n")
    return directory

write_report = write_outputs
