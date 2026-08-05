# Installs MarketAdvisor.exe beside pythonw.exe (same folder as python3xx.dll).
# Task Manager Details then shows MarketAdvisor.exe instead of pythonw.exe.
# Re-run after a Python upgrade (the copy is not updated automatically).
#
# Also refreshes Desktop / Start Menu / project shortcuts + VBS via Create-Desktop-Shortcut.ps1.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

function Resolve-Pythonw {
    $candidates = @(
        "C:\Users\machi\AppData\Local\Programs\Python\Python312\pythonw.exe",
        "C:\Python314\pythonw.exe"
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { return (Resolve-Path $c).Path }
    }
    $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cmd) { return $cmd.Source }
    throw "pythonw.exe not found. Install Python 3.12+ or add pythonw to PATH."
}

$pythonw = Resolve-Pythonw
$pyDir = Split-Path -Parent $pythonw
$launcher = Join-Path $pyDir "MarketAdvisor.exe"

# Refuse to overwrite a non-python-sized mystery binary; pythonw is typically ~100KB.
$srcInfo = Get-Item $pythonw
Copy-Item -Path $pythonw -Destination $launcher -Force
$dstInfo = Get-Item $launcher
if ($dstInfo.Length -ne $srcInfo.Length) {
    throw "Launcher copy size mismatch - aborting."
}

Write-Host "Installed launcher: $launcher"
Write-Host "Source pythonw:     $pythonw"
Write-Host "Size:               $($dstInfo.Length) bytes"

# Point shortcuts / VBS at MarketAdvisor.exe
& (Join-Path $root "Create-Desktop-Shortcut.ps1")

Write-Host ""
Write-Host "Done. Quit any running Market Advisor, then start from:"
Write-Host "  - Desktop: Market Advisor.lnk"
Write-Host "  - Project: Start Market Advisor.lnk  (or .vbs)"
Write-Host ""
Write-Host "Task Manager -> Details should list MarketAdvisor.exe."
Write-Host "Task Manager -> Processes (Apps) groups under the window title / AppUserModelID."
Write-Host ""
Write-Host "After upgrading Python, re-run this script."
Write-Host "For a fully standalone freeze (no system Python), see Build-MarketAdvisor-PyInstaller.ps1."
