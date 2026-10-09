[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$SetupPath)

$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
Set-Location -LiteralPath $projectRoot
$reportRoot = Join-Path $projectRoot 'build'
$setup = [IO.Path]::GetFullPath($SetupPath)
if (-not $setup.StartsWith($projectRoot + [IO.Path]::DirectorySeparatorChar,
        [StringComparison]::OrdinalIgnoreCase)) { throw '安装包必须在本工程内。' }
$checksumFile = "$setup.sha256"
$expectedHash = ([IO.File]::ReadAllText($checksumFile).Trim() -split '\s+')[0]
$actualHash = (Get-FileHash -LiteralPath $setup -Algorithm SHA256).Hash.ToLowerInvariant()
if ($expectedHash -ne $actualHash) { throw '安装包摘要与校验文件不一致。' }
$uninstallKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{9F903D62-C3EA-4F18-9544-C1CE2EF5A762}_is1'
$before = Get-ItemProperty -LiteralPath $uninstallKey
$installRoot = [IO.Path]::GetFullPath($before.InstallLocation).TrimEnd('\')
$executable = Join-Path $installRoot 'LocalVault.exe'
if (Get-Process -Name LocalVault -ErrorAction SilentlyContinue) {
    throw '请先保存编辑并关闭本地密匣，再运行覆盖更新；不会强制终止应用。'
}
$python = Join-Path $projectRoot '.venv-build/Scripts/python.exe'
$version = (& $python -c 'from local_vault import __version__; print(__version__)' | Out-String).Trim()
if ($LASTEXITCODE -ne 0) { throw '读取目标版本失败。' }
New-Item -ItemType Directory -Force -Path $reportRoot | Out-Null
$process = Start-Process -FilePath $setup -ArgumentList @(
    '/CURRENTUSER', '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-',
    ('/DIR="' + $installRoot + '"'), ('/LOG="' + (Join-Path $reportRoot 'local-update.log') + '"')
) -PassThru -Wait -WindowStyle Hidden
if ($process.ExitCode -ne 0) { throw "覆盖安装失败：$($process.ExitCode)" }
$after = Get-ItemProperty -LiteralPath $uninstallKey
if ($after.DisplayVersion -ne $version -or
    -not $after.InstallLocation.TrimEnd('\').Equals($installRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw '覆盖安装后的版本或安装位置不正确。'
}
if ((Get-Item -LiteralPath $executable).VersionInfo.ProductVersion -ne $version) {
    throw '安装后的可执行文件版本不正确。'
}
$versionReport = Join-Path $reportRoot 'installed-version.json'
$smokeReport = Join-Path $reportRoot 'local-installed-smoke.json'
$fakeData = Join-Path $reportRoot ('local-update-fake-' + [guid]::NewGuid().ToString('N'))
foreach ($arguments in @(
    @('--version', '--data-dir', ('"' + $fakeData + '"'), '--report', ('"' + $versionReport + '"')),
    @('--smoke-test', '--data-dir', ('"' + $fakeData + '"'), '--report', ('"' + $smokeReport + '"'))
)) {
    $process = Start-Process -FilePath $executable -ArgumentList $arguments -PassThru -Wait -WindowStyle Hidden
    if ($process.ExitCode -ne 0) { throw '安装后的隔离版本查询或自检失败。' }
}
$reportedVersion = Get-Content -LiteralPath $versionReport -Raw | ConvertFrom-Json
$smoke = Get-Content -LiteralPath $smokeReport -Raw | ConvertFrom-Json
if ($reportedVersion.版本 -ne $version -or $smoke.版本 -ne $version -or $smoke.结果 -ne '通过') {
    throw '安装后的实际程序自检或版本报告不正确。'
}
[ordered]@{
    更新前版本 = $before.DisplayVersion
    更新后版本 = $after.DisplayVersion
    安装位置 = $installRoot
    应用编号 = '{9F903D62-C3EA-4F18-9544-C1CE2EF5A762}'
    安装包 = $setup
    安装包SHA256 = $actualHash
    程序SHA256 = (Get-FileHash -LiteralPath $executable -Algorithm SHA256).Hash.ToLowerInvariant()
    隔离自检 = '通过'
    数据保护 = '沿用原应用编号及安装位置；未打开或读取真实保险库，未对真实数据目录执行写入或清理。'
} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $reportRoot 'local-update-report.json') -Encoding utf8
Write-Host "本机已覆盖更新为 $version，隔离自检通过。"
