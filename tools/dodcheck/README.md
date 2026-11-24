# dodcheck

Per-project "definition of done" checks for Claude Code hooks, for monorepos.

Hooks are configured once in the user-global `~/.claude/settings.json`. dodcheck makes them opt-in per project: a folder anywhere in a monorepo opts in by containing a `.dodcheck.json`. When an agent session edits files inside that project, the project's checks (tests, lint) run when the agent stops, and only that project's checks run. Standard library only, no network.

## Subcommands

Invoke as `python "<path>/tools/dodcheck/hook.py" <subcommand>` (no PYTHONPATH needed), `python -m dodcheck <subcommand>` with `PYTHONPATH` set, or the `dodcheck.ps1` / `dodcheck.cmd` shims.

- `record` (PostToolUse hook). Reads the hook JSON on stdin (`session_id`, `tool_input.file_path` or `tool_input.notebook_path`, `cwd`) and appends the absolute path, deduplicated, to a per-session state file. Prints nothing and always exits 0. State dir: `DODCHECK_STATE_DIR`, else `%LOCALAPPDATA%\dodcheck` on Windows, else `$XDG_STATE_HOME/dodcheck` or `~/.local/state/dodcheck`. State files older than 7 days are deleted opportunistically.
- `stop` (Stop hook). For each recorded path, finds the nearest ancestor directory with a `.dodcheck.json`, groups paths by project, drops files that do not match the project's `watch` globs (and anything under `node_modules`, `.venv`, `venv`, `.git`, `__pycache__`, `dist`, `build`), then runs each project's checks in order (cwd = project dir, shell, combined stdout+stderr, last 30 lines kept). A timeout or non-zero exit is a failure.
  - All pass: state is cleared, no output.
  - Failure, mode `block`, `stop_hook_active` false: prints `{"decision":"block","reason":...}`; paths stay in state so the next stop re-runs.
  - Failure otherwise (mode `warn`, or `stop_hook_active` true): prints `{"systemMessage":"dodcheck: ..."}` and clears the paths.
  - Internal error: prints `{"systemMessage":"dodcheck internal error: ..."}`. Always exits 0; dodcheck bugs never block.
- `run [PROJECT_DIR]`. Runs the project's checks now (default: nearest `.dodcheck.json` at or above the current directory), prints a plain-text summary, exits 0 if all pass, else 1.

## Config: `.dodcheck.json`

```json
{
  "name": "buckaroo-consultant",
  "mode": "block",
  "watch": ["**/*.py", "pyproject.toml"],
  "checks": [
    {"name": "tests", "run": ".venv\\Scripts\\python.exe -m pytest -q", "timeout": 300}
  ]
}
```

- `name`: label in messages (default: folder name).
- `mode`: `block`, `warn` or `off` (default `warn`).
- `watch`: globs matched against the path relative to the project dir, using `/` separators. `**` matches any number of directories (`**/*.py` matches `a.py` and `src/x/a.py`); `*` and `?` do not cross `/`. Default: every file.
- `checks`: ordered list of `{name, run, timeout}`; `timeout` is seconds (default 300).
- Environment variable `DODCHECK_MODE` (`block|warn|off`), when set, overrides every project's mode.
- An invalid `DODCHECK_MODE` value is ignored (the project's own mode applies); when dodcheck prints a message or block reason it adds `DODCHECK_MODE=<value> ignored: not block|warn|off`.
- Mode precedence: valid `DODCHECK_MODE`, else the file's `mode` if the JSON parses and the mode is valid, else `warn`. If that effective mode is `off`, the project is skipped silently even when the rest of its config is invalid. Otherwise invalid config produces a `systemMessage` for that project and never crashes or blocks.
- If a block-mode project and a warn-mode project both fail in one stop, the block `reason` also lists the warn-mode failures, one line each.
- `stop` uses the payload `cwd` only to resolve recorded paths that are relative.

## User settings (hooks)

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit|NotebookEdit",
        "hooks": [
          {
            "type": "command",
            "command": "python \"C:/sourcecode/github/coding_agent_useful_tools/tools/dodcheck/hook.py\" record",
            "timeout": 10
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python \"C:/sourcecode/github/coding_agent_useful_tools/tools/dodcheck/hook.py\" stop",
            "timeout": 900
          }
        ]
      }
    ]
  }
}
```

## Switching block to warn

Edit `"mode"` in the project's `.dodcheck.json`, or set `DODCHECK_MODE=warn` in the environment to relax every project at once (`off` disables all checks).

## Limitations

- Only edits made through the Edit, Write, MultiEdit and NotebookEdit tools are recorded. Files changed via shell commands (formatters, `sed`, `git checkout`) are not.
- Paths are recorded per session; a session that never stops cleanly leaves its state file until the 7-day cleanup.
- Checks run with `shell=True`, so commands are interpreted by `cmd.exe` on Windows and `sh` elsewhere.

## Tests

```
cd tools/dodcheck
python -m unittest discover -s tests
```
