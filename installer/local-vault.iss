; 本地密匣：每用户安装，程序和保险库数据分开保存。
#ifndef AppVersion
  #define AppVersion "1.0.1"
#endif
#ifndef AppIdentifier
  #define AppIdentifier "9F903D62-C3EA-4F18-9544-C1CE2EF5A762"
#endif
#ifndef AppSourceRoot
  #define AppSourceRoot "..\dist\LocalVault"
#endif
#ifndef AppMutexName
  #define AppMutexName "LocalVaultAppMutex"
#endif

[Setup]
AppId={{{#AppIdentifier}}
AppName=本地密匣
AppVersion={#AppVersion}
AppVerName=本地密匣 {#AppVersion}
AppPublisher=LocalVault
AppMutex={#AppMutexName}
DefaultDirName={localappdata}\Programs\LocalVault
DefaultGroupName=本地密匣
DisableDirPage=no
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
UsePreviousAppDir=yes
UsePreviousTasks=yes
OutputDir=..\dist
OutputBaseFilename=LocalVault-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\LocalVault.exe
CloseApplications=no
RestartApplications=no
SetupLogging=yes
VersionInfoVersion={#AppVersion}.0
VersionInfoDescription=本地密匣安装程序
VersionInfoProductName=本地密匣
VersionInfoProductVersion={#AppVersion}

[Languages]
Name: "chinesesimplified"; MessagesFile: "ChineseSimplified.isl"

[Messages]
WelcomeLabel2=此向导将在您的电脑上安装 [name/ver]。%n%n保险库数据保存在您的本地应用数据目录中，与程序文件分开存放。升级和卸载程序都会保留保险库数据。%n%n继续前，请保存编辑内容并关闭正在运行的本地密匣。
FinishedLabel=本地密匣已安装完成。%n%n首次打开时，请设置主密码。请妥善保管主密码，遗忘后无法读取加密正文。

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; Flags: unchecked

[Files]
Source: "{#AppSourceRoot}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\本地密匣"; Filename: "{app}\LocalVault.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\本地密匣"; Filename: "{app}\LocalVault.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\LocalVault.exe"; Description: "打开本地密匣"; Flags: nowait postinstall skipifsilent

; 不设置删除用户数据的目录、注册表项或卸载脚本。
