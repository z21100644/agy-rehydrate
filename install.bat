@echo off
setlocal DisableDelayedExpansion

set "PLUGIN_NAME=agy-rehydrate"
set "PLUGIN_SRC=%~dp0plugins\%PLUGIN_NAME%"
set "PLUGINS_DST=%USERPROFILE%\.gemini\config\plugins"
set "PLUGIN_DST=%PLUGINS_DST%\%PLUGIN_NAME%"

if not exist "%PLUGIN_SRC%\plugin.json" (
  echo [ERROR] Source plugin manifest missing: "%PLUGIN_SRC%\plugin.json"
  exit /b 1
)

if not exist "%PLUGINS_DST%\*" mkdir "%PLUGINS_DST%"

robocopy "%PLUGIN_SRC%" "%PLUGIN_DST%" /MIR /XD __pycache__ /XF *.pyc *.pyo *.pyd /NFL /NDL /NJH /NJS /NP /R:2 /W:1 >nul
if errorlevel 8 (
  echo [ERROR] Failed to install %PLUGIN_NAME% to "%PLUGIN_DST%"
  exit /b 1
)

for /d /r "%PLUGIN_DST%" %%D in (__pycache__) do @if exist "%%D\*" rmdir /s /q "%%D"
for /r "%PLUGIN_DST%" %%F in (*.pyc *.pyo *.pyd) do @if exist "%%F" del /f /q "%%F"
echo Installed %PLUGIN_NAME% to "%PLUGIN_DST%"
exit /b 0

endlocal
