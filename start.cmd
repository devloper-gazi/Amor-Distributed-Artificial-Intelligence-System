@echo off
rem AMOR -- start.ps1 wrapper that works even when Windows' default
rem ExecutionPolicy (Restricted) blocks unsigned PowerShell scripts.
rem
rem Why this exists: out-of-the-box PowerShell on Win10/11 refuses to
rem execute .ps1 files until the user runs Set-ExecutionPolicy. The
rem user shouldn't have to know that. This .cmd file invokes PS with
rem -ExecutionPolicy Bypass for THIS run only -- it does NOT modify
rem the system policy.
rem
rem Usage (works from cmd, PowerShell, or double-click in Explorer):
rem     start.cmd                    -- bring services up
rem     start.cmd --skip-pull        -- forward args to start.ps1
rem
rem ASCII-only comments: em-dashes confuse cmd.exe on non-UTF8 code
rem pages and produce 'AMOR is not recognized' stderr noise.

setlocal
powershell.exe -NoProfile -NoLogo -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
exit /b %ERRORLEVEL%
