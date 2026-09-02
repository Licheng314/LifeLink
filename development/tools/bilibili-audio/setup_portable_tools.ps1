$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()

param(
    [string]$BinDir = $(
        $DataRoot = if ($env:LIFE_LINK_DATA_ROOT) { $env:LIFE_LINK_DATA_ROOT } elseif ($env:LIFE_LINK_RUNTIME_ROOT) { $env:LIFE_LINK_RUNTIME_ROOT } elseif ($env:USERPROFILE) { Join-Path $env:USERPROFILE 'LifeLink' } else { Join-Path $HOME 'LifeLink' }
        Join-Path $DataRoot 'tools\bilibili-audio\bin'
    )
)
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null

$YtDlp = Join-Path $BinDir 'yt-dlp.exe'
$Ffmpeg = Join-Path $BinDir 'ffmpeg.exe'

if (-not (Test-Path -LiteralPath $YtDlp)) {
    Write-Host 'Downloading portable yt-dlp.exe ...'
    Invoke-WebRequest -Uri 'https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe' -OutFile $YtDlp
} else {
    Write-Host "yt-dlp already exists: $YtDlp"
}

if (-not (Test-Path -LiteralPath $Ffmpeg)) {
    $Zip = Join-Path $env:TEMP 'ffmpeg-release-essentials.zip'
    $Extract = Join-Path $env:TEMP 'ffmpeg-essentials-extract'
    if (Test-Path -LiteralPath $Extract) { Remove-Item -LiteralPath $Extract -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $Extract | Out-Null

    Write-Host 'Downloading portable ffmpeg essentials ...'
    Invoke-WebRequest -Uri 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' -OutFile $Zip
    Expand-Archive -LiteralPath $Zip -DestinationPath $Extract -Force

    $FfmpegSrc = Get-ChildItem -LiteralPath $Extract -Recurse -Filter 'ffmpeg.exe' | Select-Object -First 1
    if (-not $FfmpegSrc) { throw 'Downloaded ffmpeg archive did not contain ffmpeg.exe.' }
    Copy-Item -LiteralPath $FfmpegSrc.FullName -Destination $Ffmpeg -Force

    $FfprobeSrc = Join-Path $FfmpegSrc.Directory.FullName 'ffprobe.exe'
    if (Test-Path -LiteralPath $FfprobeSrc) {
        Copy-Item -LiteralPath $FfprobeSrc -Destination (Join-Path $BinDir 'ffprobe.exe') -Force
    }
} else {
    Write-Host "ffmpeg already exists: $Ffmpeg"
}

Write-Host 'Portable dependencies are ready.'
& $YtDlp --version
& $Ffmpeg -version | Select-Object -First 1
