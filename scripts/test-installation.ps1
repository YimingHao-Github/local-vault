[CmdletBinding()]
param([string]$IsccPath = '')

$ErrorActionPreference = 'Stop'
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
Set-Location -LiteralPath $projectRoot
$python = Join-Path $projectRoot '.venv-build/Scripts/python.exe'
$testRoot = Join-Path $projectRoot ('.tools/installation-test/' + [guid]::NewGuid().ToString('N'))
$installRoot = Join-Path $testRoot 'app'
$profileRoot = Join-Path $testRoot 'profile'
$dataRoot = Join-Path $profileRoot 'LocalVault'
$vaultPath = Join-Path $dataRoot 'vault.lvault'
$reportRoot = Join-Path $projectRoot 'build'
$uninstallKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{9F903D62-C3EA-4F18-9544-C1CE2EF5A762}_is1'
if ([string]::IsNullOrWhiteSpace($IsccPath)) {
    $IsccPath = Join-Path $projectRoot '.tools/InnoSetup/ISCC.exe'
}
if (-not $testRoot.StartsWith($projectRoot + [IO.Path]::DirectorySeparatorChar,
        [StringComparison]::OrdinalIgnoreCase)) { throw '测试目录必须在工程内。' }
if (Test-Path -LiteralPath $uninstallKey) {
    $existingInstall = (Get-ItemProperty -LiteralPath $uninstallKey).InstallLocation.TrimEnd('\')
    if (-not $existingInstall.Equals($installRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw '电脑中已经安装正式本地密匣。请在干净测试环境执行安装测试，避免修改正式安装。'
    }
}
if (Test-Path -LiteralPath $vaultPath) {
    throw '虚构安装测试目录已存在。为避免覆盖文件，请先保管旧测试目录后再运行。'
}
New-Item -ItemType Directory -Force -Path $dataRoot, $reportRoot | Out-Null
$createFake = @'
import sys
from pathlib import Path
from local_vault.core import Vault
vault = Vault(Path(sys.argv[1]))
vault.create("安装验证虚构主密码-12345")
vault.put_entry("虚构安装条目", "仅用于验证安装与升级\n  保留空格  \n密码：FAKE-INSTALL-ONLY\n")
vault.lock()
'@
& $python -c $createFake $vaultPath
if ($LASTEXITCODE -ne 0) { throw '准备虚构测试数据失败。' }
$originalHash = (Get-FileHash -LiteralPath $vaultPath -Algorithm SHA256).Hash

function Invoke-TestInstaller {
    param([string]$SetupPath, [string]$LogName)
    $process = Start-Process -FilePath $SetupPath -ArgumentList @(
        '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-', '/NOICONS',
        ('/DIR="' + $installRoot + '"'), ('/LOG="' + (Join-Path $reportRoot $LogName) + '"')
    ) -PassThru -Wait -WindowStyle Hidden
    if ($process.ExitCode -ne 0) { throw "安装验证失败，退出码：$($process.ExitCode)" }
}

function Invoke-InstalledSmoke {
    param([string]$ReportName)
    $executable = Join-Path $installRoot 'LocalVault.exe'
    if (-not (Test-Path -LiteralPath $executable)) { throw '安装后找不到程序。' }
    $process = Start-Process -FilePath $executable -ArgumentList @(
        '--smoke-test', '--report', ('"' + (Join-Path $reportRoot $ReportName) + '"')
    ) -PassThru -Wait -WindowStyle Hidden
    if ($process.ExitCode -ne 0) { throw '安装后程序自检失败。' }
}

function Invoke-InstalledWindow {
    # 模拟独立 Windows 用户资料，实际启动安装版并确认默认数据目录。
    $previousLocalAppData = $env:LOCALAPPDATA
    $process = $null
    try {
        $env:LOCALAPPDATA = $profileRoot
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
            throw '安装版未使用模拟用户的默认保险库目录。'
        }
        if (-not $process.CloseMainWindow()) { throw '安装版窗口无法正常关闭。' }
        if (-not $process.WaitForExit(10000)) { throw '安装版关闭超时。' }
        if ($process.ExitCode -ne 0) { throw '安装版退出异常。' }
    } finally {
        $env:LOCALAPPDATA = $previousLocalAppData
        if ($null -ne $process -and -not $process.HasExited) {
            # 只终止本测试刚创建的进程。
            $process.Kill()
            $process.WaitForExit()
        }
    }
}

function Assert-DataPreserved {
    if ((Get-FileHash -LiteralPath $vaultPath -Algorithm SHA256).Hash -ne $originalHash) {
        throw '安装过程修改了用户数据。'
    }
    $verifyFake = @'
import sys
from pathlib import Path
from local_vault.core import Vault
vault = Vault(Path(sys.argv[1]))
vault.unlock("安装验证虚构主密码-12345")
entries = vault.list_titles()
assert len(entries) == 1
assert vault.get_entry(entries[0][0]).body == "仅用于验证安装与升级\n  保留空格  \n密码：FAKE-INSTALL-ONLY\n"
'@
    & $python -c $verifyFake $vaultPath
    if ($LASTEXITCODE -ne 0) { throw '安装或升级后无法读取原有正文。' }
}

$version = (& $python -c 'from local_vault import __version__; print(__version__)' | Out-String).Trim()
if ($LASTEXITCODE -ne 0) { throw '读取版本失败。' }
$setup = Join-Path $projectRoot "dist/LocalVault-Setup-$version.exe"
Invoke-TestInstaller $setup 'installation.log'
Invoke-InstalledSmoke 'installed-smoke-report.json'
Invoke-InstalledWindow
Assert-DataPreserved

# 在隔离目录编译具有同一应用编号的更高安装版本，验证实际升级路径。
$numbers = $version.Split('.')
$upgradeVersion = "$($numbers[0]).$($numbers[1]).$([int]$numbers[2] + 1)"
& $IsccPath /Q "/DAppVersion=$upgradeVersion" "/O$testRoot" (Join-Path $projectRoot 'installer/local-vault.iss')
if ($LASTEXITCODE -ne 0) { throw '测试用升级安装包编译失败。' }
Invoke-TestInstaller (Join-Path $testRoot "LocalVault-Setup-$upgradeVersion.exe") 'upgrade.log'
Invoke-InstalledSmoke 'upgraded-smoke-report.json'
Invoke-InstalledWindow
Assert-DataPreserved
$registeredVersion = (Get-ItemProperty -LiteralPath $uninstallKey).DisplayVersion
if ($registeredVersion -ne $upgradeVersion) { throw '升级后的注册版本不正确。' }

$uninstaller = Join-Path $installRoot 'unins000.exe'
$process = Start-Process -FilePath $uninstaller -ArgumentList @(
    '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
    ('/LOG="' + (Join-Path $reportRoot 'uninstallation.log') + '"')
) -PassThru -Wait -WindowStyle Hidden
if ($process.ExitCode -ne 0) { throw '卸载测试失败。' }
Assert-DataPreserved
if (Test-Path -LiteralPath $uninstallKey) { throw '卸载后的应用注册信息未清理。' }

[ordered]@{
    应用 = '本地密匣'
    安装版本 = $version
    升级测试版本 = $upgradeVersion
    安装 = '通过'
    安装后自检 = '通过'
    升级后自检 = '通过'
    安装与升级后正常启动主窗口 = '通过'
    默认用户数据目录 = '通过'
    原保险库逐字节保留 = '通过'
    原中文正文读取 = '通过'
    卸载保留保险库 = '通过'
    数据目录 = $dataRoot
    限制 = '升级测试包仅将安装版本增加一位，应用源码保持当前版本；此包不对外发布。'
} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $reportRoot 'installation-report.json') -Encoding utf8
Write-Host '安装、升级和卸载保留虚构保险库验证通过。'
