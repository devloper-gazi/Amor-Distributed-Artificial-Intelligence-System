<#
.SYNOPSIS
  Cycle UI v2.5 -- per-PR token rename codemod.

.DESCRIPTION
  Locates every TSX / TS / CSS file under web_ui/v2/src/ that
  references the OLD Tailwind utility class or CSS variable and
  rewrites the reference to the NEW name in-place.  Idempotent
  + dry-run flag + per-PR batching design.

  Used during Phase 2 + Phase 3 of the Cycle UI v2.5 implementation
  plan as a bridge from the legacy `--color-bg-primary` / `bg-bg-
  primary` token system to the new OKLch tokens (`--color-bg-canvas`
  / `bg-bg-canvas`).  Phase 3 closing commit invokes this against
  the last remaining references; theme.css's legacy variable
  definitions are then deleted once `rg` confirms zero refs.

.PARAMETER OldToken
  The legacy token name as it appears in source files.  May be a
  Tailwind utility class (e.g. ``bg-bg-primary``), a CSS variable
  reference (e.g. ``var(--color-bg-primary)``), or any literal
  substring.  Substring match -- provide enough context to disambig.

.PARAMETER NewToken
  The new token name to substitute.

.PARAMETER Root
  Directory tree to sweep.  Default:
  ``web_ui/v2/src`` (relative to the repo root the script is run from).

.PARAMETER Extensions
  Comma-separated list of file extensions to consider.  Default
  ``tsx,ts,css`` covers SolidJS components, TypeScript helpers,
  and CSS files.

.PARAMETER DryRun
  When set, print the files + lines that WOULD change without
  writing anything.  Always do a dry-run before a real apply.

.EXAMPLE
  pwsh tools/codemod/rename_token.ps1 `
       -OldToken "bg-bg-primary" -NewToken "bg-bg-canvas" -DryRun

.EXAMPLE
  pwsh tools/codemod/rename_token.ps1 `
       -OldToken "text-text-secondary" -NewToken "text-text-body"

.NOTES
  Cross-platform via PowerShell 7+ (pwsh).  Works on Windows native
  and inside WSL2 / Linux containers where pwsh is installed.

  Tip: when chaining multiple renames in a PR, use the script
  multiple times -- once per (OldToken, NewToken) pair -- and
  commit each batch as a separate file change so `git revert`
  granularity stays per-batch.
#>

param(
    [Parameter(Mandatory = $true)] [string] $OldToken,
    [Parameter(Mandatory = $true)] [string] $NewToken,
    [string] $Root = "web_ui/v2/src",
    [string] $Extensions = "tsx,ts,css",
    [switch] $DryRun
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -Path $Root -PathType Container)) {
    Write-Error "Root path not found: $Root"
    exit 2
}

if ($OldToken -eq $NewToken) {
    Write-Warning "OldToken and NewToken are identical; nothing to do."
    exit 0
}

$exts = $Extensions -split "," | ForEach-Object { ".$($_.Trim())" }

Write-Host "[CODEMOD] root=$Root ext=$($Extensions) dry_run=$DryRun" -ForegroundColor Cyan
Write-Host "[CODEMOD] '$OldToken' -> '$NewToken'" -ForegroundColor Cyan

$candidates = Get-ChildItem -Path $Root -Recurse -File |
    Where-Object { $exts -contains $_.Extension }

$matched = 0
$updated = 0
$totalReplacements = 0

foreach ($file in $candidates) {
    $content = Get-Content -Path $file.FullName -Raw
    if ($null -eq $content) { continue }
    if (-not $content.Contains($OldToken)) { continue }

    $matched++
    $count = ([regex]::Matches($content, [regex]::Escape($OldToken))).Count
    $totalReplacements += $count

    if ($DryRun) {
        $rel = Resolve-Path -Relative $file.FullName
        Write-Host "  $rel -- $count match$(if ($count -ne 1) { 'es' })" `
                   -ForegroundColor Yellow
    } else {
        $next = $content.Replace($OldToken, $NewToken)
        # Preserve the file's original line-ending style by writing
        # via -NoNewline + the existing buffer's trailing newline.
        [System.IO.File]::WriteAllText($file.FullName, $next)
        $updated++
        $rel = Resolve-Path -Relative $file.FullName
        Write-Host "  $rel -- patched ($count)" -ForegroundColor Green
    }
}

if ($DryRun) {
    Write-Host ("[CODEMOD] DRY-RUN: {0} files would change, {1} total replacements." `
                -f $matched, $totalReplacements) -ForegroundColor Cyan
    exit 0
}

Write-Host ("[CODEMOD] DONE: patched {0}/{1} files, {2} replacements." `
            -f $updated, $matched, $totalReplacements) -ForegroundColor Cyan
if ($matched -eq 0) {
    Write-Host "[CODEMOD] No matches -- nothing to do.  Token may already be renamed?" `
               -ForegroundColor Yellow
}
