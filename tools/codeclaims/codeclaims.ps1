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

# Runs codeclaims from this folder without installing it. The caller's working
# directory is kept, so relative paths resolve where the command was run.
# Set CODING_AGENT_TOOLS_PYTHON to a python.exe path to choose the interpreter.
$pythonCommand = if ($env:CODING_AGENT_TOOLS_PYTHON) { $env:CODING_AGENT_TOOLS_PYTHON } else { 'python' }
$previousPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = "$PSScriptRoot$([IO.Path]::PathSeparator)$previousPythonPath"
    & $pythonCommand -m codeclaims @args
    $toolExit = $LASTEXITCODE
} finally {
    $env:PYTHONPATH = $previousPythonPath
}
exit $toolExit
