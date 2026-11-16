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

"""Compare two normalized session streams."""
from collections import defaultdict
from dataclasses import asdict
from difflib import SequenceMatcher, unified_diff
from pathlib import Path
import re
import json

from .core.config import add_common_arguments, default_out
from .core.normalize import mask_volatile
from .core.out import summary, write_outputs
from .core.tokens import est_tokens
from .readers.discover import load_session, input_paths
from . import trend


def _category(event):
    text = event.text.lower()
    if event.kind == 'user_msg': return 'first user prompt'
    if '# agents.md' in text or 'agents.md instructions' in text: return 'AGENTS.md'
    if '<environment_context>' in text: return 'environment'
    if event.role == 'developer': return 'developer'
    return 'base instructions'


def _contexts(events):
    result = defaultdict(list); got_user = False
    for e in events:
        if e.kind == 'context': result[_category(e)].append(e.text)
        elif e.kind == 'user_msg' and not got_user:
            result['first user prompt'].append(e.text); got_user = True
    return {k: '\n'.join(v) for k,v in result.items()}


def _alphabet(event):
    head = ''
    if event.kind == 'tool_call':
        match = re.search(r'(?i)\b(git|python|pytest|cargo|npm|cat|type|get-content|rg|ls|dir)\b(?:\s+([\w-]+))?', event.text)
        if match:
            command, subcommand = match.groups()
            head = command + (' ' + subcommand if subcommand and command.lower() in ('git', 'cargo', 'npm') else '')
        else: head = event.tool or ''
    return (event.kind, event.tool or '', head)


def _settings(events):
    values = defaultdict(set)
    def visit(item, key='', in_tools=False):
        if isinstance(item, dict):
            for name, value in item.items(): visit(value, name, in_tools or 'tool' in key.lower())
        elif isinstance(item, list):
            for value in item: visit(value, key, in_tools or 'tool' in key.lower())
        elif item is not None:
            if in_tools and key == 'name': values['tools'].add(str(item))
            elif any(word in key.lower() for word in ('sandbox', 'approval', 'history_mode')): values[key].add(str(item))
            elif 'tool' in key.lower() and isinstance(item, str): values['tools'].add(item)
    for event in events:
        if event.kind != 'settings': continue
        try: visit(json.loads(event.text))
        except (ValueError, TypeError): continue
    return {key: sorted(value) for key, value in values.items()}


def compare(good_meta, good, bad_meta, bad):
    left, right = asdict(good_meta), asdict(bad_meta)
    keys = ('model', 'reasoning_effort', 'cli_version', 'originator', 'source', 'thread_source', 'cwd', 'git_branch', 'git_commit', 'context_window')
    meta = {key: [left[key], right[key]] for key in keys if left[key] != right[key]}
    unchanged = [key for key in keys if left[key] == right[key]]
    contexts = []
    a, b = _contexts(good), _contexts(bad)
    for key in ('base instructions', 'developer', 'AGENTS.md', 'environment', 'first user prompt'):
        old, new = a.get(key, ''), b.get(key, '')
        old_masked, new_masked = mask_volatile(old)[0], mask_volatile(new)[0]
        diff = list(unified_diff(old_masked.splitlines(), new_masked.splitlines(), fromfile='good', tofile='bad', lineterm=''))[:60] if old_masked != new_masked else []
        contexts.append({'category': key, 'good_tokens': est_tokens(old), 'bad_tokens': est_tokens(new), 'diff': diff})
    good_metrics, bad_metrics = trend.analyze(good_meta, good), trend.analyze(bad_meta, bad)
    paths = set(good_metrics['inputs']) | set(bad_metrics['inputs'])
    inputs = [{'path': path, 'good_tokens': good_metrics['inputs'].get(path, 0), 'bad_tokens': bad_metrics['inputs'].get(path, 0)} for path in paths]
    inputs.sort(key=lambda x: (-(x['bad_tokens']-x['good_tokens']), x['path']))
    matcher = SequenceMatcher(a=[_alphabet(e) for e in good], b=[_alphabet(e) for e in bad], autojunk=False)
    divergence = None
    for op, i, _, j, _ in matcher.get_opcodes():
        if op != 'equal':
            divergence = {'good': f'E{good[i].seq}' if i < len(good) else None, 'bad': f'E{bad[j].seq}' if j < len(bad) else None}
            break
    old_settings, new_settings = _settings(good), _settings(bad)
    settings = {key: {'added': sorted(set(new_settings.get(key, ())) - set(old_settings.get(key, ()))), 'removed': sorted(set(old_settings.get(key, ())) - set(new_settings.get(key, ())))} for key in sorted(set(old_settings) | set(new_settings)) if old_settings.get(key) != new_settings.get(key)}
    return {'meta': meta, 'unchanged': unchanged, 'settings': settings, 'instructions': contexts,
            'inputs': inputs, 'good_metrics': good_metrics, 'bad_metrics': bad_metrics,
            'first_divergence': divergence}


def _markdown(data):
    lines = ['# Diff', '## Meta']
    lines += [f'- {k}: {v[0]} -> {v[1]}' for k,v in data['meta'].items()]
    lines.append('unchanged: ' + ', '.join(data['unchanged']))
    lines.append('history_mode: unavailable in normalized metadata')
    lines.append('## Settings and tools')
    lines.append(str(data['settings']))
    lines.append('## Instructions')
    for c in data['instructions']:
        lines.append(f"### {c['category']}: {c['good_tokens']} -> {c['bad_tokens']}")
        lines.extend(c['diff'] or ['unchanged'])
    lines.append('## Inputs')
    lines += [f"- {x['path']}{' (memory file)' if trend.MEMORY.search(x['path']) else ''}: {x['good_tokens']} -> {x['bad_tokens']}" for x in data['inputs']]
    lines.append('## Behaviour metrics')
    for key in data['good_metrics']:
        if key in ('inputs', 'tool_mix'): continue
        a, b = data['good_metrics'][key], data['bad_metrics'][key]
        ratio = b/a if isinstance(a,(int,float)) and not isinstance(a,bool) and a and isinstance(b,(int,float)) else None
        lines.append(f'- {key}: {a} -> {b}; ratio {ratio}')
    lines += ['## Tool mix', str(data['good_metrics']['tool_mix']) + ' -> ' + str(data['bad_metrics']['tool_mix']),
              '## New content', str(data.get('new_blocks', [])),
              '## First divergence', str(data['first_divergence']) + ' (use agentlog show with these E refs)']
    return '\n'.join(lines)


def run(args):
    good_meta, good = load_session(args.good, args.root, threads_csv=getattr(args, 'threads_csv', None))
    bad_meta, bad = load_session(args.bad, args.root, threads_csv=getattr(args, 'threads_csv', None))
    data = compare(good_meta, good, bad_meta, bad)
    from .compress import compress_session
    old = compress_session(good_meta, good, lossless=True)
    new = compress_session(bad_meta, bad, lossless=True)
    old_bodies = {(entry['kind'], entry['tool'], entry['masked_text']) for group in ('blocks', 'variants') for entry in old[group].values()}
    additions = (entry for group in ('blocks', 'variants') for entry in new[group].values() if (entry['kind'], entry['tool'], entry['masked_text']) not in old_bodies)
    data['new_blocks'] = [{key: entry[key] for key in ('ref', 'first_ref', 'total_tokens', 'kind', 'tool')} for entry in sorted(additions, key=lambda x: (-x['total_tokens'], x['ref']))[:15]]
    from .core.paths import checked_destination
    directory = Path(args.out) if args.out else default_out(bad_meta.session_id)
    for target in (directory, directory / 'diff.md', directory / 'diff.json'):
        checked_destination(target, inputs=input_paths([good_meta, bad_meta], getattr(args, 'threads_csv', None)))
    write_outputs('diff', bad_meta, data, _markdown(data), args.out, max_tokens=6000)
    summary({'good': good_meta.session_id, 'bad': bad_meta.session_id, 'first_divergence': data['first_divergence']}, json_mode=args.json)
    return 0


def register(subparsers):
    p = subparsers.add_parser('diff'); p.add_argument('good'); p.add_argument('bad'); p.add_argument('--root', action='append'); add_common_arguments(p); p.set_defaults(func=run)
