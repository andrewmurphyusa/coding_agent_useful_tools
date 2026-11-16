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

"""Compaction summaries, drift and repeated file reads."""
from collections import defaultdict
from difflib import unified_diff
from pathlib import Path
import re
from .core.config import add_common_arguments
from .core.normalize import mask_volatile
from .core.out import write_outputs, summary
from .core.tokens import est_tokens
from .readers.discover import load_session, input_paths

_READ = re.compile(r'\b(?:cat|type|head|tail|less|more|Get-Content|read_file|read|open)\b', re.I)
_PATH = re.compile(r'''(?:[A-Za-z]:[\\/]|/|\.{0,2}[\\/])?[\w.-]+(?:[\\/][\w. -]+)+|[\w.-]+\.[A-Za-z0-9]{1,12}''')

SUMMARY_MAX_TOKENS = 1500


def _cap_summary(text, ref):
    """Cap report data itself so JSON and Markdown share the same summary."""
    if est_tokens(text) <= SUMMARY_MAX_TOKENS:
        return text
    marker = f'\n[Summary truncated; show {ref} for full text.]'
    # Match the canonical ceil(chars / 3.5) estimator, including the marker.
    chars = SUMMARY_MAX_TOKENS * 7 // 2
    return text[:chars - len(marker)] + marker


def _paths(event):
    if event.kind != 'tool_call' or not _READ.search((event.tool or '') + ' ' + event.text): return []
    return sorted(set(m.group().strip() for m in _PATH.finditer(event.text)))

def analyze(meta, events):
    usage = [e for e in events if e.kind == 'token_usage' and e.usage and e.usage.context_used is not None]
    compacts = [e for e in events if e.kind == 'compaction']
    calls = {e.call_id: e for e in events if e.kind == 'tool_call' and e.call_id}
    costs = defaultdict(int)
    for e in events:
        if e.kind == 'tool_result' and e.call_id in calls: costs[e.call_id] += e.est_tokens
    rows = []
    previous_summary = None
    for c in compacts:
        before = next((u.usage.context_used for u in reversed(usage) if u.seq < c.seq), None)
        after = next((u.usage.context_used for u in usage if u.seq > c.seq), None)
        prior = {path for e in events if e.seq < c.seq for path in _paths(e)}
        reacquired = defaultdict(lambda: {'tokens': 0, 'refs': []})
        for e in events:
            if e.seq <= c.seq or not (c.turn <= e.turn <= c.turn + 10): continue
            for path in _paths(e):
                if path in prior:
                    reacquired[path]['tokens'] += costs[e.call_id] if e.call_id else 0
                    reacquired[path]['refs'].append(f'E{e.seq}')
        normalized = mask_volatile(c.text)[0]
        drift = []
        if previous_summary is not None:
            diff = unified_diff(previous_summary.splitlines(), normalized.splitlines(), lineterm='')
            drift = [line for line in diff if (line.startswith('+') and not line.startswith('+++')) or (line.startswith('-') and not line.startswith('---'))][:40]
        if c.text: previous_summary = normalized
        ref = f'E{c.seq}'
        rows.append({'ref': ref, 'seq': c.seq, 'ts': c.ts, 'turn': c.turn, 'before': before, 'after': after, 'summary': _cap_summary(c.text, ref), 'summary_tokens': est_tokens(c.text), 'summary_truncated': est_tokens(c.text) > SUMMARY_MAX_TOKENS, 'drift': drift, 'reacquired': [{'path': path, **item} for path, item in sorted(reacquired.items())]})
    peak = max((u.usage.context_used for u in usage), default=None)
    return {'compactions': rows, 'peak_context': peak}

def _markdown(data):
    if not data['compactions']: return f"No compactions\n\nPeak context: {data['peak_context']}"
    lines = ['# Compactions']
    for c in data['compactions']:
        lines += [f"## {c['ref']} \u2013 turn {c['turn']}", f"{c['ts']}; context {c['before']} \u2192 {c['after']}", f"Summary (show {c['ref']} for full text):", c['summary'], 'Drift:', *c['drift'], 'Re-acquired files:']
        lines += [f"- {x['path']}: {x['tokens']} estimated tokens ({', '.join(x['refs'])})" for x in c['reacquired']]
    return '\n'.join(lines)

def run(args):
    meta, events = load_session(args.session, args.root, threads_csv=getattr(args, 'threads_csv', None))
    from .core.paths import checked_destination
    from .core.config import default_out
    directory = Path(args.out) if args.out else default_out(meta.session_id)
    for target in (directory, directory / 'compactions.md', directory / 'compactions.json'):
        checked_destination(target, inputs=input_paths([meta], getattr(args, 'threads_csv', None)))
    data = analyze(meta, events)
    write_outputs('compactions', meta, data, _markdown(data), args.out, max_tokens=6000)
    summary({'compactions': len(data['compactions']), 'peak_context': data['peak_context']} if data['compactions'] else f"No compactions; peak context: {data['peak_context']}", json_mode=args.json)
    return 0

def register(subparsers):
    parser = subparsers.add_parser('compactions', help='Analyze compactions and summaries')
    parser.add_argument('session'); parser.add_argument('--root', action='append', type=Path)
    add_common_arguments(parser); parser.set_defaults(func=run)
