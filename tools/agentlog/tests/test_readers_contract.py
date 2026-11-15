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

"""Streaming reader contracts; all inputs are hand-written synthetic records."""
from dataclasses import FrozenInstanceError, asdict, replace
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import tracemalloc
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from agentlog import cli
from agentlog.core.jsonl import iter_records
from agentlog.core.zst import ZstdUnavailable, open_binary
from agentlog.readers import codex, claude, generic
from agentlog.readers.base import Event, SessionMeta, Usage
from agentlog.readers.discover import default_roots, discover_sessions, load_session, read_session, resolve_session

FIXTURES=Path(__file__).parent/'fixtures'

class FixtureReaderTests(unittest.TestCase):
    def test_codex_kinds_metadata_turns_and_usage(self):
        rows=list(codex.read(FIXTURES/'codex.jsonl'));meta=rows.pop(0)
        self.assertIsInstance(meta,SessionMeta)
        self.assertEqual(meta.source,'codex');self.assertEqual(meta.thread_source,'automation')
        self.assertEqual(meta.context_window,1000);self.assertEqual(meta.model,'synthetic-model')
        self.assertEqual(meta.reasoning_effort,'high');self.assertEqual(len(meta.instructions_sha1),40)
        self.assertEqual(meta.started,'2026-01-01T00:00:00Z');self.assertEqual(meta.ended,'2026-01-01T00:00:12Z')
        self.assertEqual([r.seq for r in rows],list(range(len(rows))))
        kinds={r.kind for r in rows}
        self.assertTrue({'context','user_msg','assistant_msg','tool_call','tool_result','reasoning','token_usage','compaction','task_start','task_end','settings','other'}<=kinds)
        self.assertEqual([r.turn for r in rows if r.kind=='context'],[0])
        self.assertEqual([r.turn for r in rows if r.kind=='user_msg'],[1])
        result=next(r for r in rows if r.kind=='tool_result');self.assertEqual(result.tool,'exec_command');self.assertEqual(result.exit_code,0)
        usage=next(r.usage for r in rows if r.kind=='token_usage')
        self.assertEqual((usage.input,usage.cached_input,usage.output,usage.reasoning,usage.total,usage.cumulative_total),(100,20,30,10,130,130))
        self.assertIsNone(usage.cache_creation_input)
        self.assertEqual(sum(r.raw_bytes for r in rows),meta.bytes)
        with self.assertRaises(FrozenInstanceError):meta.source='changed'
        with self.assertRaises(FrozenInstanceError):rows[0].text='changed'
    def test_claude_blocks_usage_context_compactions(self):
        rows=list(claude.read(FIXTURES/'claude.jsonl'));meta=rows.pop(0)
        self.assertEqual(meta.source,'claude');self.assertEqual(meta.cli_version,'0.2')
        self.assertEqual([r.seq for r in rows],list(range(len(rows))))
        self.assertEqual(sum(r.raw_bytes for r in rows),meta.bytes)
        self.assertEqual(len([r for r in rows if r.kind=='user_msg']),1)
        self.assertEqual(len([r for r in rows if r.kind=='compaction']),2)
        self.assertEqual(next(r for r in rows if r.kind=='tool_result').tool,'Read')
        usage=next(r.usage for r in rows if r.kind=='token_usage')
        self.assertEqual((usage.input,usage.cached_input,usage.cache_creation_input),(100,30,20))
        self.assertEqual(usage.context_used,150);self.assertEqual(usage.total,160)
        self.assertEqual(rows[-1].turn,1)
    def test_usage_additive_field_preserves_construction_and_lossless_reports(self):
        from agentlog.compress import compress_session
        from agentlog.expand import expand_events
        legacy=Usage(5,2,3,1,10,7,20)
        self.assertEqual(asdict(legacy),{'input':5,'cached_input':2,'output':3,
            'reasoning':1,'total':10,'context_used':7,'cumulative_total':20,
            'cache_creation_input':None})
        meta=SessionMeta('synthetic-usage','claude','synthetic.jsonl',0)
        events=[Event(seq=n,kind='token_usage',usage=replace(legacy,cache_creation_input=value))
                for n,value in enumerate((None,0,4))]
        report=compress_session(meta,events,lossless=True)
        # A JSON encode/decode exercises the persisted artifact representation.
        report=json.loads(json.dumps(report))
        self.assertEqual(list(expand_events(report)),events)
        for occurrence in report['occurrences']:
            occurrence['usage'].pop('cache_creation_input')
        self.assertEqual(list(expand_events(report)),
                         [replace(row,usage=legacy) for row in events])
    def test_generic_and_dispatch(self):
        for source in ('codex','claude','generic'):
            rows=list(read_session(FIXTURES/(source+'.jsonl')))
            self.assertEqual(rows[0].source,source)
            self.assertTrue(all(isinstance(r,Event) for r in rows[1:]))
        meta,rows=load_session(FIXTURES/'generic.jsonl')
        self.assertEqual([r.kind for r in rows],['user_msg','assistant_msg','other'])
        self.assertEqual(meta.session_id,'33333333-3333-4333-8333-333333333333')
    def test_reference_resolution(self):
        self.assertEqual(resolve_session('11111111',FIXTURES).name,'codex.jsonl')
        self.assertEqual(resolve_session('11111111-1111-4111-8111-111111111111',FIXTURES).name,'codex.jsonl')
        with self.assertRaises(ValueError):resolve_session('1111',FIXTURES)
        with self.assertRaises(FileNotFoundError):resolve_session('ffffffff',FIXTURES)

class SyntheticReaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.temp=tempfile.TemporaryDirectory();cls.root=Path(cls.temp.name)
    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()
    def write(self,name,rows):
        p=self.root/name;p.write_text(''.join(json.dumps(row)+'\n' for row in rows),encoding='utf-8');return p
    def test_secrets_never_enter_event_or_meta(self):
        secret='sk-'+'a'*30;account='user-'+'z'*25;email='synthetic'+'@'+'example.invalid'
        p=self.write('private.jsonl',[{'type':'session_meta','payload':{'id':'synthetic-id','cwd':email}},{'type':'response_item','payload':{'type':'message','role':'user','content':[{'text':secret+' '+account+' '+json.dumps({'ownerAccount'+'Uuid': 'synthetic-sensitive-value'})}]}}])
        rows=list(codex.read(p));text=repr(rows)
        self.assertNotIn(secret,text);self.assertNotIn(account,text);self.assertNotIn(email,text);self.assertNotIn('synthetic-sensitive-value',text)
    def test_codex_escaped_json_account_value_is_redacted_before_event(self):
        value = 'synthetic-sensitive-value\\'
        arguments = json.dumps({'message':json.dumps({'ACCOUNT'+'_ID':value})})
        record = {'type':'response_item','payload':{'type':'function_call','name':'synthetic','arguments':arguments}}
        rows = list(codex.read(self.write('escaped-account.jsonl',[record])))
        self.assertNotIn('synthetic-sensitive-value',repr(rows))
        decoded = json.loads(json.loads(rows[1].text)['message'])
        self.assertEqual(decoded['ACCOUNT_ID'],'[REDACTED:account_id]')
    def test_codex_modern_usage_precedes_all_legacy_counts(self):
        def legacy(total):
            return {'type':'event_msg','payload':{'type':'token_count','info':{'last_token_usage':{'total_tokens':total},'total_token_usage':{'total_tokens':total}}}}
        # The format reference guarantees modern completeness per file. Legacy
        # counts are ignored even if unequal, nonadjacent or in different turns.
        modern={'type':'token_usage_record','payload':{'response_id':'synthetic-response','turn_id':'synthetic-turn','usage':{'input_tokens':8,'output_tokens':2,'total_tokens':10}}}
        message={'type':'response_item','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':'synthetic next turn'}]}}
        records=[legacy(5), message, legacy(10), modern, legacy(40)]
        for order in (records, list(reversed(records))):
            with self.subTest(modern_index=order.index(modern)):
                rows=list(codex.read(self.write('modern.jsonl',order)))
                usage=[r.usage for r in rows[1:] if r.kind=='token_usage']
                self.assertEqual([u.total for u in usage],[10])
                self.assertIsNone(usage[0].cumulative_total)
                self.assertEqual(sum(r.raw_bytes for r in rows[1:]),rows[0].bytes)
                self.assertEqual([r.seq for r in rows[1:]],list(range(len(rows)-1)))
        # Distinct modern requests with equal usage remain distinct.
        second={**modern,'payload':{**modern['payload'],'response_id':'synthetic-second'}}
        rows=list(codex.read(self.write('two-modern.jsonl',[modern,second])))
        self.assertEqual([r.usage.total for r in rows[1:] if r.kind=='token_usage'],[10,10])
    def test_codex_legacy_cumulative_dedup_differences_and_reset(self):
        def usage(n):
            return {'input_tokens':n*8,'cached_input_tokens':n*3,'cache_write_input_tokens':0,
                    'output_tokens':n*2,'reasoning_output_tokens':n,'total_tokens':n*10}
        def legacy(last,total):
            return {'type':'event_msg','payload':{'type':'token_count','info':{'last_token_usage':usage(last),'total_token_usage':usage(total)}}}
        # First totals can be inherited by a continuation, so use last there.
        # A missed count makes the next difference twice last_token_usage.
        records=[legacy(1,10), legacy(1,10),
                 {'type':'event_msg','payload':{'type':'token_count','info':None}},
                 legacy(1,12), legacy(1,1), legacy(1,2)]
        rows=list(codex.read(self.write('legacy-cumulative.jsonl',records)))
        values=[r.usage for r in rows[1:] if r.kind=='token_usage']
        self.assertEqual([u.total for u in values],[10,20,10,10])
        self.assertEqual([u.cumulative_total for u in values],[100,120,10,20])
        self.assertEqual((values[1].input,values[1].cached_input,values[1].output,values[1].reasoning),(16,6,4,2))
        self.assertEqual(sum(r.raw_bytes for r in rows[1:]),rows[0].bytes)
    def test_codex_legacy_without_cumulative_and_zero_fields(self):
        def legacy(last,total=None):
            info={'last_token_usage':last}
            if total is not None: info['total_token_usage']=total
            return {'type':'event_msg','payload':{'type':'token_count','info':info}}
        rows=list(codex.read(self.write('legacy-last-only.jsonl',[legacy({'total_tokens':5}),legacy({'total_tokens':5})])))
        self.assertEqual([r.usage.total for r in rows[1:] if r.kind=='token_usage'],[5,5])
        # Zero field deltas are not replaced with an unrelated last-call value.
        rows=list(codex.read(self.write('zero-fields.jsonl',[
            legacy({'input_tokens':3,'output_tokens':2,'total_tokens':5},{'input_tokens':0,'output_tokens':0,'total_tokens':5}),
            legacy({'input_tokens':3,'output_tokens':2,'total_tokens':5},{'input_tokens':0,'output_tokens':0,'total_tokens':6})])))
        values=[r.usage for r in rows[1:] if r.kind=='token_usage']
        self.assertEqual((values[1].input,values[1].output,values[1].total),(0,0,1))
    def test_codex_encrypted_errors_null_and_custom_tools(self):
        p=self.write('edges.jsonl',[{'type':'session_meta','payload':{'id':'synthetic-id','source':{'subagent':{'thread_spawn':{}}}}},{'type':'response_item','payload':{'type':'reasoning','encrypted_content':'hidden-payload'}},{'type':'response_item','payload':{'type':'custom_tool_call','name':'Read','call_id':'c','input':'synthetic input'}},{'type':'response_item','payload':{'type':'custom_tool_call_output','call_id':'c','output':[{'type':'text','text':'Process exited with code 2'}]}},{'type':'event_msg','payload':{'type':'notice','message':'Rate limit reached'}},{'type':'event_msg','payload':{'type':'token_count','info':None}},{'type':'future_type','payload':[]},{'type':'session_meta','payload':{'id':'wrong-parent-id'}}])
        rows=list(codex.read(p));self.assertEqual(rows[0].session_id,'synthetic-id')
        self.assertNotIn('hidden-payload',repr(rows));self.assertIn('[encrypted 14 bytes]',repr(rows))
        result=next(r for r in rows[1:] if r.kind=='tool_result');self.assertTrue(result.is_error);self.assertEqual(result.exit_code,2)
        self.assertTrue(all(r.sidechain for r in rows[1:]));self.assertTrue(any(r.kind=='error' for r in rows[1:]))
        self.assertFalse(any(r.kind=='token_usage' for r in rows[1:]))
    def test_claude_usage_dedup_and_errors(self):
        assistant={'type':'assistant','requestId':'request','message':{'id':'message','model':'synthetic','content':[{'type':'text','text':'synthetic'}],'usage':{'input_tokens':5,'output_tokens':2}}}
        rows=list(claude.read(self.write('claude-dedup.jsonl',[assistant,assistant,{'type':'user','isMeta':True,'message':{'content':'Injected'}},{'type':'assistant','isApiErrorMessage':True,'message':{'content':'Synthetic API error'}}])))
        self.assertEqual(len([r for r in rows[1:] if r.kind=='token_usage']),1)
        self.assertEqual(rows[-1].kind,'error');self.assertEqual(rows[-2].kind,'context');self.assertEqual(rows[-1].turn,0)
    def test_claude_cache_creation_preserves_missing_zero_and_provider_semantics(self):
        values=[{}, {'cache_creation_input_tokens':0}, {'cache_creation_input_tokens':4},
                {'cache_creation_input_tokens':None}, {'cache_creation_input_tokens':True},
                {'cache_creation_input_tokens':'4'}]
        records=[{'type':'assistant','message':{'content':[],'usage':{
                    'input_tokens':5,'cache_read_input_tokens':2,'output_tokens':3,**value}}}
                 for value in values]
        rows=list(claude.read(self.write('claude-cache-creation.jsonl',records)))
        usage=[row.usage for row in rows[1:] if row.kind=='token_usage']
        self.assertEqual([u.cache_creation_input for u in usage],[None,0,4,None,None,None])
        self.assertEqual([(u.input,u.cached_input,u.context_used,u.total) for u in usage],
                         [(5,2,7,10),(5,2,7,10),(5,2,11,14),
                          (5,2,7,10),(5,2,7,10),(5,2,7,10)])
        zero={'type':'assistant','message':{'content':[],'usage':{
            'input_tokens':0,'cache_read_input_tokens':0,'cache_creation_input_tokens':0,
            'output_tokens':0}}}
        rows=list(claude.read(self.write('claude-cache-zero.jsonl',[zero])))
        self.assertEqual(rows[-1].usage,Usage(input=0,cached_input=0,output=0,total=0,
                                             context_used=0,cache_creation_input=0))
    def test_claude_unidentified_usage_records_are_preserved(self):
        records=[{'type':'assistant','message':{'content':[],'usage':{'input_tokens':n}}} for n in (10,20,20)]
        rows=list(claude.read(self.write('claude-unidentified.jsonl',records)))
        self.assertEqual([r.usage.input for r in rows[1:] if r.kind=='token_usage'],[10,20,20])
    def test_claude_present_usage_identities_deduplicate(self):
        for identity in ({'uuid':'record-id'},{'requestId':'request-id'},{'message_id':'message-id'}):
            with self.subTest(identity=identity):
                assistant={'type':'assistant','message':{'content':[],'usage':{'input_tokens':10}}}
                if 'message_id' in identity: assistant['message']['id']=identity['message_id']
                else: assistant.update(identity)
                rows=list(claude.read(self.write('claude-identity.jsonl',[assistant,assistant])))
                self.assertEqual(len([r for r in rows[1:] if r.kind=='token_usage']),1)
    def test_claude_usage_distant_repeat_preserves_pair_identity(self):
        def assistant(message_id, request_id):
            return {'type':'assistant','requestId':request_id,'message':{'id':message_id,'content':[],
                    'usage':{'input_tokens':7,'output_tokens':3}}}
        records=[assistant('synthetic-message-'+str(n),'synthetic-request-'+str(n)) for n in range(4097)]
        # Repeats straddle the former 1024-entry eviction boundary. Both parts
        # of the key matter; concatenation alone would confuse these pairs.
        distinct=[assistant('synthetic-shared','synthetic-a'),assistant('synthetic-shared','synthetic-b'),
                  assistant('synthetic-other','synthetic-b'),assistant('synthetic-a|synthetic-b','synthetic-c'),
                  assistant('synthetic-a','synthetic-b|synthetic-c')]
        rows=list(claude.read(self.write('claude-distant.jsonl',records+distinct+[records[0],records[1024]]+distinct)))
        self.assertEqual(sum(r.kind=='token_usage' for r in rows[1:]),4102)
        self.assertEqual(sum(r.raw_bytes for r in rows[1:]),rows[0].bytes)
        self.assertEqual([r.seq for r in rows[1:]],list(range(len(rows)-1)))
    def test_claude_temporary_identity_database_closes_on_generator_close_and_error(self):
        assistant={'type':'assistant','message':{'id':'synthetic','content':[],'usage':{'input_tokens':1}}}
        path=self.write('claude-cleanup.jsonl',[assistant,assistant])
        connections=[];databases=[]
        real_connect=sqlite3.connect
        def connect(filename, **kwargs):
            self.assertTrue(filename)
            database=Path(filename);databases.append(database)
            self.assertTrue(database.parent.is_dir())
            connection=real_connect(filename, **kwargs);connections.append(connection);return connection
        with patch('agentlog.readers.claude.sqlite3.connect',side_effect=connect):
            reader=claude.read(path);next(reader);next(reader)
            connection=connections[-1]
            self.assertEqual(connection.execute('PRAGMA cache_size').fetchone()[0],-1024)
            self.assertEqual(connection.execute('PRAGMA cache_spill').fetchone()[0],0)
            self.assertEqual(connection.execute('PRAGMA journal_mode').fetchone()[0],'off')
            self.assertEqual(connection.execute('PRAGMA temp_store').fetchone()[0],2)
            self.assertFalse(connection.in_transaction)
            reader.close()
            with self.assertRaises(sqlite3.ProgrammingError):connection.execute('SELECT 1')
            self.assertFalse(databases[-1].parent.exists())
            reader=claude.read(path);next(reader)
            with patch('agentlog.readers.claude._events',side_effect=OSError('synthetic read failure')):
                with self.assertRaisesRegex(OSError,'synthetic read failure'):next(reader)
            with self.assertRaises(sqlite3.ProgrammingError):connections[-1].execute('SELECT 1')
            self.assertFalse(databases[-1].parent.exists())
        self.assertEqual(len(connections),2)
    def test_claude_flat_native_memory_on_fifty_megabytes_of_unique_usage(self):
        path=self.root/'claude-large.jsonl';count=0;first=None
        with path.open('w',encoding='utf-8',newline='\n') as stream:
            while stream.tell()<50*1024*1024:
                row=json.dumps({'type':'assistant','requestId':'synthetic-request-'+str(count),
                     'message':{'id':'synthetic-message-'+str(count),'model':'synthetic','content':[],
                                'usage':{'input_tokens':1}},'padding':'synthetic '*25})+'\n'
                if first is None:first=row
                stream.write(row);count+=1
            stream.write(first)
        # tracemalloc excludes SQLite's native page cache. A child reports its
        # process peak working set/RSS after warmup and at EOF, including both
        # Python and SQLite allocations. Unique IDs defeat an in-memory set.
        script=textwrap.dedent('''\
            import json, sys
            from pathlib import Path
            from agentlog.readers.claude import read
            if sys.platform == 'win32':
                import ctypes
                from ctypes import wintypes
                class Counters(ctypes.Structure):
                    _fields_ = [('cb',wintypes.DWORD),('PageFaultCount',wintypes.DWORD)] + [
                        (name,ctypes.c_size_t) for name in ('PeakWorkingSetSize','WorkingSetSize',
                        'QuotaPeakPagedPoolUsage','QuotaPagedPoolUsage','QuotaPeakNonPagedPoolUsage',
                        'QuotaNonPagedPoolUsage','PagefileUsage','PeakPagefileUsage')]
                kernel=ctypes.WinDLL('kernel32',use_last_error=True)
                kernel.GetCurrentProcess.restype=wintypes.HANDLE
                get_memory=ctypes.WinDLL('psapi',use_last_error=True).GetProcessMemoryInfo
                get_memory.argtypes=[wintypes.HANDLE,ctypes.POINTER(Counters),wintypes.DWORD]
                get_memory.restype=wintypes.BOOL
                def peak():
                    counters=Counters();counters.cb=ctypes.sizeof(counters)
                    if not get_memory(kernel.GetCurrentProcess(),ctypes.byref(counters),counters.cb):
                        raise ctypes.WinError(ctypes.get_last_error())
                    return counters.PeakWorkingSetSize
            else:
                import resource
                def peak():
                    scale=1 if sys.platform=='darwin' else 1024
                    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*scale
            early=None;count=0
            for event in read(Path(sys.argv[1])):
                if getattr(event,'kind',None)=='token_usage':
                    count+=1
                    if count==4096:early=peak()
            print(json.dumps({'count':count,'early':early,'late':peak()}))
            ''')
        result=subprocess.run([sys.executable,'-c',script,str(path)],capture_output=True,text=True,check=True)
        measured=json.loads(result.stdout)
        self.assertGreater(count,90000)
        self.assertEqual(measured['count'],count)
        self.assertLess(measured['late']-measured['early'],8*1024*1024,measured)
    def test_jsonl_invalid_nonobject_bom_bytes(self):
        p=self.root/'invalid.jsonl';payload=b'\xef\xbb\xbf{"role":"user","content":"a"}\r\n[]\n{"bad":\xff}\n{"half"'
        p.write_bytes(payload);records=list(iter_records(p))
        self.assertEqual(sum(size for _,_,size in records),len(payload))
        self.assertEqual([r[1].get('type') for r in records[1:]],['non_object_json','malformed_json','malformed_json'])
        self.assertEqual(len(list(generic.read(p))),5)
    def test_prefix_ambiguity_and_env_roots(self):
        p1=self.write('ambiguous-a.jsonl',[{'session_id':'abcdabcd-1','role':'user','content':'synthetic'}]);p2=self.write('ambiguous-b.jsonl',[{'session_id':'abcdabcd-2','role':'user','content':'synthetic'}])
        with self.assertRaises(ValueError):resolve_session('abcdabcd',[p1,p2])
        with patch.dict(os.environ,{'AGENTLOG_CODEX_HOME':str(self.root/'synthetic-codex'),'AGENTLOG_CLAUDE_HOME':str(self.root/'synthetic-claude')}):
            self.assertEqual(default_roots()['codex'],self.root/'synthetic-codex')
            self.assertEqual(list(discover_sessions()),[])
    def test_zstd_missing_warns_without_writing(self):
        p=self.root/'synthetic.jsonl.zst';p.write_bytes(b'synthetic')
        with patch('agentlog.core.zst.native_available',return_value=False),patch('agentlog.core.zst.shutil.which',return_value=None):
            with self.assertWarnsRegex(RuntimeWarning,'install zstd'):
                with self.assertRaises(ZstdUnavailable):
                    with open_binary(p):pass
        self.assertEqual(p.read_bytes(),b'synthetic')
    def test_flat_memory_on_fifty_megabytes(self):
        p=self.root/'large.jsonl'
        row=json.dumps({'role':'assistant','content':'synthetic '*400}).encode()+b'\n'
        with p.open('wb') as stream:
            for _ in range((50*1024*1024)//len(row)+1):stream.write(row)
        tracemalloc.start()
        count=sum(1 for _ in generic.read(p))
        _,peak=tracemalloc.get_traced_memory();tracemalloc.stop()
        self.assertGreater(count,10000);self.assertLess(peak,4*1024*1024)
    def test_doctor_and_cli_error_codes(self):
        stream=io.StringIO()
        with contextlib.redirect_stdout(stream):result=cli.main(['doctor','--root',str(FIXTURES),'--out',str(self.root/'doctor'),'--json'])
        self.assertEqual(result,0);data=json.loads(stream.getvalue());self.assertEqual(data['sessions'],{'codex':1,'claude':1,'generic':1})
        self.assertIn('fts5',data);self.assertIn('compression.zstd',data)
        self.assertTrue((self.root/'doctor/doctor.json').exists())

    def test_doctor_skips_unsupported_archives_with_any_name(self):
        root=self.root/'doctor-archives';root.mkdir()
        plain=root/'synthetic.jsonl';plain.write_bytes((FIXTURES/'generic.jsonl').read_bytes())
        paths=[root/'synthetic-archive.jsonl.zst',root/'rollout-synthetic.jsonl.zst']
        for path in paths:path.write_bytes(b'synthetic archive')
        stdout=io.StringIO()
        with patch('agentlog.core.zst.native_available',return_value=False),patch('agentlog.core.zst.shutil.which',return_value=None),contextlib.redirect_stdout(stdout):
            with self.assertWarnsRegex(RuntimeWarning,'install zstd') as warning:
                result=cli.main(['doctor','--root',str(root),'--out',str(self.root/'doctor-archives-out'),'--json'])
        self.assertEqual(result,0)
        self.assertEqual(len(warning.warnings),2)
        self.assertEqual(json.loads(stdout.getvalue())['sessions'],{'codex':0,'claude':0,'generic':1})
        for path in paths:self.assertEqual(path.read_bytes(),b'synthetic archive')

    def test_zstd_external_stream_and_native_stream(self):
        p=self.root/'compressed.jsonl.zst';p.write_bytes(b'synthetic archive')
        process=SimpleNamespace(stdout=io.BytesIO(b'{"role":"user","content":"synthetic"}\n'),wait=Mock(return_value=0),poll=Mock(return_value=0))
        with patch('agentlog.core.zst.native_available',return_value=False),patch('agentlog.core.zst.shutil.which',return_value='synthetic-zstd'),patch('agentlog.core.zst.subprocess.Popen',return_value=process) as popen:
            with open_binary(p) as stream:self.assertIn(b'synthetic',stream.read())
            self.assertEqual(popen.call_args.args[0],['synthetic-zstd','-dc',str(p)])
            self.assertNotIn('shell',popen.call_args.kwargs)
        module=SimpleNamespace(open=lambda path,mode:io.BytesIO(b'synthetic decoded'))
        with patch('agentlog.core.zst.native_available',return_value=True),patch('agentlog.core.zst.importlib.import_module',return_value=module):
            with open_binary(p) as stream:self.assertEqual(stream.read(),b'synthetic decoded')
        self.assertEqual(p.read_bytes(),b'synthetic archive')
    def test_zstd_nonzero_exit_is_io_error(self):
        process=SimpleNamespace(stdout=io.BytesIO(b''),wait=Mock(return_value=1),poll=Mock(return_value=1))
        with patch('agentlog.core.zst.native_available',return_value=False),patch('agentlog.core.zst.shutil.which',return_value='synthetic-zstd'),patch('agentlog.core.zst.subprocess.Popen',return_value=process):
            with self.assertRaisesRegex(OSError,'zstd'):
                with open_binary(self.root/'failed.jsonl.zst') as stream:stream.read()
    def test_default_discovery_archives_and_subagents(self):
        codex_home=self.root/'codex-discovery';claude_home=self.root/'claude-discovery'
        session=codex_home/'sessions'/'rollout-synthetic.jsonl';session.parent.mkdir(parents=True,exist_ok=True)
        session.write_text('{"type":"session_meta","payload":{"id":"synthetic-main"}}\n',encoding='utf-8')
        archive=codex_home/'archived_sessions'/'synthetic.jsonl.zst';archive.parent.mkdir(exist_ok=True);archive.write_bytes(b'synthetic')
        claude_session=claude_home/'projects'/'synthetic'/'synthetic.jsonl';claude_session.parent.mkdir(parents=True,exist_ok=True)
        claude_session.write_text('{"type":"user","sessionId":"synthetic-claude","message":{"content":"synthetic"}}\n',encoding='utf-8')
        subagent=claude_session.parent/'synthetic'/'subagents'/'agent-synthetic.jsonl';subagent.parent.mkdir(parents=True,exist_ok=True);subagent.write_bytes(claude_session.read_bytes())
        with patch.dict(os.environ,{'AGENTLOG_CODEX_HOME':str(codex_home),'AGENTLOG_CLAUDE_HOME':str(claude_home)}):
            self.assertEqual(set(discover_sessions()),{session,archive,claude_session,subagent})
            self.assertEqual(set(discover_sessions(include_archived=False)),{session,claude_session,subagent})
            self.assertEqual(set(discover_sessions(source='claude')),{claude_session,subagent})
        self.assertTrue(next(r for r in read_session(subagent) if isinstance(r,Event)).sidechain)
    def test_cli_refusal_and_io_errors(self):
        parser=argparse.ArgumentParser();parser.add_argument('--max-tokens',type=int,default=8000);parser.set_defaults(func=lambda args:0)
        stream=io.StringIO()
        with patch('agentlog.cli.build_parser',return_value=parser),contextlib.redirect_stderr(stream):
            self.assertEqual(cli.main(['--max-tokens','20001']),2)
        self.assertIn('exceeds',stream.getvalue())
        def fail(args):raise OSError('synthetic failure')
        parser.set_defaults(func=fail)
        with patch('agentlog.cli.build_parser',return_value=parser),contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main([]),3)
    def test_malformed_known_fields_never_crash(self):
        codex_rows=[{'type':'session_meta','payload':[]},{'type':'response_item','payload':{'type':'message','role':[],'content':None}},{'type':'response_item','payload':{'type':'function_call','call_id':[],'name':{}}},{'type':'response_item','payload':{'type':'function_call_output','call_id':[]}}]
        self.assertEqual(len(list(codex.read(self.write('malformed-codex.jsonl',codex_rows)))),5)
        claude_rows=[{'type':'assistant','message':{'id':[],'usage':{},'content':[{'type':'tool_use','id':[]},{'type':'tool_result','tool_use_id':[]}]}}]
        self.assertGreater(len(list(claude.read(self.write('malformed-claude.jsonl',claude_rows)))),1)
        self.assertEqual(len(list(generic.read(self.write('malformed-generic.jsonl',[{'role':[],'content':{}}])))),2)
    def test_short_exact_id_allowed(self):
        p=self.write('short-id.jsonl',[{'session_id':'demo','role':'user','content':'synthetic'}])
        self.assertEqual(resolve_session('demo',[p]),p)
