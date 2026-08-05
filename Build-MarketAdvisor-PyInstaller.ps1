# Optional standalone freeze: dist\MarketAdvisor\MarketAdvisor.exe
# Prefer Build-MarketAdvisor-Launcher.ps1 for day-to-day (keeps live Src\ edits).
# Use this when you want a copy that does not depend on system pythonw.exe.
#
# Rebuild:
#   powershell -ExecutionPolicy Bypass -File .\Build-MarketAdvisor-PyInstaller.ps1
#
# Notes:
# - onedir (not onefile) is more reliable with PyQt5
# - settings.json / journals still live under Src\ next to this repo; the frozen
#   exe chdirs to Src so existing paths keep working
# - first build can take several minutes; fix missing hiddenimports if it fails at runtime

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$src = Join-Path $root "Src"
$main = Join-Path $src "main.py"
$ico = Join-Path $src "app_icon.ico"
$dist = Join-Path $root "dist"
$work = Join-Path $root "build"
$spec = Join-Path $root "MarketAdvisor.spec"

if (-not (Test-Path $main)) { throw "main.py not found: $main" }
if (-not (Test-Path $ico)) { throw "app_icon.ico not found: $ico" }

py -3.12 -c "import PyInstaller; print(PyInstaller.__version__)" | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller not found for Python 3.12. Run: py -3.12 -m pip install pyinstaller"
}

# Ensure frozen builds still resolve settings / icons relative to Src\
$entry = Join-Path $src "_pyinstaller_entry.py"
@"
"""PyInstaller entry — chdir to Src so settings.json paths stay stable."""
import os
import sys

# When frozen, _MEIPASS is the unpack dir; app data stays in repo Src\
_SRC = os.path.dirname(os.path.abspath(__file__))
if getattr(sys, "frozen", False):
    # Prefer Src beside dist\MarketAdvisor\, else beside the exe
    _cand = [
        os.path.abspath(os.path.join(os.path.dirname(sys.executable), "..", "..", "Src")),
        os.path.abspath(os.path.join(os.path.dirname(sys.executable), "..", "Src")),
        _SRC,
    ]
    for c in _cand:
        if os.path.isdir(c) and os.path.isfile(os.path.join(c, "main.py")):
            os.chdir(c)
            if c not in sys.path:
                sys.path.insert(0, c)
            break
else:
    os.chdir(_SRC)
    if _SRC not in sys.path:
        sys.path.insert(0, _SRC)

from main import main

if __name__ == "__main__":
    main()
"@ | Set-Content -Path $entry -Encoding UTF8

Write-Host "Building MarketAdvisor.exe (onedir, windowed)…"
# Clean previous freeze outputs only
if (Test-Path $dist) { Remove-Item $dist -Recurse -Force }
if (Test-Path $work) { Remove-Item $work -Recurse -Force }

$args = @(
    "-3.12", "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--windowed",
    "--onedir",
    "--name", "MarketAdvisor",
    "--icon", $ico,
    "--paths", $src,
    "--distpath", $dist,
    "--workpath", $work,
    "--specpath", $root,
    "--collect-all", "PyQt5",
    "--hidden-import", "gui",
    "--hidden-import", "broker",
    "--hidden-import", "scoring",
    "--hidden-import", "monitor",
    "--hidden-import", "journal",
    "--hidden-import", "market_data",
    "--hidden-import", "ipo_calendar",
    "--hidden-import", "balance_guard",
    "--hidden-import", "etrade_broker",
    "--hidden-import", "etrade_client",
    "--hidden-import", "version",
    $entry
)

& py @args
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit $LASTEXITCODE" }

$exe = Join-Path $dist "MarketAdvisor\MarketAdvisor.exe"
if (-not (Test-Path $exe)) { throw "Expected exe missing: $exe" }

Write-Host ""
Write-Host "Built: $exe"
Write-Host "Launch (keeps using Src\settings.json when run from this repo layout):"
Write-Host "  & `"$exe`""
Write-Host ""
Write-Host "Day-to-day tip: Build-MarketAdvisor-Launcher.ps1 is lighter and still shows MarketAdvisor.exe in Task Manager."
