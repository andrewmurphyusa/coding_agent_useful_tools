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

"""Common argument defaults, without user-configuration writes."""
import os
from pathlib import Path
import re

DEFAULT_MAX_TOKENS = 8000
DEFAULT_HARD_CAP = 20000

def hard_cap():
    try: value = int(os.environ.get("AGENTLOG_HARD_CAP", DEFAULT_HARD_CAP))
    except ValueError: raise ValueError("AGENTLOG_HARD_CAP must be a positive integer") from None
    if value <= 0: raise ValueError("AGENTLOG_HARD_CAP must be a positive integer")
    return value

def validate_max_tokens(value=DEFAULT_MAX_TOKENS):
    cap = hard_cap()
    if value <= 0: raise ValueError("--max-tokens must be positive")
    if value > cap: raise ValueError(f"--max-tokens {value} exceeds AGENTLOG_HARD_CAP {cap}")
    return value

def default_out(session_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", session_id[:8]):
        raise ValueError("Session ID cannot safely name a default output directory; supply --out")
    return Path("agentlog-out") / session_id[:8]

def add_common_arguments(parser, *, view=False):
    parser.add_argument("--out", type=Path)
    parser.add_argument("--json", action="store_true")
    if view: parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    return parser

add_common = add_common_arguments
