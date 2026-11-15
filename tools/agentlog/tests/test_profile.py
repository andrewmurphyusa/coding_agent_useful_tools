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

"""Synthetic tests for profile and compaction reports."""
import json
import csv
from argparse import Namespace
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from agentlog import profile, compactions
from agentlog.core.tokens import est_tokens
from agentlog.readers.base import Event, SessionMeta, Usage
from agentlog.readers.discover import load_session

class ProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)

    @classmethod
    def tearDownClass(cls): cls.tmp.cleanup()

    def meta(self, source='codex'):
        return SessionMeta('synthetic', source, str(self.root/'input.jsonl'), 100, context_window=100)

    def test_authoritative_totals_and_carry(self):
        events = [Event(0, kind='user_msg', text='abc', est_tokens=3, turn=1),
                  Event(1, kind='token_usage', usage=Usage(input=10,cached_input=4,output=2,reasoning=1,total=12,context_used=10), turn=1),
                  Event(2, kind='assistant_msg', text='def', est_tokens=2, turn=1),
                  Event(3, kind='token_usage', usage=Usage(input=20,cached_input=10,output=3,reasoning=2,total=23,context_used=20), turn=1),
                  Event(4, kind='compaction', turn=1),
                  Event(5, kind='token_usage', usage=Usage(input=5,cached_input=0,output=1,reasoning=0,total=6,context_used=5), turn=1)]
        result = profile.analyze(self.meta(), events)
        self.assertEqual(result['totals'], {'input':35,'cached_input':14,'cache_creation_input':None,'output':6,'reasoning':3,'total':41})
        self.assertEqual(result['uncached_input'], 21)
        self.assertEqual([(x['ref'], x['carry_cost']) for x in result['carry_cost']], [('E0', 6), ('E2', 2)])
        self.assertEqual(result['timeline'][0]['markers'], '\u27f3')
        self.assertEqual(result['total_sources']['input'], 'authoritative')

    def test_no_usage_and_many_buckets(self):
        result = profile.analyze(self.meta(), [Event(0,kind='user_msg',est_tokens=7,turn=1)])
        self.assertEqual(result['totals']['total'], 7)
        self.assertEqual(result['total_sources']['total'], 'estimated')
        events = [Event(i,kind='token_usage',turn=i+1,usage=Usage(input=i,total=i,context_used=i)) for i in range(130)]
        result = profile.analyze(self.meta(), events)
        self.assertEqual(len(result['timeline']), 60)
        self.assertEqual(len(result['context_sparkline']), 120)
        self.assertNotIn('\n', result['context_sparkline'])

    def test_reader_fixtures_usage(self):
        fixtures = Path(__file__).parent/'fixtures'
        for file in fixtures.rglob('*.jsonl'):
            meta, events = load_session(str(file))
            result = profile.analyze(meta, events)
            self.assertEqual(result['model_calls'], sum(e.kind == 'token_usage' for e in events))
            for key in ('input','cached_input','output','reasoning','total'):
                values = [getattr(e.usage,key) for e in events if e.kind == 'token_usage' and e.usage]
                if values and all(v is not None for v in values): self.assertEqual(result['totals'][key], sum(values))

        meta, events = load_session(str(fixtures/'codex.jsonl'))
        result = profile.analyze(meta, events)
        self.assertEqual(result['totals'], dict(input=100, cached_input=20, cache_creation_input=None, output=30, reasoning=10, total=130))
        self.assertEqual(result['uncached_input'], 80)
        self.assertEqual(compactions.analyze(meta, events)['compactions'][0]['summary'], 'Synthetic summary')
        meta, events = load_session(str(fixtures/'claude.jsonl'))
        result = profile.analyze(meta, events)
        self.assertEqual(result['totals'], dict(input=100, cached_input=30, cache_creation_input=20, output=10, reasoning=0, total=160))
        self.assertEqual(result['uncached_input'], 120)
        self.assertEqual(result['total_sources']['reasoning'], 'estimated')
        self.assertEqual(compactions.analyze(meta, events)['compactions'][1]['summary'], 'Synthetic compaction summary')

    def test_cache_creation_missing_zero_and_report_formats(self):
        for creation, expected in ((None, None), (0, 100), (20, 120)):
            with self.subTest(creation=creation):
                usage = Usage(input=100, cached_input=30, output=10, total=160,
                              context_used=150, cache_creation_input=creation)
                result = profile.analyze(self.meta('claude'), [Event(0, kind='token_usage', usage=usage, turn=1)])
                self.assertEqual(result['totals']['cache_creation_input'], creation)
                self.assertEqual(result['calls'][0]['cache_creation_input'], creation)
                self.assertEqual(result['uncached_input'], expected)
                self.assertEqual(result['turn_health']['1']['uncached_input'], expected)
                self.assertEqual(result['calls'][0]['context_used'], 150)
                self.assertEqual(result['totals']['total'], 160)
                self.assertIn(f"- E0: {creation}", profile._markdown(result))
        events = [Event(0, kind='token_usage', usage=Usage(input=100, cached_input=30, cache_creation_input=20), turn=1),
                  Event(1, kind='token_usage', usage=Usage(input=10, cached_input=0), turn=1)]
        mixed = profile.analyze(self.meta('claude'), events)
        self.assertIsNone(mixed['totals']['cache_creation_input'])
        self.assertIsNone(mixed['uncached_input'])
        self.assertEqual(profile.analyze(self.meta('codex'), events)['uncached_input'], 80)
        destination = self.root / 'profile-formats'
        fixture = Path(__file__).parent / 'fixtures' / 'claude.jsonl'
        with redirect_stdout(StringIO()):
            profile.run(Namespace(session=str(fixture), root=None, out=destination, json=False))
        saved = json.loads((destination / 'profile.json').read_text(encoding='utf-8'))
        with (destination / 'profile.csv').open(encoding='utf-8', newline='') as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(saved['totals']['cache_creation_input'], 20)
        self.assertEqual(rows[0]['cache_creation_input'], '20')
        self.assertIn('cache_creation_input', (destination / 'profile.md').read_text(encoding='utf-8'))

    def test_tool_call_additions_and_carry_across_compaction(self):
        events = [Event(0, kind='tool_call', tool='shell', call_id='a', text='cat src/a.py', est_tokens=100, turn=1),
                  Event(1, kind='user_msg', est_tokens=1, turn=1)]
        result = profile.analyze(self.meta(), events)
        self.assertEqual(result['totals']['total'], 101)
        self.assertEqual(result['tokens_added'], 101)
        self.assertEqual(result['turn_health']['1']['tokens_added'], 101)
        self.assertEqual(result['biggest_events'][0]['ref'], 'E0')
        self.assertEqual(result['categories'][0]['category'], 'tool_call shell cat src/a.py')
        events += [Event(2, kind='token_usage', usage=Usage(input=10, cached_input=0, total=11, context_used=10), turn=1),
                   Event(3, kind='compaction', turn=1),
                   Event(4, kind='tool_call', tool='shell', call_id='b', text='cat src/b.py', est_tokens=5, turn=2),
                   Event(5, kind='token_usage', usage=Usage(input=8, cached_input=0, total=9, context_used=8), turn=2)]
        result = profile.analyze(self.meta(), events)
        self.assertEqual(result['totals']['total'], 20)
        self.assertEqual(result['calls'][0]['tokens_added'], 101)
        self.assertEqual(result['timeline'][0]['top_consumer'], 'shell cat src/a.py')
        self.assertEqual({x['ref']: x['carry_cost'] for x in result['carry_cost']}, {'E0': 100, 'E1': 1, 'E4': 5})

    def test_timeline_markers_and_mixed_sources(self):
        events = [Event(0, kind='user_msg', est_tokens=7, turn=1),
                  Event(1, kind='token_usage', usage=Usage(input=10, total=12, context_used=10), turn=1),
                  Event(2, kind='compaction', turn=2),
                  Event(3, kind='error', text='rate limit', turn=2),
                  Event(4, kind='token_usage', usage=Usage(), turn=2),
                  Event(5, kind='error', text='usage limit', turn=3),
                  Event(6, kind='error', text='failed', turn=3),
                  Event(7, kind='token_usage', usage=Usage(input=4, total=5, context_used=4), turn=3)]
        result = profile.analyze(self.meta(), events)
        self.assertEqual(set(result['timeline'][1]['markers']), {'\u27f3', '\u26a0'})
        self.assertEqual(set(result['timeline'][2]['markers']), {'\u26a0', '\u2716'})
        self.assertEqual({ord(c) for c in result['timeline'][1]['markers']}, {0x27F3, 0x26A0})
        self.assertEqual({ord(c) for c in result['timeline'][2]['markers']}, {0x26A0, 0x2716})
        self.assertEqual(result['timeline'][1]['delta_total_source'], 'estimated')
        self.assertEqual(result['timeline'][1]['context_source'], 'estimated')
        self.assertEqual(result['total_sources']['total'], 'mixed')
        report = profile._markdown(result)
        self.assertIn('rate-limit errors: 1; usage-limit errors: 1', report)
        self.assertIn('(estimated)', report)
        self.assertIn(chr(0x27F3), report)
        self.assertIn(chr(0x2716), report)
        self.assertIn(chr(0x26A0), report)

    def test_compaction_drift_and_reacquisition(self):
        events = [Event(0,kind='tool_call',tool='shell',call_id='a',text='cat src/plan.md',turn=1),
                  Event(1,kind='token_usage',usage=Usage(context_used=90),turn=1),
                  Event(2,kind='compaction',text='old plan\nkeep',turn=1),
                  Event(3,kind='token_usage',usage=Usage(context_used=20),turn=2),
                  Event(4,kind='tool_call',tool='shell',call_id='b',text='cat src/plan.md',turn=2),
                  Event(5,kind='tool_result',call_id='b',est_tokens=9,turn=2),
                  Event(6,kind='compaction',text='new plan\nkeep',turn=3),
                  Event(7,kind='token_usage',usage=Usage(context_used=10),turn=4)]
        rows = compactions.analyze(self.meta(), events)['compactions']
        self.assertEqual((rows[0]['before'],rows[0]['after']),(90,20))
        self.assertEqual(rows[0]['reacquired'][0]['tokens'],9)
        self.assertEqual(rows[1]['drift'],['-old plan','+new plan'])

    def test_compaction_three_summaries_and_read_window(self):
        events = [Event(0,kind='tool_call',tool='Read',call_id='a',text='src/plan.md',turn=1),
                  Event(1,kind='compaction',text='plan 123\nold',turn=1),
                  Event(2,kind='tool_call',tool='Read',call_id='b',text='src/plan.md',turn=2),
                  Event(3,kind='tool_result',call_id='b',est_tokens=11,turn=2),
                  Event(4,kind='compaction',text='plan 456\nnew',turn=3),
                  Event(5,kind='compaction',text='plan 789\nfinal',turn=4),
                  Event(6,kind='tool_call',tool='Read',call_id='c',text='src/plan.md',turn=15),
                  Event(7,kind='tool_result',call_id='c',est_tokens=99,turn=15)]
        rows = compactions.analyze(self.meta(), events)['compactions']
        self.assertEqual(rows[0]['reacquired'], [{'path':'src/plan.md', 'tokens':11, 'refs':['E2']}])
        self.assertEqual(rows[1]['drift'], ['-old', '+new'])
        self.assertEqual(rows[2]['drift'], ['-new', '+final'])
        self.assertEqual(rows[2]['reacquired'], [])

    def test_compaction_summary_token_boundaries(self):
        for text in ('', 'x' * 5249, 'x' * 5250, 'x' * 5251, '\u2603' * 35000):
            with self.subTest(length=len(text)):
                event = Event(23, kind='compaction', text=text, turn=1)
                row = compactions.analyze(self.meta(), [event])['compactions'][0]
                self.assertLessEqual(est_tokens(row['summary']), 1500)
                self.assertEqual(row['summary_tokens'], est_tokens(text))
                self.assertEqual(row['summary_truncated'], est_tokens(text) > 1500)
                if row['summary_truncated']:
                    self.assertIn('show E23 for full text', row['summary'])
                else:
                    self.assertEqual(row['summary'], text)
                # Report generation never mutates the full normalized event.
                self.assertEqual(event.text, text)

    def test_compaction_serialized_summary_budget_and_header(self):
        text = 'Synthetic summary line.\n' * 2000
        source = self.root / 'long-compaction.jsonl'
        records = [
            {'type': 'session_meta', 'payload': {'id': 'synthetic-compaction'}},
            {'type': 'compacted', 'payload': {'message': text}},
        ]
        source.write_text('\n'.join(json.dumps(r) for r in records) + '\n', encoding='utf-8')
        destination = self.root / 'compaction-budget'
        with redirect_stdout(StringIO()):
            status = compactions.run(Namespace(session=str(source), root=None, out=destination, json=False))
        self.assertEqual(status, 0)
        report = json.loads((destination / 'compactions.json').read_text(encoding='utf-8'))
        row = report['compactions'][0]
        self.assertLessEqual(est_tokens(row['summary']), 1500)
        self.assertTrue(row['summary_truncated'])
        self.assertIn('show E1 for full text', row['summary'])
        markdown = (destination / 'compactions.md').read_text(encoding='utf-8')
        self.assertTrue(markdown.startswith('<!-- agentlog compactions | session syntheti | raw_tokens '))
        self.assertIn(row['summary'], markdown)
        self.assertLessEqual(est_tokens(markdown), 6000)
        _, events = load_session(str(source))
        self.assertEqual(events[1].text, text)

    def test_drift_line_cap_and_no_compactions(self):
        before = '\n'.join(f'old line {chr(65 + i)}' for i in range(50))
        after = '\n'.join(f'new line {chr(65 + i)}' for i in range(50))
        result = compactions.analyze(self.meta(), [Event(0, kind='compaction', text=before), Event(1, kind='compaction', text=after)])
        self.assertEqual(len(result['compactions'][1]['drift']), 40)
        self.assertTrue(all(line.startswith(('+', '-')) for line in result['compactions'][1]['drift']))
        result = compactions.analyze(self.meta(), [Event(0, kind='token_usage', usage=Usage(context_used=73))])
        self.assertEqual(compactions._markdown(result), 'No compactions\n\nPeak context: 73')

    def test_report_budget(self):
        events = [Event(i, kind='user_msg', turn=i//2+1, est_tokens=50, text='synthetic') if i % 2 == 0
                  else Event(i, kind='token_usage', turn=i//2+1, usage=Usage(input=50, cached_input=0, output=10, reasoning=0, total=60, context_used=50))
                  for i in range(5000)]
        meta = self.meta(); data = profile.analyze(meta,events)
        from agentlog.core.out import write_outputs
        dest = self.root/'budget'
        write_outputs('profile',meta,data,profile._markdown(data),dest,max_tokens=3000)
        markdown = (dest/'profile.md').read_text(encoding='utf-8')
        self.assertTrue(markdown.startswith('<!-- agentlog profile | session syntheti | raw_tokens 29 | this_view_tokens '))
        self.assertLessEqual(est_tokens(markdown),3000)
        self.assertEqual(len(data['timeline']), 60)

if __name__ == '__main__': unittest.main()
