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


function Test-SupportedPython([string]$Candidate, [string]$TargetRole) {
    if (-not $Candidate -or -not (Test-Path -LiteralPath $Candidate -PathType Leaf)) {
        return $false
    }
    $pythonWindowless = if ((Split-Path -Leaf $Candidate).ToLowerInvariant() -eq 'pythonw.exe') {
        $Candidate
    } else {
        Join-Path (Split-Path -Parent $Candidate) 'pythonw.exe'
    }
    if (-not (Test-Path -LiteralPath $pythonWindowless -PathType Leaf)) {
        return $false
    }
    $probe = if ($TargetRole -eq 'pc') {
        "import sys, tkinter; raise SystemExit(0 if sys.version_info[:2] in ((3, 14), (3, 13)) else 1)"
    } else {
        "import sys; raise SystemExit(0 if sys.version_info[:2] in ((3, 14), (3, 13)) else 1)"
    }
    & $Candidate -c $probe 2>$null
    return $LASTEXITCODE -eq 0
}


function Find-SupportedPython {
    $candidates = @()
    if ($env:LIFE_LINK_SOURCE_PYTHON) {
        $candidates += $env:LIFE_LINK_SOURCE_PYTHON
    }
    # Keep this order aligned with the generated source launchers: explicit
    # override first, then the newest supported per-user installation.
    $candidates += Join-Path $env:LOCALAPPDATA 'Programs\Python\Python314\python.exe'
    $candidates += Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'
    $command = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($command) {
        $candidates += $command.Source
    }
    foreach ($candidate in $candidates | Select-Object -Unique) {
        if (Test-SupportedPython $candidate $Role) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    return $null
}


function Ensure-SupportedPython {
    $python = Find-SupportedPython
    if ($python) {
        return $python
    }
    $requirement = if ($Role -eq 'pc') { 'Python 3.13 or 3.14 (including Tkinter)' } else { 'Python 3.13 or 3.14' }
    $answer = Read-Host "Life Link needs $requirement. Install it for this Windows user now? [Y/n]"
    if ($answer -match '^[Nn]') {
        throw 'Python 3.13 or 3.14 is required to run the source checkout.'
    }
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $winget) {
        throw 'Windows winget is unavailable. Install Python 3.13 or 3.14 from python.org, then run this file again.'
    }
    $wingetPath = $winget.Source
    & $wingetPath install --id Python.Python.3.14 --exact --scope user --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        throw "Python 3.14 installation failed with exit code $LASTEXITCODE."
    }
    $python = Find-SupportedPython
    if (-not $python) {
        throw 'Python 3.14 was installed but cannot yet be found. Close this window and run the startup file again.'
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


$python = Ensure-SupportedPython
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
    throw "The selected Python 3.13 or 3.14 installation is missing pythonw.exe: $pythonWindowless"
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
