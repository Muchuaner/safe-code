; 本地构建单个离线安装文件；不发布任何内容。
#define AppVersion "Beta-0.4.3.2"
#ifndef AppSource
  #define AppSource "..\dist\SafeCode-Beta-0.4.3.2-win-x64"
#endif
#ifndef RuntimeSource
  #define RuntimeSource "..\.local\installer\MicrosoftEdgeWebView2RuntimeInstallerX64.exe"
#endif
#ifndef SetupOutput
  #define SetupOutput "..\dist"
#endif
[Setup]
AppId={{C219972F-04EC-4647-92E0-1F93DB8DDE29}
AppName=Safe Code
AppVersion={#AppVersion}
VersionInfoVersion=0.4.3.2
AppPublisher=Safe Code
DefaultDirName={localappdata}\Programs\Safe Code
DefaultGroupName=Safe Code
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.19041
OutputDir={#SetupOutput}
OutputBaseFilename=SafeCode-{#AppVersion}-Setup-x64
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\SafeCode.exe
CloseApplications=yes
RestartApplications=no
[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
[Files]
Source: "{#AppSource}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#RuntimeSource}"; DestDir: "{tmp}"; DestName: "WebView2-offline.exe"; Flags: deleteafterinstall
[Icons]
Name: "{userdesktop}\Safe Code"; Filename: "{app}\SafeCode.exe"
Name: "{userprograms}\Safe Code"; Filename: "{app}\SafeCode.exe"
[Run]
Filename: "{tmp}\WebView2-offline.exe"; Parameters: "/silent /install"; Flags: waituntilterminated; Check: NeedsWebView
Filename: "{app}\SafeCode.exe"; Description: "启动 Safe Code"; Flags: nowait postinstall skipifsilent
[Code]
function NeedsWebView: Boolean;
var Version: String;
begin
  Result := not ((RegQueryStringValue(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0')) or
    (RegQueryStringValue(HKLM32, 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0')) or
    (RegQueryStringValue(HKLM64, 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0')));
end;
