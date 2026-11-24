@REM Copyright (c) 2026 Andrew Murphy
@REM
@REM This file is part of coding-agent-useful-tools.
@REM
@REM coding-agent-useful-tools is dual-licensed:
@REM 1. Under the GNU Affero General Public License v3.0 (AGPLv3) for open-source
@REM    and non-proprietary use. See the LICENSE file in the root directory.
@REM 2. Under a Commercial License for proprietary use or without AGPLv3 
@REM    obligations. See COMMERCIAL-LICENSE.md or contact andrew_murphy_datapro@outlook.com
@REM     with subject line: "Commercial Licensing Request for coding-agent-useful-tools".

@echo off
setlocal
rem Runs dodcheck from this folder without installing it; the working directory is kept.
rem Set CODING_AGENT_TOOLS_PYTHON to a python.exe path to choose the interpreter.
set "TOOL_PYTHON=python"
if defined CODING_AGENT_TOOLS_PYTHON set "TOOL_PYTHON=%CODING_AGENT_TOOLS_PYTHON%"
set "PYTHONPATH=%~dp0;%PYTHONPATH%"
"%TOOL_PYTHON%" -m dodcheck %*
exit /b %errorlevel%
