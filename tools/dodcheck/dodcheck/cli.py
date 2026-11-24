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

"""Command-line entry: record, stop and run subcommands."""
import json
import os
import sys

from . import core


def _read_payload(stream):
    data = stream.buffer.read() if hasattr(stream, "buffer") else stream.read().encode()
    payload = json.loads(data.decode("utf-8", "replace") or "{}")
    return payload if isinstance(payload, dict) else {}


def _record(stdin):
    try:
        core.record(_read_payload(stdin))
    except Exception:
        pass
    return 0


def _stop(stdin):
    try:
        output = core.stop(_read_payload(stdin))
    except Exception as exc:
        output = {"systemMessage": f"dodcheck internal error: {exc}"}
    if output:
        print(json.dumps(output))
    return 0


def _run(args):
    start = os.path.abspath(args[0] if args else os.getcwd())
    project = core.find_project(start)
    if not project:
        print(f"dodcheck: no {core.CONFIG_NAME} found at or above {start}")
        return 1
    code, text = core.run_command(project)
    print(text)
    return code


def main(argv=None, stdin=None):
    args = list(sys.argv[1:] if argv is None else argv)
    stdin = stdin or sys.stdin
    command = args[0] if args else ""
    if command == "record":
        return _record(stdin)
    if command == "stop":
        return _stop(stdin)
    if command == "run":
        return _run(args[1:])
    print("usage: dodcheck record | stop | run [PROJECT_DIR]", file=sys.stderr)
    return 2
