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

"""Write-safety regressions using only synthetic homes and sessions."""
from contextlib import contextmanager
from dataclasses import replace
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from agentlog.core.out import write_outputs
from agentlog.core.paths import private_temp_directory
from agentlog.readers import claude
from agentlog.readers.base import SessionMeta


class DestinationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.area = self.root/self._testMethodName
        self.area.mkdir()
        self.home = self.area/'home'
        self.home.mkdir()
        self.codex = self.home/'.codex'
        self.claude = self.home/'.claude'
        self.codex.mkdir();self.claude.mkdir()
        self.source = self.area/'session.jsonl'
        self.source.write_text('{"role":"user","content":"synthetic"}\n', encoding='utf-8')
        self.original = self.source.read_bytes()
        self.meta = SessionMeta('synthetic-session', 'generic', str(self.source), len(self.original))
        self.addCleanup(patch.stopall)
        patch('agentlog.core.paths.Path.home', return_value=self.home).start()

    @contextmanager
    def cwd(self, path):
        before = Path.cwd()
        os.chdir(path)
        try:
            yield
        finally:
            os.chdir(before)

    def symlink(self, path, target, *, directory=False):
        try:
            path.symlink_to(target, target_is_directory=directory)
        except (NotImplementedError, OSError) as error:
            # Windows without Developer Mode may forbid creating symlinks.
            if isinstance(error, OSError) and getattr(error, 'winerror', None) != 1314:
                raise
            self.skipTest('Symlink creation unavailable')

    def report(self, out=None, meta=None, command='probe'):
        return write_outputs(command, meta or self.meta, {'synthetic':True}, 'synthetic', out)

    def test_explicit_protected_outputs_refused_before_mkdir(self):
        for root in (self.codex, self.claude):
            with self.subTest(root=root.name), self.assertRaisesRegex(ValueError, 'protected'):
                self.report(root/'new'/'report')
            self.assertEqual(list(root.iterdir()), [])
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_configured_homes_are_protected(self):
        for variable in ('AGENTLOG_CODEX_HOME', 'AGENTLOG_CLAUDE_HOME', 'CODEX_HOME', 'CLAUDE_CONFIG_DIR'):
            root = self.area/variable
            with patch.dict(os.environ, {variable:str(root)}), self.assertRaisesRegex(ValueError, 'protected'):
                self.report(root/'new')
            self.assertFalse(root.exists())

    def test_default_output_checks_cwd_and_parent_symlink(self):
        for root in (self.codex, self.claude):
            with self.cwd(root), self.assertRaisesRegex(ValueError, 'protected'):
                self.report()
            self.assertEqual(list(root.iterdir()), [])
        self.symlink(self.area/'agentlog-out', self.codex, directory=True)
        with self.cwd(self.area), self.assertRaisesRegex(ValueError, 'protected'):
            self.report()
        self.assertEqual(list(self.codex.iterdir()), [])

    def test_directory_symlink_into_protected_home_refused(self):
        alias = self.area/'alias'
        self.symlink(alias, self.claude, directory=True)
        with self.assertRaisesRegex(ValueError, 'protected'):
            self.report(alias/'new')
        self.assertEqual(list(self.claude.iterdir()), [])

    def test_protected_root_symlink_alias_is_protected(self):
        target = self.area/'configuration-target'
        target.mkdir()
        configured = self.area/'configured-home'
        self.symlink(configured, target, directory=True)
        with patch.dict(os.environ, {'AGENTLOG_CODEX_HOME':str(configured)}):
            with self.assertRaisesRegex(ValueError, 'protected'):
                self.report(target/'new')
        self.assertEqual(list(target.iterdir()), [])

    def test_input_directory_and_exact_output_file_collisions(self):
        with self.assertRaisesRegex(ValueError, 'input'):
            self.report(self.source)
        source = self.area/'probe.json'
        source.write_bytes(self.original)
        meta = replace(self.meta, path=str(source))
        with self.assertRaisesRegex(ValueError, 'input'):
            self.report(self.area, meta)
        self.assertFalse((self.area/'probe.md').exists())
        self.assertEqual(source.read_bytes(), self.original)

    def test_input_symlink_and_hardlink_collisions(self):
        out = self.area/'reports'
        out.mkdir()
        target = out/'probe.json'
        self.symlink(target, self.source)
        with self.assertRaisesRegex(ValueError, 'input'):
            self.report(out)
        self.assertFalse((out/'probe.md').exists())
        target.unlink()
        target.hardlink_to(self.source)
        with self.assertRaisesRegex(ValueError, 'input'):
            self.report(out)
        self.assertFalse((out/'probe.md').exists())
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_output_symlink_to_protected_file_refused_before_first_write(self):
        source = self.codex/'synthetic.jsonl'
        source.write_bytes(self.original)
        self.symlink(self.area/'probe.json', source)
        with self.assertRaisesRegex(ValueError, 'protected'):
            self.report(self.area)
        self.assertFalse((self.area/'probe.md').exists())
        self.assertEqual(source.read_bytes(), self.original)

    def test_other_log_links_refused(self):
        other = self.area/'other.jsonl'
        other.write_bytes(self.original)
        target = self.area/'probe.json'
        self.symlink(target, other)
        with self.assertRaisesRegex(ValueError, 'links'):
            self.report(self.area)
        target.unlink()
        target.hardlink_to(other)
        with self.assertRaisesRegex(ValueError, 'links'):
            self.report(self.area)
        self.assertFalse((self.area/'probe.md').exists())
        self.assertEqual(other.read_bytes(), self.original)

    def test_invalid_components_and_redacted_inputs_create_nothing(self):
        for command in ('../escape', 'probe:stream', 'a/b', 'a\\b'):
            with self.assertRaises(ValueError):
                self.report(self.area/'new', command=command)
        with self.cwd(self.area), self.assertRaises(ValueError):
            self.report(meta=replace(self.meta, session_id='../../x'))
        with self.assertRaisesRegex(ValueError, 'redacted'):
            self.report(self.area/'new', replace(self.meta, path='[REDACTED:email]/session.jsonl'))
        self.assertFalse((self.area/'new').exists())
        self.assertFalse((self.area/'agentlog-out').exists())

    def test_safe_default_and_explicit_reports(self):
        with self.cwd(self.area):
            default = self.report()
        explicit = self.report(self.area/'explicit')
        for out in (default, explicit):
            self.assertEqual({p.name for p in out.iterdir()}, {'probe.md', 'probe.json'})
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_safe_private_scratch_skips_protected_candidates(self):
        scratch_root = self.area/'scratch'
        scratch_root.mkdir()
        candidates = [self.codex/'new', self.claude, scratch_root]
        with patch('agentlog.core.paths.temporary_candidates', return_value=candidates):
            with private_temp_directory() as scratch:
                self.assertEqual(scratch.parent, scratch_root.resolve())
                self.assertTrue(scratch.is_dir())
                (scratch/'synthetic').write_text('synthetic', encoding='utf-8')
            self.assertFalse(scratch.exists())
        self.assertEqual(list(self.codex.iterdir()), [])
        self.assertEqual(list(self.claude.iterdir()), [])

    def test_temp_symlink_is_validated_and_no_safe_candidate_refuses(self):
        alias = self.area/'temp-alias'
        self.symlink(alias, self.codex, directory=True)
        with patch('agentlog.core.paths.temporary_candidates', return_value=[alias, self.claude, self.area/'missing']):
            with self.assertRaisesRegex(OSError, 'No safe temporary directory'):
                with private_temp_directory():
                    self.fail('Unsafe scratch directory was accepted')
        self.assertEqual(list(self.codex.iterdir()), [])
        self.assertEqual(list(self.claude.iterdir()), [])

    def test_environment_temp_paths_checked_before_creation(self):
        with patch.dict(os.environ, {'TMPDIR':str(self.codex), 'TEMP':str(self.claude), 'TMP':str(self.codex/'new')}):
            with patch('agentlog.core.paths.tempfile.tempdir', str(self.claude)):
                with private_temp_directory() as scratch:
                    self.assertFalse(scratch.is_relative_to(self.codex))
                    self.assertFalse(scratch.is_relative_to(self.claude))
            self.assertFalse(scratch.exists())
        self.assertEqual(list(self.codex.iterdir()), [])
        self.assertEqual(list(self.claude.iterdir()), [])

    def test_claude_refuses_if_no_safe_scratch_before_sqlite_connect(self):
        with patch('agentlog.core.paths.temporary_candidates', return_value=[self.codex, self.claude]), patch('agentlog.readers.claude.sqlite3.connect') as connect:
            reader = claude.read(self.source)
            next(reader)
            with self.assertRaisesRegex(OSError, 'No safe temporary directory'):
                next(reader)
            connect.assert_not_called()
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_scratch_cleanup_after_normal_completion_and_connect_error(self):
        scratch_root = self.area/'scratch'
        scratch_root.mkdir()
        with patch('agentlog.core.paths.temporary_candidates', return_value=[scratch_root]):
            list(claude.read(self.source))
            self.assertEqual(list(scratch_root.iterdir()), [])
            with patch('agentlog.readers.claude.sqlite3.connect', side_effect=sqlite3.OperationalError('synthetic failure')):
                with self.assertRaises(sqlite3.OperationalError):
                    list(claude.read(self.source))
            self.assertEqual(list(scratch_root.iterdir()), [])
