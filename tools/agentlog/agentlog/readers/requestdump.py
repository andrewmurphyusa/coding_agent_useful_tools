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

from pathlib import Path
from collections import OrderedDict
import csv
import hashlib
import json
import re

from .base import SessionMeta, Usage, event, fallback_id, safe, utc
from .codex import _CONTEXT
from ..core.redact import redact_object


_FILE = re.compile(r'^request-(\d+)\.txt$')
_ITEM = re.compile(r'^ITEM \d{4}$')
_SEPARATOR = '-' * 60
_FIELDS = {'Input tokens': 'input', 'Cached input tokens': 'cached_input',
           'Output tokens': 'output', 'Reasoning output tokens': 'reasoning',
           'Total tokens': 'total'}


def request_files(folder):
    files = [(int(match.group(1)), path.name, path) for path in Path(folder).iterdir()
             if path.is_file() and (match := _FILE.fullmatch(path.name))]
    return [path for _, _, path in sorted(files)]


def _parse(path):
    header = {}; items = []; fields = None; body = []; section = False
    with path.open(encoding='utf-8-sig') as stream:
        lines = iter(stream)
        while (line := next(lines, None)) is not None:
            line = line.rstrip('\r\n')
            if not section:
                if line == 'RECONSTRUCTED MODEL INPUT': section = True
                elif ': ' in line:
                    key, value = line.split(': ', 1)
                    if key in _FIELDS:
                        try: header[_FIELDS[key]] = int(value.replace(',', ''))
                        except ValueError: raise ValueError('Invalid request token count') from None
                    elif key in ('Session ID', 'Timestamp', 'Turn ID', 'Response ID', 'Request kind'):
                        header[key] = value
                continue
            if line == _SEPARATOR:
                following = next(lines, None)
                if following is not None and _ITEM.fullmatch(following.rstrip('\r\n')):
                    if fields is not None: items.append((*fields, '\n'.join(body)))
                    attributes = {}
                    for attribute in lines:
                        attribute = attribute.rstrip('\r\n')
                        if not attribute: break
                        if ': ' not in attribute: raise ValueError('Invalid request item header')
                        key, value = attribute.split(': ', 1)
                        attributes[key] = value
                    if 'type' not in attributes: raise ValueError('Request item has no type')
                    fields = tuple(attributes.get(k, '') for k in ('type', 'role', 'name', 'call_id'))
                    body = []
                    continue
                if fields is not None:
                    body.append(line)
                if following is not None:
                    from itertools import chain
                    lines = chain((following,), lines)
                continue
            if fields is not None: body.append(line)
    if not section or 'Session ID' not in header: raise ValueError('Invalid request snapshot header')
    if fields is not None: items.append((*fields, '\n'.join(body)))
    return header, items


def _identity(item):
    return hashlib.sha1(json.dumps(item, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).digest()


def _kind(item):
    typ, role, _, _, body = item
    if typ == 'message':
        if role in ('system', 'developer') or (role == 'user' and _CONTEXT.search(body)): return 'context'
        return {'user': 'user_msg', 'assistant': 'assistant_msg'}.get(role, 'other')
    if typ in ('function_call', 'custom_tool_call'): return 'tool_call'
    if typ in ('function_call_output', 'custom_tool_call_output'): return 'tool_result'
    if typ == 'reasoning': return 'reasoning'
    return 'other'


def _threads(path, session_id):
    if path is None: return {}
    rows = []
    with Path(path).open(encoding='utf-8-sig', newline='') as stream:
        for row in csv.DictReader(stream):
            if row.get('session_id') != session_id or row.get('thread_id') == session_id:
                continue
            source = row.get('source', '')
            try: source = json.loads(source)
            except ValueError: pass
            rows.append({'thread_id': row.get('thread_id'), 'parent': row.get('parent_thread_id'),
                         'source': source, 'requests': row.get('requests'),
                         'tokens': row.get('total_tokens'), 'start': row.get('start') or row.get('started') or row.get('start_time'),
                         'end': row.get('end') or row.get('ended') or row.get('end_time')})
    rows.sort(key=lambda row: (row['thread_id'] or '', row['parent'] or ''))
    return {'threads': redact_object(rows)}


def inspect(folder, threads_csv=None):
    files = request_files(folder)
    if not files: raise ValueError('No request snapshots found')
    first = last = None; title = None; instructions = None; errors = 0; session_id = None
    for path in files:
        try: header, items = _parse(path)
        except (ValueError, UnicodeError): errors += 1; continue
        session_id = session_id or header['Session ID']
        timestamp = utc(header.get('Timestamp'))
        first = first or timestamp; last = timestamp or last
        if instructions is None:
            context = '\n'.join(item[4] for item in items if _kind(item) == 'context')
            instructions = hashlib.sha1(context.encode('utf-8')).hexdigest()
        if title is None:
            title = next((item[4].splitlines()[0] for item in items if _kind(item) == 'user_msg' and item[4].splitlines()), None)
    meta = SessionMeta(session_id=safe(session_id or fallback_id(folder)), source='requestdump',
                           path=safe(str(folder)), bytes=sum(p.stat().st_size for p in files),
                           started=first, ended=last, title=safe(title), instructions_sha1=instructions,
                           partial=True, partial_reason='main thread only; reconstructed from snapshots',
                           extra=_threads(threads_csv, session_id))
    return meta, len(files), errors


def read(folder, threads_csv=None):
    meta, _, _ = inspect(folder, threads_csv)
    yield meta
    previous = []; previous_input = None; seq = turn = cumulative = 0; calls = OrderedDict(); first_request = True
    for path in request_files(folder):
        header, items = _parse(path)
        hashes = [_identity(item) for item in items]
        old_length = len(previous)
        prefix = len(hashes) >= old_length and hashes[:old_length] == previous
        if header.get('Request kind') == 'user-turn': turn += 1
        timestamp = {'timestamp': header.get('Timestamp')}
        usage_input = header.get('input')
        if prefix:
            new = items[old_length:]
            if not first_request and len(hashes) == old_length:
                yield event(seq, timestamp, 0, kind='context', text='identical re-send', turn=turn, raw_type='requestdump/identical'); seq += 1
        else:
            old = set(previous); new = [item for item, digest in zip(items, hashes) if digest not in old]
            text = f'context reset: {old_length} \u2192 {len(items)} items; {previous_input} \u2192 {usage_input} tokens'
            if new and _kind(new[0]) == 'user_msg' and 'summary' in new[0][4][:200].lower(): text += '\n' + new[0][4]
            yield event(seq, timestamp, 0, kind='compaction', text=text, turn=turn, raw_type='requestdump/reset'); seq += 1
        previous = hashes; previous_input = usage_input; first_request = False
        for item in new:
            typ, role, name, call_id, body = item; kind = _kind(item)
            if kind == 'tool_call' and call_id:
                calls[call_id] = name or None
                if len(calls) > 1024: calls.popitem(last=False)
            tool = name or (calls.pop(call_id, None) if kind == 'tool_result' else None)
            match = re.search(r'(?:Process exited with code|exit(?:[_ ]code)?\s*[:=])\s*(-?\d+)', body, re.I)
            code = int(match[1]) if kind == 'tool_result' and match else None
            mapped_role = 'tool' if kind == 'tool_result' else 'assistant' if kind in ('tool_call', 'reasoning') else role if role != '-' else None
            yield event(seq, timestamp, len(body.encode('utf-8')), kind=kind, role=mapped_role, tool=tool, call_id=call_id or None, text=body, exit_code=code, is_error=code is not None and code != 0, turn=turn, raw_type='requestdump/'+typ); seq += 1
        total = header.get('total')
        if total is not None: cumulative += total
        usage = Usage(input=usage_input, cached_input=header.get('cached_input'), output=header.get('output'), reasoning=header.get('reasoning'), total=total, context_used=usage_input, cumulative_total=cumulative if total is not None else None)
        yield event(seq, timestamp, 0, kind='token_usage', usage=usage, turn=turn, raw_type='requestdump/usage'); seq += 1


read_session = read
