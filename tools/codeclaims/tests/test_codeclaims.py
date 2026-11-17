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

import contextlib
import builtins
import hashlib
import io
import json
import os
import re
import struct
import tempfile
import unittest
import zlib
import subprocess
import sys
from collections import Counter
from pathlib import Path
from unittest.mock import patch
from codeclaims import cli

class CodeclaimsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory(); cls.root=Path(cls.tmp.name)
        cls.repo=cls.root/'repo'; cls.repo.mkdir(); (cls.repo/'auth').mkdir()
        (cls.repo/'auth'/'creds.sh').write_text('planted credential',encoding='utf8')
        (cls.repo/'app.py').write_text("""from fastapi import APIRouter
router = APIRouter(prefix='/api')
app.include_router(router, prefix='/v1')
@router.post('/items/{id}')
async def create():
    pass
class Widget(Base):
    __tablename__ = 'widgets'
    id = Column(int)
class Gadget(Base):
    __tablename__ = 'gadgets'
    name = mapped_column(str)
class Config(BaseSettings):
    API_HOST: str
x = os.getenv('HOST_NAME')
# TODO: implementation
def unfinished():
    raise NotImplementedError()
""",encoding='utf8')
        (cls.repo/'bad.py').write_text('def bad(:',encoding='utf8')
        (cls.repo/'main.tf').write_text('resource "aws_instance" "web" {}',encoding='utf8')
        (cls.repo/'requirements.txt').write_text('some-lib>=1',encoding='utf8')
        (cls.repo/'test_app.py').write_text('def test_widget(): pass',encoding='utf8')
        (cls.repo/'migrations').mkdir(); (cls.repo/'migrations'/'versions').mkdir()
        (cls.repo/'migrations'/'versions'/'one.py').write_text('"""Initial schema"""\nrevision = "abc123"\ndown_revision = None\n',encoding='utf8')
        (cls.repo/'migrations'/'versions'/'two.py').write_text('revision = "def456"\ndown_revision = "abc123"\n',encoding='utf8')
        (cls.repo/'flask.py').write_text('\n'.join([
            "bp = Blueprint('x', __name__, url_prefix='/flask')",
            "@bp.route('/things', methods=['POST'])", 'def things(): pass', '']), encoding='utf8')
        cls.out=cls.root/'out'
    @classmethod
    def tearDownClass(cls): cls.tmp.cleanup()

    def indexed_fixture(self, name, files, gitlinks=None):
        """Hand-write a clean SHA-1 repository; never run Git mutations."""
        repo = self.fixture(name, files)
        metadata = repo / '.git'
        (metadata / 'objects').mkdir(parents=True)
        (metadata / 'refs' / 'heads').mkdir(parents=True)
        (metadata / 'config').write_text('[core]\nrepositoryformatversion = 0\nbare = false\n', encoding='ascii')
        (metadata / 'HEAD').write_text('ref: refs/heads/main\n', encoding='ascii')

        def store(kind, content):
            raw = kind.encode() + b' ' + str(len(content)).encode() + b'\0' + content
            oid = hashlib.sha1(raw).digest()
            target = metadata / 'objects' / oid.hex()[:2] / oid.hex()[2:]
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(zlib.compress(raw))
            return oid

        tree = {}; entries = []
        for path in sorted(set(files) | set(gitlinks or {})):
            mode = 0o160000 if path in (gitlinks or {}) else 0o100644
            content = b'' if mode == 0o160000 else (repo / path).read_bytes()
            oid = bytes.fromhex(gitlinks[path]) if mode == 0o160000 else store('blob', content)
            filename = path.encode()
            entry = struct.pack('!10I20sH', 0, 0, 0, 0, 0, 0, mode, 0, 0,
                                len(content), oid, min(len(filename), 4095)) + filename + b'\0'
            entries.append(entry + b'\0' * (-len(entry) % 8))
            directory = tree
            for part in path.split('/')[:-1]:
                directory = directory.setdefault(part, {})
            directory[path.split('/')[-1]] = (mode, oid)

        def store_tree(directory):
            content = b''
            for filename, value in sorted(directory.items(), key=lambda item: item[0] + ('/' if isinstance(item[1], dict) else '')):
                mode, oid = (0o40000, store_tree(value)) if isinstance(value, dict) else value
                content += f'{mode:o} {filename}'.encode() + b'\0' + oid
            return store('tree', content)

        tree_id = store_tree(tree)
        identity = b'Fixture <fixture' + b'@' + b'example.invalid> 1 +0000\n'
        commit = store('commit', b'tree ' + tree_id.hex().encode() + b'\nauthor ' + identity +
                       b'committer ' + identity + b'\nSynthetic fixture\n')
        (metadata / 'refs' / 'heads' / 'main').write_text(commit.hex() + '\n', encoding='ascii')
        index = struct.pack('!4sII', b'DIRC', 2, len(entries)) + b''.join(entries)
        (metadata / 'index').write_bytes(index + hashlib.sha1(index).digest())
        return repo, commit.hex()

    def test_version_flag(self):
        from codeclaims import __version__
        stdout=io.StringIO()
        with contextlib.redirect_stdout(stdout), self.assertRaises(SystemExit) as raised:
            cli.main(['--version'])
        self.assertEqual(raised.exception.code,0)
        self.assertEqual(stdout.getvalue().strip(),__version__)

    def test_scan_and_check(self):
        orig=Path.read_text
        def guarded(path,*a,**kw):
            self.assertNotIn('creds.sh',str(path)); return orig(path,*a,**kw)
        original_open = builtins.open
        def guarded_open(path, *args, **kwargs):
            self.assertNotIn('creds.sh', str(path))
            return original_open(path, *args, **kwargs)
        with patch.object(Path,'read_text',guarded), patch.object(builtins, 'open', guarded_open):
            self.assertEqual(cli.main(['scan','--repo',str(self.repo),'--out',str(self.out),'--exclude','auth/**']),0)
        facts=json.loads((self.out/'codefacts.json').read_text(encoding='utf8'))
        self.assertEqual(facts['routes'][0]['prefixed_path'],'/v1/api/items/{id}')
        self.assertEqual(facts['models'][0]['columns'],['id'])
        self.assertTrue(cli.check_one({'kind':'route','value':'POST /flask/things'},facts)['found'])
        self.assertEqual(facts['migration_chain'],['abc123', 'def456'])
        self.assertEqual({m['table'] for m in facts['models']}, {'widgets', 'gadgets'})
        markers = [json.loads(line) for line in (self.out/'markers.jsonl').read_text(encoding='utf8').splitlines()]
        self.assertTrue({'TODO', 'RAISE'}.issubset({m['kind'] for m in markers}))
        self.assertIn('bad.py',(self.out/'errors.jsonl').read_text(encoding='utf8'))
        checks={'path':'app.py','symbol':'Widget','route':'POST /v1/api/items/<item>','env':'HOST_NAME','table':'widgets','test':'test_widget','dep':'some_lib','tf':'aws_instance.web','text':'APIRouter','migration':'abc'}
        for kind,value in checks.items():
            self.assertTrue(cli.check_one({'kind':kind,'value':value},facts)['found'],kind)
            self.assertFalse(cli.check_one({'kind':kind,'value':'missing_zzz'},facts)['found'],kind)
        self.assertIn('Widget',cli.check_one({'kind':'symbol','value':'Widgets'},facts)['near'])
        self.assertIn('widgets',cli.check_one({'kind':'table','value':'widgetss'},facts)['near'])
        self.assertIn('HOST_NAME',cli.check_one({'kind':'env','value':'HOST_NAM'},facts)['near'])
        first={p.name:p.read_bytes() for p in self.out.iterdir()}
        cli.main(['scan','--repo',str(self.repo),'--out',str(self.out),'--exclude','auth/**'])
        self.assertEqual(first,{p.name:p.read_bytes() for p in self.out.iterdir()})
        self.assertLessEqual(len(cli.markdown(facts,[],250)),875)
    def test_markdown_cap_and_priority(self):
        facts, markers, _ = cli.scan(self.repo, ['auth/**'])
        facts['dependencies'] = [{'specifier': 'dependency-' + str(i)} for i in range(500)]
        md = cli.markdown(facts, markers, 250)
        self.assertLessEqual(len(md), 875)
        self.assertLess(md.index('## Git snapshot'), md.index('## Routes'))
        self.assertLess(md.index('## Routes'), md.index('## ORM tables'))
        self.assertIn('more (codefacts.json', md)

    def test_independent_redaction_contract(self):
        from codeclaims import redact
        # Behavioral expectations are local: the tool and its tests must work
        # when installed or copied without either of the other public tools.
        samples = {
            'private_key': '-----BEGIN ' + 'PRIVATE KEY-----\nsynthetic\n-----END ' + 'PRIVATE KEY-----',
            'anthropic_key': 'sk-' + 'ant-' + 'a' * 24,
            'openai_key': 'sk-' + 'proj-' + 'b' * 24,
            'github_token': 'ghp' + '_' + 'c' * 36,
            'aws_key': 'AK' + 'IA' + 'A' * 16,
            'azure_conn': 'Account' + 'Key=' + 'd' * 24,
            'stripe_key': 'sk_' + 'test_' + 'e' * 20,
            'jwt': 'ey' + 'J' + 'f' * 12 + '.' + 'g' * 12 + '.' + 'h' * 12,
            'conn_password': 'pw' + 'd=' + 'i' * 20,
            'bearer': 'Bear' + 'er ' + 'j' * 24,
            'account_' + 'id': 'us' + 'er-' + 'k' * 24,
            'email': 'fixture' + '@' + 'example.invalid',
        }
        self.assertEqual({kind for kind, _ in redact.PATTERNS}, set(samples))
        for kind, sample in samples.items():
            with self.subTest(kind=kind):
                text, counts = redact.redact(sample)
                self.assertEqual(text, '[REDACTED:' + kind + ']')
                self.assertIn((kind, 1), counts)
        keys = ['creator_' + 'user_id', 'creator_' + 'account_id', 'owner' + 'AccountUuid',
                'owner' + 'OrganizationUuid', 'account_' + 'id', 'user_' + 'id',
                'account' + 'Uuid', 'organization' + 'Uuid']
        self.assertEqual(set(redact.ACCOUNT_KEYS), set(keys))
        for key in keys:
            with self.subTest(key=key):
                value = 'synthetic-' + 'identifier'
                self.assertEqual(cli.clean({key: value}), {key: '[REDACTED:account_id]'})
                self.assertNotIn(value, cli.redact(key + '="' + value + '"'))
                for value in ['<sensitive-' + 'account>', '${sensitive}', 'placeholder', 'synthetic', 'null']:
                    text = key + '="' + value + '"'
                    redacted = cli.redact(text)
                    self.assertNotIn(value, redacted)
                    self.assertIn('[REDACTED:account_id]', redacted)
                    self.assertEqual(cli.redact(redacted), redacted)
                for value in ['null', 'None', '""']:
                    text = key + '=' + value
                    self.assertEqual(cli.redact(text), text)
        sha = 'b' * 40
        uuid = '12345678-' + 'abcd-4321-abcd-' + '123456789abc'
        self.assertEqual(cli.redact('auth revision ' + sha + ' request ' + uuid),
                         'auth revision ' + sha + ' request ' + uuid)

    def test_key_value_and_contextual_entropy_redaction(self):
        from codeclaims import redact
        for key in ['api_key', 'secret', 'token', 'passwd', 'client-secret']:
            for quote in ['', '"', "'"]:
                with self.subTest(key=key, quote=quote):
                    value = 'synthetic-' + 'credential'
                    text, counts = redact.redact(key + '=' + quote + value + quote)
                    self.assertNotIn(value, text)
                    self.assertIn('[REDACTED:kv_secret]', text)
                    self.assertIn(('kv_secret', 1), counts)
                    self.assertEqual(redact.redact(text)[0], text)
        value = 'AbCdEfGhIjKlMnOp' + 'QrStUvWxYz0123456789+/='
        for prefix in ['auth credential ', 'signing key ', 'secret material ']:
            with self.subTest(prefix=prefix):
                text, counts = redact.redact(prefix + value)
                self.assertEqual(text, prefix + '[REDACTED:high_entropy]')
                self.assertEqual(counts, [('high_entropy', 1)])
                self.assertEqual(redact.redact(text)[0], text)
        self.assertEqual(redact.redact('ordinary identifier ' + value)[0],
                         'ordinary identifier ' + value)
        self.assertEqual(redact.redact('auth ' + 'a' * 40)[0], 'auth ' + 'a' * 40)

    def test_env_files_are_excluded_before_opening_and_from_claims(self):
        hidden = ['.env', '.env.local', '.env.production.py',
                  'nested/.env', 'nested/.env.example.json']
        value = 'synthetic-' + 'private-material'
        repo = self.fixture('default-env-exclusion',
                            {**{p: value for p in hidden}, 'visible.py': 'value = 1\n'})
        original_read = Path.read_text
        original_open = builtins.open
        def guarded_read(path, *args, **kwargs):
            self.assertFalse(path.name.startswith('.env'))
            return original_read(path, *args, **kwargs)
        def guarded_open(path, *args, **kwargs):
            self.assertFalse(Path(path).name.startswith('.env'))
            return original_open(path, *args, **kwargs)
        with patch.object(Path, 'read_text', guarded_read), patch.object(builtins, 'open', guarded_open):
            facts, markers, errors = cli.scan(repo, [])
            self.assertEqual(facts['files'], ['visible.py'])
            for path in hidden:
                self.assertFalse(cli.check_one({'kind': 'path', 'value': path}, facts)['found'])
            # A hand-edited facts file must not bypass the text-search guard.
            facts['files'].extend(hidden)
            self.assertFalse(cli.check_one({'kind': 'text', 'value': value}, facts)['found'])
        self.assertEqual(markers, [])
        self.assertEqual(errors, [])

    def test_markdown_all_budget_boundaries_account_for_remaining_items(self):
        facts, markers, _ = cli.scan(self.repo, ['auth/**'])
        full = cli.markdown(facts, markers, 15000)
        keys = ['git', 'routes', 'models', None, 'migration_chain', 'modules',
                None, 'tests', 'dependencies', 'terraform', 'assets']
        sections = re.split(r'(?=\n## )', full)[1:]
        groups = [[line for line in section.splitlines() if line.startswith('- ')
                   and not line.startswith(('- By kind:', '- By top-level dir:'))]
                  for section in sections]
        pointers = [['codefacts.json → '+key]*len(group) if key else []
                    for key,group in zip(keys,groups)]
        pointers[3] = ['codefacts.json → env']*len(facts['env']) + [
            'codefacts.json → settings']*sum(len(x['fields']) for x in facts['settings'])
        pointers[6] = ['markers.jsonl']*len(markers)
        all_items = [item for group in groups for item in group]
        rejected = 0
        for cap in range(1, int(len(full) / 3.5) + 3):
            with self.subTest(cap=cap):
                try:
                    md = cli.markdown(facts, markers, cap)
                except ValueError as exc:
                    rejected += 1
                    self.assertIn('too small to report all omitted sections', str(exc))
                    continue
                self.assertLessEqual(len(md), int(cap * 3.5))
                shown = [line for line in md.splitlines() if line.startswith('- ')
                         and not line.startswith(('- By kind:', '- By top-level dir:'))]
                self.assertEqual(shown, all_items[:len(shown)])
                offset = 0
                expected_notices=[]
                for group,targets,section in zip(groups,pointers,sections):
                    self.assertIn(section.splitlines()[1], md)
                    present = min(max(len(shown) - offset, 0), len(group))
                    expected_notices.extend((str(count),target) for target,count
                                            in Counter(targets[present:]).items())
                    offset += len(group)
                self.assertEqual(re.findall(r'… (\d+) more \(([^)]+)\)',md),expected_notices)
        self.assertGreater(rejected, 10)

    def test_infeasible_markdown_budget_is_usage_error_before_writes(self):
        facts, _, _ = cli.scan(self.repo, ['auth/**'])
        with self.assertRaisesRegex(ValueError, 'at least .* tokens are required'):
            cli.markdown(facts, [], 10)
        output = self.root / 'infeasible-budget-output'
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            cli.main(['scan', '--repo', str(self.repo), '--out', str(output),
                      '--exclude', 'auth/**', '--max-md-tokens', '10'])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn('--max-md-tokens is too small', stderr.getvalue())
        self.assertFalse(output.exists())

    def test_crowded_markdown_counts_fixed_limits_and_budget_omissions(self):
        repo=self.fixture('crowded-report',{
            'app.py':'\n'.join(f'# TODO: synthetic marker {i}' for i in range(70)),
            'settings.py':'class Config(BaseSettings):\n    FIRST: str\n    SECOND: str\n',
            'requirements.txt':'visible-package>=1\n',
        })
        facts,markers,_=cli.scan(repo,[])
        facts['git'].update(branch='main',head='abc123',total_commits=50,
            commits=[{'date':'2026-01-01','sha':f'c{i:06d}',
                      'subject':f'synthetic commit {i}'} for i in range(50)])
        full=cli.markdown(facts,markers,15000)
        self.assertIn('## Git snapshot (0 dirty files, 50 recent commits)',full)
        self.assertIn('## Markers (70)',full)
        self.assertIn('## Env and settings (0 env vars, 2 settings fields)',full)
        self.assertIn('… 35 more (codefacts.json → git.commits)',full)
        self.assertIn('… 30 more (markers.jsonl)',full)
        self.assertIn('- By kind: TODO: 70',full)
        self.assertIn('- By top-level dir: .: 70',full)
        self.assertIn('synthetic commit 14\n',full)
        self.assertNotIn('synthetic commit 15\n',full)
        self.assertIn('synthetic marker 39\n',full)
        self.assertNotIn('synthetic marker 40\n',full)
        self.assertIn('- visible-package>=1',full)
        headings=re.findall(r'^## .*$',full,re.M)
        for cap in [250,350,500,1000,15000]:
            with self.subTest(cap=cap):
                md=cli.markdown(facts,markers,cap)
                self.assertLessEqual(len(md),int(cap*3.5))
                self.assertEqual(re.findall(r'^## .*$',md,re.M),headings)
                missing=dict((target,int(count)) for count,target in
                             re.findall(r'… (\d+) more \(([^)]+)\)',md))
                shown_commits=len(re.findall(r'^- 2026-01-01 c\d{6} ',md,re.M))
                shown_markers=len(re.findall(r'^- app.py:\d+ TODO ',md,re.M))
                self.assertEqual(shown_commits+missing.get('codefacts.json → git.commits',0),50)
                self.assertEqual(shown_markers+missing.get('markers.jsonl',0),70)
                for pointer in missing:
                    if pointer=='markers.jsonl':
                        continue
                    self.assertTrue(pointer.startswith('codefacts.json → '))
                    value=facts
                    for key in pointer.split(' → ',1)[1].split('.'):
                        value=value[key]
                    self.assertTrue(value)

    def test_nested_escaped_account_values_and_case_are_redacted(self):
        from codeclaims import redact
        key=('account'+'_id').upper()
        account_value='synthetic owner "quoted" value'
        for depth in range(4):
            with self.subTest(depth=depth):
                text=json.dumps({key:account_value})
                for _ in range(depth): text=json.dumps({'arguments':text})
                safe,counts=redact.redact(text)
                self.assertNotIn('synthetic owner',safe)
                self.assertIn(('account_id',1),counts)
                value=json.loads(safe)
                for _ in range(depth): value=json.loads(value['arguments'])
                self.assertEqual(value[key],'[REDACTED:account_id]')
                self.assertEqual(redact.redact(safe)[0],safe)
        self.assertEqual(redact.redact_object({'outer':{key:account_value}}),
                         {'outer':{key:'[REDACTED:account_id]'}})

    def test_markdown_stops_at_first_omitted_priority_item(self):
        for section in ['git-dirty', 'git-final-commit', 'routes']:
            with self.subTest(section=section):
                facts, _, _ = cli.scan(self.repo, ['auth/**'])
                facts['git'].update(branch='main', head='abc123', total_commits=1,
                                    first_date=None, last_date=None, dirty=[], commits=[])
                facts['routes'] = [{'method': 'GET', 'prefixed_path': '/kept',
                                    'handler': 'app:kept', 'location': 'app.py:1'}]
                facts['models'] = [{'table': 'lower_priority_table', 'class': 'Lower',
                                    'columns': ['id']}]
                oversized = 'oversized-' + 'x' * 2000
                if section == 'git-dirty':
                    facts['git']['dirty'] = [{'status': ' M', 'path': oversized},
                                              {'status': ' M', 'path': 'later.py'}]
                elif section == 'git-final-commit':
                    facts['git']['commits'] = [{'date': '2026-01-01', 'sha': 'abc123',
                                               'subject': oversized}]
                else:
                    facts['routes'].append({'method': 'GET', 'prefixed_path': '/' + oversized,
                                            'handler': 'app:large', 'location': 'app.py:2'})
                markdown = cli.markdown(facts, [], 220)
                key = {'routes':'routes','git-dirty':'git.dirty',
                       'git-final-commit':'git.commits'}[section]
                count = 2 if section == 'git-dirty' else 1
                self.assertLessEqual(len(markdown), 770)
                self.assertIn(f'{count} more (codefacts.json → {key})', markdown)
                self.assertIn('Branch: main', markdown)
                self.assertNotIn('oversized-', markdown)
                self.assertIn('## ORM tables (1)', markdown)
                self.assertNotIn('- lower_priority_table:', markdown)
                if section == 'routes':
                    self.assertIn('GET /kept', markdown)
                else:
                    self.assertIn('## Routes (1)', markdown)
                    self.assertNotIn('- GET /kept', markdown)

    def test_scan_artifacts_redact_directory_keys_without_losing_groups(self):
        directories = ['sk-' + 'proj-' + letter * 24 for letter in ['a', 'b']]
        directories += ['us' + 'er-' + 'c' * 24, 'fixture' + '@' + 'example.invalid']
        repo = self.fixture('private-directory-keys', {
            directory + '/source.py': '# TODO: synthetic marker\ndef visible(): pass\n'
            for directory in directories
        })
        output = self.root / 'private-directory-output'
        argv = ['scan', '--repo', str(repo), '--out', str(output)]
        with contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(cli.main(argv), 0)
        artifacts = {p.name: p.read_bytes() for p in output.iterdir()}
        visible = stdout.getvalue() + stderr.getvalue() + ''.join(
            content.decode('utf8') for content in artifacts.values())
        for directory in directories:
            self.assertNotIn(directory, visible)
        facts = json.loads(artifacts['codefacts.json'])
        self.assertEqual(len(facts['tree']), len(directories))
        self.assertEqual(sum(group['files'] for group in facts['tree'].values()), len(directories))
        self.assertEqual(len(facts['files']), len(directories))
        self.assertEqual(len(artifacts['markers.jsonl'].splitlines()), 2 * len(directories))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(argv), 0)
        self.assertEqual(artifacts, {p.name: p.read_bytes() for p in output.iterdir()})

    def test_redacted_dictionary_key_collisions_preserve_nested_values(self):
        first = 'sk-' + 'proj-' + 'd' * 24
        second = 'sk-' + 'proj-' + 'e' * 24
        original = {first: {'nested': 1}, second: {'nested': 2},
                    '[REDACTED:openai_key]': {'nested': 3},
                    '[REDACTED:openai_key]#2': {'nested': 4}}
        cleaned = cli.clean(original)
        self.assertEqual(len(cleaned), len(original))
        self.assertEqual(sorted(item['nested'] for item in cleaned.values()), [1, 2, 3, 4])
        self.assertEqual(cli.clean(cleaned), cleaned)
        self.assertEqual(cli.clean(original), cleaned)
        for secret in [first, second]:
            self.assertNotIn(secret, json.dumps(cleaned))

    def fixture(self, name, files):
        repo = self.root / name
        repo.mkdir()
        for path, content in files.items():
            target = repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding='utf8')
        return repo

    def test_hidden_excludes_are_never_opened(self):
        repo = self.fixture('hidden-excludes', {
            '.credentials/secret.py': 'planted = "must not be read"',
            '.private.py': 'planted = "must not be read"',
            '.settings.ini': 'planted = must not be read',
            'visible.py': 'def visible(): pass',
        })
        read_text = Path.read_text
        original_open = builtins.open
        def assert_allowed(path):
            self.assertFalse(any(part.startswith('.') for part in Path(path).relative_to(repo).parts), str(path))
        def guarded_read(path, *args, **kwargs):
            assert_allowed(path)
            return read_text(path, *args, **kwargs)
        def guarded_open(path, *args, **kwargs):
            assert_allowed(path)
            return original_open(path, *args, **kwargs)
        patterns = [
            ['.credentials/**', '.private.*', '.settings.*'],
            ['./.credentials/**', './.private.*', './.settings.*'],
            ['.\\.credentials\\**', '.\\.private.*', '.\\.settings.*'],
        ]
        for excludes in patterns:
            with self.subTest(excludes=excludes), patch.object(Path, 'read_text', guarded_read), patch.object(builtins, 'open', guarded_open):
                facts, _, errors = cli.scan(repo, excludes)
                self.assertEqual(facts['files'], ['visible.py'])
                self.assertEqual(errors, [])
                self.assertFalse(cli.check_one({'kind': 'text', 'value': 'planted'}, facts)['found'])
        self.assertFalse(cli.excluded('credentials/secret.py', ['.credentials/**']))
        self.assertFalse(cli.excluded('private.py', ['.private.*']))

    def test_private_and_nested_functions_contribute_routes_and_stubs(self):
        repo = self.fixture('private-functions', {'app.py': '\n'.join([
            'router = APIRouter()',
            "@router.get('/hidden')",
            'def _endpoint(): pass',
            'class _Hidden:',
            "    @router.post('/method')",
            '    async def _method(self): ...',
            'class Public:',
            '    def _private(self): pass',
            '    def visible(self): pass',
            'def outer():',
            "    @router.get('/nested')",
            '    def _inner(): ...',
            '    return _inner',
        ])})
        facts, markers, _ = cli.scan(repo, [])
        self.assertEqual({(r['method'], r['path'], r['handler']) for r in facts['routes']}, {
            ('GET', '/hidden', 'app:_endpoint'),
            ('POST', '/method', 'app:_Hidden._method'),
            ('GET', '/nested', 'app:outer._inner'),
        })
        self.assertEqual({m['enclosing'] for m in markers if m['kind'] == 'STUB'},
                         {'_endpoint', '_Hidden._method', 'Public._private', 'Public.visible', 'outer._inner'})
        module = facts['modules'][0]
        self.assertEqual([f['name'] for f in module['functions']], ['outer'])
        self.assertEqual([c['name'] for c in module['classes']], ['Public'])
        self.assertEqual([m['name'] for m in module['classes'][0]['methods']], ['visible'])

    def test_regex_markers_have_qualified_ast_enclosures(self):
        repo=self.fixture('nested-markers',{'nested.py':'\n'.join([
            '# TODO: module scope',
            'class Outer:',
            '    # FIXME: class scope',
            '    value = 1',
            '    def method(self):',
            '        # TODO: method scope',
            '        class Inner:',
            '            # XXX: inner class scope',
            '            @decorate("TODO: decorator scope")',
            '            async def worker(self):',
            '                def nested():',
            '                    raise NotImplementedError()',
            '                return nested',
            '        return Inner',
            'def unfinished(): pass # stub',
        ])})
        _,markers,_=cli.scan(repo,[])
        by_line={int(m['location'].rsplit(':',1)[1]):m['enclosing']
                 for m in markers if m['kind']!='STUB'}
        self.assertEqual(by_line,{1:'',3:'Outer',6:'Outer.method',
            8:'Outer.method.Inner',9:'Outer.method.Inner.worker',
            12:'Outer.method.Inner.worker.nested',15:'unfinished'})

    def test_markdown_reports_per_file_and_total_test_counts(self):
        repo = self.fixture('test-totals', {
            'test_first.py': 'def test_one(): pass',
            'second_test.py': 'class Cases:\n    def test_two(self): pass\n    def test_three(self): pass',
        })
        facts, markers, _ = cli.scan(repo, [])
        markdown = cli.markdown(facts, markers, 15000)
        self.assertIn('## Tests (3 total in 2 files)', markdown)
        self.assertIn('- test_first.py: 1', markdown)
        self.assertIn('- second_test.py: 2', markdown)

    def test_sqlmodel_requires_explicit_table_true(self):
        repo = self.fixture('sqlmodel', {'models.py': '\n'.join([
            'class Request(SQLModel): pass',
            'class Response(SQLModel, table=False): pass',
            'class Stored(SQLModel, table=True):',
            '    id: Mapped[int]',
            'class Irrelevant(Other, table=True): pass',
            'class Explicit(SQLModel):',
            "    __tablename__ = 'explicit'",
            'class Alchemy(Base): pass',
            'class Declarative(orm.DeclarativeBase): pass',
            'class User(CustomBase):',
            '    id: Mapped[int]',
            'class CustomDeclarative(orm.CustomDeclarativeBase): pass',
            'class CustomRequest(CustomSQLModel): pass',
            'class CustomResponse(CustomSQLModel, table=False): pass',
            'class CustomStored(CustomSQLModel, table=True): pass',
            'class RequestBase(CustomSQLModelBase): pass',
            'class ResponseBase(CustomSQLModelBase, table=False): pass',
            'class StoredBase(CustomSQLModelBase, table=True): pass',
        ])})
        facts, _, _ = cli.scan(repo, [])
        self.assertEqual({model['class'] for model in facts['models']},
                         {'Stored', 'Explicit', 'Alchemy', 'Declarative',
                          'User', 'CustomDeclarative', 'CustomStored', 'StoredBase'})
        self.assertEqual(next(m['columns'] for m in facts['models'] if m['class'] == 'Stored'), ['id'])
        self.assertEqual(next(m['columns'] for m in facts['models'] if m['class'] == 'User'), ['id'])
        self.assertTrue(cli.check_one({'kind': 'table', 'value': 'User'}, facts)['found'])

    def test_scoped_router_aliases_nested_and_multiple_registrations(self):
        repo = self.fixture('routers', {
            'api/__init__.py': '',
            'api/users.py': '\n'.join(["router = APIRouter(prefix='/users')", "@router.get('/{id}')", 'def user(): pass', '']),
            'api/admin.py': '\n'.join(["router = APIRouter(prefix='/admins')", "@router.post('/{id}')", 'def admin(): pass', '']),
            'api/mount.py': '\n'.join([
                'from .users import router as users',
                'from . import admin',
                "outer = APIRouter(prefix='/outer')",
                "outer.include_router(users, prefix='/active')",
                "outer.include_router(admin.router, prefix='/staff')",
            ]),
            'main.py': '\n'.join([
                'from api.mount import outer',
                'import api.users as users_module',
                'app = FastAPI()',
                "app.include_router(outer, prefix='/v1')",
                "app.include_router(outer, prefix='/v2')",
                "app.include_router(users_module.router, prefix='/direct')",
            ]),
        })
        facts, _, _ = cli.scan(repo, [])
        paths = {r['prefixed_path'] for r in facts['routes']}
        self.assertEqual(paths, {'/v1/outer/active/users/{id}', '/v2/outer/active/users/{id}',
                                 '/direct/users/{id}', '/v1/outer/staff/admins/{id}',
                                 '/v2/outer/staff/admins/{id}'})
        self.assertTrue(all(r['prefix_resolved'] for r in facts['routes']))
        self.assertFalse(cli.check_one({'kind': 'route', 'value': 'GET /v1/outer/staff/users/:id'}, facts)['found'])
        for path in paths:
            self.assertTrue(cli.check_one({'kind': 'route', 'value': path.replace('{id}', '<item>')}, facts)['found'])

    def test_unknown_prefixes_cycles_and_blueprint_registration(self):
        repo = self.fixture('prefixes', {'app.py': '\n'.join([
            "bp = Blueprint('bp', __name__, url_prefix='/default')",
            'app = Flask(__name__)',
            "app.register_blueprint(bp, url_prefix='/override')",
            "app.register_blueprint(bp, url_prefix='/other')",
            "@bp.route('/items', methods=['GET', 'POST'])",
            'def items(): pass',
            'dynamic = APIRouter(prefix=PREFIX)',
            "@dynamic.get('/dynamic')",
            'def unknown(): pass',
            "first = APIRouter(prefix='/first')",
            "second = APIRouter(prefix='/second')",
            'first.include_router(second)',
            'second.include_router(first)',
            "@first.get('/cycle')",
            'def cyclic(): pass',
        ])})
        facts, _, _ = cli.scan(repo, [])
        registered = [r for r in facts['routes'] if r['path'] == '/items']
        self.assertEqual({(r['method'], r['prefixed_path']) for r in registered},
                         {(method, prefix + '/items') for method in ['GET', 'POST'] for prefix in ['/override', '/other']})
        self.assertTrue(all(r['prefix_resolved'] for r in registered))
        self.assertFalse(next(r['prefix_resolved'] for r in facts['routes'] if r['path'] == '/dynamic'))
        self.assertFalse(next(r['prefix_resolved'] for r in facts['routes'] if r['path'] == '/cycle'))

    def test_configured_alembic_locations_and_branch_chain(self):
        repo = self.fixture('custom-migrations', {
            'alembic.ini': '[alembic]\nscript_location = db/upgrade\n',
            'db/upgrade/first.py': '"""Create records"""\nrevision = "start"\ndown_revision = None\n',
            'db/upgrade/versions/left.py': 'revision: str = "left"\ndown_revision = "start"\n',
            'db/upgrade/versions/right.py': 'revision = "right"\ndown_revision = "start"\n',
            'db/upgrade/env.py': 'config = None\n',
            'unrelated/versions/no.py': 'revision = "unrelated"\ndown_revision = None\n',
            'config/alembic.ini': '[alembic]\nscript_location = unused\nversion_locations = %(here)s/revisions\n',
            'config/revisions/extra.py': 'revision = "extra"\ndown_revision = None\n',
        })
        facts, _, _ = cli.scan(repo, [])
        self.assertEqual({m['revision'] for m in facts['migrations']}, {'start', 'left', 'right', 'extra'})
        self.assertLess(facts['migration_chain'].index('start'), facts['migration_chain'].index('left'))
        self.assertLess(facts['migration_chain'].index('start'), facts['migration_chain'].index('right'))
        self.assertTrue(facts['migration_branches'])
        self.assertTrue(cli.check_one({'kind': 'migration', 'value': 'Create records'}, facts)['found'])

    def test_git_queries_leave_index_and_hook_unmodified(self):
        repo = self.fixture('git-safety', {
            '.git/HEAD': 'ref: refs/heads/main\n',
            '.git/config': '[core]\nrepositoryformatversion = 0\nbare = false\n' +
                           'fsmonitor = echo invoked > hook-invoked\n' +
                           '[filter "fixture"]\nclean = echo invoked > filter-invoked\n' +
                           '[filter "stream"]\nprocess = echo invoked > process-invoked\n',
            '.gitattributes': 'source.py filter=fixture\n',
            'source.py': 'raise RuntimeError("must never execute")\n',
        })
        (repo / '.git/objects').mkdir()
        (repo / '.git/refs/heads').mkdir(parents=True)
        # Hand-write an indexed blob with deliberately stale stat information.
        # A normal Git status refreshes this index even though the file's
        # content is unchanged. No Git mutation commands are used by tests.
        content = (repo / 'source.py').read_bytes()
        def store(kind, payload):
            raw = kind.encode() + b' ' + str(len(payload)).encode() + b'\0' + payload
            object_id = hashlib.sha1(raw).digest()
            hex_id = object_id.hex()
            object_path = repo / '.git/objects' / hex_id[:2] / hex_id[2:]
            object_path.parent.mkdir(exist_ok=True)
            object_path.write_bytes(zlib.compress(raw))
            return object_id
        object_id = store('blob', content)
        filename = b'source.py'
        entry = struct.pack('!10I20sH', 0, 0, 0, 0, 0, 0, 0o100644, 0, 0,
                            len(content), object_id, len(filename)) + filename + b'\0'
        entry += b'\0' * (-len(entry) % 8)
        index = struct.pack('!4sII', b'DIRC', 2, 1) + entry
        (repo / '.git/index').write_bytes(index + hashlib.sha1(index).digest())
        snapshot = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in repo.rglob('*') if p.is_file()}
        self.assertEqual(cli.gitfacts(repo)['branch'], 'main')
        cli.scan(repo, [])
        self.assertEqual(snapshot, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in repo.rglob('*') if p.is_file()})

        self.assertFalse((repo / 'hook-invoked').exists())
        self.assertFalse((repo / 'filter-invoked').exists())
        self.assertIn({'status': 'A ', 'path': 'source.py'}, cli.gitfacts(repo)['dirty'])
        # A process filter is a second independent executable path in status.
        (repo / '.gitattributes').write_text('source.py filter=stream\n', encoding='utf8')
        snapshot = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in repo.rglob('*') if p.is_file()}
        cli.scan(repo, [])
        self.assertEqual(snapshot, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in repo.rglob('*') if p.is_file()})
        self.assertFalse((repo / 'process-invoked').exists())
        # Also exercise a real HEAD tree, rather than only an unborn branch.
        tree_id = store('tree', b'100644 source.py\0' + object_id)
        identity = b'Fixture <fixture' + b'@' + b'example.invalid> 1 +0000\n'
        commit_id = store('commit', b'tree ' + tree_id.hex().encode() + b'\n' +
                          b'author ' + identity + b'committer ' + identity + b'\nSynthetic commit\n')
        (repo / '.git/refs/heads/main').write_text(commit_id.hex() + '\n', encoding='ascii')
        snapshot = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in repo.rglob('*') if p.is_file()}
        self.assertNotIn('source.py', [item['path'] for item in cli.gitfacts(repo)['dirty']])
        self.assertEqual(snapshot, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in repo.rglob('*') if p.is_file()})
        (repo / 'source.py').write_bytes(content + b'# changed\n')
        snapshot = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in repo.rglob('*') if p.is_file()}
        self.assertIn({'status': ' M', 'path': 'source.py'}, cli.gitfacts(repo)['dirty'])
        self.assertEqual(snapshot, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in repo.rglob('*') if p.is_file()})

    def test_real_git_only_receives_eligible_worktree_copies(self):
        files = {
            'visible.py': 'value = 1\n', 'nested/visible.py': 'value = 2\n',
            'auth/creds.sh': 'synthetic credential\n',
            'nested/keys/secret.txt': 'synthetic credential\n',
            '.env.local': 'synthetic environment\n',
            'nested/.env': 'synthetic environment\n',
            '.gitattributes': '*.py filter=fixture\n',
            'nested/.gitattributes': '*.py filter=fixture\n',
            '.gitignore': 'ignored.py\n', 'nested/.gitignore': 'ignored.py\n',
        }
        repo, _ = self.indexed_fixture('eligible-status', files)
        (repo / 'visible.py').write_text('value = 3\n', encoding='utf8')
        (repo / 'new.py').write_text('value = 4\n', encoding='utf8')
        with (repo / '.git/config').open('a', encoding='ascii') as config:
            config.write('[filter "fixture"]\nclean = echo invoked > filter-invoked\n')
        patterns = ['auth/**', 'nested/keys/**', '.gitattributes', '**/.gitattributes',
                    '.gitignore', '**/.gitignore']
        original_run = subprocess.run
        observed = []

        def inspect_and_run(command, **kwargs):
            args = command[command.index('-C') + 2:]
            if args[0] == 'status':
                mirror = Path(kwargs['env']['GIT_WORK_TREE'])
                observed.append(mirror)
                self.assertNotEqual(mirror, repo)
                self.assertEqual(set(p.relative_to(mirror).as_posix() for p in mirror.rglob('*') if p.is_file()),
                                 {'visible.py', 'nested/visible.py', 'new.py'})
                self.assertNotIn(str(repo), command)
            return original_run(command, **kwargs)

        with patch.object(cli.subprocess, 'run', inspect_and_run):
            facts, _, _ = cli.scan(repo, patterns)
        self.assertEqual(facts['git']['dirty'], [{'status': ' M', 'path': 'visible.py'},
                                                {'status': '??', 'path': 'new.py'}])
        self.assertEqual(len(observed), 1)
        self.assertFalse(observed[0].exists())
        self.assertFalse((repo / 'filter-invoked').exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows denies reads across both Python and Git processes')
    def test_excluded_files_are_os_locked_during_real_scan_process(self):
        import ctypes
        from ctypes import wintypes
        files = {'visible.py': 'value = 1\n', 'auth/creds.sh': 'synthetic secret\n',
                 'nested/keys/secret.txt': 'synthetic secret\n', '.env.test': 'synthetic env\n',
                 '.gitattributes': '*.py filter=fixture\n',
                 'nested/.gitattributes': '*.py filter=fixture\n',
                 '.gitignore': 'ignored.py\n', 'nested/.gitignore': 'ignored.py\n',
                 'templates/view.py': 'asset content must never be read\n',
                 'nested/static/app.js': 'asset content must never be read\n',
                 'static/data.json': 'asset content must never be read\n'}
        repo, _ = self.indexed_fixture('locked-status', files)
        with (repo / '.git/config').open('a', encoding='ascii') as config:
            config.write('[include]\npath = ../auth/creds.sh\n')
        (repo / 'visible.py').write_text('value = 2\n', encoding='utf8')
        (repo / 'nested/static/app.js').write_text('changed size\n', encoding='utf8')
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        create = kernel.CreateFileW
        create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                           wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        create.restype = wintypes.HANDLE
        close = kernel.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close.restype = wintypes.BOOL
        handles = []
        patterns = ['auth/**', 'nested/keys/**', '.gitattributes', '**/.gitattributes',
                    '.gitignore', '**/.gitignore']
        try:
            for filename in files:
                if filename == 'visible.py': continue
                handle = create(str(repo / filename), 0x80000000, 0, None, 3, 0, None)
                self.assertNotEqual(handle, wintypes.HANDLE(-1).value, ctypes.get_last_error())
                handles.append(handle)
            # This child proves the OS prohibition also applies outside Python
            # mocks; Git inherits the same inability to consume these bytes.
            probe = subprocess.run([sys.executable, '-c',
                'import pathlib,sys\nfor p in sys.argv[1:]:\n try: pathlib.Path(p).read_bytes()\n except PermissionError: continue\n raise SystemExit("read was not denied")',
                *[str(repo / path) for path in files if path != 'visible.py']], capture_output=True, timeout=20)
            self.assertEqual(probe.returncode, 0, probe.stderr)
            output = self.root / 'locked-status-output'
            command = [sys.executable, '-m', 'codeclaims', 'scan', '--repo', str(repo), '--out', str(output)]
            for pattern in patterns: command += ['--exclude', pattern]
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            facts = json.loads((output / 'codefacts.json').read_text(encoding='utf8'))
            self.assertEqual(facts['git']['dirty'], [{'status': ' M', 'path': 'nested/static/app.js'},
                                                   {'status': ' ?', 'path': 'static/data.json'},
                                                   {'status': ' ?', 'path': 'templates/view.py'},
                                                   {'status': ' M', 'path': 'visible.py'}])
            self.assertEqual(set(facts['files']), {'visible.py', 'templates/view.py',
                                                  'nested/static/app.js', 'static/data.json'})
            self.assertEqual({asset['path']: asset['bytes'] for asset in facts['assets']},
                             {path: (repo / path).stat().st_size for path in files if cli.asset_path(path)})
            self.assertEqual((output / 'errors.jsonl').read_text(encoding='utf8'), '')
        finally:
            for handle in handles: close(handle)

    def test_asset_status_uses_metadata_without_python_or_git_content_reads(self):
        files = {'visible.py': 'value = 1\n',
                 'templates/clean.py': 'base\n', 'templates/same-size.txt': 'base\n',
                 'templates/deleted.txt': 'base\n', 'templates/staged.txt': 'base\n',
                 'nested/static/changed.js': 'base\n'}
        repo, _ = self.indexed_fixture('asset-status', files)
        (repo / 'nested/static/changed.js').write_text('changed size\n', encoding='utf8')
        (repo / 'templates/same-size.txt').write_text('edit\n', encoding='utf8')
        (repo / 'templates/deleted.txt').unlink()
        (repo / 'templates/new.txt').write_text('untracked asset\n', encoding='utf8')
        # Stage the existing visible blob for one asset using hand-written index
        # bytes. No git add/commit/update-index or repository code is executed.
        index_path = repo / '.git/index'
        data = bytearray(index_path.read_bytes())
        entry = data.index(b'templates/staged.txt\0') - 62
        content = files['visible.py'].encode()
        data[entry + 36:entry + 40] = struct.pack('!I', len(content))
        data[entry + 40:entry + 60] = hashlib.sha1(b'blob ' + str(len(content)).encode() + b'\0' + content).digest()
        data[-20:] = hashlib.sha1(data[:-20]).digest()
        index_path.write_bytes(data)
        (repo / 'templates/staged.txt').write_bytes(content)
        original_open = builtins.open
        original_io_open = io.open
        original_run = subprocess.run
        observed = []

        def guard(original):
            def guarded(path, *args, **kwargs):
                if isinstance(path, (str, os.PathLike)):
                    resolved = Path(path).resolve()
                    if repo in resolved.parents:
                        self.assertFalse(cli.asset_path(resolved.relative_to(repo)), str(path))
                return original(path, *args, **kwargs)
            return guarded

        def inspect_and_run(command, **kwargs):
            args = command[command.index('-C') + 2:]
            if args[0] == 'status':
                mirror = Path(kwargs['env']['GIT_WORK_TREE'])
                observed.append(mirror)
                self.assertFalse(any(cli.asset_path(path.relative_to(mirror)) for path in mirror.rglob('*')))
            return original_run(command, **kwargs)

        with patch.object(builtins, 'open', guard(original_open)), patch.object(io, 'open', guard(original_io_open)), \
                patch.object(cli.subprocess, 'run', inspect_and_run):
            facts, _, errors = cli.scan(repo, [])
        self.assertEqual(errors, [])
        self.assertEqual(facts['git']['dirty'], [
            {'status': ' M', 'path': 'nested/static/changed.js'},
            {'status': ' ?', 'path': 'templates/clean.py'},
            {'status': ' D', 'path': 'templates/deleted.txt'},
            {'status': ' ?', 'path': 'templates/same-size.txt'},
            {'status': 'M?', 'path': 'templates/staged.txt'},
            {'status': '??', 'path': 'templates/new.txt'}])
        self.assertTrue(observed)
        self.assertEqual(set(asset['path'] for asset in facts['assets']),
                         (set(files) - {'visible.py', 'templates/deleted.txt'}) | {'templates/new.txt'})
        self.assertTrue(facts['git']['status_notes'])
        markdown = cli.markdown(facts, [], 15000)
        self.assertIn('6 dirty or possible files', markdown)
        self.assertIn('Possible (asset contents not read):  ? templates/same-size.txt', markdown)
        self.assertIn('Dirty: ?? templates/new.txt', markdown)
        self.assertEqual(facts['git'], cli.gitfacts(repo))

    def test_unknown_asset_index_membership_never_asserts_untracked_or_clean(self):
        repo, _ = self.indexed_fixture('unknown-asset-index', {'static/known.txt': 'base\n'})
        (repo / 'static/new.txt').write_text('new\n', encoding='utf8')
        with patch.object(cli, 'index_asset_metadata', return_value=({}, set(), False)):
            self.assertEqual(cli.gitfacts(repo)['dirty'], [
                {'status': ' ?', 'path': 'static/known.txt'},
                {'status': ' ?', 'path': 'static/new.txt'}])

    def test_staged_asset_deletion_and_untracked_replacement_are_both_reported(self):
        repo, _ = self.indexed_fixture('asset-deletion-replacement', {'templates/replaced.txt': 'base\n'})
        index = struct.pack('!4sII', b'DIRC', 2, 0)
        (repo / '.git/index').write_bytes(index + hashlib.sha1(index).digest())
        self.assertEqual(cli.gitfacts(repo)['dirty'], [
            {'status': 'D ', 'path': 'templates/replaced.txt'},
            {'status': '??', 'path': 'templates/replaced.txt'}])

    def test_zero_cached_asset_size_does_not_assert_a_modification(self):
        repo, _ = self.indexed_fixture('zero-asset-size', {'static/item.txt': 'base\n'})
        with patch.object(cli, 'index_asset_metadata', return_value=(
                {'static/item.txt': 0}, {'static/item.txt'}, True)):
            self.assertEqual(cli.gitfacts(repo)['dirty'], [{'status': ' ?', 'path': 'static/item.txt'}])

    def test_possible_submodule_asset_changes_remain_possible_in_parent(self):
        import shutil
        nested, commit = self.indexed_fixture('possible-submodule-source', {'static/item.txt': 'base\n'})
        repo, _ = self.indexed_fixture('possible-submodule-parent', {}, {'module': commit})
        shutil.copytree(nested, repo / 'module', ignore=shutil.ignore_patterns('.git'))
        shutil.copytree(nested / '.git', repo / '.git/modules/module')
        (repo / 'module/.git').write_text('gitdir: ../.git/modules/module\n', encoding='ascii')
        self.assertEqual(cli.gitfacts(repo)['dirty'], [{'status': ' ?', 'path': 'module'}])
        (repo / 'module/static/item.txt').write_text('edit\n', encoding='utf8')
        self.assertEqual(cli.gitfacts(repo)['dirty'], [{'status': ' ?', 'path': 'module'}])
        (repo / 'module/static/new.txt').write_text('new\n', encoding='utf8')
        self.assertEqual(cli.gitfacts(repo)['dirty'], [{'status': ' M', 'path': 'module'}])

    def test_text_search_rechecks_resolved_assets_and_intermediate_aliases(self):
        repo = self.fixture('text-alias-validation', {
            'ordinary/item.py': 'asset_marker = 1\n',
            'templates/item.py': 'asset_marker = 2\n',
            'static/item.py': 'asset_marker = 3\n'})
        facts, _, _ = cli.scan(repo, [])
        facts['files'] = ['ordinary/item.py']
        claim = {'kind': 'text', 'value': 'asset_marker'}
        self.assertTrue(cli.check_one(claim, facts)['found'])
        resolve = Path.resolve
        original_read = Path.read_text

        def guarded_read(path, *args, **kwargs):
            self.fail('an aliased text candidate must never be opened: ' + str(path))

        for folder in ('templates', 'static'):
            def alias(path, *args, **kwargs):
                if path == repo / 'ordinary/item.py':
                    return resolve(repo / folder / 'item.py', *args, **kwargs)
                return resolve(path, *args, **kwargs)
            with self.subTest(folder=folder), patch.object(Path, 'resolve', alias), \
                    patch.object(Path, 'read_text', guarded_read):
                self.assertFalse(cli.check_one(claim, facts)['found'])

        original_link = cli.filesystem_link
        for component in (repo.parent, repo / 'ordinary', repo / 'ordinary/item.py'):
            def linked(path):
                return path == component or original_link(path)
            with self.subTest(component=component), patch.object(cli, 'filesystem_link', linked), \
                    patch.object(Path, 'read_text', guarded_read):
                self.assertFalse(cli.check_one(claim, facts)['found'])

        # Simulate a Windows junction through its lstat attribute; this also
        # exercises every intermediate component without symlink privileges.
        original_stat = Path.lstat
        def reparse(path, *args, **kwargs):
            if path == repo / 'ordinary':
                return type('ReparseStat', (), {'st_file_attributes': 0x400,
                                              'st_mode': original_stat(path).st_mode})()
            return original_stat(path, *args, **kwargs)
        with patch.object(Path, 'lstat', reparse), patch.object(Path, 'read_text', guarded_read):
            self.assertFalse(cli.check_one(claim, facts)['found'])
        with patch.object(Path, 'read_text', original_read):
            self.assertTrue(cli.check_one(claim, facts)['found'])

    def test_text_search_skips_real_directory_link_created_after_scan(self):
        repo = self.fixture('text-real-alias', {'ordinary/item.py': 'safe = 1\n',
                                               'templates/item.py': 'hidden_asset_marker = 2\n'})
        facts, _, _ = cli.scan(repo, [])
        (repo / 'ordinary/item.py').unlink()
        (repo / 'ordinary').rmdir()
        try:
            (repo / 'ordinary').symlink_to(repo / 'templates', target_is_directory=True)
        except OSError as exc:
            if getattr(exc, 'winerror', None) == 1314:
                self.skipTest('symlink creation privilege is unavailable')
            raise
        with patch.object(Path, 'read_text', side_effect=AssertionError('asset content read')):
            self.assertFalse(cli.check_one({'kind': 'text', 'value': 'hidden_asset_marker'}, facts)['found'])

    def test_dirty_submodule_is_reported_with_nested_exclusions(self):
        nested, commit = self.indexed_fixture('submodule-source', {'tracked.py': 'value = 1\n',
                                                                 'auth/creds.sh': 'synthetic secret\n'})
        repo, _ = self.indexed_fixture('submodule-parent', {'parent.py': 'value = 1\n'}, {'module': commit})
        # All data remain synthetic; copying avoids Git clone/init/add/commit.
        import shutil
        shutil.copytree(nested, repo / 'module', ignore=shutil.ignore_patterns('.git'))
        shutil.copytree(nested / '.git', repo / '.git/modules/module')
        (repo / 'module/.git').write_text('gitdir: ../.git/modules/module\n', encoding='ascii')
        patterns = ['module/auth/**']
        self.assertEqual(cli.gitfacts(repo, patterns)['dirty'], [])
        (repo / 'module/tracked.py').write_text('value = 2\n', encoding='utf8')
        self.assertIn({'status': ' M', 'path': 'module'}, cli.gitfacts(repo, patterns)['dirty'])
        (repo / 'module/tracked.py').write_text('value = 1\n', encoding='utf8')
        (repo / 'module/new.py').write_text('value = 3\n', encoding='utf8')
        self.assertIn({'status': ' M', 'path': 'module'}, cli.gitfacts(repo, patterns)['dirty'])
        # A commit-only change still dirties the parent independently of files.
        (repo / 'module/new.py').unlink()
        different, different_commit = self.indexed_fixture('different-submodule-head', {'tracked.py': 'value = 1\n'})
        # Reuse this different repository's valid object database and refs.
        shutil.copytree(different / '.git/objects',
                        repo / '.git/modules/module/objects', dirs_exist_ok=True)
        (repo / '.git/modules/module/refs/heads/main').write_text(different_commit + '\n', encoding='ascii')
        self.assertIn({'status': ' M', 'path': 'module'}, cli.gitfacts(repo, patterns)['dirty'])

    def test_check_jsonl_verdicts(self):
        facts, _, _ = cli.scan(self.repo, ['auth/**'])
        facts_path = self.root / 'facts.json'
        input_path = self.root / 'claims.jsonl'
        output_path = self.root / 'results.jsonl'
        facts_path.write_text(json.dumps(facts), encoding='utf8')
        records = [
            {'id': 'yes', 'claims': [{'kind': 'table', 'value': 'widgets'}]},
            {'id': 'partial', 'claims': [{'kind': 'table', 'value': 'widgets'}, {'kind': 'table', 'value': 'missing'}]},
            {'id': 'no', 'claims': [{'kind': 'table', 'value': 'missing'}]},
            {'id': 'empty', 'claims': []},
        ]
        input_path.write_text(''.join(json.dumps(r) + '\n' for r in records), encoding='utf8')
        self.assertEqual(cli.main(['check', '--facts', str(facts_path), '--in', str(input_path), '--out', str(output_path)]), 0)
        self.assertEqual([json.loads(line)['verdict'] for line in output_path.read_text(encoding='utf8').splitlines()], ['verified', 'partial', 'not-found', 'n/a'])

    def assert_usage_error(self, argv):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            cli.main(argv)
        self.assertEqual(raised.exception.code, 2)
        self.assertIn('--out must', stderr.getvalue())

    def test_nested_git_source_is_rejected_before_any_content_read(self):
        repo, _ = self.indexed_fixture('nested-source-boundary', {
            'project/app.py': 'visible = 1\n',
            'project/auth/creds.sh': 'synthetic credential\n',
            'sibling.py': 'outside_requested_scope = 1\n',
        })
        nested = repo / 'project'
        output = self.root / 'nested-source-output'
        self.assertEqual(cli.repository_paths(nested)[0], repo)
        original_open = builtins.open
        original_io_open = io.open

        def guard(original):
            def opened(path, *args, **kwargs):
                if isinstance(path, (str, bytes, os.PathLike)):
                    candidate = Path(os.fsdecode(path)).absolute()
                    self.assertFalse(candidate == repo or repo in candidate.parents,
                                     'nested source content was opened: ' + str(candidate))
                return original(path, *args, **kwargs)
            return opened

        with patch.object(builtins, 'open', guard(original_open)), \
                patch.object(io, 'open', guard(original_io_open)), \
                patch.object(cli.os, 'walk', side_effect=AssertionError('source walk')), \
                patch.object(cli.subprocess, 'run', side_effect=AssertionError('Git ran')):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
                cli.main(['scan', '--repo', str(nested), '--out', str(output),
                          '--exclude', 'auth/**'])
            self.assertEqual(raised.exception.code, 2)
            self.assertIn('nested --repo directories are unsupported', stderr.getvalue())
            # Direct package callers must obey the same boundary before Git
            # can copy credentials with incorrectly rooted exclusion patterns.
            for operation in (
                    lambda: cli.scan(nested, ['auth/**']),
                    lambda: cli.gitfacts(nested, ['auth/**']),
                    lambda: cli.git_status(nested, ['status', '--porcelain'], ['auth/**']),
                    lambda: cli.git(nested, ['rev-parse', 'HEAD'])):
                with self.subTest(operation=operation), self.assertRaisesRegex(ValueError, 'worktree root'):
                    operation()
        self.assertFalse(output.exists())

    def test_nested_git_facts_cannot_write_anywhere_in_parent_worktree(self):
        repo, _ = self.indexed_fixture('nested-output-boundary', {
            'project/app.py': 'visible = 1\n',
            'sibling.py': 'unchanged = 1\n',
        })
        nested = repo / 'project'
        facts = self.root / 'nested-output-facts.json'
        # Old facts may identify a nested directory. Reject them before claims
        # are read, even when the requested output is outside the worktree.
        facts.write_text(json.dumps({'repo': str(nested)}), encoding='utf8')
        snapshot = {p: p.read_bytes() for p in repo.rglob('*') if p.is_file()}
        targets = (repo / 'results.jsonl', repo / 'sibling.py',
                   nested / 'results.jsonl', self.root / 'nested-external-results.jsonl')
        for target in targets:
            stderr = io.StringIO()
            with self.subTest(target=target), contextlib.redirect_stderr(stderr), \
                    patch.object(cli, 'check_one') as check, self.assertRaises(SystemExit) as raised:
                cli.main(['check', '--facts', str(facts), '--in',
                          str(self.root / 'absent-nested-claims.jsonl'), '--out', str(target)])
            self.assertEqual(raised.exception.code, 2)
            self.assertIn('nested --repo directories are unsupported', stderr.getvalue())
            check.assert_not_called()
        self.assertEqual(snapshot, {p: p.read_bytes() for p in repo.rglob('*') if p.is_file()})
        self.assertFalse(targets[-1].exists())
        # Output validation independently protects the complete worktree for
        # callers that supply a nested repository identity directly.
        for target in targets[:-1]:
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                cli.output_path(target, nested, cli.argparse.ArgumentParser())
            self.assertEqual(raised.exception.code, 2)

    def test_own_git_marker_cannot_bypass_enclosing_worktree_boundary(self):
        parent, _ = self.indexed_fixture('enclosing-worktree', {
            'sibling.py': 'unchanged = 1\n',
            'auth/creds.sh': 'synthetic parent credential\n',
        })
        nested, _ = self.indexed_fixture('enclosing-worktree/nested', {
            'app.py': 'visible = 1\n',
            'auth/creds.sh': 'synthetic nested credential\n',
        })
        self.assertTrue((parent / '.git/index').is_file())
        self.assertTrue((nested / '.git/index').is_file())
        # Metadata resolution still finds the inner repository for parent-root
        # scans; source/output authorization must consider every enclosing root.
        self.assertEqual(cli.repository_paths(nested)[0], nested)
        self.assertEqual(cli.worktree_root(nested), parent)
        facts = self.root / 'enclosed-source-facts.json'
        facts.write_text(json.dumps({'repo': str(nested), 'exclude': ['auth/**']}), encoding='utf8')
        external = self.root / 'enclosed-source-output'
        targets = (parent / 'generated', parent / 'sibling.py',
                   parent / 'auth/results.jsonl', nested / 'generated', external)
        snapshot = {p: p.read_bytes() for p in parent.rglob('*') if p.is_file()}
        original_open, original_io_open = builtins.open, io.open

        def guard(original):
            def opened(path, *args, **kwargs):
                if isinstance(path, (str, bytes, os.PathLike)):
                    candidate = Path(os.fsdecode(path)).resolve()
                    self.assertFalse(candidate == parent or parent in candidate.parents,
                                     'enclosed source content was opened: ' + str(candidate))
                return original(path, *args, **kwargs)
            return opened

        with patch.object(builtins, 'open', guard(original_open)), \
                patch.object(io, 'open', guard(original_io_open)), \
                patch.object(cli.os, 'walk', side_effect=AssertionError('source walk')), \
                patch.object(cli.subprocess, 'run', side_effect=AssertionError('Git ran')), \
                patch.object(cli, 'check_one', side_effect=AssertionError('claim processing')):
            for target in targets:
                commands = (
                    ['scan', '--repo', str(nested), '--out', str(target), '--exclude', 'auth/**'],
                    ['check', '--facts', str(facts), '--in', str(self.root / 'absent-enclosed-claims.jsonl'),
                     '--out', str(target)],
                )
                for command in commands:
                    stderr = io.StringIO()
                    with self.subTest(command=command), contextlib.redirect_stderr(stderr), \
                            self.assertRaises(SystemExit) as raised:
                        cli.main(command)
                    self.assertEqual(raised.exception.code, 2)
                    self.assertIn('nested --repo directories are unsupported', stderr.getvalue())
            for operation in (
                    lambda: cli.scan(nested, ['auth/**']),
                    lambda: cli.gitfacts(nested, ['auth/**']),
                    lambda: cli.git_status(nested, ['status', '--porcelain'], ['auth/**']),
                    lambda: cli.git(nested, ['rev-parse', 'HEAD'])):
                with self.subTest(operation=operation), self.assertRaisesRegex(ValueError, 'worktree root'):
                    operation()
            for target in targets[:-1]:
                with self.subTest(output=target), contextlib.redirect_stderr(io.StringIO()), \
                        self.assertRaises(SystemExit) as raised:
                    cli.output_path(target, nested, cli.argparse.ArgumentParser())
                self.assertEqual(raised.exception.code, 2)
        self.assertFalse(external.exists())
        self.assertFalse((parent / 'generated').exists())
        self.assertEqual(snapshot, {p: p.read_bytes() for p in parent.rglob('*') if p.is_file()})

    def test_scan_refuses_outputs_in_source_before_any_work(self):
        snapshot = {p: p.read_bytes() for p in self.repo.rglob('*') if p.is_file()}
        targets = [self.repo, self.repo / 'generated' / 'nested',
                   self.repo / 'auth' / 'output', self.repo / 'new' / '..' / 'output']
        for target in targets:
            with self.subTest(target=target), patch.object(cli, 'scan') as scan:
                self.assert_usage_error(['scan', '--repo', str(self.repo), '--out', str(target)])
                scan.assert_not_called()
        self.assertEqual(snapshot, {p: p.read_bytes() for p in self.repo.rglob('*') if p.is_file()})

    def test_check_refuses_outputs_in_source_without_modifying_files(self):
        facts = self.root / 'readonly-facts.json'
        facts.write_text(json.dumps({'repo': str(self.repo.resolve())}), encoding='utf8')
        snapshot = {p: p.read_bytes() for p in self.repo.rglob('*') if p.is_file()}
        for target in [self.repo, self.repo / 'app.py', self.repo / 'results.jsonl',
                       self.repo / 'new' / '..' / 'results.jsonl']:
            with self.subTest(target=target):
                # A missing input proves validation runs before claim processing.
                self.assert_usage_error(['check', '--facts', str(facts), '--in',
                                         str(self.root / 'absent-claims.jsonl'), '--out', str(target)])
        self.assertEqual(snapshot, {p: p.read_bytes() for p in self.repo.rglob('*') if p.is_file()})

    def test_check_refuses_redacted_repository_identity_before_any_work(self):
        private_name = 'fixture' + '@' + 'example.invalid'
        repo = self.fixture('redacted-source-' + private_name, {'visible.py': 'searchable_literal = 1\n'})
        output = self.root / 'redacted-source-facts'
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['scan', '--repo', str(repo), '--out', str(output)]), 0)
        facts_path = output / 'codefacts.json'
        facts_text = facts_path.read_text(encoding='utf8')
        self.assertNotIn(private_name, facts_text)
        self.assertIn('[REDACTED:email]', facts_text)
        claims = self.root / 'redacted-source-claims.jsonl'
        claims.write_text(json.dumps({'id': 'text', 'claims': [{'kind': 'text', 'value': 'searchable_literal'}]}) + '\n', encoding='utf8')
        for target in (repo / 'results.jsonl', self.root / 'redacted-source-results.jsonl'):
            stderr = io.StringIO()
            with self.subTest(target=target), contextlib.redirect_stderr(stderr), \
                    patch.object(cli, 'check_one') as check, self.assertRaises(SystemExit) as raised:
                cli.main(['check', '--facts', str(facts_path), '--in', str(claims), '--out', str(target)])
            self.assertEqual(raised.exception.code, 2)
            self.assertIn('repository path is redacted', stderr.getvalue())
            check.assert_not_called()
            self.assertFalse(target.exists())
        self.assertEqual((repo / 'visible.py').read_text(encoding='utf8'), 'searchable_literal = 1\n')

    def test_external_sibling_with_same_name_prefix_is_allowed(self):
        output = self.root / 'repo-output'
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(cli.main(['scan', '--repo', str(self.repo), '--out', str(output),
                                       '--exclude', 'auth/**']), 0)
        self.assertLessEqual(len(stdout.getvalue().splitlines()), 20)
        claims = self.root / 'external-claims.jsonl'
        claims.write_text('{"id":"empty","claims":[]}\n', encoding='utf8')
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(cli.main(['check', '--facts', str(output / 'codefacts.json'),
                                       '--in', str(claims), '--out', str(output / 'results.jsonl')]), 0)
        self.assertLessEqual(len(stdout.getvalue().splitlines()), 20)

    def test_agent_configuration_outputs_are_refused_before_work(self):
        # Patch the home root; never create fixtures under real user settings.
        home = self.root / 'synthetic-home'
        facts = self.root / 'protected-facts.json'
        facts.write_text(json.dumps({'repo': str(self.repo.resolve())}), encoding='utf8')
        with patch.object(Path, 'home', return_value=home):
            for name in ('.codex', '.claude'):
                for suffix in ('', 'logs', 'new/../sessions'):
                    target = home / name / suffix
                    with self.subTest(target=target), patch.object(cli, 'scan') as scan:
                        self.assert_usage_error(['scan', '--repo', str(self.repo), '--out', str(target)])
                        scan.assert_not_called()
                        self.assert_usage_error(['check', '--facts', str(facts), '--in', 'absent.jsonl',
                                                 '--out', str(target / 'results.jsonl')])
            # Ordinary user-selected folders, including similarly named ones,
            # remain valid output locations.
            parser = cli.argparse.ArgumentParser()
            self.assertEqual(cli.output_path(home / '.codex-output', self.repo, parser),
                             (home / '.codex-output').resolve())
        self.assertFalse(home.exists())

    def test_output_alias_into_agent_configuration_is_refused(self):
        home = self.root / 'alias-home'
        outside = self.root / 'configuration-alias'
        resolve = Path.resolve
        def alias(path, *args, **kwargs):
            if path == outside or outside in path.parents:
                return resolve(home / '.claude' / path.relative_to(outside), *args, **kwargs)
            return resolve(path, *args, **kwargs)
        with patch.object(Path, 'home', return_value=home), patch.object(Path, 'resolve', alias), patch.object(cli, 'scan') as scan:
            self.assert_usage_error(['scan', '--repo', str(self.repo), '--out', str(outside)])
            scan.assert_not_called()
        self.assertFalse(outside.exists())
        self.assertFalse(home.exists())

    def test_git_temporary_metadata_never_uses_source_or_configuration(self):
        repo = self.fixture('temporary-root', {})
        def metadata(path, args, **kwargs):
            if args[0] == 'status':
                return cli.git_status(path, args)
            if '--show-toplevel' in args: return str(repo)
            if '--show-object-format' in args: return 'sha1'
            if '--git-path' in args: return str(repo / '.git' / args[-1])
            return ''
        home = self.root / 'temporary-home'
        for temporary in (repo, repo / 'tmp', home / '.codex' / 'tmp', home / '.claude' / 'tmp'):
            with self.subTest(temporary=temporary), patch.object(Path, 'home', return_value=home), \
                    patch.object(cli.tempfile, 'gettempdir', return_value=str(temporary)), \
                    patch.object(cli.tempfile, 'TemporaryDirectory') as directory, patch.object(cli, 'git', metadata):
                self.assertEqual(cli.git_status(repo, ['status', '--porcelain']), '')
                directory.assert_not_called()

    def test_resolved_output_aliases_into_source_are_refused(self):
        # Exercise link resolution on all platforms, including Windows without
        # permission to create actual symlinks or junctions.
        resolve = Path.resolve
        outside = self.root / 'output-alias'
        def alias(path, *args, **kwargs):
            if path == outside or outside in path.parents:
                return resolve(self.repo / path.relative_to(outside), *args, **kwargs)
            return resolve(path, *args, **kwargs)
        facts = self.root / 'alias-facts.json'
        facts.write_text(json.dumps({'repo': str(self.repo.resolve())}), encoding='utf8')
        with patch.object(Path, 'resolve', alias), patch.object(cli, 'scan') as scan:
            self.assert_usage_error(['scan', '--repo', str(self.repo), '--out', str(outside)])
            scan.assert_not_called()
            self.assert_usage_error(['check', '--facts', str(facts), '--in', 'unused.jsonl',
                                     '--out', str(outside / 'results.jsonl')])
        self.assertFalse(outside.exists())

    def test_each_scan_artifact_is_validated_before_any_write(self):
        resolve = Path.resolve
        outside = self.root / 'artifact-alias'
        for filename in ['codefacts.json', 'codefacts.md', 'markers.jsonl', 'errors.jsonl']:
            def alias(path, *args, **kwargs):
                if path == outside / filename:
                    return resolve(self.repo / 'app.py', *args, **kwargs)
                return resolve(path, *args, **kwargs)
            with self.subTest(filename=filename), patch.object(Path, 'resolve', alias), patch.object(cli, 'scan') as scan:
                self.assert_usage_error(['scan', '--repo', str(self.repo), '--out', str(outside)])
                scan.assert_not_called()
                self.assertFalse(outside.exists())

    def test_real_output_symlinks_into_source_are_refused(self):
        outside = self.root / 'linked-artifacts'
        outside.mkdir()
        link = outside / 'codefacts.json'
        try:
            link.symlink_to(self.repo / 'app.py')
        except OSError as exc:
            if isinstance(exc, NotImplementedError) or getattr(exc, 'winerror', None) == 1314:
                self.skipTest('symlink creation privilege is unavailable')
            raise
        original = (self.repo / 'app.py').read_bytes()
        self.assert_usage_error(['scan', '--repo', str(self.repo), '--out', str(outside)])
        facts = self.root / 'symlink-facts.json'
        facts.write_text(json.dumps({'repo': str(self.repo.resolve())}), encoding='utf8')
        self.assert_usage_error(['check', '--facts', str(facts), '--in', 'unused.jsonl', '--out', str(link)])
        self.assertEqual((self.repo / 'app.py').read_bytes(), original)
        self.assertEqual(list(outside.iterdir()), [link])

    def test_hardlinked_output_cannot_overwrite_source(self):
        outside = self.root / 'hardlinked-artifacts'
        outside.mkdir()
        link = outside / 'codefacts.json'
        os.link(self.repo / 'app.py', link)
        original = (self.repo / 'app.py').read_bytes()
        self.assert_usage_error(['scan', '--repo', str(self.repo), '--out', str(outside)])
        facts = self.root / 'hardlink-facts.json'
        facts.write_text(json.dumps({'repo': str(self.repo.resolve())}), encoding='utf8')
        self.assert_usage_error(['check', '--facts', str(facts), '--in', 'unused.jsonl', '--out', str(link)])
        self.assertEqual((self.repo / 'app.py').read_bytes(), original)
        self.assertEqual(link.read_bytes(), original)

    def test_subprocess_allowlist(self):
        commands=[]
        repo, _ = self.indexed_fixture('allowlist-status', {'visible.py': 'value = 1\n'})
        def run(cmd,**kw):
            self.assertEqual(cmd[0], 'git')
            self.assertEqual(cmd[1:7], ['-c', 'core.fsmonitor=false', '-c',
                                       'core.hooksPath=' + os.devnull, '-c', 'core.untrackedCache=false'])
            self.assertEqual(kw['env']['GIT_OPTIONAL_LOCKS'], '0')
            self.assertEqual(kw['env']['GIT_TERMINAL_PROMPT'], '0')
            self.assertEqual(kw['env']['GIT_NO_LAZY_FETCH'], '1')
            self.assertEqual(cmd[7:9], ['-c', 'log.showSignature=false'])
            self.assertEqual(cmd[9:11], ['-c', 'core.attributesFile=' + os.devnull])
            self.assertEqual(kw['env']['GIT_CONFIG_GLOBAL'], os.devnull)
            self.assertEqual(kw['env']['GIT_CONFIG_NOSYSTEM'], '1')
            self.assertNotIn('GIT_CONFIG_COUNT', kw['env'])
            self.assertFalse(kw.get('shell', False))
            args = cmd[cmd.index('-C') + 2:]
            commands.append(args)
            if args[0] == 'status':
                self.assertNotEqual(kw['env']['GIT_DIR'], str(self.repo / '.git'))
                self.assertNotEqual(kw['env']['GIT_WORK_TREE'], str(self.repo))
                self.assertFalse((Path(kw['env']['GIT_WORK_TREE']) / 'auth').exists())
                self.assertNotIn('filter', (Path(kw['env']['GIT_DIR']) / 'config').read_text())
            result = ''
            if '--show-toplevel' in args: result = str(self.repo)
            if '--show-object-format' in args: result = 'sha1'
            if '--git-path' in args: result = str(self.repo / '.git' / args[-1])
            return type('Result',(),{'returncode':0,'stdout':result})()
        with patch.object(cli.subprocess,'run',run), patch.dict(os.environ, {'GIT_CONFIG_COUNT': '7'}):
            cli.gitfacts(repo, ['auth/**'])
        self.assertTrue(all(c[0] in {'rev-parse','branch','log','status','rev-list'} for c in commands))
        self.assertIn(['status', '--porcelain=v1', '-z', '--untracked-files=all',
                       '--ignore-submodules=dirty', '--no-renames'], commands)

if __name__=='__main__': unittest.main()
