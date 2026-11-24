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

"""Synthetic checks for the toolcalls command."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from agentlog import cli

SECRET = "sk-" + "a" * 30


def claude_call(name, **tool_input):
    return {"type": "assistant", "sessionId": "claude-synthetic", "cwd": "C:/synthetic/proj",
            "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "t%d" % id(tool_input), "name": name, "input": tool_input}]}}


def codex_call(name, arguments):
    return {"type": "response_item", "payload": {"type": "function_call", "name": name, "call_id": "c%d" % id(arguments), "arguments": json.dumps(arguments)}}


class ToolcallsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/"sessions"; self.root.mkdir()
        self.out = Path(self.temp.name)/"out"

    def write(self, name, rows):
        path = self.root/name
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        return path

    def run_cmd(self, *extra):
        with contextlib.redirect_stdout(io.StringIO()):
            code = cli.main(["toolcalls", "--root", str(self.root), "--min-count", "1", "--out", str(self.out), *extra])
        self.assertEqual(code, 0)
        return json.loads((self.out/"toolcalls.json").read_text(encoding="utf-8"))

    def commands(self, data):
        return {(c["tool"], c["key"]): c["count"] for c in data["commands"]}

    def test_claude_bash_chains_pipes_rtk_env_sudo(self):
        command = "cd repo && git status | head -5; FOO=bar sudo timeout 30 npm test || rtk git status\nrtk proxy git log -3\nnohup env A=1 python -m pytest"
        self.write("a.jsonl", [claude_call("Bash", command=command), claude_call("Read", file_path="x"), claude_call("mcp__srv__lookup", q="1")])
        data = self.run_cmd()
        found = self.commands(data)
        for key in ("cd", "git status", "head", "npm test", "rtk git status", "rtk proxy git", "python -m pytest"):
            self.assertIn(("Bash", key), found, key)
        self.assertEqual(found[("Bash", "rtk git status")], 1)
        tools = {t["name"]: t["count"] for t in data["tools"]}
        self.assertEqual(tools, {"Bash": 1, "Read": 1, "mcp__srv__lookup": 1})

    def test_heredoc_body_ignored(self):
        command = "python - <<'EOF'\nimport os\ndef helper():\n    return 1\nprint(helper())\nEOF\ngit diff\ncat <<-END\n\tsecretword here\n\tEND\nls"
        self.write("a.jsonl", [claude_call("Bash", command=command)])
        found = self.commands(self.run_cmd())
        self.assertEqual(set(found), {("Bash", "python"), ("Bash", "git diff"), ("Bash", "cat"), ("Bash", "ls")})

    def test_heredoc_with_pipe_and_chain_ignored(self):
        command = "cat <<EOF | python -\nbody text\nx = compute()\nEOF\ncat <<'A' && echo ok\nsome body\nA\ncat <<-B; cat <<\"C\"\n\tbbody\n\tB\ncbody\nC\nls"
        self.write("a.jsonl", [claude_call("Bash", command=command)])
        found = self.commands(self.run_cmd())
        self.assertEqual(set(found), {("Bash", "cat"), ("Bash", "python"), ("Bash", "echo"), ("Bash", "ls")})

    def test_code_looking_lines_dropped(self):
        self.write("a.jsonl", [claude_call("Bash", command="try:\nelse:\nx = 1\ny=2\nfoo = bar baz\ngit status")])
        self.assertEqual(set(self.commands(self.run_cmd())), {("Bash", "git status")})

    def test_second_word_only_for_subcommand_tools(self):
        command = "cd secret-project; python my_script_name; rg token; mytool arg; git Secret; git status; python -m pytest; py -m http.server; rtk proxy curl x"
        self.write("a.jsonl", [claude_call("Bash", command=command)])
        found = set(self.commands(self.run_cmd()))
        self.assertEqual(found, {("Bash", k) for k in ("cd", "python", "rg", "mytool", "git", "git status", "python -m pytest", "py -m http.server", "rtk proxy curl")})

    def test_subcommand_tool_flag(self):
        self.write("a.jsonl", [claude_call("Bash", command="mytool build; other build")])
        found = set(self.commands(self.run_cmd("--subcommand-tool", "MyTool")))
        self.assertEqual(found, {("Bash", "mytool build"), ("Bash", "other")})

    def test_powershell_tool(self):
        self.write("a.jsonl", [claude_call("PowerShell", command="Get-ChildItem -Recurse; dotnet.exe build && C:\\tools\\Rtk.exe status")])
        found = self.commands(self.run_cmd())
        self.assertEqual(set(found), {("PowerShell", "Get-ChildItem"), ("PowerShell", "dotnet build"), ("PowerShell", "Rtk status")})

    def test_codex_shell_argv(self):
        self.write("rollout-x.jsonl", [{"type": "session_meta", "payload": {"id": "codex-synthetic", "cwd": "C:/synthetic/proj"}},
                                       codex_call("shell", {"command": ["git", "status", "--short"]}),
                                       codex_call("shell", {"command": ["bash", "-lc", "rg foo && pytest -q"]}),
                                       codex_call("exec_command", {"cmd": "ls -la"})])
        found = self.commands(self.run_cmd())
        self.assertEqual(found, {("shell", "git status"): 1, ("shell", "rg"): 1, ("shell", "pytest"): 1, ("exec_command", "ls"): 1})

    def test_privacy_filter_and_outputs(self):
        command = f"https://example.invalid/x?t={SECRET}\n{SECRET} run\ncurl --header {SECRET}\n$env:TOKEN = 'abc'\ngit status"
        self.write("a.jsonl", [claude_call("Bash", command=command)])
        data = self.run_cmd()
        self.assertEqual(data["dropped_segments"], 3)
        self.assertEqual(self.commands(data), {("Bash", "curl"): 1, ("Bash", "git status"): 1})
        for name in ("toolcalls.md", "toolcalls.json"):
            text = (self.out/name).read_text(encoding="utf-8")
            for leaked in (SECRET, "example.invalid", "abc"): self.assertNotIn(leaked, text)

    def test_min_count_top_project_last(self):
        rows = [claude_call("Bash", command="git status; git status; git status; ls; ls; pwd")]
        self.write("a.jsonl", rows)
        data = self.run_cmd("--min-count", "2")
        self.assertEqual([(c["key"], c["count"]) for c in data["commands"]], [("git status", 3), ("ls", 2)])
        data = self.run_cmd("--min-count", "1", "--top", "1")
        self.assertEqual([c["key"] for c in data["commands"]], ["git status"])
        self.assertEqual(self.run_cmd("--project", "no-such-project")["sessions_scanned"], 0)
        self.assertEqual(self.run_cmd("--project", "SYNTHETIC")["sessions_scanned"], 1)
        self.assertEqual(self.run_cmd("--source", "codex")["sessions_scanned"], 0)

    def test_last_selects_most_recent(self):
        old = self.write("old.jsonl", [claude_call("Bash", command="oldcmd")])
        new = self.write("new.jsonl", [claude_call("Bash", command="newcmd")])
        import os
        os.utime(old, (1_000_000, 1_000_000)); os.utime(new, (2_000_000, 2_000_000))
        self.assertEqual(set(self.commands(self.run_cmd("--last", "1"))), {("Bash", "newcmd")})

    def test_markdown_written_and_max_tokens_cap(self):
        self.write("a.jsonl", [claude_call("Bash", command="; ".join(f"cmd{i}" for i in range(500)))])
        self.run_cmd("--max-tokens", "400", "--top", "500")
        self.assertIn("view truncated", (self.out/"toolcalls.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
