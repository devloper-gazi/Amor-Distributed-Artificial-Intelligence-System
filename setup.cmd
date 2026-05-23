@echo off
rem AMOR -- setup.ps1 wrapper that works even when Windows' default
rem ExecutionPolicy (Restricted) blocks unsigned PowerShell scripts.
rem
rem Why this exists: out-of-the-box PowerShell on Win10/11 refuses to
rem execute .ps1 files until the user runs Set-ExecutionPolicy. The
rem user shouldn't have to know that. This .cmd file invokes PS with
rem -ExecutionPolicy Bypass for THIS run only -- it does NOT modify
rem the system policy.
rem
rem Usage (works from cmd, PowerShell, or double-click in Explorer):
rem     setup.cmd                      -- default: install (full profile)
rem     setup.cmd install --profile dev
rem     setup.cmd doctor
rem     setup.cmd verify
rem     setup.cmd start
rem     setup.cmd stop
rem     setup.cmd status
rem     setup.cmd logs app -f
rem
rem ASCII-only comments: em-dashes confuse cmd.exe on non-UTF8 code
rem pages and produce 'AMOR is not recognized' stderr noise.

setlocal
powershell.exe -NoProfile -NoLogo -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
exit /b %ERRORLEVEL%
