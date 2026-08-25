# Copy the latest Market Advisor Companion APK into Google Drive\apks.
# Keeps ONLY the current versioned file for this app:
#   MarketAdvisorCompanion-1.16.1-7.apk
# Deletes any older MarketAdvisorCompanion-*.apk (including leftover *-latest.apk).
# Does not create a -latest alias.
#
# Usage (from repo):
#   .\android\publish-apk-to-drive.ps1
#   .\android\publish-apk-to-drive.ps1 -Build   # assembleDebug first

param(
    [switch]$Build
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$DriveApks = "G:\My Drive\apks"
$ApkSource = Join-Path $PSScriptRoot "app\build\outputs\apk\debug\app-debug.apk"
$GradleFile = Join-Path $PSScriptRoot "app\build.gradle.kts"
$Prefix = "MarketAdvisorCompanion-"

if (-not (Test-Path "G:\My Drive")) {
    throw "Google Drive not available at G:\My Drive"
}

New-Item -ItemType Directory -Force -Path $DriveApks | Out-Null

if ($Build) {
    $env:JAVA_HOME = "C:\Program Files\Microsoft\jdk-21.0.12.8-hotspot"
    if (-not (Test-Path $env:JAVA_HOME)) {
        $env:JAVA_HOME = "C:\Program Files\Android\Android Studio\jbr"
    }
    & .\gradlew.bat :app:assembleDebug
    if ($LASTEXITCODE -ne 0) {
        throw "assembleDebug failed"
    }
}

if (-not (Test-Path -LiteralPath $ApkSource)) {
    throw "APK not found: $ApkSource (build first)"
}

$gradleText = Get-Content -LiteralPath $GradleFile -Raw
if ($gradleText -notmatch 'versionName\s*=\s*"([^"]+)"') {
    throw "Could not read versionName from app\build.gradle.kts"
}
$versionName = $Matches[1]
$code = if ($gradleText -match 'versionCode\s*=\s*(\d+)') { $Matches[1] } else { "0" }

$destName = "$Prefix$versionName-$code.apk"
$destPath = Join-Path $DriveApks $destName

# Remove every other MarketAdvisorCompanion APK first (old versions + any -latest aliases).
$removed = @()
Get-ChildItem -LiteralPath $DriveApks -Filter "MarketAdvisorCompanion*.apk" -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -ne $destName } |
    ForEach-Object {
        $removed += $_.Name
        Remove-Item -LiteralPath $_.FullName -Force
    }

Copy-Item -LiteralPath $ApkSource -Destination $destPath -Force

Write-Host "Drive apks (Market Advisor Companion): $destPath"
if ($removed.Count -gt 0) {
    Write-Host "Removed older companion APKs: $($removed -join ', ')"
}
