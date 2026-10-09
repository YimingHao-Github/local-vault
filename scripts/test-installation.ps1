[CmdletBinding()]
param([string]$IsccPath = '', [string]$LegacyRef = 'v1.0.1')

$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
Set-Location -LiteralPath $projectRoot
$python = Join-Path $projectRoot '.venv-build/Scripts/python.exe'
$testIdentifier = [guid]::NewGuid().ToString().ToUpperInvariant()
$testRoot = Join-Path $projectRoot ('.tools/installation-test/' + $testIdentifier)
$legacySource = Join-Path $testRoot 'legacy-source'
$legacyDist = Join-Path $testRoot 'legacy-dist'
$legacyBuild = Join-Path $testRoot 'legacy-build'
$fixtureSource = Join-Path $testRoot 'fixture-v1.0.0-source'
$newTestSource = Join-Path $testRoot 'new-package-source'
$installRoot = Join-Path $testRoot 'app'
$profileRoot = Join-Path $testRoot 'profile'
$dataRoot = Join-Path $profileRoot 'LocalVault'
$customRoot = Join-Path $testRoot 'custom-vault'
$freshInstallRoot = Join-Path $testRoot 'fresh-app'
$freshProfileRoot = Join-Path $testRoot 'fresh-profile'
$freshDataRoot = Join-Path $freshProfileRoot 'LocalVault'
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
foreach ($path in @($legacySource, $legacyDist, $legacyBuild, $fixtureSource, $newTestSource, $installRoot, $profileRoot, $dataRoot,
    $customRoot, $freshInstallRoot, $freshProfileRoot, $freshDataRoot, $scratchRoot, $temporaryRoot)) {
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

function Test-ExcludedPayload {
    param([IO.FileInfo]$File)
    return $File.Extension -in @('.lvault', '.lvexport', '.lvbackup', '.bak', '.lock', '.tmp') -or $File.Name -eq 'location.json'
}
if (Get-ChildItem -LiteralPath $newApplication -File -Recurse -Force | Where-Object { Test-ExcludedPayload $_ }) {
    throw '新版分发目录含数据或本机位置设置，请先自行保管，安装验证不会读取或复制这些文件。'
}
# 新版测试安装源只是分发程序的隔离副本；故意加入虚构禁止项，验证根目录和子目录都不会装入。
Copy-Item -LiteralPath $newApplication -Destination $newTestSource -Recurse
$excludedRelativePaths = @()
foreach ($relativeDirectory in @('', 'payload-probes')) {
    $probeRoot = if ($relativeDirectory) { Join-Path $newTestSource $relativeDirectory } else { $newTestSource }
    New-Item -ItemType Directory -Force -Path $probeRoot | Out-Null
    foreach ($name in @('vault.lvault', 'probe.lvexport', 'probe.lvbackup', 'probe.bak', 'probe.lock', 'probe.tmp', 'location.json')) {
        [IO.File]::WriteAllText((Join-Path $probeRoot $name), '仅用于安装包排除验证的虚构内容')
        $excludedRelativePaths += if ($relativeDirectory) { Join-Path $relativeDirectory $name } else { $name }
    }
}

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
    # 在构建目录生成正确版本资源，不改动归档的真实旧版源码。
    $legacyVersionResource = Join-Path $legacyBuild 'windows-version.txt'
    $legacyVersionTemplate = [IO.File]::ReadAllText((Join-Path $legacySource 'scripts/windows-version.txt'))
    $legacyVersionTemplate = $legacyVersionTemplate.Replace('(1, 0, 0, 0)', '(' + $legacyVersion.Replace('.', ', ') + ', 0)')
    $legacyVersionTemplate = $legacyVersionTemplate.Replace("'1.0.0'", "'$legacyVersion'")
    [IO.File]::WriteAllText($legacyVersionResource, $legacyVersionTemplate, [Text.UTF8Encoding]::new($false))
    Invoke-Checked $python @('-m', 'PyInstaller', '--noconfirm', '--clean', '--windowed', '--onedir',
        '--name', 'LocalVault', '--noupx', '--exclude-module', '_hashlib', '--paths', $legacySource,
        '--distpath', $legacyDist, '--workpath', $legacyBuild, '--specpath', $legacyBuild,
        '--version-file', $legacyVersionResource,
        (Join-Path $legacySource 'local_vault/__main__.py'))
} finally { Pop-Location }

# 8 字符旧密码和旧加密参数必须由真实 v1.0.0 生成；随后交由待测真实旧版读取。
# v1.0.1 的新建密码规则不作修改，也不冒用工作区核心来制造兼容性数据。
$fixtureRef = 'v1.0.0'
$fixtureCommit = (& git rev-parse --verify ($fixtureRef + '^{commit}') | Out-String).Trim()
if ($LASTEXITCODE -ne 0) { throw '虚构旧格式数据源码引用不存在。' }
$fixtureArchive = Join-Path $testRoot 'fixture-v1.0.0-source.zip'
Invoke-Checked 'git' @('archive', '--format=zip', ('--output=' + $fixtureArchive), $fixtureCommit)
Expand-Archive -LiteralPath $fixtureArchive -DestinationPath $fixtureSource
Push-Location -LiteralPath $fixtureSource
try {
    $createFake = @'
import json
import sys
from pathlib import Path
from local_vault import __version__
from local_vault.core import Vault
assert __version__ == "1.0.0"
folder = Path(sys.argv[1])
vault = Vault(folder / "vault.lvault")
vault.create("FAKE1234")  # 真实 v1.0.0 允许的 8 字符虚构密码。
vault.put_entry("虚构安装条目", "仅用于验证旧版到新版升级\n  保留空格  \n密码：FAKE-INSTALL-ONLY\n")
vault.export_file(folder / "legacy.lvexport", "FAKE1234")
vault.backup_file(folder / "legacy.lvbackup")
vault.lock()
(folder / "unrelated.bak").write_text("虚构无关副本，迁移不得处理", encoding="utf-8")
(folder / "unrelated.tmp").write_text("虚构无关临时文件，迁移不得处理", encoding="utf-8")
(folder / "unrelated.txt").write_text("虚构无关说明，迁移不得处理", encoding="utf-8")
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
$originalVaultHash = (Get-FileHash -LiteralPath (Join-Path $dataRoot 'vault.lvault') -Algorithm SHA256).Hash
$originalHashes = @{}
foreach ($file in Get-ChildItem -LiteralPath $dataRoot -File) {
    if ($file.Extension -ne '.lock' -and $file.Name -ne 'vault.lvault') {
        $originalHashes[$file.FullName] = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash
    }
}

function New-TestInstaller {
    param([string]$ApplicationRoot, [string]$ApplicationVersion)
    # 两个包使用同一随机测试编号；完全不编译快捷方式，不能仅依赖 /NOICONS。
    Invoke-Checked $IsccPath @('/Q', "/DAppVersion=$ApplicationVersion",
        "/DAppIdentifier=$testIdentifier", "/DAppSourceRoot=$ApplicationRoot",
        "/DAppMutexName=LocalVaultInstallationTest-$testIdentifier", '/DAppSkipIcons=1', "/O$testRoot",
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
function Assert-ExcludedPayloadAbsent {
    foreach ($relative in $excludedRelativePaths) {
        if (Test-Path -LiteralPath (Join-Path $installRoot $relative)) {
            throw "安装包带入了禁止的数据或配置：$relative"
        }
    }
}
function Assert-InstalledFiles {
    param([string]$ApplicationRoot, [string]$ExpectedVersion)
    foreach ($source in Get-ChildItem -LiteralPath $ApplicationRoot -File -Recurse) {
        if (Test-ExcludedPayload $source) { continue }
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
    param([string]$ExpectedDirectory)
    Assert-TestPath $ExpectedDirectory
    # 继承已隔离的 LOCALAPPDATA；显式使用不同工作目录，普通启动不传 --data-dir。
    $process = $null
    try {
        $process = Start-Process -FilePath (Join-Path $installRoot 'LocalVault.exe') -WorkingDirectory $scratchRoot -PassThru -WindowStyle Hidden
        if (-not $process.WaitForInputIdle(10000)) { throw '安装版窗口未在限定时间内就绪。' }
        for ($attempt = 0; $attempt -lt 50; $attempt++) {
            $process.Refresh()
            if ($process.HasExited) { throw '安装版启动后意外退出。' }
            if ($process.MainWindowTitle -like '本地密匣*') { break }
            Start-Sleep -Milliseconds 100
        }
        if ($process.MainWindowTitle -notlike '本地密匣*') { throw '未出现正常主窗口。' }
        $lockPath = Join-Path $ExpectedDirectory 'application.lock'
        if (-not (Test-Path -LiteralPath $lockPath)) { throw '安装版未锁定预期保存目录。' }
        # 文件可能在关闭后留存，因此实际尝试取得锁，不能只依据文件存在来判断当前目录。
        $verifyActiveLock = @'
import msvcrt
import sys
with open(sys.argv[1], "r+b") as stream:
    stream.seek(0)
    try:
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        pass
    else:
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        raise AssertionError("普通启动未持有预期保存目录的锁")
'@
        Invoke-Checked $python @('-c', $verifyActiveLock, $lockPath)
        if (Test-Path -LiteralPath (Join-Path $scratchRoot 'vault.lvault')) { throw '普通启动错误地使用了工作目录。' }
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
    param([string]$VaultDirectory = $dataRoot)
    Assert-TestPath $VaultDirectory
    $vaultPath = Join-Path $VaultDirectory 'vault.lvault'
    if (-not (Test-Path -LiteralPath $vaultPath) -or
        (Get-FileHash -LiteralPath $vaultPath -Algorithm SHA256).Hash -ne $originalVaultHash) {
        throw '安装、升级、迁移或卸载改变了虚构主库的字节。'
    }
    foreach ($path in $originalHashes.Keys) {
        if (-not (Test-Path -LiteralPath $path) -or
            (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash -ne $originalHashes[$path]) {
            throw '安装、升级、迁移或卸载修改了旧目录的无关虚构文件。'
        }
    }
}
function Assert-PackagedCompatibility {
    param([string]$ExpectedVersion, [string]$Stage, [string]$VaultDirectory = $dataRoot)
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
vault_folder = Path(sys.argv[7])
scratch = Path(sys.argv[3]) / sys.argv[5]
scratch.mkdir()
original = "仅用于验证旧版到新版升级\n  保留空格  \n密码：FAKE-INSTALL-ONLY\n"
vault = module.Vault(vault_folder / "vault.lvault")
vault.unlock("FAKE1234")
titles = vault.list_titles()
assert len(titles) == 1 and titles[0][1] == "虚构安装条目"
assert vault.get_entry(titles[0][0]).body == original
other = module.Vault(scratch / "import.lvault")
other.create("安装兼容验证专用长口令-1234567890")
expected_n = 32768 if package.__version__ == "1.0.0" else 131072
assert json.loads((scratch / "import.lvault").read_text(encoding="utf-8"))["kdf"]["n"] == expected_n
assert json.loads((vault_folder / "vault.lvault").read_text(encoding="utf-8"))["kdf"]["n"] == 32768
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
        $scratchRoot, $ExpectedVersion, $Stage, (Join-Path $reportRoot "$Stage-compatibility-report.json"), $VaultDirectory)
    Assert-DataPreserved $VaultDirectory
}

function Invoke-InstalledLocationScenario {
    param([string]$Scenario, [string]$LegacyDirectory, [string]$TargetDirectory)
    Assert-TestPath $LegacyDirectory
    Assert-TestPath $TargetDirectory
    $verifyLocation = @'
import hashlib
import json
import sys
import types
from pathlib import Path
from PyInstaller.archive.readers import CArchiveReader
archive = CArchiveReader(sys.argv[1]).open_embedded_archive("PYZ.pyz")
package = types.ModuleType("local_vault")
package.__path__ = []
sys.modules[package.__name__] = package
exec(archive.extract(package.__name__), package.__dict__)
modules = {}
for name in ("local_vault.core", "local_vault.location"):
    module = types.ModuleType(name)
    sys.modules[name] = module
    exec(archive.extract(name), module.__dict__)
    modules[name] = module
assert package.__version__ == sys.argv[6]
installation = Path(sys.argv[2])
legacy = Path(sys.argv[3])
target = Path(sys.argv[4])
scenario = sys.argv[5]
session_class = modules["local_vault.location"].LocationSession
session = session_class(installation, legacy)
try:
    assert session.directory == installation.resolve()
    if scenario == "custom":
        before = (installation / "vault.lvault").read_bytes()
        session.vault.unlock("FAKE1234")
        original = session.vault.get_entry(session.vault.list_titles()[0][0]).body
        session.change_directory(target)
        assert session.directory == target.resolve()
        assert (target / "vault.lvault").read_bytes() == before
        assert not (installation / "vault.lvault").exists()
        assert session.vault.get_entry(session.vault.list_titles()[0][0]).body == original
    elif scenario == "fresh":
        assert not session.vault.path.exists()
        session.vault.create("全新安装虚构验证口令-1234567890")
        session.vault.put_entry("全新虚构条目", "只用于安装目录默认保存验证")
        before = (installation / "vault.lvault").read_bytes()
    else:
        raise AssertionError("未知验证场景")
finally:
    session.close()
session = session_class(installation, legacy)
try:
    expected = target if scenario == "custom" else installation
    assert session.directory == expected.resolve()
    assert (expected / "vault.lvault").read_bytes() == before
    password = "FAKE1234" if scenario == "custom" else "全新安装虚构验证口令-1234567890"
    session.vault.unlock(password)
    body = session.vault.get_entry(session.vault.list_titles()[0][0]).body
    assert body == (original if scenario == "custom" else "只用于安装目录默认保存验证")
finally:
    session.close()
if scenario == "custom":
    # 用户切换位置后可在旧安装目录另留独立保险库；它不得被重启、升级或卸载处理。
    other = modules["local_vault.core"].Vault(installation / "vault.lvault")
    other.create("安装目录独立虚构库口令-1234567890")
    other.put_entry("独立虚构库", "自定义位置启用后不得覆盖这份库")
    other.lock()
report = {"版本": package.__version__, "来源": "实际安装程序的内嵌保存位置模块",
          "场景": scenario, "迁移或创建后重启读取": "通过",
          "保险库摘要": hashlib.sha256(before).hexdigest()}
Path(sys.argv[7]).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
'@
    Invoke-Checked $python @('-c', $verifyLocation, (Join-Path $installRoot 'LocalVault.exe'), $installRoot,
        $LegacyDirectory, $TargetDirectory, $Scenario, $version, (Join-Path $reportRoot "$Scenario-location-report.json"))
}

function Invoke-TestUninstaller {
    param([string]$LogName)
    $uninstaller = Join-Path $installRoot 'unins000.exe'
    Assert-TestPath $uninstaller
    $process = Start-Process -FilePath $uninstaller -ArgumentList @(
        '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
        ('/LOG="' + (Join-Path $reportRoot $LogName) + '"')
    ) -PassThru -Wait -WindowStyle Hidden
    if ($process.ExitCode -ne 0) { throw '卸载测试失败。' }
    if (Test-Path -LiteralPath $uninstallKey) { throw '卸载后的测试应用注册信息未清理。' }
    if (Test-Path -LiteralPath (Join-Path $installRoot 'LocalVault.exe')) { throw '卸载后程序文件仍存在。' }
}

$legacySetup = New-TestInstaller $legacyApplication $legacyVersion
$newSetup = New-TestInstaller $newTestSource $version
$upgradedInstallRoot = $installRoot
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
    Invoke-InstalledWindow $dataRoot
    Assert-PackagedCompatibility $legacyVersion 'legacy-installed'

    Invoke-TestInstaller $newSetup 'upgrade.log'
    Assert-InstalledFiles $newApplication $version
    Assert-ExcludedPayloadAbsent
    Assert-DataPreserved
    Invoke-InstalledSmoke 'upgraded-smoke-report.json' $version
    # 迁移由第一次普通启动触发；安装程序本身不读取用户保险库。
    Invoke-InstalledWindow $installRoot
    if (Test-Path -LiteralPath (Join-Path $dataRoot 'vault.lvault')) { throw '旧主库迁移成功后仍未移除。' }
    Assert-PackagedCompatibility $version 'upgraded' $installRoot

    Invoke-TestInstaller $newSetup 'repeated-upgrade.log'
    Assert-InstalledFiles $newApplication $version
    Invoke-InstalledWindow $installRoot
    Assert-PackagedCompatibility $version 'repeated-upgrade' $installRoot

    Invoke-InstalledLocationScenario 'custom' $dataRoot $customRoot
    Assert-DataPreserved $customRoot
    $otherVaultPath = Join-Path $installRoot 'vault.lvault'
    $otherVaultHash = (Get-FileHash -LiteralPath $otherVaultPath -Algorithm SHA256).Hash
    $configPath = Join-Path $dataRoot 'location.json'
    if (-not (Test-Path -LiteralPath $configPath)) { throw '自定义保存位置未持久化。' }
    $configHash = (Get-FileHash -LiteralPath $configPath -Algorithm SHA256).Hash
    Invoke-InstalledWindow $customRoot
    Invoke-TestInstaller $newSetup 'custom-position-upgrade.log'
    Assert-InstalledFiles $newApplication $version
    Invoke-InstalledWindow $customRoot
    Assert-PackagedCompatibility $version 'custom-position-upgraded' $customRoot
    if ((Get-FileHash -LiteralPath $otherVaultPath -Algorithm SHA256).Hash -ne $otherVaultHash -or
        (Get-FileHash -LiteralPath $configPath -Algorithm SHA256).Hash -ne $configHash) {
        throw '自定义位置启用后，重启或覆盖升级修改了安装目录独立库或位置设置。'
    }

    Invoke-TestUninstaller 'uninstallation.log'
    $testUninstalled = $true
    Assert-DataPreserved $customRoot
    if ((Get-FileHash -LiteralPath $otherVaultPath -Algorithm SHA256).Hash -ne $otherVaultHash -or
        (Get-FileHash -LiteralPath $configPath -Algorithm SHA256).Hash -ne $configHash) {
        throw '普通卸载删除或修改了安装目录保险库或自定义位置设置。'
    }

    # 在无旧数据、无位置配置的全新隔离用户目录中安装；仍不使用正式应用编号。
    $installRoot = $freshInstallRoot
    New-Item -ItemType Directory -Force -Path $freshProfileRoot | Out-Null
    $env:LOCALAPPDATA = $freshProfileRoot
    Invoke-TestInstaller $newSetup 'fresh-installation.log'
    $testUninstalled = $false
    Assert-InstalledFiles $newApplication $version
    Assert-ExcludedPayloadAbsent
    Invoke-InstalledWindow $installRoot
    if (Test-Path -LiteralPath (Join-Path $freshDataRoot 'vault.lvault')) { throw '全新安装错误地创建了旧默认位置的保险库。' }
    Invoke-InstalledLocationScenario 'fresh' $freshDataRoot $installRoot
    $freshVaultPath = Join-Path $installRoot 'vault.lvault'
    $freshVaultHash = (Get-FileHash -LiteralPath $freshVaultPath -Algorithm SHA256).Hash
    Invoke-InstalledWindow $installRoot
    if ((Get-FileHash -LiteralPath $freshVaultPath -Algorithm SHA256).Hash -ne $freshVaultHash) {
        throw '全新安装后的普通重启改变了保险库字节。'
    }
    Invoke-TestUninstaller 'fresh-uninstallation.log'
    $testUninstalled = $true
    if ((Get-FileHash -LiteralPath $freshVaultPath -Algorithm SHA256).Hash -ne $freshVaultHash) {
        throw '全新安装的普通卸载删除或修改了安装目录保险库。'
    }
} finally {
    foreach ($name in $savedEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name], 'Process')
    }
    if ($testInstalled -and -not $testUninstalled) {
        Write-Warning "测试未全部完成，隔离测试安装保留在 $installRoot，测试编号为 $testIdentifier。"
    }
}

$installationReport = [ordered]@{
    应用 = '本地密匣'
    旧版源码引用 = $LegacyRef
    旧版源码提交 = $legacyCommit
    虚构旧格式数据源码引用 = $fixtureRef
    虚构旧格式数据源码提交 = $fixtureCommit
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
    旧默认用户数据目录隔离 = '通过'
    全新默认库与程序安装目录一致且不随工作目录改变 = '通过'
    旧主库普通启动自动迁移且逐字节一致 = '通过'
    旧主库迁移完整成功后原文件移除 = '通过'
    旧导出旧备份及无关副本临时文件逐字节保留 = '通过'
    重复覆盖升级保留安装目录主库 = '通过'
    自定义位置迁移内容和密码保留且重启继续使用 = '通过'
    自定义位置覆盖升级保留配置与两处保险库 = '通过'
    根目录与子目录数据和位置配置均未打包安装 = '通过'
    实际打包模块使用旧8字符密码读取正文导入和恢复 = '通过'
    卸载保留虚构数据与清理测试应用注册 = '通过'
    正式应用注册与真实数据 = '未访问'
    剪贴板 = '未访问'
    数据目录 = $dataRoot
    升级安装目录 = $upgradedInstallRoot
    自定义保险库目录 = $customRoot
    全新安装目录 = $freshInstallRoot
    测试目录 = $testRoot
    限制 = 'Windows 隔离安装验证；测试包更换应用编号、安装保护编号和程序来源，并故意加入会被排除的虚构数据，不对外发布。自定义迁移与全新创建调用实际安装程序内嵌模块，普通重启使用真实主窗口。未验证另一台物理电脑或 Windows 10。'
} | ConvertTo-Json
$installationReport | Set-Content -LiteralPath (Join-Path $reportRoot 'installation-report.json') -Encoding utf8
# 连续验证两种旧版时，分别保留升级路径报告，避免后一次覆盖前一次结果。
$installationReport | Set-Content -LiteralPath (Join-Path $reportRoot "installation-$legacyVersion-to-$version-report.json") -Encoding utf8
Write-Host "真实 $legacyVersion 到 $version 安装、旧数据迁移、自定义位置、重复升级与普通卸载保留虚构数据验证通过。"
