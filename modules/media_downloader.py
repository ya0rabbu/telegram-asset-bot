"""Thin async wrapper around yt-dlp for YouTube / X / Facebook."""

from __future__ import annotations
import asyncio
import os
import re
import tempfile
import uuid

MAX_DOWNLOAD_BYTES       = 49 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 180

SUPPORTED_HOST_RE = re.compile(
    r"(youtube\.com|youtu\.be|x\.com|twitter\.com|facebook\.com|fb\.watch)",
    re.IGNORECASE,
)


def is_supported_url(url: str) -> bool:
    return bool(SUPPORTED_HOST_RE.search(url))


def _base_opts(out_template: str) -> dict:
    return {
        "outtmpl":          out_template,
        "noplaylist":       True,
        "quiet":            True,
        "no_warnings":      True,
        "restrictfilenames":True,
        "max_filesize":     MAX_DOWNLOAD_BYTES,
        "socket_timeout":   30,
    }


async def fetch_metadata(url: str) -> dict:
    import yt_dlp

    def _run() -> dict:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}) as ydl:
            info = ydl.extract_info(url, download=False)
            return {
                "title":    info.get("title") or "media",
                "duration": info.get("duration"),
                "uploader": info.get("uploader"),
            }

    return await asyncio.to_thread(_run)


async def download_video(url: str, max_height: int = 720) -> tuple[bytes, str]:
    import yt_dlp

    def _run() -> tuple[bytes, str]:
        with tempfile.TemporaryDirectory() as tmp:
            out_tmpl = os.path.join(tmp, f"{uuid.uuid4().hex}.%(ext)s")
            opts = _base_opts(out_tmpl)
            opts["format"] = (
                f"bestvideo[height<={max_height}]+bestaudio"
                f"/best[height<={max_height}]/best"
            )
            opts["merge_output_format"] = "mp4"
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                path = ydl.prepare_filename(info)
                if not os.path.exists(path):
                    alt  = os.path.splitext(path)[0] + ".mp4"
                    path = alt if os.path.exists(alt) else path
                with open(path, "rb") as f:
                    data = f.read()
                return data, info.get("title") or "video"

    return await asyncio.wait_for(
        asyncio.to_thread(_run), timeout=DOWNLOAD_TIMEOUT_SECONDS
    )


async def download_audio(url: str) -> tuple[bytes, str]:
    import yt_dlp

    def _run() -> tuple[bytes, str]:
        with tempfile.TemporaryDirectory() as tmp:
            out_tmpl = os.path.join(tmp, f"{uuid.uuid4().hex}.%(ext)s")
            opts = _base_opts(out_tmpl)
            opts["format"] = "bestaudio/best"
            opts["postprocessors"] = [{
                "key":              "FFmpegExtractAudio",
                "preferredcodec":   "mp3",
                "preferredquality": "192",
            }]
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                path = os.path.splitext(ydl.prepare_filename(info))[0] + ".mp3"
                with open(path, "rb") as f:
                    data = f.read()
                return data, info.get("title") or "audio"

    return await asyncio.wait_for(
        asyncio.to_thread(_run), timeout=DOWNLOAD_TIMEOUT_SECONDS
    )
