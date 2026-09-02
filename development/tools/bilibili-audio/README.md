# Bilibili Audio（阶段一：命令行版）

这是一个独立小工具，不导入、不修改 Life Link 现有中央服务、PC 客户端或契约代码。

## 目标

给一个 B 站视频链接，自动下载音轨并转成 MP3，默认保存到中央服务的数据区：

```text
%USERPROFILE%\LifeLink\central\media\audio
```

临时文件默认放在：

```text
%USERPROFILE%\LifeLink\central\media\incoming
```

## 一次性安装便携依赖

在 PowerShell 中运行：

```powershell
cd .\development\tools\bilibili-audio
powershell -ExecutionPolicy Bypass -File .\setup_portable_tools.ps1
```

这会把 `yt-dlp.exe`、`ffmpeg.exe` 和 `ffprobe.exe` 下载到统一运行时目录
`%USERPROFILE%\LifeLink\tools\bilibili-audio\bin\`，不写入系统目录。源码可有多个检出副本，但同一 Windows 用户只保留一套运行依赖。

## 使用

```powershell
.\bilibili_audio.bat "https://www.bilibili.com/video/BV..."
```

或短链：

```powershell
.\bilibili_audio.bat "https://b23.tv/..."
```

检查依赖：

```powershell
.\bilibili_audio.bat --check
```

覆盖同名文件：

```powershell
.\bilibili_audio.bat "https://www.bilibili.com/video/BV..." --overwrite
```

自定义输出目录：

```powershell
.\bilibili_audio.bat "https://www.bilibili.com/video/BV..." --output-dir "D:\Somewhere"
```

## 命名

默认文件名：

```text
视频标题 - UP主 - 视频ID.mp3
```

Windows 不允许的文件名字符会自动替换。

## 范围

阶段一只做：

- 单个公开 B 站视频链接；
- 本地命令行；
- MP3 输出；
- 默认保存到中央服务媒体目录。

暂不做：

- Web UI（当前入口已暂时隐藏）；
- 手机同步；
- 合辑、收藏夹、批量下载；
- 会员或登录内容；
- 修改中央数据库或现有 API。
