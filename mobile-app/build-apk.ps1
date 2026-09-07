[CmdletBinding()]
param(
    [ValidateSet("debug", "release")]
    [string]$Variant = "debug",
    [string]$ReleaseDirectory = $env:LIFE_RADIO_RELEASES_DIR,
    [switch]$Publish,
    [switch]$ConfirmPublish
)

$ErrorActionPreference = "Stop"
$projectDirectory = $PSScriptRoot
$workspaceDirectory = Split-Path -Parent $projectDirectory

if ([string]::IsNullOrWhiteSpace($ReleaseDirectory)) {
    $ReleaseDirectory = Join-Path $workspaceDirectory "build\android"
}

$gradleTask = if ($Variant -eq "release") { "assembleRelease" } else { "assembleDebug" }
$sourceName = if ($Variant -eq "release") { "app-release-unsigned.apk" } else { "app-debug.apk" }
$sourceApk = Join-Path $projectDirectory "app\build\outputs\apk\$Variant\$sourceName"
$appBuildFile = Join-Path $projectDirectory "app\build.gradle.kts"

$versionMatch = Select-String -LiteralPath $appBuildFile -Pattern 'versionName\s*=\s*"([^"]+)"' | Select-Object -First 1
if (-not $versionMatch) {
    throw "Unable to read versionName from $appBuildFile"
}
$versionName = $versionMatch.Matches[0].Groups[1].Value
$publishedVariant = if ($Variant -eq "release") { "release-unsigned" } else { "debug" }
$targetApk = Join-Path $ReleaseDirectory "LifeLink-v$versionName-$publishedVariant.apk"
$checksumPath = "$targetApk.sha256"
$stableApk = Join-Path $ReleaseDirectory "LifeLink-Android.apk"

Write-Host "=== Testing Life Link Android ==="
& (Join-Path $projectDirectory "gradlew.bat") testDebugUnitTest --console=plain
if ($LASTEXITCODE -ne 0) {
    throw "Android unit tests failed with exit code $LASTEXITCODE"
}

Write-Host "=== Building Life Link Android $Variant candidate ==="
& (Join-Path $projectDirectory "gradlew.bat") $gradleTask
if ($LASTEXITCODE -ne 0) {
    throw "Gradle build failed with exit code $LASTEXITCODE"
}
if (-not (Test-Path -LiteralPath $sourceApk -PathType Leaf)) {
    throw "Build completed but APK was not found: $sourceApk"
}

New-Item -ItemType Directory -Path $ReleaseDirectory -Force | Out-Null
Copy-Item -LiteralPath $sourceApk -Destination $targetApk -Force
$sha256 = [System.Security.Cryptography.SHA256]::Create()
$apkStream = [System.IO.File]::OpenRead($targetApk)
try {
    $hash = ([System.BitConverter]::ToString($sha256.ComputeHash($apkStream))).Replace("-", "").ToLowerInvariant()
}
finally {
    $apkStream.Dispose()
    $sha256.Dispose()
}
Set-Content -LiteralPath $checksumPath -Value "$hash  $(Split-Path -Leaf $targetApk)" -Encoding ascii

Write-Host "=== Android candidate APK ==="
Write-Host $targetApk
Write-Host "SHA-256: $hash"

if (-not $Publish) {
    Write-Host "Candidate only; no Git tag or GitHub Release was created."
    exit 0
}

if (-not $ConfirmPublish) {
    throw "Publishing is external. Re-run with -Publish -ConfirmPublish after reviewing the candidate APK."
}
if ($Variant -ne "debug") {
    throw "Automated GitHub publishing currently supports only the installable debug APK."
}
if (git status --porcelain) {
    throw "Git working tree must be clean before publishing. Commit or stash source changes first."
}
$githubCli = Get-Command gh -ErrorAction SilentlyContinue
if (-not $githubCli) {
    $installedGithubCli = Join-Path $env:ProgramFiles "GitHub CLI\gh.exe"
    if (Test-Path -LiteralPath $installedGithubCli -PathType Leaf) {
        $githubCli = Get-Item -LiteralPath $installedGithubCli
    }
}
if (-not $githubCli) {
    throw "GitHub CLI (gh) is required for -Publish. Install it and run 'gh auth login' first."
}
$githubCliPath = $githubCli.Source
if ([string]::IsNullOrWhiteSpace($githubCliPath)) {
    $githubCliPath = $githubCli.FullName
}

$tag = "android-v$versionName"
& $githubCliPath release view $tag 2>$null
if ($LASTEXITCODE -eq 0) {
    throw "GitHub Release '$tag' already exists; refusing to overwrite it."
}

# The stable asset name is the target of README's /releases/latest/download link.
Copy-Item -LiteralPath $targetApk -Destination $stableApk -Force
Write-Host "=== Publishing GitHub Release $tag ==="
& $githubCliPath release create $tag $targetApk $checksumPath $stableApk --target HEAD --title "Life Link Android v$versionName" --generate-notes
if ($LASTEXITCODE -ne 0) {
    throw "GitHub Release creation failed with exit code $LASTEXITCODE"
}
Write-Host "Published: https://github.com/Licheng314/LifeLink/releases/tag/$tag"
