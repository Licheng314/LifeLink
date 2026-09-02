[CmdletBinding()]
param(
    [ValidateSet("debug", "release")]
    [string]$Variant = "debug",
    [string]$ReleaseDirectory = $env:LIFE_RADIO_RELEASES_DIR
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

Write-Host "=== Building Life Link Android $Variant APK ==="
& (Join-Path $projectDirectory "gradlew.bat") $gradleTask
if ($LASTEXITCODE -ne 0) {
    throw "Gradle build failed with exit code $LASTEXITCODE"
}
if (-not (Test-Path -LiteralPath $sourceApk -PathType Leaf)) {
    throw "Build completed but APK was not found: $sourceApk"
}

New-Item -ItemType Directory -Path $ReleaseDirectory -Force | Out-Null
Copy-Item -LiteralPath $sourceApk -Destination $targetApk -Force

Write-Host "=== Published APK ==="
Write-Host $targetApk
