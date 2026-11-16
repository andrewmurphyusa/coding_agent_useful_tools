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

"""Synthetic end-to-end checks for automation regression analysis."""
import unittest
from datetime import datetime, timedelta, timezone

from agentlog import compress, diff, loops, trend
from agentlog.core.tokens import est_tokens
from agentlog.readers.base import Event, SessionMeta


def make_run(number):
    started = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=number)
    degraded = number >= 4
    meta = SessionMeta(
        f"synthetic-run-{number}", "codex", f"synthetic-{number}.jsonl", 40_000,
        cli_version="2.0" if number >= 4 else "1.0", thread_source="automation",
        started=started.isoformat().replace("+00:00", "Z"),
        ended=(started + timedelta(minutes=10 if degraded else 2)).isoformat().replace("+00:00", "Z"),
        context_window=100_000,
    )
    events = []

    def add(kind, text="", **fields):
        events.append(Event(len(events), kind=kind, text=text, **fields))

    add("user_msg", "Run the synthetic scheduled task", turn=1)
    memory_size = 100 if not degraded else 100 * (number - 2)
    add("tool_call", 'read_file("C:/synthetic/automation/memory.md")',
        tool="read_file", call_id="memory", turn=1)
    add("tool_result", "memory " + "x" * memory_size, call_id="memory", turn=1,
        est_tokens=(memory_size + 7) // 4)
    if degraded:
        add("compaction", "synthetic summary", turn=1)
    repeats = 1 if not degraded else 10
    for _ in range(repeats):
        call_id = f"retry-{len(events)}"
        add("tool_call", "python check.py", tool="shell", call_id=call_id, turn=1)
        add("tool_result", "synthetic command failed", call_id=call_id,
            exit_code=1, is_error=True, turn=1, est_tokens=8)
    if degraded:
        add("compaction", "synthetic summary", turn=1)
    add("token_usage", usage=None, turn=1, est_tokens=10)
    if degraded:
        add("token_usage", usage=None, turn=1, est_tokens=20)
    add("task_end", "done", turn=1)
    return meta, events


class AutomationRegressionIntegration(unittest.TestCase):
    def test_six_run_degradation_walkthrough(self):
        runs = [make_run(n) for n in range(1, 7)]
        rows = []
        for meta, events in runs:
            rows.append(trend.analyze(meta, events))
        trend.mark(rows)
        self.assertEqual([row["id"] for row in rows if row["first_outlier"]], ["synthetic-run-4"])

        good_meta, good_events = runs[2]
        bad_meta, bad_events = runs[3]
        comparison = diff.compare(good_meta, good_events, bad_meta, bad_events)
        markdown = diff._markdown(comparison)
        self.assertEqual(comparison["meta"]["cli_version"], ["1.0", "2.0"])
        self.assertIn("memory.md (memory file)", markdown)
        self.assertIn("(memory file): 26 -> 51", markdown)

        _, retry_events = runs[5]
        compressed = compress.compress_session(runs[5][0], retry_events)
        found = loops.analyze(runs[5][0], retry_events, compressed)
        self.assertTrue(any(item["rule"] == "R1" for item in found["findings"]))
        rendered = compress.render_compress(compressed)
        ratio = est_tokens(rendered) / compressed["stats"]["raw_tokens"]
        self.assertLessEqual(ratio, 0.25)


if __name__ == "__main__":
    unittest.main()
