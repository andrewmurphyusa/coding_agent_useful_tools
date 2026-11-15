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

"""Synthetic tests for orientation views."""
import contextlib
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from agentlog import census, compress, sessions, show, skeleton
from agentlog.cli import main
from agentlog.core.tokens import est_tokens
from agentlog.readers.base import Event, SessionMeta, Usage
from agentlog.readers.discover import load_session


class ViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(); cls.root=Path(cls.temp.name)
        cls.path=cls.root/"synthetic.jsonl"
        records=[
            {"type":"session_meta","timestamp":"2026-01-01T00:00:00Z","payload":{"id":"abcdefgh-1234","title":"Synthetic test","cwd":"/work","cli_version":"1"}},
            {"type":"response_item","payload":{"type":"message","role":"user","content":"hello"}},
            {"type":"response_item","payload":{"type":"function_call","name":"shell","call_id":"c1","arguments":"{\"command\":\"git status\"}"}},
            {"type":"response_item","payload":{"type":"function_call_output","call_id":"c1","output":"ok"}},
            {"type":"compacted","payload":{"summary":"summary"}},
        ]
        cls.path.write_text("".join(json.dumps(x)+"\n" for x in records),encoding="utf-8")
    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()
    def test_census_groups_and_commands(self):
        meta,events=load_session(str(self.path)); data=census.census(meta,events)
        self.assertAlmostEqual(sum(x["percent"] for x in data["kind"].values()),100,places=1)
        self.assertEqual(data["command_heads"]["git status"]["count"],1)
        self.assertEqual(data["tools"]["shell"]["count"],1)
        self.assertEqual(census.command_head('{"command":"python -m unittest"}'),"python -m")

    def test_census_all_pairs_calls_per_file_and_preserves_aggregate_statistics(self):
        case_root=self.root/"aggregate_pairing"; case_root.mkdir(exist_ok=True)
        def call(call_id, command):
            return {"type":"response_item","payload":{"type":"function_call","name":"shell",
                    "call_id":call_id,"arguments":json.dumps({"command":command})}}
        def result(call_id, size):
            return {"type":"response_item","payload":{"type":"function_call_output",
                    "call_id":call_id,"output":"x"*size}}
        for reused_session_id in (False, True):
            for unmatched in (False, True):
                with self.subTest(reused_session_id=reused_session_id, unmatched=unmatched):
                    first=[{"type":"session_meta","payload":{"id":"aggregate-first"}},
                           call("shared-a","git status"),result("shared-a",14),
                           call("shared-b","python -m unittest"),result("shared-b",7)]
                    second=[{"type":"session_meta","payload":{"id":"aggregate-first" if reused_session_id else "aggregate-second"}}]
                    if unmatched:
                        first.append(call("cross-file","dotnet build"))
                        second.append(result("cross-file",21))
                    second.extend([call("shared-a","git status"),result("shared-a",28),
                                   call("shared-b","npm run test"),result("shared-b",35)])
                    if unmatched:
                        second.extend([call("cross-file","dotnet test"),result("cross-file",42)])
                    paths=[case_root/"first.jsonl",case_root/"second.jsonl"]
                    contents=["".join(json.dumps(record)+"\n" for record in records) for records in (first,second)]
                    for path, content in zip(paths,contents): path.write_text(content,encoding="utf-8",newline="\n")
                    out=case_root/"out"; stdout=io.StringIO()
                    with contextlib.redirect_stdout(stdout):
                        self.assertEqual(main(["census",str(case_root/"*.jsonl"),"--all","--out",str(out),"--json"]),0)
                    data=json.loads((out/"census.json").read_text(encoding="utf-8"))
                    total_bytes=sum(len(content.encode("utf-8")) for content in contents)
                    total_events=len(first)+len(second)
                    self.assertEqual(json.loads(stdout.getvalue()),{"sessions":2,"events":total_events,"bytes":total_bytes})
                    self.assertEqual(data["sessions"],2)
                    for label in ("raw_type","kind"):
                        self.assertEqual(sum(row["count"] for row in data[label].values()),total_events)
                        self.assertEqual(sum(row["bytes"] for row in data[label].values()),total_bytes)
                        self.assertAlmostEqual(sum(row["percent"] for row in data[label].values()),100,places=6)
                        for row in data[label].values():
                            self.assertAlmostEqual(row["percent"],100*row["bytes"]/total_bytes)
                    expected_heads={"git status":{"count":2,"total_tokens":12,"max_tokens":8,"mean_tokens":6},
                                    "python -m":{"count":1,"total_tokens":2,"max_tokens":2,"mean_tokens":2},
                                    "npm run":{"count":1,"total_tokens":10,"max_tokens":10,"mean_tokens":10}}
                    expected_tools={"shell":{"count":4,"total_tokens":24,"max_tokens":10,"mean_tokens":6}}
                    if unmatched:
                        expected_heads["dotnet test"]={"count":1,"total_tokens":12,"max_tokens":12,"mean_tokens":12}
                        expected_tools={"shell":{"count":5,"total_tokens":36,"max_tokens":12,"mean_tokens":7.2},
                                        "(unknown)":{"count":1,"total_tokens":6,"max_tokens":6,"mean_tokens":6}}
                    self.assertEqual(data["command_heads"],expected_heads)
                    self.assertEqual(data["tools"],expected_tools)
                    self.assertEqual(data["kind"]["tool_result"]["tokens"],42 if unmatched else 24)
                    self.assertEqual(data["kind"]["tool_result"]["count"],6 if unmatched else 4)
    def test_skeleton_patch_collapse_and_annotations(self):
        events=[Event(1,kind="tool_call",tool="apply_patch",text="*** Update File: a.py\n+x\n-y",turn=1),Event(2,kind="token_usage",turn=1,usage=Usage(context_used=3)),Event(3,kind="token_usage",turn=1,usage=Usage(context_used=4))]
        notes=skeleton.annotations({"blocks":{"B1":{"occurrences":[0,1]}},"occurrences":[{"seq":1,"units":[{"block":"B1","chunk":0}]},{"seq":2,"units":[{"block":"B1","chunk":0}]}],"loops":[{"ref":"L1","period":1,"repetitions":2,"occurrence_indexes":[0,1]}]})
        text=skeleton.render(SessionMeta("abcdefgh","generic","",1000),events,8000,notes=notes)
        self.assertIn("a.py (+1 \u22121)",text); self.assertIn("[B1 \u00d72]",text); self.assertIn("[L1 iter 1/2]",text)
        self.assertNotIn("E2 token_usage",text); self.assertIn("E3 token_usage",text)

    def test_skeleton_exact_display_strings_and_codepoints(self):
        meta=SessionMeta("abcdefgh","generic","",10000,context_window=272000)
        stamp="2026-11-12T14:22:10Z"
        events=[Event(3,ts=stamp,kind="context",text="# AGENTS.md instructions",est_tokens=2140,turn=7),
                Event(4,kind="token_usage",usage=Usage(context_used=182400,cumulative_total=1900000),turn=7),
                Event(5,kind="tool_call",tool="apply_patch",text="*** Update File: a.py\n+x\n-y",turn=7)]
        text=skeleton.render(meta,events,8000,raw_values=True,notes={5:["[B17 \u00d79]"]})
        self.assertEqual(text.splitlines(),[
            "\u2500\u2500 turn 7 \u00b7 "+stamp+" \u2500\u2500",
            "E3 context (AGENTS.md, 2,140t)",
            "E4 token_usage ctx 182,400 / 272,000 (67%) \u00b7 \u03a3 1.9M",
            'E5 tool_call apply_patch 0t "a.py (+1 \u22121)" [B17 \u00d79]'])
        self.assertTrue({0x2500,0x00b7,0x03a3,0x2212,0x00d7}.issubset(map(ord,text)))
        self.assertNotIn("?",text)
        self.assertEqual(skeleton.line_for(Event(6,kind="context",est_tokens=0),meta,False,{}),
                         "E6 context (context, 0t)")

    def test_skeleton_unknown_and_zero_usage_values(self):
        meta=SessionMeta("abcdefgh","generic","",1000)
        unknown=Event(1,kind="token_usage",usage=Usage())
        self.assertEqual(skeleton.line_for(unknown,meta,False,{}),
                         "E1 token_usage ctx ? / ? \u00b7 \u03a3 ?")
        zero=replace(unknown,usage=Usage(context_used=0,cumulative_total=0))
        self.assertEqual(skeleton.line_for(zero,replace(meta,context_window=100),False,{}),
                         "E1 token_usage ctx 0 / 100 (0%) \u00b7 \u03a3 0")
        self.assertEqual(skeleton.line_for(zero,replace(meta,context_window=0),False,{}),
                         "E1 token_usage ctx 0 / 0 \u00b7 \u03a3 0")
        text=skeleton.render(meta,[Event(2,kind="other",turn=1)],8000)
        self.assertIn("\u00b7 ? \u2500\u2500",text)

    def test_skeleton_argument_summary_ellipsis_and_length(self):
        event=Event(1,kind="user_msg",text="z"*101)
        line=skeleton.line_for(event,SessionMeta("abcdefgh","generic","",1000),True,{})
        summary=line.split('"')[1]
        self.assertEqual(summary,"z"*99+"\u2026")
        self.assertEqual(len(summary),100)
        self.assertEqual(ord(summary[-1]),0x2026)

    def test_skeleton_loop_annotations_from_compressor_with_bookkeeping(self):
        meta=SessionMeta("abcdefgh","generic",str(self.path),10000)
        events=[]
        for _ in range(5):
            for kind,text in (("tool_call","cycle call"),("token_usage","usage"),
                              ("tool_result","cycle result"),("settings","settings")):
                events.append(Event(len(events),kind=kind,text=text,turn=1))
        data=compress.compress_session(meta,events,min_block_tokens=0)
        loop=data["loops"][0]
        self.assertEqual((loop["period"],loop["repetitions"]),(2,5))
        out=self.root/"skeleton_loop"; out.mkdir(exist_ok=True)
        (out/"compress.json").write_text(json.dumps(data),encoding="utf-8")
        stdout=io.StringIO()
        with patch.object(skeleton,"load_session",return_value=(meta,events)), contextlib.redirect_stdout(stdout):
            self.assertEqual(main(["skeleton",str(self.path),"--out",str(out)]),0)
        lines={int(line.split()[0][1:]):line for line in stdout.getvalue().splitlines() if line.startswith("E")}
        for iteration in range(5):
            for seq in (iteration*4,iteration*4+2):
                self.assertIn(f"[{loop['ref']} iter {iteration+1}/5]",lines[seq])
            for seq in (iteration*4+1,iteration*4+3):
                self.assertNotIn(f"[{loop['ref']} iter",lines[seq])

    def test_skeleton_rejects_malformed_loop_metadata_without_output(self):
        meta=SessionMeta("abcdefgh","generic",str(self.path),10000)
        events=[Event(1,kind="tool_call"),Event(2,kind="tool_result")]
        out=self.root/"bad_skeleton_loop"; out.mkdir(exist_ok=True)
        valid={"ref":"L1","period":1,"repetitions":2,"occurrence_indexes":[0,1]}
        cases=[{"period":0},{"period":True},{"repetitions":0},{"repetitions":True},
               {"occurrence_indexes":None},{"occurrence_indexes":[0]},
               {"occurrence_indexes":[-1,1]},{"occurrence_indexes":[0,2]},
               {"occurrence_indexes":[0,0]},{"occurrence_indexes":[1,0]},
               {"occurrence_indexes":[0,True]}]
        for changes in cases + [{"sequences":[2,1]},{"sequences":[1,1]},{"sequences":[1,None]}]:
            with self.subTest(changes=changes):
                loop=dict(valid); loop.update({k:v for k,v in changes.items() if k != "sequences"})
                seqs=changes.get("sequences",[1,2])
                data={"loops":[loop],"occurrences":[{"seq":seq} for seq in seqs]}
                (out/"compress.json").write_text(json.dumps(data),encoding="utf-8")
                stdout=io.StringIO(); stderr=io.StringIO()
                with patch.object(skeleton,"load_session",return_value=(meta,events)), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    self.assertEqual(main(["skeleton",str(self.path),"--out",str(out)]),2)
                self.assertEqual(stdout.getvalue(),""); self.assertIn("invalid compressor",stderr.getvalue())
                self.assertFalse((out/"skeleton.md").exists()); self.assertFalse((out/"skeleton.json").exists())
    def test_sampling_retains_salient_turns(self):
        events=[Event(i,kind="error" if i==30 else "compaction" if i==40 else "token_usage" if i==50 else "other",turn=i,text="x",usage=Usage(context_used=999) if i==50 else None) for i in range(100)]
        text=skeleton.render(SessionMeta("abcdefgh","generic","",10000),events,450)
        for seq in (30,40,50): self.assertIn(f"E{seq} ",text)
        self.assertIn("turns omitted",text)
        omitted=text.splitlines()[-1]
        self.assertRegex(omitted,r"^\u2026 \d+ turns omitted \(use --turns\)$")
        self.assertEqual(ord(omitted[0]),0x2026)
    def test_skeleton_refuses_when_salient_turns_cannot_fit(self):
        meta=SessionMeta("abcdefgh","generic",str(self.path),10000)
        events=[Event(i,kind="error",turn=i,text="x") for i in range(29)]
        out=self.root/"refused"; stdout=io.StringIO(); stderr=io.StringIO()
        with patch.object(skeleton,"load_session",return_value=(meta,events)), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(main(["skeleton",str(self.path),"--out",str(out),"--max-tokens","100"]),2)
        self.assertEqual(stdout.getvalue(),"")
        self.assertIn("mandatory skeleton turns",stderr.getvalue())
        self.assertFalse(out.exists())

    def test_skeleton_budget_includes_report_and_json_stdout(self):
        meta=SessionMeta("abcdefgh","generic",str(self.path),10000)
        events=[Event(i,kind="error" if i==5 else "other",turn=i,text="x") for i in range(30)]
        out=self.root/"bounded"; stdout=io.StringIO()
        with patch.object(skeleton,"load_session",return_value=(meta,events)), contextlib.redirect_stdout(stdout):
            self.assertEqual(main(["skeleton",str(self.path),"--out",str(out),"--max-tokens","200","--json"]),0)
        self.assertLessEqual(est_tokens(stdout.getvalue()),200)
        report=(out/"skeleton.md").read_text(encoding="utf-8")
        self.assertLessEqual(est_tokens(report),200)
        self.assertIn("E5 error",report)
        self.assertNotIn("view truncated",report)
    def test_sessions_cache_and_filters(self):
        previous=Path.cwd(); os.chdir(self.root)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["sessions","--root",str(self.path),"--cwd","/work","--out",str(self.root/"listing")]),0)
                with patch.object(sessions,"read_session",side_effect=AssertionError("cache missed")):
                    self.assertEqual(main(["sessions","--root",str(self.path),"--cwd","/work","--out",str(self.root/"listing")]),0)
            rows=json.loads((self.root/"listing/sessions.json").read_text(encoding="utf-8")); self.assertEqual(len(rows),1)
        finally: os.chdir(previous)
    def test_show_and_cap(self):
        out=self.root/"showout"; output=io.StringIO()
        with contextlib.redirect_stdout(output): self.assertEqual(main(["show","E1","--session",str(self.path),"--out",str(out),"--context","0"]),0)
        self.assertIn("hello",output.getvalue()); self.assertTrue((out/"show.md").read_text(encoding="utf-8").startswith("<!-- agentlog show"))
        with contextlib.redirect_stdout(io.StringIO()): self.assertEqual(main(["show","E999","--session",str(self.path),"--out",str(out)]),1)
        with contextlib.redirect_stderr(io.StringIO()): self.assertEqual(main(["show","E1","--session",str(self.path),"--max-tokens","999999"]),2)

    def test_show_shared_variant_source_occurrences_and_caps(self):
        base="".join(f"component {chr(97+n//26)}{chr(97+n%26)} reports a healthy maple result\n" for n in range(80))
        texts=[base, base+"additional branch healthy\n", base+"additional branch healthy\n"]
        texts=[text+f"checked at 2026-01-01T01:0{n}:00Z\n" for n,text in enumerate(texts)]
        path=self.root/"variant_source.jsonl"
        path.write_text("".join(json.dumps({"role":"tool","content":text})+"\n" for text in texts),encoding="utf-8")
        out=self.root/"variant_out"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["compress",str(path),"--out",str(out),"--lossless","--chunk-lines","200"]),0)
        data=json.loads((out/"compress.json").read_text(encoding="utf-8"))
        self.assertEqual(data["variants"]["V1"]["occurrences"],[1,2])
        buf=io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(main(["show","V1","--session",str(path),"--out",str(out),"--context","3"]),0)
        result=json.loads((out/"show.json").read_text(encoding="utf-8"))["results"]
        self.assertEqual(result[0]["parent"],"B1")
        self.assertEqual([x["event"] for x in result[0]["occurrences"]],["E1","E2"])
        self.assertEqual(result[1]["text"],texts[1]); self.assertNotIn("masked_text",result[0])
        self.assertIn("2026-01-01T01:01:00Z",buf.getvalue()); self.assertNotIn("2026-01-01T01:02:00Z",buf.getvalue())
        buf=io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(main(["show","V1","--session",str(path),"--out",str(out),"--max-tokens","200"]),0)
        self.assertLessEqual(est_tokens(buf.getvalue()),200)
        self.assertLessEqual(est_tokens((out/"show.md").read_text(encoding="utf-8")),200)
        self.assertIn("truncated (+",buf.getvalue())
        buf=io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(main(["show","V99","--session",str(path),"--out",str(out)]),1)
        self.assertIn("V99: NOT FOUND",buf.getvalue())
        for ref in ("V", "V-1", "abcdefgh:V1"):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(["show",ref,"--session",str(path),"--out",str(out)]),2)
        broken=json.loads(json.dumps(data)); broken["variants"]["V1"]["occurrences"]=[999]
        (out/"compress.json").write_text(json.dumps(broken),encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["show","V1","--session",str(path),"--out",str(out)]),2)
        self.assertEqual(stdout.getvalue(),"")

    def test_show_analysis_refs(self):
        out=self.root/"analysis"; out.mkdir(exist_ok=True)
        (out/"compress.json").write_text(json.dumps({"blocks":{"B1":{"ref":"B1","first_seq":1,"occurrences":[0,1]}},"options":{"unit":"event","chunk_lines":40},"occurrences":[{"seq":1,"units":[{"block":"B1","chunk":0}]},{"seq":2,"units":[{"block":"B1","chunk":0}]}],"loops":[{"ref":"L1","start_seq":1,"period":1,"repetitions":2,"occurrence_indexes":[0,1]}]}),encoding="utf-8")
        (out/"loops.json").write_text(json.dumps({"findings":[{"ref":"F1","evidence":["E1"]}]}),encoding="utf-8")
        buf=io.StringIO()
        with contextlib.redirect_stdout(buf): self.assertEqual(main(["show","B1","L1","F1","--session",str(self.path),"--out",str(out)]),0)
        self.assertEqual(buf.getvalue().count("hello"),3)

    def test_show_valid_finding_evidence_strings_and_objects(self):
        out=self.root/"valid_finding_evidence"; out.mkdir(exist_ok=True)
        evidence=["E1",{"ref":"E3","reason":"paired result"},"B1",{"ref":"L1"}]
        finding={"ref":"F1","evidence":evidence}
        (out/"loops.json").write_text(json.dumps({"findings":[finding]}),encoding="utf-8")
        stdout=io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(main(["show","E2","F1","--session",str(self.path),"--context","0","--out",str(out)]),0)
        results=json.loads((out/"show.json").read_text(encoding="utf-8"))["results"]
        self.assertEqual(results[1],finding)
        self.assertEqual([event["seq"] for event in (results[0],*results[2:])],[2,1,3])
        self.assertIn("hello",stdout.getvalue())
        self.assertIn('"B1"',stdout.getvalue()); self.assertIn('"ref": "L1"',stdout.getvalue())

    def test_show_rejects_malformed_finding_evidence_before_output_or_report_changes(self):
        out=self.root/"invalid_finding_evidence"; out.mkdir(exist_ok=True)
        targets=[out/"show.md",out/"show.json"]
        originals=[b"existing Markdown\n",b'{"existing":true}\n']
        invalid=[None,17,2.5,True,False,[],["E1"],{},
                 "","E","E-1","not a reference","abcdefgh:B1"]
        cases=invalid+[{"ref":value} for value in invalid]
        for evidence in cases:
            for existing in (False,True):
                for json_mode in (False,True):
                    with self.subTest(evidence=evidence,existing=existing,json_mode=json_mode):
                        for path, content in zip(targets,originals):
                            if existing: path.write_bytes(content)
                            elif path.exists(): path.unlink()
                        finding={"ref":"F1","evidence":["E1",evidence]}
                        (out/"loops.json").write_text(json.dumps({"findings":[finding]}),encoding="utf-8")
                        stdout=io.StringIO(); stderr=io.StringIO()
                        with contextlib.redirect_stdout(stdout),contextlib.redirect_stderr(stderr):
                            self.assertEqual(main(["show","E1","F1","--session",str(self.path),"--out",str(out)]+(["--json"] if json_mode else [])),2)
                        self.assertEqual(stdout.getvalue(),"")
                        self.assertIn("F1 evidence[1]",stderr.getvalue())
                        self.assertIn("invalid findings evidence reference",stderr.getvalue())
                        self.assertNotIn("Traceback",stderr.getvalue())
                        if existing: self.assertEqual([path.read_bytes() for path in targets],originals)
                        else: self.assertFalse(any(path.exists() for path in targets))

    def test_show_loop_from_compressor_with_and_without_bookkeeping(self):
        meta=SessionMeta("abcdefgh","generic",str(self.path),10000)
        for bookkeeping in (False,True):
            events=[]
            def add(kind,text):
                events.append(Event(len(events),kind=kind,text=text,turn=1))
            for _ in range(5):
                add("tool_call","cycle call")
                if bookkeeping: add("token_usage","usage")
                add("tool_result","cycle result")
                if bookkeeping: add("settings","settings")
            data=compress.compress_session(meta,events,min_block_tokens=0)
            loop=data["loops"][0]
            expected=[data["occurrences"][i]["seq"] for i in loop["occurrence_indexes"][:loop["period"]]]
            out=self.root/("loop_bookkeeping" if bookkeeping else "loop_plain"); out.mkdir(exist_ok=True)
            (out/"compress.json").write_text(json.dumps(data),encoding="utf-8")
            buf=io.StringIO()
            with patch("agentlog.show.load_session",return_value=(meta,events)), contextlib.redirect_stdout(buf):
                self.assertEqual(main(["show",loop["ref"],"--session",str(self.path),"--out",str(out)]),0)
            results=json.loads((out/"show.json").read_text(encoding="utf-8"))["results"]
            self.assertEqual([event["seq"] for event in results[1:]],expected)
            self.assertEqual([event["kind"] for event in results[1:]],["tool_call","tool_result"])

    def test_show_loop_rejects_invalid_indexes_and_missing_events(self):
        out=self.root/"invalid_loops"; out.mkdir(exist_ok=True)
        cases=[(None,[{"seq":1}],"occurrence_indexes"),
               ([],[{"seq":1}],"occurrence_indexes"),
               (["0"],[{"seq":1}],"occurrence index"),
               ([-1],[{"seq":1}],"occurrence index"),
               ([1],[{"seq":1}],"occurrence index"),
               ([0],[{}],"event sequence"),
               ([0],[{"seq":999}],"E999")]
        for indexes,occurrences,message in cases:
            loop={"ref":"L1","period":1}
            if indexes is not None: loop["occurrence_indexes"]=indexes
            (out/"compress.json").write_text(json.dumps({"loops":[loop],"occurrences":occurrences}),encoding="utf-8")
            stdout=io.StringIO(); stderr=io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                self.assertEqual(main(["show","L1","--session",str(self.path),"--out",str(out)]),2)
            self.assertIn(message,stderr.getvalue()); self.assertEqual(stdout.getvalue(),"")

    def test_show_loop_rejects_unordered_indexes_and_sequences_without_output(self):
        out=self.root/"unordered_loops"; out.mkdir(exist_ok=True)
        cases=[([1,0],[{"seq":1},{"seq":2}],"occurrence indexes"),
               ([0,0],[{"seq":1},{"seq":2}],"occurrence indexes"),
               ([0,1],[{"seq":2},{"seq":1}],"event sequences"),
               ([0,1],[{"seq":1},{"seq":1}],"event sequences"),
               ([0,1,1],[{"seq":1},{"seq":2}],"occurrence indexes"),
               ([0,1,2],[{"seq":1},{"seq":2},{"seq":1}],"event sequences")]
        for indexes,occurrences,message in cases:
            with self.subTest(indexes=indexes, occurrences=occurrences):
                loop={"ref":"L1","period":2,"occurrence_indexes":indexes}
                (out/"compress.json").write_text(json.dumps({"loops":[loop],"occurrences":occurrences}),encoding="utf-8")
                stdout=io.StringIO(); stderr=io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    self.assertEqual(main(["show","E1","L1","--session",str(self.path),"--out",str(out)]),2)
                self.assertIn(message,stderr.getvalue()); self.assertEqual(stdout.getvalue(),"")
                self.assertFalse((out/"show.md").exists()); self.assertFalse((out/"show.json").exists())

    def test_show_loop_accepts_nonconsecutive_occurrence_indexes(self):
        out=self.root/"nonconsecutive_loop"; out.mkdir(exist_ok=True)
        loop={"ref":"L1","period":2,"occurrence_indexes":[0,2]}
        data={"loops":[loop],"occurrences":[{"seq":1},{"seq":2},{"seq":3}]}
        (out/"compress.json").write_text(json.dumps(data),encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["show","L1","--session",str(self.path),"--out",str(out)]),0)
        results=json.loads((out/"show.json").read_text(encoding="utf-8"))["results"]
        self.assertEqual([event["seq"] for event in results[1:]],[1,3])

    def test_show_cross_session_range_and_missing_analysis_refs(self):
        second=self.root/"second.jsonl"
        second.write_text("".join(json.dumps(x)+"\n" for x in [
            {"type":"session_meta","payload":{"id":"ijklmnop-1234"}},
            {"type":"response_item","payload":{"type":"message","role":"user","content":"second session"}},
            {"type":"response_item","payload":{"type":"message","role":"assistant","content":"answer"}},
        ]),encoding="utf-8")
        out=self.root/"cross"; buf=io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(main(["show","E1-E2","ijklmnop:E1-E2","B99","L99","F99","--session",str(self.path),"--root",str(second),"--out",str(out),"--context","0"]),1)
        text=buf.getvalue()
        self.assertIn("hello",text); self.assertIn("second session",text); self.assertIn("answer",text)
        for ref in ("B99","L99","F99"): self.assertIn(ref+": NOT FOUND",text)

    def test_sessions_all_metadata_filters(self):
        meta=SessionMeta("filterid-123","codex",str(self.path),self.path.stat().st_size,
                         cwd="/work/project",model="model-a",reasoning_effort="high",
                         cli_version="1.2",thread_source="automation",started="2026-01-02T00:00:00Z")
        def stream(_):
            yield meta
            yield Event(1,kind="error",turn=1)
        cases=[(["--source","codex"],True),(["--source","claude"],False),
               (["--since","2026-01-02"],True),(["--since","2026-01-03"],False),
               (["--until","2026-01-02"],True),(["--until","2026-01-01"],False),
               (["--cwd","/work"],True),(["--cwd","/else"],False),
               (["--thread-source","automation"],True),(["--thread-source","user"],False),
               (["--model","model-a"],True),(["--model","other"],False),
               (["--cli-version","1.2"],True),(["--cli-version","2"],False),
               (["--min-bytes",str(meta.bytes)],True),(["--min-bytes",str(meta.bytes+1)],False)]
        previous=Path.cwd(); case_root=self.root/"filter_cases"; case_root.mkdir(exist_ok=True); os.chdir(case_root)
        try:
            with patch.object(sessions,"read_session",side_effect=stream), contextlib.redirect_stdout(io.StringIO()):
                for flags,expected in cases:
                    out=case_root/"out"
                    self.assertEqual(main(["sessions","--root",str(self.path),"--out",str(out)]+flags),0)
                    rows=json.loads((out/"sessions.json").read_text(encoding="utf-8"))
                    self.assertEqual(bool(rows),expected,flags)
        finally: os.chdir(previous)
    def test_claude_census_and_large_skeleton_budget(self):
        path=self.root/"claude.jsonl"
        records=[{"type":"user","sessionId":"abcdefgh-claude","message":{"role":"user","content":"x"*4000}} for _ in range(1000)]
        path.write_text("".join(json.dumps(record)+"\n" for record in records),encoding="utf-8")
        meta,events=load_session(str(path)); data=census.census(meta,events)
        self.assertAlmostEqual(sum(row["percent"] for row in data["raw_type"].values()),100,places=1)
        rendered=skeleton.render(meta,events,8000)
        self.assertLessEqual(est_tokens(rendered),est_tokens(bytes(meta.bytes))*.05)
    def test_show_truncation_marker(self):
        path=self.root/"large.jsonl"; path.write_text(json.dumps({"role":"user","content":"z"*10000})+"\n",encoding="utf-8")
        out=self.root/"largeout"; buf=io.StringIO()
        with contextlib.redirect_stdout(buf): self.assertEqual(main(["show","E0","--session",str(path),"--out",str(out),"--max-tokens","200"]),0)
        self.assertIn("\u2026 truncated (+",buf.getvalue()); self.assertLessEqual(est_tokens(buf.getvalue()),200)
        marker=buf.getvalue().splitlines()[-1]
        self.assertEqual(ord(marker[0]),0x2026)
        self.assertRegex(marker,r"^\u2026 truncated \(\+\d+ tokens\)$")
        self.assertIn(marker,(out/"show.md").read_text(encoding="utf-8"))

    def test_show_caps_actual_escaped_stdout_and_complete_report(self):
        meta=SessionMeta("abcdefgh","generic",str(self.path),100000)
        for number, text in enumerate(("\\"*20000, '"'*20000, "\x00\t\n"*7000)):
            for json_mode in (False, True):
                with self.subTest(number=number, json_mode=json_mode):
                    out=self.root/f"escaped_show_{number}_{json_mode}"
                    stdout=io.StringIO()
                    refs=["E0"] if number < 2 else ["E0", "E0", "E0"]
                    with patch.object(show,"load_session",return_value=(meta,[Event(0,kind="user_msg",text=text)])), contextlib.redirect_stdout(stdout):
                        self.assertEqual(main(["show",*refs,"--session",str(self.path),"--context","0","--out",str(out),"--max-tokens","200"]+(["--json"] if json_mode else [])),0)
                    rendered=stdout.getvalue()
                    self.assertLessEqual(est_tokens(rendered),200)
                    view=json.loads(rendered)["view"] if json_mode else rendered.rstrip("\n")
                    marker=view.splitlines()[-1]
                    self.assertRegex(marker,r"^\u2026 truncated \(\+\d+ tokens\)$")
                    markdown=(out/"show.md").read_text(encoding="utf-8")
                    self.assertLessEqual(est_tokens(markdown),200)
                    self.assertEqual(markdown.split("\n",1)[1],view)
                    self.assertIn(f"this_view_tokens {est_tokens(markdown)}",markdown.splitlines()[0])
                    complete=json.loads((out/"show.json").read_text(encoding="utf-8"))
                    self.assertEqual(complete["references"],refs)
                    self.assertEqual([result["text"] for result in complete["results"]],[text]*len(refs))

    def test_show_redacts_before_boundary_truncation(self):
        meta=SessionMeta("abcdefgh","generic",str(self.path),100000)
        blank, _=show._bounded_view("z"*10000,["E0"],meta,200,False)
        boundary=blank.index("\n\u2026 truncated")
        secret="sk-"+"q"*100
        text="z"*(boundary-20)+" "+secret+" "+"tail "*1000
        stdout, markdown=show._bounded_view(text,["E0"],meta,200,False)
        for rendered in (stdout,markdown):
            self.assertNotIn("sk-",rendered)
            self.assertNotIn("q"*20,rendered)
            self.assertLessEqual(est_tokens(rendered),200)

    def test_show_tiny_budget_refuses_before_writes_and_preserves_reports(self):
        meta=SessionMeta("abcdefgh","generic",str(self.path),100000)
        event=Event(0,kind="user_msg",text="z"*20000)
        out=self.root/"tiny_show"; out.mkdir(exist_ok=True)
        targets=[out/"show.md",out/"show.json"]
        originals=[b"existing Markdown\n",b'{"existing": true}\n']
        for path, content in zip(targets,originals): path.write_bytes(content)
        cases=[(["E0"],1,False),(["E0"],35,True),(["E0"]*100,100,True)]
        for refs,budget,json_mode in cases:
            stdout=io.StringIO(); stderr=io.StringIO()
            with patch.object(show,"load_session",return_value=(meta,[event])), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                self.assertEqual(main(["show",*refs,"--session",str(self.path),"--context","0","--out",str(out),"--max-tokens",str(budget)]+(["--json"] if json_mode else [])),2)
            self.assertEqual(stdout.getvalue(),"")
            self.assertIn("wrapper, header and full truncation marker",stderr.getvalue())
            self.assertEqual([target.read_bytes() for target in targets],originals)
        fresh=self.root/"tiny_show_fresh"
        with patch.object(show,"load_session",return_value=(meta,[event])), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["show","E0","--session",str(self.path),"--out",str(fresh),"--max-tokens","1"]),2)
        self.assertFalse(fresh.exists())

    def test_show_block_first_chunk_from_actual_flattened_units(self):
        meta=SessionMeta("abcdefgh","generic",str(self.path),10000)
        for unit in ("event","chunk"):
            for ending in ("\n","\r\n"):
                events=[Event(0,kind="tool_result",text=ending.join(("maple first","cedar second","willow third"))+ending)]
                data=compress.compress_session(meta,events,unit=unit,chunk_lines=1,min_block_tokens=0)
                self.assertEqual(data["blocks"]["B2"]["occurrences"],[1])
                out=self.root/"source_chunk"; out.mkdir(exist_ok=True)
                (out/"compress.json").write_text(json.dumps(data),encoding="utf-8")
                with patch.object(show,"load_session",return_value=(meta,events)), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main(["show","B2","--session",str(self.path),"--out",str(out)]),0)
                result=json.loads((out/"show.json").read_text(encoding="utf-8"))["results"]
                self.assertEqual(result[1]["text"],"cedar second"+ending)
                self.assertEqual(result[0]["occurrences"],[{"event":"E0","chunk":1}])

    def test_skeleton_shared_variant_multiplicity_with_bookkeeping(self):
        base="".join(f"component {chr(97+n//26)}{chr(97+n%26)} reports a healthy maple result\n" for n in range(80))
        meta=SessionMeta("abcdefgh","generic",str(self.path),100000)
        events=[Event(0,kind="tool_result",text=base),Event(1,kind="token_usage",text="usage"),
                Event(2,kind="tool_result",text=base+"additional branch healthy\n"),
                Event(3,kind="settings",text="settings"),
                Event(4,kind="tool_result",text=base+"additional branch healthy\n")]
        data=compress.compress_session(meta,events,chunk_lines=200,min_block_tokens=0)
        self.assertEqual(data["variants"]["V1"]["occurrences"],[1,2])
        notes=skeleton.annotations(data)
        self.assertIn("[V1 \u00d72]",notes[2]); self.assertIn("[V1 \u00d72]",notes[4])
        self.assertFalse(notes.get(1)); self.assertFalse(notes.get(3))

    def test_skeleton_rejects_explicit_malformed_annotation_metadata(self):
        meta=SessionMeta("abcdefgh","generic",str(self.path),10000)
        valid=compress.compress_session(meta,[Event(0,kind="tool_result",text="one\ntwo\n")],chunk_lines=1,min_block_tokens=0)
        cases=[None,[],{"occurrences":None},{"blocks":None},{"variants":None},{"loops":None},
               {"blocks":{"B1":None}},{"variants":{"V1":[]}},{"loops":[None]},
               {"occurrences":[None]},{"occurrences":[{"seq":None}]},
               {"occurrences":[{"seq":-1}]},{"occurrences":[{"seq":True}]},
               {"occurrences":[{"seq":0,"units":None}]},
               {"occurrences":[{"seq":0,"units":{}}]},
               {"occurrences":[{"seq":0,"units":[None]}]},
               {"occurrences":[{"seq":0,"units":[{"block":[],"chunk":0}]}]}]
        for key, value in (("occurrences",None),("occurrences",[True]),("occurrences",[1]),("occurrences",[0,0]),("first_seq",None)):
            broken=json.loads(json.dumps(valid)); broken["blocks"]["B1"][key]=value; cases.append(broken)
        for value in (None,True,-1,2):
            broken=json.loads(json.dumps(valid)); broken["occurrences"][0]["units"][0]["chunk"]=value; cases.append(broken)
        for data in cases:
            with self.subTest(data=data), self.assertRaisesRegex(ValueError,"invalid compressor|inconsistent first"):
                skeleton.annotations(data)

    def test_show_rejects_malformed_analysis_objects_before_output(self):
        out=self.root/"malformed_objects"; out.mkdir(exist_ok=True)
        for ref,key in (("B1","blocks"),("V1","variants"),("L1","loops"),("F1","findings")):
            filename="loops.json" if ref.startswith("F") else "compress.json"
            for value in (None,[None],{ref:None},[1]):
                (out/filename).write_text(json.dumps({key:value}),encoding="utf-8")
                stdout=io.StringIO(); stderr=io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    self.assertEqual(main(["show",ref,"--session",str(self.path),"--out",str(out)]),2)
                self.assertEqual(stdout.getvalue(),"")
                self.assertIn("invalid compressor",stderr.getvalue())
                self.assertFalse((out/"show.md").exists()); self.assertFalse((out/"show.json").exists())

    def test_sessions_omission_ellipsis_and_report_budget(self):
        previous=Path.cwd(); case_root=self.root/"session_omission"; case_root.mkdir(exist_ok=True)
        os.chdir(case_root)
        try:
            metas=[SessionMeta(f"session{i:04}","generic",str(self.path),1000,
                               title="Synthetic session title "+"x"*50) for i in range(150)]
            def stream(_):
                yield next(meta_iter)
            meta_iter=iter(metas); out=case_root/"out"
            with patch.object(sessions,"discover_sessions",return_value=[self.path]*len(metas)), \
                    patch.object(sessions,"read_session",side_effect=stream), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["sessions","--out",str(out)]),0)
            report=(out/"sessions.md").read_text(encoding="utf-8")
            marker=report.splitlines()[-1]
            self.assertEqual(ord(marker[0]),0x2026)
            self.assertRegex(marker,r"^\u2026 \d+ more \(sessions\.json\)$")
            self.assertLessEqual(est_tokens(report),4000)
            self.assertTrue(report.startswith("<!-- agentlog sessions |"))
            self.assertEqual(len(json.loads((out/"sessions.json").read_text(encoding="utf-8"))),150)
        finally: os.chdir(previous)
