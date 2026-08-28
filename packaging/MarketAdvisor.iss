; Market Advisor Windows installer (ytarr/Arr-style: per-user Start Menu install)
; Built by publish-exe-to-drive.ps1 after staging release\installer-payload\
;
; Trading-desk note: Plex migrate prefers the portable zip (settings + Restore-
; Sessions). This installer raises Packaging with Start Menu / uninstall +
; LocalAppData install. Unsigned (same as ytarr / Arrs Hub) — SmartScreen may warn.

#ifndef MyAppVersion
  #define MyAppVersion "1.29.3"
#endif

#define MyAppName "Market Advisor"
#define MyAppNameCompact "MarketAdvisor"
#define MyAppPublisher "machineshop44"
#define MyAppURL "https://github.com/machineshop44"
#define MyAppExeName "MarketAdvisor.exe"

[Setup]
AppId={{B8D4F02C-5E3A-4C9B-A126-9208D1FFE102}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
DefaultDirName={localappdata}\Programs\{#MyAppNameCompact}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
AllowNoIcons=yes
OutputDir=..\release
OutputBaseFilename={#MyAppNameCompact}-{#MyAppVersion}-x64
SetupIconFile=..\Src\app_icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=force
CloseApplicationsFilter=MarketAdvisor.exe
RestartApplications=no
InfoBeforeFile=..\packaging\INSTALL-PLEX.txt

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Dirs]
Name: "{app}\Src"; Flags: uninsneveruninstall

[Files]
; Staging folder built by publish-exe-to-drive.ps1 (no live secrets in default installer)
Source: "..\release\installer-payload\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\Src\app_icon.ico"; WorkingDir: "{app}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\Src\app_icon.ico"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM {#MyAppExeName} /T"; Flags: runhidden skipifdoesntexist; RunOnceId: "KillMarketAdvisorUninstall"

[Code]
procedure KillApp();
var
  ResultCode: Integer;
begin
  Exec(
    ExpandConstant('{sys}\taskkill.exe'),
    '/F /IM {#MyAppExeName} /T',
    '',
    SW_HIDE,
    ewWaitUntilTerminated,
    ResultCode
  );
  Sleep(750);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  NeedsRestart := False;
  KillApp();
  Result := '';
end;
