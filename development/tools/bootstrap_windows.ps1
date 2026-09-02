[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('central', 'pc')]
    [string]$Role,
    [switch]$NoStart
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$dataRoot = if ($env:LIFE_LINK_DATA_ROOT) {
    $env:LIFE_LINK_DATA_ROOT
} else {
    Join-Path $env:USERPROFILE 'LifeLink'
}


function Test-Python313([string]$Candidate) {
    if (-not $Candidate -or -not (Test-Path -LiteralPath $Candidate -PathType Leaf)) {
        return $false
    }
    & $Candidate -c "import sys, tkinter; raise SystemExit(0 if sys.version_info[:2] == (3, 13) else 1)" 2>$null
    return $LASTEXITCODE -eq 0
}


function Find-Python313 {
    $candidates = @()
    if ($env:LIFE_LINK_SOURCE_PYTHON) {
        $candidates += $env:LIFE_LINK_SOURCE_PYTHON
    }
    $candidates += Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'
    $command = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($command) {
        $candidates += $command.Source
    }
    foreach ($candidate in $candidates | Select-Object -Unique) {
        if (Test-Python313 $candidate) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    return $null
}


function Ensure-Python313 {
    $python = Find-Python313
    if ($python) {
        return $python
    }
    $answer = Read-Host 'Life Link needs Python 3.13 (including Tkinter). Install it for this Windows user now? [Y/n]'
    if ($answer -match '^[Nn]') {
        throw 'Python 3.13 is required to run the source checkout.'
    }
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $winget) {
        throw 'Windows winget is unavailable. Install Python 3.13 from python.org, then run this file again.'
    }
    $wingetPath = $winget.Source
    & $wingetPath install --id Python.Python.3.13 --exact --scope user --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        throw "Python 3.13 installation failed with exit code $LASTEXITCODE."
    }
    $python = Find-Python313
    if (-not $python) {
        throw 'Python 3.13 was installed but cannot yet be found. Close this window and run the startup file again.'
    }
    return $python
}


function Launcher-Path([string]$TargetRole) {
    if ($TargetRole -eq 'central') {
        return Join-Path $projectRoot 'central-server\LifeLink Central Service.exe'
    }
    return Join-Path $projectRoot 'pc-dashboard\LifeLink PC Client.exe'
}


function Ensure-Launcher([string]$Python, [string]$TargetRole) {
    $launcher = Launcher-Path $TargetRole
    if (Test-Path -LiteralPath $launcher -PathType Leaf) {
        return
    }
    $venvRoot = Join-Path $dataRoot 'tools\build-python'
    $venvPython = Join-Path $venvRoot 'Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        Write-Host 'Preparing Life Link build tools for the first startup...'
        & $Python -m venv $venvRoot
        if ($LASTEXITCODE -ne 0) {
            throw "Life Link build environment creation failed with exit code $LASTEXITCODE."
        }
    }
    & $venvPython -m pip install --disable-pip-version-check --quiet -r (Join-Path $projectRoot 'life-link-mcp\requirements-build.txt')
    if ($LASTEXITCODE -ne 0) {
        throw "Life Link launcher dependency installation failed with exit code $LASTEXITCODE."
    }
    Write-Host "Creating the $TargetRole startup launcher..."
    & (Join-Path $projectRoot 'development\tools\build_source_launchers.ps1') -Target $TargetRole -Python $venvPython
    if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
        throw 'Life Link launcher build finished without creating its executable.'
    }
}


$python = Ensure-Python313
Ensure-Launcher -Python $python -TargetRole $Role
$startupScript = if ($Role -eq 'central') {
    Join-Path $projectRoot 'central-server\central_windows_startup.py'
} else {
    Join-Path $projectRoot 'pc-dashboard\pc_windows_startup.py'
}
& $python $startupScript
if ($LASTEXITCODE -ne 0) {
    Write-Warning "Life Link $Role launcher is ready, but login-startup registration failed. The application will still start."
}
if ($NoStart) {
    Write-Host "Life Link $Role bootstrap is ready."
    exit 0
}

$pythonWindowless = Join-Path (Split-Path -Parent $python) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonWindowless -PathType Leaf)) {
    throw "Python 3.13 is missing pythonw.exe: $pythonWindowless"
}
$moduleRoot = if ($Role -eq 'central') {
    Join-Path $projectRoot 'central-server'
} else {
    Join-Path $projectRoot 'pc-dashboard'
}
$scriptName = if ($Role -eq 'central') { 'central_server_app.py' } else { 'start_central_client.py' }
$scriptPath = Join-Path $moduleRoot $scriptName
if ($Role -eq 'central') {
    $setupScript = Join-Path $moduleRoot 'central_first_run.py'
    $launcher = Launcher-Path 'central'
    & $python $setupScript --launcher $launcher
    exit $LASTEXITCODE
}
Start-Process -FilePath $pythonWindowless -ArgumentList ('"{0}"' -f $scriptPath) -WorkingDirectory $moduleRoot
