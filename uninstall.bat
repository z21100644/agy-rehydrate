@echo off
setlocal DisableDelayedExpansion

set "PLUGIN_NAME=agy-rehydrate"
if defined AGY_PLUGINS_DST (
  set "PLUGINS_DST=%AGY_PLUGINS_DST%"
) else (
  set "PLUGINS_DST=%USERPROFILE%\.gemini\config\plugins"
)
set "PLUGIN_DST=%PLUGINS_DST%\%PLUGIN_NAME%"

if exist "%PLUGIN_DST%\*" (
  rmdir /s /q "%PLUGIN_DST%"
  if exist "%PLUGIN_DST%\*" (
    echo [ERROR] Failed to remove "%PLUGIN_DST%"
    exit /b 1
  )
  echo Removed "%PLUGIN_DST%"
) else (
  echo Not installed: "%PLUGIN_DST%"
)

endlocal
