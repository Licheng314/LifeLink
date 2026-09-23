# Deploy an exact committed LifeLink SHA to the central Docker host.
param(
    [switch]$DryRun,
    [string]$ConfigPath,
    [string]$Commit = 'HEAD'
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
if (-not $ConfigPath) { $ConfigPath = Join-Path $repoRoot 'central-server/deploy.local.json' }
if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
    throw "缺少本机配置：$ConfigPath。请复制 central-server/deploy.local.example.json 并填写。"
}
$config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
$hostName = [string]$config.ssh_host
$port = [int]$config.ssh_port
$identity = [string]$config.identity_file
$remoteRepo = [string]$config.remote_repo
$pushRemote = [string]$config.push_remote
$pipIndex = [string]$config.pip_index_url
if ($hostName -notmatch '^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+$' -or $port -lt 1 -or $port -gt 65535 -or
    $remoteRepo -notmatch '^/[A-Za-z0-9._/-]+$' -or $pushRemote -notmatch '^[A-Za-z0-9_-]+$' -or
    $pipIndex -notmatch '^https://[A-Za-z0-9._/-]+$' -or [string]::IsNullOrWhiteSpace($identity)) {
    throw '部署配置无效：请检查主机、端口、私钥路径、仓库、远端名称及 HTTPS 镜像地址。'
}

function Invoke-Git([string[]]$GitArgs) {
    $output = & git -C $repoRoot @GitArgs
    if ($LASTEXITCODE -ne 0) { throw "Git 操作失败：$($GitArgs[0])" }
    return $output
}

$branch = [string](Invoke-Git @('branch', '--show-current'))
if ($branch -notmatch '^codex/[a-z0-9][a-z0-9._/-]*$') { throw '仅允许从 codex/ 功能分支发布。' }
$sha = [string](Invoke-Git @('rev-parse', $Commit))
$head = [string](Invoke-Git @('rev-parse', 'HEAD'))
if ($sha -notmatch '^[0-9a-f]{40}$' -or $sha -ne $head) { throw '目标必须是当前 HEAD 的完整提交。' }
$remoteUrl = [string](Invoke-Git @('remote', 'get-url', '--push', $pushRemote))
$serverHostPart = $hostName.Split('@')[1]
if ($remoteUrl -notmatch '^ssh://[^@/]+@([^/:]+)(?::[0-9]+)?/' -or $Matches[1] -ne $serverHostPart) {
    throw 'Git 推送地址与部署主机不一致，请检查配置。'
}

$dirty = @(Invoke-Git @('status', '--porcelain=v1', '--', '.dockerignore', 'central-server', 'life-link-mcp', '.codex/skills/life-link-ai-reader', '.codex/skills/life-link-central-deploy', 'development/tools/deploy-central.ps1'))
if ($dirty.Count -gt 0) {
    Write-Host '待提交的部署相关文件（不会被本脚本自动提交）：'
    $dirty | ForEach-Object { Write-Host "  $_" }
    if (-not $DryRun) { throw '请先审查并提交上述文件；其他模块的未提交修改不会自动进入部署。' }
}

$serverScript = Join-Path $repoRoot 'central-server/maintenance/deploy_server.sh'
if (-not (Test-Path -LiteralPath $serverScript)) { throw '找不到服务器部署脚本。' }
Write-Host "部署目标：${hostName}:$port，分支 $branch，提交 $sha"
if ($DryRun) {
    Write-Host 'DRY_RUN_OK：未推送、未连接服务器、未改变线上服务。'
    exit 0
}
if (-not (Test-Path -LiteralPath $identity -PathType Leaf)) {
    throw 'SSH 私钥路径不可访问。'
}

$sshCommand = "ssh -i `"$identity`" -p $port -o BatchMode=yes -o IdentitiesOnly=yes"
& git -C $repoRoot -c "core.sshCommand=$sshCommand" push $pushRemote "${sha}:refs/heads/$branch"
if ($LASTEXITCODE -ne 0) { throw '推送目标提交失败，服务器未部署。' }

$remoteCommand = "bash -s -- --apply --commit $sha --branch $branch --repo $remoteRepo --pip-index $pipIndex"
$sshArgs = @('-i', $identity, '-p', "$port", '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', $hostName, $remoteCommand)
$output = @(Get-Content -LiteralPath $serverScript -Raw | & ssh @sshArgs)
$sshExit = $LASTEXITCODE
$output | ForEach-Object { Write-Host $_ }
if ($sshExit -ne 0 -or -not ($output -match "^DEPLOY_OK commit=$sha ")) {
    throw '部署未得到成功确认。请查看上方 DEPLOY_STEP/DEPLOY_RECOVERY；不要盲目重试。'
}
Write-Host "部署完成：$sha"
