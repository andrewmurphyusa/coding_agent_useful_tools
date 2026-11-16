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

import tempfile
import unittest
import contextlib
import io
import json
from unittest.mock import patch
from pathlib import Path

from agentlog.readers import requestdump


SEP = '-' * 60


def snapshot(number, items, kind='tool-followup'):
    header = (f'Codex request #{number}\nSession ID: synthetic-session\n'
              f'Timestamp: 2026-01-01T00:00:{number:02d}Z\nRequest kind: {kind}\n\n'
              'TOKEN USAGE\nInput tokens: 1,234\nCached input tokens: 1,000\n'
              'Output tokens: 34\nReasoning output tokens: 10\nTotal tokens: 1,268\n\n'
              f'{SEP}\nRECONSTRUCTED MODEL INPUT\n{SEP}\n\n')
    for index, (typ, role, name, call_id, body) in enumerate(items, 1):
        header += (f'{SEP}\nITEM {index:04d}\ntype: {typ}\nrole: {role}\n'
                   f'name: {name}\ncall_id: {call_id}\n\n{body}\n')
    return header


class RequestDumpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.folder = cls.root / 'snapshots'; cls.folder.mkdir()
        base = [('message', 'developer', '', '', 'instructions'),
                ('message', 'user', '', '', 'First question')]
        for number in range(1, 13):
            if number == 3:
                pass
            elif number == 4:
                base = [('message', 'user', '', '', 'summary of prior work'),
                        ('function_call', '-', 'shell', 'call-1', 'run command')]
            elif number == 5:
                base = base + [('function_call_output', '-', '', 'call-1', f'line\n{SEP}\nnot an item\nExit code: 2')]
            elif number > 5:
                base = base + [('message', 'assistant', '', '', f'answer {number}')]
            elif number == 2:
                base = base + [('message', 'assistant', '', '', 'response')]
            (cls.folder / f'request-{number:03d}.txt').write_text(snapshot(number, base, 'user-turn' if number in (1, 4) else 'tool-followup'), encoding='utf-8')
        (cls.folder / 'request-001-new.txt').write_text('ignored', encoding='utf-8')

    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()

    def test_reconstruction(self):
        meta, *events = requestdump.read(self.folder)
        self.assertEqual(meta.source, 'requestdump')
        self.assertTrue(meta.partial)
        self.assertEqual(meta.title, 'First question')
        self.assertEqual([e.kind for e in events],
                         ['context', 'user_msg', 'token_usage', 'assistant_msg',
                          'token_usage', 'context', 'token_usage', 'compaction',
                          'user_msg', 'tool_call', 'token_usage', 'tool_result',
                          'token_usage'] + ['assistant_msg', 'token_usage'] * 7)
        self.assertEqual(sum(e.kind == 'token_usage' for e in events), 12)
        self.assertEqual(sum(e.kind == 'compaction' for e in events), 1)
        self.assertEqual(sum(e.text == 'identical re-send' for e in events), 1)
        compact = next(e for e in events if e.kind == 'compaction')
        self.assertIn('3 \u2192 2 items; 1234 \u2192 1234 tokens\nsummary of prior work', compact.text)
        result = next(e for e in events if e.kind == 'tool_result')
        self.assertEqual((result.tool, result.exit_code, result.is_error), ('shell', 2, True))
        self.assertIn(SEP + '\nnot an item', result.text)
        usage = [e.usage for e in events if e.kind == 'token_usage'][-1]
        self.assertEqual((usage.input, usage.cached_input, usage.output, usage.reasoning, usage.total, usage.cumulative_total), (1234, 1000, 34, 10, 1268, 15216))
        self.assertEqual([e.seq for e in events], list(range(len(events))))
        self.assertEqual(requestdump.inspect(self.folder)[1:], (12, 0))

    def test_secrets_and_threads(self):
        folder = self.folder / 'privacy'; folder.mkdir(exist_ok=True)
        secret = 'pass' + 'word' + '=' + 'synthetic-long-value'
        (folder / 'request-001.txt').write_text(snapshot(1, [('message', 'user', '', '', secret)]), encoding='utf-8')
        csv_path = folder / 'threads.csv'
        csv_path.write_text('session_id,thread_id,parent_thread_id,source,requests,total_tokens,start,end\nsynthetic-session,child,main,guardian,2,123,begin,end\n', encoding='utf-8')
        meta, *events = requestdump.read(folder, threads_csv=csv_path)
        self.assertNotIn(secret, meta.title)
        self.assertNotIn(secret, events[0].text)
        self.assertEqual(meta.extra['threads'][0]['thread_id'], 'child')

    def test_cli_integration(self):
        from agentlog.cli import main
        from agentlog.readers.discover import detect_source, resolve_session, discover_sessions
        self.assertEqual(detect_source(self.folder), 'requestdump')
        self.assertEqual(resolve_session(str(self.folder)), self.folder)
        self.assertIn(self.folder, list(discover_sessions([self.folder])))
        destination = self.root / 'cli'; destination.mkdir(exist_ok=True)
        csv_path = destination / 'threads-test.csv'
        csv_path.write_text('session_id,thread_id,parent_thread_id,source,requests,total_tokens\nsynthetic-session,child,main,guardian,2,123\n', encoding='utf-8')
        out = destination / 'dump-reports'
        for command in ('profile', 'compactions', 'compress', 'skeleton'):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main([command, str(self.folder), '--threads-csv', str(csv_path), '--out', str(out)]), 0)
        for command in (['census', str(self.folder)], ['show', 'E0', '--session', str(self.folder)],
                        ['diff', str(self.folder), str(self.folder)]):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(command + ['--threads-csv', str(csv_path), '--out', str(out)]), 0)
        data = json.loads((out/'profile.json').read_text(encoding='utf-8'))
        self.assertEqual(data['threads'][0]['thread_id'], 'child')
        self.assertIn('Sub-threads (from CSV; not in dumps)', (out/'profile.md').read_text(encoding='utf-8'))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['doctor', str(self.folder), '--out', str(out)]), 0)
        data = json.loads((out/'doctor.json').read_text(encoding='utf-8'))
        self.assertEqual(data['requestdump']['requests'], 12)
        collision = out / 'profile.csv'
        original = 'session_id,thread_id,parent_thread_id\nsynthetic-session,child,main\n'
        collision.write_text(original, encoding='utf-8')
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(['profile', str(self.folder), '--threads-csv', str(collision), '--out', str(out)]), 2)
        self.assertEqual(collision.read_text(encoding='utf-8'), original)
        generic = Path(__file__).parent / 'fixtures' / 'generic.jsonl'
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['profile', str(generic), '--out', str(out)]), 0)
            before = (out/'profile.md').read_bytes()
            self.assertEqual(main(['profile', str(generic), '--threads-csv', str(out/'missing.csv'), '--out', str(out)]), 0)
            self.assertEqual((out/'profile.md').read_bytes(), before)

    def test_unresolved_parent_rows_reach_loops_and_profile(self):
        from agentlog.cli import main
        folder = self.root / 'unresolved-dump'; folder.mkdir(exist_ok=True)
        (folder / 'request-001.txt').write_text(
            snapshot(1, [('message', 'user', '', '', 'question')]), encoding='utf-8')
        out = self.root / 'unresolved-reports'
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['profile', str(folder), '--out', str(out)]), 0)
        baseline = json.loads((out / 'profile.json').read_text(encoding='utf-8'))['totals']
        cases = (
            ('session_id,thread_id,parent_thread_id,total_tokens\n'
             'synthetic-session,child,,999999\n'
             'synthetic-session,synthetic-session,,999999\n'
             'foreign-session,foreign,,999999\n', ''),
            ('session_id,thread_id,total_tokens\n'
             'synthetic-session,child,999999\n'
             'synthetic-session,synthetic-session,999999\n'
             'foreign-session,foreign,999999\n', None),
        )
        for index, (contents, parent) in enumerate(cases):
            with self.subTest(parent=parent):
                csv_path = self.root / f'unresolved-{index}.csv'
                csv_path.write_text(contents, encoding='utf-8')
                case_out = self.root / f'unresolved-reports-{index}'
                common = [str(folder), '--threads-csv', str(csv_path), '--out', str(case_out)]
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main(['loops'] + common + ['--rules', 'R10', '--fail-on', 'medium']), 0)
                    self.assertEqual(main(['profile'] + common), 0)
                loops = json.loads((case_out / 'loops.json').read_text(encoding='utf-8'))
                self.assertEqual(loops['unresolved_thread_branches'], 1)
                self.assertEqual(loops['findings'], [])
                self.assertEqual((loops['finding_tokens'], loops['unknown_cost_findings']), (0, 0))
                profile = json.loads((case_out / 'profile.json').read_text(encoding='utf-8'))
                self.assertEqual(profile['totals'], baseline)
                self.assertEqual([(r['thread_id'], r['parent']) for r in profile['threads']], [('child', parent)])

    def test_dump_sessions_and_trend_do_not_reuse_directory_cache(self):
        import os
        from agentlog.cli import main
        folder = self.folder / 'cache-test'; folder.mkdir(exist_ok=True)
        target = folder / 'request-001.txt'
        item = ('message', 'user', '', '', 'first')
        target.write_text(snapshot(1, [item]), encoding='utf-8')
        original_cwd = Path.cwd()
        scratch = self.root / 'cache'; scratch.mkdir(exist_ok=True)
        try:
            os.chdir(scratch)
            out = scratch / 'out'
            for command in ('sessions', 'trend'):
                args = ([command, '--root', str(folder), '--out', str(out)] if command == 'sessions'
                        else [command, str(folder), '--out', str(out)])
                with contextlib.redirect_stdout(io.StringIO()): self.assertEqual(main(args), 0)
            (folder / 'request-002.txt').write_text(snapshot(2, [item, ('message', 'assistant', '', '', 'next')]), encoding='utf-8')
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(['sessions', '--root', str(folder), '--out', str(out)]), 0)
                self.assertEqual(main(['trend', str(folder), '--out', str(out)]), 0)
            sessions = json.loads((out / 'sessions.json').read_text(encoding='utf-8'))
            trend = json.loads((out / 'trend.json').read_text(encoding='utf-8'))
            self.assertEqual(sessions[0]['events'], 4)
            self.assertEqual(trend[0]['model_calls'], 2)
        finally:
            os.chdir(original_cwd)

    def test_two_request_lists_with_large_snapshots(self):
        folder = self.folder / 'large'; folder.mkdir(exist_ok=True)
        content = snapshot(1, [('message', 'user', '', '', 'x' * (2 * 1024 * 1024))])
        first = folder / 'request-001.txt'
        first.write_text(content, encoding='utf-8')
        import os
        for number in range(2, 201): os.link(first, folder / f'request-{number:03d}.txt')
        class TrackedList(list):
            live = peak = 0
            def __init__(self, values):
                super().__init__(values)
                type(self).live += 1
                type(self).peak = max(type(self).peak, type(self).live)
            def __del__(self): type(self).live -= 1
        parse = requestdump._parse
        def tracked(path):
            header, items = parse(path)
            return header, TrackedList(items)
        with patch.object(requestdump, '_parse', side_effect=tracked):
            count = sum(1 for _ in requestdump.read(folder))
        self.assertEqual(count, 401)
        self.assertLessEqual(TrackedList.peak, 2)


if __name__ == '__main__': unittest.main()
