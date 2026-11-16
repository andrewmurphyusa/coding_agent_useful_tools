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

"""Synthetic automation tests."""
import tempfile
import json
import struct
import warnings
from pathlib import Path
import unittest
from argparse import Namespace
from contextlib import chdir, nullcontext, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from itertools import permutations
from types import SimpleNamespace
from unittest.mock import patch
from agentlog import automations
from agentlog.cli import main
from agentlog.core.tokens import est_tokens
from agentlog.readers.base import Event, SessionMeta

class AutomationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)

    @classmethod
    def tearDownClass(cls): cls.tmp.cleanup()

    def test_matching(self):
        meta = SessionMeta('synthetic', 'codex', '', 0, thread_source='automation', cwd='work')
        events = [Event(0, kind='user_msg', text='one two three four five six seven')]
        configs = [{'id':'a', 'prompt':'one two three four five six', 'cwds':['work']}, {'id':'b', 'prompt':'other unrelated words are not the same', 'cwds':[]}]
        self.assertEqual(automations.match_detail(meta, events, configs)[0], 'a')
        configs[1]['prompt']=configs[0]['prompt']; configs[1]['cwds']=['work']
        self.assertEqual(automations.match_detail(meta, events, configs), (None, 'ambiguous'))

    def test_schedule_rules_and_unknown(self):
        rule='DTSTART:20260101T120000Z\nRRULE:FREQ=DAILY;INTERVAL=3;BYHOUR=12;BYMINUTE=0'
        self.assertTrue(automations._schedule_fit(rule, '2026-01-04T12:30:00Z'))
        self.assertFalse(automations._schedule_fit(rule, '2026-01-05T12:30:00Z'))
        self.assertIsNone(automations._schedule_fit('FREQ=MONTHLY', '2026-01-04T12:00:00Z'))

    def test_snapshot(self):
        root=self.root; src=root/'source'; src.mkdir()
        (src/'automation.toml').write_text('id = "sample"', encoding='utf-8')
        (src/'memory.md').write_text('synthetic', encoding='utf-8')
        config={'id':'sample', '_dir':src}
        self.assertEqual(automations.snapshot([config], root/'dest'),1)
        self.assertEqual(automations.snapshot([config], root/'dest'),0)
        (src/'memory.md').write_text('changed', encoding='utf-8')
        self.assertEqual(automations.snapshot([config], root/'dest'),1)

    def test_matching_threshold_cwd_schedule_and_old_snapshot(self):
        prompt = 'one two three four five six seven eight nine'
        meta = SessionMeta('synthetic', 'codex', '', 0, thread_source='automation', cwd='work',
                           started='2026-01-05T12:00:00Z')
        events = [Event(0, kind='context', text='one two three four five six seven eight'),
                  Event(1, kind='user_msg', text='request details'),
                  Event(2, kind='user_msg', text=prompt)]
        configs = [{'id': 'a', 'prompt': prompt, 'cwds': ['work'], 'rrule': 'FREQ=MONTHLY'}]
        # Four of five prompt shingles is exactly the acceptance threshold;
        # later user messages must not affect matching.
        self.assertEqual(automations.match_detail(meta, events, configs), ('a', 'matched'))
        self.assertEqual(automations.match_detail(meta, [events[1], events[2]], configs), (None, 'no-match'))
        non_automation = SessionMeta('interactive', 'codex', '', 0, thread_source='user')
        self.assertEqual(automations.match_detail(non_automation, events, configs), (None, 'no-match'))
        configs.append({'id': 'b', 'prompt': prompt, 'cwds': ['elsewhere'],
                        'rrule': 'FREQ=DAILY;BYHOUR=12;BYMINUTE=0'})
        self.assertEqual(automations.match_detail(meta, events, configs), ('a', 'matched'))
        configs[1]['cwds'] = ['work']
        self.assertEqual(automations.match_detail(meta, events, configs), ('b', 'matched'))
        configs[1]['rrule'] = 'FREQ=DAILY;BYHOUR=20;BYMINUTE=0'
        self.assertEqual(automations.match_detail(meta, events, configs), ('a', 'matched'))
        snapshots = self.root / 'historical' / 'a' / '20260101T000000000000Z'
        snapshots.mkdir(parents=True)
        (snapshots / 'automation.toml').write_text('prompt = ' + json.dumps(prompt), encoding='utf-8')
        configs[0]['prompt'] = 'entirely unrelated current automation prompt with no matching shingles'
        self.assertEqual(automations.match_detail(meta, events, configs[:1], snapshots.parents[1]), ('a', 'matched'))

    def test_schedule_evidence_ranks_fit_unknown_and_miss_in_any_order(self):
        prompt = 'one two three four five six seven eight nine'
        meta = SessionMeta('synthetic', 'codex', '', 0, thread_source='automation', cwd='work',
                           started='2026-01-05T12:00:00Z')
        events = [Event(0, kind='user_msg', text=prompt)]
        fit = {'id': 'a-fit', 'prompt': prompt, 'cwds': ['work'], 'rrule': 'FREQ=DAILY;BYHOUR=12;BYMINUTE=0'}
        unknown = {'id': 'z-unknown', 'prompt': prompt, 'cwds': ['work'], 'rrule': 'FREQ=MONTHLY'}
        miss = {'id': 'zz-miss', 'prompt': prompt, 'cwds': ['work'], 'rrule': 'FREQ=DAILY;BYHOUR=20;BYMINUTE=0'}
        cases = [([fit, unknown], 'a-fit'), ([fit, miss], 'a-fit'),
                 ([unknown, miss], 'z-unknown'), ([fit, unknown, miss], 'a-fit')]
        for configs, expected in cases:
            for order in permutations(configs):
                with self.subTest(order=[config['id'] for config in order]):
                    self.assertEqual(automations.match_detail(meta, events, order), (expected, 'matched'))

    def test_equal_schedule_evidence_remains_ambiguous_in_any_order(self):
        prompt = 'one two three four five six seven eight nine'
        meta = SessionMeta('synthetic', 'codex', '', 0, thread_source='automation', cwd='work',
                           started='2026-01-05T12:00:00Z')
        events = [Event(0, kind='user_msg', text=prompt)]
        rules = [('FREQ=DAILY;BYHOUR=12;BYMINUTE=0', 'FREQ=DAILY;BYHOUR=13;BYMINUTE=0'),
                 ('FREQ=MONTHLY', 'invalid'),
                 ('FREQ=DAILY;BYHOUR=20;BYMINUTE=0', 'FREQ=DAILY;BYHOUR=21;BYMINUTE=0')]
        for first_rule, second_rule in rules:
            configs = [{'id': 'a', 'prompt': prompt, 'cwds': ['work'], 'rrule': first_rule},
                       {'id': 'z', 'prompt': prompt, 'cwds': ['work'], 'rrule': second_rule}]
            for order in permutations(configs):
                with self.subTest(rules=(first_rule, second_rule), order=[c['id'] for c in order]):
                    self.assertEqual(automations.match_detail(meta, events, order), (None, 'ambiguous'))

    def test_prompt_and_cwd_take_precedence_over_schedule_evidence(self):
        prompt = 'one two three four five six seven eight'
        meta = SessionMeta('synthetic', 'codex', '', 0, thread_source='automation', cwd='work',
                           started='2026-01-05T12:00:00Z')
        events = [Event(0, kind='user_msg', text=prompt)]
        preferred = {'id': 'a', 'prompt': prompt, 'cwds': ['elsewhere'], 'rrule': 'FREQ=DAILY;BYHOUR=20;BYMINUTE=0'}
        other = {'id': 'z', 'prompt': prompt + ' nine', 'cwds': ['work'], 'rrule': 'FREQ=DAILY;BYHOUR=12;BYMINUTE=0'}
        for order in permutations([preferred, other]):
            self.assertEqual(automations.match_detail(meta, events, order), ('a', 'matched'))
        preferred['cwds'] = ['work']
        other['prompt'] = prompt
        other['cwds'] = ['elsewhere']
        for order in permutations([preferred, other]):
            self.assertEqual(automations.match_detail(meta, events, order), ('a', 'matched'))

    def test_sole_schedule_miss_is_eligible_only_above_prompt_threshold(self):
        prompt = 'one two three four five six seven eight nine'
        meta = SessionMeta('synthetic', 'codex', '', 0, thread_source='automation', cwd='work',
                           started='2026-01-05T12:00:00Z')
        configs = [{'id': 'miss', 'prompt': prompt, 'cwds': ['work'], 'rrule': 'FREQ=DAILY;BYHOUR=20;BYMINUTE=0'}]
        for text, expected in [(prompt, ('miss', 'matched')),
                               ('one two three four five six seven eight', ('miss', 'matched')),
                               ('one two three four five six seven', (None, 'no-match'))]:
            with self.subTest(text=text):
                events = [Event(0, kind='user_msg', text=text)]
                self.assertEqual(automations.match_detail(meta, events, configs), expected)

    def test_supported_schedule_occurrences_and_boundaries(self):
        weekly = 'DTSTART:20260105T120000Z\nRRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE;BYHOUR=12;BYMINUTE=30'
        self.assertTrue(automations._schedule_fit(weekly, '2026-01-07T11:00:00Z'))
        self.assertFalse(automations._schedule_fit(weekly, '2026-01-07T10:59:59Z'))
        self.assertFalse(automations._schedule_fit(weekly, '2026-01-14T12:30:00Z'))
        self.assertTrue(automations._schedule_fit(weekly, '2026-01-21T12:30:00Z'))
        hourly = 'DTSTART:20260101T000000Z\nRRULE:FREQ=HOURLY;INTERVAL=6;BYMINUTE=15'
        self.assertTrue(automations._schedule_fit(hourly, '2026-01-02T00:15:00Z'))
        self.assertFalse(automations._schedule_fit(hourly, '2026-01-02T03:15:00Z'))
        self.assertIsNone(automations._schedule_fit('FREQ=HOURLY;INTERVAL=6', '2026-01-02T00:15:00Z'))
        self.assertIsNone(automations._schedule_fit('FREQ=DAILY;BYMINUTE=90', '2026-01-02T00:15:00Z'))

    def test_hourly_interval_uses_anchor_hour_before_applying_minutes(self):
        prefix = 'DTSTART:20260101T003000Z\nRRULE:FREQ=HOURLY;INTERVAL=6;BYMINUTE='
        cases = [
            ('15', '2026-01-01T06:15:00Z', True),
            ('15', '2026-01-01T08:30:00Z', False),
            ('15', '2026-01-01T04:45:00Z', True),
            ('15', '2026-01-01T04:44:59.999999Z', False),
            ('15', '2026-01-01T07:45:00Z', True),
            ('15', '2026-01-01T07:45:00.000001Z', False),
            ('15', '2026-01-01T00:15:00Z', False),
            ('15', '2026-01-01T01:45:00Z', False),
            ('45', '2026-01-01T00:45:00Z', True),
            ('45', '2026-01-01T06:45:00Z', True),
            ('45', '2026-01-01T08:15:00Z', True),
            ('45', '2026-01-01T08:15:00.000001Z', False),
            ('15,45', '2026-01-01T00:45:00Z', True),
            ('15,45', '2026-01-01T08:15:00Z', True),
            ('15,45', '2026-01-01T08:16:00Z', False),
            ('15', '2026-01-01T14:00:00+05:30', False),
            ('15', '2026-01-01T10:15:00+05:30', True),
            ('15', '2026-01-01T10:14:59.999999+05:30', False),
        ]
        for minutes, started, expected in cases:
            with self.subTest(minutes=minutes, started=started):
                self.assertIs(automations._schedule_fit(prefix + minutes, started), expected)

    def test_inherited_time_fields_and_seconds_keep_inclusive_utc_boundaries(self):
        cases = [('DAILY', '2026-01-02T12:15:30+00:00'),
                 ('WEEKLY', '2026-01-08T12:15:30+00:00'),
                 ('HOURLY;INTERVAL=6', '2026-01-02T00:15:30+00:00')]
        for frequency, occurrence in cases:
            for fields in ('', ';BYHOUR=12;BYMINUTE=15' if frequency != 'HOURLY;INTERVAL=6' else ';BYMINUTE=15'):
                rule = f'DTSTART:20260101T121530Z\nRRULE:FREQ={frequency}{fields}'
                instant = datetime.fromisoformat(occurrence)
                for delta, expected in [(timedelta(minutes=-90), True),
                                        (timedelta(minutes=-90, microseconds=-1), False),
                                        (timedelta(minutes=90), True),
                                        (timedelta(minutes=90, microseconds=1), False)]:
                    started = (instant + delta).astimezone(timezone(timedelta(hours=5, minutes=30))).isoformat()
                    with self.subTest(frequency=frequency, fields=fields, started=started):
                        self.assertIs(automations._schedule_fit(rule, started), expected)
        self.assertTrue(automations._schedule_fit('DTSTART:20260101T120030Z\nRRULE:FREQ=DAILY',
                                                 '2026-01-02T13:30:15Z'))

    def test_missing_unanchored_time_fields_and_bysecond_are_unknown(self):
        cases = ['FREQ=DAILY', 'FREQ=DAILY;BYMINUTE=0', 'FREQ=DAILY;BYHOUR=12',
                 'FREQ=WEEKLY;BYDAY=MO', 'FREQ=WEEKLY;BYDAY=MO;BYMINUTE=0',
                 'FREQ=WEEKLY;BYDAY=MO;BYHOUR=12', 'FREQ=WEEKLY;BYHOUR=12;BYMINUTE=0',
                 'FREQ=HOURLY', 'FREQ=HOURLY;BYHOUR=12']
        for frequency in ('DAILY', 'WEEKLY', 'HOURLY'):
            cases.append(f'DTSTART:20260101T120030Z\nRRULE:FREQ={frequency};BYSECOND=30')
        for rule in cases:
            for started in ('2026-01-05T12:00:00Z', '2026-01-05T20:00:00Z'):
                with self.subTest(rule=rule, started=started):
                    self.assertIsNone(automations._schedule_fit(rule, started))

    def test_unanchored_explicit_times_have_zero_seconds(self):
        for frequency in ('DAILY', 'WEEKLY;BYDAY=MO', 'HOURLY'):
            rule = f'FREQ={frequency};BYHOUR=12;BYMINUTE=0'
            for started, expected in [('2026-01-05T10:30:00Z', True),
                                      ('2026-01-05T10:29:59.999999Z', False),
                                      ('2026-01-05T13:30:00Z', True),
                                      ('2026-01-05T13:30:00.000001Z', False)]:
                with self.subTest(frequency=frequency, started=started):
                    self.assertIs(automations._schedule_fit(rule, started), expected)
        self.assertTrue(automations._schedule_fit('FREQ=HOURLY;BYMINUTE=0', '2026-01-05T17:24:30Z'))

    def test_missing_time_fields_do_not_outrank_fit_or_equal_unknown(self):
        prompt = 'one two three four five six seven eight nine'
        meta = SessionMeta('synthetic', 'codex', '', 0, thread_source='automation', cwd='work',
                           started='2026-01-02T13:30:15Z')
        events = [Event(0, kind='user_msg', text=prompt)]
        def config(identifier, rule):
            return {'id': identifier, 'prompt': prompt, 'cwds': ['work'], 'rrule': rule}
        fit = config('fit', 'DTSTART:20260101T120030Z\nRRULE:FREQ=DAILY')
        miss = config('miss', 'FREQ=DAILY;BYHOUR=20;BYMINUTE=0')
        unsupported = config('unsupported', 'FREQ=MONTHLY')
        for rule in ('FREQ=DAILY', 'FREQ=DAILY;BYHOUR=12', 'FREQ=WEEKLY;BYDAY=FR;BYMINUTE=0', 'FREQ=HOURLY'):
            unknown = config('unknown', rule)
            for configs, expected in [([fit, unknown, miss], ('fit', 'matched')),
                                      ([unknown, miss], ('unknown', 'matched')),
                                      ([unknown, unsupported], (None, 'ambiguous'))]:
                for order in permutations(configs):
                    with self.subTest(rule=rule, order=[c['id'] for c in order]):
                        self.assertEqual(automations.match_detail(meta, events, order), expected)

    def test_hourly_anchor_lower_bound_seconds_midnight_and_filters(self):
        cases = [
            ('DTSTART:20260101T001530Z\nRRULE:FREQ=HOURLY;INTERVAL=6;BYMINUTE=15',
             [('2026-01-01T00:15:00Z', True), ('2026-01-01T01:45:30Z', True),
              ('2026-01-01T04:45:00Z', False), ('2026-01-01T04:45:30Z', True),
              ('2026-01-01T04:45:29Z', False)]),
            ('DTSTART:20260101T003030Z\nRRULE:FREQ=HOURLY;INTERVAL=6;BYMINUTE=15',
             [('2026-01-01T00:15:30Z', False), ('2026-01-01T01:45:30Z', False)]),
            ('DTSTART:20260101T223030Z\nRRULE:FREQ=HOURLY;INTERVAL=6;BYMINUTE=15',
             [('2026-01-01T23:45:30Z', False), ('2026-01-02T02:00:30Z', False),
              ('2026-01-02T02:45:30Z', True), ('2026-01-02T05:45:30Z', True),
              ('2026-01-02T05:45:31Z', False)]),
            ('DTSTART:20260105T003000Z\nRRULE:FREQ=HOURLY;INTERVAL=6;BYMINUTE=15;BYHOUR=6,18;BYDAY=MO,WE',
             [('2026-01-05T06:15:00Z', True), ('2026-01-05T08:30:00Z', False),
              ('2026-01-05T12:15:00Z', False), ('2026-01-06T06:15:00Z', False),
              ('2026-01-07T18:15:00Z', True)]),
        ]
        for rule, starts in cases:
            for started, expected in starts:
                with self.subTest(rule=rule, started=started):
                    self.assertIs(automations._schedule_fit(rule, started), expected)
        for rule in ('FREQ=HOURLY;INTERVAL=6;BYMINUTE=15',
                     'DTSTART:20260101T003000Z\nRRULE:FREQ=HOURLY;BYSECOND=30',
                     'DTSTART:20260101T003000Z\nRRULE:FREQ=HOURLY;BYMINUTE=60'):
            with self.subTest(unknown=rule):
                self.assertIsNone(automations._schedule_fit(rule, '2026-01-01T08:30:00Z'))

    def _transition_zone(self):
        from io import BytesIO
        from zoneinfo import ZoneInfo
        transitions = [int(datetime(2026, 3, 8, 7, tzinfo=timezone.utc).timestamp()),
                       int(datetime(2026, 11, 1, 6, tzinfo=timezone.utc).timestamp())]
        # Synthetic TZif v1 data: two transitions between -05:00 and -04:00.
        header = b'TZif\x00' + b'\x00' * 15 + struct.pack('>6l', 0, 0, 0, 2, 2, 8)
        offsets = struct.pack('>lbb', -18000, 0, 0) + struct.pack('>lbb', -14400, 1, 4)
        return ZoneInfo.from_file(BytesIO(header + struct.pack('>2l', *transitions) +
                                           b'\x01\x00' + offsets + b'STD\x00DST\x00'), key='Synthetic/Eastern')

    def _check_calendar_transition_fit(self, zone_name, second=0):
        cases = [
            # The spring gap contains no real 02:15 occurrence.
            ('20260301T003000', 2, '2026-03-08T05:45:00Z', False),
            ('20260301T003000', 2, '2026-03-08T06:15:00Z', False),
            ('20260301T003000', 2, '2026-03-08T07:15:00Z', False),
            ('20260301T003000', 2, '2026-03-08T08:45:00Z', False),
            # An ordinary 02:15 EDT occurrence retains inclusive UTC tolerance.
            ('20260301T003000', 2, '2026-03-15T04:44:59.999999Z', False),
            ('20260301T003000', 2, '2026-03-15T04:45:00Z', True),
            ('20260301T003000', 2, '2026-03-15T06:15:00Z', True),
            ('20260301T003000', 2, '2026-03-15T07:45:00Z', True),
            ('20260301T003000', 2, '2026-03-15T07:45:00.000001Z', False),
            # Repeated 01:15 uses only fold=0 (05:15 UTC), including when
            # the session itself starts during fold=1.
            ('20261025T003000', 1, '2026-11-01T03:44:59.999999Z', False),
            ('20261025T003000', 1, '2026-11-01T03:45:00Z', True),
            ('20261025T003000', 1, '2026-11-01T05:15:00Z', True),
            ('20261025T003000', 1, '2026-11-01T06:15:00Z', True),
            ('20261025T003000', 1, '2026-11-01T06:45:00Z', True),
            ('20261025T003000', 1, '2026-11-01T06:45:00.000001Z', False),
            ('20261025T003000', 1, '2026-11-01T07:15:00Z', False),
            ('20261025T003000', 1, '2026-11-01T07:45:00Z', False),
        ]
        for frequency in ('DAILY', 'WEEKLY'):
            for anchor, hour, started, expected in cases:
                anchor = anchor[:-2] + f'{second:02d}'
                started = (datetime.fromisoformat(started.replace('Z', '+00:00')) + timedelta(seconds=second)).isoformat()
                rule = (f'DTSTART;TZID={zone_name}:{anchor}\n'
                        f'RRULE:FREQ={frequency};BYHOUR={hour};BYMINUTE=15')
                if frequency == 'WEEKLY': rule += ';BYDAY=SU'
                with self.subTest(frequency=frequency, anchor=anchor, started=started):
                    self.assertIs(automations._schedule_fit(rule, started), expected)

    def test_daily_weekly_named_zone_reject_gaps_and_use_first_fold(self):
        zone = self._transition_zone()
        with patch('zoneinfo.ZoneInfo', return_value=zone) as constructor, \
                warnings.catch_warnings(record=True) as caught:
            self._check_calendar_transition_fit('Synthetic/Eastern')
            self._check_calendar_transition_fit('Synthetic/Eastern', second=30)
        self.assertTrue(constructor.called)
        self.assertTrue(all(call.args == ('Synthetic/Eastern',) for call in constructor.call_args_list))
        self.assertEqual(caught, [])

    def test_daily_weekly_fallback_reject_gaps_and_use_first_fold(self):
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        with patch('zoneinfo.ZoneInfo', side_effect=ZoneInfoNotFoundError), \
                patch('zoneinfo.ZoneInfo.from_file', side_effect=ZoneInfo.from_file), \
                patch.dict('sys.modules', {'winreg': self._eastern_registry()}), \
                warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            self._check_calendar_transition_fit('Missing/Other_Zone')
            self._check_calendar_transition_fit('Missing/Other_Zone', second=30)
        self.assertTrue(caught)
        for item in caught:
            self.assertIn('assuming US Eastern Time with DST', str(item.message))
            self.assertIn('Windows Eastern Standard Time', str(item.message))

    def test_hourly_interval_and_tolerance_remain_utc_across_dst(self):
        for second in (0, 30):
            self._check_hourly_transition_fit(second)

    def _check_hourly_transition_fit(self, second):
        zone = self._transition_zone()
        cases = [
            ('20260308T003000', [('2026-03-08T11:15:00Z', True),
                                ('2026-03-08T09:45:00Z', True),
                                ('2026-03-08T09:44:59Z', False),
                                ('2026-03-08T12:45:00Z', True),
                                ('2026-03-08T12:45:01Z', False)]),
            ('20261101T003000', [('2026-11-01T10:15:00Z', True),
                                ('2026-11-01T08:45:00Z', True),
                                ('2026-11-01T08:44:59Z', False),
                                ('2026-11-01T11:45:00Z', True),
                                ('2026-11-01T11:45:01Z', False)]),
        ]
        for anchor, starts in cases:
            anchor = anchor[:-2] + f'{second:02d}'
            rule = ('DTSTART;TZID=Synthetic/Eastern:' + anchor +
                    '\nRRULE:FREQ=HOURLY;INTERVAL=6;BYMINUTE=15')
            for started, expected in starts:
                started = (datetime.fromisoformat(started.replace('Z', '+00:00')) + timedelta(seconds=second)).isoformat()
                with self.subTest(anchor=anchor, started=started), patch('zoneinfo.ZoneInfo', return_value=zone):
                    self.assertIs(automations._schedule_fit(rule, started), expected)

        fold_rule = ('DTSTART;TZID=Synthetic/Eastern:20261101T003000'
                     '\nRRULE:FREQ=HOURLY;INTERVAL=2;BYHOUR=1;BYMINUTE=15')
        gap_rule = ('DTSTART;TZID=Synthetic/Eastern:20260308T003000'
                    '\nRRULE:FREQ=HOURLY;INTERVAL=2;BYHOUR=2;BYMINUTE=15')
        ordinary_rule = ('DTSTART;TZID=Synthetic/Eastern:20260309T003000'
                         '\nRRULE:FREQ=HOURLY;BYHOUR=2;BYMINUTE=15')
        cases = [
            (fold_rule, '2026-11-01T04:44:59Z', False),
            (fold_rule, '2026-11-01T04:45:00Z', True),
            (fold_rule, '2026-11-01T06:15:00Z', True),
            (fold_rule, '2026-11-01T07:45:00Z', True),
            (fold_rule, '2026-11-01T07:45:01Z', False),
            (gap_rule, '2026-03-08T07:15:00Z', False),
            (ordinary_rule, '2026-03-09T06:15:00Z', True),
            (ordinary_rule, '2026-03-09T07:45:01Z', False),
        ]
        for rule, started, expected in cases:
            rule = rule.replace('T003000', f'T0030{second:02d}')
            started = (datetime.fromisoformat(started.replace('Z', '+00:00')) + timedelta(seconds=second)).isoformat()
            with self.subTest(rule=rule, started=started), patch('zoneinfo.ZoneInfo', return_value=zone):
                self.assertIs(automations._schedule_fit(rule, started), expected)

    def test_hourly_interval_evidence_controls_matching_in_either_order(self):
        prompt = 'one two three four five six seven eight nine'
        events = [Event(0, kind='user_msg', text=prompt)]
        hourly = {'id': 'hourly', 'prompt': prompt, 'cwds': ['work'],
                  'rrule': 'DTSTART:20260101T003000Z\nRRULE:FREQ=HOURLY;INTERVAL=6;BYMINUTE=15'}
        unknown = {'id': 'unknown', 'prompt': prompt, 'cwds': ['work'], 'rrule': 'FREQ=MONTHLY'}
        for started, expected in [('2026-01-01T08:30:00Z', 'unknown'),
                                  ('2026-01-01T04:45:00Z', 'hourly'),
                                  ('2026-01-01T07:45:00Z', 'hourly')]:
            meta = SessionMeta('synthetic', 'codex', '', 0, thread_source='automation',
                               cwd='work', started=started)
            for order in permutations([hourly, unknown]):
                with self.subTest(started=started, order=[c['id'] for c in order]):
                    self.assertEqual(automations.match_detail(meta, events, order), (expected, 'matched'))

    def _eastern_registry(self):
        # Hand-written Windows TZI fixtures, independent of the host's database.
        def tzi(start_month, start_week, end_month, end_week):
            return struct.pack('<lll16H', 300, 0, -60,
                               0, end_month, 0, end_week, 2, 0, 0, 0,
                               0, start_month, 0, start_week, 2, 0, 0, 0)
        values = {'FirstEntry': 2006, 'LastEntry': 2026,
                  '2006': tzi(4, 1, 10, 5),
                  **{str(year): tzi(3, 2, 11, 1) for year in range(2007, 2027)}}
        return SimpleNamespace(HKEY_LOCAL_MACHINE=0,
                               OpenKey=lambda *args: nullcontext('synthetic'),
                               QueryValueEx=lambda key, name: (values[name], 0))

    def test_windows_eastern_rules_preserve_offsets_folds_gaps_and_history(self):
        with patch.dict('sys.modules', {'winreg': self._eastern_registry()}):
            zone, description = automations._windows_eastern_zone(2006, 2026)
        self.assertIn('2006-2026', description)
        self.assertNotIn('extrapolated', description)
        for stamp, offset, fold in [('2026-01-01T12:00:00', -5, 0),
                                    ('2026-07-01T12:00:00', -4, 0),
                                    ('2026-03-08T06:59:59', -5, 0),
                                    ('2026-03-08T07:00:00', -4, 0),
                                    ('2026-11-01T05:15:00', -4, 0),
                                    ('2026-11-01T06:15:00', -5, 1),
                                    ('2006-03-20T12:00:00', -5, 0),
                                    ('2006-04-03T12:00:00', -4, 0)]:
            with self.subTest(stamp=stamp):
                local = datetime.fromisoformat(stamp).replace(tzinfo=timezone.utc).astimezone(zone)
                self.assertEqual(local.utcoffset(), timedelta(hours=offset))
                self.assertEqual(local.fold, fold)
        gap = datetime(2026, 3, 8, 2, 15, tzinfo=zone)
        self.assertNotEqual(gap.astimezone(timezone.utc).astimezone(zone).hour, 2)

    def test_missing_named_zone_uses_windows_eastern_with_visible_assumption(self):
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        rule = ('DTSTART;TZID=Missing/Other_Zone:20261101T003000'
                '\nRRULE:FREQ=HOURLY;INTERVAL=2;BYHOUR=1;BYMINUTE=15')
        with patch('zoneinfo.ZoneInfo', side_effect=ZoneInfoNotFoundError), \
                patch.dict('sys.modules', {'winreg': self._eastern_registry()}), \
                warnings.catch_warnings(record=True) as caught:
            # Patch only the constructor, preserving the real from_file loader.
            with patch('zoneinfo.ZoneInfo.from_file', side_effect=ZoneInfo.from_file):
                self.assertTrue(automations._schedule_fit(rule, '2026-11-01T06:15:00Z'))
                self.assertFalse(automations._schedule_fit(rule, '2026-11-01T07:45:01Z'))
                gap_rule = rule.replace('20261101', '20260308').replace('BYHOUR=1', 'BYHOUR=2')
                self.assertFalse(automations._schedule_fit(gap_rule, '2026-03-08T07:15:00Z'))
        messages = '\n'.join(str(item.message) for item in caught)
        self.assertIn('Missing/Other_Zone', messages)
        self.assertIn('assuming US Eastern Time with DST', messages)
        self.assertIn('Windows Eastern Standard Time', messages)
        self.assertIn('does not confirm fit', messages)

    def test_missing_zone_prefers_available_new_york_without_windows(self):
        from zoneinfo import ZoneInfoNotFoundError
        eastern = timezone(timedelta(hours=-4))
        with patch('zoneinfo.ZoneInfo', side_effect=[ZoneInfoNotFoundError, eastern]) as constructor, \
                patch.object(automations, '_windows_eastern_zone') as windows, \
                self.assertWarnsRegex(UserWarning, 'Missing/Non_Eastern.*America/New_York'):
            zone = automations._schedule_zone('Missing/Non_Eastern', 2026, 2026)
        self.assertIs(zone, eastern)
        self.assertEqual(constructor.call_args_list[0].args, ('Missing/Non_Eastern',))
        windows.assert_not_called()

    def test_existing_named_zone_is_unaltered_and_silent(self):
        eastern = timezone(timedelta(hours=-5))
        with patch('zoneinfo.ZoneInfo', return_value=eastern) as constructor, \
                patch.object(automations, '_windows_eastern_zone') as windows, \
                warnings.catch_warnings(record=True) as caught:
            self.assertIs(automations._schedule_zone('Available/Zone', 2026, 2026), eastern)
        constructor.assert_called_once_with('Available/Zone')
        windows.assert_not_called()
        self.assertEqual(caught, [])

    def test_cli_fallback_warning_is_on_stderr_and_assumed_fit_affects_matching(self):
        from zoneinfo import ZoneInfoNotFoundError
        with patch.dict('sys.modules', {'winreg': self._eastern_registry()}):
            zone, source = automations._windows_eastern_zone(2025, 2027)
        root = self.root / 'fallback-cli'
        root.mkdir()
        (root / 'automation.toml').write_text('id = "eastern"', encoding='utf-8')
        prompt = 'one two three four five six seven eight nine'
        rule = 'DTSTART;TZID=Missing/Other_Zone:20260101T120000\nRRULE:FREQ=DAILY;BYHOUR=12'
        configs = [{'id': 'eastern', 'prompt': prompt, 'rrule': rule, '_dir': root},
                   {'id': 'unknown', 'prompt': prompt, 'rrule': 'FREQ=MONTHLY', '_dir': root}]
        meta = SessionMeta('synthetic', 'codex', 'synthetic', 0, thread_source='automation',
                           started='2026-01-02T17:00:00Z')
        events = [Event(0, kind='user_msg', text=prompt)]
        stdout, stderr = StringIO(), StringIO()
        with patch.object(automations, 'configurations', return_value=configs), \
                patch.object(automations, 'discover_sessions', return_value=['synthetic']), \
                patch.object(automations, 'read_session', side_effect=lambda path: iter([meta, *events])), \
                patch('zoneinfo.ZoneInfo', side_effect=ZoneInfoNotFoundError), \
                patch.object(automations, '_windows_eastern_zone', return_value=(zone, source)), \
                warnings.catch_warnings(), redirect_stdout(stdout), redirect_stderr(stderr):
            warnings.simplefilter('always')
            self.assertEqual(main(['automations', 'runs', 'eastern', '--json', '--out', str(root / 'report')]), 0)
        self.assertEqual(json.loads(stdout.getvalue())[0]['automation_id'], 'eastern')
        self.assertIn('Missing/Other_Zone', stderr.getvalue())
        self.assertIn('assuming US Eastern Time with DST', stderr.getvalue())
        self.assertIn('may change matching', stderr.getvalue())
        self.assertEqual(configs[0]['rrule'], rule)

    def test_unavailable_fallback_remains_unknown_and_redacts_warning(self):
        from zoneinfo import ZoneInfoNotFoundError
        secret = 'sk-' + 'a' * 48
        rule = f'DTSTART;TZID={secret}:20260101T003000\nRRULE:FREQ=HOURLY'
        for error in (ImportError('no registry'), OSError('no rules'), ValueError('bad rules')):
            with self.subTest(error=type(error)), \
                    patch('zoneinfo.ZoneInfo', side_effect=ZoneInfoNotFoundError), \
                    patch.object(automations, '_windows_eastern_zone', side_effect=error), \
                    warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter('always')
                self.assertIsNone(automations._schedule_fit(rule, '2026-01-01T05:00:00Z'))
            message = str(caught[0].message)
            self.assertNotIn(secret, message)
            self.assertIn('[REDACTED:', message)
            self.assertIn('schedule fit is unknown', message)
            self.assertNotIn(str(error), message)

    def test_windows_edge_year_assumption_is_disclosed_and_bad_rules_fail_closed(self):
        with patch.dict('sys.modules', {'winreg': self._eastern_registry()}):
            _, description = automations._windows_eastern_zone(2026, 2028)
            self.assertIn('edge-year rules extrapolated', description)
        registry = self._eastern_registry()
        registry.QueryValueEx = lambda key, name: (2026 if name in ('FirstEntry', 'LastEntry') else b'bad', 0)
        with patch.dict('sys.modules', {'winreg': registry}):
            with self.assertRaises(struct.error):
                automations._windows_eastern_zone(2026, 2026)

    def test_snapshot_cli_copies_every_file_only_after_hash_change(self):
        root = self.root / 'snapshot-cli'
        source = root / 'automations' / 'sample'
        source.mkdir(parents=True)
        originals = {'automation.toml': b'id = "sample"\nprompt = "synthetic prompt"\n',
                     'memory.md': b'synthetic memory', 'memory.backup.1.md': b'synthetic backup',
                     'memory.archive-old.md': b'synthetic archive'}
        for name, content in originals.items():
            (source / name).write_bytes(content)
        destination = root / 'snapshots'
        arguments = ['automations', 'snapshot', '--id', 'sample', '--dest', str(destination), '--json']
        def run():
            output = StringIO()
            with chdir(root), patch.object(automations, 'home', return_value=source.parent), redirect_stdout(output):
                self.assertEqual(main(arguments), 0)
            return json.loads(output.getvalue())['snapshots_written']
        self.assertEqual(run(), 1)
        first = next((destination / 'sample').iterdir())
        self.assertEqual({p.name: p.read_bytes() for p in first.iterdir()}, originals)
        self.assertEqual(run(), 0)
        self.assertEqual(len(list((destination / 'sample').iterdir())), 1)
        (source / 'memory.md').write_bytes(b'different memory')  # same byte length
        self.assertEqual(run(), 1)
        self.assertEqual(run(), 0)
        self.assertEqual(len(list((destination / 'sample').iterdir())), 2)
        self.assertEqual({p.name: p.read_bytes() for p in first.iterdir()}, originals)
        self.assertEqual((source / 'automation.toml').read_bytes(), originals['automation.toml'])

    def test_explicit_memory_output_is_redacted_and_capped(self):
        root = self.root / 'long-private'
        source = root / 'sample'
        source.mkdir(parents=True)
        secret = 'sk-' + 'a' * 48
        (source / 'automation.toml').write_text('id = "sample"\nprompt = ' + json.dumps(secret + ' prompt words' * 3000), encoding='utf-8')
        (source / 'memory.md').write_text(secret + ' memory words' * 3000, encoding='utf-8')
        output = root / 'report'
        stdout = StringIO()
        with patch.object(automations, 'home', return_value=root), redirect_stdout(stdout):
            self.assertEqual(main(['automations', 'show', 'sample', '--memory', '--json', '--out', str(output)]), 0)
        data = json.loads((output / 'automations.json').read_text(encoding='utf-8'))
        self.assertLessEqual(est_tokens(data['prompt']), 2000)
        self.assertLessEqual(est_tokens(data['memory']), 2000)
        for text in [stdout.getvalue(), *[p.read_text(encoding='utf-8') for p in output.iterdir()]]:
            self.assertNotIn(secret, text)
            self.assertIn('[REDACTED:', text)
        self.assertLessEqual(est_tokens((output / 'automations.md').read_text(encoding='utf-8')), 4000)

    def test_show_hides_prompt_and_memory(self):
        root=self.root; src=root/'synthetic'; src.mkdir()
        (src/'automation.toml').write_text('id = "synthetic"\nprompt = "private prompt phrase"\n', encoding='utf-8')
        (src/'memory.md').write_text('private memory phrase', encoding='utf-8')
        out=root/'report'; stdout=StringIO()
        args=Namespace(action='show', id='synthetic', memory=False, out=out, json=False)
        with patch.object(automations, 'home', return_value=root), redirect_stdout(stdout):
            self.assertEqual(automations.run(args), 0)
        for text in (stdout.getvalue(), (out/'automations.md').read_text(encoding='utf-8'), (out/'automations.json').read_text(encoding='utf-8')):
            self.assertNotIn('private prompt phrase', text)
            self.assertNotIn('private memory phrase', text)
        args.memory=True
        with patch.object(automations, 'home', return_value=root), redirect_stdout(StringIO()):
            self.assertEqual(automations.run(args), 0)
        shown=(out/'automations.md').read_text(encoding='utf-8')
        self.assertIn('private prompt phrase', shown)
        self.assertIn('private memory phrase', shown)
