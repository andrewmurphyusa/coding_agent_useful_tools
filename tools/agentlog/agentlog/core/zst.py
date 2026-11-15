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

"""Read archives without ever decompressing to disk."""
from contextlib import contextmanager
import importlib
import io
from pathlib import Path
import shutil
import subprocess
import warnings

class ZstdUnavailable(OSError): pass

def native_available():
    try: importlib.import_module("compression.zstd"); return True
    except ImportError: return False

@contextmanager
def open_binary(path):
    path = Path(path)
    if path.suffix != ".zst":
        with path.open("rb") as stream: yield stream
        return
    if native_available():
        module = importlib.import_module("compression.zstd")
        with module.open(path, "rb") as binary: yield binary
        return
    binary = shutil.which("zstd")
    if not binary:
        message = "Skipping .zst archive: install zstd (https://facebook.github.io/zstd/) or use Python 3.14+."
        warnings.warn(message, RuntimeWarning)
        raise ZstdUnavailable(message)
    process = subprocess.Popen([binary, "-dc", str(path)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        with process.stdout as stream: yield stream
        if process.wait() != 0: raise OSError("zstd could not read archive")
    finally:
        if process.poll() is None: process.terminate()
        process.wait()

@contextmanager
def open_text(path):
    with open_binary(path) as binary:
        with io.TextIOWrapper(binary, encoding="utf-8", errors="replace", newline="") as stream:
            yield stream
