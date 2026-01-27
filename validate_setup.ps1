# Validation Script for Monochrome Chat UI Integration
# This script validates the setup without starting Docker

Write-Host "`n========================================" -ForegroundColor Cyan
Write-Host "  Monochrome Chat UI - Setup Validation" -ForegroundColor Cyan
Write-Host "========================================`n" -ForegroundColor Cyan

$allGood = $true

# Check 1: Verify old UI files are removed
Write-Host "[1/7] Checking old UI files removed..." -ForegroundColor Yellow
$oldFiles = @(
    "web_ui\templates\chat_research.html",
    "web_ui\templates\local_research.html",
    "web_ui\static\css\local-ai.css",
    "web_ui\static\js\local-ai.js"
)

$filesRemoved = $true
foreach ($file in $oldFiles) {
    if (Test-Path $file) {
        Write-Host "  ❌ Old file still exists: $file" -ForegroundColor Red
        $filesRemoved = $false
        $allGood = $false
    }
}

if ($filesRemoved) {
    Write-Host "  ✅ All old UI files removed" -ForegroundColor Green
}

# Check 2: Verify new UI files exist
Write-Host "`n[2/7] Checking new UI files exist..." -ForegroundColor Yellow
$newFiles = @(
    "web_ui\templates\index.html",
    "web_ui\static\css\tokens.css",
    "web_ui\static\css\styles.css",
    "web_ui\static\css\chat-research.css",
    "web_ui\static\js\app.js",
    "web_ui\static\js\chat-research.js"
)

$filesExist = $true
foreach ($file in $newFiles) {
    if (Test-Path $file) {
        $fileSize = (Get-Item $file).Length
        Write-Host "  ✅ $file ($fileSize bytes)" -ForegroundColor Green
    } else {
        Write-Host "  ❌ Missing: $file" -ForegroundColor Red
        $filesExist = $false
        $allGood = $false
    }
}

# Check 3: Verify backend API routes updated
Write-Host "`n[3/7] Checking backend API files..." -ForegroundColor Yellow
$backendFiles = @(
    "document_processor\main.py",
    "document_processor\api\chat_research_routes.py",
    "document_processor\api\local_ai_routes_simple.py"
)

foreach ($file in $backendFiles) {
    if (Test-Path $file) {
        Write-Host "  ✅ $file exists" -ForegroundColor Green
    } else {
        Write-Host "  ❌ Missing: $file" -ForegroundColor Red
        $allGood = $false
    }
}

# Check 4: Verify Thinking endpoint in chat_research_routes.py
Write-Host "`n[4/7] Checking Thinking endpoint in Claude API routes..." -ForegroundColor Yellow
$chatRoutesContent = Get-Content "document_processor\api\chat_research_routes.py" -Raw
if ($chatRoutesContent -match '/thinking') {
    Write-Host "  ✅ Thinking endpoint found" -ForegroundColor Green
} else {
    Write-Host "  ❌ Thinking endpoint not found" -ForegroundColor Red
    $allGood = $false
}

# Check 5: Verify Coding endpoint in chat_research_routes.py
Write-Host "`n[5/7] Checking Coding endpoint in Claude API routes..." -ForegroundColor Yellow
if ($chatRoutesContent -match '/coding') {
    Write-Host "  ✅ Coding endpoint found" -ForegroundColor Green
} else {
    Write-Host "  ❌ Coding endpoint not found" -ForegroundColor Red
    $allGood = $false
}

# Check 6: Verify Thinking endpoint in local_ai_routes_simple.py
Write-Host "`n[6/7] Checking Thinking endpoint in Local AI routes..." -ForegroundColor Yellow
$localRoutesContent = Get-Content "document_processor\api\local_ai_routes_simple.py" -Raw
if ($localRoutesContent -match '@router\.post\("/thinking"\)') {
    Write-Host "  ✅ Thinking endpoint found" -ForegroundColor Green
} else {
    Write-Host "  ❌ Thinking endpoint not found" -ForegroundColor Red
    $allGood = $false
}

# Check 7: Verify Coding endpoint in local_ai_routes_simple.py
Write-Host "`n[7/7] Checking Coding endpoint in Local AI routes..." -ForegroundColor Yellow
if ($localRoutesContent -match '@router\.post\("/coding"\)') {
    Write-Host "  ✅ Coding endpoint found" -ForegroundColor Green
} else {
    Write-Host "  ❌ Coding endpoint not found" -ForegroundColor Red
    $allGood = $false
}

# Final Report
Write-Host "`n========================================" -ForegroundColor Cyan
if ($allGood) {
    Write-Host "  ✅ VALIDATION PASSED" -ForegroundColor Green
    Write-Host "========================================`n" -ForegroundColor Cyan
    Write-Host "All checks passed! The integration is complete." -ForegroundColor Green
    Write-Host "`nNext steps:" -ForegroundColor Yellow
    Write-Host "1. Start Docker Desktop" -ForegroundColor White
    Write-Host "2. Run: .\start.ps1" -ForegroundColor White
    Write-Host "3. Wait for services to start" -ForegroundColor White
    Write-Host "4. Open: http://localhost:8000" -ForegroundColor White
    Write-Host "`nDocumentation:" -ForegroundColor Yellow
    Write-Host "- INTEGRATION_COMPLETE.md - Full integration details" -ForegroundColor White
    Write-Host "- TEST_REPORT.md - Frontend testing report" -ForegroundColor White
} else {
    Write-Host "  ❌ VALIDATION FAILED" -ForegroundColor Red
    Write-Host "========================================`n" -ForegroundColor Cyan
    Write-Host "Some checks failed. Please review the errors above." -ForegroundColor Red
}

Write-Host ""
