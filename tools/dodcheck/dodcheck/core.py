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

"""Recording, project discovery, check execution and stop-hook decisions."""
import json
import os
import re
import subprocess
import time

CONFIG_NAME = ".dodcheck.json"
IGNORED_DIRS = {"node_modules", ".venv", "venv", ".git", "__pycache__", "dist", "build"}
MODES = ("block", "warn", "off")
DEFAULT_TIMEOUT = 300
TAIL_LINES = 30
STATE_MAX_AGE_SECONDS = 7 * 24 * 3600
BLOCK_SUFFIX = (
    "Fix these failures before finishing. If they are pre-existing or unrelated "
    "to your change, say so explicitly in your final message instead of fixing them."
)


# ---------- state ----------

def state_dir():
    override = os.environ.get("DODCHECK_STATE_DIR")
    if override:
        return override
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "dodcheck")
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "dodcheck")


def state_file(session_id):
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(session_id or "unknown"))[:100] or "unknown"
    return os.path.join(state_dir(), safe + ".json")


def load_paths(session_id):
    try:
        with open(state_file(session_id), encoding="utf-8") as handle:
            data = json.load(handle)
        return [p for p in data if isinstance(p, str)]
    except (OSError, ValueError, TypeError):
        return []


def save_paths(session_id, paths):
    path = state_file(session_id)
    if not paths:
        try:
            os.remove(path)
        except OSError:
            pass
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(paths, handle)


def purge_old_state(now=None):
    cutoff = (now or time.time()) - STATE_MAX_AGE_SECONDS
    directory = state_dir()
    for name in os.listdir(directory):
        full = os.path.join(directory, name)
        if name.endswith(".json") and os.path.getmtime(full) < cutoff:
            os.remove(full)


def normalise(path, cwd=None):
    if not os.path.isabs(path):
        path = os.path.join(cwd or os.getcwd(), path)
    return os.path.normpath(path)


def record(payload):
    """Append the edited file path to the session state. Raises on bad input."""
    tool_input = payload.get("tool_input") or {}
    raw = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not raw:
        return
    path = normalise(raw, payload.get("cwd"))
    session_id = payload.get("session_id")
    paths = load_paths(session_id)
    if os.path.normcase(path) not in {os.path.normcase(p) for p in paths}:
        paths.append(path)
        save_paths(session_id, paths)
    try:
        purge_old_state()
    except OSError:
        pass


# ---------- projects and config ----------

def find_project(start):
    """Nearest directory at or above start (a directory) containing a config file."""
    current = start
    while True:
        if os.path.isfile(os.path.join(current, CONFIG_NAME)):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def load_config(project_dir):
    """Return (config, error). Config is normalised; error is a string or None."""
    try:
        with open(os.path.join(project_dir, CONFIG_NAME), encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError) as exc:
        return None, f"cannot read {CONFIG_NAME}: {exc}"
    if not isinstance(raw, dict):
        return None, f"{CONFIG_NAME} must be a JSON object"
    mode = raw.get("mode", "warn")
    watch = raw.get("watch", ["**"])
    checks = raw.get("checks", [])
    if mode not in MODES:
        return None, f"invalid mode {mode!r} (expected block, warn or off)"
    if not isinstance(watch, list) or not all(isinstance(w, str) for w in watch):
        return None, "watch must be a list of glob strings"
    if not isinstance(checks, list):
        return None, "checks must be a list"
    parsed = []
    for index, check in enumerate(checks):
        if not isinstance(check, dict) or not isinstance(check.get("run"), str):
            return None, f"check #{index + 1} needs a string 'run'"
        timeout = check.get("timeout", DEFAULT_TIMEOUT)
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            return None, f"check #{index + 1} has an invalid timeout"
        parsed.append({"name": str(check.get("name") or check["run"]), "run": check["run"], "timeout": timeout})
    name = str(raw.get("name") or os.path.basename(project_dir))
    return {"name": name, "mode": mode, "watch": watch, "checks": parsed}, None


def env_mode():
    """Valid DODCHECK_MODE value, or None."""
    override = os.environ.get("DODCHECK_MODE", "").strip().lower()
    return override if override in MODES else None


def env_mode_note():
    """Note about an invalid DODCHECK_MODE value, or an empty string."""
    raw = os.environ.get("DODCHECK_MODE", "").strip()
    if raw and raw.lower() not in MODES:
        return f"DODCHECK_MODE={raw} ignored: not block|warn|off"
    return ""


def effective_mode(config_mode):
    return env_mode() or config_mode


def peek_mode(project_dir):
    """Effective mode without validating the rest of the config.

    DODCHECK_MODE if valid, else the file's mode if the JSON parses and the
    mode is valid, else warn.
    """
    override = env_mode()
    if override:
        return override
    try:
        with open(os.path.join(project_dir, CONFIG_NAME), encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return "warn"
    mode = raw.get("mode", "warn") if isinstance(raw, dict) else "warn"
    return mode if mode in MODES else "warn"


def glob_to_regex(pattern):
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z", re.IGNORECASE if os.name == "nt" else 0)


def glob_match(rel_posix, pattern):
    return glob_to_regex(pattern.replace("\\", "/")).match(rel_posix) is not None


def relevant(path, project_dir, watch):
    rel = os.path.relpath(path, project_dir).replace(os.sep, "/")
    if rel.startswith("../") or any(part in IGNORED_DIRS for part in rel.split("/")[:-1]):
        return False
    return any(glob_match(rel, pattern) for pattern in watch)


# ---------- running checks ----------

def _kill_tree(proc):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
        else:
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass
    proc.kill()


def run_check(check, project_dir):
    """Return dict: name, ok, exit_code (None on timeout), timed_out, timeout, tail."""
    extra = {} if os.name == "nt" else {"start_new_session": True}
    proc = subprocess.Popen(
        check["run"], shell=True, cwd=project_dir, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **extra,
    )
    timed_out = False
    try:
        out, _ = proc.communicate(timeout=check["timeout"])
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        try:
            out, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            out = b""
    lines = out.decode("utf-8", "replace").splitlines()[-TAIL_LINES:]
    code = None if timed_out else proc.returncode
    return {"name": check["name"], "ok": code == 0, "exit_code": code,
            "timed_out": timed_out, "timeout": check["timeout"], "tail": "\n".join(lines)}


def run_project(config, project_dir):
    return [run_check(check, project_dir) for check in config["checks"]]


def describe_failure(project_name, result):
    status = f"timed out after {result['timeout']}s" if result["timed_out"] else f"exit code {result['exit_code']}"
    text = f"Project {project_name}, check '{result['name']}': {status}."
    if result["tail"]:
        text += "\nLast output lines:\n" + result["tail"]
    return text


def short_failure(project_name, result):
    status = "timeout" if result["timed_out"] else f"exit {result['exit_code']}"
    return f"{project_name}/{result['name']} ({status})"


# ---------- stop hook ----------

def group_by_project(paths):
    groups = {}
    for path in paths:
        project = find_project(os.path.dirname(path))
        if project:
            groups.setdefault(project, []).append(path)
    return groups


def stop(payload):
    """Return the JSON-serialisable hook output dict, or None for silence."""
    session_id = payload.get("session_id")
    paths = load_paths(session_id)
    if not paths:
        return None
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        paths = [normalise(p, cwd) for p in paths]
    active = bool(payload.get("stop_hook_active"))
    kept = []
    block_texts, warn_shorts = [], []
    for project_dir, files in group_by_project(paths).items():
        if peek_mode(project_dir) == "off":
            continue
        config, error = load_config(project_dir)
        if error:
            warn_shorts.append(f"{os.path.basename(project_dir)}: {error}")
            continue
        mode = effective_mode(config["mode"])
        if not any(relevant(f, project_dir, config["watch"]) for f in files):
            continue
        failures = [r for r in run_project(config, project_dir) if not r["ok"]]
        if not failures:
            continue
        if mode == "block" and not active:
            block_texts.extend(describe_failure(config["name"], r) for r in failures)
            kept.extend(files)
        else:
            warn_shorts.extend(short_failure(config["name"], r) for r in failures)
    save_paths(session_id, kept)
    note = env_mode_note()
    if block_texts:
        reason = "dodcheck failures:\n\n" + "\n\n".join(block_texts)
        if warn_shorts:
            reason += "\n\nAlso failing (warn mode):\n" + "\n".join("- " + w for w in warn_shorts)
        if note:
            reason += "\n\n" + note
        return {"decision": "block", "reason": reason + "\n\n" + BLOCK_SUFFIX}
    if warn_shorts:
        message = "dodcheck: " + "; ".join(warn_shorts)
        return {"systemMessage": message + (f" ({note})" if note else "")}
    return None


def run_command(project_dir):
    """Run a project's checks; return (exit_code, text)."""
    config, error = load_config(project_dir)
    if error:
        return 1, f"{project_dir}: {error}"
    results = run_project(config, project_dir)
    lines = [f"dodcheck: {config['name']} ({project_dir})"]
    for result in results:
        if result["ok"]:
            lines.append(f"  PASS {result['name']}")
        else:
            lines.append("  FAIL " + describe_failure(config["name"], result).replace("\n", "\n    "))
    ok = all(r["ok"] for r in results)
    lines.append("RESULT: " + ("PASS" if ok else "FAIL"))
    return (0 if ok else 1), "\n".join(lines)
