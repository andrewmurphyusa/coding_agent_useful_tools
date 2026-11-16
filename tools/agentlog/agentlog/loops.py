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

"""Deterministic loop and stall findings from normalized sessions."""
from collections import defaultdict
from dataclasses import asdict
import json
from pathlib import Path
import re

from . import compress
from .core.config import add_common_arguments, default_out, hard_cap
from .core.normalize import mask_volatile
from .core.out import summary, write_outputs
from .core.paths import checked_destination
from .core.tokens import est_tokens
from .readers.discover import input_paths, load_session

THRESHOLDS = {
    'R1': {'min': 3, 'high': 5, 'window': 50}, 'R2': {'min': 3},
    'R3': {}, 'R4': {'window': 200}, 'R5': {'min': 3, 'high': 20000},
    'R6': {'min': 3, 'factor': 2}, 'R7': {'min': 5, 'novelty': 0.10},
    'R8': {'ratio': 0.5, 'turns': 10}, 'R9': {'ratio': 0.3, 'context': 0.85},
    'R10': {'max': 20, 'depth': 2}, 'R11': {'min': 3, 'growth': 0.25}, 'R12': {},
}
WHY = {
    'R1': 'Repeated calls can spend tokens without advancing the task.',
    'R2': 'Repeated errors can indicate an unresolved failure.',
    'R3': 'Repeated attempts have the same outcome.',
    'R4': 'Reversing an earlier edit can indicate oscillation.',
    'R5': 'Repeated unchanged reads consume context.',
    'R6': 'Growing output can crowd out useful context.',
    'R7': 'Low novelty without writes can indicate a stall.',
    'R8': 'Reacquiring context after compaction consumes tokens.',
    'R9': 'High context occupancy increases compaction and limit risk.',
    'R10': 'Excessive delegation can increase coordination cost.',
    'R11': 'Growing instructions repeatedly consume context.',
    'R12': 'A limit can interrupt or degrade the run.',
}
READ = re.compile(r'(?i)\b(?:read(?:_file)?|cat|type|get-content|rg|grep|sed|head|tail)\b')
WRITE = re.compile(r'(?i)\b(?:write(?:_file)?|edit|apply_patch|set-content|add-content|out-file|tee|touch|mv|cp)\b|(?<![<>=])>{1,2}(?![=])')
PATH = re.compile(r"""(?:[A-Za-z]:[\\/]|/|\.{0,2}[\\/])?[\w.-]+(?:[\\/][\w. -]+)+|[\w.-]+\.[A-Za-z0-9]{1,12}""")
INSTRUCTION = re.compile(r'(?i)memory\.md|agents\.md|claude\.md|todo|status|notes')
LIMIT = re.compile(r'(?i)(?:usage|rate|context|quota|token).{0,40}(?:limit|exceed|exhaust|maximum|too long)|(?:limit|exceed|exhaust).{0,40}(?:usage|rate|context|quota|token)')


def _paths(event):
    return {m.group().strip().replace('\\', '/').casefold() for m in PATH.finditer(event.text)}


def _read(event):
    return event.kind == 'tool_call' and bool(READ.search((event.tool or '') + ' ' + event.text))


def _write(event):
    return event.kind == 'tool_call' and bool(WRITE.search((event.tool or '') + ' ' + event.text))


def _result_costs(events):
    costs = defaultdict(int)
    for event in events:
        if event.kind == 'tool_result' and event.call_id:
            costs[event.call_id] += event.est_tokens
    return costs


def _cost(event, costs):
    return event.est_tokens + (costs.get(event.call_id, 0) if event.kind == 'tool_call' else 0)


def _finding(rule, title, events, cost, severity='high', evidence=None, count=None):
    refs = [f'E{e.seq}' for e in events]
    return {'rule': rule, 'severity': severity, 'title': title[:100],
            'evidence': (evidence or refs)[:8], 'first': refs[0], 'last': refs[-1],
            'occurrences': count if count is not None else len(events), 'tokens': cost,
            'why_it_matters': WHY[rule]}


def _groups(items, minimum):
    return [group for group in items.values() if len(group) >= minimum]


def _thread_chains(meta):
    rows = meta.extra.get('threads')
    if not isinstance(rows, list): return [], 0, False
    def valid(value):
        return isinstance(value, str) and bool(value) and '[redacted:' not in value.casefold()
    parents = defaultdict(set)
    invalid = 0
    for row in rows:
        if not isinstance(row, dict) or not valid(row.get('thread_id')) or not valid(row.get('parent')):
            invalid += 1
        else:
            parents[row['thread_id']].add(row['parent'])
    chains = []
    unresolved = invalid
    for child in sorted(parents):
        chain = [child]
        while chain[-1] != meta.session_id:
            choices = parents.get(chain[-1], set())
            if len(choices) != 1 or next(iter(choices)) in chain or not valid(meta.session_id):
                unresolved += 1
                break
            chain.append(next(iter(choices)))
        else:
            chains.append(list(reversed(chain)))
    return chains, unresolved, True


def analyze(meta, events, compressed, thresholds=None, rules=None):
    t = thresholds or THRESHOLDS
    enabled = set(rules or THRESHOLDS)
    findings = []
    costs = _result_costs(events)
    def add(rule, title, group, cost=None, severity='high', evidence=None, count=None):
        if rule in enabled and group:
            findings.append(_finding(rule, title, group, sum(_cost(e, costs) for e in group) if cost is None else cost, severity, evidence, count))
    calls = [e for e in events if e.kind == 'tool_call']
    writes = [(e.seq, _paths(e)) for e in events if _write(e)]
    def changed(a, b, paths):
        return any(a < seq < b and (not paths or not wp or bool(paths & wp)) for seq, wp in writes)
    def finish_calls(group):
        start = 0
        best = []
        for end, event in enumerate(group):
            while event.seq - group[start].seq >= t['R1']['window']:
                start += 1
            if end - start + 1 > len(best):
                best = group[start:end + 1]
        if len(best) >= t['R1']['min']:
            add('R1', 'Repeated tool call', best, severity='high' if len(best) >= t['R1']['high'] else 'medium')
    groups = defaultdict(list)
    for e in calls:
        key = (e.tool, mask_volatile(e.text)[0])
        group = groups[key]
        if group and changed(group[-1].seq, e.seq, _paths(e)):
            finish_calls(group)
            group = groups[key] = []
        group.append(e)
    for group in groups.values(): finish_calls(group)
    errors = defaultdict(list)
    for e in events:
        if e.is_error or e.exit_code not in (None, 0):
            errors[(e.exit_code, e.is_error, '\n'.join(mask_volatile(e.text)[0].splitlines()[:3]))].append(e)
    for group in _groups(errors, t['R2']['min']): add('R2', 'Repeated error', group)
    for loop in compressed.get('loops', []):
        if loop.get('no_change_in_outcome'):
            group = [events[i] for i in loop['occurrence_indexes']]
            add('R3', 'Loop with no change in outcome', group, cost=sum(e.est_tokens for e in group), evidence=[loop['ref'], f"E{group[0].seq}", f"E{group[-1].seq}"], count=loop['repetitions'])
    patches = defaultdict(list)
    for e in calls:
        lines = e.text.splitlines()
        added = tuple(line[1:].strip() for line in lines if line.startswith('+') and not line.startswith('+++'))
        removed = tuple(line[1:].strip() for line in lines if line.startswith('-') and not line.startswith('---'))
        if not added or not removed: continue
        for path in sorted(_paths(e)):
            for prior, old_added, old_removed in reversed(patches[path]):
                if e.seq-prior.seq > t['R4']['window']: break
                if added == old_removed and removed == old_added:
                    add('R4', 'Edit reverted', [prior, e], severity='medium'); break
            patches[path].append((e, added, removed))
    reads = defaultdict(list)
    def finish_read(path, group):
        if len(group) >= t['R5']['min']:
            cost = sum(costs.get(e.call_id, 0) for e in group[1:])
            add('R5', 'Repeated unchanged read', group, cost, 'high' if cost > t['R5']['high'] else 'medium')
    for e in events:
        if _write(e):
            paths = _paths(e)
            for path in list(reads):
                if not paths or path in paths:
                    finish_read(path, reads.pop(path))
        if _read(e):
            for path in sorted(_paths(e)): reads[path].append(e)
    for path, group in reads.items(): finish_read(path, group)
    commands = defaultdict(list)
    for e in calls: commands[(e.tool, mask_volatile(e.text)[0])].append(e)
    for group in commands.values():
        run = []
        for e in group:
            if run and costs.get(e.call_id, 0) <= costs.get(run[-1].call_id, 0):
                if len(run) >= t['R6']['min'] and costs.get(run[-1].call_id, 0) >= t['R6']['factor'] * costs.get(run[0].call_id, 0): add('R6', 'Growing command output', run, severity='medium')
                run = []
            run.append(e)
        if len(run) >= t['R6']['min'] and costs.get(run[-1].call_id, 0) >= t['R6']['factor'] * costs.get(run[0].call_id, 0): add('R6', 'Growing command output', run, severity='medium')
    turn_events = defaultdict(list)
    for e in events: turn_events[e.turn].append(e)
    stretch = []
    def finish_stretch(group):
        if len(group) >= t['R7']['min']:
            selected = [e for turn in group for e in turn_events[turn]]
            add('R7', 'No-progress stretch', selected, count=len(group))
    for row in compressed['stats']['novelty_per_turn']:
        turn = row['turn']
        if row['novelty_ratio'] < t['R7']['novelty'] and not any(_write(e) for e in turn_events[turn]) and (not stretch or turn == stretch[-1]+1): stretch.append(turn)
        else:
            finish_stretch(stretch); stretch = []
            if row['novelty_ratio'] < t['R7']['novelty'] and not any(_write(e) for e in turn_events[turn]): stretch = [turn]
    finish_stretch(stretch)
    for compact in (e for e in events if e.kind == 'compaction'):
        prior = [e for e in calls if e.seq < compact.seq and _read(e)]
        paths = set().union(*(_paths(e) for e in prior)) if prior else set()
        denominator = sum(costs.get(e.call_id, 0) for e in prior)
        later = [e for e in calls if e.seq > compact.seq and compact.turn <= e.turn <= compact.turn+t['R8']['turns'] and _read(e) and _paths(e) & paths]
        cost = sum(costs.get(e.call_id, 0) for e in later)
        if denominator and cost / denominator >= t['R8']['ratio']: add('R8', 'Post-compaction reacquisition', [compact]+later, cost, count=len(later))
    usages = [e for e in events if e.usage and e.usage.context_used is not None]
    saturated = [e for e in usages if meta.context_window and e.usage.context_used >= t['R9']['context']*meta.context_window]
    if usages and len(saturated)/len(usages) >= t['R9']['ratio']: add('R9', 'Context saturation', saturated, count=len(saturated))
    fan = defaultdict(list)
    for e in calls:
        if re.search(r'(?i)spawn|agent|task|delegate', e.tool or ''): fan[e.turn].append(e)
    for group in fan.values():
        if len(group) > t['R10']['max']: add('R10', 'Sub-agent fan-out', group, severity='medium')
    chains, unresolved, has_threads = _thread_chains(meta)
    deep = [chain for chain in chains if len(chain) - 1 > t['R10']['depth']]
    if deep and 'R10' in enabled:
        findings.append({'rule': 'R10', 'severity': 'medium', 'title': 'Sub-agent nesting depth',
                         'evidence': [], 'evidence_kind': 'thread_metadata', 'metadata_evidence': deep[:8],
                         'first': None, 'last': None, 'occurrences': len(deep), 'tokens': None,
                         'why_it_matters': WHY['R10']})
    instructions = defaultdict(list)
    for e in calls:
        if _read(e):
            for path in sorted(_paths(e)):
                if INSTRUCTION.search(path): instructions[path].append(e)
    for group in instructions.values():
        turns = {e.turn for e in group}
        first, last = costs.get(group[0].call_id, 0), costs.get(group[-1].call_id, 0)
        if len(turns) >= t['R11']['min'] and len(turns) == max(turns)-min(turns)+1 and first > 0 and last >= first*(1+t['R11']['growth']): add('R11', 'Instruction file growth', group)
    for e in events:
        if e.kind == 'error' and LIMIT.search(e.text): add('R12', 'Usage or context limit hit', [e])
    findings.sort(key=lambda f: (f['tokens'] is None, -(f['tokens'] or 0), int(f['rule'][1:]), int(f['first'][1:]) if f['first'] else -1, f['title']))
    for index, finding in enumerate(findings, 1):
        finding['ref'] = f'F{index}'
        if finding.get('evidence_kind') == 'thread_metadata': finding['show_hint'] = f"show F{index}"
    total = sum(e.est_tokens for e in events)
    cost = sum(f['tokens'] for f in findings if f['tokens'] is not None)
    return {'findings': findings, 'session_tokens': total, 'finding_tokens': cost,
            'finding_percent': 100 * cost / total if total else 0,
            'unknown_cost_findings': sum(f['tokens'] is None for f in findings),
            'unresolved_thread_branches': unresolved,
            'limitations': [f"Thread ancestry: {'CSV metadata evaluated' if has_threads else 'unavailable; nesting depth unsupported'}; {unresolved} unresolved branches.", 'Finding costs can overlap and are estimates.']}


def _markdown(data):
    fs = data['findings']
    lines = [f"Findings: {sum(f['severity']=='high' for f in fs)} high, {sum(f['severity']=='medium' for f in fs)} medium; {data['finding_tokens']} known estimated tokens ({data['finding_percent']:.1f}% of session); {data['unknown_cost_findings']} unknown costs. Costs may overlap.", '', '| F | rule | severity | title | occurrences | tokens | first -> last |', '| --- | --- | --- | --- | ---: | ---: | --- |']
    blocks = []
    for f in fs:
        span = f"{f['first']} -> {f['last']}" if f['first'] else 'unavailable'
        cost = f['tokens'] if f['tokens'] is not None else '?'
        row = f"| {f['ref']} | {f['rule']} | {f['severity']} | {f['title']} | {f['occurrences']} | {cost} | {span} |"
        evidence = ', '.join(f['evidence']) if f['evidence'] else '; '.join(' -> '.join(c) for c in f.get('metadata_evidence', []))
        block = f"\n{f['ref']}: {evidence} (show {f['ref']})\nWhy it matters: {f['why_it_matters']}"
        candidate = '\n'.join(lines+[row, f'... {len(fs)-len(blocks)-1} more (loops.json)']+blocks+[block])
        if est_tokens(candidate) > 2700: break
        lines.append(row); blocks.append(block)
    if len(blocks) < len(fs): lines.append(f'... {len(fs)-len(blocks)} more (loops.json)')
    return '\n'.join(lines+blocks+['', *data['limitations']])


def _overrides(values):
    result = {rule: dict(fields) for rule, fields in THRESHOLDS.items()}
    for value in values or []:
        match = re.fullmatch(r'(R\d+)\.([a-z]+)=(\d+(?:\.\d+)?)', value)
        if not match or match[1] not in result or match[2] not in result[match[1]]: raise ValueError(f'Invalid threshold: {value}')
        original = result[match[1]][match[2]]
        number = float(match[3]) if isinstance(original, float) else int(match[3]) if '.' not in match[3] else None
        if number is None or number <= 0: raise ValueError(f'Invalid threshold: {value}')
        result[match[1]][match[2]] = number
    return result


def run(args):
    thresholds = _overrides(args.set)
    rules = args.rules.split(',') if args.rules else list(THRESHOLDS)
    if not rules or any(r not in THRESHOLDS for r in rules): raise ValueError('Unknown rule in --rules')
    meta, events = load_session(args.session, args.root, threads_csv=args.threads_csv)
    directory = checked_destination(args.out or default_out(meta.session_id), inputs=input_paths([meta], args.threads_csv))
    for name in ('loops.md', 'loops.json', 'compress.md', 'compress.json', 'compress-stats.md'):
        target = directory/name
        checked_destination(target, inputs=input_paths([meta], args.threads_csv))
        if target.is_symlink() or (target.exists() and target.stat().st_nlink > 1):
            raise ValueError('Report files must not be symlinks or hard links')
    cache = directory/'compress.json'
    if cache.exists():
        data = json.loads(cache.read_text(encoding='utf-8'))
        if (not isinstance(data, dict) or data.get('session') != asdict(meta)
                or [o.get('seq') for o in data.get('occurrences', [])] != [e.seq for e in events]):
            raise ValueError('compress.json does not match the selected session')
    else:
        data = compress.compress_session(meta, events)
    report = analyze(meta, events, data, thresholds, rules)
    if not cache.exists():
        target = min(8000, hard_cap())
        body = compress.render_compress(data, max_tokens=target)
        rendered = compress._bounded_report('compress', data, body, target)
        stats = compress._bounded_report('compress-stats', data, compress.render_stats(data), target)
        compress._write_reports(directory, meta, data, rendered, stats)
    write_outputs('loops', meta, report, _markdown(report), directory, max_tokens=min(3000, hard_cap()))
    summary({'findings': len(report['findings']), 'high': sum(f['severity']=='high' for f in report['findings']), 'known_tokens': report['finding_tokens'], 'unknown_cost_findings': report['unknown_cost_findings']}, json_mode=args.json)
    return int(bool(args.fail_on and any(f['severity']=='high' or args.fail_on == 'medium' for f in report['findings'])))


def register(subparsers):
    parser = subparsers.add_parser('loops', help='Detect deterministic loop and stall patterns')
    parser.add_argument('session'); parser.add_argument('--root', action='append', type=Path)
    parser.add_argument('--rules'); parser.add_argument('--set', action='append', default=[])
    parser.add_argument('--fail-on', choices=('high', 'medium'))
    add_common_arguments(parser); parser.set_defaults(func=run)
