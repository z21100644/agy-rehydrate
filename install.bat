@echo off
setlocal DisableDelayedExpansion

set "PLUGIN_NAME=agy-rehydrate"
set "PLUGIN_SRC=%~dp0plugins\%PLUGIN_NAME%"
if defined AGY_PLUGINS_DST (
  set "PLUGINS_DST=%AGY_PLUGINS_DST%"
) else (
  set "PLUGINS_DST=%USERPROFILE%\.gemini\config\plugins"
)
set "PLUGIN_DST=%PLUGINS_DST%\%PLUGIN_NAME%"

if not exist "%PLUGIN_SRC%\plugin.json" (
  echo [ERROR] Source plugin manifest missing: "%PLUGIN_SRC%\plugin.json"
  exit /b 1
)

set "PYTHON_LAUNCHER="
set "PYTHON_BIN="
python -B -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul && set "PYTHON_LAUNCHER=python"
if not defined PYTHON_LAUNCHER (
  py -3 -B -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul && set "PYTHON_LAUNCHER=py -3"
)
if not defined PYTHON_LAUNCHER (
  python3 -B -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul && set "PYTHON_LAUNCHER=python3"
)
if defined PYTHON_LAUNCHER (
  for /f "delims=" %%E in ('%PYTHON_LAUNCHER% -B -c "import sys; print(sys.executable)" 2^>nul') do set "PYTHON_BIN=%%E"
)

if not exist "%PLUGINS_DST%\*" mkdir "%PLUGINS_DST%"

robocopy "%PLUGIN_SRC%" "%PLUGIN_DST%" /MIR /XD __pycache__ /XF *.pyc *.pyo *.pyd /NFL /NDL /NJH /NJS /NP /R:2 /W:1 >nul
if errorlevel 8 (
  echo [ERROR] Failed to install %PLUGIN_NAME% to "%PLUGIN_DST%"
  exit /b 1
)

for /d /r "%PLUGIN_DST%" %%D in (__pycache__) do @if exist "%%D\*" rmdir /s /q "%%D"
for /r "%PLUGIN_DST%" %%F in (*.pyc *.pyo *.pyd) do @if exist "%%F" del /f /q "%%F"
if defined PYTHON_BIN (
  if exist "%PLUGIN_DST%\hooks.json" call :pin_hooks "%PLUGIN_DST%\hooks.json"
)
echo Installed %PLUGIN_NAME% to "%PLUGIN_DST%"
endlocal
exit /b 0

:pin_hooks
set "AGY_HOOKS_FILE=%~1"
set "AGY_PIN_PYTHON=%PYTHON_BIN%"
%PYTHON_LAUNCHER% -B -c "import json, os; from pathlib import Path; p = Path(os.environ['AGY_HOOKS_FILE']); exe = os.environ['AGY_PIN_PYTHON']; inv = ('\"' + exe + '\"') if any(c.isspace() for c in exe) else exe; data = json.loads(p.read_text(encoding='utf-8')); [(h.__setitem__('command', inv + ' -B ' + h['command'].split('||', 1)[0].split(' -B ', 1)[1].strip()) if isinstance(h, dict) and h.get('type') == 'command' and isinstance(h.get('command'), str) and ' -B ' in h['command'].split('||', 1)[0] else None) for v in data.values() if isinstance(v, dict) for k, arr in v.items() if isinstance(arr, list) for entry in arr if isinstance(entry, dict) for h in (entry.get('hooks') if isinstance(entry.get('hooks'), list) else [entry])]; p.write_bytes((json.dumps(data, indent=2) + '\n').encode('utf-8'))" >nul 2>nul
exit /b 0
