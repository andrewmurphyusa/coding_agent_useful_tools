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

"""Synthetic checks for loop rules and reporting."""
import tempfile
import unittest
import contextlib
import io
import json
from pathlib import Path
from unittest.mock import patch
from dataclasses import replace

from agentlog import compress, loops
from agentlog.core.tokens import est_tokens
from agentlog.readers.base import Event, SessionMeta, Usage

META = SessionMeta('synthetic', 'generic', 'synthetic.jsonl', 1000, context_window=100)


def call(seq, text, turn=0, tool='shell'):
    return Event(seq, kind='tool_call', tool=tool, call_id=str(seq), text=text, turn=turn, est_tokens=2)


def result(seq, call_seq, tokens=10, text='ok', error=False):
    return Event(seq, kind='tool_result', call_id=str(call_seq), text=text, est_tokens=tokens, exit_code=1 if error else 0)


def find(events, rule, compressed=None):
    compressed = compressed or compress.compress_session(META, events)
    return [f for f in loops.analyze(META, events, compressed)['findings'] if f['rule'] == rule]


class LoopRules(unittest.TestCase):
    def test_repeated_call_and_write_boundary(self):
        calls = [call(i, 'cat file.txt') for i in range(3)]
        self.assertEqual(len(find(calls, 'R1')), 1)
        self.assertFalse(find([calls[0], call(1, 'write file.txt'), call(2, 'cat file.txt'), call(3, 'cat file.txt')], 'R1'))

    def test_repeated_error(self):
        events = [result(i, i, text='failure', error=True) for i in range(3)]
        self.assertEqual(len(find(events, 'R2')), 1)
        self.assertFalse(find(events[:2], 'R2'))

    def test_no_change_compressor_loop(self):
        events = [result(i, i, text='failure', error=True) for i in range(3)]
        self.assertTrue(find(events, 'R3'))
        self.assertFalse(find(events[:2], 'R3'))

    def test_edit_revert(self):
        events = [call(0, '*** Update File: file.txt\n-old\n+new', tool='apply_patch'), call(1, '*** Update File: file.txt\n-new\n+old', tool='apply_patch')]
        self.assertTrue(find(events, 'R4'))
        self.assertFalse(find([events[0], call(1, '*** Update File: file.txt\n-other\n+old', tool='apply_patch')], 'R4'))

    def test_reread_cost_and_write(self):
        events = [call(0, 'cat file.txt'), result(1, 0, 5), call(2, 'cat file.txt'), result(3, 2, 7), call(4, 'cat file.txt'), result(5, 4, 9)]
        self.assertEqual(find(events, 'R5')[0]['tokens'], 16)
        self.assertFalse(find(events[:2]+[call(2, 'write file.txt')]+[call(3, 'cat file.txt'), call(4, 'cat file.txt')], 'R5'))

    def test_output_growth(self):
        events = sum(([call(i*2, 'cat file.txt'), result(i*2+1, i*2, n)] for i, n in enumerate([4, 6, 8])), [])
        self.assertTrue(find(events, 'R6'))
        self.assertFalse(find(events[:-2], 'R6'))

    def test_no_progress(self):
        events = [Event(i, kind='other', turn=i) for i in range(5)]
        self.assertTrue(find(events, 'R7'))
        self.assertFalse(find(events[:-1], 'R7'))

    def test_reacquisition(self):
        events = [call(0, 'cat file.txt'), result(1, 0, 10), Event(2, kind='compaction'), call(3, 'cat file.txt'), result(4, 3, 5)]
        self.assertTrue(find(events, 'R8'))
        self.assertFalse(find(events[:-1]+[result(4, 3, 4)], 'R8'))

    def test_saturation(self):
        events = [Event(i, kind='token_usage', usage=Usage(context_used=n)) for i, n in enumerate([85, 10, 10])]
        self.assertTrue(find(events, 'R9'))
        self.assertFalse(find(events+[Event(3, kind='token_usage', usage=Usage(context_used=10))], 'R9'))

    def test_fan_out(self):
        events = [call(i, '{}', tool='spawn_agent') for i in range(21)]
        self.assertTrue(find(events, 'R10'))
        self.assertFalse(find(events[:-1], 'R10'))

    def test_thread_depth_and_unresolved_branches(self):
        rows = [
            {'thread_id': 'a', 'parent': 'synthetic'},
            {'thread_id': 'b', 'parent': 'a'},
            {'thread_id': 'c', 'parent': 'b'},
            {'thread_id': 'c', 'parent': 'b'},
            {'thread_id': 'd', 'parent': 'c'},
            {'thread_id': 'bad', 'parent': 'a'}, {'thread_id': 'bad', 'parent': 'b'},
            {'thread_id': 'lost', 'parent': 'missing'},
            {'thread_id': 'cycle1', 'parent': 'cycle2'}, {'thread_id': 'cycle2', 'parent': 'cycle1'},
            {'thread_id': '[REDACTED:account_id]', 'parent': 'synthetic'},
        ]
        meta = replace(META, extra={'threads': rows})
        events = [Event(0, kind='error', text='rate limit exceeded', est_tokens=5)]
        data = loops.analyze(meta, events, compress.compress_session(meta, events))
        finding = next(f for f in data['findings'] if f['rule'] == 'R10')
        self.assertEqual(finding['metadata_evidence'], [['synthetic', 'a', 'b', 'c'], ['synthetic', 'a', 'b', 'c', 'd']])
        self.assertEqual((finding['occurrences'], finding['tokens'], finding['first'], finding['evidence']), (2, None, None, []))
        self.assertEqual(finding['show_hint'], 'show F2')
        self.assertEqual(data['unknown_cost_findings'], 1)
        self.assertEqual(data['unresolved_thread_branches'], 5)
        self.assertIn('| ? | unavailable |', loops._markdown(data))
        self.assertEqual(loops._thread_chains(META), ([], 0, False))

    def test_instruction_growth(self):
        events = sum(([call(i*2, 'cat memory.md', turn=i), result(i*2+1, i*2, n)] for i, n in enumerate([8, 9, 10])), [])
        self.assertTrue(find(events, 'R11'))
        self.assertFalse(find(events[:-2], 'R11'))

    def test_limits(self):
        self.assertTrue(find([Event(0, kind='error', text='rate limit exceeded')], 'R12'))
        self.assertFalse(find([Event(0, kind='error', text='unrelated failure')], 'R12'))

    def test_order_budget_and_overrides(self):
        events = [Event(i, kind='error', text='rate limit exceeded', est_tokens=i+1) for i in range(100)]
        data = loops.analyze(META, events, compress.compress_session(META, events))
        self.assertEqual(data['findings'][0]['ref'], 'F1')
        self.assertEqual(data['findings'][0]['tokens'], 100)
        self.assertIn('more (loops.json)', loops._markdown(data))
        self.assertLessEqual(est_tokens(loops._markdown(data)), 3000)
        self.assertEqual(loops._overrides(['R1.min=4'])['R1']['min'], 4)
        with self.assertRaises(ValueError): loops._overrides(['R1.unknown=4'])

    def test_sliding_window_and_large_severity(self):
        events = [call(i, 'cat file.txt') for i in (0, 40, 51, 52, 53, 54)]
        finding = find(events, 'R1')[0]
        self.assertEqual((finding['first'], finding['last'], finding['severity']), ('E40', 'E54', 'high'))

    def test_command_exit_and_references(self):
        from agentlog.cli import main
        events = [Event(i, kind='error', text='rate limit exceeded', est_tokens=8) for i in range(3)]
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'out'
            with patch('agentlog.loops.load_session', return_value=(META, events)), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(['loops', 'fake', '--out', str(directory), '--rules', 'R12', '--fail-on', 'high']), 1)
                self.assertEqual(main(['loops', 'fake', '--out', str(directory), '--rules', 'R1', '--fail-on', 'high']), 0)
                self.assertEqual(main(['loops', 'fake', '--out', str(directory), '--set', 'R1.nope=4']), 2)
            self.assertTrue((directory/'compress.json').is_file())
            report = json.loads((directory/'loops.json').read_text(encoding='utf-8'))
            self.assertEqual(report['findings'], [])
            self.assertLessEqual(est_tokens((directory/'loops.md').read_text(encoding='utf-8')), 3000)

    def test_requestdump_threads_csv_and_show(self):
        from agentlog.cli import main
        from test_reader_requestdump import snapshot
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root/'dump'
            folder.mkdir()
            (folder/'request-001.txt').write_text(snapshot(1, [('message', 'user', '', '', 'Question')]), encoding='utf-8')
            csv = root/'threads.csv'
            csv.write_text('session_id,thread_id,parent_thread_id\nsynthetic-session,a,synthetic-session\nsynthetic-session,b,a\nsynthetic-session,c,b\n', encoding='utf-8')
            out = root/'out'
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(['loops', str(folder), '--threads-csv', str(csv), '--out', str(out), '--rules', 'R10', '--fail-on', 'medium']), 1)
                self.assertEqual(main(['show', 'F1', '--session', str(folder), '--threads-csv', str(csv), '--out', str(out)]), 0)
            finding = json.loads((out/'loops.json').read_text(encoding='utf-8'))['findings'][0]
            self.assertEqual(finding['metadata_evidence'], [['synthetic-session', 'a', 'b', 'c']])


if __name__ == '__main__': unittest.main()
