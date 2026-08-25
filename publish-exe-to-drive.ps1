# Market Advisor publish - best of MA portable + ytarr/Arrs Hub install story.
#
# Keeps MA's trading-desk strength:
#   versioned portable zip + optional settings/journals/TLS/Restore-Sessions (Plex)
# Adopts ytarr/Arrs Hub strengths:
#   Inno per-user Start Menu installer -> Drive as MarketAdvisor-<ver>-x64.exe
#   dual artifacts, Drive\exe cleanup of older MA builds, SHA256 checksums
#
# Usage (repo root):
#   powershell -ExecutionPolicy Bypass -File .\publish-exe-to-drive.ps1
#   powershell -ExecutionPolicy Bypass -File .\publish-exe-to-drive.ps1 -SkipBuild
#   powershell -ExecutionPolicy Bypass -File .\publish-exe-to-drive.ps1 -IncludeSettings
#   powershell -ExecutionPolicy Bypass -File .\publish-exe-to-drive.ps1 -NoInstaller
#   powershell -ExecutionPolicy Bypass -File .\publish-exe-to-drive.ps1 -NoCopyToDrive
#   powershell -ExecutionPolicy Bypass -File .\publish-exe-to-drive.ps1 -InstallerOnly
#     → Plex PC path: build Inno x64.exe only, copy to Drive\exe (no portable zip)

param(
    [switch]$SkipBuild,
    [switch]$IncludeSettings,
    [switch]$NoInstaller,
    [switch]$NoCopyToDrive,
    [switch]$InstallerOnly,
    [string]$DriveExeDir = "G:\My Drive\exe"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$Src = Join-Path $Root "Src"
$DistApp = Join-Path $Root "dist\MarketAdvisor"
$ReleaseDir = Join-Path $Root "release"
$Packaging = Join-Path $Root "packaging"
$Ico = Join-Path $Src "app_icon.ico"

$Version = (py -3.12 -c "import sys; sys.path.insert(0, r'$Src'); import version; print(version.__version__)").Trim()
if (-not $Version) { throw "Could not read version from Src\version.py" }

if ($InstallerOnly) {
    $IncludeSettings = $false
    $NoInstaller = $false
}

Write-Host "Market Advisor publish $Version"
Write-Host "IncludeSettings=$IncludeSettings  NoInstaller=$NoInstaller  InstallerOnly=$InstallerOnly  Drive=$DriveExeDir"

if (-not $SkipBuild) {
    Write-Host "Running PyInstaller freeze..."
    powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root "Build-MarketAdvisor-PyInstaller.ps1")
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed" }
}

$Exe = Join-Path $DistApp "MarketAdvisor.exe"
if (-not (Test-Path $Exe)) { throw "Missing freeze EXE: $Exe" }
if (-not (Test-Path (Join-Path $DistApp "_internal"))) { throw "Missing _internal under $DistApp" }

New-Item -ItemType Directory -Force -Path $ReleaseDir | Out-Null

$ZipName = $null
$ZipPath = $null
$StageName = "MarketAdvisor-$Version-portable"
$Stage = Join-Path $ReleaseDir $StageName

if (-not $InstallerOnly) {
    if (Test-Path $Stage) { Remove-Item $Stage -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $Stage | Out-Null

    Write-Host "Staging portable payload -> $Stage"
    Copy-Item $Exe $Stage -Force
    Copy-Item (Join-Path $DistApp "_internal") (Join-Path $Stage "_internal") -Recurse -Force

    # Src runtime (code + icons). Optionally include live desk state for Plex.
    $StageSrc = Join-Path $Stage "Src"
    New-Item -ItemType Directory -Force -Path $StageSrc | Out-Null
    Get-ChildItem $Src -File | Where-Object {
        $_.Name -match '\.(py|ico|png|jpg|svg|qss)$' -or
        $_.Name -eq "settings.example.json"
    } | ForEach-Object { Copy-Item $_.FullName $StageSrc -Force }

    # Always useful empty dirs / optional TLS module folder structure
    foreach ($sub in @("monitor_tls")) {
        $p = Join-Path $Src $sub
        if (Test-Path $p) {
            Copy-Item $p (Join-Path $StageSrc $sub) -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    if ($IncludeSettings) {
        Write-Host "Bundling live settings / desk state (CONTAINS SECRETS)..."
        $deskFiles = @(
            "settings.json", "scoring_state.json", "activity_log.txt",
            "trade_journal.jsonl", "decision_journal.jsonl"
        )
        foreach ($f in $deskFiles) {
            $p = Join-Path $Src $f
            if (Test-Path $p) { Copy-Item $p $StageSrc -Force }
        }
        # TLS material for stable companion pin
        $tls = Join-Path $Src "monitor_tls"
        if (Test-Path $tls) {
            Copy-Item $tls (Join-Path $StageSrc "monitor_tls") -Recurse -Force
        }

        $restoreDir = Join-Path $Stage "Restore-Tokens"
        New-Item -ItemType Directory -Force -Path $restoreDir | Out-Null
        $pickle = Join-Path $env:USERPROFILE ".tokens\robinhood.pickle"
        if (Test-Path $pickle) {
            Copy-Item $pickle (Join-Path $restoreDir "robinhood.pickle") -Force
        }
        # Export E*TRADE keyring secrets when keyring is available
        $etJson = Join-Path $restoreDir "etrade_keyring.json"
        $env:MA_ETRADE_EXPORT_JSON = $etJson
        py -3.12 -c @"
import json, os, sys
try:
    import keyring
except Exception:
    sys.exit(0)
svc = 'MarketAdvisor.ETrade'
secrets = {}
for env in ('sandbox', 'live'):
    for kind in ('access_token', 'access_token_secret', 'request_token', 'request_token_secret'):
        name = f'{env}:{kind}'
        try:
            v = keyring.get_password(svc, name)
        except Exception:
            v = None
        if v:
            secrets[name] = v
out = os.environ.get('MA_ETRADE_EXPORT_JSON') or ''
if secrets and out:
    json.dump({'service': svc, 'secrets': secrets}, open(out, 'w', encoding='utf-8'), indent=2)
    print('Exported', len(secrets), 'E*TRADE keyring secret(s)')
"@
        Remove-Item Env:MA_ETRADE_EXPORT_JSON -ErrorAction SilentlyContinue

        Copy-Item (Join-Path $Packaging "Restore-Sessions.ps1") $Stage -Force
        $plexReadme = @"
Market Advisor $Version - portable package for Plex PC (CONTAINS SECRETS)

SECURITY
- This zip includes live settings.json, broker API keys/passwords, journals,
  Robinhood session pickle, and E*TRADE Credential Manager secrets.
- Do NOT share the Google Drive link publicly. Keep Drive access private.

QUICK START
1. Unzip this folder onto the Plex PC. Keep MarketAdvisor.exe, _internal\, Src\,
   Restore-Tokens\, and Restore-Sessions.ps1 together (same parent folder).
2. (Recommended once) Right-click Restore-Sessions.ps1 -> Run with PowerShell
   OR from this folder:
     powershell -ExecutionPolicy Bypass -File .\Restore-Sessions.ps1
   Needs Python 3.12 + keyring on Plex for E*TRADE restore:
     py -3.12 -m pip install keyring
3. Double-click MarketAdvisor.exe
4. Coinbase should connect from settings.json API keys.
   Robinhood uses the restored pickle when possible.
   E*TRADE uses restored Credential Manager tokens when possible.
5. Web monitor: Settings already has remote bind. On Plex, use the Plex LAN IP
   (https://<plex-lan-ip>:8791/). Re-scan companion QR if the TLS fingerprint
   changes (packaged certs are included under Src\monitor_tls\ to keep the pin
   stable when possible).

AUTH CAVEATS
- Robinhood pickle / E*TRADE OAuth tokens can expire or be machine-bound.
  If a broker shows Sign-in / Reauth needed, use Settings -> Connect.
- E*TRADE access tokens expire around midnight ET - reauth may still be needed
  the next day even after a successful restore.
- Do not commit this unzipped folder to git.

NOTES
- Onedir layout (not single-file EXE) - more reliable with PyQt5.
- settings.json, journals, scoring_state, activity logs, and TLS certs live
  under Src\ next to the EXE (portable path resolution).
- settings.example.json is also included as a blank-template backup.
- Also on Drive: MarketAdvisor-$Version-x64.exe (Start Menu installer, no secrets).
"@
        Set-Content -Path (Join-Path $Stage "PLEX-README.txt") -Value $plexReadme -Encoding UTF8
    }

    Copy-Item (Join-Path $Packaging "INSTALL-PLEX.txt") (Join-Path $Stage "INSTALL-PLEX.txt") -Force

    # Zip portable (tar.exe is more reliable than Compress-Archive on locked PyQt files)
    $ZipName = "$StageName.zip"
    $ZipPath = Join-Path $ReleaseDir $ZipName
    if (Test-Path $ZipPath) { Remove-Item $ZipPath -Force }
    Write-Host "Compressing $ZipName ..."
    Start-Sleep -Seconds 1
    Push-Location $ReleaseDir
    try {
        & tar.exe -a -cf $ZipPath $StageName
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path $ZipPath)) {
            Add-Type -AssemblyName System.IO.Compression.FileSystem
            if (Test-Path $ZipPath) { Remove-Item $ZipPath -Force }
            [System.IO.Compression.ZipFile]::CreateFromDirectory($Stage, $ZipPath)
        }
    } finally {
        Pop-Location
    }
    if (-not (Test-Path $ZipPath)) { throw "Failed to create portable zip" }
    $ZipMb = [math]::Round((Get-Item $ZipPath).Length / 1MB, 1)
    Write-Host "Portable zip: $ZipPath ($ZipMb MB)"
} else {
    Write-Host "InstallerOnly: skipping portable zip (Plex uses Start Menu x64.exe)"
}

# Installer payload (no live secrets - Start Menu path)
$InstallerPayload = Join-Path $ReleaseDir "installer-payload"
if (Test-Path $InstallerPayload) { Remove-Item $InstallerPayload -Recurse -Force }
New-Item -ItemType Directory -Force -Path $InstallerPayload | Out-Null
Copy-Item $Exe $InstallerPayload -Force
Copy-Item (Join-Path $DistApp "_internal") (Join-Path $InstallerPayload "_internal") -Recurse -Force
$PaySrc = Join-Path $InstallerPayload "Src"
New-Item -ItemType Directory -Force -Path $PaySrc | Out-Null
Get-ChildItem $Src -File | Where-Object {
    $_.Name -match '\.(py|ico|png|jpg|svg|qss)$' -or $_.Name -eq "settings.example.json"
} | ForEach-Object { Copy-Item $_.FullName $PaySrc -Force }
Copy-Item (Join-Path $Packaging "INSTALL-PLEX.txt") $InstallerPayload -Force

$InstallerName = "MarketAdvisor-$Version-x64.exe"
$InstallerPath = Join-Path $ReleaseDir $InstallerName

if (-not $NoInstaller) {
    $IsccCandidates = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles}\Inno Setup 6\ISCC.exe",
        "${env:LocalAppData}\Programs\Inno Setup 6\ISCC.exe"
    )
    $Iscc = $IsccCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $Iscc) {
        Write-Warning "Inno Setup 6 not found - skipping installer (winget install JRSoftware.InnoSetup). Portable zip still published."
    } else {
        Write-Host "Compiling Inno installer with $Iscc ..."
        $prevEap = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        & $Iscc "/DMyAppVersion=$Version" (Join-Path $Packaging "MarketAdvisor.iss")
        $isccExit = $LASTEXITCODE
        $ErrorActionPreference = $prevEap
        if ($isccExit -ne 0 -or -not (Test-Path $InstallerPath)) {
            Write-Warning "Inno Setup failed - portable zip still available."
            $InstallerPath = $null
        } else {
            $InstMb = [math]::Round((Get-Item $InstallerPath).Length / 1MB, 1)
            Write-Host "Installer: $InstallerPath ($InstMb MB)"
        }
    }
} else {
    $InstallerPath = $null
}

# Checksums
$ShaPath = Join-Path $ReleaseDir "MarketAdvisor-$Version-SHA256.txt"
$shaLines = @()
foreach ($p in @($ZipPath, $InstallerPath)) {
    if ($p -and (Test-Path $p)) {
        $h = (Get-FileHash -Algorithm SHA256 -Path $p).Hash
        $shaLines += "$(Split-Path -Leaf $p)  $h"
    }
}
$shaLines += ""
$shaLines += "Unsigned build (SmartScreen may warn) - same trust model as ytarr / Arrs Hub."
$shaLines += "Authenticode signing is what raises Packaging toward A."
Set-Content -Path $ShaPath -Value ($shaLines -join "`r`n") -Encoding ASCII
Write-Host "Checksums: $ShaPath"

if (-not $NoCopyToDrive) {
    if (-not (Test-Path $DriveExeDir)) {
        throw "Drive exe folder not found: $DriveExeDir (use -NoCopyToDrive to skip)"
    }
    # Remove older Market Advisor versioned artifacts (keep other apps)
    Get-ChildItem -Path $DriveExeDir -Filter "MarketAdvisor-*" -ErrorAction SilentlyContinue |
        Remove-Item -Force -Recurse -ErrorAction SilentlyContinue
    if ($ZipPath -and (Test-Path $ZipPath) -and -not $InstallerOnly) {
        Copy-Item $ZipPath (Join-Path $DriveExeDir $ZipName) -Force
        Write-Host "Drive: $(Join-Path $DriveExeDir $ZipName)"
    }
    if ($InstallerPath -and (Test-Path $InstallerPath)) {
        Copy-Item $InstallerPath (Join-Path $DriveExeDir $InstallerName) -Force
        Write-Host "Drive: $(Join-Path $DriveExeDir $InstallerName)"
    } elseif ($InstallerOnly) {
        throw "InstallerOnly requested but installer was not built"
    }
    Copy-Item $ShaPath (Join-Path $DriveExeDir (Split-Path -Leaf $ShaPath)) -Force
    Write-Host "Drive: $(Join-Path $DriveExeDir (Split-Path -Leaf $ShaPath))"
}

Write-Host "Done."
