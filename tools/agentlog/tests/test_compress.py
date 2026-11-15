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

"""Synthetic repetition, reconstruction, bounds and performance regressions."""
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import asdict, replace
import io
import json
import os
from pathlib import Path
import random
import tempfile
import time
import tracemalloc
import unittest
from unittest.mock import patch

from agentlog.cli import main
from agentlog.compress import compress_session, preview, render_compress, _bounded_report
from agentlog.core.tokens import est_tokens
from agentlog.expand import expand_events
from agentlog.readers.base import Event, SessionMeta, Usage
from agentlog.sequences import find_loops, find_sequences


def event(seq, text, kind="tool_call", **kw):
    return Event(seq, text=text, kind=kind, est_tokens=est_tokens(text),
                 raw_bytes=len(text.encode("utf-8")) + 250, **kw)


def compress(events, **kw):
    meta = SessionMeta("synthetic-compress", "generic", "synthetic.jsonl",
                       sum(e.raw_bytes for e in events))
    return compress_session(meta, iter(events), **kw)


def loop_events(repetitions=14):
    events = []
    bodies = ["Run the tests and inspect the reported mismatch. " * 30,
              "test command returned failure in module maple\n" * 40,
              "I will adjust the failing comparison and retry. " * 30,
              "apply patch to maple: change the expected result\n" * 30]
    for iteration in range(repetitions):
        for offset, text in enumerate(bodies):
            events.append(event(len(events), text, ["tool_call", "tool_result", "assistant_msg", "tool_call"][offset],
                                tool=["shell", "shell", None, "apply_patch"][offset],
                                exit_code=1 if offset == 1 else None,
                                is_error=offset == 1, ts=f"2026-01-01T01:{iteration:02}:00Z"))
    return events


class CompressionTests(unittest.TestCase):
    def test_timestamps_mask_to_exact_repeat_and_values_survive(self):
        events = [event(n, f"read at 2026-01-01T01:{n:02}:00Z\n" + "file body\n" * 8, "tool_result") for n in range(3)]
        data = compress(events, lossless=True)
        self.assertEqual(data["stats"]["blocks"], 1)
        self.assertEqual(data["stats"]["exact_repeats"], 2)
        self.assertEqual(list(expand_events(json.loads(json.dumps(data)))), events)
        self.assertNotEqual(data["occurrences"][0]["units"][0]["values"], data["occurrences"][1]["units"][0]["values"])

    def test_growing_log_has_near_plus_diff(self):
        text = "".join(f"module {chr(97 + n)} reports a healthy result for component tree\n" for n in range(24))
        events = [event(0, text, "tool_result"), event(1, text + "new component birch reports healthy\n", "tool_result")]
        data = compress(events, chunk_lines=100, lossless=True)
        unit = data["occurrences"][1]["units"][0]
        self.assertEqual(unit["repeat_kind"], "near")
        self.assertTrue(any(line.startswith("+new component") for line in unit["diff"]))
        self.assertEqual(list(expand_events(json.loads(json.dumps(data)))), events)
        # Later exact matches reuse the shared variant definition.
        again = compress(events + [replace(events[1], seq=2)], chunk_lines=100, lossless=True)
        self.assertEqual(again["occurrences"][2]["units"][0]["repeat_kind"], "exact")
        self.assertEqual(again["occurrences"][2]["units"][0]["block"], "V1")
        self.assertEqual(again["variants"]["V1"]["parent"], "B1")
        self.assertNotIn("masked_text", again["occurrences"][2]["units"][0])
        self.assertEqual(list(expand_events(json.loads(json.dumps(again))))[-1].text, events[1].text)

    @staticmethod
    def variant_base():
        return "".join(f"component {chr(97 + n // 26)}{chr(97 + n % 26)} reports a healthy maple result\n"
                       for n in range(80))

    def test_explicit_variant_ancestry_two_structures_and_exact_reuse(self):
        base = self.variant_base()
        branch = "additional maple branch is healthy\n"
        extension = "additional birch branch is healthy\n"
        leaf = "additional elm branch is healthy\n"
        for final, parent in ((base + branch + leaf, "V1"), (base + leaf, "B1")):
            with self.subTest(parent=parent):
                bodies = [base, base + branch, base + branch, base + branch + extension, final, final]
                events = [event(n, text + f"checked at 2026-01-01T01:{n:02}:00Z\n", "tool_result")
                          for n, text in enumerate(bodies)]
                data = compress(events, lossless=True, chunk_lines=200)
                self.assertEqual(list(data["blocks"]), ["B1"])
                self.assertEqual(list(data["variants"]), ["V1", "V2", "V3"])
                self.assertEqual([data["variants"][ref]["parent"] for ref in data["variants"]],
                                 ["B1", "V1", parent])
                units = [row["units"][0] for row in data["occurrences"]]
                self.assertEqual([u["block"] for u in units], ["B1", "V1", "V1", "V2", "V3", "V3"])
                self.assertEqual([u["repeat_kind"] for u in units], ["new", "near", "exact", "near", "near", "exact"])
                self.assertTrue(all("masked_text" not in u for u in units))
                self.assertEqual(data["stats"]["near_repeats"], 3)
                self.assertEqual(data["stats"]["exact_repeats"], 2)
                self.assertEqual(data["variants"]["V1"]["occurrences"], [1, 2])
                self.assertEqual(list(expand_events(json.loads(json.dumps(data)))), events)
                self.assertEqual(data, compress(events, lossless=True, chunk_lines=200))
                text = render_compress(data)
                self.assertIn("V1", text)
                self.assertIn("similarity parent", text)
                self.assertIn("V1 exact", text)

    def test_parent_tie_uses_creation_order_across_blocks_and_variants(self):
        # All three candidate line sets have equal overlap with the final
        # body. Force every signature into the candidate bucket to isolate
        # exact scoring and creation order from probabilistic LSH discovery.
        base = self.variant_base()
        bodies = [base + "maple branch\n", base + "birch branch\n", base + "elm branch\n"]
        with patch("agentlog.compress.MinHash") as hasher:
            hasher.return_value.return_value = (1,) * 128
            data = compress([event(n, text, "tool_result") for n, text in enumerate(bodies)],
                            chunk_lines=200, lossless=True)
        self.assertEqual(data["variants"]["V2"]["parent"], "B1")
        # A B2 created after V1 must lose a tied score to V1. Create B2
        # using the existing diff eligibility gate, then offer a candidate
        # that is close to both, with the exact score explicitly tied.
        from agentlog.compress import _Blocks
        dictionary = _Blocks(.85, 0, 0, True)
        with patch("agentlog.compress.MinHash.__call__", return_value=(1,) * 128):
            dictionary.add(event(0, ""), bodies[0], 0, 0)
            dictionary.add(event(1, ""), bodies[1], 0, 1)
            with patch("agentlog.compress.jaccard", return_value=0):
                dictionary.add(event(2, ""), bodies[2], 0, 2)
            with patch("agentlog.compress.jaccard", side_effect=lambda a, b: 0 if b == dictionary.sets["B1"] else 1):
                unit = dictionary.add(event(3, ""), base + "oak branch\n", 0, 3)
        self.assertEqual(dictionary.variants[unit["block"]]["parent"], "V1")
        self.assertLess(dictionary.variants["V1"]["creation_order"], dictionary.blocks["B2"]["creation_order"])

    def test_exact_matches_verify_body_equality_on_hash_collision(self):
        with patch("agentlog.compress.hashlib.sha1") as digest:
            digest.return_value.digest.return_value = b"synthetic collision"
            data = compress([event(n, text) for n, text in enumerate(("maple", "birch", "maple", "birch"))],
                            min_block_tokens=100, lossless=True)
        self.assertEqual(data["stats"]["blocks"], 2)
        self.assertEqual([row["units"][0]["block"] for row in data["occurrences"]], ["B1", "B2", "B1", "B2"])
        self.assertEqual(data["stats"]["exact_repeats"], 2)

    def test_near_loops_use_explicit_sibling_parents_and_exact_reuse(self):
        base = self.variant_base()
        texts = [base] + [base + name + " branch\n" for name in ("maple", "birch", "elm", "maple", "birch", "elm")]
        data = compress([event(n, text, "tool_result") for n, text in enumerate(texts)], chunk_lines=200)
        self.assertEqual(len(data["loops"]), 1)
        self.assertEqual((data["loops"][0]["start_seq"], data["loops"][0]["period"], data["loops"][0]["repetitions"]), (1, 1, 6))
        symbols = [("tool_result", "shell", (ref,), (kind,), (parent,)) for ref, kind, parent in
                   (("V1", "near", "B1"), ("V2", "near", "B1"), ("V1", "exact", "B1"))]
        self.assertEqual(find_loops(symbols)[0]["repetitions"], 3)
        different = symbols[:2] + [("tool_result", "shell", ("V3",), ("near",), ("V1",))]
        self.assertEqual(find_loops(different), [])
        self.assertEqual(find_loops([(s[0], s[1], s[2], s[3]) for s in symbols]), [])

    def test_variant_graph_validation_rejects_missing_forward_and_wrong_parents(self):
        base = self.variant_base()
        data = compress([event(0, base, "tool_result"), event(1, base + "new branch\n", "tool_result")],
                        lossless=True, chunk_lines=200)
        for parent in ("V1", "V999", "B999"):
            broken = json.loads(json.dumps(data))
            broken["variants"]["V1"]["parent"] = parent
            with self.subTest(parent=parent), self.assertRaisesRegex(ValueError, "earlier body"):
                list(expand_events(broken))
        broken = json.loads(json.dumps(data))
        broken["occurrences"][1]["units"][0]["parent"] = "V1"
        with self.assertRaisesRegex(ValueError, "does not match"):
            list(expand_events(broken))

    def test_large_repeated_exact_and_variant_bodies_are_shared(self):
        lines = [(f"maple branch {chr(97 + n)} ").ljust(799, "q") + "\n" for n in range(8)]
        base = "".join(lines * 20)
        self.assertEqual(len(base), 128000)
        peaks = {}
        for variant in (False, True):
            for count in (10, 100):
                tracemalloc.start()
                meta = SessionMeta("synthetic-memory", "generic", "synthetic.jsonl", count * len(base))
                def events():
                    for n in range(count):
                        # A fresh input object on every iteration must not
                        # become a retained occurrence body.
                        text = (base + "new healthy branch\n" if variant and n else base).encode().decode()
                        yield event(n, text, "tool_result")
                data = compress_session(meta, events(), lossless=True, chunk_lines=1000, body_cap=0)
                _, peaks[variant, count] = tracemalloc.get_traced_memory()
                tracemalloc.stop()
                self.assertEqual(len(data["blocks"]), 1)
                self.assertEqual(len(data["variants"]), int(variant))
                self.assertTrue(all("masked_text" not in unit for row in data["occurrences"] for unit in row["units"]))
                bodies = [entry["masked_text"] for entry in [*data["blocks"].values(), *data["variants"].values()]]
                self.assertEqual(len(bodies), len(set(bodies)))
                restored = expand_events(json.loads(json.dumps(data)))
                for original, expanded in zip(events(), restored, strict=True):
                    self.assertEqual(original, expanded)
        print("synthetic shared bodies: 128000 bytes; traced peaks " + ", ".join(
            f"{'variant' if variant else 'exact'} {count}={peaks[variant, count]} bytes"
            for variant in (False, True) for count in (10, 100)))

    def test_distinct_variants_and_volatile_data_roundtrip_fifty_sessions(self):
        rng = random.Random(8128)
        base = self.variant_base()
        for session in range(50):
            texts = [base]
            for n in range(8):
                texts.append(base + "".join(rng.choice(("maple", "birch", "elm", "oak")) + " branch\n"
                                             for _ in range(rng.randint(1, 3))))
            events = [event(n, text + f"checked at 2026-01-01T01:{n:02}:00Z\n", "tool_result", turn=n // 2)
                      for n, text in enumerate(texts)]
            data = compress(events, lossless=True, chunk_lines=200)
            with self.subTest(session=session):
                self.assertGreater(len(data["variants"]), 0)
                self.assertEqual(list(expand_events(json.loads(json.dumps(data)))), events)

    def test_distinct_large_variant_bodies_and_values_are_retained_losslessly(self):
        base = "".join(((f"maple branch {chr(97 + n)} ").ljust(799, "q") + "\n")
                       for n in range(8)) * 20
        texts = [base + "".join(f"new healthy {chr(97 + offset)} branch\n" for offset in range(n))
                 + f"checked at 2026-01-01T01:{n:02}:00Z\n" for n in range(12)]
        events = [event(n, text, "tool_result") for n, text in enumerate(texts)]
        tracemalloc.start()
        data = compress(events, lossless=True, chunk_lines=1000, body_cap=0)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        self.assertEqual(len(data["blocks"]) + len(data["variants"]), len(events))
        self.assertGreater(len(data["variants"]), 0)
        self.assertTrue(all("masked_text" not in row["units"][0] for row in data["occurrences"]))
        self.assertEqual(list(expand_events(json.loads(json.dumps(data)))), events)
        values = [row["units"][0]["values"] for row in data["occurrences"]]
        self.assertEqual(len({tuple(value) for value in values}), 12)
        print(f"synthetic distinct bodies: 12 inputs, {sum(map(len, texts))} chars; traced peak {peak} bytes")

    def test_large_line_diff_rejected_even_with_similar_shingles(self):
        text = " ".join(chr(97 + n) + "tree" for n in range(24))
        data = compress([event(0, text * 4, "assistant_msg"), event(1, text * 4 + " extra", "assistant_msg")])
        self.assertEqual(data["stats"]["blocks"], 2)

    def test_four_step_loop_outcome_and_ratio(self):
        data = compress(loop_events())
        self.assertEqual(len(data["loops"]), 1)
        loop = data["loops"][0]
        self.assertEqual((loop["period"], loop["repetitions"]), (4, 14))
        self.assertTrue(loop["no_change_in_outcome"])
        rendered = _bounded_report("compress", data, render_compress(data), 8000)
        self.assertIn("no change in outcome across 14 iterations", rendered)
        self.assertIn("iters 1–14: identical", rendered)
        self.assertLessEqual(est_tokens(rendered) / data["stats"]["raw_tokens"], .25)

    def test_outcome_change_is_not_reported_unchanged(self):
        events = loop_events()
        events[-3] = replace(events[-3], exit_code=0, is_error=False)
        data = compress(events)
        self.assertFalse(data["loops"][0]["no_change_in_outcome"])

    def test_period_one_and_bookkeeping_does_not_break_loop(self):
        events = []
        for n in range(20):
            events.append(event(len(events), "run command maple", tool="shell"))
            events.append(event(len(events), "usage", "token_usage", usage=Usage(total=n)))
            events.append(event(len(events), "settings", "settings"))
        data = compress(events)
        self.assertEqual((data["loops"][0]["period"], data["loops"][0]["repetitions"]), (1, 20))
        self.assertEqual(data["stats"]["blocks"], 1)
        self.assertEqual(data["loops"][0]["end_seq"], 57)

    def test_nonadjacent_ngram_annotation_and_maximality(self):
        names = "abXabYabZ"
        symbols = [(name, None, None, None) for name in names]
        patterns = find_sequences(symbols)
        self.assertEqual(len(patterns), 1)
        self.assertEqual(patterns[0]["occurrences"], [0, 3, 6])
        events = [event(n, name, "assistant_msg") for n, name in enumerate(names)]
        self.assertIn("[S1 occ 3/3]", render_compress(compress(events)))
        long_symbols = [(name, None, None, None) for name in "abcXabcYabcZ"]
        self.assertEqual([p["length"] for p in find_sequences(long_symbols)], [3])

    def test_chunk_level_repeat_inside_larger_outputs(self):
        common = "maple file line\n" * 40
        events = [event(0, "first preamble\n" * 40 + common + "tail one\n", "tool_result"),
                  event(1, "second preamble\n" * 40 + common + "tail two\n", "tool_result")]
        data = compress(events, lossless=True)
        self.assertEqual(data["occurrences"][1]["units"][1]["repeat_kind"], "exact")
        self.assertEqual(data["occurrences"][1]["units"][1]["block"], data["occurrences"][0]["units"][1]["block"])
        self.assertEqual(list(expand_events(json.loads(json.dumps(data)))), events)

    def test_random_lossless_roundtrip_fifty_sessions(self):
        rng = random.Random(7331)
        alphabet = ["maple", "birch", "elm", "<NUM>", "π", "literal <TS>", "", "line\r\nend\n"]
        for session in range(50):
            events = []
            for seq in range(rng.randrange(1, 35)):
                text = " ".join(rng.choice(alphabet) for _ in range(rng.randrange(12)))
                text += rng.choice(["", " 1234", " 1.25", " 2026-01-01T01:02:03Z", " /tmp/synthetic.txt", " :12:4"])
                kind = rng.choice(["tool_call", "tool_result", "assistant_msg", "settings", "other", "token_usage", "compaction", "error"])
                events.append(event(seq * 2, text, kind, turn=seq // 4, sidechain=seq % 2 == 0,
                                    usage=Usage(input=seq, total=seq * 2) if kind == "token_usage" else None))
            with self.subTest(session=session):
                data = json.loads(json.dumps(compress(events, lossless=True, chunk_lines=3)))
                self.assertEqual(list(expand_events(data)), events)

    def test_no_repetition_ratio_and_cap(self):
        rng = random.Random(41)
        events = [event(n, "".join(rng.choice("abcdefghijklmnopqrstuvwxyz ") for _ in range(5000)), "assistant_msg", turn=n)
                  for n in range(20)]
        data = compress(events)
        self.assertEqual(data["stats"]["blocks"], len(events))
        view = _bounded_report("compress", data, render_compress(data), 8000)
        self.assertLessEqual(est_tokens(view) / data["stats"]["raw_tokens"], .6)
        for cap in (80, 400, 8000):
            text = _bounded_report("compress", data, render_compress(data, max_tokens=cap), cap)
            if est_tokens(text) > cap:
                self.assertIn("Mandatory structure exceeds", text)
                self.assertNotIn("Repetition cost", text)
            else:
                self.assertLessEqual(est_tokens(text), cap)
            self.assertIn(f"this_view_tokens {est_tokens(text)}", text)
            for seq in range(len(events)):
                self.assertIn(f"E{seq} ⟦", text)
                self.assertIn(f"turn {seq}", text)

    def test_mandatory_overflow_keeps_late_structure_and_omits_optional_detail(self):
        events = [event(n, f"unique text {chr(97 + n)} " * 200, "assistant_msg", turn=n) for n in range(20)]
        events += [event(20, "", "token_usage", turn=20, usage=Usage(context_used=1234)),
                   event(21, "compaction optional body", "compaction", turn=20),
                   event(22, "", "token_usage", turn=20, usage=Usage(context_used=234)),
                   event(23, "late error optional body", "error", turn=21, is_error=True),
                   event(24, "bookkeeping optional body", "settings", turn=22),
                   event(25, "other optional body", "other", turn=22)]
        data = compress(events)
        rendered = _bounded_report("compress", data, render_compress(data, max_tokens=80), 80)
        actual = est_tokens(rendered)
        self.assertGreater(actual, 80)
        self.assertLessEqual(actual, 20000)
        self.assertIn(f"target 80, actual {actual} estimated tokens", rendered)
        self.assertIn(f"this_view_tokens {actual}", rendered)
        self.assertNotIn("optional body", rendered)
        self.assertNotIn("Repetition cost", rendered)
        for seq in range(20):
            self.assertIn(f"E{seq} ", rendered)
            self.assertIn(f"turn {seq}", rendered)
        for marker in ("E20 token_usage", "compaction context 1234 → 234", "error ERROR", "E24 settings", "E25 other", "turn 22"):
            self.assertIn(marker, rendered)

    def test_loop_positions_bookkeeping_errors_and_turns_survive_overflow(self):
        events = []
        for n in range(8):
            events += [event(n * 10, "repeat maple", tool="shell", turn=n),
                       event(n * 10 + 2, "usage", "token_usage", turn=n),
                       event(n * 10 + 4, "settings", "settings", turn=n),
                       event(n * 10 + 6, "failure", "tool_result", turn=n, exit_code=1),
                       event(n * 10 + 8, "other", "other", turn=n)]
        data = compress(events)
        self.assertTrue(data["loops"])
        text = render_compress(data, max_tokens=50)
        for n in range(8):
            for offset in (0, 2, 4, 6, 8):
                self.assertRegex(text, rf"\bE{n * 10 + offset}\b")
            self.assertIn(f"turn {n} ── (E{n * 10})", text)
            self.assertRegex(text, rf"E{n * 10 + 6} [^\n]*exit=1 ERROR")
            self.assertIn(f"E{n * 10 + 8} other", text)

    def test_large_variations_are_optional_at_small_target_and_hard_cap(self):
        events = [event(n, "run " + str(n + 1) * 3000, tool="shell") for n in range(4)]
        data = compress(events, lossless=True)
        before = json.dumps(data, sort_keys=True)
        with patch.dict(os.environ, {"AGENTLOG_HARD_CAP": "500"}):
            text = _bounded_report("compress", data, render_compress(data, max_tokens=100), 100)
        self.assertLessEqual(est_tokens(text), 100)
        self.assertIn("L1", text)
        self.assertIn("cycle: shell", text)
        self.assertIn("events: E0\u2013E3", text)
        self.assertIn("L1 variations: complete data in compress.json", text)
        self.assertNotIn("varied values", text)
        self.assertNotIn("1" * 3000, text)
        self.assertNotIn("Mandatory structure exceeds", text)
        self.assertEqual(json.dumps(data, sort_keys=True), before)
        restored = json.loads(before)
        self.assertEqual(list(expand_events(restored)), events)
        self.assertEqual([next(iter(row.values())) for row in restored["loops"][0]["variations"]],
                         [str(n + 1) * 3000 for n in range(4)])

    def test_multiple_loops_budget_detail_independently_and_keep_boundaries(self):
        events = [event(n, f"run {n + 1000}ms", tool="shell", turn=n) for n in range(4)]
        events += [event(4, "", "token_usage", turn=4, usage=Usage(context_used=9000)),
                   event(5, "summary", "compaction", turn=4),
                   event(6, "", "token_usage", turn=4, usage=Usage(context_used=2000)),
                   event(7, "failure", "error", turn=5, is_error=True)]
        events += [event(n + 8, "inspect " + str(n + 1) * 3000, tool="shell", turn=n + 6)
                   for n in range(4)]
        data = compress(events, lossless=True, body_cap=0)
        self.assertEqual(len(data["loops"]), 2)
        complete = json.dumps(data, sort_keys=True)
        for target in (40, 100, 400, 800):
            with self.subTest(target=target), patch.dict(os.environ, {"AGENTLOG_HARD_CAP": "1000"}):
                text = _bounded_report("compress", data, render_compress(data, max_tokens=target), target)
                self.assertLessEqual(est_tokens(text), 1000)
                self.assertIn("events: E0\u2013E3", text)
                self.assertIn("events: E8\u2013E11", text)
                self.assertEqual(text.count("cycle: shell"), 2)
                self.assertIn("compaction context 9000 \u2192 2000", text)
                self.assertIn("error ERROR", text)
                self.assertIn("E4 token_usage", text)
                self.assertIn("E6 token_usage", text)
                for turn in range(10):
                    self.assertIn(f"turn {turn} ", text)
                if est_tokens(text) > target:
                    self.assertIn(f"target {target}, actual {est_tokens(text)} estimated tokens", text)
                    self.assertNotIn("varied values", text)
                    self.assertNotIn("variations: complete data", text)
                else:
                    self.assertNotIn("Mandatory structure exceeds", text)
        text = _bounded_report("compress", data, render_compress(data, max_tokens=800), 800)
        self.assertIn("<DUR>#1=1000ms", text)
        self.assertIn("L2 variations: complete data in compress.json", text)
        self.assertNotIn("L1 variations: complete data", text)
        self.assertNotIn("2" * 3000, text)
        self.assertEqual(json.dumps(data, sort_keys=True), complete)
        self.assertEqual(list(expand_events(json.loads(complete))), events)

    def test_variation_notice_cannot_extend_mandatory_overflow_or_hard_minimum(self):
        events = [event(n * 3, "run " + str(n + 1) * 3000, tool="shell", turn=n) for n in range(4)]
        data = compress(events)
        text = _bounded_report("compress", data, render_compress(data, max_tokens=40), 40)
        minimum = est_tokens(text)
        self.assertGreater(minimum, 40)
        self.assertNotIn("variations: complete data", text)
        self.assertNotIn("varied values", text)
        for seq in (0, 3, 6, 9):
            self.assertRegex(text, rf"\bE{seq}\b")
        with patch.dict(os.environ, {"AGENTLOG_HARD_CAP": str(minimum)}):
            self.assertEqual(_bounded_report("compress", data, render_compress(data, max_tokens=40), 40), text)
        with patch.dict(os.environ, {"AGENTLOG_HARD_CAP": str(minimum - 1)}):
            with self.assertRaisesRegex(ValueError, rf"estimated minimum of {minimum} tokens"):
                render_compress(data, max_tokens=40)

    def test_header_and_notice_count_towards_absolute_cap(self):
        data = compress([event(n, f"item {chr(97 + n)}", "assistant_msg", turn=n) for n in range(16)])
        text = _bounded_report("compress", data, render_compress(data, max_tokens=40), 40)
        minimum = est_tokens(text)
        with patch.dict(os.environ, {"AGENTLOG_HARD_CAP": str(minimum)}):
            self.assertEqual(_bounded_report("compress", data, render_compress(data, max_tokens=40), 40), text)
        with patch.dict(os.environ, {"AGENTLOG_HARD_CAP": str(minimum - 1)}):
            with self.assertRaisesRegex(ValueError, rf"estimated minimum of {minimum} tokens.*AGENTLOG_HARD_CAP.*{minimum - 1}"):
                render_compress(data, max_tokens=40)

    def test_deterministic_numbering_and_per_turn_novelty(self):
        events = loop_events()
        events = [replace(e, turn=e.seq // 4) for e in events]
        first = compress(events)
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(compress(events), sort_keys=True))
        ratios = first["stats"]["novelty_per_turn"]
        self.assertEqual(ratios[0]["novelty_ratio"], 1)
        self.assertEqual(ratios[-1]["novelty_ratio"], 0)

    def test_greedy_loops_and_large_chunk_symbols(self):
        symbols = [("tool_result", "shell", ("B1", "B2", "B3", "B4"), "new"),
                   ("tool_result", "shell", ("B1", "B2", "B3", "B5"), "new")] * 10
        self.assertEqual(find_loops(symbols)[0]["period"], 2)
        self.assertEqual(find_sequences(symbols, find_loops(symbols)), [])

    def test_fifty_thousand_events_under_thirty_seconds(self):
        start = time.monotonic()
        events = (event(n, ["run maple", "failed birch", "fix elm", "patch oak"][n % 4],
                        ["tool_call", "tool_result", "assistant_msg", "tool_call"][n % 4]) for n in range(50000))
        meta = SessionMeta("synthetic-performance", "generic", "synthetic.jsonl", 15000000)
        data = compress_session(meta, events)
        elapsed = time.monotonic() - start
        self.assertEqual(data["stats"]["events"], 50000)
        self.assertEqual(data["stats"]["blocks"], 4)
        self.assertEqual(data["loops"][0]["repetitions"], 12500)
        self.assertLess(elapsed, 30, f"50000-event compression took {elapsed:.2f}s")

    def test_compaction_errors_turns_and_preview(self):
        events = [event(0, "first", "user_msg", turn=1),
                  event(1, "before 1200 after 300", "compaction", turn=1),
                  event(2, "synthetic failure", "error", turn=2)]
        rendered = render_compress(compress(events))
        self.assertIn("turn 1", rendered)
        self.assertIn("compaction", rendered)
        self.assertIn("error", rendered)
        self.assertIn("turn 2", rendered)
        for cap in (0, 5, 30, 100):
            self.assertLessEqual(len(preview("a" * 500, cap)), cap)

    def test_logged_compaction_context_and_typed_variation(self):
        events = [event(0, "", "token_usage", usage=Usage(context_used=1200)),
                  event(1, "summary", "compaction"),
                  event(2, "", "token_usage", usage=Usage(context_used=300))]
        self.assertIn("context 1200 → 300", render_compress(compress(events)))
        repeated = [event(n, f"same command duration {n + 1000}ms") for n in range(4)]
        data = compress(repeated)
        self.assertEqual(len(data["loops"]), 1)
        self.assertIn("<DUR>", render_compress(data))
        self.assertEqual(compress([event(0, "", "token_usage", turn=2)])["stats"]["novelty_per_turn"],
                         [{"turn": 2, "new_block_tokens": 0, "all_tokens": 0, "novelty_ratio": 0}])

    def test_input_validation(self):
        for options in ({"chunk_lines": 0}, {"near": 2}, {"min_reps": 1}, {"body_cap": -1}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                compress([], **options)
        with self.assertRaises(ValueError):
            compress([event(1, "one"), event(0, "two")])
        with self.assertRaises(ValueError):
            list(expand_events(compress([event(0, "text")])) )


class CompressionCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.source = cls.root / "synthetic.jsonl"
        rows = [{"role": "assistant", "content": "read maple at 2026-01-01T01:02:03Z\n" + "result maple\n" * 30} for _ in range(6)]
        cls.source.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def run_cli(self, args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_cli_files_print_cap_and_expansion(self):
        directory = self.root / "report"
        code, stdout, stderr = self.run_cli(["compress", str(self.source), "--out", str(directory), "--lossless", "--print", "--max-tokens", "500"])
        self.assertEqual(code, 0, stderr)
        self.assertLessEqual(est_tokens(stdout), 500)
        self.assertEqual(stdout, (directory / "compress.md").read_text(encoding="utf-8"))
        data = json.loads((directory / "compress.json").read_text(encoding="utf-8"))
        self.assertEqual(data["stats"]["view_tokens"], est_tokens(stdout))
        stats = (directory / "compress-stats.md").read_text(encoding="utf-8")
        self.assertTrue(stats.startswith("<!-- agentlog compress-stats"))
        output = self.root / "expanded.jsonl"
        code, _, stderr = self.run_cli(["expand", str(directory / "compress.json"), "--out", str(output)])
        self.assertEqual(code, 0, stderr)
        rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([r["text"] for r in rows], [e.text for e in expand_events(data)])

    def test_cli_shared_variants_expand_and_preserve_block_loop_event_refs(self):
        source = self.root / "shared-variant.jsonl"
        base = CompressionTests.variant_base()
        texts = [base] + [base + name + " branch\n" for name in ("maple", "birch", "elm", "maple", "birch", "elm")]
        source.write_text("".join(json.dumps({"role": "tool", "content": text}) + "\n" for text in texts), encoding="utf-8")
        directory = self.root / "shared-variant-report"
        code, _, stderr = self.run_cli(["compress", str(source), "--out", str(directory), "--lossless", "--chunk-lines", "200"])
        self.assertEqual(code, 0, stderr)
        data = json.loads((directory / "compress.json").read_text(encoding="utf-8"))
        self.assertEqual(len(data["variants"]), 3)
        self.assertEqual(data["loops"][0]["start_seq"], 1)
        expanded = self.root / "shared-variant-expanded.jsonl"
        code, _, stderr = self.run_cli(["expand", str(directory / "compress.json"), "--out", str(expanded)])
        self.assertEqual(code, 0, stderr)
        self.assertEqual([json.loads(line)["text"] for line in expanded.read_text(encoding="utf-8").splitlines()], texts)
        for ref, expected in (("B1", "E0"), ("L1", "E1"), ("E2", "E2")):
            code, stdout, stderr = self.run_cli(["show", ref, "--session", str(source), "--out", str(directory), "--context", "0"])
            self.assertEqual(code, 0, stderr)
            self.assertIn(ref, stdout)
            self.assertIn(expected, stdout)

    def test_cli_large_variations_succeed_and_json_retains_complete_values(self):
        source = self.root / "large-variation.jsonl"
        texts = ["run " + str(n + 1) * 3000 for n in range(4)]
        source.write_text("".join(json.dumps({"role": "assistant", "content": text}) + "\n"
                                  for text in texts), encoding="utf-8")
        directory = self.root / "large-variation-report"
        with patch.dict(os.environ, {"AGENTLOG_HARD_CAP": "500"}):
            code, stdout, stderr = self.run_cli(["compress", str(source), "--out", str(directory),
                                                 "--lossless", "--print", "--max-tokens", "100"])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stderr, "")
        self.assertLessEqual(est_tokens(stdout), 100)
        self.assertEqual(stdout, (directory / "compress.md").read_text(encoding="utf-8"))
        self.assertNotIn("varied values", stdout)
        data = json.loads((directory / "compress.json").read_text(encoding="utf-8"))
        self.assertEqual(data["stats"]["view_tokens"], est_tokens(stdout))
        self.assertEqual([item.text for item in expand_events(data)], texts)
        self.assertEqual([next(iter(row.values())) for row in data["loops"][0]["variations"]],
                         [str(n + 1) * 3000 for n in range(4)])

    def test_help_explains_target_overflow_and_absolute_refusal(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout), self.assertRaises(SystemExit) as result:
            main(["compress", "--help"])
        self.assertEqual(result.exception.code, 0)
        help_text = " ".join(stdout.getvalue().split())
        for phrase in ("Target for the complete view", "Mandatory structure may exceed the target",
                       "optional detail cannot cause overflow", "AGENTLOG_HARD_CAP", "absolute hard cap",
                       "header and overflow notice", "Refuses with exit 2 and no partial view"):
            self.assertIn(phrase, help_text)

    def test_overflow_print_notice_is_stderr_and_view_is_exact(self):
        directory = self.root / "overflow-print"
        code, stdout, stderr = self.run_cli(["compress", str(self.source), "--out", str(directory),
                                             "--print", "--max-tokens", "40"])
        self.assertEqual(code, 0, stderr)
        actual = est_tokens(stdout)
        self.assertGreater(actual, 40)
        self.assertLessEqual(actual, 20000)
        self.assertIn(f"target 40, actual {actual} estimated tokens", stderr)
        self.assertIn(f"target 40, actual {actual} estimated tokens", stdout)
        self.assertEqual(len(stderr.splitlines()), 1)
        self.assertEqual(stdout, (directory / "compress.md").read_text(encoding="utf-8"))
        self.assertIn(f"this_view_tokens {actual}", stdout)
        data = json.loads((directory / "compress.json").read_text(encoding="utf-8"))
        self.assertEqual(data["stats"]["view_tokens"], actual)
        self.assertNotIn("Repetition cost", stdout)

    def test_overflow_summary_and_json_keep_stdout_conventions(self):
        for json_mode in (False, True):
            directory = self.root / ("overflow-json" if json_mode else "overflow-summary")
            args = ["compress", str(self.source), "--out", str(directory), "--max-tokens", "40"]
            code, stdout, stderr = self.run_cli(args + (["--json"] if json_mode else []))
            self.assertEqual(code, 0, stderr)
            self.assertEqual(stderr, "")
            self.assertLessEqual(len(stdout.splitlines()), 20)
            if json_mode:
                self.assertEqual(len(stdout.splitlines()), 1)
                stats = json.loads(stdout)
                self.assertIn(f"target 40, actual {stats['view_tokens']} estimated tokens", stats["target_overflow"])
            else:
                self.assertIn("target_overflow: Mandatory structure exceeds", stdout)
            self.assertNotIn("## Transcript", stdout)

    def test_mandatory_refusal_has_no_stdout_or_partial_files(self):
        source = self.root / "mandatory-refusal.jsonl"
        source.write_text("".join(json.dumps({"role": "user", "content": f"task {n}"}) + "\n"
                                  for n in range(60)), encoding="utf-8")
        directory = self.root / "refused-report"
        with patch.dict(os.environ, {"AGENTLOG_HARD_CAP": "150"}):
            code, stdout, stderr = self.run_cli(["compress", str(source), "--out", str(directory),
                                                 "--print", "--max-tokens", "80"])
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertFalse(directory.exists())
        self.assertRegex(stderr, r"estimated minimum of \d+ tokens.*AGENTLOG_HARD_CAP is 150")
        self.assertIn("Raise AGENTLOG_HARD_CAP if appropriate", stderr)
        directory.mkdir()
        names = ("compress.md", "compress.json", "compress-stats.md")
        for name in names:
            (directory / name).write_text("existing synthetic report\n", encoding="utf-8")
        with patch.dict(os.environ, {"AGENTLOG_HARD_CAP": "150"}):
            code, stdout, stderr = self.run_cli(["compress", str(source), "--out", str(directory), "--max-tokens", "80"])
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        for name in names:
            self.assertEqual((directory / name).read_text(encoding="utf-8"), "existing synthetic report\n")

    def test_hard_cap_and_protected_destination_refused(self):
        with patch.dict(os.environ, {"AGENTLOG_HARD_CAP": "400"}):
            code, _, stderr = self.run_cli(["compress", str(self.source), "--out", str(self.root / "over-cap"), "--max-tokens", "401"])
        self.assertEqual(code, 2)
        self.assertIn("exceeds", stderr)
        self.assertFalse((self.root / "over-cap").exists())
        with patch.dict(os.environ, {"AGENTLOG_CODEX_HOME": str(self.root / "protected")}):
            code, _, _ = self.run_cli(["compress", str(self.source), "--out", str(self.root / "protected")])
        self.assertEqual(code, 2)
        self.assertFalse((self.root / "protected").exists())

    def test_expand_cannot_overwrite_source(self):
        directory = self.root / "safe-report"
        code, _, stderr = self.run_cli(["compress", str(self.source), "--out", str(directory), "--lossless"])
        self.assertEqual(code, 0, stderr)
        before = self.source.read_bytes()
        code, _, _ = self.run_cli(["expand", str(directory / "compress.json"), "--out", str(self.source)])
        self.assertEqual(code, 2)
        self.assertEqual(self.source.read_bytes(), before)

    def test_expand_invalid_report_does_not_truncate_output(self):
        report = self.root / "invalid-report.json"
        report.write_text(json.dumps({"schema_version": 1, "lossless": True,
                                      "blocks": {}, "occurrences": [{"units": [{"block": "B1"}]}]}), encoding="utf-8")
        output = self.root / "existing-output.jsonl"
        output.write_text("synthetic existing output\n", encoding="utf-8")
        code, _, _ = self.run_cli(["expand", str(report), "--out", str(output)])
        self.assertEqual(code, 2)
        self.assertEqual(output.read_text(encoding="utf-8"), "synthetic existing output\n")

    def test_stats_hardlink_refused_before_any_report_write(self):
        directory = self.root / "linked-stats"
        directory.mkdir()
        try:
            (directory / "compress-stats.md").hardlink_to(self.source)
        except NotImplementedError:
            self.skipTest("hard links unavailable")
        original = self.source.read_bytes()
        code, _, _ = self.run_cli(["compress", str(self.source), "--out", str(directory)])
        self.assertEqual(code, 2)
        self.assertEqual(self.source.read_bytes(), original)
        self.assertFalse((directory / "compress.md").exists())

    def test_symlink_outputs_refused_without_modifying_target(self):
        directory = self.root / "symlink-report"
        directory.mkdir()
        victim = self.root / "symlink-victim.txt"
        victim.write_text("synthetic target\n", encoding="utf-8")
        stats = directory / "compress-stats.md"
        try:
            stats.symlink_to(victim)
        except (NotImplementedError, OSError) as error:
            if isinstance(error, OSError) and getattr(error, "winerror", None) != 1314:
                raise
            self.skipTest("symlink creation unavailable")
        code, _, _ = self.run_cli(["compress", str(self.source), "--out", str(directory)])
        self.assertEqual(code, 2)
        self.assertFalse((directory / "compress.md").exists())
        self.assertEqual(victim.read_text(encoding="utf-8"), "synthetic target\n")
        report = self.root / "symlink-expand.json"
        data = compress([event(0, "synthetic result")], lossless=True)
        report.write_text(json.dumps(data), encoding="utf-8")
        target = self.root / "symlink-expanded.jsonl"
        target.symlink_to(victim)
        code, _, _ = self.run_cli(["expand", str(report), "--out", str(target)])
        self.assertEqual(code, 2)
        self.assertEqual(victim.read_text(encoding="utf-8"), "synthetic target\n")


if __name__ == "__main__":
    unittest.main()
