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

"""Core contracts using synthetic strings only."""
import contextlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
from agentlog.core.config import validate_max_tokens
from agentlog.core.minhash import MinHash, cluster, containment, jaccard, line_shingles, lsh_candidates, shingles
from agentlog.core.normalize import mask_volatile, restore_volatile
from agentlog.core.out import cap_text, dumps, summary, write_outputs
from agentlog.core.redact import ACCOUNT_KEYS, redact, redact_object
from agentlog.core.refs import Reference, event_ref, find_refs, parse_ref
from agentlog.core.tokens import est_tokens
from agentlog.readers.base import SessionMeta

class RedactionTests(unittest.TestCase):
    def test_all_pattern_kinds(self):
        planted = {
            "anthropic_key": "sk-"+"ant-"+"a"*25,
            "openai_key": "sk-"+"proj-"+"a"*25,
            "github_token": "gh"+"p_"+"a"*35,
            "aws_key": "AK"+"IA"+"A"*16,
            "azure_conn": "Account"+"Key="+"a"*25,
            "stripe_key": "sk_"+"test_"+"a"*25,
            "jwt": "ey"+"J"+"a"*12+"."+"b"*12+"."+"c"*12,
            "private_key": "-----BEGIN "+"PRIVATE KEY-----\nsynthetic\n-----END "+"PRIVATE KEY-----",
            "conn_password": "pwd"+"="+"a"*12,
            "bearer": "Bearer "+"a"*25,
            "kv_secret": "secret"+":"+"a"*12,
            "high_entropy": "auth "+"aB3dE5fG7hI9jK0m"+"N2pQ4rS6tU8vW1xY",
            "email": "synthetic"+"@"+"example.invalid",
            "account"+"_id": "user-"+"a"*25,
        }
        for kind, text in planted.items():
            with self.subTest(kind=kind):
                result, findings=redact(text)
                self.assertIn("[REDACTED:"+kind+"]",result)
                self.assertIn((kind,1),findings)
                self.assertNotIn(text,result)
                self.assertEqual(redact(result)[0],result)
    def test_all_account_keys_and_json(self):
        for key in ACCOUNT_KEYS:
            for value in ("synthetic-account-value",12345):
                with self.subTest(key=key,value=value):
                    text=json.dumps({key:value})
                    result, findings=redact(text)
                    self.assertEqual(json.loads(result)[key],"[REDACTED:account_id]")
                    self.assertIn(("account_id",1),findings)
    def test_embedded_json_account_values_at_multiple_escape_depths(self):
        for key in ACCOUNT_KEYS:
            for variant in (key, key.upper(), key.swapcase()):
                original = 'synthetic-identifier'
                text = json.dumps({variant:original})
                for depth in range(5):
                    with self.subTest(key=variant, depth=depth):
                        result, findings = redact(text)
                        self.assertNotIn(original,result)
                        self.assertEqual(findings,[("account_id",1)])
                        self.assertEqual(redact(result),(result,[]))
                        decoded = result
                        for _ in range(depth): decoded = json.loads(decoded)['message']
                        self.assertEqual(json.loads(decoded)[variant],'[REDACTED:account_id]')
                    text = json.dumps({'message':text})
                obj = {variant:original, 'nested':[{variant:original}]}
                result = redact_object(obj)
                self.assertNotIn(original,repr(result))
                self.assertEqual(redact_object(result),result)
    def test_embedded_json_escaped_quotes_and_secret_keys(self):
        for key, kind in (('account'+'_id','account_id'),('api'+'_key','kv_secret')):
            for value in ('synthetic-identifier with "quoted" content and \\ path', 'synthetic-identifier\\', 'synthetic-identifier\\\\', 'synthetic-identifier\\"'):
                text = json.dumps({key:value})
                for depth in range(4):
                    with self.subTest(kind=kind,depth=depth,value=value):
                        result, findings = redact(text)
                        self.assertNotIn('synthetic-identifier',result)
                        self.assertNotIn('quoted',result)
                        self.assertEqual(findings,[(kind,1)])
                        self.assertEqual(redact(result),(result,[]))
                        decoded = result
                        for _ in range(depth): decoded = json.loads(decoded)['message']
                        self.assertEqual(json.loads(decoded)[key],'[REDACTED:'+kind+']')
                    text = json.dumps({'message':text})
    def test_false_positive_guards(self):
        text="git "+"abcdef0123456789"*2+" 11111111-1111-4111-8111-111111111111 S0123456789 D01234567"
        self.assertEqual(redact(text),(text,[]))
    def test_options_and_nested(self):
        email="synthetic"+"@"+"example.invalid"
        self.assertEqual(redact(email,redact_emails=False),(email,[]))
        self.assertEqual(redact("planted",extra_patterns=[("custom","planted")])[0],"[REDACTED:custom]")
        key="user"+"_id"
        obj={"nested":[{key:"synthetic-account-value"}]}
        self.assertEqual(redact_object(obj)["nested"][0]["user_id"],"[REDACTED:account_id]")
    def test_dictionary_keys_are_redacted_in_objects_and_json(self):
        secrets = ('sk-'+'a'*30, 'user-'+'b'*25, 'synthetic'+'@'+'example.invalid')
        obj = {'nested': [{sensitive_key: 'synthetic'} for sensitive_key in secrets]}
        cleaned = redact_object(obj)
        serialized = dumps(obj)
        for secret in secrets:
            self.assertNotIn(secret, repr(cleaned))
            self.assertNotIn(secret, serialized)
        self.assertEqual(json.loads(serialized), cleaned)
        self.assertEqual(redact_object(cleaned), cleaned)
    def test_redacted_key_collisions_preserve_all_values_and_safe_keys(self):
        placeholder = '[REDACTED:email]'
        first = 'first'+'@'+'example.invalid'
        second = 'second'+'@'+'example.invalid'
        obj = {first: 'first-value', placeholder+'#2': 'reserved-value',
               second: 'second-value', placeholder: 'literal-value'}
        expected = {placeholder: 'literal-value', placeholder+'#2': 'reserved-value',
                    placeholder+'#3': 'first-value', placeholder+'#4': 'second-value'}
        self.assertEqual(redact_object(obj), expected)
        self.assertEqual(redact_object(dict(reversed(list(obj.items())))), expected)
        self.assertEqual(redact_object(expected), expected)
        self.assertEqual(json.loads(dumps(obj)), expected)
        self.assertEqual(len(redact_object({1: 'integer', '1': 'string', '1#2': 'reserved'})), 3)
    def test_placeholder_guards(self):
        for value in ("","[REDACTED:account_id]",None):
            text=json.dumps({"account"+"_id":value})
            self.assertEqual(redact(text),(text,[]))
    def test_placeholder_looking_account_values_are_redacted(self):
        for key in ACCOUNT_KEYS:
            for value in ("<sensitive-account>","<synthetic>","${private-value}","placeholder","synthetic","example","null","[REDACTED:other]"):
                with self.subTest(key=key,value=value):
                    result,findings=redact(json.dumps({key:value}))
                    self.assertEqual(json.loads(result)[key],"[REDACTED:account_id]")
                    self.assertEqual(findings,[("account_id",1)])
                    self.assertEqual(redact(result),(result,[]))

class NormalizationTests(unittest.TestCase):
    def test_every_mask_and_reconstruction(self):
        text='2026-01-01T01:02:03.456Z 03:04:05 2026-01-02 11111111-1111-4111-8111-111111111111 abcdef0123 1.5ms 123 1.25 /tmp/unique/file pid=12 file.py:4:9'
        masked,values=mask_volatile(text)
        for kind in ("TS","DATE","UUID","HEX","DUR","NUM","TMP","PID","LC"):
            self.assertIn("<"+kind+">",masked)
        self.assertEqual(restore_volatile(masked,values),text)
        self.assertEqual(mask_volatile(masked),(masked,[]))
    def test_existing_placeholders(self):
        text="Already <NUM> but 123 changes"
        masked,values=mask_volatile(text)
        self.assertEqual(restore_volatile(masked,values),text)
    def test_temp_windows_and_numeric(self):
        self.assertEqual(mask_volatile(r'C:\Users\synthetic\AppData\Local\Temp\work\a.txt')[0],"<TMP>")
        self.assertEqual(mask_volatile("123456789")[0],"<HEX>")
    def test_hex_numeric_overlap_has_explicit_precedence(self):
        text = '123456 1234567 01234567 deadbeef 0x1234567 1234567.1234567'
        masked, values = mask_volatile(text)
        self.assertEqual(masked,'<NUM> <HEX> <HEX> <HEX> <HEX> <NUM>')
        self.assertEqual(restore_volatile(masked,values),text)
        self.assertEqual(mask_volatile(masked),(masked,[]))
    def test_equivalent_logs(self):
        a="at 2026-01-01T00:00:00Z pid 42 after 120ms"
        b="at 2026-01-02T12:00:00Z pid 75 after 900ms"
        self.assertEqual(mask_volatile(a)[0],mask_volatile(b)[0])

class HashAndRefTests(unittest.TestCase):
    def test_estimate(self):
        for size in range(30):self.assertEqual(est_tokens("a"*size),-(-size*2//7))
    def test_minhash_and_exact_metrics(self):
        a=shingles("one two three four five");b=shingles("one two three four other")
        self.assertEqual(MinHash()(a),MinHash()(a))
        self.assertEqual(len(MinHash()(a)),128)
        self.assertEqual(jaccard(a,b),0.5)
        self.assertEqual(containment(a,a),1)
        sig=MinHash()(a)
        self.assertEqual(lsh_candidates({"b":sig,"a":sig}),[("a","b")])
    def test_line_mode_and_cluster(self):
        a="first 123\nsecond 456\nthird 789\nlast 100"
        b="first 321\nsecond 654\nthird 987\nlast 999"
        self.assertEqual(line_shingles(a),line_shingles(b))
        self.assertEqual(cluster({"b":a,"a":b},mode="line"),{"a":["a","b"]})
    def test_references(self):
        for ref in ("E0","B1","V2","L2","F3","12345678:E9"):
            self.assertEqual(str(parse_ref(ref)),ref)
        self.assertEqual(event_ref(8),"E8")
        for ref in ("E-1","abc:E1","12345678:B1","xE1",""):
            with self.assertRaises(ValueError):parse_ref(ref)
    def test_variant_reference_fields_and_numbering(self):
        self.assertEqual(parse_ref("V17"),Reference("V",17))
        self.assertEqual(parse_ref("V0"),Reference("V",0))
        self.assertEqual(str(parse_ref("V002")),"V2")
    def test_invalid_variant_reference_diagnostics(self):
        for ref in ("V", "V-1", "V+1", "v1", "V1.5", "V1suffix", " V1", "V1 "):
            with self.subTest(ref=ref):
                with self.assertRaisesRegex(ValueError,r"expected .*V<n>.*<session8>:E<n>"):
                    parse_ref(ref)
    def test_cross_session_references_remain_event_only(self):
        for kind in ("B","V","L","F"):
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(ValueError,"Cross-session references must identify an event"):
                    parse_ref("12345678:"+kind+"1")
        self.assertEqual(parse_ref("session-123:E9"),Reference("E",9,"session-123"))
    def test_find_refs_includes_variants_and_skips_cross_session_non_events(self):
        text="[E0] B1 -> V2 -> V3; L4 F5 12345678:E6; 12345678:V7 12345678:B8 12345678:L9 12345678:F10"
        self.assertEqual(list(find_refs(text)),[
            Reference("E",0),Reference("B",1),Reference("V",2),Reference("V",3),
            Reference("L",4),Reference("F",5),Reference("E",6,"12345678")])

class OutputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.temp=tempfile.TemporaryDirectory();cls.root=Path(cls.temp.name)
    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()
    def test_cap_refusal(self):
        with patch.dict(os.environ,{"AGENTLOG_HARD_CAP":"100"}):
            self.assertEqual(validate_max_tokens(100),100)
            with self.assertRaises(ValueError):validate_max_tokens(101)
            with self.assertRaises(ValueError):validate_max_tokens(0)
            self.assertLessEqual(est_tokens(cap_text("a"*10000,100)),100)
    def test_summary_limit(self):
        stream=io.StringIO()
        with contextlib.redirect_stdout(stream):summary({str(n):n for n in range(100)})
        self.assertEqual(len(stream.getvalue().splitlines()),20)
    def test_json_remains_valid_when_redacted(self):
        key="account"+"_id"
        data={"text":"pwd"+"="+"a"*25,key:"synthetic-private-account"}
        parsed=json.loads(dumps(data))
        self.assertEqual(parsed["account_id"],"[REDACTED:account_id]")
        self.assertNotIn("a"*25,parsed["text"])
    def test_deterministic_files_header_budget(self):
        meta=SessionMeta("synthetic-session","generic","synthetic",10000)
        directory=write_outputs("probe",meta,{"complete":"z"*10000},"a"*10000,self.root,max_tokens=200)
        before=(directory/'probe.md').read_bytes();text=before.decode()
        self.assertLessEqual(est_tokens(text),200)
        self.assertIn("session syntheti",text)
        stated=int(re.search(r"this_view_tokens (\d+)",text)[1]);self.assertEqual(stated,est_tokens(text))
        self.assertEqual(len(json.loads((directory/'probe.json').read_text())["complete"]),10000)
        write_outputs("probe",meta,{"complete":"z"*10000},"a"*10000,self.root,max_tokens=200)
        self.assertEqual(before,(directory/'probe.md').read_bytes())
