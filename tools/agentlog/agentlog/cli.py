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

"""Fixed plugin registry: subsequent commands register without shared edits."""
import argparse
import importlib
import json
import platform
from pathlib import Path
import shutil
import sqlite3
import sys
from . import __version__
from .core.config import add_common_arguments, validate_max_tokens
from .core.out import summary, write_outputs
from .core.redact import redact
from .core.zst import ZstdUnavailable, native_available
from .readers.base import SessionMeta
from .readers.discover import default_roots, discover_sessions, detect_source

PLUGINS = ("census", "sessions", "skeleton", "show", "profile", "compactions", "compress", "sequences", "expand", "loops", "diff", "trend", "automations", "search")

def _doctor(args):
    connection=sqlite3.connect(":memory:")
    try:
        try:connection.execute("CREATE VIRTUAL TABLE probe USING fts5(text)");fts5=True
        except sqlite3.OperationalError:fts5=False
    finally:connection.close()
    roots=default_roots();counts={"codex":0,"claude":0,"generic":0}
    # Identify sources from the stream so unsupported archives warn and skip
    # regardless of filename, including archives without the rollout- prefix.
    for path in discover_sessions(args.root):
        try:source=detect_source(path)
        except ZstdUnavailable:continue
        counts[source]+=1
    data={"python":platform.python_version(),"fts5":fts5,"compression.zstd":native_available(),"zstd_on_path":shutil.which("zstd") is not None,"roots":{key:{"exists":p.exists()} for key,p in roots.items()},"sessions":counts}
    meta=SessionMeta("doctor","generic","",0)
    write_outputs("doctor",meta,data,"\n".join(f"{k}: {v}" for k,v in data.items()),args.out)
    summary(data,json_mode=args.json)
    return 0

def build_parser():
    parser=argparse.ArgumentParser(prog="agentlog",description="Local, deterministic coding-agent session analysis")
    parser.add_argument("--version",action="version",version=__version__)
    subparsers=parser.add_subparsers(dest="command",required=True)
    doctor=subparsers.add_parser("doctor",help="Check local Python, compression and session discovery")
    add_common_arguments(doctor);doctor.add_argument("--root",action="append",type=Path);doctor.set_defaults(func=_doctor)
    for name in PLUGINS:
        try:module=importlib.import_module("."+name,__package__)
        except ImportError:continue
        module.register(subparsers)
    return parser

def main(argv=None):
    parser=build_parser();args=parser.parse_args(argv)
    try:
        if hasattr(args,"max_tokens"):validate_max_tokens(args.max_tokens)
        handler=getattr(args,"func",None) or getattr(args,"handler",None) or getattr(args,"run",None)
        if handler is None:parser.error("Command has no registered handler")
        result=handler(args)
        return result if isinstance(result,int) else 0
    except ValueError as exc:
        print(redact("agentlog: "+str(exc))[0],file=sys.stderr);return 2
    except (OSError,sqlite3.Error) as exc:
        print(redact("agentlog: "+str(exc))[0],file=sys.stderr);return 3

if __name__=="__main__":raise SystemExit(main())
