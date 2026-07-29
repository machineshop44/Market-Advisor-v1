# Creates Desktop + Start Menu shortcuts with custom icon AND AppUserModelID
# so the Windows taskbar uses Market Advisor's icon instead of pythonw.exe.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$src = Join-Path $root "Src"
$main = Join-Path $src "main.py"
$ico = Join-Path $src "app_icon.ico"
$pythonw = "C:\Users\machi\AppData\Local\Programs\Python\Python312\pythonw.exe"
if (-not (Test-Path $pythonw)) {
    $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cmd) { $pythonw = $cmd.Source }
}
if (-not (Test-Path $pythonw)) { throw "pythonw.exe not found" }
if (-not (Test-Path $main)) { throw "main.py not found: $main" }
if (-not (Test-Path $ico)) { throw "app_icon.ico not found: $ico" }

$appId = "machineshop44.MarketAdvisor.1"
$desktop = [Environment]::GetFolderPath("Desktop")
$startDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
New-Item -ItemType Directory -Force -Path $startDir | Out-Null

function New-MaShortcut([string]$lnkPath) {
    $w = New-Object -ComObject WScript.Shell
    $sc = $w.CreateShortcut($lnkPath)
    $sc.TargetPath = $pythonw
    $sc.Arguments = "`"$main`""
    $sc.WorkingDirectory = $src
    $sc.WindowStyle = 1
    $sc.Description = "Market Advisor"
    $sc.IconLocation = "$ico,0"
    $sc.Save()
    [System.Runtime.InteropServices.Marshal]::ReleaseComObject($sc) | Out-Null
    [System.Runtime.InteropServices.Marshal]::ReleaseComObject($w) | Out-Null
}

# Set System.AppUserModel.ID on an existing .lnk (required for taskbar icon remap)
$aumidType = @"
using System;
using System.Runtime.InteropServices;

public static class LnkAumid {
  [StructLayout(LayoutKind.Sequential, Pack=4)]
  public struct PROPERTYKEY {
    public Guid fmtid;
    public UInt32 pid;
  }

  [StructLayout(LayoutKind.Sequential)]
  public struct PROPVARIANT {
    public UInt16 vt;
    public UInt16 w1, w2, w3;
    public IntPtr pszVal;
    public IntPtr p2;
  }

  [ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"),
   InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  public interface IPropertyStore {
    uint GetCount(out uint cProps);
    uint GetAt(uint iProp, out PROPERTYKEY pkey);
    uint GetValue(ref PROPERTYKEY key, out PROPVARIANT pv);
    uint SetValue(ref PROPERTYKEY key, ref PROPVARIANT pv);
    uint Commit();
  }

  [DllImport("ole32.dll")]
  static extern void PropVariantClear(ref PROPVARIANT pvar);

  [DllImport("shell32.dll", CharSet=CharSet.Unicode, PreserveSig=false)]
  static extern void SHGetPropertyStoreFromParsingName(
      string pszPath, IntPtr pbc, uint flags,
      [MarshalAs(UnmanagedType.LPStruct)] Guid riid,
      out IPropertyStore ppv);

  public static void SetAppUserModelId(string lnkPath, string appId) {
    Guid iid = new Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99");
    IPropertyStore store;
    // GPS_READWRITE = 0x2
    SHGetPropertyStoreFromParsingName(lnkPath, IntPtr.Zero, 0x2, iid, out store);
    PROPERTYKEY key = new PROPERTYKEY();
    // PKEY_AppUserModel_ID
    key.fmtid = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3");
    key.pid = 5;
    PROPVARIANT pv = new PROPVARIANT();
    pv.vt = 31; // VT_LPWSTR
    pv.pszVal = Marshal.StringToCoTaskMemUni(appId);
    store.SetValue(ref key, ref pv);
    store.Commit();
    PropVariantClear(ref pv);
    Marshal.ReleaseComObject(store);
  }
}
"@

try {
    Add-Type -TypeDefinition $aumidType -ErrorAction Stop
} catch {
    # type may already be loaded from a prior run in this session
    if (-not ([System.Management.Automation.PSTypeName]'LnkAumid').Type) { throw }
}

$desktopLnk = Join-Path $desktop "Market Advisor.lnk"
$startLnk = Join-Path $startDir "Market Advisor.lnk"
New-MaShortcut $desktopLnk
New-MaShortcut $startLnk
[LnkAumid]::SetAppUserModelId($desktopLnk, $appId)
[LnkAumid]::SetAppUserModelId($startLnk, $appId)

# Keep VBS as a thin launcher that calls the same pythonw+main (icon still from Start Menu AUMID match)
$vbs = @"
' Market Advisor launcher (no CMD window)
Option Explicit
Dim sh
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "$($src.Replace('\','\\'))"
sh.Run """$($pythonw.Replace('\','\\'))"" ""$($main.Replace('\','\\'))""", 0, False
"@
# VBS needs single backslashes in paths when using quotes - write carefully
$vbs = @"
Option Explicit
Dim sh
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "$src"
sh.Run """$pythonw"" ""$main""", 0, False
"@
Set-Content -Path (Join-Path $root "Start Market Advisor.vbs") -Value $vbs -Encoding ASCII

Write-Host "Desktop shortcut: $desktopLnk"
Write-Host "Start Menu shortcut: $startLnk"
Write-Host "Target: $pythonw"
Write-Host "Icon: $ico"
Write-Host "AppUserModelID: $appId"
Write-Host "Quit any running Market Advisor, then start from the Desktop shortcut."
