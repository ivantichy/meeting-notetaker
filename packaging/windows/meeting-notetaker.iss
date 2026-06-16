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
#define MyAppVersion "1.0.0"

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
; Remove logs and runtime files created next to the exe at run time so the
; install directory is left clean. User data (notes\, models\, config.json) is
; intentionally left in place; see the comment block below.
Type: files; Name: "{app}\notetaker.log"
Type: dirifempty; Name: "{app}"

; NOTE: notes\, models\ and config.json are created next to the exe while the
; app runs. They are deliberately NOT auto-deleted on uninstall so the user
; keeps their transcripts, downloaded Whisper models and (secret) ICS URL.
