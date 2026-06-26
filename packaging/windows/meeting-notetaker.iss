; Inno Setup script for Meeting Notetaker
; Builds a per-user (no-admin) installer that bundles the PyInstaller onedir
; output (dist\MeetingNotetaker\) into MeetingNotetaker-Setup.exe.
;
; Features:
;   * Installs under %LOCALAPPDATA%\Programs\MeetingNotetaker (per user, no admin).
;   * Start Menu shortcut "Meeting Notetaker".
;   * Autostart at login via HKCU\...\Run value "MeetingNotetaker" (windowless;
;     the bundled exe is a GUI app that lives in the tray).
;   * Uninstaller removes the app and the autostart entry.
;
; Compile from the project root, e.g.:
;   ISCC.exe packaging\windows\meeting-notetaker.iss
; (SourceDir below is set so relative [Files] paths resolve from the repo root.)

#define MyAppName "Meeting Notetaker"
#define MyAppExeName "MeetingNotetaker.exe"
#define MyAppPublisher "Meeting Notetaker"
; Verzi předává release workflow z gitového tagu: ISCC /DMyAppVersion=0.3.1
; (bez tagu, např. lokální build, zůstane dev placeholder).
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0-dev"
#endif

[Setup]
AppId={{8B2A1F3C-7D4E-4A9B-9C1E-7F0A2B3C4D5E}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
; Per-user install: no administrator rights required.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={localappdata}\Programs\MeetingNotetaker
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; Source paths in [Files] are relative to the repo root.
SourceDir=..\..
OutputDir=packaging\windows\Output
OutputBaseFilename=MeetingNotetaker-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "autostart"; Description: "Start {#MyAppName} automatically when I sign in to Windows"; GroupDescription: "Startup:"
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
; Bundle the entire PyInstaller onedir output.
Source: "dist\MeetingNotetaker\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Registry]
; Autostart at login. The bundled exe is a windowless (no-console) GUI app that
; shows a tray icon, so pointing the Run value straight at it launches it
; silently. uninsdeletevalue removes this entry on uninstall.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
    ValueType: string; ValueName: "MeetingNotetaker"; \
    ValueData: """{app}\{#MyAppExeName}"""; \
    Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName} now"; \
    WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Tidy up the install directory after the standard uninstaller has removed the
; recorded app files. User data created next to the exe at run time
; (notes\, models\, config.json, notetaker.log) is NOT listed here: by default
; it is kept, and it is only removed when the user explicitly opts in via the
; confirmation prompt in the [Code] section below (CurUninstallStepChanged).
Type: dirifempty; Name: "{app}"

; NOTE: notes\, models\, config.json and notetaker.log are created next to the
; exe while the app runs. They are deliberately NOT auto-deleted on uninstall:
; the uninstaller asks the user first and defaults to keeping them, so the user
; keeps their transcripts, downloaded Whisper models and (secret) ICS URL
; unless they explicitly choose otherwise.

[Code]
{ On uninstall, ask whether the user also wants to delete their personal data
  (recordings/transcripts in notes\, downloaded Whisper models\, config.json
  and notetaker.log) that lives next to the installed exe. The default answer
  is "No" (keep the data); deletion only happens on an explicit "Yes". The
  standard uninstaller still removes all normal program files regardless. }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Response: Integer;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    Response := MsgBox(
      'Smazat i vaše nahrávky, přepisy a nastavení (složky notes a models, '
        + 'config.json a notetaker.log)?' + #13#10 + #13#10
        + 'Pokud zvolíte Ne, zůstanou na disku.',
      mbConfirmation, MB_YESNO or MB_DEFBUTTON2);
    if Response = IDYES then
    begin
      { Folders: notes\ (transcripts + index) and models\ (downloaded models). }
      DelTree(ExpandConstant('{app}\notes'), True, True, True);
      DelTree(ExpandConstant('{app}\models'), True, True, True);
      { Individual files: config and runtime log. }
      DeleteFile(ExpandConstant('{app}\config.json'));
      DeleteFile(ExpandConstant('{app}\notetaker.log'));
    end;
  end;
end;
