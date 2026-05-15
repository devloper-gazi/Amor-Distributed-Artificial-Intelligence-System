# AMOR — legacy `start.ps1` entry point.
#
# Cycle E v18 replaced this script with `tools/setup/` (a Python
# orchestrator with preflight + idempotent install + verify).  This
# stub simply forwards to `setup.ps1 start` so existing muscle memory
# (.\start.ps1) still works.

#Requires -Version 5.1
$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Shim = Join-Path $RepoRoot "setup.ps1"
if (-not (Test-Path $Shim)) {
    Write-Host "[start.ps1] ERROR: setup.ps1 missing — repo state corrupted." -ForegroundColor Red
    exit 1
}
& $Shim start @args
exit $LASTEXITCODE
