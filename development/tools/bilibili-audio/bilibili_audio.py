"""
Independent Bilibili audio extraction command-line tool.

Stage 1 scope:
- Accept one Bilibili video/b23 short URL.
- Use yt-dlp to fetch metadata and best audio.
- Use ffmpeg via yt-dlp post-processing to create MP3.
- Store final MP3 under Life Link central media directory by default.

This module intentionally does not import Life Link application code.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass
WINDOWS_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
INVALID_FS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
MULTI_SPACE = re.compile(r"\s+")
_configured_data_root = os.environ.get("LIFE_LINK_DATA_ROOT") or os.environ.get("LIFE_LINK_RUNTIME_ROOT")
_DATA_ROOT = (
    Path(_configured_data_root)
    if _configured_data_root
    else Path(os.environ.get("USERPROFILE") or Path.home()) / "LifeLink"
)
DEFAULT_CENTRAL_MEDIA = _DATA_ROOT / "central" / "media"
TOOL_DIR = Path(__file__).resolve().parent
RUNTIME_BIN = Path(
    os.environ.get("LIFE_LINK_BILIBILI_TOOL_BIN")
    or _DATA_ROOT / "tools" / "bilibili-audio" / "bin"
)


@dataclass(frozen=True)
class ToolPaths:
    yt_dlp: str
    ffmpeg_dir: Path | None


def sanitize_filename(value: Any, fallback: str = "untitled", max_len: int = 120) -> str:
    text = str(value or "").strip()
    text = INVALID_FS_CHARS.sub(" ", text)
    text = text.replace("\n", " ").replace("\r", " ")
    text = MULTI_SPACE.sub(" ", text).strip(" .")
    if not text:
        text = fallback
    return text[:max_len].rstrip(" .") or fallback


def find_tool_paths() -> ToolPaths:
    """Find yt-dlp/ffmpeg in PATH or Life Link's unified runtime tool directory."""
    yt_dlp = shutil.which("yt-dlp")
    if not yt_dlp:
        runtime_ytdlp = RUNTIME_BIN / ("yt-dlp.exe" if os.name == "nt" else "yt-dlp")
        if runtime_ytdlp.exists():
            yt_dlp = str(runtime_ytdlp)
    if not yt_dlp:
        raise SystemExit(
            "未找到 yt-dlp。请运行 setup_portable_tools.ps1 安装便携依赖，"
            "或自行把 yt-dlp 加入 PATH。"
        )

    ffmpeg_dir: Path | None = None
    if shutil.which("ffmpeg"):
        ffmpeg_dir = None
    elif (RUNTIME_BIN / "ffmpeg.exe").exists():
        ffmpeg_dir = RUNTIME_BIN
    else:
        raise SystemExit(
            "未找到 ffmpeg。请运行 setup_portable_tools.ps1 安装便携依赖，"
            "或自行把 ffmpeg 加入 PATH。"
        )
    return ToolPaths(yt_dlp=yt_dlp, ffmpeg_dir=ffmpeg_dir)


def run_command(command: list[str], *, description: str) -> str:
    try:
        completed = subprocess.run(
            command,
            check=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=WINDOWS_NO_WINDOW if os.name == "nt" else 0,
        )
    except FileNotFoundError as exc:
        raise SystemExit(f"{description}失败：找不到程序 {exc.filename}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise SystemExit(f"{description}失败：\n{detail}") from exc
    return completed.stdout


def fetch_metadata(tools: ToolPaths, url: str) -> dict[str, Any]:
    command = [
        tools.yt_dlp,
        "--no-playlist",
        "--no-warnings",
        "--dump-single-json",
        url,
    ]
    raw = run_command(command, description="读取视频信息")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit("无法解析 yt-dlp 返回的视频信息。") from exc
    if not isinstance(data, dict):
        raise SystemExit("yt-dlp 返回了意外的视频信息格式。")
    return data


def build_output_stem(meta: dict[str, Any]) -> str:
    title = sanitize_filename(meta.get("title"), fallback="bilibili-audio")
    uploader = sanitize_filename(
        meta.get("uploader") or meta.get("uploader_id") or meta.get("channel"),
        fallback="unknown-uploader",
        max_len=60,
    )
    video_id = sanitize_filename(meta.get("id"), fallback="unknown-id", max_len=40)
    return sanitize_filename(f"{title} - {uploader} - {video_id}", max_len=180)


def download_mp3(
    tools: ToolPaths,
    url: str,
    output_dir: Path,
    temp_dir: Path,
    stem: str,
    overwrite: bool,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    temp_dir.mkdir(parents=True, exist_ok=True)

    output_template = str(temp_dir / f"{stem}.%(ext)s")
    command = [
        tools.yt_dlp,
        "--no-playlist",
        "--no-warnings",
        "--format",
        "bestaudio/best",
        "--extract-audio",
        "--audio-format",
        "mp3",
        "--audio-quality",
        "0",
        "--paths",
        str(temp_dir),
        "--output",
        output_template,
        "--no-mtime",
        "--no-part",
    ]
    if tools.ffmpeg_dir is not None:
        command.extend(["--ffmpeg-location", str(tools.ffmpeg_dir)])
    if overwrite:
        command.append("--force-overwrites")
    else:
        command.append("--no-overwrites")
    command.append(url)

    print(f"开始下载并转换：{url}")
    print(f"临时目录：{temp_dir}")
    try:
        subprocess.run(command, check=True, encoding="utf-8", errors="replace", creationflags=WINDOWS_NO_WINDOW if os.name == "nt" else 0)
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"下载或 MP3 转换失败，退出码 {exc.returncode}。") from exc

    produced = temp_dir / f"{stem}.mp3"
    if not produced.exists():
        matches = sorted(temp_dir.glob(f"{stem}*.mp3"))
        if matches:
            produced = matches[0]
    if not produced.exists():
        raise SystemExit("yt-dlp 执行结束，但没有找到生成的 MP3 文件。")

    final_path = output_dir / f"{stem}.mp3"
    if final_path.exists() and overwrite:
        final_path.unlink()
    shutil.move(str(produced), str(final_path))
    return final_path


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="从 B 站视频链接提取 MP3，保存到 Life Link 中央媒体目录。"
    )
    parser.add_argument("url", nargs="?", default=None, help="B 站视频链接，例如 https://www.bilibili.com/video/BV... 或 https://b23.tv/...")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_CENTRAL_MEDIA / "audio",
        help="MP3 输出目录，默认使用 Life Link 中央服务 media/audio。",
    )
    parser.add_argument(
        "--temp-dir",
        type=Path,
        default=DEFAULT_CENTRAL_MEDIA / "incoming",
        help="下载和转换临时目录，默认使用 Life Link 中央服务 media/incoming。",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="如果同名 MP3 已存在，允许覆盖。",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只检查依赖和默认输出目录，不下载视频。",
    )
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.check and not args.url:
        raise SystemExit("请提供 B 站视频链接。")
    tools = find_tool_paths()

    print(f"yt-dlp: {tools.yt_dlp}")
    if tools.ffmpeg_dir:
        print(f"ffmpeg: {tools.ffmpeg_dir / 'ffmpeg.exe'}")
    else:
        print("ffmpeg: PATH")
    print(f"MP3 输出目录：{args.output_dir}")
    print(f"临时目录：{args.temp_dir}")

    if args.check:
        print("依赖检查通过。")
        return 0

    meta = fetch_metadata(tools, args.url)
    stem = build_output_stem(meta)
    title = meta.get("title", "")
    uploader = meta.get("uploader") or meta.get("uploader_id") or ""
    video_id = meta.get("id", "")
    print(f"标题：{title}")
    print(f"UP 主：{uploader}")
    print(f"视频 ID：{video_id}")

    final_path = args.output_dir / f"{stem}.mp3"
    if final_path.exists() and not args.overwrite:
        print(f"同名 MP3 已存在，跳过下载：{final_path}")
        print(f"OUTPUT_FILE: {final_path}")
        return 0

    final_path = download_mp3(
        tools=tools,
        url=args.url,
        output_dir=args.output_dir,
        temp_dir=args.temp_dir,
        stem=stem,
        overwrite=args.overwrite,
    )
    print(f"完成：{final_path}")
    print(f"OUTPUT_FILE: {final_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
