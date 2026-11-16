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

"""Synthetic diff and trend tests."""
import unittest
import csv
import json
import re
import tempfile
from pathlib import Path
from argparse import Namespace
from contextlib import chdir, redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import patch
from agentlog import automations, diff, trend
from agentlog.cli import main
from agentlog.core.tokens import est_tokens
from agentlog.readers.base import Event, SessionMeta, Usage

class DiffTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)

    @classmethod
    def tearDownClass(cls): cls.tmp.cleanup()

    def test_comparison(self):
        a=SessionMeta('a','codex','',100,cli_version='1')
        b=SessionMeta('b','codex','',100,cli_version='2')
        good=[Event(0,kind='context',text='AGENTS.md instructions old'), Event(1,kind='tool_call',call_id='x',text='cat memory.md'), Event(2,kind='tool_result',call_id='x',est_tokens=10)]
        bad=[Event(0,kind='context',text='AGENTS.md instructions new'), Event(1,kind='tool_call',call_id='x',text='cat memory.md'), Event(2,kind='tool_result',call_id='x',est_tokens=40),Event(3,kind='error')]
        result=diff.compare(a,good,b,bad)
        self.assertEqual(result['meta']['cli_version'], ['1','2'])
        self.assertEqual(result['inputs'][0]['bad_tokens'],40)
        self.assertIn('memory.md (memory file): 10 -> 40', diff._markdown(result))
        self.assertEqual(result['first_divergence']['bad'],'E3')

    def test_settings_compare_tools_and_policy(self):
        a=SessionMeta('a','codex','',1); b=SessionMeta('b','codex','',1)
        good=[Event(0,kind='settings',text='{"tools":[{"name":"read"}],"approval_policy":"ask"}')]
        bad=[Event(0,kind='settings',text='{"tools":[{"name":"write"}],"approval_policy":"never"}')]
        settings=diff.compare(a,good,b,bad)['settings']
        self.assertEqual(settings['tools'], {'added':['write'], 'removed':['read']})
        self.assertEqual(settings['approval_policy'], {'added':['never'], 'removed':['ask']})

    def test_outlier(self):
        rows=[]
        for i in range(8):
            meta=SessionMeta(str(i),'codex','',100,context_window=100,cli_version='2' if i >= 5 else '1')
            events=[Event(0,kind='token_usage',usage=Usage(input=10,total=30 if i >= 5 else 10,context_used=30 if i >= 5 else 10))]
            rows.append(trend.analyze(meta,events))
        trend.mark(rows)
        self.assertTrue(rows[5]['first_outlier'])
        self.assertIn('cli_version', rows[5]['changes'])

    def test_memory_read_tool_name(self):
        meta=SessionMeta('claude','claude','',0)
        events=[Event(0,kind='tool_call',tool='Read',call_id='x',text='notes/memory.md'), Event(1,kind='tool_result',call_id='x',est_tokens=19)]
        row=trend.analyze(meta,events)
        self.assertEqual(row['memory_read_tokens'],19)
        self.assertEqual(row['inputs']['notes/memory.md'],19)

    def test_memory_read_full_content_and_partial_results(self):
        meta = SessionMeta('synthetic', 'codex', '', 0)
        events = [
            Event(0, kind='tool_call', tool='shell', call_id='full', text='cat notes/memory.md'),
            Event(1, kind='tool_result', call_id='full', text='caf\u00e9', est_tokens=7),
            Event(2, kind='tool_call', tool='shell', call_id='partial', text='head notes/memory.md'),
            Event(3, kind='tool_result', call_id='partial', text='partial', est_tokens=3),
            Event(4, kind='tool_call', tool='Read', call_id='limited', text='{"file_path":"notes/memory.md","limit":10}'),
            Event(5, kind='tool_result', call_id='limited', text='partial', est_tokens=4),
            Event(6, kind='tool_call', tool='shell', call_id='truncated', text='cat notes/memory.md'),
            Event(7, kind='tool_result', call_id='truncated', text='output truncated', est_tokens=5),
        ]
        row = trend.analyze(meta, events)
        self.assertEqual(row['memory_read_tokens'], 7 + est_tokens('caf\u00e9'.encode('utf-8')) + 3 + 4 + 5)
        self.assertEqual(row['inputs']['notes/memory.md'], 19)

    def test_cached_trend_does_not_store_prompt(self):
        source = self.root/'source.jsonl'
        source.write_text('synthetic', encoding='utf-8')
        meta = SessionMeta('cachetest','codex',str(source),9)
        events = [Event(0,kind='user_msg',text='a private synthetic prompt with several words')]
        args = Namespace(automation=None, sessions=[str(source)], root=None, since=None, deep=False, out=self.root/'report', json=False)
        cache = self.root/'cache.jsonl'
        with patch.object(trend, 'checked_destination', side_effect=lambda p, **kw: cache if str(p).endswith('trend.jsonl') else Path(p)), patch.object(trend, 'read_session', return_value=iter([meta]+events)) as reader, redirect_stdout(StringIO()):
            self.assertEqual(trend.run(args), 0)
            self.assertEqual(trend.run(args), 0)
            self.assertEqual(reader.call_count, 1)
        self.assertNotIn('private synthetic prompt', cache.read_text(encoding='utf-8'))


class AutomationWorkflowTests(unittest.TestCase):
    """Exercise the actual CLI/readers/writers with a complete hand-written tree."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.configs = cls.root / 'automations'
        cls.sessions = cls.root / 'sessions'
        cls.sessions.mkdir()
        cls.prompt = 'inspect the synthetic project and summarize all changes in the working tree with clear evidence and detailed supporting examples'
        cls.private_prompt = 'CONFIG_PROMPT_PRIVATE'
        cls.private_memory = 'CONFIG_MEMORY_PRIVATE'
        other = 'review another synthetic project and report all outcomes with references and useful examples and detailed supporting evidence for every conclusion'
        for name, prompt in (('first', cls.prompt), ('second', other)):
            directory = cls.configs / name
            directory.mkdir(parents=True)
            config = {'id': name, 'name': name.title(), 'status': 'ACTIVE', 'kind': 'scheduled',
                      'prompt': prompt + ' ' + cls.private_prompt, 'rrule': 'FREQ=MONTHLY;BYDAY=MO',
                      'cwds': ['/synthetic'], 'model': 'synthetic-model', 'reasoning_effort': 'high',
                      'updated_at': 1}
            (directory / 'automation.toml').write_text('\n'.join(f'{k} = {json.dumps(v)}' for k, v in config.items()), encoding='utf-8')
            (directory / 'memory.md').write_text(cls.private_memory, encoding='utf-8')
        (cls.configs / 'first' / 'memory.backup.1.md').write_text('synthetic backup', encoding='utf-8')
        (cls.configs / 'first' / 'memory.archive-old.md').write_text('synthetic archive', encoding='utf-8')
        cls.memory_bodies = {}
        for number in range(1, 9):
            degraded = number >= 6
            timestamp = f'2026-01-{number:02d}T12:00:00Z'
            def record(kind, payload):
                return {'type': kind, 'timestamp': timestamp, 'payload': payload}
            def message(role, text):
                return record('response_item', {'type': 'message', 'role': role,
                                               'content': [{'type': 'input_text', 'text': text}]})
            agents = '# AGENTS.md instructions\n' + ('new guidance' if number >= 7 else 'old guidance')
            body = 'caf\u00e9 memory entry\n' * (10 if not degraded else 100 * (number - 5))
            cls.memory_bodies[number] = body
            usage = 300 if degraded else 100
            records = [record('session_meta', {'id': f'run{number:05d}', 'thread_source': 'automation',
                                              'cwd': '/synthetic', 'cli_version': '2' if degraded else '1',
                                              'base_instructions': agents}),
                       record('turn_context', {'model': 'synthetic-model', 'reasoning_effort': 'high',
                                               'history_mode': 'enabled', 'approval_policy': 'ask'}),
                       message('developer', 'inspect carefully' if not degraded else 'inspect carefully and verify each change'),
                       message('user', agents), message('user', cls.prompt),
                       record('response_item', {'type': 'function_call', 'name': 'shell', 'call_id': 'read',
                                                'arguments': 'cat memory.md'}),
                       record('response_item', {'type': 'function_call_output', 'call_id': 'read', 'output': body}),
                       record('event_msg', {'type': 'token_count', 'info': {
                           'last_token_usage': {'input_tokens': usage, 'cached_input_tokens': 0, 'total_tokens': usage},
                           'total_token_usage': {'total_tokens': usage}, 'model_context_window': 1000}})]
            if degraded:
                records.append(record('compacted', {'replacement_history': []}))
            records.append(record('event_msg', {'type': 'task_complete'}))
            records[-1]['timestamp'] = f'2026-01-{number:02d}T12:{"03" if degraded else "01"}:00Z'
            (cls.sessions / f'run{number}.jsonl').write_text('\n'.join(json.dumps(r) for r in records) + '\n', encoding='utf-8')
        records[0]['payload']['id'] = 'ambiguous'
        records[4]['payload']['content'][0]['text'] = cls.prompt + ' ' + cls.private_prompt + ' ' + other + ' ' + cls.private_prompt
        (cls.sessions / 'ambiguous.jsonl').write_text('\n'.join(json.dumps(r) for r in records) + '\n', encoding='utf-8')

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def cli(self, arguments, output):
        stdout, stderr = StringIO(), StringIO()
        with chdir(self.root), patch.object(automations, 'home', return_value=self.configs), redirect_stdout(stdout), redirect_stderr(stderr):
            status = main([*map(str, arguments), '--out', str(output)])
        self.assertEqual(status, 0, stderr.getvalue())
        self.assertEqual(stderr.getvalue(), '')
        if '--json' not in arguments:
            self.assertLessEqual(len(stdout.getvalue().splitlines()), 20)
        return stdout.getvalue()

    def assert_view(self, directory, command, session, raw_bytes, cap):
        text = (directory / f'{command}.md').read_text(encoding='utf-8')
        match = re.match(r'<!-- agentlog (\w+) \| session (\w+) \| raw_tokens (\d+) \| this_view_tokens (\d+) \| ratio ([\d.]+) -->\n', text)
        self.assertIsNotNone(match, text[:200])
        self.assertEqual((match[1], match[2]), (command, session[:8]))
        raw_tokens = (raw_bytes * 2 + 6) // 7
        self.assertEqual(int(match[3]), raw_tokens)
        self.assertEqual(int(match[4]), est_tokens(text))
        self.assertAlmostEqual(float(match[5]), est_tokens(text) / raw_tokens, places=6)
        self.assertLessEqual(est_tokens(text), cap)
        return text

    def test_trend_cli_eight_runs_and_cache(self):
        output = self.root / 'trend-report'
        arguments = ['trend', '--automation', 'first', '--root', self.sessions]
        cache = self.root / 'agentlog-out' / '.cache' / 'trend.jsonl'
        cache.unlink(missing_ok=True)
        with patch.object(trend, 'read_session', wraps=trend.read_session) as reader, patch('agentlog.compress.compress_session', side_effect=AssertionError('cheap trend must not compress')):
            self.cli(arguments, output)
        self.assertEqual(reader.call_count, 9)
        rows = json.loads((output / 'trend.json').read_text(encoding='utf-8'))
        self.assertEqual([row['id'] for row in rows], [f'run{n:05d}' for n in range(1, 9)])
        self.assertEqual([r['id'] for r in rows if r['first_outlier']], ['run00006'])
        self.assertIn('cli_version', rows[5]['flags'])
        self.assertIn('cli_version', rows[5]['changes'])
        self.assertNotIn('instructions_sha1', rows[5]['changes'])
        self.assertIn('instructions_sha1', rows[6]['changes'])
        for number, row in enumerate(rows, 1):
            body = self.memory_bodies[number]
            result_tokens = (len(body) * 2 + 6) // 7
            content_tokens = (len(body.encode('utf-8')) * 2 + 6) // 7
            self.assertEqual(row['memory_read_tokens'], result_tokens + content_tokens)
            self.assertEqual(row['inputs'], {'memory.md': result_tokens})
            self.assertEqual(row['completed'], True)
        self.assertTrue(all(rows[i]['memory_read_tokens'] < rows[i + 1]['memory_read_tokens'] for i in (4, 5, 6)))
        raw_bytes = sum((self.sessions / f'run{n}.jsonl').stat().st_size for n in range(1, 9))
        view = self.assert_view(output, 'trend', 'trend', raw_bytes, 3000)
        self.assertIn('FIRST OUTLIER', view)
        self.assertIn('cli_version=2 \u25b2 \u0394', view)
        with (output / 'trend.csv').open(encoding='utf-8', newline='') as source:
            csvrows = list(csv.DictReader(source))
        self.assertEqual([int(r['memory_read_tokens']) for r in csvrows], [r['memory_read_tokens'] for r in rows])
        original = {p.name: p.read_bytes() for p in output.iterdir()}
        with patch.object(trend, 'read_session', side_effect=AssertionError('cache hit must not reread session')):
            self.cli(arguments, output)
        self.assertEqual({p.name: p.read_bytes() for p in output.iterdir()}, original)

    def test_existing_deep_reports_enrich_cheap_metrics(self):
        source = self.root / 'enriched.jsonl'
        source.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': 'enriched'}}) + '\n', encoding='utf-8')
        directory = self.root / 'agentlog-out' / 'enriched'
        directory.mkdir(parents=True)
        reports = {'profile': {'model_calls': 4, 'uncached_input': 9, 'peak_percent': 75, 'totals': {'total': 30}},
                   'compress': {'stats': {'novelty_per_turn': [{'new_block_tokens': 5, 'all_tokens': 20}]}},
                   'loops': {'findings': [{'severity': 'high'}, {'severity': 'low'}]}}
        for name, data in reports.items():
            (directory / f'{name}.json').write_text(json.dumps(data), encoding='utf-8')
        output = self.root / 'enriched-report'
        self.cli(['trend', source], output)
        row, = json.loads((output / 'trend.json').read_text(encoding='utf-8'))
        for key, value in {'model_calls': 4, 'uncached_input': 9, 'peak_context_percent': 75,
                           'cumulative_tokens': 30, 'novelty_ratio': .25, 'loops_high': 1}.items():
            self.assertEqual(row[key], value)

    def test_diff_cli_reports_actual_growth_instruction_and_refs(self):
        output = self.root / 'diff-report'
        self.cli(['diff', self.sessions / 'run5.jsonl', self.sessions / 'run6.jsonl', '--json'], output)
        data = json.loads((output / 'diff.json').read_text(encoding='utf-8'))
        self.assertEqual(data['meta']['cli_version'], ['1', '2'])
        memory = next(item for item in data['inputs'] if item['path'] == 'memory.md')
        expected = [(len(self.memory_bodies[n]) * 2 + 6) // 7 for n in (5, 6)]
        self.assertEqual([memory['good_tokens'], memory['bad_tokens']], expected)
        self.assertEqual(data['first_divergence'], {'good': 'E8', 'bad': 'E8'})
        for number, side, kind in ((5, 'good', 'task_end'), (6, 'bad', 'compaction')):
            _, events = diff.load_session(str(self.sessions / f'run{number}.jsonl'))
            seq = int(data['first_divergence'][side][1:])
            self.assertEqual(next(event for event in events if event.seq == seq).kind, kind)
        developer = next(item for item in data['instructions'] if item['category'] == 'developer')
        self.assertIn('-inspect carefully', developer['diff'])
        self.assertIn('+inspect carefully and verify each change', developer['diff'])
        self.assertTrue(data['new_blocks'])
        self.assertTrue(all(re.fullmatch(r'[BV]\d+', block['ref']) and re.fullmatch(r'E\d+', block['first_ref']) for block in data['new_blocks']))
        view = self.assert_view(output, 'diff', 'run00006', (self.sessions / 'run6.jsonl').stat().st_size, 6000)
        self.assertIn(f'memory.md (memory file): {expected[0]} -> {expected[1]}', view)
        self.assertIn('E8', view)
        self.assertIn('agentlog show', view)
        output7 = self.root / 'agents-diff'
        self.cli(['diff', self.sessions / 'run6.jsonl', self.sessions / 'run7.jsonl'], output7)
        changed = json.loads((output7 / 'diff.json').read_text(encoding='utf-8'))
        agents = next(item for item in changed['instructions'] if item['category'] == 'AGENTS.md')
        self.assertIn('-old guidance', agents['diff'])
        self.assertIn('+new guidance', agents['diff'])

    def test_cli_full_file_and_partial_memory_read_accounting(self):
        path = self.root / 'read-accounting.jsonl'
        body = 'caf\u00e9 full memory contents'
        cases = [('shell', 'cat notes/memory.md', body, True),
                 ('exec_command', json.dumps({'cmd': 'Get-Content notes/memory.md'}), body, True),
                 ('shell', json.dumps({'command': 'cat notes/memory.md'}), body, True),
                 ('Read', json.dumps({'file_path': 'notes/memory.md'}), body, True),
                 ('shell', 'head notes/memory.md', body, False),
                 ('Read', json.dumps({'file_path': 'notes/memory.md', 'limit': 10}), body, False),
                 ('Read', json.dumps({'file_path': 'notes/memory.md', 'offset': 0}), body, False),
                 ('shell', 'cat notes/memory.md', 'output truncated: ' + body, False),
                 ('shell', 'cat notes/memory.md | head', body, False)]
        records = [{'type': 'session_meta', 'payload': {'id': 'readaccount'}}]
        for index, (tool, command, result, _) in enumerate(cases):
            records.extend([{'type': 'response_item', 'payload': {
                'type': 'function_call', 'name': tool, 'call_id': str(index), 'arguments': command}},
                {'type': 'response_item', 'payload': {
                    'type': 'function_call_output', 'call_id': str(index), 'output': result}}])
        path.write_text('\n'.join(json.dumps(record) for record in records) + '\n', encoding='utf-8')
        output = self.root / 'read-accounting-report'
        self.cli(['trend', path], output)
        row, = json.loads((output / 'trend.json').read_text(encoding='utf-8'))
        result_tokens = sum((len(result) * 2 + 6) // 7 for _, _, result, _ in cases)
        content_tokens = sum((len(result.encode('utf-8')) * 2 + 6) // 7 for _, _, result, full in cases if full)
        self.assertEqual(row['inputs'], {'notes/memory.md': result_tokens})
        self.assertEqual(row['memory_read_tokens'], result_tokens + content_tokens)

    def test_automation_cli_matching_stats_and_all_output_privacy(self):
        commands = [(['automations', 'list', '--root', self.sessions], 'automations'),
                    (['automations', 'runs', 'first', '--root', self.sessions], 'automations'),
                    (['automations', 'show', 'first'], 'automations'),
                    (['trend', '--automation', 'first', '--root', self.sessions], 'trend'),
                    (['diff', self.sessions / 'run5.jsonl', self.sessions / 'run6.jsonl'], 'diff')]
        for index, (command, filename) in enumerate(commands):
            for json_mode in (False, True):
                with self.subTest(command=command[:2], json=json_mode):
                    output = self.root / f'privacy-{index}-{json_mode}'
                    stdout = self.cli(command + (['--json'] if json_mode else []), output)
                    for text in [stdout, *[p.read_text(encoding='utf-8') for p in output.iterdir()]]:
                        self.assertNotIn(self.private_prompt, text)
                        self.assertNotIn(self.private_memory, text)
                    data = json.loads((output / f'{filename}.json').read_text(encoding='utf-8'))
                    if command[:2] == ['automations', 'list']:
                        first = next(row for row in data if row['id'] == 'first')
                        self.assertEqual(first['runs'], 8)
                        self.assertEqual(next(row for row in data if row['id'] == 'second')['runs'], 0)
                        self.assertEqual(first['memory_bytes'], len(self.private_memory.encode('utf-8')))
                        self.assertEqual(first['prompt_tokens'], est_tokens(self.prompt + ' ' + self.private_prompt))
                        self.assertEqual({r['name']: r['bytes'] for r in first['backups']}, {'memory.backup.1.md': len('synthetic backup'), 'memory.archive-old.md': len('synthetic archive')})
                        self.assertEqual(first['schedule_phrase'], 'FREQ=MONTHLY;BYDAY=MO')
                    elif command[:2] == ['automations', 'runs']:
                        self.assertEqual(len(data), 9)
                        ambiguous = next(row for row in data if row['id'] == 'ambiguous')
                        self.assertEqual(ambiguous['match_reason'], 'ambiguous')
                        self.assertIsNone(ambiguous['automation_id'])
                        self.assertTrue(all(r['automation_id'] == 'first' for r in data if r['id'] != 'ambiguous'))
                    if filename == 'automations':
                        raw_bytes = sum(p.stat().st_size for p in self.configs.glob('*/automation.toml'))
                        self.assert_view(output, filename, 'automations', raw_bytes, 4000)
