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

"""Usage, context and event-cost analysis over normalized sessions."""
import csv
from collections import defaultdict
from datetime import datetime
from pathlib import Path
import re
from .core.config import add_common_arguments, default_out
from .core.out import write_outputs, summary
from .core.paths import checked_destination
from .readers.discover import load_session, input_paths

CATEGORIES = {'tool_call', 'tool_result', 'assistant_msg', 'reasoning', 'user_msg', 'context'}
LIMIT = re.compile(r'rate[ -]limit|usage[ -]limit|quota|context length|context window', re.I)
RATE_LIMIT = re.compile(r'rate[ -]limit', re.I)
USAGE_LIMIT = re.compile(r'usage[ -]limit|quota', re.I)

def _head(text):
    return ' '.join(text.split())[:100]

def _duration(start, end):
    try:
        return (datetime.fromisoformat(end.replace('Z', '+00:00')) - datetime.fromisoformat(start.replace('Z', '+00:00'))).total_seconds()
    except (TypeError, ValueError, AttributeError):
        return None

def _spark(values):
    if not values: return ''
    if len(values) > 120:
        values = [values[(i * len(values)) // 120] for i in range(120)]
    low, high = min(values), max(values)
    return ''.join(' .:-=+*#%@'[0 if high == low else (v-low)*9//(high-low)] for v in values)

def analyze(meta, events):
    calls_by_id = {}
    categories = defaultdict(int)
    additions = []
    calls = []
    turn_health = defaultdict(lambda: {'calls': 0, 'tokens_added': 0, 'uncached_input': 0, 'tool_errors': 0, 'novel_ratio': None})
    totals = dict.fromkeys(('input', 'cached_input', 'output', 'reasoning', 'total'), 0)
    totals['cache_creation_input'] = None
    sources = {key: 'authoritative' for key in totals}
    pending = []
    current_segment = []
    last_turn = 0
    markers = []
    errors = []
    for e in events:
        last_turn = max(last_turn, e.turn)
        health = turn_health[e.turn]
        if e.kind == 'tool_call' and e.call_id: calls_by_id[e.call_id] = (e.tool or '', _head(e.text))
        if e.kind in CATEGORIES:
            if e.kind == 'tool_call': tool, head = e.tool or '', _head(e.text)
            elif e.kind == 'tool_result': tool, head = calls_by_id.get(e.call_id, (e.tool or '', ''))
            else: tool, head = '', ''
            category = e.kind + ((' ' + tool + ' ' + head).rstrip() if e.kind in ('tool_call', 'tool_result') else '')
            categories[category] += e.est_tokens
            item = {'ref': f'E{e.seq}', 'seq': e.seq, 'turn': e.turn, 'kind': e.kind, 'tool': tool, 'head': head, 'tokens': e.est_tokens, 'later_calls': 0}
            additions.append(item); pending.append(item); current_segment.append(item)
            health['tokens_added'] += e.est_tokens
        if e.kind == 'compaction':
            current_segment = []
            markers.append((e.seq, e.turn, '\u27f3'))
        if e.kind == 'error' or e.is_error:
            is_limit = bool(LIMIT.search(e.text))
            errors.append({'ref': f'E{e.seq}', 'ts': e.ts, 'text': e.text.splitlines()[0] if e.text.splitlines() else '', 'limit': is_limit, 'rate_limit': bool(RATE_LIMIT.search(e.text)), 'usage_limit': bool(USAGE_LIMIT.search(e.text))})
            markers.append((e.seq, e.turn, '\u26a0' if is_limit else '\u2716'))
            if e.kind == 'tool_result': health['tool_errors'] += 1
        if e.kind == 'token_usage':
            u = e.usage
            added = sum(x['tokens'] for x in pending)
            vals = {}
            for key in totals:
                value = getattr(u, key, None) if u else None
                if key == 'cache_creation_input':
                    vals[key] = value
                    if not calls:
                        totals[key] = value
                    elif totals[key] is not None and value is not None:
                        totals[key] += value
                    else:
                        totals[key] = None
                    sources[key] = 'authoritative' if totals[key] is not None else 'unknown'
                    continue
                if value is None:
                    value = added if key in ('input', 'total') else 0
                    sources[key] = 'estimated' if not calls else 'mixed' if sources[key] == 'authoritative' else sources[key]
                elif sources[key] == 'estimated': sources[key] = 'mixed'
                vals[key] = value
                totals[key] += value
            context_authoritative = bool(u and u.context_used is not None)
            context = u.context_used if context_authoritative else sum(x['tokens'] for x in current_segment)
            # Unknown cache creation cannot be treated as zero for Claude.
            if meta.source == 'claude':
                creation = vals['cache_creation_input']
                uncached = vals['input'] + creation if creation is not None else None
            else:
                uncached = max(0, vals['input'] - vals['cached_input'])
            health['calls'] += 1
            if health['uncached_input'] is not None:
                health['uncached_input'] = health['uncached_input'] + uncached if uncached is not None else None
            for item in current_segment: item['later_calls'] += 1
            calls.append({'seq': e.seq, 'turn': e.turn, 'ts': e.ts, 'context_used': context,
                          'context_source': 'authoritative' if context_authoritative else 'estimated',
                          **vals, 'uncached_input': uncached, 'tokens_added': added,
                          'cumulative_total': totals['total'], 'sources': {k: 'authoritative' if u and getattr(u, k, None) is not None else ('unknown' if k == 'cache_creation_input' else 'estimated') for k in totals}})
            pending = []
    if not calls:
        estimated = sum(x['tokens'] for x in additions)
        totals['input'] = totals['total'] = estimated
        sources = {key: ('unknown' if key == 'cache_creation_input' else 'estimated') for key in totals}
    for item in additions: item['carry_cost'] = item['tokens'] * item['later_calls']
    turn_health = {str(k): v for k, v in sorted(turn_health.items())}
    buckets = []
    if calls:
        turns = sorted({e.turn for e in events})
        if len(turns) <= 60:
            groups = [[e for e in calls if e['turn'] == t] for t in turns]
            groups = [g for g in groups if g]
        else:
            n = min(60, len(calls)); groups = [calls[i*len(calls)//n:(i+1)*len(calls)//n] for i in range(n)]
        previous = -1
        for group in groups:
            end = group[-1]['seq']
            # Attribute trailing events to the final bucket.
            is_last = group is groups[-1]
            marks = sorted({m for seq, turn, m in markers if previous < seq and (seq <= end or is_last)})
            bucket_events = [x for x in additions if previous < x['seq'] and (x['seq'] <= end or is_last)]
            consumer = defaultdict(int)
            for x in bucket_events:
                if x['kind'] in ('tool_call', 'tool_result'): consumer[(x['tool'], x['head'])] += x['tokens']
            top = max(consumer, key=lambda k: (consumer[k], k)) if consumer else None
            total_sources = {c['sources']['total'] for c in group}
            buckets.append({'turns': [group[0]['turn'], group[-1]['turn']], 'start': group[0]['ts'], 'end': group[-1]['ts'], 'calls': len(group), 'context_used': group[-1]['context_used'], 'context_source': group[-1]['context_source'], 'context_percent': (100*group[-1]['context_used']/meta.context_window if group[-1]['context_used'] is not None and meta.context_window else None), 'delta_total': sum(c['total'] for c in group), 'delta_total_source': next(iter(total_sources)) if len(total_sources) == 1 else 'mixed', 'top_consumer': ' '.join(top).strip() if top else None, 'markers': ''.join(marks)})
            previous = end
    peak = max((c['context_used'] for c in calls if c['context_used'] is not None), default=None)
    added_total = sum(x['tokens'] for x in additions)
    resent = sum(c['context_used'] for c in calls if c['context_used'] is not None)
    return {'duration_seconds': _duration(meta.started, meta.ended), 'turns': last_turn, 'model_calls': len(calls), 'tool_calls': sum(e.kind == 'tool_call' for e in events), 'totals': totals, 'total_sources': sources, 'uncached_input': (sum(c['uncached_input'] for c in calls) if all(c['uncached_input'] is not None for c in calls) else None) if calls else totals['input'], 'peak_context': peak, 'peak_percent': 100*peak/meta.context_window if peak is not None and meta.context_window else None, 'context_window': meta.context_window, 'compactions': sum(e.kind == 'compaction' for e in events), 'completed': any(e.kind == 'task_end' for e in events), 'errors': errors, 'calls': calls, 'timeline': buckets, 'context_sparkline': _spark([c['context_used'] or 0 for c in calls]), 'cumulative_sparkline': _spark([c['cumulative_total'] for c in calls]), 'categories': [{'category': k, 'tokens': v, 'share': v/added_total if added_total else 0} for k,v in sorted(categories.items(), key=lambda x: (-x[1], x[0]))], 'tokens_added': added_total, 'resent_context': resent, 'resent_ratio': resent/added_total if added_total else None, 'carry_cost': sorted(additions, key=lambda x: (-x['carry_cost'], x['seq']))[:10], 'biggest_events': sorted(additions, key=lambda x: (-x['tokens'], x['seq']))[:15], 'turn_health': turn_health}

def _markdown(data):
    lines = ['# Profile', f"Duration: {data['duration_seconds']} s; turns: {data['turns']}; model calls: {data['model_calls']}; tool calls: {data['tool_calls']}", f"Totals: {data['totals']} (sources: {data['total_sources']}); uncached input: {data['uncached_input']}", f"Peak context: {data['peak_context']} / {data['context_window']} ({data['peak_percent']}%); compactions: {data['compactions']}; completed: {data['completed']}", f"Errors: {len(data['errors'])}; rate-limit errors: {sum(e['rate_limit'] for e in data['errors'])}; usage-limit errors: {sum(e['usage_limit'] for e in data['errors'])}", '## Timeline', '| Turns | Time | Calls | Context (%) | \u0394 tokens | Top consumer | Markers |', '|---|---|---:|---|---:|---|---|']
    for b in data['timeline']:
        lines.append(f"| {b['turns']} | {b['start']} \u2013 {b['end']} | {b['calls']} | {b['context_used']} ({b['context_percent']}%, {b['context_source']}) | {b['delta_total']} ({b['delta_total_source']}) | {b['top_consumer']} | {b['markers']} |")
    lines += ['## Cache creation per call']
    lines += [f"- E{c['seq']}: {c['cache_creation_input']} ({c['sources']['cache_creation_input']})" for c in data['calls'][:60]]
    if len(data['calls']) > 60: lines.append('Additional calls are in profile.json and profile.csv.')
    lines += ['## Context per call', '`'+data['context_sparkline']+'`', '## Cumulative total', '`'+data['cumulative_sparkline']+'`', '## Context additions']
    lines += [f"- {x['category']}: {x['tokens']} ({x['share']:.1%}, estimated)" for x in data['categories'][:12]]
    lines += [f"Re-sent context: {data['resent_context']}; new estimated tokens: {data['tokens_added']}; ratio: {data['resent_ratio']}", '## Carry cost']
    lines += [f"- {x['ref']}: {x['carry_cost']} ({x['tokens']} \u00d7 {x['later_calls']}) {x['tool']} {x['head']}" for x in data['carry_cost']]
    lines += ['## Biggest events'] + [f"- {x['ref']}: {x['tokens']} {x['kind']} {x['tool']} {x['head']}" for x in data['biggest_events']]
    lines += ['## Errors and limits'] + [f"- {x['ref']} {x['ts']}: {x['text']}" for x in data['errors']]
    return '\n'.join(lines)

def run(args):
    meta, events = load_session(args.session, args.root, threads_csv=getattr(args, 'threads_csv', None))
    data = analyze(meta, events)
    if meta.partial:
        data['partial'] = True
        data['partial_reason'] = meta.partial_reason
    threads = meta.extra.get('threads')
    if threads is not None: data['threads'] = threads
    inputs = input_paths([meta], getattr(args, 'threads_csv', None))
    directory = checked_destination(Path(args.out) if args.out else default_out(meta.session_id), inputs=inputs)
    for suffix in ('md', 'json'): checked_destination(directory / ('profile.' + suffix), inputs=inputs)
    target = checked_destination(directory / 'profile.csv', inputs=inputs)
    if target.is_symlink() or (target.exists() and target.stat().st_nlink > 1): raise ValueError('Report files must not be symlinks or hard links')
    markdown = _markdown(data)
    if meta.partial: markdown = 'Partial session: ' + str(meta.partial_reason) + '\n\n' + markdown
    if threads is not None:
        from .core.out import cap_text
        from .core.tokens import est_tokens
        columns = ('thread_id', 'parent', 'source', 'requests', 'tokens', 'start', 'end')
        section = '## Sub-threads (from CSV; not in dumps)\n| Thread | Parent | Source | Requests | Tokens | Start | End |\n|---|---|---|---:|---:|---|---|\n'
        section += '\n'.join('| ' + ' | '.join(str(row.get(key) or '').replace('|', '\\|').replace('\n', ' ') for key in columns) + ' |' for row in threads)
        section = cap_text(section, 1200)
        markdown = section + '\n\n' + cap_text(markdown, max(1, 2850 - est_tokens(section)))
    directory = write_outputs('profile', meta, data, markdown, args.out, max_tokens=3000)
    fields = ['seq', 'turn', 'ts', 'context_used', 'context_source', 'input', 'cached_input', 'cache_creation_input', 'output', 'reasoning', 'total', 'uncached_input', 'tokens_added', 'cumulative_total', 'sources']
    with (directory/'profile.csv').open('w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields, lineterminator='\n'); writer.writeheader(); writer.writerows(data['calls'])
    summary({'session': meta.session_id, 'model_calls': data['model_calls'], 'peak_context': data['peak_context'], 'totals': data['totals']}, json_mode=args.json)
    return 0

def register(subparsers):
    parser = subparsers.add_parser('profile', help='Analyze token usage and context')
    parser.add_argument('session'); parser.add_argument('--root', action='append', type=Path)
    add_common_arguments(parser); parser.set_defaults(func=run)
