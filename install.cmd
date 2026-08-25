@echo off
rem Run install.ps1 without touching the machine's PowerShell execution policy.
rem Works double-clicked from Explorer or run from a prompt; any arguments
rem (-Uninstall, -AllUsers, -SkipTerminalProfile) are passed straight through.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set PORTER_RC=%ERRORLEVEL%

rem Explorer launches us with `cmd /c`, so the window would vanish with the
rem output in it.  Hold it open in that case only.
echo %cmdcmdline% | find /i "%~nx0" >nul && pause

exit /b %PORTER_RC%
