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

"""Validate every report and scratch destination before creating anything."""
from contextlib import contextmanager
import os
from pathlib import Path
import tempfile


def _absolute(path):
    return Path(os.path.abspath(Path(path).expanduser()))


def _resolved(path):
    try:
        return path.resolve()
    except RuntimeError:
        raise ValueError("Cannot safely resolve destination paths") from None


def protected_roots():
    """Include configured read-only homes as well as the conventional homes."""
    roots = [Path.home()/".codex", Path.home()/".claude"]
    for name in ("AGENTLOG_CODEX_HOME", "AGENTLOG_CLAUDE_HOME", "CODEX_HOME", "CLAUDE_CONFIG_DIR"):
        if os.environ.get(name):
            roots.append(Path(os.environ[name]))
    return roots


def checked_destination(path, *, inputs=()):
    """Return a resolved path, rejecting protected trees and input aliases."""
    absolute = _absolute(path)
    resolved = _resolved(absolute)
    for root in protected_roots():
        root = _absolute(root)
        if absolute.is_relative_to(root) or resolved.is_relative_to(_resolved(root)):
            raise ValueError("Destination is inside a protected agent configuration directory")
    for source in inputs:
        if not source:
            continue
        # Public metadata is redacted. Refuse rather than guess at a filename
        # whose original identity can no longer be established safely.
        if "[REDACTED:" in str(source):
            raise ValueError("Cannot validate an input path containing redacted components")
        source = _resolved(_absolute(source))
        if resolved == source or (source.is_dir() and resolved.is_relative_to(source)):
            raise ValueError("Destination would modify the input session")
        if resolved.exists() and source.exists() and resolved.samefile(source):
            raise ValueError("Destination aliases the input session")
    return resolved


def temporary_candidates():
    """List candidates without tempfile.gettempdir's implicit write probes."""
    candidates = [tempfile.tempdir] if tempfile.tempdir else []
    candidates.extend(os.environ.get(name) for name in ("TMPDIR", "TEMP", "TMP"))
    if os.name == "nt":
        if os.environ.get("LOCALAPPDATA"):
            candidates.append(Path(os.environ["LOCALAPPDATA"])/"Temp")
        if os.environ.get("SystemRoot"):
            candidates.append(Path(os.environ["SystemRoot"])/"Temp")
    else:
        candidates.extend(("/tmp", "/var/tmp", "/usr/tmp"))
    return [Path(candidate) for candidate in candidates if candidate]


@contextmanager
def private_temp_directory():
    """Create private scratch outside protected trees, or fail without writes."""
    for candidate in temporary_candidates():
        try:
            candidate = checked_destination(candidate)
        except ValueError:
            continue
        if not candidate.is_dir():
            continue
        # Do not retry creation failures: in particular, a fresh-folder access
        # denial must surface to the caller rather than trigger an Avast loop.
        with tempfile.TemporaryDirectory(prefix="agentlog-", dir=candidate) as name:
            yield checked_destination(name)
        return
    raise OSError("No safe temporary directory is available outside protected agent configuration directories")
