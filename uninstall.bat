@echo off
setlocal DisableDelayedExpansion

set "PLUGIN_NAME=agy-rehydrate"
set "PLUGIN_DST=%USERPROFILE%\.gemini\config\plugins\%PLUGIN_NAME%"

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
