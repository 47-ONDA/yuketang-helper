; Inno Setup 6 脚本: 雨课堂助手 Windows 安装包
; 由 packaging/build-windows.ps1 调用, 输出到 dist\installer\

[Setup]
AppId={8E5C2F1A-6B7D-4A0E-9C3F-5A1B2C3D4E5F}
AppName=雨课堂助手
AppVersion=1.1.0
AppVerName=雨课堂助手 1.1.0
DefaultDirName={autopf}\YuketangHelper
DefaultGroupName=雨课堂助手
OutputDir=..\dist\installer
OutputBaseFilename=yuketang-helper-1.1.0-windows-setup
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\ykt-helper-gui.exe
; 运行数据在 %USERPROFILE%\.yuketang-helper, 卸载不影响用户课件与配置

[Languages]
Name: "chinese"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Files]
Source: "..\dist\ykt-helper-gui\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\雨课堂助手"; Filename: "{app}\ykt-helper-gui.exe"
Name: "{autodesktop}\雨课堂助手"; Filename: "{app}\ykt-helper-gui.exe"

[Run]
Filename: "{app}\ykt-helper-gui.exe"; Description: "立即启动雨课堂助手"; Flags: nowait postinstall skipifsilent
