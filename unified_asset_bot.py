"""
BangaliIcon Bot — v4.0
=======================
Single-file deployment.  All helper modules (storage, qr_tools,
password_tools, image_extra) are embedded as source strings and loaded
at import time via _load_embedded_module().

Run
---
    pip install -r requirements.txt
    export TELEGRAM_BOT_TOKEN=...
    python bot_single_file.py
"""

from __future__ import annotations

import types


# ── Embedded module loader ────────────────────────────────────────────────────

def _load_embedded_module(name: str, source: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__file__ = f"<embedded:{name}>"
    exec(compile(source, f"<embedded:{name}>", "exec"), mod.__dict__)  # noqa: S102
    return mod


# ════════════════════════════════════════════════════════════════════════════
#  EMBEDDED: storage.py
# ════════════════════════════════════════════════════════════════════════════

_STORAGE_SOURCE = r'''
"""Lightweight JSON-backed persistence layer."""

from __future__ import annotations

import json
import os
import time
from collections import defaultdict, deque
from typing import Deque

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DATA_DIR, exist_ok=True)

CUSTOM_WORDS_PATH    = os.path.join(DATA_DIR, "custom_words.json")
USAGE_STATS_PATH     = os.path.join(DATA_DIR, "usage_stats.json")
USER_ACTIVITY_PATH   = os.path.join(DATA_DIR, "user_activity.json")
WATERMARKS_PATH      = os.path.join(DATA_DIR, "watermarks.json")
CUSTOM_LIMITS_PATH   = os.path.join(DATA_DIR, "custom_limits.json")
LIMIT_REQUESTS_PATH  = os.path.join(DATA_DIR, "limit_requests.json")
BLOCKED_USERS_PATH   = os.path.join(DATA_DIR, "blocked_users.json")
RATE_LIMIT_LOG_PATH  = os.path.join(DATA_DIR, "rate_limit_log.json")

MAX_WORDS_PER_USER       = 100
RESERVED_WORDS           = {"fiverr"}
MAX_LOG_ENTRIES_PER_USER = 50
RATE_LIMIT_WINDOW_SECONDS = 60
RATE_LIMIT_HEAVY_MAX_CALLS = 5

import asyncio
HEAVY_JOB_SEMAPHORE = asyncio.Semaphore(3)

HEAVY_TOOLS = {
    "bgremove", "effects", "pptx2images", "pdf2img",
    "watermark", "compress", "iconrender", "mediadownload",
}

_lock: asyncio.Lock | None = None
_rate_lock: asyncio.Lock | None = None

def _get_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock

def _get_rate_lock() -> asyncio.Lock:
    global _rate_lock
    if _rate_lock is None:
        _rate_lock = asyncio.Lock()
    return _rate_lock

_heavy_call_log: dict[int, Deque[float]] = defaultdict(deque)
_rate_log_loaded = False


def _load(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _load_list(path: str) -> list:
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def _save(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


async def add_word(chat_id: int, word: str, replacement: str) -> tuple[bool, str]:
    word, replacement = word.strip().lower(), replacement.strip()
    if not word or not replacement:
        return False, "Both word and replacement must be non-empty."
    if word in RESERVED_WORDS:
        return False, f"'{word}' is reserved and cannot be overridden."
    async with _get_lock():
        data = _load(CUSTOM_WORDS_PATH)
        user_words = data.setdefault(str(chat_id), {})
        was_update = word in user_words
        if not was_update and len(user_words) >= MAX_WORDS_PER_USER:
            return False, f"Limit reached ({MAX_WORDS_PER_USER} words). Remove one with /delword first."
        user_words[word] = replacement
        _save(CUSTOM_WORDS_PATH, data)
    return True, "updated" if was_update else "added"


async def get_words(chat_id: int) -> dict[str, str]:
    async with _get_lock():
        return dict(_load(CUSTOM_WORDS_PATH).get(str(chat_id), {}))


async def del_word(chat_id: int, word: str) -> bool:
    word = word.strip().lower()
    async with _get_lock():
        data = _load(CUSTOM_WORDS_PATH)
        user_words = data.get(str(chat_id), {})
        if word not in user_words:
            return False
        del user_words[word]
        _save(CUSTOM_WORDS_PATH, data)
    return True


async def reset_words(chat_id: int) -> None:
    async with _get_lock():
        data = _load(CUSTOM_WORDS_PATH)
        data.pop(str(chat_id), None)
        _save(CUSTOM_WORDS_PATH, data)


async def record_usage(
    tool_name: str,
    chat_id: int | None = None,
    username: str | None = None,
) -> None:
    now = time.time()
    async with _get_lock():
        stats = _load(USAGE_STATS_PATH)
        stats[tool_name] = stats.get(tool_name, 0) + 1
        _save(USAGE_STATS_PATH, stats)
        if chat_id is not None:
            activity = _load(USER_ACTIVITY_PATH)
            entry = activity.setdefault(str(chat_id), {
                "username": username, "counts": {}, "last_seen": now, "log": [],
            })
            if username:
                entry["username"] = username
            entry["counts"][tool_name] = entry["counts"].get(tool_name, 0) + 1
            entry["last_seen"] = now
            entry["log"].append({"tool": tool_name, "ts": now})
            entry["log"] = entry["log"][-MAX_LOG_ENTRIES_PER_USER:]
            _save(USER_ACTIVITY_PATH, activity)


async def get_stats() -> list[tuple[str, int]]:
    async with _get_lock():
        data = _load(USAGE_STATS_PATH)
    return sorted(data.items(), key=lambda kv: kv[1], reverse=True)


async def get_user_activity_all() -> dict:
    async with _get_lock():
        return _load(USER_ACTIVITY_PATH)


async def get_user_activity(chat_id: int) -> dict | None:
    async with _get_lock():
        return _load(USER_ACTIVITY_PATH).get(str(chat_id))


async def find_chat_id_by_username(username: str) -> int | None:
    username = username.strip().lstrip("@").lower()
    async with _get_lock():
        data = _load(USER_ACTIVITY_PATH)
    for cid, entry in data.items():
        if (entry.get("username") or "").lower() == username:
            return int(cid)
    return None


async def set_watermark(chat_id: int, file_id: str) -> None:
    async with _get_lock():
        data = _load(WATERMARKS_PATH)
        data[str(chat_id)] = file_id
        _save(WATERMARKS_PATH, data)


async def get_watermark(chat_id: int) -> str | None:
    async with _get_lock():
        return _load(WATERMARKS_PATH).get(str(chat_id))


async def clear_watermark(chat_id: int) -> None:
    async with _get_lock():
        data = _load(WATERMARKS_PATH)
        data.pop(str(chat_id), None)
        _save(WATERMARKS_PATH, data)


def _ensure_rate_log_loaded() -> None:
    global _rate_log_loaded
    if _rate_log_loaded:
        return
    raw = _load(RATE_LIMIT_LOG_PATH)
    now = time.monotonic()
    real_now = time.time()
    for cid_str, timestamps in raw.items():
        dq = _heavy_call_log[int(cid_str)]
        for ts in timestamps:
            age = real_now - ts
            if age < RATE_LIMIT_WINDOW_SECONDS:
                dq.append(now - age)
    _rate_log_loaded = True


def _persist_rate_log() -> None:
    now_mono = time.monotonic()
    now_real = time.time()
    out: dict[str, list[float]] = {}
    for cid, dq in _heavy_call_log.items():
        out[str(cid)] = [now_real - (now_mono - ts) for ts in dq]
    _save(RATE_LIMIT_LOG_PATH, out)


async def set_custom_limit(chat_id: int, max_calls: int | None) -> None:
    async with _get_lock():
        data = _load(CUSTOM_LIMITS_PATH)
        if max_calls is None:
            data.pop(str(chat_id), None)
        else:
            data[str(chat_id)] = max(0, int(max_calls))
        _save(CUSTOM_LIMITS_PATH, data)


async def get_custom_limit(chat_id: int) -> int | None:
    async with _get_lock():
        return _load(CUSTOM_LIMITS_PATH).get(str(chat_id))


async def _effective_limit(chat_id: int) -> int:
    custom = await get_custom_limit(chat_id)
    return custom if custom is not None else RATE_LIMIT_HEAVY_MAX_CALLS


async def is_blocked(chat_id: int) -> bool:
    async with _get_lock():
        return chat_id in _load_list(BLOCKED_USERS_PATH)


async def block_user(chat_id: int) -> None:
    async with _get_lock():
        blocked = _load_list(BLOCKED_USERS_PATH)
        if chat_id not in blocked:
            blocked.append(chat_id)
            _save(BLOCKED_USERS_PATH, blocked)
    await set_custom_limit(chat_id, 0)


async def unblock_user(chat_id: int) -> None:
    async with _get_lock():
        blocked = _load_list(BLOCKED_USERS_PATH)
        if chat_id in blocked:
            blocked.remove(chat_id)
            _save(BLOCKED_USERS_PATH, blocked)
    await set_custom_limit(chat_id, None)


async def check_rate_limit(chat_id: int, exempt: bool = False) -> tuple[bool, float]:
    if exempt:
        return True, 0.0
    limit = await _effective_limit(chat_id)
    now = time.monotonic()
    async with _get_rate_lock():
        _ensure_rate_log_loaded()
        log = _heavy_call_log[chat_id]
        while log and now - log[0] > RATE_LIMIT_WINDOW_SECONDS:
            log.popleft()
        if limit <= 0:
            return False, float(RATE_LIMIT_WINDOW_SECONDS)
        if len(log) >= limit:
            wait = RATE_LIMIT_WINDOW_SECONDS - (now - log[0])
            return False, max(wait, 1.0)
        log.append(now)
        _persist_rate_log()
        return True, 0.0


async def calls_used(chat_id: int) -> tuple[int, int]:
    limit = await _effective_limit(chat_id)
    now = time.monotonic()
    async with _get_rate_lock():
        _ensure_rate_log_loaded()
        log = _heavy_call_log[chat_id]
        while log and now - log[0] > RATE_LIMIT_WINDOW_SECONDS:
            log.popleft()
        return len(log), limit


async def add_limit_request(chat_id: int, username: str | None, requested: int) -> None:
    async with _get_lock():
        data = _load(LIMIT_REQUESTS_PATH)
        data[str(chat_id)] = {"username": username, "requested": requested, "ts": time.time()}
        _save(LIMIT_REQUESTS_PATH, data)


async def get_limit_requests() -> dict:
    async with _get_lock():
        return _load(LIMIT_REQUESTS_PATH)


async def remove_limit_request(chat_id: int) -> None:
    async with _get_lock():
        data = _load(LIMIT_REQUESTS_PATH)
        data.pop(str(chat_id), None)
        _save(LIMIT_REQUESTS_PATH, data)
'''

# ════════════════════════════════════════════════════════════════════════════
#  EMBEDDED: qr_tools.py
# ════════════════════════════════════════════════════════════════════════════

_QR_TOOLS_SOURCE = r'''
from __future__ import annotations
import asyncio
from io import BytesIO


async def generate_qr(text: str) -> bytes:
    import qrcode
    from qrcode.constants import ERROR_CORRECT_M

    def _run() -> bytes:
        qr = qrcode.QRCode(version=None, error_correction=ERROR_CORRECT_M, box_size=10, border=4)
        qr.add_data(text)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        buf = BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue()

    return await asyncio.to_thread(_run)


async def scan_qr(image_bytes: bytes) -> list[str]:
    import cv2, numpy as np

    def _run() -> list[str]:
        arr = np.frombuffer(image_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError("Could not decode image.")
        detector = cv2.QRCodeDetector()
        found: list[str] = []
        try:
            ok, decoded_info, _, _ = detector.detectAndDecodeMulti(img)
            if ok:
                found = [s for s in decoded_info if s]
        except cv2.error:
            pass
        if not found:
            data, _, _ = detector.detectAndDecode(img)
            if data:
                found = [data]
        return found

    return await asyncio.to_thread(_run)
'''

# ════════════════════════════════════════════════════════════════════════════
#  EMBEDDED: password_tools.py
# ════════════════════════════════════════════════════════════════════════════

_PASSWORD_TOOLS_SOURCE = r'''
from __future__ import annotations
import secrets, string

AMBIGUOUS_CHARS = "il1Lo0O"
LOWER   = string.ascii_lowercase
UPPER   = string.ascii_uppercase
DIGITS  = string.digits
SYMBOLS = "!@#$%^&*()-_=+[]{};:,.?/"
MIN_LENGTH, MAX_LENGTH, DEFAULT_LENGTH = 4, 128, 16


def generate_password(
    length: int = DEFAULT_LENGTH,
    use_upper: bool = True,
    use_lower: bool = True,
    use_digits: bool = True,
    use_symbols: bool = True,
    no_ambiguous: bool = False,
) -> str:
    length = max(MIN_LENGTH, min(MAX_LENGTH, length))
    pools = [p for flag, p in ((use_lower, LOWER), (use_upper, UPPER), (use_digits, DIGITS), (use_symbols, SYMBOLS)) if flag] or [LOWER, DIGITS]
    if no_ambiguous:
        pools = ["".join(c for c in p if c not in AMBIGUOUS_CHARS) or p for p in pools]
    alphabet = "".join(pools)
    chars = [secrets.choice(p) for p in pools] + [secrets.choice(alphabet) for _ in range(length - len(pools))]
    for i in range(len(chars) - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        chars[i], chars[j] = chars[j], chars[i]
    return "".join(chars[:length])


def generate_pin(length: int = 4) -> str:
    return "".join(secrets.choice(string.digits) for _ in range(max(3, min(12, length))))
'''

# ════════════════════════════════════════════════════════════════════════════
#  EMBEDDED: image_extra.py
# ════════════════════════════════════════════════════════════════════════════

_IMAGE_EXTRA_SOURCE = r'''
from __future__ import annotations
import asyncio
from io import BytesIO

DEFAULT_WATERMARK_SCALE   = 0.15
DEFAULT_WATERMARK_OPACITY = 0.60
DEFAULT_MARGIN_PX         = 16
_POSITIONS = {"top-left", "top-right", "bottom-left", "bottom-right", "center"}


def _compute_position(pos, bw, bh, ww, wh, m):
    if pos == "top-left":    return m, m
    if pos == "top-right":   return bw - ww - m, m
    if pos == "bottom-left": return m, bh - wh - m
    if pos == "center":      return (bw - ww) // 2, (bh - wh) // 2
    return bw - ww - m, bh - wh - m


async def apply_watermark(
    base_bytes: bytes, watermark_bytes: bytes,
    position: str = "bottom-right",
    scale: float = DEFAULT_WATERMARK_SCALE,
    opacity: float = DEFAULT_WATERMARK_OPACITY,
) -> bytes:
    from PIL import Image, ImageOps
    position = position if position in _POSITIONS else "bottom-right"
    scale    = min(max(scale, 0.02), 0.9)
    opacity  = min(max(opacity, 0.05), 1.0)

    def _run() -> bytes:
        base = ImageOps.exif_transpose(Image.open(BytesIO(base_bytes))).convert("RGBA")
        wm   = Image.open(BytesIO(watermark_bytes)).convert("RGBA")
        tw   = max(1, int(base.width * scale))
        wm   = wm.resize((tw, max(1, int(wm.height * tw / wm.width))))
        if opacity < 1.0:
            wm.putalpha(wm.split()[3].point(lambda a: int(a * opacity)))
        x, y = _compute_position(position, base.width, base.height, wm.width, wm.height, DEFAULT_MARGIN_PX)
        composed = base.copy()
        composed.alpha_composite(wm, dest=(x, y))
        buf = BytesIO()
        composed.convert("RGB").save(buf, format="JPEG", quality=95, optimize=True)
        return buf.getvalue()

    return await asyncio.to_thread(_run)


async def compress_to_target(image_bytes: bytes, target_bytes: int) -> bytes:
    from PIL import Image, ImageOps

    def _run() -> bytes:
        img = ImageOps.exif_transpose(Image.open(BytesIO(image_bytes))).convert("RGB")
        for q in (95, 85, 75, 65, 55, 45, 35, 25):
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=q, optimize=True)
            if buf.tell() <= target_bytes:
                return buf.getvalue()
        cur = img
        for _ in range(6):
            cur = cur.resize((max(1, int(cur.width * 0.8)), max(1, int(cur.height * 0.8))))
            for q in (75, 60, 45, 30):
                buf = BytesIO()
                cur.save(buf, format="JPEG", quality=q, optimize=True)
                if buf.tell() <= target_bytes:
                    return buf.getvalue()
        return buf.getvalue()

    return await asyncio.to_thread(_run)


def parse_size_to_bytes(text: str) -> int | None:
    text = text.strip().lower().replace(" ", "")
    try:
        if text.endswith("kb"): return int(float(text[:-2]) * 1024)
        if text.endswith("mb"): return int(float(text[:-2]) * 1024 * 1024)
        if text.endswith("b"):  return int(text[:-1])
        return int(text)
    except ValueError:
        return None


async def resize_image(image_bytes: bytes, width: int, height: int) -> bytes:
    """Resize a raster image (Lummi asset etc.) to an explicit width/height."""
    from PIL import Image, ImageOps
    def _run() -> bytes:
        img = ImageOps.exif_transpose(Image.open(BytesIO(image_bytes)))
        mode = "RGBA" if img.mode in ("RGBA", "LA", "P") else "RGB"
        img  = img.convert(mode).resize((max(1, int(width)), max(1, int(height))), Image.LANCZOS)
        buf  = BytesIO()
        img.save(buf, format="PNG" if mode == "RGBA" else "JPEG",
                 optimize=True, quality=95 if mode == "RGB" else None)
        return buf.getvalue()
    return await asyncio.to_thread(_run)


async def crop_to_aspect(image_bytes: bytes, ratio_w: int, ratio_h: int, mode: str = "crop") -> bytes:
    """Crop or pad a raster image to a target aspect ratio (independent of effects)."""
    from PIL import Image, ImageOps
    def _run() -> bytes:
        img = ImageOps.exif_transpose(Image.open(BytesIO(image_bytes))).convert("RGB")
        w, h = img.size
        target = ratio_w / ratio_h
        cur = w / h
        if abs(cur - target) < 1e-3:
            out = img
        elif mode == "pad":
            if cur > target:
                new_h = max(1, int(round(w / target)))
                canvas = Image.new("RGB", (w, new_h), (0, 0, 0))
                canvas.paste(img, (0, (new_h - h) // 2))
                out = canvas
            else:
                new_w = max(1, int(round(h * target)))
                canvas = Image.new("RGB", (new_w, h), (0, 0, 0))
                canvas.paste(img, ((new_w - w) // 2, 0))
                out = canvas
        else:
            if cur > target:
                new_w = max(1, int(round(h * target)))
                left = (w - new_w) // 2
                out = img.crop((left, 0, left + new_w, h))
            else:
                new_h = max(1, int(round(w / target)))
                top = (h - new_h) // 2
                out = img.crop((0, top, w, top + new_h))
        buf = BytesIO()
        out.save(buf, format="JPEG", quality=95, optimize=True)
        return buf.getvalue()
    return await asyncio.to_thread(_run)
'''

# ════════════════════════════════════════════════════════════════════════════
#  EMBEDDED: media_downloader.py  (YouTube / X / Facebook video+audio)
# ════════════════════════════════════════════════════════════════════════════

_MEDIA_DOWNLOADER_SOURCE = r'''
"""Thin async wrapper around yt-dlp for the supported platforms.

Only public, non-age-restricted, non-DRM content that the platform serves
without login is expected to work. This module does not attempt to bypass
any paywall, login wall, or DRM. Users are responsible for respecting the
copyright and terms of service of the platform and the content owner —
only download content you have the right to download (your own uploads,
Creative-Commons / public-domain material, or content whose owner has
given permission).
"""

from __future__ import annotations
import asyncio
import os
import re
import tempfile
import uuid

MAX_DOWNLOAD_BYTES = 49 * 1024 * 1024  # Telegram bot upload cap
DOWNLOAD_TIMEOUT_SECONDS = 180

SUPPORTED_HOST_RE = re.compile(
    r"(youtube\.com|youtu\.be|x\.com|twitter\.com|facebook\.com|fb\.watch)",
    re.IGNORECASE,
)


def is_supported_url(url: str) -> bool:
    return bool(SUPPORTED_HOST_RE.search(url))


def _base_opts(out_template: str) -> dict:
    return {
        "outtmpl": out_template,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "max_filesize": MAX_DOWNLOAD_BYTES,
        "socket_timeout": 30,
    }


async def fetch_metadata(url: str) -> dict:
    import yt_dlp

    def _run() -> dict:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}) as ydl:
            info = ydl.extract_info(url, download=False)
            return {
                "title": info.get("title") or "media",
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
            opts["format"] = f"bestvideo[height<={max_height}]+bestaudio/best[height<={max_height}]/best"
            opts["merge_output_format"] = "mp4"
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                path = ydl.prepare_filename(info)
                if not os.path.exists(path):
                    alt = os.path.splitext(path)[0] + ".mp4"
                    path = alt if os.path.exists(alt) else path
                with open(path, "rb") as f:
                    data = f.read()
                title = info.get("title") or "video"
                return data, title

    return await asyncio.wait_for(asyncio.to_thread(_run), timeout=DOWNLOAD_TIMEOUT_SECONDS)


async def download_audio(url: str) -> tuple[bytes, str]:
    import yt_dlp

    def _run() -> tuple[bytes, str]:
        with tempfile.TemporaryDirectory() as tmp:
            out_tmpl = os.path.join(tmp, f"{uuid.uuid4().hex}.%(ext)s")
            opts = _base_opts(out_tmpl)
            opts["format"] = "bestaudio/best"
            opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }]
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                path = os.path.splitext(ydl.prepare_filename(info))[0] + ".mp3"
                with open(path, "rb") as f:
                    data = f.read()
                title = info.get("title") or "audio"
                return data, title

    return await asyncio.wait_for(asyncio.to_thread(_run), timeout=DOWNLOAD_TIMEOUT_SECONDS)
'''

# ── Load embedded modules ─────────────────────────────────────────────────────
storage           = _load_embedded_module("storage",           _STORAGE_SOURCE)
qr_tools          = _load_embedded_module("qr_tools",          _QR_TOOLS_SOURCE)
password_tools    = _load_embedded_module("password_tools",    _PASSWORD_TOOLS_SOURCE)
image_extra       = _load_embedded_module("image_extra",       _IMAGE_EXTRA_SOURCE)
media_downloader  = _load_embedded_module("media_downloader",  _MEDIA_DOWNLOADER_SOURCE)


# ════════════════════════════════════════════════════════════════════════════
#  MAIN BOT
# ════════════════════════════════════════════════════════════════════════════

import asyncio
import base64
import datetime
import hashlib
import json
import logging
import os
import re
import secrets
import string
import sys
import tempfile
import time
import uuid
from io import BytesIO
from typing import Any, Callable, Coroutine
from urllib.parse import quote, unquote, urlsplit

import httpx
from bs4 import BeautifulSoup
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Update,
)
from telegram.constants import ChatAction
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


# ── Constants ─────────────────────────────────────────────────────────────────
MAX_UPLOAD_BYTES          = 49 * 1024 * 1024
MAX_CAPTION_LENGTH        = 1_024
BOT_DOWNLOAD_LIMIT_BYTES  = 20 * 1024 * 1024
PDF2IMG_MAX_PAGES         = 20
MAX_PENDING_FILES         = 500
TOKEN_TTL_SECONDS         = 1_800          # 30 minutes
TRANSLATE_MAX_CHARS       = 4_500
MYMEMORY_MAX_CHARS        = 500
FIVER_SANITIZE_MAX_CHARS  = 4_500
REMBG_MAX_DIMENSION       = 2_000
REMBG_TIMEOUT_SECONDS     = 90
GIF_MAX_FRAMES            = 10
IMAGE_MAX_DIM             = 1_200
DEFAULT_COMPRESS_TARGET   = 1 * 1024 * 1024
MAX_LIMIT_REQUEST_VALUE   = 100

REQUEST_TIMEOUT     = httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=10.0)
HTTP_RETRY_ATTEMPTS = 3
HTTP_RETRY_BACKOFF  = 1.5

ENGINE_SCRIPT    = os.path.join(os.path.dirname(__file__), "python_engine.py")
U2NETP_MODEL_URL = "https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2netp.onnx"
U2NETP_MODEL_PATH = os.path.join(tempfile.gettempdir(), "u2netp.onnx")

ICON_SIZE_PRESETS: list[int] = [64, 128, 256, 512, 1024]

ASPECT_RATIO_PRESETS: list[tuple[str, str]] = [
    ("original", "Original"),
    ("1:1",  "1:1 Square"),
    ("4:5",  "4:5 Portrait"),
    ("9:16", "9:16 Story"),
    ("16:9", "16:9 Wide"),
    ("4:3",  "4:3"),
    ("3:2",  "3:2"),
]


# ── Admin roster ──────────────────────────────────────────────────────────────
SUPER_ADMINS: dict[str, dict[str, str]] = {
    "ya_rabbu": {
        "name": "Yasir Abed Rabbu",
        "email": "yasirabedrabbu@gmail.com",
        "telegram": "https://t.me/YA_Rabbu",
    },
}
ADMINS: dict[str, dict[str, str]] = {
    "smashik_softvence": {
        "name": "Sheikh Muhammad Ashik",
        "email": "smashik716@gmail.com",
        "telegram": "https://t.me/smashik_softvence",
    },
}

ADMIN_CHAT_ID    = int(os.getenv("ADMIN_CHAT_ID", "0") or 0)
_admin_chat_ids: dict[str, int] = {}
_ADMIN_ERROR_THROTTLE_SECONDS  = 600
_last_admin_alert: dict[str, float] = {}


# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("bangaliicon")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

LUMMI_URL_RE = re.compile(
    r"https?://(?:www\.)?lummi\.ai/(?:photo|illustration|3d)/[^\s<>]+", re.IGNORECASE
)
URL_RE       = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
LUMMI_CID_RE = re.compile(r"Qm[1-9A-HJ-NP-Za-km-z]{44}")
GREETING_RE  = re.compile(
    r"^(hi+|he+llo+|hey+|yo|start|salam|assalamu\s*alaikum|assalamualaikum)[!.\s]*$",
    re.IGNORECASE,
)
MEDIA_URL_RE = re.compile(
    r"https?://[^\s<>]*(?:youtube\.com|youtu\.be|x\.com|twitter\.com|facebook\.com|fb\.watch)[^\s<>]*",
    re.IGNORECASE,
)

_http_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(headers=HEADERS, timeout=REQUEST_TIMEOUT, follow_redirects=True)
    return _http_client


async def close_http_client() -> None:
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
        _http_client = None


async def with_retry(fn: Callable[[], Coroutine], attempts: int = HTTP_RETRY_ATTEMPTS, backoff: float = HTTP_RETRY_BACKOFF) -> Any:
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            return await fn()
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            last_exc = exc
            if attempt < attempts - 1:
                await asyncio.sleep(backoff * (2 ** attempt))
    raise last_exc


# ── Admin helpers ─────────────────────────────────────────────────────────────

def _username_of(update: Update) -> str | None:
    user = update.effective_user
    return (user.username or "").lower() if user and user.username else None


def _display_name(update: Update, escape_markdown: bool = False) -> str:
    user = update.effective_user
    if user and user.username:
        label = f"@{user.username}"
    elif user and user.first_name:
        label = user.first_name
    else:
        label = f"id:{update.effective_chat.id}"
    return _esc_md(label) if escape_markdown else label


def is_super_admin(update: Update) -> bool:
    uname = _username_of(update)
    return uname is not None and uname in SUPER_ADMINS


def is_admin(update: Update) -> bool:
    uname = _username_of(update)
    return uname is not None and (uname in SUPER_ADMINS or uname in ADMINS)


def admin_role_label(update: Update) -> str:
    if is_super_admin(update): return "Super Admin"
    if is_admin(update):       return "Admin"
    return "User"


def _remember_admin_chat_id(update: Update) -> None:
    uname = _username_of(update)
    if uname and (uname in SUPER_ADMINS or uname in ADMINS) and update.effective_chat:
        _admin_chat_ids[uname] = update.effective_chat.id


async def _get_profile_photo_bytes(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bytes | None:
    try:
        photos = await context.bot.get_user_profile_photos(user_id, limit=1)
        if not photos.photos:
            return None
        file_id = photos.photos[0][-1].file_id
        return await _download_file(context, file_id)
    except Exception:
        return None


async def notify_admin(
    context: ContextTypes.DEFAULT_TYPE,
    error_key: str,
    message: str,
    throttle: bool = True,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    targets = set(_admin_chat_ids.values())
    if ADMIN_CHAT_ID:
        targets.add(ADMIN_CHAT_ID)
    if not targets:
        return
    if throttle:
        now  = asyncio.get_event_loop().time()
        last = _last_admin_alert.get(error_key, 0)
        if now - last < _ADMIN_ERROR_THROTTLE_SECONDS:
            return
        _last_admin_alert[error_key] = now
    for chat_id in targets:
        try:
            await context.bot.send_message(chat_id=chat_id, text=f"⚠️ {message}"[:4000], reply_markup=reply_markup)
        except Exception:
            logger.exception("Failed to notify admin %s", chat_id)


# ── Effects catalogue ─────────────────────────────────────────────────────────
EFFECT_CATEGORIES: dict[str, str] = {
    "retro":   "Retro & Print",
    "tone":    "Color & Tone",
    "art":     "Artistic",
    "digital": "Digital & Glitch",
    "distort": "Distort & Special",
}
EFFECT_CATEGORY_ORDER = ["retro", "tone", "art", "digital", "distort"]

EFFECTS: list[tuple[str, str, str]] = [
    ("halftone-dots",        "Halftone",       "retro"),
    ("comic-cmyk",           "Pop-Art",        "retro"),
    ("retro-8bit",           "8-Bit CRT",      "retro"),
    ("risograph-duo",        "Risograph",      "retro"),
    ("crosshatch-engraving", "Crosshatch",     "retro"),
    ("bayer-dither",         "Dither",         "retro"),
    ("vhs-tape",             "VHS Tape",       "retro"),
    ("lomography",           "Lomography",     "retro"),
    ("duotone",              "Duotone",        "tone"),
    ("autumn-tone",          "Autumn",         "tone"),
    ("forest-green",         "Forest",         "tone"),
    ("desert-sand",          "Desert",         "tone"),
    ("cherry-blossom",       "Cherry Blossom", "tone"),
    ("moonlight",            "Moonlight",      "tone"),
    ("frozen-ice",           "Frozen",         "tone"),
    ("watercolor",           "Watercolor",     "art"),
    ("oil-paint",            "Oil Paint",      "art"),
    ("cinematic-noir",       "Noir",           "art"),
    ("emboss",               "Emboss",         "art"),
    ("pencil-sketch",        "Pencil Sketch",  "art"),
    ("color-splash",         "Color Splash",   "art"),
    ("stained-glass",        "Stained Glass",  "art"),
    ("cyber-glitch",         "Cyberpunk",      "digital"),
    ("glitch-art",           "Glitch Art",     "digital"),
    ("ascii-matrix",         "ASCII",          "digital"),
    ("sobel-neon",           "Neon Edge",      "digital"),
    ("vaporwave",            "Vaporwave",      "digital"),
    ("neon-poster",          "Neon Poster",    "digital"),
    ("neon-glow",            "Neon Glow",      "digital"),
    ("swirl-distort",        "Swirl",          "distort"),
    ("mirror-reflect",       "Mirror",         "distort"),
    ("pixelate",             "Pixelate",       "distort"),
    ("blueprint-cyan",       "Blueprint",      "distort"),
    ("thermal-flir",         "Thermal",        "distort"),
    ("inferno",              "Inferno",        "distort"),
    ("horror-red",           "Horror",         "distort"),
    ("tilt-shift",           "Tilt-Shift",     "distort"),
    ("kaleidoscope",         "Kaleidoscope",   "distort"),
]
_seen: set[str] = set()
EFFECTS = [(k, l, c) for k, l, c in EFFECTS if k not in _seen and not _seen.add(k)]  # type: ignore[func-returns-value]

EFFECT_NAMES = {key: label for key, label, _ in EFFECTS}

DEFAULT_FX_PARAMS: dict[str, Any] = {
    "dotPitch":       8,
    "contrast":       1.2,
    "brightness":     1.0,
    "grainIntensity": 18,
    "vignette":       0.3,
    "aspectRatio":    "original",
    "cropMode":       "crop",
    "outputWidth":    None,
    "outputHeight":   None,
}

TRANSLATE_LANGUAGES: list[tuple[str, str]] = [
    ("en",    "🇬🇧 English"),
    ("bn",    "🇧🇩 বাংলা"),
    ("hi",    "🇮🇳 हिन्दी"),
    ("ar",    "🇸🇦 العربية"),
    ("es",    "🇪🇸 Español"),
    ("fr",    "🇫🇷 Français"),
    ("zh-CN", "🇨🇳 中文"),
    ("ja",    "🇯🇵 日本語"),
]


# ── Fiver Sanitizer v3 ────────────────────────────────────────────────────────

_AI_SIGNS = re.compile(
    r"[\u2014\u2013\u2012\u2015]"
    r"|[\u2018\u2019\u201A\u201B]"
    r"|[\u201C\u201D\u201E\u201F]"
    r"|[\u2026]"
    r"|(?<!\w)_(?!\w)"
    r"|(?<=\s)_|_(?=\s)",
    re.UNICODE,
)

FIVER_WORD_MAP: dict[str, str] = {
    "email": "ema-il", "gmail": "gma-il", "whatsapp": "wha-tsapp",
    "skype": "sky-pe", "telegram": "tele-gram", "discord": "dis-cord",
    "phone": "pho-ne", "mobile": "mobi-le", "number": "num-ber",
    "contact": "conta-ct", "zoom": "zo-om", "slack": "sla-ck",
    "linkedin": "link-edin", "facebook": "face-book", "instagram": "inst-agram",
    "twitter": "twit-ter", "youtube": "yout-ube", "tiktok": "tik-tok",
    "snapchat": "snap-chat", "pinterest": "pint-erest", "reddit": "red-dit",
    "meeting": "mee-ting", "call": "ca-ll", "anydesk": "any-desk",
    "teamviewer": "team-viewer", "payment": "pa-yment", "paypal": "p-ay-pal",
    "pay": "p-ay", "payoneer": "p-ay-oneer", "crypto": "cry-pto",
    "bitcoin": "bit-coin", "wire": "wi-re", "bank": "ba-nk",
    "transfer": "trans-fer", "cash": "ca-sh", "invoice": "invo-ice",
    "outside": "outsi-de", "direct": "dire-ct", "stripe": "str-ipe",
    "fee": "f-ee", "price": "pri-ce", "cost": "co-st", "billing": "bill-ing",
    "homework": "home-work", "assignment": "assign-ment", "essay": "ess-ay",
    "thesis": "the-sis", "exam": "ex-am", "test": "te-st",
    "review": "revi-ew", "feedback": "feed-back", "rating": "rat-ing",
    "password": "pass-word", "login": "lo-gin", "address": "addr-ess",
    "guaranteed": "guaran-teed", "money": "mon-ey", "income": "inco-me",
    "profit": "pro-fit", "fiverr": "fiv-err", "order": "ord-er",
    "cancel": "can-cel", "refund": "refu-nd",
}


def _preserve_case(original: str, replacement: str) -> str:
    if original.islower():              return replacement.lower()
    if original.isupper():              return replacement.upper()
    if original[:1].isupper():         return replacement[:1].upper() + replacement[1:].lower()
    return replacement


def _build_patterns(word_map: dict[str, str]) -> list[tuple[str, re.Pattern]]:
    return [
        (w, re.compile(rf"\b{re.escape(w)}\b", re.IGNORECASE))
        for w in sorted(word_map, key=len, reverse=True)
    ]


_FIVER_WORD_PATTERNS = _build_patterns(FIVER_WORD_MAP)

FIVER_INFO_PATTERN_HINT = "ClientName_OrderID_ProfileName_Amount"

GREETING_NORMALIZE_RE = re.compile(
    r"^\s*(hi+|hey+|hello+)\b[\s,!.]*",
    re.IGNORECASE,
)

SIGNOFF_RE = re.compile(
    r"\n{1,3}\s*(best\s*regards|regards|best|thanks\s*&?\s*regards|warm\s*regards|sincerely)[\s,]*\n?.*$",
    re.IGNORECASE | re.DOTALL,
)

THANK_YOU_CHECK_CHARS = 250
DEFAULT_CLOSING_LINE = "Thank you again for your support, and I'll keep you updated on the progress."

FIVER_TEMPLATE = (
    "==================================\n"
    "Status: Update (Updated)\n"
    "Profile Name: {profile}\n"
    "Client Name: {client}\n"
    "Project Name: {project}\n"
    "Ord-er ID: {order_id}\n"
    "Amount: {amount}\n"
    "Quality Checked by: Me\n"
    "==================================\n\n"
    "{body}\n"
    "=================================="
)


def parse_fiver_info(text: str) -> dict[str, str] | None:
    parts = [p.strip() for p in text.strip().split("_")]
    if len(parts) != 5 or not all(parts):
        return None
    client, order_id, project, profile, amount = parts
    if not amount.startswith("$"):
        amount = f"${amount}"
    return {
        "client": client,
        "order_id": order_id,
        "project": project,
        "profile": profile,
        "amount": amount,
    }


def normalize_greeting(text: str) -> tuple[str, str | None]:
    m = GREETING_NORMALIZE_RE.match(text)
    if not m:
        return text, None
    original = m.group(0).strip().rstrip(",.! ")
    new_text = GREETING_NORMALIZE_RE.sub("Hello there,\n\n", text, count=1)
    return new_text, f"Greeting normalized: `{original}` → `Hello there,`"


def normalize_closing(text: str) -> tuple[str, str | None]:
    tail = text[-THANK_YOU_CHECK_CHARS:]
    has_thanks_in_tail = "thank you" in tail.lower()

    signoff_match = SIGNOFF_RE.search(text)
    if signoff_match:
        stripped = text[: signoff_match.start()].rstrip()
        new_text = f"{stripped}\n\n{DEFAULT_CLOSING_LINE}"
        return new_text, "Sign-off removed and replaced with a thank-you closing line."

    if not has_thanks_in_tail:
        new_text = f"{text.rstrip()}\n\n{DEFAULT_CLOSING_LINE}"
        return new_text, "No thank-you found at the end — closing line added."

    return text, None


def build_fiver_output(info: dict[str, str], sanitized_body: str) -> str:
    return FIVER_TEMPLATE.format(
        profile=info["profile"],
        client=info["client"],
        project=info["project"],
        order_id=info["order_id"],
        amount=info["amount"],
        body=sanitized_body.strip(),
    )


def sanitize_fiver_text(
    text: str,
    custom_words: dict[str, str] | None = None,
) -> tuple[str, list[str], int]:
    changes: list[str] = []

    plain = markdown_to_plain_text(text)
    if plain != text:
        changes.append("Markdown formatting removed (bold/italic/headers/links)")
    text = plain

    ai_stripped = _AI_SIGNS.sub("", text)
    stripped_count = len(text) - len(ai_stripped)
    if stripped_count:
        changes.append(f"AI signs removed ({stripped_count} character(s))")
    result = ai_stripped

    merged   = {**FIVER_WORD_MAP, **(custom_words or {})}
    patterns = _build_patterns(merged) if custom_words else _FIVER_WORD_PATTERNS

    for word, pattern in patterns:
        replacement = merged[word]
        new_result, n = pattern.subn(
            lambda m: _preserve_case(m.group(0), replacement), result
        )
        if n:
            changes.append(f"`{word}` → `{replacement}` (×{n})")
        result = new_result

    result, greet_note = normalize_greeting(result)
    if greet_note:
        changes.append(greet_note)

    result, close_note = normalize_closing(result)
    if close_note:
        changes.append(close_note)

    score = max(0, 100 - len(changes) * 8)
    return result, changes, score


def format_sanitizer_report(changes: list[str], score: int) -> str:
    quality_icon = "✅" if score >= 80 else ("⚠️" if score >= 50 else "❌")
    lines = [f"📊 *Quality Check:* {quality_icon} {score}/100", ""]
    if changes:
        lines.append("🔄 *What was changed:*")
        for c in changes[:15]:
            lines.append(f"  • {c}")
        if len(changes) > 15:
            lines.append(f"  … and {len(changes) - 15} more")
    else:
        lines.append("✅ No flagged words found — message is clean!")
    return "\n".join(lines)


# ── Color Tools (no external API) ─────────────────────────────────────────────

def hex_to_rgb(hex_str: str) -> tuple[int, int, int] | None:
    hex_str = hex_str.strip().lstrip("#")
    if len(hex_str) == 3:
        hex_str = "".join(c * 2 for c in hex_str)
    if len(hex_str) != 6:
        return None
    try:
        r = int(hex_str[0:2], 16)
        g = int(hex_str[2:4], 16)
        b = int(hex_str[4:6], 16)
        return r, g, b
    except ValueError:
        return None


def rgb_to_hex(r: int, g: int, b: int) -> str:
    return f"#{r:02X}{g:02X}{b:02X}"


def rgb_to_hsl(r: int, g: int, b: int) -> tuple[float, float, float]:
    r_, g_, b_ = r / 255, g / 255, b / 255
    cmax, cmin = max(r_, g_, b_), min(r_, g_, b_)
    delta = cmax - cmin
    l = (cmax + cmin) / 2
    s = 0.0 if delta == 0 else delta / (1 - abs(2 * l - 1))
    if delta == 0:
        h = 0.0
    elif cmax == r_:
        h = 60 * (((g_ - b_) / delta) % 6)
    elif cmax == g_:
        h = 60 * ((b_ - r_) / delta + 2)
    else:
        h = 60 * ((r_ - g_) / delta + 4)
    return round(h, 1), round(s * 100, 1), round(l * 100, 1)


def rgb_to_cmyk(r: int, g: int, b: int) -> tuple[int, int, int, int]:
    if r == g == b == 0:
        return 0, 0, 0, 100
    r_, g_, b_ = r / 255, g / 255, b / 255
    k = 1 - max(r_, g_, b_)
    c = (1 - r_ - k) / (1 - k)
    m = (1 - g_ - k) / (1 - k)
    y = (1 - b_ - k) / (1 - k)
    return round(c * 100), round(m * 100), round(y * 100), round(k * 100)


def wcag_contrast(r1, g1, b1, r2, g2, b2) -> float:
    def lum(r, g, b):
        vals = [v / 255 for v in (r, g, b)]
        vals = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in vals]
        return 0.2126 * vals[0] + 0.7152 * vals[1] + 0.0722 * vals[2]
    l1, l2 = lum(r1, g1, b1), lum(r2, g2, b2)
    if l1 < l2:
        l1, l2 = l2, l1
    return round((l1 + 0.05) / (l2 + 0.05), 2)


def tint_shade(r: int, g: int, b: int) -> str:
    lines = []
    for pct in (90, 75, 50, 25, 10):
        tr = int(r + (255 - r) * pct / 100)
        tg = int(g + (255 - g) * pct / 100)
        tb = int(b + (255 - b) * pct / 100)
        lines.append(f"  Tint  {pct:3d}%: {rgb_to_hex(tr, tg, tb)}")
    for pct in (10, 25, 50, 75, 90):
        sr = int(r * (100 - pct) / 100)
        sg = int(g * (100 - pct) / 100)
        sb = int(b * (100 - pct) / 100)
        lines.append(f"  Shade {pct:3d}%: {rgb_to_hex(sr, sg, sb)}")
    return "\n".join(lines)


def css_gradient(r: int, g: int, b: int) -> str:
    r2 = min(255, r + 60)
    g2 = min(255, g + 60)
    b2 = min(255, b + 60)
    c1, c2 = rgb_to_hex(r, g, b), rgb_to_hex(r2, g2, b2)
    return (
        f"/* Linear */\n"
        f"background: linear-gradient(135deg, {c1}, {c2});\n\n"
        f"/* Radial */\n"
        f"background: radial-gradient(circle, {c1}, {c2});"
    )


def css_shadow(r: int, g: int, b: int) -> str:
    return (
        f"/* Soft shadow */\n"
        f"box-shadow: 0 4px 16px rgba({r}, {g}, {b}, 0.25);\n\n"
        f"/* Hard shadow */\n"
        f"box-shadow: 4px 4px 0px rgba({r}, {g}, {b}, 0.8);\n\n"
        f"/* Inner glow */\n"
        f"box-shadow: inset 0 0 12px rgba({r}, {g}, {b}, 0.4);"
    )


# ── Dev Tools (no external API) ───────────────────────────────────────────────

def format_json(text: str) -> str:
    try:
        parsed = json.loads(text)
        return json.dumps(parsed, indent=2, ensure_ascii=False)
    except json.JSONDecodeError as exc:
        return f"❌ Invalid JSON: {exc}"


def hash_text(text: str) -> str:
    encoded = text.encode("utf-8")
    return (
        f"MD5:    `{hashlib.md5(encoded).hexdigest()}`\n"
        f"SHA1:   `{hashlib.sha1(encoded).hexdigest()}`\n"
        f"SHA256: `{hashlib.sha256(encoded).hexdigest()}`\n"
        f"SHA512: `{hashlib.sha512(encoded).hexdigest()[:64]}…`"
    )


def generate_uuid() -> str:
    u = uuid.uuid4()
    return (
        f"UUID v4: `{u}`\n"
        f"No dashes: `{u.hex}`\n"
        f"URN: `urn:uuid:{u}`"
    )


def convert_timestamp(value: str) -> str:
    value = value.strip()
    try:
        ts = float(value)
        dt = datetime.datetime.utcfromtimestamp(ts)
        return (
            f"Unix: `{int(ts)}`\n"
            f"UTC:  `{dt.strftime('%Y-%m-%d %H:%M:%S')} UTC`\n"
            f"ISO:  `{dt.isoformat()}Z`"
        )
    except ValueError:
        pass
    try:
        dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (
            f"Unix: `{int(dt.timestamp())}`\n"
            f"UTC:  `{dt.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC`\n"
            f"ISO:  `{dt.isoformat()}`"
        )
    except ValueError:
        return "❌ Provide a Unix timestamp (e.g. `1700000000`) or ISO date (`2024-01-15T12:00:00`)."


_LOREM_WORDS = (
    "lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor "
    "incididunt ut labore et dolore magna aliqua enim ad minim veniam quis nostrud "
    "exercitation ullamco laboris nisi ut aliquip ex ea commodo consequat duis aute "
    "irure dolor in reprehenderit voluptate velit esse cillum dolore eu fugiat nulla "
    "pariatur excepteur sint occaecat cupidatat non proident sunt in culpa qui officia "
    "deserunt mollit anim id est laborum".split()
)


def lorem_ipsum(n_words: int = 50) -> str:
    import random
    words = [random.choice(_LOREM_WORDS) for _ in range(n_words)]  # noqa: S311
    words[0] = words[0].capitalize()
    return " ".join(words) + "."


def convert_case(text: str, mode: str) -> str:
    if mode == "camel":
        words = re.split(r"[\s_\-]+", text)
        return words[0].lower() + "".join(w.capitalize() for w in words[1:])
    if mode == "pascal":
        return "".join(w.capitalize() for w in re.split(r"[\s_\-]+", text))
    if mode == "snake":
        s = re.sub(r"(?<!^)(?=[A-Z])", "_", text).lower()
        return re.sub(r"[\s\-]+", "_", s)
    if mode == "kebab":
        s = re.sub(r"(?<!^)(?=[A-Z])", "-", text).lower()
        return re.sub(r"[\s_]+", "-", s)
    if mode == "upper":
        return text.upper()
    if mode == "lower":
        return text.lower()
    if mode == "title":
        return text.title()
    return text


def px_to_rem(px: float, base: float = 16.0) -> str:
    return (
        f"`{px}px` = `{px / base:.4f}rem` (base {base}px)\n"
        f"Common bases:\n"
        f"  16px base → `{px/16:.4f}rem`\n"
        f"  14px base → `{px/14:.4f}rem`\n"
        f"  18px base → `{px/18:.4f}rem`"
    )


def url_encode(text: str) -> str:
    return quote(text, safe="")


def url_decode(text: str) -> str:
    return unquote(text)


def base64_encode(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def base64_decode(text: str) -> str:
    try:
        return base64.b64decode(text.encode("ascii")).decode("utf-8")
    except Exception:
        return "❌ Invalid base64 string."


def word_count(text: str) -> str:
    words = text.split()
    chars = len(text)
    chars_no_space = len(text.replace(" ", ""))
    lines = text.count("\n") + 1
    read_time = max(1, round(len(words) / 200))
    return (
        f"Words:          `{len(words)}`\n"
        f"Characters:     `{chars}`\n"
        f"Chars (no sp):  `{chars_no_space}`\n"
        f"Lines:          `{lines}`\n"
        f"Reading time:   ~{read_time} min"
    )


def diff_texts(text_a: str, text_b: str) -> str:
    import difflib
    diff = list(difflib.unified_diff(
        text_a.splitlines(keepends=True),
        text_b.splitlines(keepends=True),
        fromfile="Text A", tofile="Text B", lineterm="",
    ))
    if not diff:
        return "✅ Texts are identical."
    result = "".join(diff[:80])
    if len(diff) > 80:
        result += f"\n… ({len(diff) - 80} more lines)"
    return result


def regex_test(pattern: str, text: str) -> str:
    try:
        rx = re.compile(pattern)
        matches = list(rx.finditer(text))
        if not matches:
            return f"No matches for `{pattern}`."
        lines = [f"Found {len(matches)} match(es):"]
        for i, m in enumerate(matches[:10], 1):
            lines.append(f"  {i}. `{m.group()}` at [{m.start()}:{m.end()}]")
        return "\n".join(lines)
    except re.error as exc:
        return f"❌ Regex error: {exc}"


# ── Strings & Menu ────────────────────────────────────────────────────────────

WELCOME_MESSAGE = (
    "```\n"
    "┌───────────────────────────────┐\n"
    "│    B A N G A L I · I C O N    │\n"
    "└───────────────────────────────┘\n"
    "```\n"
    "⚡ *@BangaliIconBot* — all modules loaded\n\n"
    "🔗 Send a *Lummi.ai* or *Hugeicons* link → pick a size → instant asset\n"
    "🖼 Send a *photo* → resize/aspect ratio, then 38 real-time visual effects\n"
    "🎬 Send a *YouTube / X / Facebook* video link → video or audio download\n"
    "😄 Send a *sticker or GIF* → extract or convert\n"
    "🛡 `/FiverMessage` → sanitize with AI-sign detection\n"
    "🎨 `/colortools` → colour conversion & CSS helpers\n"
    "🔧 `/devtools` → JSON, UUID, hash & more\n\n"
    "_Tap_ `/menu` _anytime · /cancel to stop a task_\n\n"
    "✦ Designed & developed by *@YA\\_Rabbu*"
)

MENU_INTRO = "🧰 *Select a tool*"


# ── Keyboard builders ─────────────────────────────────────────────────────────

def build_main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎨 Image Effects",    callback_data="menu|effects"),
            InlineKeyboardButton("🧹 Remove BG",        callback_data="menu|bgremove"),
        ],
        [
            InlineKeyboardButton("📐 Resize / Ratio",   callback_data="menu|resizetool"),
            InlineKeyboardButton("📋 Copy Image",       callback_data="menu|copyimage"),
        ],
        [
            InlineKeyboardButton("😄 Sticker → PNG",    callback_data="menu|sticker2png"),
            InlineKeyboardButton("🎞 GIF → Frames",     callback_data="menu|gif2frames"),
        ],
        [
            InlineKeyboardButton("🛡 Fiver Sanitizer",  callback_data="menu|fiversanitize"),
            InlineKeyboardButton("🌐 Translate",        callback_data="menu|translate"),
        ],
        [
            InlineKeyboardButton("💧 Watermark",        callback_data="menu|watermark"),
            InlineKeyboardButton("📉 Compress",         callback_data="menu|compress"),
        ],
        [
            InlineKeyboardButton("🎬 Video/Audio DL",   callback_data="menu|mediadownload"),
            InlineKeyboardButton("🔳 QR Generate",      callback_data="menu|qrgen"),
        ],
        [
            InlineKeyboardButton("🔍 QR Scan",          callback_data="menu|qrscan"),
            InlineKeyboardButton("🔑 Password Gen",     callback_data="menu|genpass"),
        ],
        [
            InlineKeyboardButton("📚 My Words",         callback_data="menu|mywords"),
            InlineKeyboardButton("🖼 Image Tools",      callback_data="cat|image"),
        ],
        [
            InlineKeyboardButton("📄 PDF Tools",        callback_data="cat|pdf"),
            InlineKeyboardButton("📝 Documents",        callback_data="cat|word"),
        ],
        [
            InlineKeyboardButton("📊 Spreadsheet",      callback_data="cat|sheet"),
            InlineKeyboardButton("📽 Presentation",     callback_data="cat|ppt"),
        ],
        [
            InlineKeyboardButton("✍️ Text & Markup",    callback_data="cat|text"),
            InlineKeyboardButton("🎨 Colour Tools",     callback_data="cat|color"),
        ],
        [
            InlineKeyboardButton("🔧 Dev Tools",        callback_data="cat|dev"),
            InlineKeyboardButton("📚 More →",           callback_data="cat|more"),
        ],
    ])


def build_category_keyboard(cat: str) -> InlineKeyboardMarkup:
    back = [InlineKeyboardButton("◀ Main Menu", callback_data="cat|back")]
    menus: dict[str, list] = {
        "image": [
            [InlineKeyboardButton("🖼→📄 Image → PDF",   callback_data="menu|img2pdf"),
             InlineKeyboardButton("📄→🖼 PDF → Images",  callback_data="menu|pdf2img")],
            [InlineKeyboardButton("🔁 JPEG → PNG",       callback_data="menu|jpg2png"),
             InlineKeyboardButton("🔁 PNG → JPEG",       callback_data="menu|png2jpg")],
            [InlineKeyboardButton("📐 Resize / Ratio",   callback_data="menu|resizetool"),
             InlineKeyboardButton("📋 Copy",             callback_data="menu|copyimage")],
            [InlineKeyboardButton("💧 Watermark",        callback_data="menu|watermark"),
             InlineKeyboardButton("📉 Compress",         callback_data="menu|compress")],
            back,
        ],
        "pdf": [
            [InlineKeyboardButton("📄→📝 PDF → DOCX",   callback_data="menu|pdf2docx"),
             InlineKeyboardButton("📄→📃 PDF → TXT",    callback_data="menu|pdf2txt")],
            [InlineKeyboardButton("📄→🖼 PDF → Images", callback_data="menu|pdf2img"),
             InlineKeyboardButton("🖼→📄 Image → PDF",  callback_data="menu|img2pdf")],
            back,
        ],
        "word": [
            [InlineKeyboardButton("📝→📄 DOCX → PDF",   callback_data="menu|docx2pdf"),
             InlineKeyboardButton("📝→📃 DOCX → TXT",   callback_data="menu|docx2txt")],
            [InlineKeyboardButton("📝→🌐 DOCX → HTML",  callback_data="menu|docx2html"),
             InlineKeyboardButton("📄→📝 DOC → DOCX",   callback_data="menu|doc2docx")],
            [InlineKeyboardButton("📄→📝 ODT → DOCX",   callback_data="menu|odt2docx"),
             InlineKeyboardButton("📄→📄 ODT → PDF",    callback_data="menu|odt2pdf")],
            back,
        ],
        "sheet": [
            [InlineKeyboardButton("📊→📄 XLSX → PDF",   callback_data="menu|xlsx2pdf"),
             InlineKeyboardButton("📊→📃 XLSX → CSV",   callback_data="menu|xlsx2csv")],
            [InlineKeyboardButton("📃→📊 CSV → XLSX",   callback_data="menu|csv2xlsx"),
             InlineKeyboardButton("📊→📊 XLS → XLSX",   callback_data="menu|xls2xlsx")],
            back,
        ],
        "ppt": [
            [InlineKeyboardButton("📽→📄 PPTX → PDF",   callback_data="menu|pptx2pdf"),
             InlineKeyboardButton("📽→🖼 PPTX → Images",callback_data="menu|pptx2images")],
            [InlineKeyboardButton("📽→📽 PPT → PPTX",   callback_data="menu|ppt2pptx")],
            back,
        ],
        "text": [
            [InlineKeyboardButton("📃→📄 TXT → PDF",    callback_data="menu|txt2pdf"),
             InlineKeyboardButton("🌐→📄 HTML → PDF",   callback_data="menu|html2pdf")],
            [InlineKeyboardButton("📝→📄 MD → PDF",     callback_data="menu|md2pdf"),
             InlineKeyboardButton("📝→📃 MD → TXT",     callback_data="menu|md2txt")],
            [InlineKeyboardButton("📝→📝 MD → DOCX",    callback_data="menu|md2docx"),
             InlineKeyboardButton("📄→📃 RTF → TXT",    callback_data="menu|rtf2txt")],
            [InlineKeyboardButton("📄→📄 RTF → PDF",    callback_data="menu|rtf2pdf")],
            back,
        ],
        "color": [
            [InlineKeyboardButton("🔄 Color Convert",   callback_data="color|convert"),
             InlineKeyboardButton("✅ Contrast Check",  callback_data="color|contrast")],
            [InlineKeyboardButton("🌈 Gradient CSS",    callback_data="color|gradient"),
             InlineKeyboardButton("💧 Shadow CSS",      callback_data="color|shadow")],
            [InlineKeyboardButton("🖌 Tint & Shade",    callback_data="color|tintshade")],
            back,
        ],
        "dev": [
            [InlineKeyboardButton("📋 JSON Format",     callback_data="dev|json"),
             InlineKeyboardButton("🔑 Hash Gen",        callback_data="dev|hash")],
            [InlineKeyboardButton("🆔 UUID Gen",        callback_data="dev|uuid"),
             InlineKeyboardButton("⏰ Timestamp",       callback_data="dev|timestamp")],
            [InlineKeyboardButton("📝 Lorem Ipsum",     callback_data="dev|lorem"),
             InlineKeyboardButton("🔤 Case Convert",    callback_data="dev|case")],
            [InlineKeyboardButton("📐 px → rem",        callback_data="dev|pxrem"),
             InlineKeyboardButton("🔗 URL Encode",      callback_data="dev|urlencode")],
            [InlineKeyboardButton("🔗 URL Decode",      callback_data="dev|urldecode"),
             InlineKeyboardButton("💻 Base64 Encode",   callback_data="dev|b64enc")],
            [InlineKeyboardButton("💻 Base64 Decode",   callback_data="dev|b64dec"),
             InlineKeyboardButton("📊 Word Count",      callback_data="dev|wordcount")],
            [InlineKeyboardButton("🔀 Diff Checker",    callback_data="dev|diff"),
             InlineKeyboardButton("🔍 Regex Test",      callback_data="dev|regex")],
            back,
        ],
        "more": [
            [InlineKeyboardButton("📚→📄 EPUB → PDF",   callback_data="menu|epub2pdf")],
            back,
        ],
    }
    return InlineKeyboardMarkup(menus.get(cat, [back]))


def build_effect_toolbox_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎨 Pick an Effect",        callback_data=f"fxtool|effect|{token}")],
        [InlineKeyboardButton("📐 Aspect Ratio",           callback_data=f"fxtool|aspect|{token}"),
         InlineKeyboardButton("📏 Resize (custom)",        callback_data=f"fxtool|resize|{token}")],
        [InlineKeyboardButton("🎛 Customize Parameters",   callback_data=f"fxtool|params|{token}")],
        [InlineKeyboardButton("📋 Copy (as-is)",           callback_data=f"fxtool|copy|{token}"),
         InlineKeyboardButton("🔁 Convert Format",         callback_data=f"fxtool|convert|{token}")],
        [InlineKeyboardButton("📉 Compress",               callback_data=f"fxtool|compress|{token}")],
        [InlineKeyboardButton("✅ Apply & Continue",        callback_data=f"fxtool|go|{token}")],
    ])


def build_aspect_ratio_keyboard(token: str) -> InlineKeyboardMarkup:
    rows, row = [], []
    for key, label in ASPECT_RATIO_PRESETS:
        row.append(InlineKeyboardButton(label, callback_data=f"fxaspect|{key}|{token}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([
        InlineKeyboardButton("✂ Crop mode", callback_data=f"fxcropmode|crop|{token}"),
        InlineKeyboardButton("🖼 Pad mode",  callback_data=f"fxcropmode|pad|{token}"),
    ])
    rows.append([InlineKeyboardButton("◀ Back", callback_data=f"fxtoolback|{token}")])
    return InlineKeyboardMarkup(rows)


def build_resize_presets_keyboard(token: str) -> InlineKeyboardMarkup:
    presets = [("Small (480px)", 480), ("Medium (720px)", 720),
               ("Large (1080px)", 1080), ("XL (1600px)", 1600)]
    rows = [[InlineKeyboardButton(label, callback_data=f"fxresize|{px}|{token}")] for label, px in presets]
    rows.append([InlineKeyboardButton("✏️ Custom WxH (type it)", callback_data=f"fxresizecustom|{token}")])
    rows.append([InlineKeyboardButton("◀ Back", callback_data=f"fxtoolback|{token}")])
    return InlineKeyboardMarkup(rows)


def build_effect_categories_keyboard(token: str) -> InlineKeyboardMarkup:
    rows, row = [], []
    for cat_key in EFFECT_CATEGORY_ORDER:
        row.append(InlineKeyboardButton(EFFECT_CATEGORIES[cat_key], callback_data=f"fxcat|{cat_key}|{token}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("◀ Toolbox", callback_data=f"fxtoolback|{token}")])
    return InlineKeyboardMarkup(rows)


def build_effect_keyboard(category: str, token: str) -> InlineKeyboardMarkup:
    buttons, row = [], []
    for key, label, cat in EFFECTS:
        if cat != category:
            continue
        row.append(InlineKeyboardButton(label, callback_data=f"fx|{key}|{token}"))
        if len(row) == 2:
            buttons.append(row); row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("◀ Categories", callback_data=f"fxback|{token}")])
    return InlineKeyboardMarkup(buttons)


def build_param_keyboard(token: str, params: dict) -> InlineKeyboardMarkup:
    def row(label, key, step, fmt="{:.1f}"):
        val = params.get(key, 0)
        shown = fmt.format(val) if isinstance(val, float) else str(val)
        return [
            InlineKeyboardButton(f"{label}: {shown}", callback_data="fxnoop"),
            InlineKeyboardButton("➖", callback_data=f"fxparam|{key}|-{step}|{token}"),
            InlineKeyboardButton("➕", callback_data=f"fxparam|{key}|{step}|{token}"),
        ]
    return InlineKeyboardMarkup([
        row("Contrast",   "contrast", 0.1),
        row("Brightness", "brightness", 0.1),
        row("Grain",      "grainIntensity", 5, "{:.0f}"),
        row("Vignette",   "vignette", 0.1),
        row("Dot Pitch",  "dotPitch", 1, "{:.0f}"),
        [InlineKeyboardButton("♻ Reset to defaults", callback_data=f"fxparamreset|{token}")],
        [InlineKeyboardButton("◀ Toolbox", callback_data=f"fxtoolback|{token}")],
    ])


def build_translate_lang_keyboard() -> InlineKeyboardMarkup:
    buttons, row = [], []
    for code, label in TRANSLATE_LANGUAGES:
        row.append(InlineKeyboardButton(label, callback_data=f"trlang|{code}"))
        if len(row) == 2:
            buttons.append(row); row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(buttons)


def build_watermark_position_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("↖ Top-Left",     callback_data="wmpos|top-left"),
         InlineKeyboardButton("↗ Top-Right",    callback_data="wmpos|top-right")],
        [InlineKeyboardButton("↙ Bottom-Left",  callback_data="wmpos|bottom-left"),
         InlineKeyboardButton("↘ Bottom-Right", callback_data="wmpos|bottom-right")],
        [InlineKeyboardButton("⏺ Centre",       callback_data="wmpos|center")],
    ])


def build_genpass_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("8",  callback_data="genpass|8"),
         InlineKeyboardButton("12", callback_data="genpass|12"),
         InlineKeyboardButton("16", callback_data="genpass|16"),
         InlineKeyboardButton("20", callback_data="genpass|20")],
        [InlineKeyboardButton("🔢 4-digit PIN", callback_data="genpin|4")],
        [InlineKeyboardButton("🔢 6-digit PIN", callback_data="genpin|6")],
    ])


def build_limit_request_keyboard(chat_id: int, requested: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"limitreq|approve|{chat_id}|{requested}"),
        InlineKeyboardButton("❌ Deny",    callback_data=f"limitreq|deny|{chat_id}"),
    ]])


def build_color_input_keyboard(tool: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("◀ Colour Tools", callback_data="cat|color")],
    ])


def build_dev_input_keyboard(tool: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("◀ Dev Tools", callback_data="cat|dev")],
    ])


def build_case_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("camelCase",  callback_data="case|camel"),
         InlineKeyboardButton("PascalCase", callback_data="case|pascal")],
        [InlineKeyboardButton("snake_case", callback_data="case|snake"),
         InlineKeyboardButton("kebab-case", callback_data="case|kebab")],
        [InlineKeyboardButton("UPPERCASE",  callback_data="case|upper"),
         InlineKeyboardButton("lowercase",  callback_data="case|lower")],
        [InlineKeyboardButton("Title Case", callback_data="case|title")],
        [InlineKeyboardButton("◀ Dev Tools", callback_data="cat|dev")],
    ])


def build_icon_size_keyboard(token: str, allow_svg: bool) -> InlineKeyboardMarkup:
    row, rows = [], []
    for size in ICON_SIZE_PRESETS:
        row.append(InlineKeyboardButton(f"{size}px", callback_data=f"iconsz|{size}|{token}"))
        if len(row) == 3:
            rows.append(row); row = []
    if row:
        rows.append(row)
    if allow_svg:
        rows.append([InlineKeyboardButton("🔺 Original SVG (vector)", callback_data=f"iconsz|svg|{token}")])
    else:
        rows.append([InlineKeyboardButton("🔺 Original size", callback_data=f"iconsz|orig|{token}")])
    return InlineKeyboardMarkup(rows)


def build_media_format_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎬 Video (best, ≤720p)", callback_data=f"mediadl|video720|{token}")],
        [InlineKeyboardButton("🎬 Video (best, ≤1080p)", callback_data=f"mediadl|video1080|{token}")],
        [InlineKeyboardButton("🎵 Audio only (MP3)",     callback_data=f"mediadl|audio|{token}")],
    ])


# ── Utility helpers ───────────────────────────────────────────────────────────

def trim_url(url: str) -> str:
    return url.rstrip(".,!?;:)]}>\"'")


def truncate_caption(caption: str) -> str:
    return caption if len(caption) <= MAX_CAPTION_LENGTH else caption[:MAX_CAPTION_LENGTH - 3] + "..."


def detect_platform(url: str) -> str | None:
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    path = urlsplit(url).path
    if host == "lummi.ai" and re.match(r"^/(?:photo|illustration|3d)/[^/]+", path, re.IGNORECASE):
        return "lummi"
    if host == "hugeicons.com" and path.lower().startswith("/icon/"):
        return "hugeicons"
    return None


def markdown_v2_escape(text: str) -> str:
    return re.sub(r"([_\*\[\]\(\)~`>#+\-=|{}.!\\])", r"\\\1", text)


def markdown_code_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("`", "\\`")


def _user_hint(exc: Exception) -> str:
    msg = str(exc).lower()
    if "timeout" in msg:   return "⏱ The operation timed out. Try a smaller file or retry later."
    if "too large" in msg: return "📦 File exceeds the limit (20 MB download / 49 MB upload)."
    if "memory" in msg:    return "💾 Server ran low on memory. Try a smaller image."
    return f"❌ Something went wrong: {exc}"


def _esc_md(text: str) -> str:
    return re.sub(r"([_*`\[])", r"\\\1", text) if text else text


def _bar(count: int, max_count: int, width: int = 12) -> str:
    filled = max(1, round((count / max_count) * width)) if max_count > 0 and count > 0 else 0
    return "█" * filled + "░" * (width - filled)


async def safe_edit(message: Any, text: str, **kwargs: Any) -> None:
    try:
        await message.edit_text(text, **kwargs)
    except BadRequest as exc:
        if "message to edit not found" not in str(exc).lower():
            raise


async def safe_delete(message: Any) -> None:
    try:
        await message.delete()
    except BadRequest as exc:
        if "message to delete not found" not in str(exc).lower():
            raise


def parse_wh(text: str) -> tuple[int, int] | None:
    m = re.match(r"^\s*(\d{1,5})\s*[xX,]\s*(\d{1,5})\s*$", text.strip())
    if not m:
        return None
    w, h = int(m.group(1)), int(m.group(2))
    if w < 1 or h < 1 or w > 8000 or h > 8000:
        return None
    return w, h


# ── Token store (with TTL) ────────────────────────────────────────────────────

def _store_token(bot_data: dict, file_id: str) -> str:
    pending: dict[str, tuple[str, float]] = bot_data.setdefault("pending_files", {})
    now = time.monotonic()
    expired = [k for k, (_, ts) in pending.items() if now - ts > TOKEN_TTL_SECONDS]
    for k in expired:
        pending.pop(k, None)
    if len(pending) >= MAX_PENDING_FILES:
        oldest = min(pending, key=lambda k: pending[k][1])
        pending.pop(oldest, None)
    token = uuid.uuid4().hex[:10]
    pending[token] = (file_id, now)
    return token


def _get_file_id(bot_data: dict, token: str) -> str | None:
    entry = bot_data.get("pending_files", {}).get(token)
    if entry is None:
        return None
    file_id, ts = entry
    if time.monotonic() - ts > TOKEN_TTL_SECONDS:
        bot_data["pending_files"].pop(token, None)
        return None
    return file_id


def _drop_token(bot_data: dict, token: str) -> None:
    bot_data.get("pending_files", {}).pop(token, None)


def _store_side_data(bot_data: dict, key: str, token: str, value: Any) -> None:
    bot_data.setdefault(key, {})[token] = value


def _get_side_data(bot_data: dict, key: str, token: str) -> Any:
    return bot_data.get(key, {}).get(token)


def _drop_side_data(bot_data: dict, key: str, token: str) -> None:
    bot_data.get(key, {}).pop(token, None)


# ── Rate-limit guard ──────────────────────────────────────────────────────────

async def _check_heavy_rate_limit(update: Update, tool: str) -> bool:
    if tool not in storage.HEAVY_TOOLS:
        return True
    chat_id = update.effective_chat.id
    allowed, wait = await storage.check_rate_limit(chat_id, exempt=is_admin(update))
    if not allowed:
        used, limit = await storage.calls_used(chat_id)
        extra = " You have been fully blocked by an admin." if limit <= 0 else ""
        await update.effective_message.reply_text(
            f"⏳ Please wait ~{int(wait)}s before using another heavy tool "
            f"({used}/{limit} used in the last {storage.RATE_LIMIT_WINDOW_SECONDS}s).{extra}\n"
            f"Need a higher limit? Try `/requestlimit <number>`.",
            parse_mode="Markdown",
        )
        return False
    return True


async def _record(context: ContextTypes.DEFAULT_TYPE, update: Update, tool: str) -> None:
    chat_id  = update.effective_chat.id if update.effective_chat else None
    username = _username_of(update)
    await storage.record_usage(tool, chat_id=chat_id, username=username)


# ── Watermark bytes cache (in-memory, per bot session) ────────────────────────
_watermark_cache: dict[str, bytes] = {}


async def _get_watermark_bytes(context: ContextTypes.DEFAULT_TYPE, file_id: str) -> bytes:
    if file_id in _watermark_cache:
        return _watermark_cache[file_id]
    data = await _download_file(context, file_id)
    _watermark_cache[file_id] = data
    return data


# ── Image helpers ─────────────────────────────────────────────────────────────

def _get_image_dimensions(image_bytes: bytes) -> tuple[int, int]:
    try:
        from PIL import Image
        img = Image.open(BytesIO(image_bytes))
        w, h = img.size
        if w > IMAGE_MAX_DIM or h > IMAGE_MAX_DIM:
            ratio = min(IMAGE_MAX_DIM / w, IMAGE_MAX_DIM / h)
            w, h = int(w * ratio), int(h * ratio)
        return w, h
    except Exception:
        return 800, 600


async def _image_to_rgba_b64(image_bytes: bytes, width: int, height: int) -> str:
    from PIL import Image
    img = Image.open(BytesIO(image_bytes)).convert("RGBA").resize((width, height))
    return base64.b64encode(img.tobytes()).decode()


async def apply_effect_to_image(
    image_bytes: bytes,
    effect: str,
    width: int,
    height: int,
    params: dict | None = None,
) -> bytes:
    rgba_b64 = await _image_to_rgba_b64(image_bytes, width, height)
    payload  = json.dumps({
        "effect":          effect,
        "width":           width,
        "height":          height,
        "pixels_rgba_b64": rgba_b64,
        "params": params or DEFAULT_FX_PARAMS,
    })
    proc = await asyncio.create_subprocess_exec(
        sys.executable, ENGINE_SCRIPT,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(payload.encode()), timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"Engine error: {stderr.decode()[:300]}")
    result = json.loads(stdout.decode())
    if result.get("status") != "success":
        raise RuntimeError(result.get("message", "Unknown engine error"))
    bmp_bytes = base64.b64decode(result["bmp_data_url"].split(",", 1)[1])
    from PIL import Image
    png_buf = BytesIO()
    Image.open(BytesIO(bmp_bytes)).convert("RGBA").save(png_buf, format="PNG", optimize=True)
    return png_buf.getvalue()


async def sticker_to_png(sticker_bytes: bytes) -> bytes:
    from PIL import Image
    def _run():
        buf = BytesIO()
        Image.open(BytesIO(sticker_bytes)).convert("RGBA").save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    return await asyncio.to_thread(_run)


async def copy_image_bytes(image_bytes: bytes) -> bytes:
    return image_bytes


async def gif_to_frames(gif_bytes: bytes, max_frames: int = GIF_MAX_FRAMES) -> list[bytes]:
    from PIL import Image, ImageSequence
    def _run():
        img    = Image.open(BytesIO(gif_bytes))
        frames = list(ImageSequence.Iterator(img))
        total  = len(frames)
        if total == 0:
            raise ValueError("GIF contains no frames.")
        indices = list(range(total)) if total <= max_frames else [int(i * total / max_frames) for i in range(max_frames)]
        out = []
        for idx in indices:
            buf = BytesIO()
            frames[idx].convert("RGBA").save(buf, format="PNG", optimize=True)
            out.append(buf.getvalue())
        return out
    return await asyncio.to_thread(_run)


# ── Background removal ────────────────────────────────────────────────────────
_REMBG_SESSION = None


def _ensure_u2netp_model() -> str:
    if os.path.exists(U2NETP_MODEL_PATH) and os.path.getsize(U2NETP_MODEL_PATH) > 1_000_000:
        return U2NETP_MODEL_PATH
    with httpx.Client(timeout=60.0, follow_redirects=True) as c:
        r = c.get(U2NETP_MODEL_URL)
        r.raise_for_status()
        tmp = U2NETP_MODEL_PATH + ".part"
        with open(tmp, "wb") as f:
            f.write(r.content)
        os.replace(tmp, U2NETP_MODEL_PATH)
    return U2NETP_MODEL_PATH


def _get_rembg_session():
    global _REMBG_SESSION
    if _REMBG_SESSION is None:
        import onnxruntime as ort
        _REMBG_SESSION = ort.InferenceSession(_ensure_u2netp_model(), providers=["CPUExecutionProvider"])
    return _REMBG_SESSION


def _u2netp_predict_mask(session, img):
    import numpy as np
    from PIL import Image
    resized = img.resize((320, 320), Image.Resampling.LANCZOS)
    arr     = np.array(resized).astype(np.float32) / max(float(np.array(resized).max()), 1e-6)
    mean, std = (0.485, 0.456, 0.406), (0.229, 0.224, 0.225)
    normed  = np.zeros((320, 320, 3), dtype=np.float32)
    for c in range(3):
        normed[:, :, c] = (arr[:, :, c] - mean[c]) / std[c]
    inp  = np.expand_dims(normed.transpose(2, 0, 1), 0)
    pred = session.run(None, {session.get_inputs()[0].name: inp})[0][:, 0, :, :]
    ma, mi = float(pred.max()), float(pred.min())
    pred = np.squeeze((pred - mi) / max(ma - mi, 1e-6))
    mask = Image.fromarray((pred * 255).astype("uint8"), "L").resize(img.size, Image.Resampling.LANCZOS)
    from PIL import ImageFilter
    return mask.filter(ImageFilter.GaussianBlur(1.0))


async def remove_background(image_bytes: bytes) -> bytes:
    from PIL import Image, ImageOps
    def _run():
        img  = ImageOps.exif_transpose(Image.open(BytesIO(image_bytes))).convert("RGB")
        if max(img.size) > REMBG_MAX_DIMENSION:
            img.thumbnail((REMBG_MAX_DIMENSION, REMBG_MAX_DIMENSION), Image.LANCZOS)
        mask = _u2netp_predict_mask(_get_rembg_session(), img)
        out  = img.convert("RGBA")
        out.putalpha(mask)
        buf = BytesIO()
        out.save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    try:
        return await asyncio.wait_for(asyncio.to_thread(_run), timeout=REMBG_TIMEOUT_SECONDS)
    except asyncio.TimeoutError as exc:
        raise RuntimeError("Background removal timed out. The first run downloads a model — please retry.") from exc


# ── Format conversions ────────────────────────────────────────────────────────

async def convert_image_format(image_bytes: bytes, target_format: str) -> bytes:
    from PIL import Image, ImageOps
    def _run():
        img = ImageOps.exif_transpose(Image.open(BytesIO(image_bytes)))
        out = BytesIO()
        if target_format.upper() == "JPEG":
            img.convert("RGB").save(out, format="JPEG", quality=100, subsampling=0, optimize=True)
        else:
            img.convert("RGBA").save(out, format="PNG", optimize=True)
        return out.getvalue()
    return await asyncio.to_thread(_run)


async def image_to_pdf(image_bytes: bytes) -> bytes:
    from PIL import Image, ImageOps
    def _run():
        out = BytesIO()
        ImageOps.exif_transpose(Image.open(BytesIO(image_bytes))).convert("RGB").save(out, format="PDF", quality=100, resolution=300.0)
        return out.getvalue()
    return await asyncio.to_thread(_run)


async def pdf_to_images(pdf_bytes: bytes, max_pages: int = PDF2IMG_MAX_PAGES) -> list[bytes]:
    import fitz
    def _run():
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        try:
            return [p.get_pixmap(dpi=200).tobytes("png") for i, p in enumerate(doc) if i < max_pages]
        finally:
            doc.close()
    return await asyncio.to_thread(_run)


# ── Translation ───────────────────────────────────────────────────────────────

_RATE_LIMIT_MARKERS = ("429", "too many requests", "rate limit", "quota")


def _looks_rate_limited(exc: Exception) -> bool:
    return any(m in str(exc).lower() for m in _RATE_LIMIT_MARKERS)


async def _translate_google(text: str, target_lang: str) -> str:
    from deep_translator import GoogleTranslator
    return await asyncio.to_thread(lambda: GoogleTranslator(source="auto", target=target_lang).translate(text))


async def _translate_mymemory(text: str, target_lang: str) -> str:
    from deep_translator import MyMemoryTranslator
    return await asyncio.to_thread(lambda: MyMemoryTranslator(source="auto", target=target_lang).translate(text))


async def translate_text(text: str, target_lang: str) -> str:
    last_exc: Exception | None = None
    for attempt, delay in enumerate((0, 2, 4)):
        if delay:
            await asyncio.sleep(delay)
        try:
            return await _translate_google(text, target_lang)
        except Exception as exc:
            last_exc = exc
            if not _looks_rate_limited(exc):
                break
    try:
        if len(text) <= MYMEMORY_MAX_CHARS:
            return await _translate_mymemory(text, target_lang)
        chunks, current = [], ""
        for word in text.split(" "):
            candidate = f"{current} {word}".strip()
            if len(candidate) > MYMEMORY_MAX_CHARS and current:
                chunks.append(current); current = word
            else:
                current = candidate
        if current:
            chunks.append(current)
        return " ".join([await _translate_mymemory(c, target_lang) for c in chunks])
    except Exception as exc:
        raise RuntimeError("rate_limited") from (last_exc or exc)


# ── Markdown → plain text ─────────────────────────────────────────────────────

def markdown_to_plain_text(md: str) -> str:
    text = re.sub(r"!\[.*?\]\(.*?\)", "", md)
    text = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"(\*\*|__)(.*?)\1", r"\2", text, flags=re.DOTALL)
    text = re.sub(r"(?<!\*)\*(?!\*)(.*?)\*(?!\*)", r"\1", text)
    text = re.sub(r"(?<!_)_(?!_)(.*?)_(?!_)", r"\1", text)
    text = re.sub(r"`{1,3}(.*?)`{1,3}", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"^\s*>\s?", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ── Lummi & Hugeicons (asset fetch, size not yet applied) ─────────────────────

def find_lummi_cid(page_html: str, slug: str) -> str | None:
    soup    = BeautifulSoup(page_html, "html.parser")
    scripts = [s.string or s.get_text() for s in soup.find_all("script")]
    for script in ([s for s in scripts if slug in s] + scripts):
        for key in ("outpaintAssetPath", "path"):
            m = re.search(rf'{re.escape(key)}\\?":\\?"assets/({LUMMI_CID_RE.pattern})', script)
            if m: return m.group(1)
        m = LUMMI_CID_RE.search(script)
        if m: return m.group(0)
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        m = LUMMI_CID_RE.search(og["content"])
        if m: return m.group(0)
    return None


async def fetch_lummi_asset(url: str) -> dict[str, Any]:
    parsed     = urlsplit(url)
    slug_match = re.match(r"^/(?:photo|illustration|3d)/([^/?#]+)", parsed.path, re.IGNORECASE)
    if not slug_match:
        raise ValueError("Unsupported Lummi URL format.")
    slug   = unquote(slug_match.group(1))
    client = get_http_client()

    async def _fetch():
        page_r = await client.get(url)
        page_r.raise_for_status()
        cid = find_lummi_cid(page_r.text, slug)
        if not cid:
            raise ValueError("Could not locate a Lummi asset on that page.")
        direct_url = f"https://assets.lummi.ai/assets/{cid}"
        asset_r    = await client.get(direct_url, headers={**HEADERS, "Referer": "https://www.lummi.ai/"})
        asset_r.raise_for_status()
        return asset_r, cid, direct_url

    asset_r, cid, direct_url = await with_retry(_fetch)
    content = asset_r.content
    if not content:
        raise ValueError("The Lummi asset download was empty.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError("The Lummi file exceeds Telegram's upload limit (49 MB).")
    ct  = asset_r.headers.get("content-type", "image/jpeg").split(";", 1)[0].lower()
    ext = {"image/jpeg":"jpg","image/png":"png","image/webp":"webp","image/gif":"gif","image/tiff":"tiff"}.get(ct, "jpg")
    return {"bytes": content, "filename": f"lummi_{cid[:8]}.{ext}", "direct_url": direct_url, "size_mb": len(content)/(1024*1024)}


async def fetch_hugeicons_svg(url: str) -> dict[str, str]:
    m = re.search(r"hugeicons\.com/icon/([^?#]+)", url, re.IGNORECASE)
    if not m:
        raise ValueError("Invalid Hugeicons URL.")
    icon_name = unquote(m.group(1)).strip("/")
    sm        = re.search(r"[?&]style=([^&]+)", url, re.IGNORECASE)
    style     = unquote(sm.group(1)) if sm else "stroke-rounded"
    cdn_url   = f"https://cdn.hugeicons.com/icons/{icon_name}-{style}.svg?v=1.0.0"
    client    = get_http_client()

    async def _fetch():
        cdn_r = await client.get(cdn_url, headers={**HEADERS, "Referer": "https://hugeicons.com/"})
        if cdn_r.status_code == 200 and "<svg" in cdn_r.text.lower():
            return cdn_r.text.strip(), icon_name, style
        page_r = await client.get(url)
        page_r.raise_for_status()
        svg_m = re.search(r"<svg[\s\S]*?</svg>", page_r.text, re.IGNORECASE)
        if not svg_m:
            raise ValueError("SVG not found on Hugeicons page.")
        return svg_m.group(0).strip(), icon_name, style

    svg, icon_name, style = await with_retry(_fetch)
    if len(svg.encode()) > MAX_UPLOAD_BYTES:
        raise ValueError("SVG exceeds Telegram's upload limit.")
    return {"svg": svg, "icon_name": icon_name, "style": style}


def format_svg(svg: str) -> str:
    return re.sub(r"\s+", " ", svg).replace("> <", ">\n  <").strip()


async def render_svg_to_png(svg_text: str, size: int) -> bytes:
    import cairosvg
    return await asyncio.to_thread(
        cairosvg.svg2png, bytestring=svg_text.encode("utf-8"),
        output_width=size, output_height=size,
    )


# ── Shared image-tool runner ──────────────────────────────────────────────────

_SINGLE_IMAGE_TOOLS: dict[str, tuple[str, str, Callable]] = {
    "bgremove":    ("Removing background",  "background_removed.png", remove_background),
    "img2pdf":     ("Converting to PDF",    "image.pdf",              image_to_pdf),
    "jpg2png":     ("Converting to PNG",    "converted.png",          lambda b: convert_image_format(b, "PNG")),
    "png2jpg":     ("Converting to JPEG",   "converted.jpg",          lambda b: convert_image_format(b, "JPEG")),
    "sticker2png": ("Converting sticker",   "sticker.png",            sticker_to_png),
    "copyimage":   ("Copying file",         "copy.jpg",               copy_image_bytes),
}

_AWAITING_LABELS: dict[str, str] = {
    **{k: v[0] for k, v in _SINGLE_IMAGE_TOOLS.items()},
    "gif2frames":      "Extract GIF frames",
    "translate_text":  "Translate text",
    "fiver_info":      "Fiver order info",
    "fiver_body":      "Fiver message body",
    "watermark_setup": "Upload watermark logo",
    "watermark_apply": "Apply watermark to photo",
    "compress_image":  "Compress image",
    "qrscan":          "Scan QR code",
    "color_input":     "Colour tool input",
    "dev_input":       "Dev tool input",
    "resizetool":      "Resize / aspect ratio",
    "resize_custom_wh":"Custom width x height",
    "mediadownload":   "Video/audio link",
}


async def _download_file(context: ContextTypes.DEFAULT_TYPE, file_id: str) -> bytes:
    tg_file = await context.bot.get_file(file_id)
    if tg_file.file_size and tg_file.file_size > BOT_DOWNLOAD_LIMIT_BYTES:
        raise ValueError("File exceeds the 20 MB bot download limit.")
    buf = BytesIO()
    await tg_file.download_to_memory(buf)
    return buf.getvalue()


async def _run_image_tool(update: Update, context: ContextTypes.DEFAULT_TYPE, file_id: str, tool: str) -> None:
    if not await _check_heavy_rate_limit(update, tool):
        return
    label, filename, transform = _SINGLE_IMAGE_TOOLS[tool]
    status = await update.message.reply_text(f"⏳ {label}…")
    try:
        image_bytes = await _download_file(context, file_id)
        if tool in storage.HEAVY_TOOLS:
            async with storage.HEAVY_JOB_SEMAPHORE:
                out_bytes = await transform(image_bytes)
        else:
            out_bytes = await transform(image_bytes)
        doc = BytesIO(out_bytes); doc.name = filename
        await update.message.reply_document(document=doc, filename=filename)
        await safe_delete(status)
        await _record(context, update, tool)
    except Exception as exc:
        logger.warning("%s failed: %s", tool, exc)
        await safe_edit(status, _user_hint(exc))
        await notify_admin(context, tool, f"Tool `{tool}` failed for chat {update.effective_chat.id}: {exc}")


async def _run_gif_tool(update: Update, context: ContextTypes.DEFAULT_TYPE, file_id: str) -> None:
    status = await update.message.reply_text(f"⏳ Extracting GIF frames (up to {GIF_MAX_FRAMES})…")
    try:
        gif_bytes = await _download_file(context, file_id)
        frames    = await gif_to_frames(gif_bytes)
        if not frames:
            await safe_edit(status, "❌ No frames could be extracted from that GIF."); return
        await safe_delete(status)
        for i in range(0, len(frames), 10):
            await update.message.reply_media_group([InputMediaPhoto(BytesIO(f)) for f in frames[i:i+10]])
        await update.message.reply_text(f"✅ {len(frames)} frame(s) extracted.")
        await _record(context, update, "gif2frames")
    except Exception as exc:
        logger.warning("gif2frames failed: %s", exc)
        await safe_edit(status, _user_hint(exc))


async def _run_resize_tool(update: Update, context: ContextTypes.DEFAULT_TYPE, file_id: str,
                            mode: str, value: Any) -> None:
    if not await _check_heavy_rate_limit(update, "effects"):
        return
    status = await update.message.reply_text("⏳ Resizing…")
    try:
        raw = await _download_file(context, file_id)
        if mode == "width":
            from PIL import Image
            img = Image.open(BytesIO(raw))
            w0, h0 = img.size
            new_w = int(value)
            new_h = max(1, int(h0 * (new_w / w0)))
            out = await image_extra.resize_image(raw, new_w, new_h)
            fname = f"resized_{new_w}x{new_h}.png" if Image.open(BytesIO(out)).mode == "RGBA" else f"resized_{new_w}x{new_h}.jpg"
        elif mode == "wh":
            w, h = value
            out = await image_extra.resize_image(raw, w, h)
            fname = f"resized_{w}x{h}.jpg"
        else:  # ratio
            ratio_key, crop_mode = value
            rw, rh = _ratio_key_to_wh(ratio_key)
            out = await image_extra.crop_to_aspect(raw, rw, rh, crop_mode)
            fname = f"aspect_{ratio_key.replace(':','-')}.jpg"
        doc = BytesIO(out); doc.name = fname
        await update.message.reply_document(document=doc, filename=fname)
        await safe_delete(status)
        await _record(context, update, "resizetool")
    except Exception as exc:
        logger.warning("resizetool failed: %s", exc)
        await safe_edit(status, _user_hint(exc))


def _ratio_key_to_wh(ratio_key: str) -> tuple[int, int]:
    mapping = {
        "1:1": (1, 1), "4:5": (4, 5), "9:16": (9, 16),
        "16:9": (16, 9), "4:3": (4, 3), "3:2": (3, 2), "5:4": (5, 4), "3:4": (3, 4),
    }
    return mapping.get(ratio_key, (1, 1))


async def _deliver_icon_at_size(update: Update, context: ContextTypes.DEFAULT_TYPE, token: str, size_choice: str) -> None:
    kind = _get_side_data(context.bot_data, "icon_kind", token)  # "hugeicons" | "lummi"
    if kind is None:
        await update.callback_query.edit_message_text("❌ Request expired — please resend the link.")
        return

    if kind == "hugeicons":
        payload = _get_side_data(context.bot_data, "icon_payload", token)
        if size_choice == "svg":
            svg = format_svg(payload["svg"])
            filename = f"{payload['icon_name']}-{payload['style']}.svg"
            doc = BytesIO(svg.encode()); doc.name = filename
            await update.callback_query.message.reply_document(document=doc, filename=filename,
                                                                 caption=truncate_caption(f"{filename} — vector, any size."))
        else:
            if not await _check_heavy_rate_limit(update, "iconrender"):
                return
            size = int(size_choice)
            async with storage.HEAVY_JOB_SEMAPHORE:
                png_bytes = await render_svg_to_png(payload["svg"], size)
            filename = f"{payload['icon_name']}-{payload['style']}-{size}px.png"
            doc = BytesIO(png_bytes); doc.name = filename
            await update.callback_query.message.reply_document(document=doc, filename=filename,
                                                                 caption=truncate_caption(f"✅ {filename} ({size}×{size}px)"))
        await _record(context, update, "hugeicons")

    else:  # lummi
        payload = _get_side_data(context.bot_data, "icon_payload", token)
        if size_choice == "orig":
            doc = BytesIO(payload["bytes"]); doc.name = payload["filename"]
            await update.callback_query.message.reply_document(document=doc, filename=payload["filename"],
                                                                 caption=truncate_caption(f"✅ {payload['size_mb']:.2f} MB — original size"))
        else:
            if not await _check_heavy_rate_limit(update, "iconrender"):
                return
            size = int(size_choice)
            async with storage.HEAVY_JOB_SEMAPHORE:
                out = await image_extra.resize_image(payload["bytes"], size, size)
            filename = f"lummi_{size}px.png"
            doc = BytesIO(out); doc.name = filename
            await update.callback_query.message.reply_document(document=doc, filename=filename,
                                                                 caption=truncate_caption(f"✅ {filename} ({size}×{size}px)"))
        await _record(context, update, "lummi")

    _drop_side_data(context.bot_data, "icon_kind", token)
    _drop_side_data(context.bot_data, "icon_payload", token)


async def _run_media_download(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str, fmt: str) -> None:
    if not await _check_heavy_rate_limit(update, "mediadownload"):
        return
    status = await update.effective_message.reply_text("⏳ Fetching media (this can take up to a couple of minutes)…")
    try:
        async with storage.HEAVY_JOB_SEMAPHORE:
            if fmt == "video720":
                data, title = await media_downloader.download_video(url, max_height=720)
                fname = f"{title[:60]}.mp4".replace("/", "_")
            elif fmt == "video1080":
                data, title = await media_downloader.download_video(url, max_height=1080)
                fname = f"{title[:60]}.mp4".replace("/", "_")
            else:
                data, title = await media_downloader.download_audio(url)
                fname = f"{title[:60]}.mp3".replace("/", "_")
        if len(data) > MAX_UPLOAD_BYTES:
            await safe_edit(status, "📦 The downloaded file exceeds Telegram's 49 MB upload limit. "
                                     "Try a shorter clip or the audio-only option.")
            return
        doc = BytesIO(data); doc.name = fname
        await update.effective_message.reply_document(document=doc, filename=fname,
                                                        caption=truncate_caption(f"✅ {title}"))
        await safe_delete(status)
        await _record(context, update, "mediadownload")
    except Exception as exc:
        logger.warning("mediadownload failed: %s", exc)
        await safe_edit(status, _user_hint(exc))
        await notify_admin(context, "mediadownload", f"mediadownload failed for `{url}`: {exc}")


# ── Doc converters (optional) ─────────────────────────────────────────────────

try:
    from doc_converters import DOC_TOOLS as _DOC_TOOLS
    from doc_converters import pptx_to_images as _pptx_to_images
    _DOC_AVAILABLE = True
except ImportError:
    _DOC_TOOLS = {}; _DOC_AVAILABLE = False
    logger.warning("doc_converters.py not found — document conversion disabled.")

_DOC_ACCEPTS: dict[str, tuple[list[str], list[str]]] = {
    "pdf2docx":    (["application/pdf"],                                        [".pdf"]),
    "pdf2txt":     (["application/pdf"],                                        [".pdf"]),
    "pdf2img":     (["application/pdf"],                                        [".pdf"]),
    "docx2pdf":    (["application/vnd.openxmlformats"],                         [".docx"]),
    "docx2txt":    (["application/vnd.openxmlformats"],                         [".docx"]),
    "docx2html":   (["application/vnd.openxmlformats"],                         [".docx"]),
    "doc2docx":    (["application/msword"],                                     [".doc"]),
    "xlsx2pdf":    (["application/vnd.openxmlformats","application/vnd.ms-excel"],[".xlsx"]),
    "xlsx2csv":    (["application/vnd.openxmlformats","application/vnd.ms-excel"],[".xlsx"]),
    "xls2xlsx":    (["application/vnd.ms-excel"],                               [".xls"]),
    "csv2xlsx":    (["text/csv","text/plain"],                                  [".csv"]),
    "pptx2pdf":    (["application/vnd.openxmlformats"],                         [".pptx"]),
    "pptx2images": (["application/vnd.openxmlformats"],                         [".pptx"]),
    "ppt2pptx":    (["application/vnd.ms-powerpoint"],                          [".ppt"]),
    "txt2pdf":     (["text/plain"],                                             [".txt"]),
    "html2pdf":    (["text/html"],                                              [".html",".htm"]),
    "md2pdf":      (["text/plain","text/markdown"],                             [".md"]),
    "md2txt":      (["text/plain","text/markdown"],                             [".md"]),
    "md2docx":     (["text/plain","text/markdown"],                             [".md"]),
    "rtf2txt":     (["text/rtf","application/rtf"],                             [".rtf"]),
    "rtf2pdf":     (["text/rtf","application/rtf"],                             [".rtf"]),
    "odt2pdf":     (["application/vnd.oasis"],                                  [".odt"]),
    "odt2docx":    (["application/vnd.oasis"],                                  [".odt"]),
    "epub2pdf":    (["application/epub+zip"],                                   [".epub"]),
}


def _doc_accepts(tool: str, mime: str, filename: str) -> bool:
    if tool not in _DOC_ACCEPTS:
        return True
    mimes, exts = _DOC_ACCEPTS[tool]
    fn = filename.lower()
    return any(fn.endswith(e) for e in exts) or any(mime.startswith(m) for m in mimes)


async def _run_doc_tool(update: Update, context: ContextTypes.DEFAULT_TYPE, file_id: str, tool: str) -> None:
    if not await _check_heavy_rate_limit(update, tool):
        return
    if not _DOC_AVAILABLE:
        await update.message.reply_text("❌ Document tools are unavailable on this server."); return

    if tool == "pptx2images":
        status = await update.message.reply_text("⏳ Rendering slides…")
        try:
            raw   = await _download_file(context, file_id)
            async with storage.HEAVY_JOB_SEMAPHORE:
                pages = await _pptx_to_images(raw)
            if not pages:
                await safe_edit(status, "❌ No slides could be rendered."); return
            await safe_delete(status)
            for i in range(0, len(pages), 10):
                await update.message.reply_media_group([InputMediaPhoto(BytesIO(p)) for p in pages[i:i+10]])
            await update.message.reply_text(f"✅ {len(pages)} slide(s) rendered.")
            await _record(context, update, "pptx2images")
        except Exception as exc:
            logger.warning("pptx2images failed: %s", exc)
            await safe_edit(status, _user_hint(exc))
        return

    entry = _DOC_TOOLS.get(tool)
    if not entry:
        await update.message.reply_text("❌ Unknown document tool."); return
    label, _, handler = entry
    status = await update.message.reply_text(f"⏳ {label}…")
    try:
        raw = await _download_file(context, file_id)
        out_bytes, out_name = await handler(raw)
        doc = BytesIO(out_bytes); doc.name = out_name
        await update.message.reply_document(document=doc, filename=out_name)
        await safe_delete(status)
        await _record(context, update, tool)
    except Exception as exc:
        logger.warning("%s failed: %s", tool, exc)
        await safe_edit(status, _user_hint(exc))
        await notify_admin(context, tool, f"Doc tool `{tool}` failed: {exc}")


# ════════════════════════════════════════════════════════════════════════════
#  COMMAND HANDLERS
# ════════════════════════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return
    _remember_admin_chat_id(update)
    context.user_data.pop("awaiting", None)
    await update.message.reply_text(WELCOME_MESSAGE, parse_mode="Markdown", reply_markup=build_main_menu_keyboard())


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return
    await update.message.reply_text(MENU_INTRO, parse_mode="Markdown", reply_markup=build_main_menu_keyboard())


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return
    had = context.user_data.pop("awaiting", None)
    for key in (
        "target_lang", "compress_target_bytes", "watermark_position", "color_tool",
        "dev_tool", "dev_tool_step", "fiver_info", "diff_text_a", "case_input",
        "fx_token", "fx_params", "resize_wh_token", "media_url", "media_token",
    ):
        context.user_data.pop(key, None)
    await update.message.reply_text("✅ Cancelled." if had else "Nothing to cancel.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return
    await update.message.reply_text(
        "📖 *BangaliIcon Bot — Help*\n\n"
        "*Asset extraction:*\n"
        "Send a Lummi.ai or Hugeicons link → choose a size → instant download\n\n"
        "*Image effects & toolbox:*\n"
        "Send any photo → Aspect Ratio / Resize / Customize Parameters / "
        "Copy / Convert / Compress, or jump straight into an effect category\n\n"
        "*Video / Audio download:*\n"
        "Send a YouTube, X (twitter.com) or Facebook video link → choose "
        "Video (720p/1080p) or Audio-only (MP3). Only download content you "
        "have the right to download.\n\n"
        "*Fiver Sanitizer:*\n"
        "`/FiverMessage` → send order info, then the message text\n"
        "Order info format: `ClientName_OrderID_ProfileName_Amount`\n"
        "Output is a copy-ready template in a code block + a separate quality report.\n"
        "`/addword` · `/mywords` · `/delword` · `/resetwords`\n\n"
        "*🎨 Colour Tools (/menu → Colour Tools):*\n"
        "Color Convert · Contrast Check · Gradient · Shadow · Tint & Shade\n\n"
        "*🔧 Dev Tools (/menu → Dev Tools):*\n"
        "JSON · Hash · UUID · Timestamp · Lorem Ipsum\n"
        "Case Convert · px→rem · URL Encode/Decode\n"
        "Base64 · Word Count · Diff · Regex Tester\n\n"
        "*Other tools:*\n"
        "`/qr <text>` · `/genpass` · `/genpin`\n"
        "`/compress [size]` · `/watermark`\n\n"
        "*Admin:*\n"
        "`/stats` · `/useractivity` · `/setlimit` · `/blockuser`\n"
        "`/mylimit` · `/requestlimit <n>` · `/pendingrequests`",
        parse_mode="Markdown",
    )


# ── Fiver Sanitizer v3 ────────────────────────────────────────────────────────

async def fivermessage_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return
    context.user_data.pop("fiver_info", None)
    context.user_data["awaiting"] = "fiver_info"
    await update.message.reply_text(
        "🛡 প্রথমে অর্ডার তথ্য দিন এই ফরম্যাটে (একটাই লাইনে, `_` দিয়ে আলাদা করে):\n\n"
        f"`{FIVER_INFO_PATTERN_HINT}`\n\n"
        "উদাহরণ: `jmbattaglia_FO41C0CAC4D84_CustomerPortal_brainflux_1000`\n\n"
        "অথবা /cancel করে বাদ দিন。",
        parse_mode="Markdown",
    )


async def addword_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: `/addword <word> <replacement>`", parse_mode="Markdown"); return
    ok, info = await storage.add_word(update.effective_chat.id, context.args[0], " ".join(context.args[1:]))
    if not ok:
        await update.message.reply_text(f"❌ {info}"); return
    verb = "Updated" if info == "updated" else "Added"
    await update.message.reply_text(f"✅ {verb}: *{context.args[0]}* → *{' '.join(context.args[1:])}*", parse_mode="Markdown")


async def mywords_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return
    words = await storage.get_words(update.effective_chat.id)
    if not words:
        await update.message.reply_text("No custom words yet. Add with `/addword <word> <replacement>`.", parse_mode="Markdown"); return
    PAGE_SIZE = 25
    items = sorted(words.items())
    lines = []
    for pi, page in enumerate([items[i:i+PAGE_SIZE] for i in range(0, len(items), PAGE_SIZE)], 1):
        lines.append(f"*Page {pi}*")
        lines.extend(f"• `{w}` → `{r}`" for w, r in page)
    text = "\n".join(lines)
    if len(text) > 3800:
        text = text[:3800] + "\n… (truncated)"
    await update.message.reply_text(f"📚 *Custom words* ({len(words)}):\n\n{text}", parse_mode="Markdown")


async def delword_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return
    if not context.args:
        await update.message.reply_text("Usage: `/delword <word>`", parse_mode="Markdown"); return
    removed = await storage.del_word(update.effective_chat.id, context.args[0])
    await update.message.reply_text(
        f"🗑 Removed *{context.args[0]}*." if removed else f"⚠️ No custom mapping for *{context.args[0]}*.",
        parse_mode="Markdown",
    )


async def resetwords_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return
    await storage.reset_words(update.effective_chat.id)
    await update.message.reply_text("♻️ Custom words cleared.")


# ── Stats / Admin ─────────────────────────────────────────────────────────────

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return
    _remember_admin_chat_id(update)
    if not is_admin(update):
        await update.message.reply_text("🚫 Admin only."); return
    rows     = await storage.get_stats()
    activity = await storage.get_user_activity_all()
    if not rows:
        await update.message.reply_text("No usage recorded yet."); return
    top      = rows[:12]
    mx       = top[0][1] if top else 0
    tool_lines = [f"`{t:<18}` {_bar(c, mx)} {c}" for t, c in top]
    leaders  = sorted(activity.items(), key=lambda kv: sum(kv[1].get("counts",{}).values()), reverse=True)[:10]
    user_lines = [
        f"• {_esc_md('@'+e.get('username','')) if e.get('username') else 'id:'+cid} — `{cid}` — {sum(e.get('counts',{}).values())}"
        for cid, e in leaders
    ]
    await update.message.reply_text(
        f"📊 *Usage Dashboard* ({admin_role_label(update)})\n\n"
        f"*By tool:*\n" + "\n".join(tool_lines) +
        "\n\n*Top users:*\n" + ("\n".join(user_lines) or "_No per-user data yet._"),
        parse_mode="Markdown",
    )


def _resolve_target_chat_id(arg: str) -> int | None:
    arg = arg.strip().lstrip("@")
    if arg.lstrip("-").isdigit():
        return int(arg)
    return _admin_chat_ids.get(arg.lower())


async def _resolve_target_chat_id_async(arg: str) -> int | None:
    direct = _resolve_target_chat_id(arg)
    return direct if direct is not None else await storage.find_chat_id_by_username(arg)


async def useractivity_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return
    _remember_admin_chat_id(update)
    if not is_admin(update):
        await update.message.reply_text("🚫 Admin only."); return
    if not context.args:
        await update.message.reply_text("Usage: `/useractivity <chat_id or @username>`", parse_mode="Markdown"); return
    target = await _resolve_target_chat_id_async(context.args[0])
    if target is None:
        await update.message.reply_text("⚠️ User not found."); return
    entry = await storage.get_user_activity(target)
    if not entry:
        await update.message.reply_text(f"No activity for `{target}`.", parse_mode="Markdown"); return
    uname  = entry.get("username")
    label  = _esc_md(f"@{uname}") if uname else "(no username)"
    used, limit = await storage.calls_used(target)
    counts = entry.get("counts", {})
    count_lines = "\n".join(f"• `{t}` — {c}" for t, c in sorted(counts.items(), key=lambda kv: -kv[1]))
    recent = entry.get("log", [])[-10:]
    recent_lines = "\n".join(
        f"• {datetime.datetime.fromtimestamp(e['ts']).strftime('%Y-%m-%d %H:%M')} — `{e['tool']}`"
        for e in reversed(recent)
    )
    await update.message.reply_text(
        f"👤 *{label}* — `{target}`\n"
        f"Rate limit: {used}/{limit}\n\n"
        f"*Totals:*\n{count_lines or '_none_'}\n\n"
        f"*Last {len(recent)}:*\n{recent_lines or '_none_'}",
        parse_mode="Markdown",
    )


async def admins_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return
    _remember_admin_chat_id(update)
    lines = [f"*Your role:* {admin_role_label(update)}\n", "👑 *Super Admin*"]
    lines += [f"• {i['name']} — [Message]({i['telegram']}) — `{i['email']}`" for i in SUPER_ADMINS.values()]
    lines += ["\n🛡 *Admin*"]
    lines += [f"• {i['name']} — [Message]({i['telegram']}) — `{i['email']}`" for i in ADMINS.values()]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True)


async def setlimit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return
    _remember_admin_chat_id(update)
    if not is_admin(update):
        await update.message.reply_text("🚫 Admin only."); return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: `/setlimit <chat_id|@user> <n>`", parse_mode="Markdown"); return
    target = await _resolve_target_chat_id_async(context.args[0])
    if target is None:
        await update.message.reply_text("⚠️ User not found."); return
    try:
        n = int(context.args[1])
    except ValueError:
        await update.message.reply_text("⚠️ Limit must be a number."); return
    await storage.set_custom_limit(target, n)
    await update.message.reply_text(f"✅ `{target}` limited to *{n}* calls/60s.", parse_mode="Markdown")
    try:
        await context.bot.send_message(target, f"ℹ️ Your limit was updated to {n} calls/60s by an admin.")
    except Exception:
        pass


async def resetlimit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return
    _remember_admin_chat_id(update)
    if not is_admin(update):
        await update.message.reply_text("🚫 Admin only."); return
    if not context.args:
        await update.message.reply_text("Usage: `/resetlimit <chat_id|@user>`", parse_mode="Markdown"); return
    target = await _resolve_target_chat_id_async(context.args[0])
    if target is None:
        await update.message.reply_text("⚠️ User not found."); return
    await storage.set_custom_limit(target, None)
    await update.message.reply_text(f"♻️ `{target}` back to default limit.", parse_mode="Markdown")


async def mylimit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return
    if is_admin(update):
        await update.message.reply_text(f"👑 You are *{admin_role_label(update)}* — rate limits do not apply.", parse_mode="Markdown"); return
    used, limit = await storage.calls_used(update.effective_chat.id)
    await update.message.reply_text(
        f"📊 *{used}/{limit}* heavy-tool calls used in the last {storage.RATE_LIMIT_WINDOW_SECONDS}s.\n"
        f"Need more? `/requestlimit <number>`",
        parse_mode="Markdown",
    )


async def requestlimit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: `/requestlimit <number>`", parse_mode="Markdown"); return
    requested = min(int(context.args[0]), MAX_LIMIT_REQUEST_VALUE)
    chat_id   = update.effective_chat.id
    username  = _username_of(update)
    display   = _display_name(update, escape_markdown=True)
    await storage.add_limit_request(chat_id, username, requested)
    used, limit = await storage.calls_used(chat_id)
    await update.message.reply_text(
        f"📨 Request for *{requested}* calls/60s sent to admins. You'll be notified once reviewed.",
        parse_mode="Markdown",
    )
    caption = (
        f"🙋 *Limit increase request*\n"
        f"User: {display}\nchat\\_id: `{chat_id}`\n"
        f"Current: {limit} (used {used})\nRequested: *{requested}*"
    )
    photo_bytes = await _get_profile_photo_bytes(context, chat_id)
    keyboard = build_limit_request_keyboard(chat_id, requested)
    targets = set(_admin_chat_ids.values())
    if ADMIN_CHAT_ID:
        targets.add(ADMIN_CHAT_ID)
    for admin_chat_id in targets:
        try:
            if photo_bytes:
                await context.bot.send_photo(admin_chat_id, photo=BytesIO(photo_bytes), caption=caption,
                                              parse_mode="Markdown", reply_markup=keyboard)
            else:
                await context.bot.send_message(admin_chat_id, caption, parse_mode="Markdown", reply_markup=keyboard)
        except Exception:
            logger.exception("Failed to notify admin %s of limit request", admin_chat_id)


async def pendingrequests_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return
    _remember_admin_chat_id(update)
    if not is_admin(update):
        await update.message.reply_text("🚫 Admin only."); return
    reqs = await storage.get_limit_requests()
    if not reqs:
        await update.message.reply_text("✅ No pending requests."); return
    for cid, info in reqs.items():
        cid_int = int(cid)
        uname = info.get("username")
        label = _esc_md(f"@{uname}") if uname else f"id:{cid}"
        entry = await storage.get_user_activity(cid_int) or {}
        counts = entry.get("counts", {})
        top3 = sorted(counts.items(), key=lambda kv: -kv[1])[:5]
        history = "\n".join(f"• `{t}` — {c}" for t, c in top3) or "_no history yet_"
        used, limit = await storage.calls_used(cid_int)
        caption = (
            f"🙋 {label} — `{cid}`\n"
            f"Requested: *{info.get('requested')}* (current: {limit}, used {used})\n\n"
            f"*Recent activity (top tools):*\n{history}"
        )
        keyboard = build_limit_request_keyboard(cid_int, int(info.get("requested", 0)))
        photo_bytes = await _get_profile_photo_bytes(context, cid_int)
        if photo_bytes:
            await update.message.reply_photo(photo=BytesIO(photo_bytes), caption=caption,
                                              parse_mode="Markdown", reply_markup=keyboard)
        else:
            await update.message.reply_text(caption, parse_mode="Markdown", reply_markup=keyboard)


async def blockuser_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return
    _remember_admin_chat_id(update)
    if not is_super_admin(update):
        await update.message.reply_text("🚫 Super-admin only."); return
    if not context.args:
        await update.message.reply_text("Usage: `/blockuser <chat_id|@user>`", parse_mode="Markdown"); return
    target = await _resolve_target_chat_id_async(context.args[0])
    if target is None:
        await update.message.reply_text("⚠️ User not found."); return
    await storage.block_user(target)
    await update.message.reply_text(f"🚫 `{target}` fully blocked.", parse_mode="Markdown")


async def unblockuser_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return
    _remember_admin_chat_id(update)
    if not is_super_admin(update):
        await update.message.reply_text("🚫 Super-admin only."); return
    if not context.args:
        await update.message.reply_text("Usage: `/unblockuser <chat_id|@user>`", parse_mode="Markdown"); return
    target = await _resolve_target_chat_id_async(context.args[0])
    if target is None:
        await update.message.reply_text("⚠️ User not found."); return
    await storage.unblock_user(target)
    await update.message.reply_text(f"✅ `{target}` unblocked.", parse_mode="Markdown")


# ── QR, Password, Compress, Watermark ────────────────────────────────────────

async def qr_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return
    text = " ".join(context.args) if context.args else ""
    if not text.strip():
        await update.message.reply_text("Usage: `/qr <text or link>`", parse_mode="Markdown"); return
    status = await update.message.reply_text("⏳ Generating QR code…")
    try:
        png = await qr_tools.generate_qr(text.strip())
        doc = BytesIO(png); doc.name = "qrcode.png"
        await update.message.reply_photo(photo=doc, caption="✅ QR code ready.")
        await safe_delete(status)
        await _record(context, update, "qr_generate")
    except Exception as exc:
        await safe_edit(status, _user_hint(exc))


async def genpass_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return
    if not context.args:
        await update.message.reply_text("🔑 Choose a length:", reply_markup=build_genpass_keyboard()); return
    length     = password_tools.DEFAULT_LENGTH
    use_sym    = False
    no_ambig   = False
    for a in context.args:
        if a.lstrip("-").isdigit():   length  = int(a)
        elif a in ("--symbols","-s"): use_sym = True
        elif a in ("--no-ambiguous","-na"): no_ambig = True
    pw = password_tools.generate_password(length=length, use_symbols=use_sym, no_ambiguous=no_ambig)
    await update.message.reply_text(f"🔑 `{pw}`", parse_mode="Markdown")
    await _record(context, update, "genpass")


async def genpin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return
    length = int(context.args[0]) if context.args and context.args[0].isdigit() else 4
    await update.message.reply_text(f"🔢 `{password_tools.generate_pin(length)}`", parse_mode="Markdown")
    await _record(context, update, "genpin")


async def compress_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return
    target = DEFAULT_COMPRESS_TARGET
    if context.args:
        parsed = image_extra.parse_size_to_bytes(context.args[0])
        if parsed: target = parsed
    context.user_data["awaiting"]              = "compress_image"
    context.user_data["compress_target_bytes"] = target
    await update.message.reply_text(f"📉 Send the photo to compress (target: ~{target//1024} KB).")


async def watermark_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return
    context.user_data["awaiting"] = "watermark_setup"
    await update.message.reply_text(
        "💧 Send your *logo/signature image* first (PNG with transparency works best).",
        parse_mode="Markdown",
    )


async def download_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return
    if not context.args:
        await update.message.reply_text(
            "🎬 Usage: `/download <YouTube / X / Facebook link>`\n\n"
            "Or just paste the link directly in chat.",
            parse_mode="Markdown",
        ); return
    url = context.args[0]
    if not media_downloader.is_supported_url(url):
        await update.message.reply_text("⚠️ Only YouTube, X (twitter.com) and Facebook links are supported.")
        return
    token = _store_token(context.bot_data, url)
    await update.message.reply_text(
        "🎬 Choose a format:", reply_markup=build_media_format_keyboard(token),
    )


# ════════════════════════════════════════════════════════════════════════════
#  CALLBACK HANDLERS
# ════════════════════════════════════════════════════════════════════════════

async def handle_effect_toolbox_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query; await query.answer()
    try: _, action, token = query.data.split("|", 2)
    except ValueError: await query.edit_message_text("❌ Invalid."); return
    file_id = _get_file_id(context.bot_data, token)
    if not file_id:
        await query.edit_message_text("❌ Request expired — please resend the photo."); return

    if action == "effect":
        await query.edit_message_text("🎨 *Choose a category:*", parse_mode="Markdown",
                                       reply_markup=build_effect_categories_keyboard(token)); return
    if action == "aspect":
        await query.edit_message_text("📐 *Choose an aspect ratio:*", parse_mode="Markdown",
                                       reply_markup=build_aspect_ratio_keyboard(token)); return
    if action == "resize":
        await query.edit_message_text("📏 *Choose a size preset or type a custom one:*", parse_mode="Markdown",
                                       reply_markup=build_resize_presets_keyboard(token)); return
    if action == "params":
        params = _get_side_data(context.bot_data, "fx_params", token) or dict(DEFAULT_FX_PARAMS)
        _store_side_data(context.bot_data, "fx_params", token, params)
        await query.edit_message_text("🎛 *Customize parameters* (tap ➕/➖):", parse_mode="Markdown",
                                       reply_markup=build_param_keyboard(token, params)); return
    if action == "copy":
        await _run_image_tool(update, context, file_id, "copyimage")
        _drop_token(context.bot_data, token); return
    if action == "convert":
        await query.edit_message_text("🔁 Convert to:", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("→ PNG", callback_data=f"fxconvert|png|{token}"),
             InlineKeyboardButton("→ JPEG", callback_data=f"fxconvert|jpeg|{token}")],
            [InlineKeyboardButton("→ PDF", callback_data=f"fxconvert|pdf|{token}")],
            [InlineKeyboardButton("◀ Back", callback_data=f"fxtoolback|{token}")],
        ])); return
    if action == "compress":
        context.user_data["awaiting"]              = "compress_image"
        context.user_data["compress_target_bytes"] = DEFAULT_COMPRESS_TARGET
        context.user_data["fx_token"]               = token
        await query.edit_message_text(
            f"📉 Compressing this photo (default ~{DEFAULT_COMPRESS_TARGET//1024} KB)…",
            parse_mode="Markdown",
        )
        await _check_heavy_rate_limit(update, "compress")
        status = await query.message.reply_text("⏳ Compressing…")
        try:
            raw = await _download_file(context, file_id)
            async with storage.HEAVY_JOB_SEMAPHORE:
                out = await image_extra.compress_to_target(raw, DEFAULT_COMPRESS_TARGET)
            doc = BytesIO(out); doc.name = "compressed.jpg"
            await query.message.reply_document(document=doc, filename="compressed.jpg",
                caption=f"✅ {len(out)/1024:.0f} KB (target ~{DEFAULT_COMPRESS_TARGET//1024} KB)")
            await safe_delete(status)
            await _record(context, update, "compress")
        except Exception as exc:
            await safe_edit(status, _user_hint(exc))
        context.user_data.pop("awaiting", None)
        return
    if action == "go":
        await query.edit_message_text("🎨 *Choose a category:*", parse_mode="Markdown",
                                       reply_markup=build_effect_categories_keyboard(token)); return


async def handle_fx_convert_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query; await query.answer()
    try: _, fmt, token = query.data.split("|", 2)
    except ValueError: await query.edit_message_text("❌ Invalid."); return
    file_id = _get_file_id(context.bot_data, token)
    if not file_id:
        await query.edit_message_text("❌ Request expired — please resend the photo."); return
    await query.edit_message_text(f"⏳ Converting to {fmt.upper()}…")
    try:
        raw = await _download_file(context, file_id)
        if fmt == "pdf":
            out = await image_to_pdf(raw); fname = "image.pdf"
        elif fmt == "png":
            out = await convert_image_format(raw, "PNG"); fname = "converted.png"
        else:
            out = await convert_image_format(raw, "JPEG"); fname = "converted.jpg"
        doc = BytesIO(out); doc.name = fname
        await query.message.reply_document(document=doc, filename=fname)
        await _record(context, update, f"convert:{fmt}")
    except Exception as exc:
        await query.message.reply_text(_user_hint(exc))


async def handle_aspect_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query; await query.answer()
    try: _, ratio_key, token = query.data.split("|", 2)
    except ValueError: await query.edit_message_text("❌ Invalid."); return
    file_id = _get_file_id(context.bot_data, token)
    if not file_id:
        await query.edit_message_text("❌ Request expired — please resend the photo."); return
    if ratio_key == "original":
        await query.answer("Already original — pick another ratio, or go back.", show_alert=False)
        return
    crop_mode = _get_side_data(context.bot_data, "fx_cropmode", token) or "crop"
    await query.edit_message_text(f"⏳ Applying {ratio_key} ({crop_mode})…")
    await _run_resize_tool(update, context, file_id, "ratio", (ratio_key, crop_mode))


async def handle_cropmode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query; await query.answer()
    try: _, mode, token = query.data.split("|", 2)
    except ValueError: return
    _store_side_data(context.bot_data, "fx_cropmode", token, mode)
    await query.answer(f"Mode set: {mode}", show_alert=False)


async def handle_resize_preset_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query; await query.answer()
    try: _, px, token = query.data.split("|", 2)
    except ValueError: await query.edit_message_text("❌ Invalid."); return
    file_id = _get_file_id(context.bot_data, token)
    if not file_id:
        await query.edit_message_text("❌ Request expired — please resend the photo."); return
    await query.edit_message_text(f"⏳ Resizing to {px}px wide…")
    await _run_resize_tool(update, context, file_id, "width", int(px))


async def handle_resize_custom_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query; await query.answer()
    try: _, token = query.data.split("|", 1)
    except ValueError: return
    if not _get_file_id(context.bot_data, token):
        await query.edit_message_text("❌ Request expired — please resend the photo."); return
    context.user_data["awaiting"]        = "resize_custom_wh"
    context.user_data["resize_wh_token"] = token
    await query.edit_message_text("✏️ Type the target size as `WIDTHxHEIGHT`, e.g. `1080x1350`.", parse_mode="Markdown")


async def handle_fx_param_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    try: _, key, delta_str, token = query.data.split("|", 3)
    except ValueError: await query.answer(); return
    params = _get_side_data(context.bot_data, "fx_params", token) or dict(DEFAULT_FX_PARAMS)
    delta  = float(delta_str)
    bounds = {
        "contrast": (0.2, 3.0), "brightness": (0.2, 3.0),
        "grainIntensity": (0, 100), "vignette": (0, 1.0), "dotPitch": (2, 40),
    }
    lo, hi = bounds.get(key, (0, 999))
    new_val = params.get(key, 0) + delta
    new_val = max(lo, min(hi, new_val))
    params[key] = round(new_val) if key in ("grainIntensity", "dotPitch") else round(new_val, 2)
    _store_side_data(context.bot_data, "fx_params", token, params)
    await query.answer()
    await safe_edit(query.message, "🎛 *Customize parameters* (tap ➕/➖):", parse_mode="Markdown",
                     reply_markup=build_param_keyboard(token, params))


async def handle_fx_param_reset_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query; await query.answer("Reset to defaults.")
    try: _, token = query.data.split("|", 1)
    except ValueError: return
    _store_side_data(context.bot_data, "fx_params", token, dict(DEFAULT_FX_PARAMS))
    await safe_edit(query.message, "🎛 *Customize parameters* (tap ➕/➖):", parse_mode="Markdown",
                     reply_markup=build_param_keyboard(token, dict(DEFAULT_FX_PARAMS)))


async def handle_fx_noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()


async def handle_effect_category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query; await query.answer()
    try: _, category, token = query.data.split("|", 2)
    except ValueError: await query.edit_message_text("❌ Invalid."); return
    if not _get_file_id(context.bot_data, token):
        await query.edit_message_text("❌ Request expired — please resend the photo."); return
    await query.edit_message_text(
        f"🎨 *{EFFECT_CATEGORIES.get(category, category)}* — choose an effect:",
        parse_mode="Markdown", reply_markup=build_effect_keyboard(category, token),
    )


async def handle_effect_back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query; await query.answer()
    try: _, token = query.data.split("|", 1)
    except ValueError: await query.edit_message_text("❌ Invalid."); return
    if not _get_file_id(context.bot_data, token):
        await query.edit_message_text("❌ Request expired — please resend the photo."); return
    await query.edit_message_text("🎨 *Choose a category:*", parse_mode="Markdown", reply_markup=build_effect_categories_keyboard(token))


async def handle_fx_toolback_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query; await query.answer()
    try: _, token = query.data.split("|", 1)
    except ValueError: await query.edit_message_text("❌ Invalid."); return
    if not _get_file_id(context.bot_data, token):
        await query.edit_message_text("❌ Request expired — please resend the photo."); return
    await query.edit_message_text("🧰 *Image Toolbox* — pick an option:", parse_mode="Markdown",
                                   reply_markup=build_effect_toolbox_keyboard(token))


async def handle_effect_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query; await query.answer()
    try: _, effect_key, token = query.data.split("|", 2)
    except ValueError: await query.edit_message_text("❌ Invalid."); return
    file_id = _get_file_id(context.bot_data, token)
    if not file_id:
        await query.edit_message_text("❌ Request expired — please resend the photo."); return

    if not await _check_heavy_rate_limit(update, "effects"):
        return

    label = EFFECT_NAMES.get(effect_key, effect_key)
    status_msg = await query.edit_message_text(f"⏳ Applying *{label}*…", parse_mode="Markdown")
    try:
        await context.bot.send_chat_action(chat_id=query.message.chat_id, action=ChatAction.UPLOAD_PHOTO)
        image_bytes = await _download_file(context, file_id)
        w, h        = _get_image_dimensions(image_bytes)
        params      = _get_side_data(context.bot_data, "fx_params", token) or dict(DEFAULT_FX_PARAMS)
        async with storage.HEAVY_JOB_SEMAPHORE:
            png_bytes = await apply_effect_to_image(image_bytes, effect_key, w, h, params)
        doc = BytesIO(png_bytes); doc.name = f"{effect_key}.png"
        await query.message.reply_document(
            document=doc, filename=f"{effect_key}.png",
            caption=truncate_caption(f"✅ {label} applied"),
        )
        await safe_edit(status_msg, f"✅ *{label}* done!", parse_mode="Markdown")
        _drop_token(context.bot_data, token)
        _drop_side_data(context.bot_data, "fx_params", token)
        _drop_side_data(context.bot_data, "fx_cropmode", token)
        await _record(context, update, f"effect:{effect_key}")
    except asyncio.TimeoutError:
        await safe_edit(status_msg, "⏱ Timed out — try a smaller image.")
    except Exception as exc:
        logger.warning("Effect %s failed: %s", effect_key, exc)
        await safe_edit(status_msg, _user_hint(exc))
        await notify_admin(context, "effects", f"Effect `{effect_key}` failed: {exc}")


async def handle_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query; await query.answer()
    try: _, action = query.data.split("|", 1)
    except ValueError: await query.edit_message_text("❌ Invalid."); return

    _TOOL_PROMPTS: dict[str, tuple[str, str]] = {
        "effects":     ("effects",        "🎨 Send me a *photo* — I'll show the toolbox (effects, resize, aspect ratio, copy, convert, compress)."),
        "bgremove":    ("bgremove",        "🧹 Send me a *photo* — I'll remove the background."),
        "sticker2png": ("sticker2png",     "😄 Send me a *static sticker* to convert to PNG."),
        "gif2frames":  ("gif2frames",      f"🎞 Send me a *GIF* — I'll extract up to {GIF_MAX_FRAMES} frames."),
        "img2pdf":     ("img2pdf",         "🖼 Send me an *image* — I'll wrap it into a PDF."),
        "pdf2img":     ("pdf2img",         f"📄 Send me a *PDF* — I'll render up to {PDF2IMG_MAX_PAGES} pages."),
        "jpg2png":     ("jpg2png",         "🔁 Send me a *JPEG* as a file for best quality."),
        "png2jpg":     ("png2jpg",         "🔁 Send me a *PNG* as a file."),
        "resizetool":  ("effects",         "📐 Send me a *photo* — you'll get the Resize / Aspect Ratio toolbox."),
        "copyimage":   ("copyimage",       "📋 Send me a *photo* — I'll send it right back as a document (lossless copy)."),
        "fiversanitize":("fiver_info",     "🛡 প্রথমে অর্ডার তথ্য দিন: `ClientName_OrderID_ProfileName_Amount`"),
        "compress":    ("compress_image",  f"📉 Send me the photo to compress (default ~{DEFAULT_COMPRESS_TARGET//1024} KB)."),
        "qrscan":      ("qrscan",          "🔍 Send me a photo containing a QR code."),
        "mediadownload":("mediadownload",  "🎬 Send me a *YouTube, X (twitter.com) or Facebook* video link."),
        **{t: (t, f"Send me the file for `{t}`.") for t in _DOC_ACCEPTS},
    }

    if action == "translate":
        await query.edit_message_text("🌐 *Choose target language:*", parse_mode="Markdown", reply_markup=build_translate_lang_keyboard()); return
    if action == "watermark":
        context.user_data["awaiting"] = "watermark_setup"
        await query.edit_message_text("💧 Send your *logo/signature image* first.", parse_mode="Markdown"); return
    if action == "genpass":
        await query.edit_message_text("🔑 Choose a length:", reply_markup=build_genpass_keyboard()); return
    if action == "qrgen":
        await query.edit_message_text("🔳 Send `/qr <text or link>` to generate a QR code.", parse_mode="Markdown"); return
    if action == "mywords":
        words = await storage.get_words(query.message.chat_id)
        if not words:
            await query.edit_message_text("No custom words yet. Use `/addword <word> <replacement>`.", parse_mode="Markdown")
        else:
            preview = "\n".join(f"• `{w}` → `{r}`" for w, r in list(sorted(words.items()))[:20])
            await query.edit_message_text(
                f"📚 *Custom words* ({len(words)} total):\n\n{preview}\n\nUse /mywords for the full list.",
                parse_mode="Markdown",
            )
        return

    if action in _TOOL_PROMPTS:
        tool_key, msg = _TOOL_PROMPTS[action]
        context.user_data["awaiting"] = tool_key
        if tool_key == "fiver_info":
            context.user_data.pop("fiver_info", None)
        await query.edit_message_text(msg, parse_mode="Markdown")
    else:
        await query.edit_message_text("❌ Unknown option.")


async def handle_category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query; await query.answer()
    try: _, cat = query.data.split("|", 1)
    except ValueError: await query.edit_message_text("❌ Invalid."); return
    if cat == "back":
        await query.edit_message_text(MENU_INTRO, parse_mode="Markdown", reply_markup=build_main_menu_keyboard()); return
    labels = {
        "image":"🖼 Image Tools","pdf":"📄 PDF Tools","word":"📝 Documents",
        "sheet":"📊 Spreadsheet","ppt":"📽 Presentation","text":"✍️ Text & Markup",
        "color":"🎨 Colour Tools","dev":"🔧 Dev Tools","more":"📚 More",
    }
    await query.edit_message_text(
        f"*{labels.get(cat, cat.title())}* — choose a tool:",
        parse_mode="Markdown", reply_markup=build_category_keyboard(cat),
    )


async def handle_color_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query; await query.answer()
    try: _, tool = query.data.split("|", 1)
    except ValueError: await query.edit_message_text("❌ Invalid."); return

    prompts = {
        "convert":   "🔄 Send a colour in any format:\n`#FF5733`  or  `255 87 51`  or  `rgb(255,87,51)`",
        "contrast":  "✅ Send two colours separated by a newline:\n`#FFFFFF`\n`#333333`",
        "gradient":  "🌈 Send a hex colour to generate gradient CSS:\n`#FF5733`",
        "shadow":    "💧 Send a hex colour to generate shadow CSS:\n`#FF5733`",
        "tintshade": "🖌 Send a hex colour to generate tints & shades:\n`#FF5733`",
    }
    context.user_data["awaiting"]    = "color_input"
    context.user_data["color_tool"]  = tool
    await query.edit_message_text(prompts.get(tool, "Send colour input:"), parse_mode="Markdown", reply_markup=build_color_input_keyboard(tool))


async def handle_dev_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query; await query.answer()
    try: _, tool = query.data.split("|", 1)
    except ValueError: await query.edit_message_text("❌ Invalid."); return

    if tool == "uuid":
        await query.edit_message_text(f"🆔 {generate_uuid()}", parse_mode="Markdown", reply_markup=build_dev_input_keyboard(tool)); return
    if tool == "lorem":
        await query.edit_message_text(f"📝 {lorem_ipsum(50)}", reply_markup=build_dev_input_keyboard(tool)); return

    if tool == "case":
        context.user_data["awaiting"]  = "dev_input"
        context.user_data["dev_tool"]  = "case_text"
        await query.edit_message_text("🔤 Send the text to convert:", parse_mode="Markdown", reply_markup=build_dev_input_keyboard(tool)); return

    prompts = {
        "json":      "📋 Send the JSON to format/validate:",
        "hash":      "🔑 Send the text to hash:",
        "timestamp": "⏰ Send a Unix timestamp (`1700000000`) or ISO date (`2024-01-15T12:00:00`):",
        "pxrem":     "📐 Send a pixel value (e.g. `16` or `24px`):",
        "urlencode": "🔗 Send the text to URL-encode:",
        "urldecode": "🔗 Send the encoded URL to decode:",
        "b64enc":    "💻 Send the text to Base64-encode:",
        "b64dec":    "💻 Send the Base64 string to decode:",
        "wordcount": "📊 Send the text to count:",
        "diff":      "🔀 Send *Text A*, then on a new message *Text B*.\n\nFirst — send Text A:",
        "regex":     "🔍 Send `<pattern>\\n<text>` (pattern on first line, text below):",
    }
    context.user_data["awaiting"]  = "dev_input"
    context.user_data["dev_tool"]  = tool
    await query.edit_message_text(prompts.get(tool, "Send input:"), parse_mode="Markdown", reply_markup=build_dev_input_keyboard(tool))


async def handle_case_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query; await query.answer()
    try: _, mode = query.data.split("|", 1)
    except ValueError: await query.edit_message_text("❌ Invalid."); return
    text = context.user_data.pop("case_input", "")
    if not text:
        await query.edit_message_text("⚠️ No text stored. Please restart the tool."); return
    result = convert_case(text, mode)
    await query.edit_message_text(f"🔤 *{mode}:*\n\n`{result}`", parse_mode="Markdown", reply_markup=build_dev_input_keyboard("case"))


async def handle_translate_lang_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query; await query.answer()
    try: _, lang_code = query.data.split("|", 1)
    except ValueError: await query.edit_message_text("❌ Invalid."); return
    lang_label = next((l for c, l in TRANSLATE_LANGUAGES if c == lang_code), lang_code)
    context.user_data["awaiting"]    = "translate_text"
    context.user_data["target_lang"] = lang_code
    await query.edit_message_text(f"✏️ Send the text to translate to *{lang_label}*.", parse_mode="Markdown")


async def handle_watermark_position_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query; await query.answer()
    try: _, position = query.data.split("|", 1)
    except ValueError: await query.edit_message_text("❌ Invalid."); return
    context.user_data["watermark_position"] = position
    context.user_data["awaiting"]           = "watermark_apply"
    await query.edit_message_text(f"✅ Position set to *{position}*. Now send the photo to stamp.", parse_mode="Markdown")


async def handle_genpass_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query; await query.answer()
    try:
        _, length_str = query.data.split("|", 1)
        pw = password_tools.generate_password(length=int(length_str), use_symbols=True)
        await query.edit_message_text(f"🔑 `{pw}`", parse_mode="Markdown")
        await _record(context, update, "genpass")
    except Exception:
        await query.edit_message_text("❌ Failed to generate password.")


async def handle_genpin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query; await query.answer()
    try:
        _, length_str = query.data.split("|", 1)
        await query.edit_message_text(f"🔢 `{password_tools.generate_pin(int(length_str))}`", parse_mode="Markdown")
        await _record(context, update, "genpin")
    except Exception:
        await query.edit_message_text("❌ Failed to generate PIN.")


async def handle_limit_request_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query; await query.answer()
    if not is_admin(update):
        await query.answer("Admins only.", show_alert=True); return
    parts     = query.data.split("|")
    action    = parts[1]
    target_id = int(parts[2])
    if action == "approve":
        requested = int(parts[3])
        await storage.set_custom_limit(target_id, requested)
        await storage.remove_limit_request(target_id)
        try:
            await query.message.edit_caption(caption=f"✅ Approved: `{target_id}` → {requested} calls/60s.", parse_mode="Markdown")
        except Exception:
            await safe_edit(query.message, f"✅ Approved: `{target_id}` → {requested} calls/60s.", parse_mode="Markdown")
        try: await context.bot.send_message(target_id, f"✅ Your limit request was approved — now {requested} calls/60s.")
        except Exception: pass
    else:
        await storage.remove_limit_request(target_id)
        try:
            await query.message.edit_caption(caption=f"❌ Denied request from `{target_id}`.", parse_mode="Markdown")
        except Exception:
            await safe_edit(query.message, f"❌ Denied request from `{target_id}`.", parse_mode="Markdown")
        try: await context.bot.send_message(target_id, "❌ Your limit increase request was denied.")
        except Exception: pass


async def handle_icon_size_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query; await query.answer()
    try: _, size_choice, token = query.data.split("|", 2)
    except ValueError: await query.edit_message_text("❌ Invalid."); return
    await safe_edit(query.message, "⏳ Preparing your asset…")
    await _deliver_icon_at_size(update, context, token, size_choice)


async def handle_media_download_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query; await query.answer()
    try: _, fmt, token = query.data.split("|", 2)
    except ValueError: await query.edit_message_text("❌ Invalid."); return
    url = _get_file_id(context.bot_data, token)
    if not url:
        await query.edit_message_text("❌ Request expired — please resend the link."); return
    await safe_edit(query.message, "⏳ Starting download…")
    _drop_token(context.bot_data, token)
    await _run_media_download(update, context, url, fmt)


# ════════════════════════════════════════════════════════════════════════════
#  MESSAGE HANDLERS (photo / document / text / URL routing)
# ════════════════════════════════════════════════════════════════════════════

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.photo:
        return
    awaiting = context.user_data.get("awaiting")
    file_id = update.message.photo[-1].file_id

    if awaiting == "watermark_setup":
        await storage.set_watermark(update.effective_chat.id, file_id)
        context.user_data["awaiting"] = None
        await update.message.reply_text(
            "✅ Watermark saved. Now `/watermark` → pick a position → send the target photo.",
            parse_mode="Markdown",
        )
        return

    if awaiting == "watermark_apply":
        wm_file_id = await storage.get_watermark(update.effective_chat.id)
        if not wm_file_id:
            await update.message.reply_text("⚠️ No watermark saved yet. Send `/watermark` first.")
            return
        status = await update.message.reply_text("⏳ Applying watermark…")
        try:
            base_bytes = await _download_file(context, file_id)
            wm_bytes = await _get_watermark_bytes(context, wm_file_id)
            position = context.user_data.get("watermark_position", "bottom-right")
            out = await image_extra.apply_watermark(base_bytes, wm_bytes, position=position)
            doc = BytesIO(out); doc.name = "watermarked.jpg"
            await update.message.reply_document(document=doc, filename="watermarked.jpg")
            await safe_delete(status)
            await _record(context, update, "watermark")
        except Exception as exc:
            await safe_edit(status, _user_hint(exc))
        return

    if awaiting == "compress_image":
        target = context.user_data.get("compress_target_bytes", DEFAULT_COMPRESS_TARGET)
        if not await _check_heavy_rate_limit(update, "compress"):
            return
        status = await update.message.reply_text("⏳ Compressing…")
        try:
            raw = await _download_file(context, file_id)
            async with storage.HEAVY_JOB_SEMAPHORE:
                out = await image_extra.compress_to_target(raw, target)
            doc = BytesIO(out); doc.name = "compressed.jpg"
            await update.message.reply_document(document=doc, filename="compressed.jpg",
                caption=f"✅ {len(out)/1024:.0f} KB (target ~{target//1024} KB)")
            await safe_delete(status)
            await _record(context, update, "compress")
        except Exception as exc:
            await safe_edit(status, _user_hint(exc))
        context.user_data["awaiting"] = None
        return

    if awaiting == "qrscan":
        status = await update.message.reply_text("🔍 Scanning…")
        try:
            raw = await _download_file(context, file_id)
            results = await qr_tools.scan_qr(raw)
            if not results:
                await safe_edit(status, "❌ No QR code found.")
            else:
                await safe_edit(status, "✅ Found:\n" + "\n".join(f"`{r}`" for r in results), parse_mode="Markdown")
            await _record(context, update, "qrscan")
        except Exception as exc:
            await safe_edit(status, _user_hint(exc))
        context.user_data["awaiting"] = None
        return

    if awaiting in _SINGLE_IMAGE_TOOLS:
        await _run_image_tool(update, context, file_id, awaiting)
        context.user_data["awaiting"] = None
        return

    # Default: no pending intent → show the Toolbox
    token = _store_token(context.bot_data, file_id)
    await update.message.reply_text(
        "🧰 *Image Toolbox* — pick an option:", parse_mode="Markdown",
        reply_markup=build_effect_toolbox_keyboard(token),
    )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.document:
        return
    doc = update.message.document
    awaiting = context.user_data.get("awaiting")

    if awaiting in _SINGLE_IMAGE_TOOLS:
        await _run_image_tool(update, context, doc.file_id, awaiting)
        context.user_data["awaiting"] = None
        return
    if awaiting == "gif2frames":
        await _run_gif_tool(update, context, doc.file_id)
        context.user_data["awaiting"] = None
        return
    if awaiting in _DOC_ACCEPTS:
        if not _doc_accepts(awaiting, doc.mime_type or "", doc.file_name or ""):
            await update.message.reply_text("⚠️ That file type doesn't match this tool. Send the correct format or /cancel.")
            return
        await _run_doc_tool(update, context, doc.file_id, awaiting)
        context.user_data["awaiting"] = None
        return

    if (doc.mime_type or "").startswith("image/"):
        token = _store_token(context.bot_data, doc.file_id)
        await update.message.reply_text(
            "🧰 *Image Toolbox* — pick an option:", parse_mode="Markdown",
            reply_markup=build_effect_toolbox_keyboard(token),
        )
    else:
        await update.message.reply_text("📎 Got your file — use /menu to pick a tool for it, or send it after choosing a tool.")


async def handle_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.sticker:
        return
    if update.message.sticker.is_animated or update.message.sticker.is_video:
        await update.message.reply_text("⚠️ Only static stickers can be converted to PNG.")
        return
    await _run_image_tool(update, context, update.message.sticker.file_id, "sticker2png")


async def handle_gif(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.animation:
        return
    await _run_gif_tool(update, context, update.message.animation.file_id)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()
    awaiting = context.user_data.get("awaiting")

    media_match = MEDIA_URL_RE.search(text)
    if media_match and (awaiting in (None, "mediadownload")):
        url = trim_url(media_match.group(0))
        token = _store_token(context.bot_data, url)
        await update.message.reply_text("🎬 Choose a format:", reply_markup=build_media_format_keyboard(token))
        context.user_data["awaiting"] = None
        return

    lummi_match = LUMMI_URL_RE.search(text)
    huge_match = re.search(r"https?://(?:www\.)?hugeicons\.com/icon/[^\s<>]+", text, re.IGNORECASE)
    if lummi_match or huge_match:
        url = trim_url((lummi_match or huge_match).group(0))
        platform = detect_platform(url)
        status = await update.message.reply_text("⏳ Fetching asset…")
        try:
            if platform == "hugeicons":
                data = await fetch_hugeicons_svg(url)
                token = _store_token(context.bot_data, "icon")
                _store_side_data(context.bot_data, "icon_kind", token, "hugeicons")
                _store_side_data(context.bot_data, "icon_payload", token, data)
                await safe_edit(status, "🔺 *Choose a size:*", parse_mode="Markdown",
                                 reply_markup=build_icon_size_keyboard(token, allow_svg=True))
            elif platform == "lummi":
                data = await fetch_lummi_asset(url)
                token = _store_token(context.bot_data, "icon")
                _store_side_data(context.bot_data, "icon_kind", token, "lummi")
                _store_side_data(context.bot_data, "icon_payload", token, data)
                await safe_edit(status, "🔺 *Choose a size:*", parse_mode="Markdown",
                                 reply_markup=build_icon_size_keyboard(token, allow_svg=False))
            else:
                await safe_edit(status, "⚠️ Unsupported link format.")
        except Exception as exc:
            await safe_edit(status, _user_hint(exc))
        return

    if GREETING_RE.match(text) and awaiting is None:
        await update.message.reply_text(WELCOME_MESSAGE, parse_mode="Markdown", reply_markup=build_main_menu_keyboard())
        return

    if awaiting == "resize_custom_wh":
        wh = parse_wh(text)
        token = context.user_data.get("resize_wh_token")
        if not wh or not token:
            await update.message.reply_text("⚠️ Format must be `WIDTHxHEIGHT`, e.g. `1080x1350`.", parse_mode="Markdown")
            return
        file_id = _get_file_id(context.bot_data, token)
        if not file_id:
            await update.message.reply_text("❌ Request expired — please resend the photo.")
            context.user_data["awaiting"] = None
            return
        await _run_resize_tool(update, context, file_id, "wh", wh)
        context.user_data["awaiting"] = None
        return

    if awaiting == "fiver_info":
        info = parse_fiver_info(text)
        if not info:
            await update.message.reply_text(
                f"❌ ফরম্যাট মিলছে না। এভাবে দিন: `{FIVER_INFO_PATTERN_HINT}`", parse_mode="Markdown"); return
        context.user_data["fiver_info"] = info
        context.user_data["awaiting"] = "fiver_body"
        await update.message.reply_text("✅ পাওয়া গেছে। এখন মেসেজের বডি (body) পাঠান।")
        return

    if awaiting == "fiver_body":
        info = context.user_data.get("fiver_info")
        if not info:
            await update.message.reply_text("⚠️ Order info missing — `/FiverMessage` দিয়ে আবার শুরু করুন।")
            context.user_data["awaiting"] = None
            return
        custom_words = await storage.get_words(update.effective_chat.id)
        sanitized, changes, score = sanitize_fiver_text(text, custom_words)
        output = build_fiver_output(info, sanitized)
        report = format_sanitizer_report(changes, score)
        await update.message.reply_text(f"```\n{output}\n```", parse_mode="Markdown")
        await update.message.reply_text(report, parse_mode="Markdown")
        context.user_data["awaiting"] = None
        context.user_data.pop("fiver_info", None)
        await _record(context, update, "fiver_sanitize")
        return

    if awaiting == "translate_text":
        lang = context.user_data.get("target_lang", "en")
        if len(text) > TRANSLATE_MAX_CHARS:
            await update.message.reply_text(f"⚠️ Max {TRANSLATE_MAX_CHARS} characters."); return
        status = await update.message.reply_text("⏳ Translating…")
        try:
            translated = await translate_text(text, lang)
            await safe_edit(status, translated)
            await _record(context, update, "translate")
        except Exception:
            await safe_edit(status, "⏳ Translation service is rate-limited — please try again shortly.")
        context.user_data["awaiting"] = None
        return

    if awaiting == "color_input":
        tool = context.user_data.get("color_tool", "convert")
        await _handle_color_input(update, tool, text)
        context.user_data["awaiting"] = None
        return

    if awaiting == "dev_input":
        await _handle_dev_input(update, context, text)
        return

    if awaiting == "qrscan":
        await update.message.reply_text("📸 Please send the QR code as a *photo*.", parse_mode="Markdown")
        return

    await update.message.reply_text("🤔 Not sure what to do with that — try /menu.")


async def _handle_color_input(update: Update, tool: str, text: str) -> None:
    def _parse_color(s: str) -> tuple[int, int, int] | None:
        s = s.strip()
        m = re.match(r"rgb\(?\s*(\d+)[,\s]+(\d+)[,\s]+(\d+)\s*\)?", s, re.IGNORECASE)
        if m:
            return tuple(int(x) for x in m.groups())
        m = re.match(r"^(\d+)\s+(\d+)\s+(\d+)$", s)
        if m:
            return tuple(int(x) for x in m.groups())
        return hex_to_rgb(s)

    if tool == "contrast":
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        if len(lines) < 2:
            await update.message.reply_text("⚠️ Send two colours, one per line."); return
        c1, c2 = _parse_color(lines[0]), _parse_color(lines[1])
        if not c1 or not c2:
            await update.message.reply_text("⚠️ Couldn't parse one of those colours."); return
        ratio = wcag_contrast(*c1, *c2)
        verdict = "✅ Passes AA" if ratio >= 4.5 else ("⚠️ AA Large only" if ratio >= 3 else "❌ Fails AA")
        await update.message.reply_text(f"Contrast ratio: *{ratio}:1* — {verdict}", parse_mode="Markdown")
        return

    rgb = _parse_color(text)
    if not rgb:
        await update.message.reply_text("⚠️ Couldn't parse that colour."); return
    r, g, b = rgb
    if tool == "convert":
        h, s, l = rgb_to_hsl(r, g, b)
        c, m, y, k = rgb_to_cmyk(r, g, b)
        await update.message.reply_text(
            f"HEX: `{rgb_to_hex(r,g,b)}`\nRGB: `{r}, {g}, {b}`\n"
            f"HSL: `{h}, {s}%, {l}%`\nCMYK: `{c}, {m}, {y}, {k}`",
            parse_mode="Markdown",
        )
    elif tool == "gradient":
        await update.message.reply_text(f"```css\n{css_gradient(r,g,b)}\n```", parse_mode="Markdown")
    elif tool == "shadow":
        await update.message.reply_text(f"```css\n{css_shadow(r,g,b)}\n```", parse_mode="Markdown")
    elif tool == "tintshade":
        await update.message.reply_text(f"```\n{tint_shade(r,g,b)}\n```", parse_mode="Markdown")


async def _handle_dev_input(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    tool = context.user_data.get("dev_tool")
    if tool == "case_text":
        context.user_data["case_input"] = text
        context.user_data["awaiting"] = None
        await update.message.reply_text("🔤 Choose a case:", reply_markup=build_case_keyboard())
        return
    if tool == "diff" and "diff_text_a" not in context.user_data:
        context.user_data["diff_text_a"] = text
        await update.message.reply_text("Now send *Text B*:", parse_mode="Markdown")
        return

    handlers = {
        "json": lambda t: format_json(t),
        "hash": lambda t: hash_text(t),
        "timestamp": lambda t: convert_timestamp(t),
        "pxrem": lambda t: px_to_rem(float(re.sub(r"[^\d.]", "", t) or 0)),
        "urlencode": lambda t: url_encode(t),
        "urldecode": lambda t: url_decode(t),
        "b64enc": lambda t: base64_encode(t),
        "b64dec": lambda t: base64_decode(t),
        "wordcount": lambda t: word_count(t),
        "regex": lambda t: regex_test(*t.split("\n", 1)) if "\n" in t else "⚠️ Need pattern + text on separate lines.",
    }
    if tool == "diff":
        a = context.user_data.pop("diff_text_a", "")
        result = diff_texts(a, text)
        await update.message.reply_text(f"```diff\n{result}\n```", parse_mode="Markdown")
    elif tool in handlers:
        try:
            result = handlers[tool](text)
        except Exception as exc:
            result = f"❌ {exc}"
        await update.message.reply_text(f"`{result}`" if len(result) < 100 else f"```\n{result}\n```", parse_mode="Markdown")
    else:
        await update.message.reply_text("⚠️ Unknown tool.")
    context.user_data["awaiting"] = None


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled error", exc_info=context.error)
    await notify_admin(context, "unhandled", f"Unhandled error: {context.error}", throttle=True)


# ════════════════════════════════════════════════════════════════════════════
#  APP WIRING & ENTRY POINT
# ════════════════════════════════════════════════════════════════════════════

def build_application() -> Application:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("FiverMessage", fivermessage_command))
    app.add_handler(CommandHandler("addword", addword_command))
    app.add_handler(CommandHandler("mywords", mywords_command))
    app.add_handler(CommandHandler("delword", delword_command))
    app.add_handler(CommandHandler("resetwords", resetwords_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("useractivity", useractivity_command))
    app.add_handler(CommandHandler("admins", admins_command))
    app.add_handler(CommandHandler("setlimit", setlimit_command))
    app.add_handler(CommandHandler("resetlimit", resetlimit_command))
    app.add_handler(CommandHandler("mylimit", mylimit_command))
    app.add_handler(CommandHandler("requestlimit", requestlimit_command))
    app.add_handler(CommandHandler("pendingrequests", pendingrequests_command))
    app.add_handler(CommandHandler("blockuser", blockuser_command))
    app.add_handler(CommandHandler("unblockuser", unblockuser_command))
    app.add_handler(CommandHandler("qr", qr_command))
    app.add_handler(CommandHandler("genpass", genpass_command))
    app.add_handler(CommandHandler("genpin", genpin_command))
    app.add_handler(CommandHandler("compress", compress_command))
    app.add_handler(CommandHandler("watermark", watermark_command))
    app.add_handler(CommandHandler("download", download_command))

    app.add_handler(CallbackQueryHandler(handle_effect_toolbox_callback, pattern=r"^fxtool\|"))
    app.add_handler(CallbackQueryHandler(handle_fx_toolback_callback, pattern=r"^fxtoolback\|"))
    app.add_handler(CallbackQueryHandler(handle_fx_convert_callback, pattern=r"^fxconvert\|"))
    app.add_handler(CallbackQueryHandler(handle_aspect_callback, pattern=r"^fxaspect\|"))
    app.add_handler(CallbackQueryHandler(handle_cropmode_callback, pattern=r"^fxcropmode\|"))
    app.add_handler(CallbackQueryHandler(handle_resize_preset_callback, pattern=r"^fxresize\|"))
    app.add_handler(CallbackQueryHandler(handle_resize_custom_callback, pattern=r"^fxresizecustom\|"))
    app.add_handler(CallbackQueryHandler(handle_fx_param_callback, pattern=r"^fxparam\|"))
    app.add_handler(CallbackQueryHandler(handle_fx_param_reset_callback, pattern=r"^fxparamreset\|"))
    app.add_handler(CallbackQueryHandler(handle_fx_noop_callback, pattern=r"^fxnoop$"))
    app.add_handler(CallbackQueryHandler(handle_effect_category_callback, pattern=r"^fxcat\|"))
    app.add_handler(CallbackQueryHandler(handle_effect_back_callback, pattern=r"^fxback\|"))
    app.add_handler(CallbackQueryHandler(handle_effect_callback, pattern=r"^fx\|"))
    app.add_handler(CallbackQueryHandler(handle_icon_size_callback, pattern=r"^iconsz\|"))
    app.add_handler(CallbackQueryHandler(handle_media_download_callback, pattern=r"^mediadl\|"))
    app.add_handler(CallbackQueryHandler(handle_menu_callback, pattern=r"^menu\|"))
    app.add_handler(CallbackQueryHandler(handle_category_callback, pattern=r"^cat\|"))
    app.add_handler(CallbackQueryHandler(handle_color_callback, pattern=r"^color\|"))
    app.add_handler(CallbackQueryHandler(handle_case_callback, pattern=r"^case\|"))
    app.add_handler(CallbackQueryHandler(handle_dev_callback, pattern=r"^dev\|"))
    app.add_handler(CallbackQueryHandler(handle_translate_lang_callback, pattern=r"^trlang\|"))
    app.add_handler(CallbackQueryHandler(handle_watermark_position_callback, pattern=r"^wmpos\|"))
    app.add_handler(CallbackQueryHandler(handle_genpass_callback, pattern=r"^genpass\|"))
    app.add_handler(CallbackQueryHandler(handle_genpin_callback, pattern=r"^genpin\|"))
    app.add_handler(CallbackQueryHandler(handle_limit_request_callback, pattern=r"^limitreq\|"))

    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.Sticker.ALL, handle_sticker))
    app.add_handler(MessageHandler(filters.ANIMATION, handle_gif))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.add_error_handler(on_error)
    return app


def main() -> None:
    app = build_application()
    logger.info("BangaliIcon Bot starting…")

    port = int(os.environ.get("PORT", 8080))
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    render_url = os.environ.get("RENDER_EXTERNAL_URL")

    if not render_url:
        raise RuntimeError(
            "RENDER_EXTERNAL_URL is not set. This should be auto-injected by Render "
            "for web services — check your service type/config."
        )

    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=bot_token,
        webhook_url=f"{render_url}/{bot_token}",
    )


if __name__ == "__main__":
    main()
