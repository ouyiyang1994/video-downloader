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
; * There is deliberately no [UninstallDelete] section for user data:
;   uninstalling removes only files this installer created. A user's .env,
;   cookies.txt, downloads and logs are never touched (under C:\Program Files
;   they live in %LOCALAPPDATA%\VideoDownloader anyway). The one exception is
;   the generated login-helper folder, see below.
;
; The Chrome sign-in helper
; -------------------------
; ``recursesubdirs`` already carries the two folders the helper needs, and both
; names are fixed by ``core/chrome_bridge.py``:
;
;   {app}\chrome-extension\                  the unpacked extension the user
;                                            loads once from chrome://extensions
;   {app}\native_host\VideoDownloaderNativeHost\
;                                            the native messaging host Chrome
;                                            starts (``--onedir``: the whole
;                                            folder travels together)
;
; The *registration* - the HKCU key Chrome reads, and the manifest it points at
; - is written at run time by 「安装 / 更新登录助手」, under the user's own token,
; and never by Setup: an elevated Setup would write into the elevating account's
; hive. The [Registry] entries below therefore only *clean up* on uninstall.
; ``dontcreatekey`` keeps installation a no-op, and ``uninsdeletekey`` removes
; exactly our host key - never another extension's.

#define AppName "Video Downloader"
#define AppVersion "1.12"
#define AppPublisher "Video Downloader"
#define AppExeName "VideoDownloader.exe"
; Must equal core.chrome_bridge.HOST_NAME (and core.native_host.HOST_NAME).
; tests/test_chrome_bridge.py::test_the_installer_cleans_up_every_registry_root
; fails the suite if the two ever drift apart.
#define NativeHostName "com.videodownloader.cookies"

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
; Must equal {#AppVersion} as a dotted quad - Windows shows this in the file's
; properties and an upgrade compares it, so a stale value would ship an
; installer whose file version contradicts the product version.
; packaging/sync_version.py keeps it in step.
VersionInfoVersion=1.12.0.0
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
; recursesubdirs carries chrome-extension\ and native_host\ as well - see the
; header for why both must be present.
; ``cookies.txt`` alone is not enough: a browser export is named
; ``<domain>_cookies.txt`` just as often, and every managed session is
; ``<platform>_cookies.txt`` - so the wildcard form is listed too. Matching is
; case-insensitive on Windows, which is what we want here.
Source: "{#SourceDir}\*"; DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs; \
    Excludes: ".env,secrets\*,secrets,cookies.txt,*_cookies.txt,downloads\*,downloads,logs\*,logs,*.db,*.db-shm,*.db-wal,*.sqlite,*.sqlite3,*.log,*.part"

[Registry]
; Clean-up only - see the header. Setup never creates these keys, so it can
; never register the helper under the wrong account; on uninstall it removes
; exactly the key our host owns, in every Chromium browser this project
; supports, and leaves every other native messaging host alone.
Root: HKCU; Subkey: "Software\Google\Chrome\NativeMessagingHosts\{#NativeHostName}"; \
    Flags: dontcreatekey uninsdeletekey
Root: HKCU; Subkey: "Software\Microsoft\Edge\NativeMessagingHosts\{#NativeHostName}"; \
    Flags: dontcreatekey uninsdeletekey
Root: HKCU; Subkey: "Software\Chromium\NativeMessagingHosts\{#NativeHostName}"; \
    Flags: dontcreatekey uninsdeletekey
Root: HKCU; Subkey: "Software\BraveSoftware\Brave-Browser\NativeMessagingHosts\{#NativeHostName}"; \
    Flags: dontcreatekey uninsdeletekey

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

[UninstallDelete]
; The only thing removed beyond what Setup installed. When the application
; folder is not writable (an install under C:\Program Files) the helper copies
; its native messaging host and manifest into %LOCALAPPDATA%\VideoDownloader\
; native_host\ - regenerable files that would otherwise be orphaned once the
; registration is gone. Nothing else in that folder is touched: .env, secrets\,
; downloads\ and logs\ all survive an uninstall.
Type: filesandordirs; Name: "{localappdata}\VideoDownloader\native_host"
