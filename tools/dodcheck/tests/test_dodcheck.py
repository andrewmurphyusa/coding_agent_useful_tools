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
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dodcheck import core  # noqa: E402
from dodcheck.cli import main  # noqa: E402

PY = f'"{sys.executable}"'


def py_check(code):
    return f'{PY} -c "{code}"'


def marker_check(marker, exit_code=0):
    return py_check(f"import sys; open('{marker}','w').write('x'); sys.exit({exit_code})")


class DodcheckTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name).resolve()
        self.root = root
        self.state = root / "state"
        self._saved = {k: os.environ.get(k) for k in ("DODCHECK_STATE_DIR", "DODCHECK_MODE")}
        os.environ["DODCHECK_STATE_DIR"] = str(self.state)
        os.environ.pop("DODCHECK_MODE", None)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def project(self, rel, **config):
        path = self.root / rel
        path.mkdir(parents=True, exist_ok=True)
        (path / ".dodcheck.json").write_text(json.dumps(config), encoding="utf-8")
        return path

    def file(self, project, rel):
        path = project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
        return path

    def call(self, command, payload=None, raw=None, args=()):
        text = raw if raw is not None else json.dumps(payload or {})
        stdin = io.TextIOWrapper(io.BytesIO(text.encode("utf-8")), encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main([command, *args], stdin=stdin)
        return code, out.getvalue()

    def rec(self, path, session="s1"):
        return self.call("record", {"session_id": session, "tool_input": {"file_path": str(path)}, "cwd": str(self.root)})

    def stop(self, session="s1", active=False):
        code, out = self.call("stop", {"session_id": session, "stop_hook_active": active, "cwd": str(self.root)})
        self.assertEqual(code, 0)
        return json.loads(out) if out.strip() else None


class RecordTests(DodcheckTestCase):
    def test_dedup_and_silent(self):
        target = self.file(self.root, "a.py")
        code, out = self.rec(target)
        self.assertEqual((code, out), (0, ""))
        self.rec(target)
        self.assertEqual(core.load_paths("s1"), [str(target)])

    def test_notebook_path_and_relative_path(self):
        self.call("record", {"session_id": "s1", "tool_input": {"notebook_path": "nb.ipynb"}, "cwd": str(self.root)})
        self.assertEqual(core.load_paths("s1"), [str(self.root / "nb.ipynb")])

    def test_garbage_stdin_exits_zero(self):
        for raw in ("not json", "", "[1,2]", "null"):
            self.assertEqual(self.call("record", raw=raw), (0, ""))

    def test_session_id_sanitised(self):
        self.rec(self.root / "a.py", session="../../evil/id")
        names = [p.name for p in self.state.iterdir()]
        self.assertEqual(len(names), 1)
        self.assertNotIn("/", names[0])

    def test_old_state_purged(self):
        self.state.mkdir()
        old = self.state / "old.json"
        old.write_text("[]")
        stamp = old.stat().st_mtime - 8 * 24 * 3600
        os.utime(old, (stamp, stamp))
        self.rec(self.root / "a.py")
        self.assertFalse(old.exists())


class GlobTests(unittest.TestCase):
    def test_patterns(self):
        cases = [
            ("a.py", "**/*.py", True), ("src/a.py", "**/*.py", True), ("src/x/a.py", "**/*.py", True),
            ("a.txt", "**/*.py", False), ("pyproject.toml", "pyproject.toml", True),
            ("src/pyproject.toml", "pyproject.toml", False), ("src/a.py", "*.py", False),
            ("src/a.py", "src/*.py", True), ("src/x/a.py", "src/**", True), ("a.py", "**", True),
            ("a.py", "?.py", True),
        ]
        for rel, pattern, expected in cases:
            with self.subTest(rel=rel, pattern=pattern):
                self.assertEqual(core.glob_match(rel, pattern), expected)


class StopTests(DodcheckTestCase):
    def test_nothing_recorded_is_silent(self):
        self.assertIsNone(self.stop())

    def test_only_edited_project_runs(self):
        a = self.project("mono/a", mode="block", checks=[{"name": "t", "run": marker_check("ran_a")}])
        b = self.project("mono/b", mode="block", checks=[{"name": "t", "run": marker_check("ran_b")}])
        self.rec(self.file(a, "src/x.py"))
        self.assertIsNone(self.stop())
        self.assertTrue((a / "ran_a").exists())
        self.assertFalse((b / "ran_b").exists())

    def test_nearest_config_wins_for_nested_projects(self):
        outer = self.project("outer", checks=[{"name": "t", "run": marker_check("ran_outer")}])
        inner = self.project("outer/inner", checks=[{"name": "t", "run": marker_check("ran_inner")}])
        self.rec(self.file(inner, "x.py"))
        self.stop()
        self.assertTrue((inner / "ran_inner").exists())
        self.assertFalse((outer / "ran_outer").exists())

    def test_path_without_project_ignored(self):
        self.rec(self.file(self.root, "loose/a.py"))
        self.assertIsNone(self.stop())
        self.assertEqual(core.load_paths("s1"), [])

    def test_watch_filter(self):
        p = self.project("p", watch=["**/*.py"], checks=[{"name": "t", "run": marker_check("ran")}])
        self.rec(self.file(p, "README.md"))
        self.assertIsNone(self.stop())
        self.assertFalse((p / "ran").exists())
        self.rec(self.file(p, "pkg/mod.py"))
        self.stop()
        self.assertTrue((p / "ran").exists())

    def test_default_watch_is_all_files(self):
        p = self.project("p", checks=[{"name": "t", "run": marker_check("ran")}])
        self.rec(self.file(p, "notes.txt"))
        self.stop()
        self.assertTrue((p / "ran").exists())

    def test_ignored_dirs(self):
        p = self.project("p", checks=[{"name": "t", "run": marker_check("ran")}])
        for name in ("node_modules", ".venv", "venv", ".git", "__pycache__", "dist", "build"):
            self.rec(self.file(p, f"{name}/sub/f.py"))
        self.assertIsNone(self.stop())
        self.assertFalse((p / "ran").exists())

    def test_pass_clears_state(self):
        p = self.project("p", mode="block", checks=[{"name": "t", "run": marker_check("ran")}])
        self.rec(self.file(p, "a.py"))
        self.assertIsNone(self.stop())
        self.assertEqual(core.load_paths("s1"), [])

    def test_block_mode_blocks_and_keeps_state(self):
        p = self.project("p", name="proj", mode="block", checks=[
            {"name": "tests", "run": py_check("print('boom line'); raise SystemExit(3)")}])
        target = self.file(p, "a.py")
        self.rec(target)
        result = self.stop()
        self.assertEqual(set(result), {"decision", "reason"})
        self.assertEqual(result["decision"], "block")
        for needle in ("proj", "tests", "exit code 3", "boom line"):
            self.assertIn(needle, result["reason"])
        self.assertTrue(result["reason"].endswith(core.BLOCK_SUFFIX))
        self.assertEqual(core.load_paths("s1"), [str(target)])

    def test_stop_hook_active_downgrades_and_clears(self):
        p = self.project("p", name="proj", mode="block", checks=[{"name": "tests", "run": marker_check("ran", 2)}])
        self.rec(self.file(p, "a.py"))
        result = self.stop(active=True)
        self.assertEqual(list(result), ["systemMessage"])
        self.assertIn("proj/tests (exit 2)", result["systemMessage"])
        self.assertEqual(core.load_paths("s1"), [])

    def test_warn_mode_default(self):
        p = self.project("p", checks=[{"name": "tests", "run": marker_check("ran", 1)}])
        self.rec(self.file(p, "a.py"))
        result = self.stop()
        self.assertTrue(result["systemMessage"].startswith("dodcheck:"))
        self.assertEqual(core.load_paths("s1"), [])

    def test_off_mode_skips(self):
        p = self.project("p", mode="off", checks=[{"name": "t", "run": marker_check("ran", 1)}])
        self.rec(self.file(p, "a.py"))
        self.assertIsNone(self.stop())
        self.assertFalse((p / "ran").exists())

    def test_env_override_warn_relaxes_block(self):
        p = self.project("p", mode="block", checks=[{"name": "t", "run": marker_check("ran", 1)}])
        self.rec(self.file(p, "a.py"))
        os.environ["DODCHECK_MODE"] = "warn"
        self.assertIn("systemMessage", self.stop())

    def test_env_override_block_tightens_warn(self):
        p = self.project("p", mode="warn", checks=[{"name": "t", "run": marker_check("ran", 1)}])
        self.rec(self.file(p, "a.py"))
        os.environ["DODCHECK_MODE"] = "block"
        self.assertEqual(self.stop()["decision"], "block")

    def test_env_override_off(self):
        p = self.project("p", mode="block", checks=[{"name": "t", "run": marker_check("ran", 1)}])
        self.rec(self.file(p, "a.py"))
        os.environ["DODCHECK_MODE"] = "off"
        self.assertIsNone(self.stop())

    def test_timeout_is_failure(self):
        p = self.project("p", mode="block", checks=[
            {"name": "slow", "run": py_check("import time; time.sleep(30)"), "timeout": 1}])
        self.rec(self.file(p, "a.py"))
        result = self.stop()
        self.assertEqual(result["decision"], "block")
        self.assertIn("timed out after 1s", result["reason"])

    def test_output_tail_limited_to_30_lines(self):
        p = self.project("p", mode="block", checks=[
            {"name": "t", "run": py_check("[print('line%d' % i) for i in range(100)]; raise SystemExit(1)")}])
        self.rec(self.file(p, "a.py"))
        reason = self.stop()["reason"]
        self.assertIn("line99", reason)
        self.assertNotIn("line69\n", reason)
        self.assertIn("line70", reason)

    def test_invalid_config_is_warning(self):
        p = self.root / "p"
        p.mkdir()
        (p / ".dodcheck.json").write_text("{not json", encoding="utf-8")
        self.rec(self.file(p, "a.py"))
        result = self.stop()
        self.assertEqual(list(result), ["systemMessage"])
        self.assertIn("cannot read", result["systemMessage"])

    def test_invalid_mode_is_warning(self):
        p = self.project("p", mode="sometimes", checks=[])
        self.rec(self.file(p, "a.py"))
        self.assertIn("invalid mode", self.stop()["systemMessage"])

    def _bad_configs(self):
        out = []
        for i, text in enumerate(['{bad', '{"mode":"off","checks":[{"run":5}]}', '{"mode":"sometimes"}']):
            p = self.root / f"bad{i}"
            p.mkdir()
            (p / ".dodcheck.json").write_text(text, encoding="utf-8")
            out.append(p)
        return out

    def test_env_off_silences_invalid_configs(self):
        for p in self._bad_configs():
            self.rec(self.file(p, "a.py"))
        os.environ["DODCHECK_MODE"] = "off"
        self.assertIsNone(self.stop())

    def test_file_off_silences_invalid_rest_of_config(self):
        p = self.root / "p"
        p.mkdir()
        (p / ".dodcheck.json").write_text('{"mode":"off","checks":[{"run":5}]}', encoding="utf-8")
        self.rec(self.file(p, "a.py"))
        self.assertIsNone(self.stop())

    def test_invalid_config_reported_when_not_off(self):
        p = self._bad_configs()[1]
        os.environ["DODCHECK_MODE"] = "warn"
        self.rec(self.file(p, "a.py"))
        self.assertIn("needs a string", self.stop()["systemMessage"])

    def test_block_reason_includes_warn_failures(self):
        b = self.project("b", name="bproj", mode="block", checks=[{"name": "t", "run": marker_check("ran", 1)}])
        w = self.project("w", name="wproj", mode="warn", checks=[{"name": "lint", "run": marker_check("ran", 2)}])
        self.rec(self.file(b, "a.py"))
        self.rec(self.file(w, "a.py"))
        result = self.stop()
        self.assertEqual(list(result), ["decision", "reason"])
        self.assertIn("bproj", result["reason"])
        self.assertIn("wproj/lint (exit 2)", result["reason"])

    def test_invalid_env_mode_ignored_with_note(self):
        p = self.project("p", mode="warn", checks=[{"name": "t", "run": marker_check("ran", 1)}])
        self.rec(self.file(p, "a.py"))
        os.environ["DODCHECK_MODE"] = "loud"
        msg = self.stop()["systemMessage"]
        self.assertIn("p/t (exit 1)", msg)
        self.assertIn("DODCHECK_MODE=loud ignored: not block|warn|off", msg)

    def test_invalid_env_mode_note_in_block_reason(self):
        p = self.project("p", mode="block", checks=[{"name": "t", "run": marker_check("ran", 1)}])
        self.rec(self.file(p, "a.py"))
        os.environ["DODCHECK_MODE"] = "loud"
        self.assertIn("DODCHECK_MODE=loud ignored", self.stop()["reason"])

    def test_payload_cwd_resolves_relative_recorded_paths(self):
        p = self.project("p", mode="block", checks=[{"name": "t", "run": marker_check("ran", 1)}])
        self.file(p, "a.py")
        core.save_paths("s1", [os.path.join("p", "a.py")])
        code, out = self.call("stop", {"session_id": "s1", "cwd": str(self.root)})
        self.assertEqual(json.loads(out)["decision"], "block")

    def test_internal_error_never_blocks(self):
        original = core.stop
        core.stop = lambda payload: (_ for _ in ()).throw(RuntimeError("bug"))
        self.addCleanup(setattr, core, "stop", original)
        code, out = self.call("stop", {"session_id": "s1"})
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"systemMessage": "dodcheck internal error: bug"})


class RunTests(DodcheckTestCase):
    def test_pass_exit_zero(self):
        p = self.project("p", checks=[{"name": "ok", "run": marker_check("ran")}])
        code, out = self.call("run", args=[str(p)])
        self.assertEqual(code, 0)
        self.assertIn("PASS ok", out)
        self.assertTrue((p / "ran").exists())

    def test_fail_exit_one(self):
        p = self.project("p", checks=[{"name": "bad", "run": marker_check("ran", 4)}])
        code, out = self.call("run", args=[str(p)])
        self.assertEqual(code, 1)
        self.assertIn("FAIL", out)
        self.assertIn("exit code 4", out)

    def test_finds_project_above_given_dir(self):
        p = self.project("p", checks=[{"name": "ok", "run": marker_check("ran")}])
        sub = p / "deep" / "er"
        sub.mkdir(parents=True)
        code, _ = self.call("run", args=[str(sub)])
        self.assertEqual(code, 0)
        self.assertTrue((p / "ran").exists())

    def test_no_config_exit_one(self):
        code, out = self.call("run", args=[str(self.root)])
        self.assertEqual(code, 1)
        self.assertIn("no .dodcheck.json", out)


if __name__ == "__main__":
    unittest.main()
