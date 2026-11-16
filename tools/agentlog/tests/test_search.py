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

import contextlib
import io
import json
import os
from pathlib import Path
import random
import sqlite3
import shutil
import subprocess
import tempfile
import tracemalloc
import unittest
from unittest.mock import patch

from agentlog.cli import main
from agentlog import search
from agentlog.core.tokens import est_tokens
from agentlog.readers.discover import read_session
from agentlog.readers import codex


class SearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.source = cls.root / 'input'
        cls.source.mkdir()
        cls.db = cls.root / 'index.sqlite'

    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()

    def setUp(self):
        self.path = self.source / 'synthetic.jsonl'
        self.path.write_text(''.join(json.dumps(row) + '\n' for row in [
            {'session_id':'12345678-abcd', 'role':'user', 'cwd':'C:/work/project', 'text':'maple token=' + 'abcdefghijkl' * 2},
            {'role':'assistant', 'text':'answer ' + 'z' * 200 + ' maple ' + 'x' * 200},
            {'role':'tool', 'text':'hidden output'},
        ]), encoding='utf-8')

    def call(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(map(str, args)))
        return code, out.getvalue(), err.getvalue()

    def test_incremental_redaction_and_search(self):
        command = ['index', '--root', self.source, '--db', self.db]
        code, out, _ = self.call(*command, '--rebuild', '--json')
        self.assertEqual(code, 0); self.assertEqual(json.loads(out)['files_indexed'], 1)
        self.assertNotIn(b'abcdefghijkl' * 2, self.db.read_bytes())
        self.assertEqual(json.loads(self.call(*command, '--json')[1])['files_indexed'], 0)
        code, out, _ = self.call('search', '--db', self.db, '--q', 'maple', '--max-chars-per-msg', '40', '--window', '1')
        self.assertEqual(code, 0); self.assertIn('12345678:E', out); self.assertIn('T12345678#', out)
        self.assertIn('maple', out); self.assertNotIn('hidden output', out)
        self.assertLessEqual(est_tokens(out), 6000)
        self.assertEqual(out, self.call('search', '--db', self.db, '--q', 'maple', '--max-chars-per-msg', '40', '--window', '1')[1])
        with self.path.open('a', encoding='utf-8') as stream: stream.write(json.dumps({'role':'user','text':'fresh acorn'}) + '\n')
        self.assertEqual(json.loads(self.call(*command, '--json')[1])['files_indexed'], 1)
        self.assertIn('acorn', self.call('search', '--db', self.db, '--q', 'acorn')[1])

    def test_selection_caps_and_indexed_listing(self):
        command = ['index', '--root', self.source, '--db', self.db]
        self.assertEqual(self.call(*command, '--rebuild', '--cwd-prefix', 'c:\\WORK', '--term', 'notfound')[0], 0)
        with contextlib.closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute('SELECT included,match_reason FROM sessions').fetchone(), (1, 'cwd'))
        output = self.root / 'listing'
        code, text, _ = self.call('sessions', '--indexed', '--db', self.db, '--out', output)
        self.assertEqual(code, 0); self.assertIn('sessions: 1', text)
        self.assertIn('12345678', (output / 'sessions.md').read_text(encoding='utf-8'))
        for args in (('--max-tokens', '9000'), ('--top', '50'), ('--window', '5')):
            self.assertEqual(self.call('search', '--db', self.db, '--q', 'maple', *args)[0], 2)
        self.assertEqual(self.call('search', '--db', self.db, '--q', '"bad')[0], 2)
        self.assertEqual(self.call(*command, '--rebuild', '--term', 'absent', '--min-term-hits', '2')[0], 0)
        with contextlib.closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute('SELECT included FROM sessions').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM messages').fetchone()[0], 0)

    def test_later_structured_cwd_and_unredacted_matching(self):
        path = self.source / 'cwd.jsonl'
        secret = 'person' + '@example.test'
        rows = [{'type':'session_meta', 'payload':{'id':'abcdef12-1234', 'cwd':'/first'}},
                {'type':'turn_context', 'payload':{'cwd':'C:/work/' + secret}},
                {'type':'response_item', 'payload':{'type':'message', 'role':'user', 'content':'searchable message'}}]
        path.write_text(''.join(json.dumps(row)+'\n' for row in rows), encoding='utf-8')
        try:
            before = next(read_session(path))
            self.assertEqual(before.extra, {})
            matched = next(read_session(path, cwd_prefixes=['c:\\WORK\\' + secret]))
            self.assertTrue(matched.extra['cwd_prefix_match'])
            self.assertFalse(next(read_session(path, cwd_prefixes=['c:/work/other' + '@example.test'])).extra['cwd_prefix_match'])
            code, _, _ = self.call('index', '--root', path, '--db', self.db, '--rebuild', '--cwd-prefix', 'c:/work/' + secret)
            self.assertEqual(code, 0)
            self.assertNotIn(secret.encode(), self.db.read_bytes())
            with contextlib.closing(sqlite3.connect(self.db)) as db:
                self.assertEqual(db.execute('SELECT included,match_reason FROM sessions').fetchone(), (1, 'cwd'))
        finally: path.unlink()

    def test_archive_indexing(self):
        archive = self.source / 'synthetic.jsonl.zst'
        try:
            from compression import zstd as native_zstd  # Python 3.14+
        except ImportError:
            native_zstd = None
        if native_zstd is not None:
            archive.write_bytes(native_zstd.compress(self.path.read_bytes()))
        elif shutil.which('zstd'):
            subprocess.run(['zstd', '-q', '-f', str(self.path), '-o', str(archive)], check=True)
        else:
            self.skipTest('neither compression.zstd nor the zstd executable is available')
        try:
            code, out, _ = self.call('index', '--root', archive, '--db', self.db, '--rebuild', '--include-archived', '--json')
            self.assertEqual(code, 0); self.assertEqual(json.loads(out)['messages'], 2)
            self.assertIn('maple', self.call('search', '--db', self.db, '--q', 'maple')[1])
        finally: archive.unlink()

    def test_dump_refresh_csv_and_collision_protection(self):
        folder = self.root / 'dump'
        folder.mkdir(exist_ok=True)
        request = folder / 'request-001.txt'
        sep = '-' * 60
        def snapshot(text):
            return ('Session ID: 12345678-abcd\nTimestamp: 2026-01-01T00:00:00Z\n' + sep +
                    '\nRECONSTRUCTED MODEL INPUT\n' + sep + '\n\n' + sep +
                    '\nITEM 0001\ntype: message\nrole: user\n\n' + text + '\n')
        request.write_text(snapshot('first cedar'), encoding='utf-8')
        csv = self.root / 'threads.csv'
        csv.write_text('session_id,thread_id,parent_thread_id\n', encoding='utf-8')
        try:
            command = ['index', '--root', folder, '--db', self.db, '--threads-csv', csv]
            with patch.object(search, 'read_session', wraps=search.read_session) as read:
                code, out, _ = self.call(*command, '--rebuild', '--json')
                self.assertEqual(read.call_args.kwargs['threads_csv'], str(csv))
            self.assertEqual(code, 0); self.assertEqual(json.loads(out)['messages'], 1)
            request.write_text(snapshot('newer willow'), encoding='utf-8')
            self.assertEqual(json.loads(self.call(*command, '--json')[1])['files_indexed'], 1)
            self.assertIn('willow', self.call('search', '--db', self.db, '--q', 'willow')[1])
            self.assertEqual(self.call('index', '--root', folder, '--db', folder / 'db.sqlite')[0], 2)
            self.assertEqual(self.call('index', '--root', folder, '--db', csv, '--threads-csv', csv)[0], 2)
            self.assertEqual(self.call('search', '--db', self.db, '--q', 'willow', '--out', csv)[0], 2)
            self.assertEqual(self.call('index', '--root', folder, '--root', self.path, '--db', self.db, '--rebuild')[0], 2)
        finally:
            request.unlink(); csv.unlink()

    def test_json_and_small_budget(self):
        self.assertEqual(self.call('index', '--root', self.path, '--db', self.db, '--rebuild')[0], 0)
        code, out, _ = self.call('search', '--db', self.db, '--q', 'maple', '--json', '--max-tokens', '100')
        self.assertEqual(code, 0)
        self.assertLessEqual(est_tokens(out), 100)
        self.assertIn('view', json.loads(out))

    def test_claude_selection_extraction_and_term_threshold(self):
        project = self.root / 'matching-project'
        project.mkdir(exist_ok=True)
        path = project / 'claude.jsonl'
        rows = [
            {'type':'user', 'sessionId':'c0ffee12-1111', 'cwd':'/elsewhere', 'message':{'content':'alpha <system-reminder>hidden context</system-reminder> maple'}},
            {'type':'assistant', 'sessionId':'c0ffee12-1111', 'isSidechain':True, 'message':{'content':[
                {'type':'thinking','thinking':'hidden reasoning'}, {'type':'tool_use','id':'call','name':'Shell','input':{'cmd':'hidden command'}},
                {'type':'text','text':'maple answer'}]}},
            {'type':'user','sessionId':'c0ffee12-1111','isMeta':True,'message':{'content':'hidden meta'}},
            {'type':'user','sessionId':'c0ffee12-1111','message':{'content':[{'type':'tool_result','tool_use_id':'call','content':'hidden tool result'}]}},
            {'type':'queue-operation','sessionId':'c0ffee12-1111','cwd':'C:/chosen/later'},
        ]
        path.write_text(''.join(json.dumps(row)+'\n' for row in rows), encoding='utf-8')
        try:
            command = ['index', '--root', path, '--db', self.db]
            self.assertEqual(self.call(*command, '--rebuild', '--project-pattern', 'matching-project', '--keep-tool-stubs')[0], 0)
            with contextlib.closing(sqlite3.connect(self.db)) as db:
                self.assertEqual(db.execute('SELECT included, match_reason FROM sessions').fetchone(), (1, 'project'))
                messages = db.execute('SELECT text,sidechain FROM messages ORDER BY ordinal').fetchall()
            self.assertEqual(messages, [('alpha  maple',0),('[tool: Shell]',1),('maple answer',1)])
            for hidden in ('hidden context', 'hidden reasoning', 'hidden command', 'hidden meta', 'hidden tool result'):
                self.assertNotIn(hidden.encode(), self.db.read_bytes())
            self.assertEqual(self.call(*command, '--rebuild', '--term', 'maple', '--min-term-hits', '2')[0], 0)
            with contextlib.closing(sqlite3.connect(self.db)) as db:
                self.assertEqual(db.execute('SELECT included,term_hits,match_reason FROM sessions').fetchone(), (1,2,'term'))
            self.assertEqual(self.call(*command, '--rebuild', '--cwd-prefix', 'c:\\chosen')[0], 0)
            with contextlib.closing(sqlite3.connect(self.db)) as db:
                self.assertEqual(db.execute('SELECT included,match_reason FROM sessions').fetchone(), (1,'cwd'))
        finally: path.unlink()

    def test_caps_output_file_stale_version_and_directory_safety(self):
        self.assertEqual(self.call('index', '--root', self.path, '--db', self.db, '--rebuild')[0], 0)
        rng = random.Random(472)
        for _ in range(30):
            budget = rng.randint(50, 500)
            code, out, _ = self.call('search','--db',self.db,'--q','maple','--max-tokens',budget,
                                     '--max-chars-per-msg',rng.randint(5,100))
            self.assertEqual(code, 0)
            self.assertLessEqual(est_tokens(out), budget)
        output = self.root / 'search.md'
        code, out, _ = self.call('search','--db',self.db,'--q','maple','--out',output)
        self.assertEqual(code, 0); self.assertEqual(len(out.splitlines()), 1)
        self.assertTrue(output.read_text(encoding='utf-8').startswith(out.rstrip()))
        self.assertEqual(self.call('search','--db',self.db,'--q','maple','--out',self.path)[0], 2)
        self.assertEqual(self.call('search','--db',self.db,'--q','maple','--out',str(self.db)+'-wal')[0], 2)
        self.assertEqual(self.call('index','--root',self.path,'--db',self.path)[0], 2)
        with contextlib.closing(sqlite3.connect(self.db)) as db:
            db.execute("UPDATE meta SET value='old' WHERE key='redaction_version'"); db.commit()
        self.assertEqual(self.call('index','--root',self.path,'--db',self.db)[0], 2)
        self.assertEqual(self.call('index','--root',self.path,'--db',self.db,'--rebuild')[0], 0)
        with patch.dict(os.environ, {'AGENTLOG_HARD_CAP':'10'}):
            self.assertEqual(self.call('search','--db',self.db,'--q','maple','--max-tokens','100')[0], 0)

    def test_codex_messages_and_bookkeeping_are_separated(self):
        path = self.source / 'codex.jsonl'
        rows = [
            {'type':'session_meta','payload':{'id':'11112222-abcd','cwd':'/other'}},
            {'type':'response_item','payload':{'type':'message','role':'developer','content':'private developer context'}},
            {'type':'response_item','payload':{'type':'message','role':'user','content':'# AGENTS.md instructions\nprivate injected context'}},
            {'type':'response_item','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':'maple question'}]}},
            {'type':'event_msg','payload':{'type':'user_message','message':'maple question'}},
            {'type':'response_item','payload':{'type':'function_call','name':'shell','call_id':'1','arguments':'private arguments'}},
            {'type':'response_item','payload':{'type':'function_call_output','call_id':'1','output':'private output'}},
            {'type':'turn_context','payload':{'cwd':'/later/selected'}},
            {'type':'response_item','payload':{'type':'message','role':'assistant','content':[{'type':'output_text','text':'maple answer'}]}},
        ]
        path.write_text(''.join(json.dumps(row)+'\n' for row in rows),encoding='utf-8')
        try:
            self.assertEqual(self.call('index','--root',path,'--db',self.db,'--rebuild','--cwd-prefix','/LATER')[0],0)
            with contextlib.closing(sqlite3.connect(self.db)) as db:
                self.assertEqual(db.execute('SELECT included,match_reason FROM sessions').fetchone(),(1,'cwd'))
                self.assertEqual(db.execute('SELECT text FROM messages ORDER BY ordinal').fetchall(), [('maple question',),('maple answer',)])
            self.assertNotIn(b'private ',self.db.read_bytes())
        finally: path.unlink()

    def test_non_string_cwd_does_not_match_or_break_index(self):
        self.path.write_text(json.dumps({'session_id':'12345678-abcd', 'cwd':{'path':'/chosen'}, 'role':'user', 'text':'maple'})+'\n', encoding='utf-8')
        self.assertFalse(next(read_session(self.path, cwd_prefixes=['/chosen'])).extra['cwd_prefix_match'])
        self.assertEqual(self.call('index','--root',self.path,'--db',self.db,'--rebuild','--cwd-prefix','/chosen')[0], 0)
        with contextlib.closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute('SELECT cwd,included FROM sessions').fetchone(), (None, 0))

    def test_duplicate_id_with_equal_redacted_paths_is_rejected(self):
        roots = [self.root / (name + '@example.test') for name in ('first','second')]
        for root, text in zip(roots, ('first original maple', 'second conflicting cedar')):
            root.mkdir(exist_ok=True)
            (root / 'session.jsonl').write_text(json.dumps({'session_id':'12345678-abcd','role':'user','text':text})+'\n',encoding='utf-8')
        try:
            code, _, error = self.call('index','--root',roots[0],'--root',roots[1],'--db',self.db,'--rebuild')
            self.assertEqual(code, 2); self.assertIn('same session ID', error)
            with contextlib.closing(sqlite3.connect(self.db)) as db:
                self.assertEqual(db.execute('SELECT session_id,n_msgs FROM sessions').fetchall(), [('12345678-abcd', 1)])
                self.assertEqual(db.execute('SELECT text FROM messages').fetchall(), [('first original maple',)])
                self.assertEqual(db.execute('SELECT count(*) FROM files').fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'cedar'").fetchone()[0], 0)
            self.assertIn('first original maple', self.call('search','--db',self.db,'--q','maple')[1])
        finally:
            for root in roots: (root / 'session.jsonl').unlink()

    def test_seeded_varied_queries_respect_serialized_budget(self):
        rng = random.Random(82943)
        terms = ('maple', 'cedar', 'acorn', 'willow', 'absent')
        rows = [{'session_id':'12345678-abcd', 'role':'user' if i % 2 == 0 else 'assistant',
                 'text':f'{terms[i % 4]} {terms[(i + 1) % 4]} phrase ' + ('"quoted" \\ path\n' * (i % 8 + 1))}
                for i in range(36)]
        self.path.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
        self.assertEqual(self.call('index','--root',self.path,'--db',self.db,'--rebuild')[0], 0)
        categories = ('term', 'phrase', 'and', 'or', 'prefix')
        for i in range(125):
            category = categories[i % len(categories)]
            first, second = rng.sample(terms, 2)
            query = {'term':first, 'phrase':f'"{first} {second}"', 'and':f'{first} AND {second}',
                     'or':f'{first} OR {second}', 'prefix':first[:rng.randint(2, len(first))]+'*'}[category]
            budget = rng.randint(100, 700)
            json_mode = bool(rng.getrandbits(1))
            flags = ['--json'] if json_mode else []
            args = ['search','--db',self.db,'--q',query,'--max-tokens',budget,'--top',rng.randint(1,20),
                    '--window',rng.randint(0,4),'--max-chars-per-msg',rng.randint(10,250),*flags]
            with self.subTest(category=category, query=query, budget=budget, json=json_mode):
                code, output, error = self.call(*args)
                self.assertEqual(code, 0, error)
                self.assertLessEqual(est_tokens(output), budget)
                if json_mode: self.assertIn('view', json.loads(output))

    def test_codex_legacy_messages_deduplicate_file_wide_and_keep_positions(self):
        path = self.source / 'legacy.jsonl'
        secret = 'sk-' + 'synthetic' * 4
        def legacy(text): return {'type':'event_msg', 'payload':{'type':'user_message','message':text}}
        def normal(text): return {'type':'response_item','payload':{'type':'message','role':'user','content':text}}
        rows = [{'type':'session_meta','payload':{'id':'abcd1234-1111'}}, legacy('before mirror'),
                legacy('legacy distinct'), normal('normal repeated'), legacy('normal repeated'),
                legacy('legacy repeated'), legacy('legacy repeated'), normal('normal repeated'),
                normal('before mirror'), legacy('usage limit help ' + secret),
                legacy('# AGENTS.md instructions\nhidden'),
                {'type':'event_msg','payload':{'type':'user_message','message':{'unexpected':'object'}}},
                normal('later mirror'), legacy('later mirror')]
        path.write_text(''.join(json.dumps(row)+'\n' for row in rows), encoding='utf-8')
        try:
            scratch_paths = []
            original = codex.private_temp_directory
            @contextlib.contextmanager
            def inspect_scratch():
                with original() as directory:
                    scratch_paths.append(directory)
                    yield directory
            with patch.object(codex, 'private_temp_directory', inspect_scratch):
                stream = read_session(path)
                meta = next(stream)
                self.assertNotIn(secret.encode(), (scratch_paths[0]/'fingerprints.sqlite').read_bytes())
                events = list(stream)
            self.assertEqual(meta.session_id, 'abcd1234-1111')
            self.assertEqual([event.seq for event in events], list(range(len(rows))))
            self.assertEqual(sum(event.raw_bytes for event in events), path.stat().st_size)
            users = [event for event in events if event.kind == 'user_msg']
            self.assertEqual([event.seq for event in users], [2,3,5,6,7,8,9,12])
            self.assertEqual([event.turn for event in users], list(range(1,9)))
            self.assertEqual(events[1].kind, 'other'); self.assertEqual(events[4].kind, 'other')
            self.assertEqual(events[13].kind, 'other'); self.assertEqual(events[10].kind, 'context')
            self.assertEqual(events[11].kind, 'other'); self.assertNotIn(secret, repr(events))
            self.assertIn('[REDACTED:', events[9].text)
            self.assertEqual(self.call('index','--root',path,'--db',self.db,'--rebuild')[0],0)
            self.assertNotIn(secret.encode(), self.db.read_bytes())
            self.assertIn('legacy distinct', self.call('search','--db',self.db,'--q','"legacy distinct"')[1])
            with contextlib.closing(sqlite3.connect(self.db)) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM messages WHERE text='normal repeated'").fetchone()[0], 2)
                self.assertEqual(db.execute("SELECT count(*) FROM messages WHERE text='legacy repeated'").fetchone()[0], 2)
        finally: path.unlink()

    def test_codex_fingerprint_memory_is_bounded(self):
        path = self.source / 'many.jsonl'
        def peak(count):
            with path.open('w', encoding='utf-8') as stream:
                stream.write(json.dumps({'type':'session_meta','payload':{'id':'aaaa1111'}})+'\n')
                for number in range(count):
                    stream.write(json.dumps({'type':'response_item','payload':{'type':'message','role':'user','content':f'synthetic entry {number}'}})+'\n')
            tracemalloc.start()
            try:
                self.assertEqual(sum(1 for _ in read_session(path)), count + 2)
                return tracemalloc.get_traced_memory()[1]
            finally: tracemalloc.stop()
        try:
            small = peak(500)
            large = peak(10000)
            self.assertLess(large, small + 1200000)
        finally: path.unlink()

    def test_claude_mixed_wrappers_preserve_events_and_index_conversation(self):
        path = self.source / 'claude-wrappers.jsonl'
        secret = 'sk-' + 'synthetic' * 4
        sid = 'cafe1234-1111'
        def record(typ, content, **extra):
            return {'type':typ,'sessionId':sid,'message':{'content':content},**extra}
        rows = [
            record('user', '<command-name>/help</command-name> leading message'),
            record('user', 'before <system-reminder>private '+secret+'</system-reminder> middle <local-command-stdout>hidden output</local-command-stdout> after'),
            record('user', '<command-message>only wrapper</command-message>'),
            record('assistant', '<local-command>hidden</local-command> assistant reply'),
            record('user', [{'type':'text','text':'<system-reminder>hidden</system-reminder>'},
                            {'type':'text','text':'multiblock conversation'},
                            {'type':'tool_result','tool_use_id':'none','content':'hidden result'}]),
            record('user', 'metadata text', isMeta=True),
            record('user', '<command-name>unclosed command with words'),
            record('user', '<custom>tag remains</custom> unrelated content'),
            record('assistant', 'error content', isApiErrorMessage=True),
            record('user', '<command-name>/x</command-name> compact summary', isCompactSummary=True),
        ]
        rows[3]['message']['usage'] = {'input_tokens':3,'output_tokens':2}
        path.write_text(''.join(json.dumps(row)+'\n' for row in rows),encoding='utf-8')
        try:
            stream = read_session(path); next(stream); events = list(stream)
            self.assertEqual([e.seq for e in events], list(range(len(events))))
            self.assertEqual(sum(e.raw_bytes for e in events), path.stat().st_size)
            self.assertEqual([e.turn for e in events if e.kind=='user_msg'], [1,2,3,4])
            self.assertEqual([e.kind for e in events], ['user_msg','user_msg','context','assistant_msg','token_usage',
                                                       'context','user_msg','tool_result','context','context','user_msg','error','compaction'])
            self.assertIn('<command-name>/help</command-name>', events[0].text)
            self.assertNotIn(secret, repr(events))
            self.assertEqual(self.call('index','--root',path,'--db',self.db,'--rebuild')[0],0)
            with contextlib.closing(sqlite3.connect(self.db)) as db:
                messages = [row[0] for row in db.execute('SELECT text FROM messages ORDER BY ordinal')]
            self.assertEqual(messages, ['leading message', 'before  middle  after', 'assistant reply',
                                        'multiblock conversation','<custom>tag remains</custom> unrelated content'])
            self.assertNotIn(secret.encode(), self.db.read_bytes())
        finally: path.unlink()
