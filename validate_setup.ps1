# AMOR -- legacy `validate_setup.ps1` entry point.
#
# Cycle E v18 replaced the bespoke validator (which checked for v1 UI
# files that no longer exist) with `tools/setup/doctor.py`.  This stub
# forwards to it so existing automation keeps working.

#Requires -Version 5.1
$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Shim = Join-Path $RepoRoot "setup.ps1"
if (-not (Test-Path $Shim)) {
    Write-Host "[validate_setup.ps1] ERROR: setup.ps1 missing." -ForegroundColor Red
    exit 1
}
& $Shim doctor @args
exit $LASTEXITCODE
