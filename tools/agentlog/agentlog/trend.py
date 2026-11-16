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

"""Compare cheap run metrics and identify the first degraded run."""
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
import csv
import json
import re
import statistics

from .core.config import add_common_arguments, default_out
from .core.out import dumps, summary, write_outputs
from .core.paths import checked_destination
from .core.tokens import est_tokens
from .readers.base import SessionMeta
from .readers.discover import discover_sessions, read_session, resolve_session

LIMIT = re.compile(r'rate[ -]limit|usage[ -]limit|quota|context length|context window', re.I)
MEMORY = re.compile(r'(?i)memory[^\s"\']*\.md')
READ = re.compile(r'(?i)\b(?:cat|type|head|tail|less|more|get-content|read_file|read|open)\b')
PATH = re.compile(r'''(?:[A-Za-z]:[\\/]|/|\.{0,2}[\\/])?[\w.-]+(?:[\\/][\w. -]+)+|[\w.-]+\.[A-Za-z0-9]{1,12}''')
HEAD = re.compile(r'(?i)\b(git|python|pytest|cargo|npm|cat|type|get-content|rg|ls|dir)\b(?:\s+([\w-]+))?')


def _full_file_read(tool, command, result, paths):
    if len(paths) != 1 or re.search(r'(?i)\b(?:truncated|output omitted)\b', result):
        return False
    try:
        arguments = json.loads(command)
    except (ValueError, TypeError):
        arguments = None
    if tool.casefold() in ('read', 'read_file'):
        if isinstance(arguments, dict):
            return not any(key in arguments for key in ('offset', 'limit', 'start_line', 'end_line'))
        return isinstance(command, str) and command.strip() == paths[0]
    if isinstance(arguments, dict):
        command = arguments.get('command', arguments.get('cmd', ''))
    if not isinstance(command, str):
        return False
    return bool(re.fullmatch(r'''(?i)\s*(?:cat|type|get-content)\s+(?:["'][^"']+["']|[^\s|;&<>]+)\s*''', command))


def duration(start, end):
    try: return (datetime.fromisoformat(end.replace('Z', '+00:00')) - datetime.fromisoformat(start.replace('Z', '+00:00'))).total_seconds()
    except (ValueError, TypeError, AttributeError): return None


def analyze(meta, events, automation_dir=None):
    calls = {}; inputs = defaultdict(int); tools = defaultdict(int); total = uncached = 0
    peak = None; count = turns = compactions = errors = limits = instruction = memory = largest = 0
    completed = False; has_usage = False; uncached_known = True; fallback = pending = 0
    cumulative = None
    for e in events:
        turns = max(turns, e.turn)
        if e.kind in ('tool_call', 'tool_result', 'assistant_msg', 'reasoning', 'user_msg', 'context'):
            fallback += e.est_tokens
            pending += e.est_tokens
        if e.kind == 'context': instruction += e.est_tokens
        if e.kind == 'tool_call':
            calls[e.call_id] = (e.tool or '', e.text)
            head = HEAD.search(e.text)
            tools[(e.tool or '', ' '.join(x for x in head.groups() if x) if head else '')] += 1
        elif e.kind == 'tool_result':
            largest = max(largest, e.est_tokens)
            tool, command = calls.get(e.call_id, ('', ''))
            paths = sorted({path.strip() for path in PATH.findall(command)}) if READ.search(tool + ' ' + command) else []
            memory_paths = [path for path in paths if MEMORY.search(path) or (automation_dir and str(automation_dir).casefold() in path.casefold())]
            if memory_paths or (automation_dir and READ.search(tool + ' ' + command) and str(automation_dir).casefold() in command.casefold()):
                memory += e.est_tokens
                if _full_file_read(tool, command, e.text, paths):
                    memory += est_tokens(e.text.encode('utf-8'))
            for path in paths: inputs[path] += e.est_tokens
        if e.kind == 'token_usage':
            count += 1; has_usage = True
            u = e.usage
            if u:
                total += u.total if u.total is not None else pending
                if u.cumulative_total is not None: cumulative = u.cumulative_total
                if u.context_used is not None: peak = max(peak or 0, u.context_used)
                if u.input is None or (meta.source == 'claude' and u.cache_creation_input is None) or (meta.source != 'claude' and u.cached_input is None): uncached_known = False
                elif meta.source == 'claude': uncached += u.input + u.cache_creation_input
                else: uncached += max(0, u.input - u.cached_input)
            else:
                total += pending
                uncached_known = False
            pending = 0
        if e.kind == 'compaction': compactions += 1
        if e.kind == 'error' or e.is_error:
            errors += 1
            limits += bool(LIMIT.search(e.text))
        completed |= e.kind == 'task_end'
    return {'id': meta.session_id, 'started': meta.started, 'cli_version': meta.cli_version,
            'model': meta.model, 'effort': meta.reasoning_effort, 'instructions_sha1': meta.instructions_sha1,
            'duration': duration(meta.started, meta.ended), 'model_calls': count, 'turns': turns,
            'cumulative_tokens': (cumulative if cumulative is not None else total) if has_usage else fallback,
            'uncached_input': uncached if uncached_known and has_usage else (fallback if not has_usage else None),
            'peak_context_percent': 100 * peak / meta.context_window if peak is not None and meta.context_window else None,
            'compactions': compactions, 'errors': errors, 'limit_hits': limits, 'completed': completed,
            'novelty_ratio': None, 'instruction_tokens': instruction, 'memory_read_tokens': memory,
            'largest_tool_result': largest, 'loops_high': None, 'inputs': dict(inputs),
            'tool_mix': [{'tool': k[0], 'head': k[1], 'count': v} for k,v in sorted(tools.items())]}


def mark(rows):
    numeric = ('duration', 'model_calls', 'turns', 'cumulative_tokens', 'uncached_input', 'peak_context_percent', 'compactions', 'errors', 'limit_hits', 'novelty_ratio', 'instruction_tokens', 'memory_read_tokens', 'largest_tool_result', 'loops_high')
    first = False
    for index, row in enumerate(rows):
        flags = []
        for key in numeric:
            previous = [x[key] for x in rows[max(0, index-5):index] if x.get(key) is not None]
            value = row.get(key)
            if previous and value is not None and value > 0 and value >= 2 * statistics.median(previous): flags.append(key)
        changes = []
        if index:
            for key in ('cli_version', 'model', 'effort', 'instructions_sha1', 'snapshot_version'):
                if row.get(key) != rows[index-1].get(key):
                    changes.append(key)
                    if key in ('cli_version', 'model', 'effort') and row.get(key) is not None: flags.append(key)
        row['flags'] = flags; row['changes'] = changes
        row['first_outlier'] = not first and len(set(flags) & {'cumulative_tokens', 'peak_context_percent', 'compactions', 'duration'}) >= 2
        first |= row['first_outlier']
    return rows


def _enrich(row, meta):
    directory = default_out(meta.session_id)
    for filename, apply in (
        ('profile.json', lambda d: row.update(model_calls=d['model_calls'], uncached_input=d['uncached_input'], peak_context_percent=d['peak_percent'], cumulative_tokens=d['totals']['total'])),
        ('compress.json', lambda d: row.update(novelty_ratio=(sum(x['new_block_tokens'] for x in d['stats']['novelty_per_turn']) / max(1, sum(x['all_tokens'] for x in d['stats']['novelty_per_turn']))))),
        ('loops.json', lambda d: row.update(loops_high=sum(x.get('severity') == 'high' for x in d['findings'])))):
        file = directory / filename
        if file.is_file():
            try: apply(json.loads(file.read_text(encoding='utf-8')))
            except (ValueError, KeyError, TypeError): pass


def _snapshot_version(identifier, started):
    if not identifier or not started: return None
    try:
        when = datetime.fromisoformat(started.replace('Z', '+00:00'))
        snapshots = []
        for path in (Path('agentlog-snapshots') / identifier).iterdir():
            if not path.is_dir(): continue
            try: stamp = datetime.strptime(path.name, '%Y%m%dT%H%M%S%fZ').replace(tzinfo=timezone.utc)
            except ValueError: continue
            if stamp <= when: snapshots.append((stamp, path.name))
        return max(snapshots)[1] if snapshots else None
    except (OSError, ValueError, TypeError): return None


def _included(meta, args):
    if args.since and (not meta.started or meta.started < args.since): return False
    until = getattr(args, 'until', None)
    if until and (not meta.started or meta.started[:len(until)] > until): return False
    for option, field in (('source', 'source'), ('model', 'model'), ('cli_version', 'cli_version'), ('thread_source', 'thread_source')):
        value = getattr(args, option, None)
        if value and getattr(meta, field) != value: return False
    cwd = getattr(args, 'cwd', None)
    if cwd and not (meta.cwd or '').startswith(cwd): return False
    minimum = getattr(args, 'min_bytes', None)
    return minimum is None or meta.bytes >= minimum


def run(args):
    from . import automations
    configs = automations.configurations() if args.automation else []
    config = automations._find(args.automation, configs) if args.automation else None
    paths = [resolve_session(s, args.root) for s in args.sessions] if args.sessions else list(discover_sessions(args.root))
    inputs = paths + ([args.threads_csv] if getattr(args, 'threads_csv', None) and args.source in (None, 'requestdump') and any(p.is_dir() for p in paths) else [])
    cache_name = Path('agentlog-out/.cache/trend.jsonl')
    if cache_name.is_symlink(): raise ValueError('Cache must not be a symlink or hard link')
    cache_path = checked_destination(cache_name, inputs=inputs)
    if cache_path.exists() and cache_path.stat().st_nlink > 1: raise ValueError('Cache must not be a symlink or hard link')
    cache = {}
    if cache_path.is_file():
        for line in cache_path.read_text(encoding='utf-8').splitlines():
            try:
                item = json.loads(line)
                if item.get('version') == 6: cache[(item['path'], item['size'], item['mtime_ns'], item['automation_dir'])] = item
            except (ValueError, TypeError, KeyError): continue
    updated = []
    rows = []; metas = []
    for path in paths:
        if path.is_dir() and args.source not in (None, 'requestdump'): continue
        stat = path.stat(); key = (str(path.resolve()), stat.st_size, stat.st_mtime_ns, str(config['_dir']) if config else '')
        cached = cache.get(key) if not args.deep and not path.is_dir() else None
        if cached:
            try:
                meta = SessionMeta(**cached['meta']); row = dict(cached['row']); shingles = cached['shingles']
            except (KeyError, TypeError): cached = None
        if cached:
            updated.append(cached)
            if not _included(meta, args): continue
            if args.automation and automations.match_detail(meta, configs=configs, session_shingles=shingles)[0] != args.automation: continue
            _enrich(row, meta)
            row['snapshot_version'] = _snapshot_version(args.automation, meta.started)
            rows.append(row); metas.append(meta)
            continue
        stream = read_session(path, threads_csv=getattr(args, 'threads_csv', None)); meta = next(stream)
        if not _included(meta, args): continue
        deep_events = []
        if args.deep:
            def collect_deep(events):
                for event in events:
                    deep_events.append(event)
                    yield event
            stream = collect_deep(stream)
        match_events = []
        def collect(events):
            found_user = False
            for event in events:
                if not found_user and event.kind in ('context', 'user_msg'):
                    match_events.append(event)
                    found_user = event.kind == 'user_msg'
                yield event
        stream = collect(stream)
        row = analyze(meta, stream, config['_dir'] if config else None)
        shingles = sorted(automations._shingles('\n'.join(e.text for e in match_events)))
        if not args.deep and not path.is_dir():
            updated.append({'version': 6, 'path': key[0], 'size': key[1], 'mtime_ns': key[2], 'automation_dir': key[3], 'meta': asdict(meta), 'row': dict(row), 'shingles': shingles})
        if args.automation and automations.match_detail(meta, configs=configs, session_shingles=shingles)[0] != args.automation: continue
        _enrich(row, meta)
        row['snapshot_version'] = _snapshot_version(args.automation, meta.started)
        rows.append(row); metas.append(meta)
        if args.deep:
            from . import profile, compress
            prof = profile.analyze(meta, deep_events)
            compressed = compress.compress_session(meta, deep_events)
            novelty = compressed['stats']['novelty_per_turn']
            denominator = sum(item['all_tokens'] for item in novelty)
            row['novelty_ratio'] = sum(item['new_block_tokens'] for item in novelty) / denominator if denominator else 0
            row.update(model_calls=prof['model_calls'], uncached_input=prof['uncached_input'], peak_context_percent=prof['peak_percent'], cumulative_tokens=prof['totals']['total'])
            try: from . import loops
            except ImportError: row['loops_high'] = None
            else:
                findings = loops.analyze(meta, deep_events)
                if isinstance(findings, dict): findings = findings['findings']
                row['loops_high'] = sum(item.get('severity') == 'high' for item in findings)
    if not args.deep:
        if cache_name.is_symlink() or (cache_path.exists() and cache_path.stat().st_nlink > 1): raise ValueError('Cache must not be a symlink or hard link')
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(''.join(dumps(item) + '\n' for item in updated), encoding='utf-8', newline='\n')
    rows.sort(key=lambda x: (x['started'] or '', x['id']))
    mark(rows)
    lines = ['# Trend']
    first = next((i for i, row in enumerate(rows) if row['first_outlier']), None)
    selected = list(range(len(rows))) if len(rows) <= 20 else sorted(set(range(max(0, (first or 0)-5), min(len(rows), (first or 0)+6))) | set(range(max(0, len(rows)-10), len(rows))))
    rendered = {}
    for i in selected:
        row = rows[i]
        values = []
        for key, value in sorted(row.items()):
            if key in ('inputs', 'tool_mix', 'flags', 'changes', 'first_outlier'): continue
            mark_flag = ' ▲' if key in row['flags'] else ''
            change = ' Δ' if key in row['changes'] else ''
            values.append(f'{key}={value}{mark_flag}{change}')
        rendered[i] = '- ' + ' | '.join(values) + (' FIRST OUTLIER' if row['first_outlier'] else '')
    priority = ([first] if first is not None else []) + list(reversed(selected))
    retained = []
    for i in priority:
        if i in retained: continue
        if est_tokens('\n'.join(lines + [rendered[j] for j in retained] + [rendered[i]])) <= 2800:
            retained.append(i)
    if len(retained) < len(rows): lines.append(f'... {len(rows)-len(retained)} runs omitted; see trend.csv')
    lines.extend(rendered[i] for i in sorted(retained))
    aggregate = SessionMeta('trend', 'generic', '', sum(m.bytes for m in metas))
    directory = checked_destination(Path(args.out) if args.out else default_out('trend'), inputs=inputs)
    csvpath = checked_destination(directory / 'trend.csv', inputs=inputs)
    for suffix in ('md', 'json'): checked_destination(directory / ('trend.' + suffix), inputs=inputs)
    if csvpath.is_symlink() or (csvpath.exists() and csvpath.stat().st_nlink > 1): raise ValueError('Report files must not be symlinks or hard links')
    write_outputs('trend', aggregate, rows, '\n'.join(lines), directory, max_tokens=3000)
    fields = sorted(key for key in rows[0] if key not in ('inputs', 'tool_mix')) if rows else ['id']
    with csvpath.open('w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore', lineterminator='\n'); writer.writeheader(); writer.writerows(rows)
    summary({'runs': len(rows), 'first_outlier': next((r['id'] for r in rows if r['first_outlier']), None)}, json_mode=args.json)
    return 0


def register(subparsers):
    p = subparsers.add_parser('trend'); p.add_argument('sessions', nargs='*'); p.add_argument('--automation'); p.add_argument('--since'); p.add_argument('--until'); p.add_argument('--root', action='append'); p.add_argument('--source', choices=('codex', 'claude', 'generic', 'requestdump')); p.add_argument('--model'); p.add_argument('--cwd'); p.add_argument('--thread-source'); p.add_argument('--cli-version'); p.add_argument('--min-bytes', type=int); p.add_argument('--deep', action='store_true'); add_common_arguments(p); p.set_defaults(func=run)
