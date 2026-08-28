# Lightweight Start Menu install from a Market Advisor portable zip (no Inno required).
# Extracts to %LOCALAPPDATA%\MarketAdvisor and creates Desktop + Start Menu shortcuts.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\MarketAdvisor-Setup.ps1
#   powershell -ExecutionPolicy Bypass -File .\MarketAdvisor-Setup.ps1 -ZipPath "G:\My Drive\exe\MarketAdvisor-1.29.0-portable.zip"

param(
    [string]$ZipPath = "",
    [string]$InstallDir = "",
    [string]$DriveExeDir = "G:\My Drive\exe"
)

$ErrorActionPreference = "Stop"

if (-not $InstallDir) {
    $InstallDir = Join-Path $env:LOCALAPPDATA "MarketAdvisor"
}

if (-not $ZipPath) {
    $latest = Get-ChildItem -Path $DriveExeDir -Filter "MarketAdvisor-*-portable.zip" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $latest) {
        $root = Split-Path -Parent $MyInvocation.MyCommand.Path
        $latest = Get-ChildItem -Path (Join-Path $root "release") -Filter "MarketAdvisor-*-portable.zip" -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1
    }
    if (-not $latest) { throw "No MarketAdvisor-*-portable.zip found. Pass -ZipPath." }
    $ZipPath = $latest.FullName
}

if (-not (Test-Path $ZipPath)) { throw "Zip not found: $ZipPath" }

Write-Host "Installing from: $ZipPath"
Write-Host "Target:          $InstallDir"

# Kill running instance
Get-Process -Name "MarketAdvisor" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 500

$stage = Join-Path $env:TEMP ("MA-setup-" + [guid]::NewGuid().ToString("n"))
New-Item -ItemType Directory -Force -Path $stage | Out-Null
try {
    Expand-Archive -Path $ZipPath -DestinationPath $stage -Force
    $payload = Get-ChildItem $stage -Directory | Where-Object {
        Test-Path (Join-Path $_.FullName "MarketAdvisor.exe")
    } | Select-Object -First 1
    if (-not $payload) {
        # zip may have expanded flat
        if (Test-Path (Join-Path $stage "MarketAdvisor.exe")) {
            $payload = Get-Item $stage
        }
    }
    if (-not $payload) { throw "MarketAdvisor.exe not found inside zip" }

    if (Test-Path $InstallDir) {
        # Preserve existing Src\settings.json if present and incoming lacks it
        $keepSettings = $null
        $oldSettings = Join-Path $InstallDir "Src\settings.json"
        if (Test-Path $oldSettings) {
            $keepSettings = Join-Path $env:TEMP "ma-settings-backup.json"
            Copy-Item $oldSettings $keepSettings -Force
        }
        Remove-Item $InstallDir -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    Copy-Item (Join-Path $payload.FullName "*") $InstallDir -Recurse -Force

    if ($keepSettings -and -not (Test-Path (Join-Path $InstallDir "Src\settings.json"))) {
        New-Item -ItemType Directory -Force -Path (Join-Path $InstallDir "Src") | Out-Null
        Copy-Item $keepSettings (Join-Path $InstallDir "Src\settings.json") -Force
    }

    $exe = Join-Path $InstallDir "MarketAdvisor.exe"
    $ico = Join-Path $InstallDir "Src\app_icon.ico"
    if (-not (Test-Path $ico)) { $ico = "$exe,0" } else { $ico = "$ico,0" }

    $shell = New-Object -ComObject WScript.Shell
    $desktop = [Environment]::GetFolderPath("Desktop")
    $startDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
    New-Item -ItemType Directory -Force -Path $startDir | Out-Null

    foreach ($lnkPath in @(
        (Join-Path $desktop "Market Advisor.lnk"),
        (Join-Path $startDir "Market Advisor.lnk")
    )) {
        $sc = $shell.CreateShortcut($lnkPath)
        $sc.TargetPath = $exe
        $sc.WorkingDirectory = $InstallDir
        $sc.Description = "Market Advisor"
        $sc.IconLocation = $ico
        $sc.Save()
    }

    Write-Host ""
    Write-Host "Installed. Shortcuts on Desktop + Start Menu."
    Write-Host "If this zip included Restore-Sessions.ps1, run it once before first launch."
    Write-Host "Launch: $exe"
} finally {
    Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
}
