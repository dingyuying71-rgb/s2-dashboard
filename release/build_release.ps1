$ErrorActionPreference = 'Stop'

$project = Split-Path -Parent $PSScriptRoot
$app = Join-Path $project 'app'
$build = Join-Path $project 'release_build'
$pyInstaller = Join-Path $env:APPDATA 'Python\Python314\Scripts\pyinstaller.exe'
if (-not (Test-Path -LiteralPath $pyInstaller)) {
    throw "PyInstaller not found: $pyInstaller"
}

if (Test-Path -LiteralPath $build) {
    Remove-Item -LiteralPath $build -Recurse -Force
}
New-Item -ItemType Directory -Path $build | Out-Null

$args = @(
    '--clean', '--noconfirm', '--onedir', '--noconsole',
    '--name', 'S2Dashboard',
    '--distpath', (Join-Path $build 'dist'),
    '--workpath', (Join-Path $build 'work'),
    '--specpath', $build,
    '--paths', $app,
    '--add-data', "$(Join-Path $app 'frontend');frontend",
    '--add-data', "$(Join-Path $app 'config\fund_whitelist.json');config",
    '--add-data', "$(Join-Path $app 'config\fund_fee_config.json');config",
    '--add-data', "$(Join-Path $app 'contracts');contracts",
    '--add-data', "$(Join-Path $app 'backend\formula_contract.json');backend",
    '--add-data', "$(Join-Path $app 'data\s2_v2_monthly.csv');data",
    '--add-data', "$(Join-Path $app 'data\s2_performance.csv');data",
    '--add-data', "$(Join-Path $app 'data\s2_crisis_summary.csv');data",
    '--add-data', "$(Join-Path $app 'data_source_contract.json');.",
    '--add-data', "$(Join-Path $app 'assets\s2-dashboard.ico');assets",
    '--hidden-import', 'backend.data_updater',
    '--hidden-import', 'backend.data_providers.fund_nav_provider',
    '--hidden-import', 'backend.data_providers.csi_provider',
    '--hidden-import', 'backend.data_providers.chinabond_provider',
    '--hidden-import', 'akshare',
    (Join-Path $app 'backend\release_entry.py')
)

& $pyInstaller @args
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

$dist = Join-Path $build 'dist\S2Dashboard'
Copy-Item -LiteralPath (Join-Path $app 'release\README.txt') -Destination (Join-Path $dist 'README.txt')
Write-Output $dist
