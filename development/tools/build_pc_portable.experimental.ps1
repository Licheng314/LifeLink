# Experimental only: the active source-checkout path uses lightweight module
# launchers, not a full portable PC package. Keep this script for a future
# packaging phase; do not invoke it from first-run bootstrap.
param(
    [string]$OutputRoot = (Join-Path $PSScriptRoot "..\..\build\LifeLink-PC-Portable")
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$pcRoot = Join-Path $projectRoot 'pc-dashboard'
$output = [System.IO.Path]::GetFullPath($OutputRoot)
$work = Join-Path $projectRoot 'build\pyinstaller-work'
$spec = Join-Path $projectRoot 'build\pyinstaller-spec'

New-Item -ItemType Directory -Force -Path $output, $work, $spec | Out-Null
Push-Location $pcRoot
try {
    python -m PyInstaller --noconfirm --clean --onedir --windowed `
        --name 'LifeLink PC Client' `
        --icon "$pcRoot\assets\life-link-client-tray.ico" `
        --distpath $output --workpath $work --specpath $spec `
        --add-data "$pcRoot\dashboard.html;." `
        --add-data "$pcRoot\central_client_setup.html;." `
        --add-data "$pcRoot\web;web" `
        --add-data "$pcRoot\assets;assets" `
        --add-data "$pcRoot\ai_context;ai_context" `
        --add-data "$projectRoot\.codex\skills\life-link-ai-reader\SKILL.md;resources\life-link-ai-reader" `
        --add-data "$projectRoot\life-link-mcp\dist\life-link-mcp.exe;resources\life-link-mcp" `
        --hidden-import desktop_app --hidden-import sync_server `
        start_central_client.py
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller 构建失败，退出码：$LASTEXITCODE"
    }
}
finally {
    Pop-Location
}

Write-Host "Portable PC client: $(Join-Path $output 'LifeLink PC Client')"
