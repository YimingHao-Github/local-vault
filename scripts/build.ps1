[CmdletBinding()]
param(
    [string]$PythonExe = 'python',
    [string]$IsccPath = '',
    [switch]$SkipTests,
    [switch]$IncludeClipboardTest,
    [switch]$SkipInstaller
)

$ErrorActionPreference = 'Stop'
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$buildRoot = Join-Path $projectRoot 'build'
$distRoot = Join-Path $projectRoot 'dist'
$toolRoot = Join-Path $projectRoot '.tools'
$venvRoot = Join-Path $projectRoot '.venv-build'
$venvPython = Join-Path $venvRoot 'Scripts/python.exe'
Set-Location -LiteralPath $projectRoot

function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "命令执行失败，退出码 $LASTEXITCODE ：$Program"
    }
}

function Assert-ProjectPath {
    param([string]$Path)
    $resolved = [System.IO.Path]::GetFullPath($Path)
    if (-not $resolved.StartsWith($projectRoot + [System.IO.Path]::DirectorySeparatorChar,
            [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "构建输出路径必须位于本项目内：$resolved"
    }
}

foreach ($path in @($buildRoot, $distRoot, $toolRoot, $venvRoot)) {
    Assert-ProjectPath $path
}

if (-not (Test-Path -LiteralPath $venvPython)) {
    Invoke-Checked $PythonExe @('-m', 'venv', $venvRoot)
}
Invoke-Checked $venvPython @('-c', 'import sys; assert sys.version_info >= (3, 12), "需要 Python 3.12 或更新版本"; assert sys.maxsize > 2**32, "需要 64 位 Python"')
Invoke-Checked $venvPython @('-m', 'pip', 'install', '--disable-pip-version-check', '-r', (Join-Path $projectRoot 'requirements-dev.txt'))

if (-not $SkipTests) {
    $testArguments = @('-m', 'pytest', '-q', '--junitxml=build/build-tests.xml')
    if (-not $IncludeClipboardTest) {
        $testArguments += @('-k', 'not plain_text_widget_preserves_text_and_save')
    }
    Invoke-Checked $venvPython $testArguments
}

$versionOutput = & $venvPython -c 'from local_vault import __version__; print(__version__)'
if ($LASTEXITCODE -ne 0) { throw '无法读取应用版本。' }
$appVersion = ($versionOutput | Out-String).Trim()
if ($appVersion -notmatch '^\d+\.\d+\.\d+$') { throw "应用版本格式无效：$appVersion" }
New-Item -ItemType Directory -Force -Path $buildRoot | Out-Null
$versionResource = Join-Path $buildRoot 'windows-version.txt'
$versionTemplate = [IO.File]::ReadAllText((Join-Path $projectRoot 'scripts/windows-version.txt'))
$versionTemplate = $versionTemplate.Replace('(1, 0, 0, 0)', '(' + $appVersion.Replace('.', ', ') + ', 0)')
$versionTemplate = $versionTemplate.Replace("'1.0.0'", "'$appVersion'")
[IO.File]::WriteAllText($versionResource, $versionTemplate, [Text.UTF8Encoding]::new($false))

# 窗口模式、目录分发：避免每次启动都解压运行库。
Invoke-Checked $venvPython @(
    '-m', 'PyInstaller', '--noconfirm', '--clean', '--windowed', '--onedir',
    '--name', 'LocalVault', '--noupx', '--exclude-module', '_hashlib', '--paths', $projectRoot,
    '--distpath', $distRoot, '--workpath', $buildRoot, '--specpath', $buildRoot,
    '--version-file', $versionResource,
    (Join-Path $projectRoot 'local_vault/__main__.py')
)

$applicationRoot = Join-Path $distRoot 'LocalVault'
$licensesRoot = Join-Path $applicationRoot 'licenses'
New-Item -ItemType Directory -Force -Path $licensesRoot | Out-Null
Copy-Item -Path (Join-Path $projectRoot 'installer/licenses/*') -Destination $licensesRoot
$pythonBase = (& $venvPython -c 'import sys; print(sys.base_prefix)' | Out-String).Trim()
if ($LASTEXITCODE -ne 0) { throw '无法定位 Python 运行库许可。' }
Copy-Item -LiteralPath (Join-Path $pythonBase 'LICENSE.txt') -Destination (Join-Path $licensesRoot 'Python.txt')
foreach ($package in @('cryptography', 'cffi')) {
    $packageLicenses = Get-ChildItem -LiteralPath (Join-Path $venvRoot 'Lib/site-packages') -Directory |
        Where-Object { $_.Name -like "$package-*.dist-info" } |
        ForEach-Object { Join-Path $_.FullName 'licenses' }
    foreach ($source in $packageLicenses) {
        $destination = Join-Path $licensesRoot $package
        New-Item -ItemType Directory -Force -Path $destination | Out-Null
        Copy-Item -Path (Join-Path $source '*') -Destination $destination -Recurse
    }
}
foreach ($component in @('tcl8.6', 'tk8.6')) {
    $license = Join-Path $pythonBase "tcl/$component/license.terms"
    if (Test-Path -LiteralPath $license) {
        Copy-Item -LiteralPath $license -Destination (Join-Path $licensesRoot "$component.txt")
    }
}
foreach ($document in @('README.md', 'GOAL.md', 'LICENSE')) {
    $source = Join-Path $projectRoot $document
    if (Test-Path -LiteralPath $source) {
        Copy-Item -LiteralPath $source -Destination $applicationRoot
    }
}
$documentationRoot = Join-Path $projectRoot 'docs'
if (Test-Path -LiteralPath $documentationRoot) {
    # 说明文档及其图片随程序安装；临时日志和测试报告保留在忽略的 build 目录。
    Copy-Item -LiteralPath $documentationRoot -Destination $applicationRoot -Recurse
}

# 仅使用项目中的虚构测试目录，不访问用户的默认保险库。
$smokeData = Join-Path $buildRoot 'smoke-data'
$smoke = Start-Process -FilePath (Join-Path $applicationRoot 'LocalVault.exe') -ArgumentList @(
    '--smoke-test', '--data-dir', ('"' + $smokeData + '"'),
    '--report', ('"' + (Join-Path $buildRoot 'smoke-report.json') + '"')
) -PassThru -Wait -WindowStyle Hidden
if ($smoke.ExitCode -ne 0) { throw "打包程序自检失败，退出码 $($smoke.ExitCode)" }

if (-not $SkipInstaller) {
    if ([string]::IsNullOrWhiteSpace($IsccPath)) {
        $IsccPath = Join-Path $toolRoot 'InnoSetup/ISCC.exe'
        if (-not (Test-Path -LiteralPath $IsccPath)) {
            New-Item -ItemType Directory -Force -Path $toolRoot | Out-Null
            $toolInstaller = Join-Path $toolRoot 'innosetup-6.7.3.exe'
            $expectedHash = '9C73C3BAE7ED48D44112A0F48E66742C00090BDB5BEF71D9D3C056C66E97B732'
            if (-not (Test-Path -LiteralPath $toolInstaller)) {
                Invoke-WebRequest -Uri 'https://github.com/jrsoftware/issrc/releases/download/is-6_7_3/innosetup-6.7.3.exe' -OutFile $toolInstaller
            }
            if ((Get-FileHash -LiteralPath $toolInstaller -Algorithm SHA256).Hash -ne $expectedHash) {
                throw 'Inno Setup 下载校验失败，请删除 .tools 中的工具安装文件并重试。'
            }
            $signature = Get-AuthenticodeSignature -LiteralPath $toolInstaller
            if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notlike '*Pyrsys B.V.*') {
                throw 'Inno Setup 官方数字签名验证失败。'
            }
            $toolDirectory = Join-Path $toolRoot 'InnoSetup'
            $toolInstall = Start-Process -FilePath $toolInstaller -ArgumentList @(
                '/PORTABLE=1', '/CURRENTUSER', '/VERYSILENT', '/SUPPRESSMSGBOXES',
                '/NORESTART', '/SP-', '/NOICONS', ('/DIR="' + $toolDirectory + '"')
            ) -PassThru -Wait -WindowStyle Hidden
            if ($toolInstall.ExitCode -ne 0) { throw "安装包编译工具准备失败：$($toolInstall.ExitCode)" }
        }
    }
    Invoke-Checked $IsccPath @('/Q', "/DAppVersion=$appVersion", (Join-Path $projectRoot 'installer/local-vault.iss'))
    $setupPath = Join-Path $distRoot "LocalVault-Setup-$appVersion.exe"
    if (-not (Test-Path -LiteralPath $setupPath)) { throw '未生成安装包。' }
    $checksum = (Get-FileHash -LiteralPath $setupPath -Algorithm SHA256).Hash.ToLowerInvariant()
    "$checksum  $([System.IO.Path]::GetFileName($setupPath))" |
        Set-Content -LiteralPath "$setupPath.sha256" -Encoding ascii
    Write-Host "安装包已生成：$setupPath"
}
Write-Host "程序已生成：$applicationRoot"
