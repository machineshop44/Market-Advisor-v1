# Creates a Desktop shortcut that starts Market Advisor with no CMD window.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$vbs = Join-Path $root "Start Market Advisor.vbs"
$ico = Join-Path $root "Src\app_icon.ico"
$desktop = [Environment]::GetFolderPath("Desktop")
$lnkPath = Join-Path $desktop "Market Advisor.lnk"

$w = New-Object -ComObject WScript.Shell
$sc = $w.CreateShortcut($lnkPath)
$sc.TargetPath = "wscript.exe"
$sc.Arguments = "`"$vbs`""
$sc.WorkingDirectory = $root
$sc.WindowStyle = 7
$sc.Description = "Market Advisor - runs in the system tray"
if (Test-Path $ico) {
    $sc.IconLocation = "$ico,0"
}
$sc.Save()

Write-Host "Desktop shortcut created: $lnkPath"
if (Test-Path $ico) { Write-Host "Icon: $ico" }
Write-Host "Double-click it to start. Close the window to keep running in the tray."
Write-Host "Right-click the tray icon -> Quit to fully exit."
