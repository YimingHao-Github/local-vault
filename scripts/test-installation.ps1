[CmdletBinding()]
param([string]$IsccPath = '', [string]$LegacyRef = 'v1.0.0')

$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
Set-Location -LiteralPath $projectRoot
$python = Join-Path $projectRoot '.venv-build/Scripts/python.exe'
$testIdentifier = [guid]::NewGuid().ToString().ToUpperInvariant()
$testRoot = Join-Path $projectRoot ('.tools/installation-test/' + $testIdentifier)
$legacySource = Join-Path $testRoot 'legacy-source'
$legacyDist = Join-Path $testRoot 'legacy-dist'
$legacyBuild = Join-Path $testRoot 'legacy-build'
$installRoot = Join-Path $testRoot 'app'
$profileRoot = Join-Path $testRoot 'profile'
$dataRoot = Join-Path $profileRoot 'LocalVault'
$scratchRoot = Join-Path $testRoot 'verification-data'
$temporaryRoot = Join-Path $testRoot 'temporary'
$reportRoot = Join-Path $projectRoot 'build'
# 只访问本轮随机测试编号的注册信息，不查询或修改正式安装的注册项。
$uninstallKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{' + $testIdentifier + '}_is1'
if ([string]::IsNullOrWhiteSpace($IsccPath)) { $IsccPath = Join-Path $projectRoot '.tools/InnoSetup/ISCC.exe' }

function Assert-TestPath {
    param([string]$Path)
    $resolved = [IO.Path]::GetFullPath($Path)
    if (-not $resolved.StartsWith($testRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "测试路径必须位于本轮隔离目录：$resolved"
    }
}
function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "测试命令失败，退出码 $LASTEXITCODE ：$Program" }
}
if (-not $testRoot.StartsWith($projectRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw '测试目录必须在工程内。'
}
if (Test-Path -LiteralPath $testRoot) { throw '测试目录已存在，拒绝覆盖。' }
foreach ($path in @($legacySource, $legacyDist, $legacyBuild, $installRoot, $profileRoot, $dataRoot, $scratchRoot, $temporaryRoot)) {
    Assert-TestPath $path
}
foreach ($tool in @($python, $IsccPath)) {
    if (-not (Test-Path -LiteralPath $tool)) { throw "缺少测试工具：$tool" }
}
$version = (& $python -c 'from local_vault import __version__; print(__version__)' | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $version -notmatch '^\d+\.\d+\.\d+$') { throw '读取新版源码版本失败。' }
$newApplication = Join-Path $projectRoot 'dist/LocalVault'
$newExecutable = Join-Path $newApplication 'LocalVault.exe'
if (-not (Test-Path -LiteralPath $newExecutable)) { throw '请先构建新版程序。' }
if ((Get-Item -LiteralPath $newExecutable).VersionInfo.ProductVersion -ne $version) {
    throw '打包程序版本与当前源码不一致，请先重新构建新版。'
}
New-Item -ItemType Directory -Force -Path $dataRoot, $legacyBuild, $scratchRoot, $temporaryRoot, $reportRoot | Out-Null

# 只读归档真实旧版源码，不切换分支、不回退或清理当前工作区。
$legacyCommit = (& git rev-parse --verify ($LegacyRef + '^{commit}') | Out-String).Trim()
if ($LASTEXITCODE -ne 0) { throw '旧版源码引用不存在。' }
$archivePath = Join-Path $testRoot 'legacy-source.zip'
Invoke-Checked 'git' @('archive', '--format=zip', ('--output=' + $archivePath), $legacyCommit)
Expand-Archive -LiteralPath $archivePath -DestinationPath $legacySource
Push-Location -LiteralPath $legacySource
try {
    $legacyVersion = (& $python -c 'from local_vault import __version__; print(__version__)' | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $legacyVersion -notmatch '^\d+\.\d+\.\d+$') { throw '读取旧版源码版本失败。' }
    if ([version]$legacyVersion -ge [version]$version) { throw '旧版源码版本必须低于新版。' }
    Invoke-Checked $python @('-m', 'PyInstaller', '--noconfirm', '--clean', '--windowed', '--onedir',
        '--name', 'LocalVault', '--noupx', '--exclude-module', '_hashlib', '--paths', $legacySource,
        '--distpath', $legacyDist, '--workpath', $legacyBuild, '--specpath', $legacyBuild,
        '--version-file', (Join-Path $legacySource 'scripts/windows-version.txt'),
        (Join-Path $legacySource 'local_vault/__main__.py'))
    $createFake = @'
import json
import sys
from pathlib import Path
from local_vault.core import Vault
folder = Path(sys.argv[1])
vault = Vault(folder / "vault.lvault")
vault.create("FAKE1234")  # 真实旧版允许的 8 字符虚构密码。
vault.put_entry("虚构安装条目", "仅用于验证旧版到新版升级\n  保留空格  \n密码：FAKE-INSTALL-ONLY\n")
vault.export_file(folder / "legacy.lvexport", "FAKE1234")
vault.backup_file(folder / "legacy.lvbackup")
vault.lock()
assert json.loads((folder / "vault.lvault").read_text(encoding="utf-8"))["kdf"]["n"] == 32768
'@
    Invoke-Checked $python @('-c', $createFake, $dataRoot)
} finally { Pop-Location }
$legacyApplication = Join-Path $legacyDist 'LocalVault'
$legacyExecutable = Join-Path $legacyApplication 'LocalVault.exe'
if ((Get-Item -LiteralPath $legacyExecutable).VersionInfo.ProductVersion -ne $legacyVersion) {
    throw '旧版打包程序版本不正确。'
}
$legacyExecutableHash = (Get-FileHash -LiteralPath $legacyExecutable -Algorithm SHA256).Hash
$newExecutableHash = (Get-FileHash -LiteralPath $newExecutable -Algorithm SHA256).Hash
if ($legacyExecutableHash -eq $newExecutableHash) { throw '新旧应用程序必须来自不同的真实源码。' }
$originalHashes = @{}
foreach ($file in Get-ChildItem -LiteralPath $dataRoot -File) {
    if ($file.Extension -ne '.lock') {
        $originalHashes[$file.FullName] = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash
    }
}

function New-TestInstaller {
    param([string]$ApplicationRoot, [string]$ApplicationVersion)
    # 两个包使用同一随机测试编号；不与正式安装编号、目录或快捷方式关联。
    Invoke-Checked $IsccPath @('/Q', "/DAppVersion=$ApplicationVersion",
        "/DAppIdentifier=$testIdentifier", "/DAppSourceRoot=$ApplicationRoot",
        "/DAppMutexName=LocalVaultInstallationTest-$testIdentifier", "/O$testRoot",
        (Join-Path $projectRoot 'installer/local-vault.iss')) | Out-Host
    $result = Join-Path $testRoot "LocalVault-Setup-$ApplicationVersion.exe"
    if (-not (Test-Path -LiteralPath $result)) { throw '未生成隔离测试安装包。' }
    return $result
}
function Invoke-TestInstaller {
    param([string]$SetupPath, [string]$LogName)
    $process = Start-Process -FilePath $SetupPath -ArgumentList @(
        '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-', '/NOICONS',
        ('/DIR="' + $installRoot + '"'), ('/LOG="' + (Join-Path $reportRoot $LogName) + '"')
    ) -PassThru -Wait -WindowStyle Hidden
    if ($process.ExitCode -ne 0) { throw "安装验证失败，退出码：$($process.ExitCode)" }
}
function Assert-InstalledFiles {
    param([string]$ApplicationRoot, [string]$ExpectedVersion)
    foreach ($source in Get-ChildItem -LiteralPath $ApplicationRoot -File -Recurse) {
        $relative = $source.FullName.Substring($ApplicationRoot.Length).TrimStart('\', '/')
        $destination = Join-Path $installRoot $relative
        if (-not (Test-Path -LiteralPath $destination) -or
            (Get-FileHash -LiteralPath $source.FullName -Algorithm SHA256).Hash -ne
            (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash) {
            throw "安装文件与实际打包文件不一致：$relative"
        }
    }
    $registered = Get-ItemProperty -LiteralPath $uninstallKey
    if ($registered.DisplayVersion -ne $ExpectedVersion -or
        -not $registered.InstallLocation.TrimEnd('\').Equals($installRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw '测试安装的版本或目录注册不正确。'
    }
    $executable = Join-Path $installRoot 'LocalVault.exe'
    $fileVersion = (Get-Item -LiteralPath $executable).VersionInfo
    if ($fileVersion.ProductVersion -ne $ExpectedVersion -or $fileVersion.FileVersion -ne $ExpectedVersion) {
        throw '实际安装程序的 Windows 文件版本不正确。'
    }
    $report = Join-Path $reportRoot "installed-version-$ExpectedVersion.json"
    $process = Start-Process -FilePath $executable -ArgumentList @('--version', '--report', ('"' + $report + '"')) -PassThru -Wait -WindowStyle Hidden
    if ($process.ExitCode -ne 0 -or
        (Get-Content -LiteralPath $report -Raw -Encoding utf8 | ConvertFrom-Json).版本 -ne $ExpectedVersion) {
        throw '实际安装程序的版本命令不正确。'
    }
}
function Invoke-InstalledSmoke {
    param([string]$ReportName, [string]$ExpectedVersion)
    $report = Join-Path $reportRoot $ReportName
    $process = Start-Process -FilePath (Join-Path $installRoot 'LocalVault.exe') -ArgumentList @(
        '--smoke-test', '--data-dir', ('"' + $scratchRoot + '"'), '--report', ('"' + $report + '"')
    ) -PassThru -Wait -WindowStyle Hidden
    if ($process.ExitCode -ne 0 -or
        (Get-Content -LiteralPath $report -Raw -Encoding utf8 | ConvertFrom-Json).版本 -ne $ExpectedVersion) {
        throw '安装版自检失败或版本不正确。'
    }
}
function Invoke-InstalledWindow {
    # 继承已隔离的 LOCALAPPDATA；普通主窗口仅检查标题和默认目录，不操作正文或剪贴板。
    $process = $null
    try {
        $process = Start-Process -FilePath (Join-Path $installRoot 'LocalVault.exe') -PassThru -WindowStyle Hidden
        if (-not $process.WaitForInputIdle(10000)) { throw '安装版窗口未在限定时间内就绪。' }
        for ($attempt = 0; $attempt -lt 50; $attempt++) {
            $process.Refresh()
            if ($process.HasExited) { throw '安装版启动后意外退出。' }
            if ($process.MainWindowTitle -like '本地密匣*') { break }
            Start-Sleep -Milliseconds 100
        }
        if ($process.MainWindowTitle -notlike '本地密匣*') { throw '未出现正常主窗口。' }
        if (-not (Test-Path -LiteralPath (Join-Path $dataRoot 'application.lock'))) {
            throw '安装版未使用隔离用户的默认保险库目录。'
        }
        if (-not $process.CloseMainWindow()) { throw '安装版窗口无法正常关闭。' }
        if (-not $process.WaitForExit(10000)) { throw '安装版关闭超时。' }
        if ($process.ExitCode -ne 0) { throw '安装版退出异常。' }
    } finally {
        if ($null -ne $process -and -not $process.HasExited) {
            # 只终止本测试刚创建的进程。
            $process.Kill()
            $process.WaitForExit()
        }
    }
}
function Assert-DataPreserved {
    foreach ($path in $originalHashes.Keys) {
        if (-not (Test-Path -LiteralPath $path) -or
            (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash -ne $originalHashes[$path]) {
            throw '安装、升级或卸载修改了虚构旧文件。'
        }
    }
}
function Assert-PackagedCompatibility {
    param([string]$ExpectedVersion, [string]$Stage)
    $verifyFake = @'
import json
import sys
import types
from pathlib import Path
from PyInstaller.archive.readers import CArchiveReader
# 直接执行安装程序内的代码对象，避免误用工作区模块替代交付版本。
archive = CArchiveReader(sys.argv[1]).open_embedded_archive("PYZ.pyz")
package = types.ModuleType("local_vault")
package.__path__ = []
sys.modules[package.__name__] = package
exec(archive.extract(package.__name__), package.__dict__)
module = types.ModuleType("local_vault.core")
sys.modules[module.__name__] = module
exec(archive.extract(module.__name__), module.__dict__)
assert package.__version__ == sys.argv[4]
folder = Path(sys.argv[2])
scratch = Path(sys.argv[3]) / sys.argv[5]
scratch.mkdir()
original = "仅用于验证旧版到新版升级\n  保留空格  \n密码：FAKE-INSTALL-ONLY\n"
vault = module.Vault(folder / "vault.lvault")
vault.unlock("FAKE1234")
titles = vault.list_titles()
assert len(titles) == 1 and titles[0][1] == "虚构安装条目"
assert vault.get_entry(titles[0][0]).body == original
other = module.Vault(scratch / "import.lvault")
other.create("安装兼容验证专用长口令-1234567890")
expected_n = 32768 if package.__version__ == "1.0.0" else 131072
assert json.loads((scratch / "import.lvault").read_text(encoding="utf-8"))["kdf"]["n"] == expected_n
assert json.loads((folder / "vault.lvault").read_text(encoding="utf-8"))["kdf"]["n"] == 32768
plan = other.inspect_import(folder / "legacy.lvexport", "FAKE1234")
other.commit_import(plan, {})
assert other.get_entry(other.list_titles()[0][0]).body == original
restored = module.Vault.for_restore(scratch / "restore.lvault")
restored.restore_backup(folder / "legacy.lvbackup", "FAKE1234")
assert restored.get_entry(restored.list_titles()[0][0]).body == original
vault.lock()
other.lock()
restored.lock()
report = {"版本": package.__version__, "来源": "实际安装程序的内嵌核心模块",
          "旧保险库、旧导出、旧备份和 8 字符密码": "通过"}
Path(sys.argv[6]).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
'@
    Invoke-Checked $python @('-c', $verifyFake, (Join-Path $installRoot 'LocalVault.exe'), $dataRoot,
        $scratchRoot, $ExpectedVersion, $Stage, (Join-Path $reportRoot "$Stage-compatibility-report.json"))
    Assert-DataPreserved
}

$legacySetup = New-TestInstaller $legacyApplication $legacyVersion
$newSetup = New-TestInstaller $newApplication $version
$savedEnvironment = @{}
foreach ($name in @('LOCALAPPDATA', 'TEMP', 'TMP')) {
    $savedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
$testInstalled = $false
$testUninstalled = $false
try {
    $env:LOCALAPPDATA = $profileRoot
    $env:TEMP = $temporaryRoot
    $env:TMP = $temporaryRoot
    Invoke-TestInstaller $legacySetup 'legacy-installation.log'
    $testInstalled = $true
    Assert-InstalledFiles $legacyApplication $legacyVersion
    Invoke-InstalledSmoke 'legacy-installed-smoke-report.json' $legacyVersion
    Invoke-InstalledWindow
    Assert-PackagedCompatibility $legacyVersion 'legacy-installed'

    Invoke-TestInstaller $newSetup 'upgrade.log'
    Assert-InstalledFiles $newApplication $version
    Invoke-InstalledSmoke 'upgraded-smoke-report.json' $version
    Invoke-InstalledWindow
    Assert-PackagedCompatibility $version 'upgraded'

    $uninstaller = Join-Path $installRoot 'unins000.exe'
    Assert-TestPath $uninstaller
    $process = Start-Process -FilePath $uninstaller -ArgumentList @(
        '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
        ('/LOG="' + (Join-Path $reportRoot 'uninstallation.log') + '"')
    ) -PassThru -Wait -WindowStyle Hidden
    if ($process.ExitCode -ne 0) { throw '卸载测试失败。' }
    $testUninstalled = $true
    Assert-DataPreserved
    if (Test-Path -LiteralPath $uninstallKey) { throw '卸载后的测试应用注册信息未清理。' }
    if (Test-Path -LiteralPath (Join-Path $installRoot 'LocalVault.exe')) { throw '卸载后程序文件仍存在。' }
} finally {
    foreach ($name in $savedEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name], 'Process')
    }
    if ($testInstalled -and -not $testUninstalled) {
        Write-Warning "测试未全部完成，隔离测试安装保留在 $installRoot，测试编号为 $testIdentifier。"
    }
}

[ordered]@{
    应用 = '本地密匣'
    旧版源码引用 = $LegacyRef
    旧版源码提交 = $legacyCommit
    安装版本 = $legacyVersion
    升级测试版本 = $version
    隔离应用编号 = $testIdentifier
    旧版程序摘要 = $legacyExecutableHash.ToLowerInvariant()
    新版程序摘要 = $newExecutableHash.ToLowerInvariant()
    真实旧源码到新版升级 = '通过'
    安装文件与交付程序摘要一致 = '通过'
    注册版本文件版本与实际版本命令一致 = '通过'
    安装后自检 = '通过'
    升级后自检 = '通过'
    安装与升级后正常启动主窗口 = '通过'
    默认用户数据目录隔离 = '通过'
    旧保险库旧导出旧备份与遗留副本逐字节保留 = '通过'
    实际打包模块使用旧8字符密码读取正文导入和恢复 = '通过'
    卸载保留虚构数据与清理测试应用注册 = '通过'
    正式应用注册与真实数据 = '未访问'
    剪贴板 = '未访问'
    数据目录 = $dataRoot
    测试目录 = $testRoot
    限制 = '本机 Windows 隔离安装验证；测试包仅更换应用编号、安装保护编号和程序来源，不对外发布。未验证另一台物理电脑或 Windows 10。'
} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $reportRoot 'installation-report.json') -Encoding utf8
Write-Host "真实 $legacyVersion 到 $version 安装、升级和卸载保留虚构数据验证通过。"
