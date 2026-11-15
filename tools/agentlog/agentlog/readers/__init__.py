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

"""Streaming session readers. Every reader emits metadata before events."""
from .base import Event, SessionMeta, Usage
from .discover import discover_sessions, load_session, read_session, resolve_session
__all__ = ["Event", "SessionMeta", "Usage", "discover_sessions", "load_session", "read_session", "resolve_session"]
