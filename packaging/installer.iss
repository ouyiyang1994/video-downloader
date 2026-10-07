; Inno Setup script for Video Downloader.
;
; Build with packaging\build-installer.ps1 (preferred) or directly:
;   ISCC.exe /DSourceDir=..\dist\VideoDownloader packaging\installer.iss
;
; Notes
; -----
; * [Files] carries an explicit Excludes list so that credentials, cookies,
;   databases, logs and downloaded videos can never be packaged, even if the
;   staging directory is not pristine.
; * There is deliberately no [UninstallDelete] section: uninstalling removes
;   only files this installer created. A user's .env, cookies.txt, downloads
;   and logs are never touched (under C:\Program Files they live in
;   %LOCALAPPDATA%\VideoDownloader anyway).

#define AppName "Video Downloader"
#define AppVersion "1.03"
#define AppPublisher "Video Downloader"
#define AppExeName "VideoDownloader.exe"

#ifndef IconFile
  #define IconFile AddBackslash(SourcePath) + "VideoDownloader.ico"
#endif
#ifndef SourceDir
  #define SourceDir AddBackslash(SourcePath) + "..\dist\VideoDownloader"
#endif
#ifndef InstallerOutputDir
  #define InstallerOutputDir AddBackslash(SourcePath) + "..\dist\installer"
#endif

[Setup]
AppId={{8F2C4B1A-6D3E-4A57-9C21-7E5B0D9A4C33}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\VideoDownloader
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir={#InstallerOutputDir}
; Output filename is fixed by the release checklist: video DL_<version>.exe
OutputBaseFilename=video DL_{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExeName}
SetupIconFile={#IconFile}
VersionInfoVersion=1.1.0.0
VersionInfoTextVersion={#AppVersion}
VersionInfoProductTextVersion={#AppVersion}
VersionInfoDescription={#AppName} Setup
VersionInfoProductName={#AppName}
AllowNoIcons=yes
DisableReadyPage=no

[Languages]
; The Simplified Chinese translation ships with the project (and is also
; available from jrsoftware). Fall back to English if it is missing, so the
; build never depends on how Inno Setup happens to be installed.
#if FileExists(AddBackslash(SourcePath) + "languages\ChineseSimplified.isl")
Name: "chinese"; MessagesFile: "languages\ChineseSimplified.isl"
#endif
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; \
    GroupDescription: "{cm:AdditionalIcons}"; Flags: checkedonce

[Files]
; Excludes is defence in depth: the staging folder is scanned before this runs.
Source: "{#SourceDir}\*"; DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs; \
    Excludes: ".env,secrets\*,secrets,cookies.txt,downloads\*,downloads,logs\*,logs,*.db,*.db-shm,*.db-wal,*.sqlite,*.sqlite3,*.log,*.part"

[Icons]
; WorkingDir is pinned to {app} so the shortcut never depends on the shell's
; current directory.
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; \
    WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; \
    Description: "{cm:LaunchProgram,{#AppName}}"; \
    Flags: nowait postinstall skipifsilent
