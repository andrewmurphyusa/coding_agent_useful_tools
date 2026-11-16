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

"""Build a redacted local FTS index and retrieve bounded conversation windows."""
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time

from . import __version__
from .core.out import summary
from .core.paths import checked_destination
from .core.redact import redact
from .core.tokens import est_tokens
from .core.zst import ZstdUnavailable
from .readers.discover import discover_sessions, read_session
from .readers.claude import strip_context_wrappers
from .readers.requestdump import request_files

REDACTION_VERSION = '1'
SCHEMA = '''
CREATE TABLE sessions(session_id TEXT PRIMARY KEY, source TEXT, path TEXT, project TEXT, cwd TEXT, title TEXT, first_ts TEXT, last_ts TEXT, n_msgs INT, term_hits INT, included INT, match_reason TEXT);
CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, ordinal INT, ts TEXT, role TEXT, sidechain INT, text TEXT, est_tokens INT);
CREATE VIRTUAL TABLE messages_fts USING fts5(text, content='messages', content_rowid='id', tokenize='porter unicode61');
CREATE TABLE files(path TEXT PRIMARY KEY, size INT, mtime_ns INT, fingerprint TEXT, session_id TEXT, source_path TEXT);
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
'''


def _destination(path, inputs=()):
    path = Path(path)
    if path.is_symlink() or (path.exists() and path.is_file() and path.stat().st_nlink > 1):
        raise ValueError('Destination must not be a symlink or hard link')
    return checked_destination(path, inputs=inputs)


def _fingerprint(path, csv_path):
    files = request_files(path) if path.is_dir() else [path]
    if path.is_dir() and csv_path: files.append(Path(csv_path))
    parts = [(p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in files]
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest(), sum(x[1] for x in parts), max((x[2] for x in parts), default=0)


def _remove(connection, session_id):
    for rowid, text in connection.execute('SELECT id, text FROM messages WHERE session_id=?', (session_id,)):
        connection.execute("INSERT INTO messages_fts(messages_fts,rowid,text) VALUES('delete',?,?)", (rowid, text))
    connection.execute('DELETE FROM messages WHERE session_id=?', (session_id,))
    connection.execute('DELETE FROM sessions WHERE session_id=?', (session_id,))


def _index_one(connection, path, args, patterns):
    stream = read_session(path, threads_csv=args.threads_csv, cwd_prefixes=args.cwd_prefix)
    meta = next(stream)
    if args.source and args.source != meta.source: return None
    if args.since and (not meta.started or meta.started < args.since): return None
    if args.until and (not meta.started or meta.started[:len(args.until)] > args.until): return None
    project_path = path.parent.parent.parent if path.parent.name == 'subagents' else path.parent
    project = redact(project_path.name)[0]
    matches = []
    if meta.source == 'claude' and any(p.search(project_path.name) for p in patterns['project']): matches.append('project')
    if meta.extra.get('cwd_prefix_match'): matches.append('cwd')
    filtered = bool(args.cwd_prefix or args.project_pattern or args.term)
    # Stage kept rows in SQLite, not memory, while selection and term counts are computed.
    connection.execute('DELETE FROM pending')
    hits = count = 0
    for event in stream:
        if event.kind in ('user_msg', 'assistant_msg'):
            text = strip_context_wrappers(event.text) if meta.source == 'claude' else event.text
            text = redact(text.strip())[0]
            role = 'user' if event.kind == 'user_msg' else 'assistant'
        elif args.keep_tool_stubs and event.kind == 'tool_call':
            text = redact('[tool: ' + (event.tool or 'unknown') + ']')[0]
            role = 'assistant'
        else: continue
        if len(text) < 3: continue
        hits += sum(len(p.findall(text)) for p in patterns['term'])
        connection.execute('INSERT INTO pending VALUES(?,?,?,?,?,?)', (event.seq, event.ts, role, int(event.sidechain), text, est_tokens(text)))
        count += 1
    if args.term and hits >= args.min_term_hits: matches.append('term')
    included = not filtered or bool(matches)
    sid = meta.session_id
    existing = connection.execute('SELECT path FROM sessions WHERE session_id=?', (sid,)).fetchone()
    safe_path = redact(str(path.resolve()))[0]
    source_key = hashlib.sha256(str(path.resolve()).encode()).hexdigest()
    other = connection.execute('SELECT 1 FROM files WHERE session_id=? AND path<>?', (sid, source_key)).fetchone()
    if other or (existing and existing[0] != safe_path):
        raise ValueError('Multiple inputs have the same session ID; index one source at a time')
    _remove(connection, sid)
    cwd = meta.cwd if isinstance(meta.cwd, str) else None
    title = meta.title if isinstance(meta.title, str) else None
    connection.execute('INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', (sid, meta.source, safe_path, project, cwd, title, meta.started, meta.ended, count, hits, int(included), ','.join(matches) if matches else ('all' if included else '')))
    if included:
        connection.execute('INSERT INTO messages(session_id,ordinal,ts,role,sidechain,text,est_tokens) SELECT ?,ordinal,ts,role,sidechain,text,est_tokens FROM pending', (sid,))
        connection.execute('INSERT INTO messages_fts(rowid,text) SELECT id,text FROM messages WHERE session_id=?', (sid,))
    return sid


def index(args):
    patterns = {}
    try:
        patterns = {'project': [re.compile(p) for p in args.project_pattern], 'term': [re.compile(p) for p in args.term]}
    except re.error as exc: raise ValueError('Invalid selection regex: ' + str(exc)) from None
    if args.min_term_hits < 1: raise ValueError('--min-term-hits must be positive')
    paths = [] if args.stats else [p for p in discover_sessions(args.root, include_archived=args.include_archived) if args.include_archived or p.suffix != '.zst']
    inputs = paths + ([args.threads_csv] if args.threads_csv else [])
    db = _destination(args.db, inputs)
    for suffix in ('-journal', '-wal', '-shm'):
        _destination(str(db) + suffix, inputs)
    if args.stats and not db.is_file(): raise ValueError('Index does not exist')
    db.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    connection = sqlite3.connect(db)
    try:
        connection.execute('PRAGMA temp_store=MEMORY')
        connection.execute('PRAGMA secure_delete=ON')
        exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
        if args.stats:
            if not exists: raise ValueError('Not an agentlog index')
            data = dict(connection.execute('SELECT key,value FROM meta'))
            data.update(sessions=connection.execute('SELECT count(*) FROM sessions WHERE included=1').fetchone()[0], messages=connection.execute('SELECT count(*) FROM messages').fetchone()[0], files=connection.execute('SELECT count(*) FROM files').fetchone()[0])
            summary(data, json_mode=args.json); return 0
        options = json.dumps([args.root, args.source, args.cwd_prefix, args.project_pattern, args.term, args.min_term_hits, args.since, args.until, args.keep_tool_stubs, args.include_archived, str(args.threads_csv or '')], sort_keys=True)
        config_hash = hashlib.sha256(options.encode()).hexdigest()
        if exists and not args.rebuild:
            meta = dict(connection.execute('SELECT key,value FROM meta'))
            if meta.get('redaction_version') != REDACTION_VERSION: raise ValueError('Index redaction version is stale; use --rebuild')
            if meta.get('config_hash') != config_hash: raise ValueError('Index selection changed; use --rebuild')
        if args.rebuild and exists:
            connection.executescript('DROP TABLE messages_fts; DROP TABLE messages; DROP TABLE sessions; DROP TABLE files; DROP TABLE meta;')
            exists = False
        if not exists:
            connection.executescript(SCHEMA)
        connection.execute('CREATE TEMP TABLE pending(ordinal INT, ts TEXT, role TEXT, sidechain INT, text TEXT, est_tokens INT)')
        connection.executemany('INSERT OR REPLACE INTO meta VALUES(?,?)', [('redaction_version', REDACTION_VERSION), ('agentlog_version', __version__), ('config_hash', config_hash), ('threads_csv', redact(str(Path(args.threads_csv).resolve()))[0] if args.threads_csv else '')])
        touched = 0; total_bytes = 0
        keys = {hashlib.sha256(str(path.resolve()).encode()).hexdigest() for path in paths}
        with connection:
            for key, sid in connection.execute('SELECT path,session_id FROM files').fetchall():
                if key not in keys:
                    if sid: _remove(connection, sid)
                    connection.execute('DELETE FROM files WHERE path=?', (key,))
        for path in paths:
            key = hashlib.sha256(str(path.resolve()).encode()).hexdigest()
            fingerprint, size, mtime = _fingerprint(path, args.threads_csv)
            old = connection.execute('SELECT fingerprint,session_id FROM files WHERE path=?', (key,)).fetchone()
            if old and old[0] == fingerprint and not path.is_dir(): continue
            try:
                with connection:
                    if old and old[1]: _remove(connection, old[1])
                    sid = _index_one(connection, path, args, patterns)
                    connection.execute('INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?)', (key, size, mtime, fingerprint, sid, redact(str(path.resolve()))[0]))
            except ZstdUnavailable:
                continue
            touched += 1; total_bytes += size
        elapsed = time.monotonic() - started
        with connection:
            connection.executemany('INSERT OR REPLACE INTO meta VALUES(?,?)', [('last_elapsed_seconds', f'{elapsed:.3f}'), ('last_mb_per_second', f'{total_bytes / 1000000 / elapsed:.3f}' if elapsed else '0')])
        summary({'files_indexed': touched, 'sessions': connection.execute('SELECT count(*) FROM sessions WHERE included=1').fetchone()[0], 'messages': connection.execute('SELECT count(*) FROM messages').fetchone()[0]}, json_mode=args.json)
        return 0
    finally: connection.close()


def run_indexed_sessions(args):
    if not 1 <= args.limit <= 500: raise ValueError('--limit must be between 1 and 500')
    if any(getattr(args, name, None) for name in ('root', 'cwd', 'thread_source', 'automation', 'min_bytes', 'model', 'cli_version', 'threads_csv')):
        raise ValueError('The requested filter is unavailable with --indexed')
    db = Path(args.db).resolve()
    if not db.is_file(): raise ValueError('Index does not exist')
    connection = sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)
    try:
        conditions = ['included=1']; params = []
        if args.since: conditions.append('first_ts >= ?'); params.append(args.since)
        if args.until: conditions.append('substr(first_ts,1,?) <= ?'); params.extend([len(args.until), args.until])
        if args.source: conditions.append('source=?'); params.append(args.source)
        rows = connection.execute('SELECT session_id,source,first_ts,last_ts,title,n_msgs,term_hits,match_reason FROM sessions WHERE ' + ' AND '.join(conditions) + ' ORDER BY first_ts,session_id LIMIT ?', params + [args.limit]).fetchall()
        inputs = _indexed_inputs(connection, db)
    finally: connection.close()
    lines = ['# Indexed sessions', '', '| Session | Source | First | Last | Title | Messages | Term hits | Match |', '|---|---|---|---|---|---:|---:|---|']
    for row in rows:
        line = '| ' + ' | '.join(str(v or '').replace('|', '\\|').replace('\n', ' ') for v in (row[0][:8], *row[1:])) + ' |'
        if est_tokens('\n'.join(lines + [line]) + '\n') > 6000: break
        lines.append(line)
    text = redact('\n'.join(lines) + '\n')[0]
    directory = Path(args.out) if args.out else Path('agentlog-out/sessions')
    target = _destination(directory / 'sessions.md', inputs)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding='utf-8', newline='\n')
    summary({'sessions': len(lines)-4, 'out': str(target)}, json_mode=args.json)
    return 0


def _snippet(text, query, limit):
    if len(text) <= limit: return text
    words = re.findall(r'[\w]+', query)
    match = next((m for word in words if word.upper() not in {'AND', 'OR', 'NOT', 'NEAR'} and (m := re.search(re.escape(word), text, re.I))), None)
    center = match.start() if match else 0
    start = min(max(0, center - limit // 2), len(text) - limit)
    omitted = len(text) - limit
    return ('…' if start else '') + text[start:start + limit] + f'…[+{omitted} chars]'


def _indexed_inputs(connection, db):
    paths = [db] + [str(db) + suffix for suffix in ('-journal', '-wal', '-shm')]
    paths += [row[0] for row in connection.execute('SELECT source_path FROM files')]
    csv = connection.execute("SELECT value FROM meta WHERE key='threads_csv'").fetchone()
    if csv and csv[0]: paths.append(csv[0])
    return paths


def search(args):
    if not 1 <= args.budget <= 8000: raise ValueError('--max-tokens must be between 1 and 8000')
    if not 1 <= args.top <= 20: raise ValueError('--top must be between 1 and 20')
    if not 0 <= args.window <= 4: raise ValueError('--window must be between 0 and 4')
    if not 1 <= args.max_chars_per_msg <= 3000: raise ValueError('--max-chars-per-msg must be between 1 and 3000')
    db = Path(args.db).resolve()
    if not db.is_file(): raise ValueError('Index does not exist')
    connection = sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    try:
        if args.out: out = _destination(args.out, _indexed_inputs(connection, db))
        conditions = ['messages_fts MATCH ?', 's.included=1']; params = [args.q]
        if args.session:
            conditions.append('s.session_id LIKE ?'); params.append(args.session + '%')
        if args.since: conditions.append('s.first_ts >= ?'); params.append(args.since)
        if args.until: conditions.append('substr(s.first_ts,1,?) <= ?'); params.extend([len(args.until), args.until])
        if args.role != 'any': conditions.append('m.role=?'); params.append(args.role)
        sql = '''SELECT m.id,m.session_id,m.ordinal,bm25(messages_fts) AS rank,s.source,s.first_ts,s.title
                 FROM messages_fts JOIN messages m ON m.id=messages_fts.rowid JOIN sessions s ON s.session_id=m.session_id
                 WHERE ''' + ' AND '.join(conditions) + ' ORDER BY rank,m.session_id,m.ordinal'
        try:
            hit_count = connection.execute('SELECT count(*) FROM (' + sql + ')', params).fetchone()[0]
            hits = connection.execute(sql + ' LIMIT ?', params + [args.top]).fetchall()
        except sqlite3.OperationalError as exc: raise ValueError('Invalid FTS query: ' + str(exc) + '; quote phrases or use OR') from None
        windows = []
        for hit in hits:
            previous = connection.execute('SELECT * FROM messages WHERE session_id=? AND ordinal < ? ORDER BY ordinal DESC LIMIT ?', (hit['session_id'], hit['ordinal'], args.window)).fetchall()
            following = connection.execute('SELECT * FROM messages WHERE session_id=? AND ordinal >= ? ORDER BY ordinal LIMIT ?', (hit['session_id'], hit['ordinal'], args.window + 1)).fetchall()
            selected = list(reversed(previous)) + following
            overlapping = [window for window in windows if window['hit']['session_id'] == hit['session_id'] and set(window['ids']) & {row['id'] for row in selected}]
            if overlapping:
                target = overlapping[0]
                combined = {r['id']: r for r in target['rows']}
                for item in overlapping[1:]:
                    combined.update({r['id']: r for r in item['rows']}); windows.remove(item)
                    target['matches'].update(item['matches'])
                combined.update({r['id']: r for r in selected}); target['rows'] = sorted(combined.values(), key=lambda r: r['ordinal'])
                target['ids'] = set(combined); target['matches'].add(hit['id'])
            else: windows.append({'hit': hit, 'rows': selected, 'ids': {r['id'] for r in selected}, 'matches': {hit['id']}})
        shown = []; safe_query = redact(args.q)[0].replace('\n', ' ')
        def render(parts, truncated):
            header = f'<!-- agentlog search | q: {json.dumps(safe_query, ensure_ascii=False)} | hits: {hit_count} | shown: {sum(len(w[1]) for w in parts)} | est_tokens: '
            body = ''.join(part[0] for part in parts)
            if truncated: body += f'\nTRUNCATED: {sum(len(w[1]) for w in parts)} of {len(hits)} hits shown; narrow the query or use --session/--since\n'
            text = header + '0 -->\n' + body
            for _ in range(4): text = header + str(est_tokens(text)) + ' -->\n' + body
            return json.dumps({'view': text}, ensure_ascii=False, sort_keys=True) + '\n' if args.json else text
        for window in windows:
            h = window['hit']; sid = h['session_id']; first = h['ordinal']
            body = f"\n## Hit {len(shown)+1} — session {sid[:8]} ({h['source']}, {h['first_ts'] or '?'}, {h['title'] or ''}) rank {h['rank']:.2f} | {sid[:8]}:E{first} | T{sid[:8]}#{first}\n"
            for row in window['rows']:
                role = 'u' if row['role'] == 'user' else 'a'
                body += f"- [{role} #{row['ordinal']} {row['ts'] or ''}] {_snippet(row['text'], args.q, args.max_chars_per_msg)}" + ('  ← match' if row['id'] in window['matches'] else '') + '\n'
            candidate = shown + [(body, window['matches'])]
            if est_tokens(render(candidate, len(candidate) < len(windows))) > args.budget: break
            shown = candidate
        text = redact(render(shown, len(shown) < len(windows)))[0]
        if est_tokens(text) > args.budget: raise ValueError('--max-tokens is too small for the search header')
        if args.out:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text, encoding='utf-8', newline='\n')
            if args.json: summary({'out': str(out)}, json_mode=True)
            else: print(text.splitlines()[0])
        else: print(text, end='')
        return 0
    finally: connection.close()


def register(subparsers):
    p = subparsers.add_parser('index', help='Build a redacted full-text index')
    p.add_argument('--db', default='agentlog-out/index.sqlite'); p.add_argument('--root', action='append')
    p.add_argument('--source', choices=('codex', 'claude', 'generic', 'requestdump'))
    p.add_argument('--cwd-prefix', action='append', default=[]); p.add_argument('--project-pattern', action='append', default=[])
    p.add_argument('--term', action='append', default=[]); p.add_argument('--min-term-hits', type=int, default=1)
    p.add_argument('--since'); p.add_argument('--until'); p.add_argument('--include-archived', action='store_true')
    p.add_argument('--keep-tool-stubs', action='store_true'); p.add_argument('--threads-csv'); p.add_argument('--rebuild', action='store_true')
    p.add_argument('--stats', action='store_true'); p.add_argument('--json', action='store_true'); p.set_defaults(func=index)
    p = subparsers.add_parser('search', help='Search the local full-text index')
    p.add_argument('--db', default='agentlog-out/index.sqlite'); p.add_argument('--q', required=True)
    p.add_argument('--session'); p.add_argument('--since'); p.add_argument('--until'); p.add_argument('--role', choices=('user','assistant','any'), default='any')
    p.add_argument('--top', type=int, default=8); p.add_argument('--window', type=int, default=2)
    p.add_argument('--max-chars-per-msg', type=int, default=1200); p.add_argument('--max-tokens', dest='budget', type=int, default=6000)
    p.add_argument('--out'); p.add_argument('--json', action='store_true'); p.set_defaults(func=search)
