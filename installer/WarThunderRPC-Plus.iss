; WarThunderRPC-Plus installer.
;
; Built via build.ps1, which passes:
;   /DAppVersion=<version>      e.g. 1.0.0
;   /DWtrpcDistDir=<path>       PyInstaller onedir output (wtrpc\ folder)
;   /DWatcherExe=<path>         compiled wtrpc-watcher.exe
;
; Output is written to whatever directory ISCC's /O flag points at (dist\).

#define MyAppName "WarThunderRPC-Plus"
#define MyAppId "{{A9F5E7B2-6C3D-4F1A-9E42-7B3C8D2F5A61}}"

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef WtrpcDistDir
  #define WtrpcDistDir "..\build\pyinstaller-dist\wtrpc"
#endif
#ifndef WatcherExe
  #define WatcherExe "..\build\watcher\wtrpc-watcher.exe"
#endif

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#AppVersion}
AppPublisher=Chawannua
DefaultDirName={localappdata}\Programs\WarThunderRPC-Plus
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
DisableWelcomePage=no
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputBaseFilename=WarThunderRPC-Plus-Setup-{#AppVersion}
SetupIconFile=..\logo.ico
UninstallDisplayIcon={app}\wtrpc-watcher.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[InstallDelete]
; An upgrade copies over the old bundle, so modules a new build no longer
; ships would linger forever. Replace the app folder wholesale instead.
Type: filesandordirs; Name: "{app}\wtrpc"

[Files]
Source: "{#WtrpcDistDir}\*"; DestDir: "{app}\wtrpc"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#WatcherExe}"; DestDir: "{app}"; Flags: ignoreversion

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "WarThunderRPC-Plus"; ValueData: """{app}\wtrpc-watcher.exe"""; Flags: uninsdeletevalue

[Run]
; Start the watcher right after install/upgrade, silently, unconditionally
; (deliberately not a "postinstall" flag, since that renders as an optional
; checkbox on the finished page -- this should just always happen).
Filename: "{app}\wtrpc-watcher.exe"; Flags: nowait runhidden

[Code]
procedure StopRunningApp();
var
  ResultCode: Integer;
  WatcherPath: String;
begin
  WatcherPath := ExpandConstant('{app}\wtrpc-watcher.exe');
  if FileExists(WatcherPath) then
    Exec(WatcherPath, '--stop', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);

  { Fallback in case the watcher wasn't running, is too old to know --stop,
    or didn't stop its child. }
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM wtrpc-watcher.exe', '', SW_HIDE,
    ewWaitUntilTerminated, ResultCode);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM wtrpc.exe', '', SW_HIDE,
    ewWaitUntilTerminated, ResultCode);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  { Covers both a fresh run over a previous install and an upgrade: stop any
    running watcher/app before files get overwritten. This cannot live in
    InitializeSetup, which runs before the app constant has a value. }
  StopRunningApp();
  Result := '';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    StopRunningApp();
end;
