[CmdletBinding()]
param(
    [ValidateSet('central', 'pc', 'all')]
    [string]$Target = 'all',
    [string]$Python = ''
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$work = Join-Path $projectRoot 'build\source-launcher-work'
$spec = Join-Path $projectRoot 'build\source-launcher-spec'
$centralRoot = Join-Path $projectRoot 'central-server'
$pcRoot = Join-Path $projectRoot 'pc-dashboard'
$pythonPath = if ($Python) { $Python } else { $env:LIFE_LINK_BUILD_PYTHON }
if (-not $pythonPath) {
    $pythonPath = Get-ChildItem (Join-Path $env:LOCALAPPDATA 'Programs\Python') -Filter python.exe -Recurse -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending |
        Select-Object -First 1 -ExpandProperty FullName
}
if (-not $pythonPath -or -not (Test-Path -LiteralPath $pythonPath)) {
    throw 'Python was not found. Install Python or set LIFE_LINK_BUILD_PYTHON.'
}

New-Item -ItemType Directory -Force -Path $work, $spec | Out-Null
if ($Target -in @('central', 'all')) {
    $centralArgs = @(
        '-m', 'PyInstaller', '--noconfirm', '--clean', '--onefile', '--windowed',
        '--name', 'LifeLink Central Service',
        '--icon', "$projectRoot\central-server\assets\life-link-server-tray.ico",
        '--distpath', $centralRoot, '--workpath', $work, '--specpath', $spec,
        "$centralRoot\launcher\source_launcher.py"
    )
    & $pythonPath @centralArgs
    if ($LASTEXITCODE -ne 0) { throw "Central launcher build failed with exit code $LASTEXITCODE." }
}

if ($Target -in @('pc', 'all')) {
    $pcArgs = @(
        '-m', 'PyInstaller', '--noconfirm', '--clean', '--onefile', '--windowed',
        '--name', 'LifeLink PC Client',
        '--icon', "$projectRoot\pc-dashboard\assets\life-link-client-tray.ico",
        '--distpath', $pcRoot, '--workpath', $work, '--specpath', $spec,
        "$pcRoot\launcher\source_launcher.py"
    )
    & $pythonPath @pcArgs
    if ($LASTEXITCODE -ne 0) { throw "PC launcher build failed with exit code $LASTEXITCODE." }
}

# These are PyInstaller diagnostics, not a runtime dependency. The resulting
# launchers live in their owning modules, so do not let temporary output grow
# under the checkout after a successful build.
Remove-Item -LiteralPath $work, $spec -Recurse -Force -ErrorAction SilentlyContinue

if ($Target -in @('central', 'all')) {
    Write-Host "Central launcher: $(Join-Path $centralRoot 'LifeLink Central Service.exe')"
}
if ($Target -in @('pc', 'all')) {
    Write-Host "PC launcher: $(Join-Path $pcRoot 'LifeLink PC Client.exe')"
}
