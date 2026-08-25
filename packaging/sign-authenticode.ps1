# Optional Authenticode signing for Market Advisor installer.
# Set env vars before publish:
#   MA_SIGN_PFX_PATH  — path to .pfx code-signing cert
#   MA_SIGN_PFX_PASSWORD — cert password (optional if pfx has none)
#
# Usage:
#   powershell -File packaging\sign-authenticode.ps1 -ExePath release\MarketAdvisor-1.32.0-x64.exe

param(
    [Parameter(Mandatory = $true)]
    [string]$ExePath
)

$ErrorActionPreference = "Stop"
if (-not (Test-Path $ExePath)) { throw "Missing: $ExePath" }

$pfx = $env:MA_SIGN_PFX_PATH
if (-not $pfx -or -not (Test-Path $pfx)) {
    Write-Warning "MA_SIGN_PFX_PATH not set or file missing — skipping Authenticode sign."
    exit 0
}

$signtoolCandidates = @(
    "${env:ProgramFiles(x86)}\Windows Kits\10\bin\*\x64\signtool.exe",
    "${env:ProgramFiles}\Windows Kits\10\bin\*\x64\signtool.exe"
)
$signtool = $signtoolCandidates | ForEach-Object { Get-Item $_ -ErrorAction SilentlyContinue } |
    Sort-Object FullName -Descending | Select-Object -First 1
if (-not $signtool) {
    Write-Warning "signtool.exe not found (install Windows SDK) — skipping sign."
    exit 0
}

$pass = $env:MA_SIGN_PFX_PASSWORD
$args = @("sign", "/fd", "SHA256", "/f", $pfx, "/tr", "http://timestamp.digicert.com", "/td", "SHA256")
if ($pass) { $args += @("/p", $pass) }
$args += $ExePath

Write-Host "Signing $(Split-Path -Leaf $ExePath) ..."
& $signtool.FullName @args
if ($LASTEXITCODE -ne 0) { throw "signtool failed ($LASTEXITCODE)" }
Write-Host "Signed: $ExePath"
