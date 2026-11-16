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

"""Read local automation metadata and manage explicit snapshots."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import hashlib
import os
import re
import shutil
import tomllib
import warnings

from .core.config import add_common_arguments
from .core.normalize import norm_for_hash
from .core.out import summary, write_outputs, cap_text
from .core.paths import checked_destination
from .core.redact import redact
from .core.tokens import est_tokens
from .readers.base import SessionMeta
from .readers.discover import default_roots, discover_sessions, read_session


def home():
    return default_roots()['codex'] / 'automations'


def configurations():
    result = []
    for path in sorted(home().glob('*/automation.toml')):
        with path.open('rb') as f:
            item = tomllib.load(f)
        item['_dir'] = path.parent
        result.append(item)
    return result


def _shingles(text):
    words = norm_for_hash(redact(text)[0]).split()
    return {hashlib.sha256(' '.join(words[i:i+5]).encode('utf-8')).hexdigest() for i in range(len(words)-4)}


def _tz_data_hint():
    """Explain how to obtain IANA data when none is reachable by zoneinfo."""
    import importlib.util
    import zoneinfo
    if importlib.util.find_spec('tzdata') is None and not any(Path(p).is_dir() for p in zoneinfo.TZPATH):
        return (' No IANA timezone data was found: install agentlog with pip or pipx (which installs the'
                ' tzdata package on Windows), or run "python -m pip install tzdata".')
    return ''


def _schedule_zone(requested):
    """Load a named zone from IANA data; unavailable zones make schedule fit unknown."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        return ZoneInfo(requested)
    except (ZoneInfoNotFoundError, ValueError):
        label = ascii(redact(requested)[0][:120])
        warnings.warn(f'Timezone {label} unavailable; schedule fit is unknown.' + _tz_data_hint(),
                      UserWarning, stacklevel=2)
        raise ZoneInfoNotFoundError(requested) from None


def _schedule_fit(rule, started):
    """Use DTSTART time defaults, or explicit minute-resolution unanchored times."""
    try:
        lines = rule.splitlines()
        recurrence = next((line for line in lines if line.startswith('RRULE:')), lines[-1])
        parts = dict(part.split('=', 1) for part in recurrence.removeprefix('RRULE:').split(';'))
        if set(parts) - {'FREQ', 'INTERVAL', 'BYHOUR', 'BYMINUTE', 'BYDAY'}: return None
        freq = parts['FREQ']
        if freq not in ('DAILY', 'WEEKLY', 'HOURLY'): return None
        interval = int(parts.get('INTERVAL', '1'))
        if interval < 1: return None
        dt = datetime.fromisoformat(started.replace('Z', '+00:00'))
        if dt.tzinfo is None: return None
        anchor = None
        for line in lines:
            if line.startswith('DTSTART'):
                prefix, stamp = line.split(':', 1)
                tzmatch = re.search(r'TZID=([^;:]+)', prefix)
                wall_anchor = datetime.strptime(stamp.removesuffix('Z'), '%Y%m%dT%H%M%S')
                zone = _schedule_zone(tzmatch.group(1)) if tzmatch and not stamp.endswith('Z') else timezone.utc
                anchor = wall_anchor.replace(tzinfo=zone)
                break
        if anchor: dt = dt.astimezone(anchor.tzinfo)
        if anchor is None and ('BYMINUTE' not in parts or (freq != 'HOURLY' and 'BYHOUR' not in parts)):
            return None
        hours = [int(x) for x in parts['BYHOUR'].split(',')] if 'BYHOUR' in parts else [anchor.hour] if anchor else []
        minutes = [int(x) for x in parts['BYMINUTE'].split(',')] if 'BYMINUTE' in parts else [anchor.minute]
        second = anchor.second if anchor else 0
        days = parts.get('BYDAY', '').split(',') if 'BYDAY' in parts else []
        weekdays = ['MO', 'TU', 'WE', 'TH', 'FR', 'SA', 'SU']
        if any(x not in weekdays for x in days) or any(not 0 <= x < 24 for x in hours) or any(not 0 <= x < 60 for x in minutes): return None
        # INTERVAL cannot be anchored without DTSTART; do not invent an anchor.
        if interval != 1 and anchor is None: return None
        if freq == 'WEEKLY' and not days and anchor is None: return None
        for offset in (-1, 0, 1):
            day = dt + timedelta(days=offset)
            if days and weekdays[day.weekday()] not in days: continue
            candidates = range(24) if freq == 'HOURLY' and 'BYHOUR' not in parts else hours
            for hour in candidates:
                for minute in minutes:
                    wall = day.replace(hour=hour, minute=minute, second=second, microsecond=0)
                    candidates_utc = set()
                    # Calendar recurrences use the first occurrence of an ambiguous wall time.
                    for fold in (0, 1) if freq == 'HOURLY' else (0,):
                        candidate = wall.replace(fold=fold)
                        instant = candidate.astimezone(timezone.utc)
                        local = instant.astimezone(candidate.tzinfo)
                        if local.replace(tzinfo=None) != candidate.replace(tzinfo=None) or local.fold != fold:
                            continue
                        if instant in candidates_utc: continue
                        candidates_utc.add(instant)
                        if anchor:
                            if instant < anchor.astimezone(timezone.utc): continue
                            if freq == 'DAILY': periods = (candidate.date() - anchor.date()).days
                            elif freq == 'WEEKLY':
                                periods = ((candidate.date() - timedelta(days=candidate.weekday())) - (anchor.date() - timedelta(days=anchor.weekday()))).days // 7
                                if not days and candidate.weekday() != anchor.weekday(): continue
                            else:
                                anchor_hour = anchor.replace(minute=0, second=0, microsecond=0)
                                candidate_hour = candidate.replace(minute=0, second=0, microsecond=0)
                                periods = int((candidate_hour.astimezone(timezone.utc) - anchor_hour.astimezone(timezone.utc)).total_seconds() // 3600)
                            if periods < 0 or periods % interval: continue
                        if abs((instant-dt.astimezone(timezone.utc)).total_seconds()) <= 5400: return True
        return False
    except (ValueError, TypeError, KeyError, AttributeError, OSError, IndexError):
        return None


def match_detail(meta, events=None, configs=None, snapshot_root=None, session_shingles=None):
    if meta.thread_source != 'automation': return None, 'no-match'
    if events is None and session_shingles is None:
        stream = read_session(meta.path)
        next(stream)
        events = stream
    if session_shingles is None:
        pieces = []
        for event in events:
            if event.kind == 'context': pieces.append(event.text)
            elif event.kind == 'user_msg':
                pieces.append(event.text)
                break
        session = _shingles('\n'.join(pieces))
    else: session = set(session_shingles)
    candidates = []
    for config in configs if configs is not None else configurations():
        prompts = [config.get('prompt', '')]
        identifier = config.get('id')
        if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', identifier): continue
        root = Path(snapshot_root or 'agentlog-snapshots') / identifier
        for path in sorted(root.glob('*/automation.toml')):
            with path.open('rb') as f: prompts.append(tomllib.load(f).get('prompt', ''))
        scores = []
        for prompt in prompts:
            shingles = _shingles(prompt)
            scores.append(len(shingles & session) / len(shingles) if shingles else 0)
        containment = max(scores, default=0)
        if containment < .8: continue
        cwd = bool(meta.cwd) and any(os.path.normcase(os.path.normpath(meta.cwd)) == os.path.normcase(os.path.normpath(value)) for value in config.get('cwds', []) if isinstance(value, str))
        fit = _schedule_fit(config.get('rrule', ''), meta.started)
        schedule_score = 2 if fit is True else 1 if fit is None else 0
        candidates.append(((containment, cwd, schedule_score), config.get('id')))
    if not candidates: return None, 'no-match'
    candidates.sort(reverse=True)
    if len(candidates) > 1 and candidates[0][0] == candidates[1][0]: return None, 'ambiguous'
    return candidates[0][1], 'matched'


def match(meta):
    return match_detail(meta)[0]


def _stats(config):
    directory = config['_dir']
    files = sorted(directory.glob('memory*.md'))
    memory = directory / 'memory.md'
    size = memory.stat().st_size if memory.is_file() else 0
    return {'id': config.get('id'), 'name': config.get('name'), 'status': config.get('status'),
            'kind': config.get('kind'), 'schedule': config.get('rrule'), 'model': config.get('model'),
            'effort': config.get('reasoning_effort'), 'cwds': config.get('cwds'),
            'prompt_tokens': est_tokens(config.get('prompt', '')), 'memory_bytes': size,
            'memory_tokens': (size * 2 + 6) // 7,
            'backups': [{'name': f.name, 'bytes': f.stat().st_size} for f in files if f.name != 'memory.md'],
            'updated_at': config.get('updated_at')}


def _schedule_phrase(rule):
    try:
        lines = rule.splitlines()
        recurrence = next((line for line in lines if line.startswith('RRULE:')), lines[-1])
        parts = dict(part.split('=', 1) for part in recurrence.removeprefix('RRULE:').split(';'))
        frequency = parts['FREQ'].lower()
        interval = int(parts.get('INTERVAL', '1'))
        suffix = {'daily': 'days', 'weekly': 'weeks', 'hourly': 'hours'}.get(frequency)
        if not suffix or interval < 1: return rule
        phrase = frequency if interval == 1 else f'every {interval} {suffix}'
        if 'BYDAY' in parts: phrase += ' on ' + parts['BYDAY']
        if 'BYHOUR' in parts: phrase += ' at ' + parts['BYHOUR'] + ':' + parts.get('BYMINUTE', '0').zfill(2)
        zone = re.search(r'TZID=([^;:]+)', rule)
        if zone: phrase += ' ' + zone.group(1)
        return phrase
    except (ValueError, KeyError, AttributeError, IndexError):
        return rule


def _find(identifier, configs):
    if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', identifier): raise ValueError('Unsafe automation ID')
    found = [item for item in configs if item.get('id') == identifier]
    if len(found) != 1: raise ValueError('Automation ID not found or ambiguous')
    return found[0]


def _duration(started, ended):
    try:
        return (datetime.fromisoformat(ended.replace('Z', '+00:00')) - datetime.fromisoformat(started.replace('Z', '+00:00'))).total_seconds()
    except (TypeError, ValueError, AttributeError): return None


def _sessions(roots, configs):
    rows = []
    for path in discover_sessions(roots):
        stream = read_session(path)
        meta = next(stream)
        if meta.thread_source != 'automation': continue
        prefix = []; found_user = False
        count = turns = compactions = errors = 0
        for event in stream:
            if not found_user and event.kind in ('context', 'user_msg'):
                prefix.append(event)
                found_user = event.kind == 'user_msg'
            count += 1; turns = max(turns, event.turn)
            compactions += event.kind == 'compaction'; errors += event.kind == 'error'
        identifier, reason = match_detail(meta, prefix, configs)
        rows.append({'id': meta.session_id, 'id8': meta.session_id[:8], 'started': meta.started, 'source': meta.source,
                     'model': meta.model, 'effort': meta.reasoning_effort, 'cli_version': meta.cli_version,
                     'duration': _duration(meta.started, meta.ended), 'thread_source': meta.thread_source,
                     'bytes': meta.bytes, 'est_tokens': (meta.bytes * 2 + 6) // 7, 'title': (meta.title or '')[:60], 'turns': turns,
                     'events': count, 'compactions': compactions,
                     'errors': errors, 'automation_id': identifier,
                     'match_reason': reason})
    return sorted(rows, key=lambda r: (r['started'] or '', r['id']))


def _hashes(files):
    return {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in files}


def snapshot(configs, dest):
    dest = checked_destination(dest, inputs=[c['_dir'] for c in configs])
    written = 0
    for config in configs:
        identifier = config.get('id')
        if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', identifier): raise ValueError('Unsafe automation ID')
        files = [config['_dir'] / 'automation.toml'] + sorted(config['_dir'].glob('memory*.md'))
        target = checked_destination(dest / identifier, inputs=[c['_dir'] for c in configs])
        previous = sorted(p for p in target.glob('*') if p.is_dir())
        if previous:
            old = [p for p in previous[-1].iterdir() if p.is_file()]
            if _hashes(files) == _hashes(old): continue
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        folder = checked_destination(target / stamp, inputs=[c['_dir'] for c in configs])
        folder.mkdir(parents=True)
        for source in files:
            destination = checked_destination(folder / source.name, inputs=[c['_dir'] for c in configs])
            shutil.copyfile(source, destination)
        written += 1
    return written


def run(args):
    configs = configurations()
    if args.action == 'snapshot':
        selected = [_find(args.id, configs)] if args.id else configs
        summary({'snapshots_written': snapshot(selected, args.dest)}, json_mode=args.json)
        return 0
    if args.action == 'list':
        runs = _sessions(args.root, configs)
        data = [_stats(c) for c in configs]
        for row in data:
            row['runs'] = sum(r['automation_id'] == row['id'] for r in runs)
            row['schedule_phrase'] = _schedule_phrase(row['schedule'])
    elif args.action == 'runs':
        _find(args.id, configs)
        data = [r for r in _sessions(args.root, configs) if r['automation_id'] == args.id or r['match_reason'] == 'ambiguous']
    else:
        item = _find(args.id, configs)
        data = _stats(item)
        if args.memory:
            data['prompt'] = cap_text(redact(item.get('prompt', ''))[0], 2000)
            memory = item['_dir'] / 'memory.md'
            data['memory'] = cap_text(redact(memory.read_text(encoding='utf-8'))[0], 2000) if memory.is_file() else ''
    markdown = '# Automations ' + args.action + '\n' + '\n'.join('- ' + str(row) for row in (data if isinstance(data, list) else [data]))
    meta = SessionMeta('automations', 'generic', '', sum(c['_dir'].joinpath('automation.toml').stat().st_size for c in configs))
    write_outputs('automations', meta, data, markdown, args.out, max_tokens=4000)
    if args.json: summary(data, json_mode=True)
    elif isinstance(data, list): summary('\n'.join(str(row) for row in data))
    else: summary(data)
    return 0


def register(subparsers):
    parser = subparsers.add_parser('automations')
    actions = parser.add_subparsers(dest='action', required=True)
    for name in ('list', 'runs', 'show', 'snapshot'):
        p = actions.add_parser(name)
        if name in ('runs', 'show'): p.add_argument('id')
        if name in ('list', 'runs'): p.add_argument('--root', action='append')
        if name == 'show': p.add_argument('--memory', action='store_true')
        if name == 'snapshot':
            p.add_argument('--dest', type=Path, default=Path('agentlog-snapshots'))
            p.add_argument('--id')
        add_common_arguments(p)
        p.set_defaults(func=run)
