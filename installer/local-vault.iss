; 本地密匣：每用户安装，默认保险库随程序安装目录保存。
#ifndef AppVersion
  #define AppVersion "1.0.2"
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
WelcomeLabel2=此向导将在您的电脑上安装 [name/ver]。%n%n保险库默认保存在程序安装目录，您可以在软件“设置”中更改保存位置。首次打开新版时会自动迁移旧版默认目录中的保险库；如有冲突或目录不可用，原数据会保留并提示处理。升级和普通卸载都会保留保险库数据。%n%n继续前，请保存编辑内容并关闭正在运行的本地密匣。请选择您有写入权限的安装目录。
FinishedLabel=本地密匣已安装完成。%n%n保险库默认与 LocalVault.exe 保存在同一目录，已设置的自定义位置会继续使用。全新使用时，请设置主密码；请妥善保管主密码，遗忘后无法读取加密正文。

; 隔离安装验证显式定义 AppSkipIcons，测试包完全不编译任何快捷方式。
; 正式构建不定义此参数，照常提供开始菜单和可选桌面快捷方式。
#ifndef AppSkipIcons
[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; Flags: unchecked
#endif

[Files]
; 数据与本机位置配置不进入安装包，也不进入卸载文件清单。
Source: "{#AppSourceRoot}\*"; DestDir: "{app}"; Excludes: "*.lvault,*.lvexport,*.lvbackup,*.bak,*.lock,*.tmp,location.json"; Flags: ignoreversion recursesubdirs createallsubdirs

#ifndef AppSkipIcons
[Icons]
Name: "{autoprograms}\本地密匣"; Filename: "{app}\LocalVault.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\本地密匣"; Filename: "{app}\LocalVault.exe"; WorkingDir: "{app}"; Tasks: desktopicon
#endif

[Run]
Filename: "{app}\LocalVault.exe"; Description: "打开本地密匣"; Flags: nowait postinstall skipifsilent

; 不设置删除用户数据的目录、注册表项或卸载脚本。卸载时非空安装目录保留。
