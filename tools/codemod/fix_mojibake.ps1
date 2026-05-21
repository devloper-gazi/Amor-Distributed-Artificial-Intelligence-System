<#
.SYNOPSIS
  Cycle UI v2.5 Phase 3 post-mortem -- restore mojibake'd files.

.DESCRIPTION
  The Phase 3 rename_token.ps1 codemod wrote files via
  System.IO.File.WriteAllText which on Windows PowerShell 5.1
  defaults to a host-specific ANSI encoding (CP1252).  The source
  bytes were read as UTF-8 strings; when written back as CP1252,
  multibyte characters double-encoded into 3-character sequences.
  This script reverses the round-trip.

  ONLY runs on files containing the canonical mojibake leader
  (U+00C3) so files that are CORRECT UTF-8 stay untouched.
#>

param(
    [string] $Root = "web_ui/v2/src",
    [switch] $DryRun
)

$ErrorActionPreference = "Stop"

$exts = @(".tsx", ".ts", ".css")
$cp1252 = [System.Text.Encoding]::GetEncoding(1252)
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$utf8WithBom = New-Object System.Text.UTF8Encoding($true)

# Mojibake leaders: UTF-8 multi-byte sequences read as CP1252 produce
# strings starting with U+00C2 - U+00C3 (Latin Extended), U+00E2 - U+00E3
# (Latin Small Letter A With Circumflex / Tilde), or U+00F0 (4-byte
# leader, emoji).  Test every candidate character to catch all layers.
# Our sources don't legitimately contain these chars in TSX/TS/CSS
# (Turkish uses normal s c o g i not A circumflex / tilde).
$mojibakeLeaders = @(
    [string]([char]0x00C2),
    [string]([char]0x00C3),
    [string]([char]0x00E2),
    [string]([char]0x00E3),
    [string]([char]0x00F0)
)

$candidates = Get-ChildItem -Path $Root -Recurse -File |
    Where-Object { $exts -contains $_.Extension }

$patched = 0
$skipped = 0
$failed = @()

foreach ($file in $candidates) {
    try {
        $current = [System.IO.File]::ReadAllText($file.FullName, $utf8NoBom)
        $hasLeader = $false
        foreach ($leader in $mojibakeLeaders) {
            if ($current.Contains($leader)) { $hasLeader = $true; break }
        }
        if (-not $hasLeader) {
            $skipped++
            continue
        }
        $cp1252Bytes = $cp1252.GetBytes($current)
        $restored = $utf8NoBom.GetString($cp1252Bytes)
        if ($restored -eq $current) {
            $skipped++
            continue
        }
        $rel = Resolve-Path -Relative $file.FullName
        if ($DryRun) {
            Write-Host "  [WOULD FIX] $rel" -ForegroundColor Yellow
        } else {
            [System.IO.File]::WriteAllText($file.FullName, $restored, $utf8WithBom)
            Write-Host "  [FIXED]     $rel" -ForegroundColor Green
        }
        $patched++
    } catch {
        $failed += @{ path = $file.FullName; error = $_.Exception.Message }
        Write-Host "  [FAIL]      $($file.FullName) -- $($_.Exception.Message)" -ForegroundColor Red
    }
}

Write-Host ""
$mode = if ($DryRun) { "DRY-RUN" } else { "DONE" }
Write-Host ("[MOJIBAKE-FIX] {0}: patched={1}, skipped={2}, failed={3}" -f $mode, $patched, $skipped, $failed.Count) -ForegroundColor Cyan
if ($failed.Count -gt 0) { exit 1 }
exit 0
