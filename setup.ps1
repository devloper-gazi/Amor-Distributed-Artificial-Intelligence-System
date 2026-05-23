# AMOR -- Windows PowerShell bootstrap shim.
#
# Locates a Python 3.9+ interpreter, then hands off to the
# `tools.setup` package which does the real work.  Pass any args
# straight through:
#
#     .\setup.ps1                       # default: install (full profile)
#     .\setup.ps1 install --profile dev
#     .\setup.ps1 doctor
#     .\setup.ps1 verify
#     .\setup.ps1 start
#     .\setup.ps1 stop
#     .\setup.ps1 status
#     .\setup.ps1 logs app -f
#
# Requires PowerShell 5.1+ (ships with every Windows 10/11).

#Requires -Version 5.1
$ErrorActionPreference = "Stop"

# Pin working directory to the repo root.
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -Path $RepoRoot

function Find-Python {
    foreach ($cmd in @("python", "python3", "py")) {
        $exe = Get-Command $cmd -ErrorAction SilentlyContinue
        if ($null -eq $exe) { continue }
        try {
            # `py` (PEP 397 launcher) wants -3 to force Python 3.
            if ($cmd -eq "py") {
                $verOut = & $exe -3 -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
            } else {
                $verOut = & $exe -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
            }
        } catch { continue }
        if ($LASTEXITCODE -ne 0 -or -not $verOut) { continue }
        $parts = $verOut.Trim().Split(".")
        if ($parts.Count -lt 2) { continue }
        $major = [int]$parts[0]
        $minor = [int]$parts[1]
        if ($major -gt 3 -or ($major -eq 3 -and $minor -ge 9)) {
            if ($cmd -eq "py") {
                return @{ Exe = $exe.Source; PreArgs = @("-3") }
            }
            return @{ Exe = $exe.Source; PreArgs = @() }
        }
    }
    return $null
}

$py = Find-Python
if ($null -eq $py) {
    Write-Host "[setup.ps1] ERROR: Python 3.9+ not found on PATH." -ForegroundColor Red
    Write-Host "[setup.ps1]   Download: https://www.python.org/downloads/" -ForegroundColor Yellow
    Write-Host "[setup.ps1]   Or:       winget install Python.Python.3.12" -ForegroundColor Yellow
    exit 127
}

# Default command: install.
if ($args.Count -eq 0) {
    $args = @("install")
}

$allArgs = @($py.PreArgs) + @("-m", "tools.setup") + $args

# Use & call operator so $LASTEXITCODE bubbles through.
& $py.Exe @allArgs
exit $LASTEXITCODE
