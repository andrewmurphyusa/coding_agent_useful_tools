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

"""One reference grammar for events, blocks, variants, loops and findings."""
from dataclasses import dataclass
import re

_PATTERN = re.compile(r"(?:(?P<session>[A-Za-z0-9-]{8,}):)?(?P<kind>[EBVLF])(?P<number>\d+)")
@dataclass(frozen=True)
class Reference:
    kind: str
    number: int
    session: str | None = None
    def __str__(self): return (self.session + ":" if self.session else "") + self.kind + str(self.number)

def parse_ref(value):
    match = _PATTERN.fullmatch(value)
    if not match: raise ValueError("Invalid reference; expected E<n>, B<n>, V<n>, L<n>, F<n>, or <session8>:E<n>")
    if match["session"] and match["kind"] != "E": raise ValueError("Cross-session references must identify an event")
    return Reference(match["kind"], int(match["number"]), match["session"])

def find_refs(text):
    for match in _PATTERN.finditer(text):
        try: yield parse_ref(match.group())
        except ValueError: pass

def event_ref(seq, session=None): return str(Reference("E", seq, session))
