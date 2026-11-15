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

"""UTF-8 JSONL streaming; malformed lines are retained as census events."""
import json
from pathlib import Path
from .zst import open_binary

def iter_records(path):
    with open_binary(path) as stream:
        for lineno, line in enumerate(stream):
            size = len(line)
            stripped = line.decode("utf-8", errors="replace").lstrip("\ufeff").strip()
            if not stripped: continue
            try: record = json.loads(stripped)
            except (ValueError, RecursionError):
                record = {"type": "malformed_json"}
            if not isinstance(record, dict): record = {"type": "non_object_json"}
            yield lineno, record, size

def read_jsonl(path):
    for _, record, _ in iter_records(path): yield record

def write_jsonl(path, records):
    with Path(path).open("w", encoding="utf-8", newline="\n") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
