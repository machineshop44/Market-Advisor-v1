# Install Market Advisor MCP bridge for Cursor (run once per machine that runs Cursor).
# Usage (from repo root or tools folder):
#   powershell -ExecutionPolicy Bypass -File .\tools\install-mcp-bridge.ps1

$ErrorActionPreference = "Stop"

function Find-PythonLauncher {
    if (Get-Command py -ErrorAction SilentlyContinue) { return "py" }
    if (Get-Command python -ErrorAction SilentlyContinue) { return "python" }
    return $null
}

$py = Find-PythonLauncher
if (-not $py) {
    Write-Host ""
    Write-Host "Python not found on PATH." -ForegroundColor Red
    Write-Host "Install Python 3.12+ from https://www.python.org/downloads/"
    Write-Host "Check 'Add python.exe to PATH', then re-run this script."
    Write-Host ""
    Write-Host "Note: The frozen Market Advisor EXE folder does NOT include pip."
    Write-Host "MCP installs on the PC where Cursor runs (can be your desk PC, not Plex)."
    exit 1
}

$root = Split-Path -Parent $PSScriptRoot
$req = Join-Path $root "requirements-mcp.txt"
if (-not (Test-Path $req)) {
    Write-Host "Missing $req — run from the Market Advisor v1 repo." -ForegroundColor Red
    exit 1
}

Write-Host "Using: $py" -ForegroundColor Cyan
& $py -m pip install --upgrade pip
& $py -m pip install -r $req

Write-Host ""
Write-Host "OK — MCP bridge installed." -ForegroundColor Green
Write-Host "Next: Market Advisor Settings -> Desk Advisor -> Copy MCP setup JSON -> Cursor Settings -> MCP"
Write-Host "Test from repo root:"
Write-Host "  $py tools\remote-desk-check.py --url https://127.0.0.1:8791 --token YOUR_TOKEN"
