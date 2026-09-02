param(
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$ModuleRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$BuildPath = Join-Path $ModuleRoot "build"
$SpecPath = Join-Path $ModuleRoot "life-link-mcp.spec"
if (-not $Python) {
    $Python = Join-Path $env:LocalAppData "Programs\Python\Python313\python.exe"
}
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Python was not found. Pass -Python with an explicit python.exe path."
}

& $Python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --name life-link-mcp `
    --distpath (Join-Path $ModuleRoot "dist") `
    --workpath $BuildPath `
    --specpath $ModuleRoot `
    (Join-Path $ModuleRoot "life_link_mcp.py")

if ($LASTEXITCODE -ne 0) {
    throw "life-link-mcp.exe build failed"
}

if (Test-Path -LiteralPath $BuildPath) {
    Remove-Item -LiteralPath $BuildPath -Recurse -Force
}
if (Test-Path -LiteralPath $SpecPath -PathType Leaf) {
    Remove-Item -LiteralPath $SpecPath -Force
}

Write-Host "Built: $(Join-Path $ModuleRoot 'dist\life-link-mcp.exe')"
