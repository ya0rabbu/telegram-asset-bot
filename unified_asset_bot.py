"""
BangaliIcon Bot — v3.1
=======================
Single-file deployment.  All helper modules (storage, qr_tools,
password_tools, image_extra) are embedded as source strings and loaded
at import time via _load_embedded_module().

v3.1 changes
------------
  • New Fiver Sanitizer flow: collects
    ClientName_OrderID_ProfileName_Amount as one combined
    string, then the message body, and outputs a fixed template inside
    a copyable Markdown code block, followed by a separate quality report.
  • Markdown stripping (bold/italic/headers/links) now runs before
    word-replacement in the sanitizer.
  • Greeting normalization (Hi/Hey/Hello -> "Hello there,").
  • Closing normalization: if the tail of the message has no "thank you",
    a closing line is appended; if a sign-off (Best regards, etc.) is
    found, it is stripped and replaced with the closing line.
  • Old button-based "Fiver Report Card" flow removed (superseded).
  • /cancel now also clears fiver_info, diff_text_a, case_input.
  • Rate limiting note: "effects" heavy-tool check preserved as before.

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
    "watermark", "compress",
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
        was_Inbox = word in user_words
        if not was_Inbox and len(user_words) >= MAX_WORDS_PER_USER:
            return False, f"Limit reached ({MAX_WORDS_PER_USER} words). Remove one with /delword first."
        user_words[word] = replacement
        _save(CUSTOM_WORDS_PATH, data)
    return True, "Inboxd" if was_Inbox else "added"


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
'''

# ── Load embedded modules ─────────────────────────────────────────────────────
storage        = _load_embedded_module("storage",        _STORAGE_SOURCE)
qr_tools       = _load_embedded_module("qr_tools",       _QR_TOOLS_SOURCE)
password_tools = _load_embedded_module("password_tools", _PASSWORD_TOOLS_SOURCE)
image_extra    = _load_embedded_module("image_extra",    _IMAGE_EXTRA_SOURCE)


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
    Inbox,
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
REMBG_MAX_DIMENSION       = 1_500
REMBG_TIMEOUT_SECONDS     = 90
GIF_MAX_FRAMES            = 10
IMAGE_MAX_DIM             = 1_200          # was 400 — quality fix
DEFAULT_COMPRESS_TARGET   = 1 * 1024 * 1024
MAX_LIMIT_REQUEST_VALUE   = 100

REQUEST_TIMEOUT     = httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=10.0)
HTTP_RETRY_ATTEMPTS = 3
HTTP_RETRY_BACKOFF  = 1.5

ENGINE_SCRIPT    = os.path.join(os.path.dirname(__file__), "python_engine.py")
U2NETP_MODEL_URL = "https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2netp.onnx"
U2NETP_MODEL_PATH = os.path.join(tempfile.gettempdir(), "u2netp.onnx")


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

def _username_of(Inbox: Inbox) -> str | None:
    user = Inbox.effective_user
    return (user.username or "").lower() if user and user.username else None


def _display_name(Inbox: Inbox, escape_markdown: bool = False) -> str:
    user = Inbox.effective_user
    if user and user.username:
        label = f"@{user.username}"
    elif user and user.first_name:
        label = user.first_name
    else:
        label = f"id:{Inbox.effective_chat.id}"
    return _esc_md(label) if escape_markdown else label


def is_super_admin(Inbox: Inbox) -> bool:
    uname = _username_of(Inbox)
    return uname is not None and uname in SUPER_ADMINS


def is_admin(Inbox: Inbox) -> bool:
    uname = _username_of(Inbox)
    return uname is not None and (uname in SUPER_ADMINS or uname in ADMINS)


def admin_role_label(Inbox: Inbox) -> str:
    if is_super_admin(Inbox): return "Super Admin"
    if is_admin(Inbox):       return "Admin"
    return "User"


def _remember_admin_chat_id(Inbox: Inbox) -> None:
    uname = _username_of(Inbox)
    if uname and (uname in SUPER_ADMINS or uname in ADMINS) and Inbox.effective_chat:
        _admin_chat_ids[uname] = Inbox.effective_chat.id


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
DEFAULT_CLOSING_LINE = "Thank you again for your support, and I'll keep you Inboxd on the progress."

FIVER_TEMPLATE = (
    "==================================\n"
    "Status: Inbox (Inbox)\n"
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
    """Parses 'ClientName_OrderID_ProfileName_Amount'."""
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
    """
    Returns (sanitized_text, changes_list, quality_score_0_to_100).
    """
    changes: list[str] = []

    # 0. Strip markdown formatting first (bold/italic/headers/links/code)
    plain = markdown_to_plain_text(text)
    if plain != text:
        changes.append("Markdown formatting removed (bold/italic/headers/links)")
    text = plain

    # 1. Strip AI signs
    ai_stripped = _AI_SIGNS.sub("", text)
    stripped_count = len(text) - len(ai_stripped)
    if stripped_count:
        changes.append(f"AI signs removed ({stripped_count} character(s))")
    result = ai_stripped

    # 2. Word replacements
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

    # 3. Greeting normalize
    result, greet_note = normalize_greeting(result)
    if greet_note:
        changes.append(greet_note)

    # 4. Closing normalize
    result, close_note = normalize_closing(result)
    if close_note:
        changes.append(close_note)

    # 5. Quality score
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
    "🔗 Send a *Lummi.ai* or *Hugeicons* link → instant asset\n"
    "🖼 Send a *photo* → 38 real-time visual effects\n"
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
            InlineKeyboardButton("🔳 QR Generate",      callback_data="menu|qrgen"),
            InlineKeyboardButton("🔍 QR Scan",          callback_data="menu|qrscan"),
        ],
        [
            InlineKeyboardButton("🔑 Password Gen",     callback_data="menu|genpass"),
            InlineKeyboardButton("📚 My Words",         callback_data="menu|mywords"),
        ],
        [
            InlineKeyboardButton("🖼 Image Tools",      callback_data="cat|image"),
            InlineKeyboardButton("📄 PDF Tools",        callback_data="cat|pdf"),
        ],
        [
            InlineKeyboardButton("📝 Documents",        callback_data="cat|word"),
            InlineKeyboardButton("📊 Spreadsheet",      callback_data="cat|sheet"),
        ],
        [
            InlineKeyboardButton("📽 Presentation",     callback_data="cat|ppt"),
            InlineKeyboardButton("✍️ Text & Markup",    callback_data="cat|text"),
        ],
        [
            InlineKeyboardButton("🎨 Colour Tools",     callback_data="cat|color"),
            InlineKeyboardButton("🔧 Dev Tools",        callback_data="cat|dev"),
        ],
        [
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


def build_effect_categories_keyboard(token: str) -> InlineKeyboardMarkup:
    rows, row = [], []
    for cat_key in EFFECT_CATEGORY_ORDER:
        row.append(InlineKeyboardButton(EFFECT_CATEGORIES[cat_key], callback_data=f"fxcat|{cat_key}|{token}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
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


# ── Rate-limit guard ──────────────────────────────────────────────────────────

async def _check_heavy_rate_limit(Inbox: Inbox, tool: str) -> bool:
    if tool not in storage.HEAVY_TOOLS:
        return True
    chat_id = Inbox.effective_chat.id
    allowed, wait = await storage.check_rate_limit(chat_id, exempt=is_admin(Inbox))
    if not allowed:
        used, limit = await storage.calls_used(chat_id)
        extra = " You have been fully blocked by an admin." if limit <= 0 else ""
        await Inbox.effective_message.reply_text(
            f"⏳ Please wait ~{int(wait)}s before using another heavy tool "
            f"({used}/{limit} used in the last {storage.RATE_LIMIT_WINDOW_SECONDS}s).{extra}\n"
            f"Need a higher limit? Try `/requestlimit <number>`.",
            parse_mode="Markdown",
        )
        return False
    return True


async def _record(context: ContextTypes.DEFAULT_TYPE, Inbox: Inbox, tool: str) -> None:
    chat_id  = Inbox.effective_chat.id if Inbox.effective_chat else None
    username = _username_of(Inbox)
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
        "params": params or {
            "dotPitch":       8,
            "contrast":       1.2,
            "brightness":     1.0,
            "grainIntensity": 18,
            "vignette":       0.3,
        },
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
    return Image.fromarray((pred * 255).astype("uint8"), "L").resize(img.size, Image.Resampling.LANCZOS)


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


# ── Lummi & Hugeicons ─────────────────────────────────────────────────────────

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


# ── Shared image-tool runner ──────────────────────────────────────────────────

_SINGLE_IMAGE_TOOLS: dict[str, tuple[str, str, Callable]] = {
    "bgremove":    ("Removing background",  "background_removed.png", remove_background),
    "img2pdf":     ("Converting to PDF",    "image.pdf",              image_to_pdf),
    "jpg2png":     ("Converting to PNG",    "converted.png",          lambda b: convert_image_format(b, "PNG")),
    "png2jpg":     ("Converting to JPEG",   "converted.jpg",          lambda b: convert_image_format(b, "JPEG")),
    "sticker2png": ("Converting sticker",   "sticker.png",            sticker_to_png),
}

_AWAITING_LABELS: dict[str, str] = {
    **{k: v[0] for k, v in _SINGLE_IMAGE_TOOLS.items()},
    "gif2frames":     "Extract GIF frames",
    "translate_text": "Translate text",
    "fiver_info":     "Fiver order info",
    "fiver_body":     "Fiver message body",
    "watermark_setup":"Upload watermark logo",
    "watermark_apply":"Apply watermark to photo",
    "compress_image": "Compress image",
    "qrscan":         "Scan QR code",
    "color_input":    "Colour tool input",
    "dev_input":      "Dev tool input",
}


async def _download_file(context: ContextTypes.DEFAULT_TYPE, file_id: str) -> bytes:
    tg_file = await context.bot.get_file(file_id)
    if tg_file.file_size and tg_file.file_size > BOT_DOWNLOAD_LIMIT_BYTES:
        raise ValueError("File exceeds the 20 MB bot download limit.")
    buf = BytesIO()
    await tg_file.download_to_memory(buf)
    return buf.getvalue()


async def _run_image_tool(Inbox: Inbox, context: ContextTypes.DEFAULT_TYPE, file_id: str, tool: str) -> None:
    if not await _check_heavy_rate_limit(Inbox, tool):
        return
    label, filename, transform = _SINGLE_IMAGE_TOOLS[tool]
    status = await Inbox.message.reply_text(f"⏳ {label}…")
    try:
        image_bytes = await _download_file(context, file_id)
        if tool in storage.HEAVY_TOOLS:
            async with storage.HEAVY_JOB_SEMAPHORE:
                out_bytes = await transform(image_bytes)
        else:
            out_bytes = await transform(image_bytes)
        doc = BytesIO(out_bytes); doc.name = filename
        await Inbox.message.reply_document(document=doc, filename=filename)
        await safe_delete(status)
        await _record(context, Inbox, tool)
    except Exception as exc:
        logger.warning("%s failed: %s", tool, exc)
        await safe_edit(status, _user_hint(exc))
        await notify_admin(context, tool, f"Tool `{tool}` failed for chat {Inbox.effective_chat.id}: {exc}")


async def _run_gif_tool(Inbox: Inbox, context: ContextTypes.DEFAULT_TYPE, file_id: str) -> None:
    status = await Inbox.message.reply_text(f"⏳ Extracting GIF frames (up to {GIF_MAX_FRAMES})…")
    try:
        gif_bytes = await _download_file(context, file_id)
        frames    = await gif_to_frames(gif_bytes)
        if not frames:
            await safe_edit(status, "❌ No frames could be extracted from that GIF."); return
        await safe_delete(status)
        for i in range(0, len(frames), 10):
            await Inbox.message.reply_media_group([InputMediaPhoto(BytesIO(f)) for f in frames[i:i+10]])
        await Inbox.message.reply_text(f"✅ {len(frames)} frame(s) extracted.")
        await _record(context, Inbox, "gif2frames")
    except Exception as exc:
        logger.warning("gif2frames failed: %s", exc)
        await safe_edit(status, _user_hint(exc))


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


async def _run_doc_tool(Inbox: Inbox, context: ContextTypes.DEFAULT_TYPE, file_id: str, tool: str) -> None:
    if not await _check_heavy_rate_limit(Inbox, tool):
        return
    if not _DOC_AVAILABLE:
        await Inbox.message.reply_text("❌ Document tools are unavailable on this server."); return

    if tool == "pptx2images":
        status = await Inbox.message.reply_text("⏳ Rendering slides…")
        try:
            raw   = await _download_file(context, file_id)
            async with storage.HEAVY_JOB_SEMAPHORE:
                pages = await _pptx_to_images(raw)
            if not pages:
                await safe_edit(status, "❌ No slides could be rendered."); return
            await safe_delete(status)
            for i in range(0, len(pages), 10):
                await Inbox.message.reply_media_group([InputMediaPhoto(BytesIO(p)) for p in pages[i:i+10]])
            await Inbox.message.reply_text(f"✅ {len(pages)} slide(s) rendered.")
            await _record(context, Inbox, "pptx2images")
        except Exception as exc:
            logger.warning("pptx2images failed: %s", exc)
            await safe_edit(status, _user_hint(exc))
        return

    entry = _DOC_TOOLS.get(tool)
    if not entry:
        await Inbox.message.reply_text("❌ Unknown document tool."); return
    label, _, handler = entry
    status = await Inbox.message.reply_text(f"⏳ {label}…")
    try:
        raw = await _download_file(context, file_id)
        out_bytes, out_name = await handler(raw)
        doc = BytesIO(out_bytes); doc.name = out_name
        await Inbox.message.reply_document(document=doc, filename=out_name)
        await safe_delete(status)
        await _record(context, Inbox, tool)
    except Exception as exc:
        logger.warning("%s failed: %s", tool, exc)
        await safe_edit(status, _user_hint(exc))
        await notify_admin(context, tool, f"Doc tool `{tool}` failed: {exc}")


# ════════════════════════════════════════════════════════════════════════════
#  COMMAND HANDLERS
# ════════════════════════════════════════════════════════════════════════════

async def start(Inbox: Inbox, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not Inbox.message: return
    _remember_admin_chat_id(Inbox)
    context.user_data.pop("awaiting", None)
    await Inbox.message.reply_text(WELCOME_MESSAGE, parse_mode="Markdown", reply_markup=build_main_menu_keyboard())


async def menu_command(Inbox: Inbox, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not Inbox.message: return
    await Inbox.message.reply_text(MENU_INTRO, parse_mode="Markdown", reply_markup=build_main_menu_keyboard())


async def cancel_command(Inbox: Inbox, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not Inbox.message: return
    had = context.user_data.pop("awaiting", None)
    for key in (
        "target_lang", "compress_target_bytes", "watermark_position", "color_tool",
        "dev_tool", "dev_tool_step", "fiver_info", "diff_text_a", "case_input",
    ):
        context.user_data.pop(key, None)
    await Inbox.message.reply_text("✅ Cancelled." if had else "Nothing to cancel.")


async def help_command(Inbox: Inbox, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not Inbox.message: return
    await Inbox.message.reply_text(
        "📖 *BangaliIcon Bot — Help*\n\n"
        "*Asset extraction:*\n"
        "Send a Lummi.ai or Hugeicons link → instant download\n\n"
        "*Image effects:*\n"
        "Send any photo → choose category → choose effect\n\n"
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
        "`/mylimit` · `/requestlimit <n>`",
        parse_mode="Markdown",
    )


# ── Fiver Sanitizer v3 ────────────────────────────────────────────────────────

async def fivermessage_command(Inbox: Inbox, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not Inbox.message: return
    context.user_data.pop("fiver_info", None)
    context.user_data["awaiting"] = "fiver_info"
    await Inbox.message.reply_text(
        "🛡 প্রথমে অর্ডার তথ্য দিন এই ফরম্যাটে (একটাই লাইনে, `_` দিয়ে আলাদা করে):\n\n"
        f"`{FIVER_INFO_PATTERN_HINT}`\n\n"
        "উদাহরণ: `jmbattaglia_FO41C0CAC4D84_CustomerPortal_brainflux_1000`\n\n"
        "অথবা /cancel করে বাদ দিন।",
        parse_mode="Markdown",
    )


async def addword_command(Inbox: Inbox, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not Inbox.message: return
    if len(context.args) < 2:
        await Inbox.message.reply_text("Usage: `/addword <word> <replacement>`", parse_mode="Markdown"); return
    ok, info = await storage.add_word(Inbox.effective_chat.id, context.args[0], " ".join(context.args[1:]))
    if not ok:
        await Inbox.message.reply_text(f"❌ {info}"); return
    verb = "Inboxd" if info == "Inboxd" else "Added"
    await Inbox.message.reply_text(f"✅ {verb}: *{context.args[0]}* → *{' '.join(context.args[1:])}*", parse_mode="Markdown")


async def mywords_command(Inbox: Inbox, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not Inbox.message: return
    words = await storage.get_words(Inbox.effective_chat.id)
    if not words:
        await Inbox.message.reply_text("No custom words yet. Add with `/addword <word> <replacement>`.", parse_mode="Markdown"); return
    PAGE_SIZE = 25
    items = sorted(words.items())
    lines = []
    for pi, page in enumerate([items[i:i+PAGE_SIZE] for i in range(0, len(items), PAGE_SIZE)], 1):
        lines.append(f"*Page {pi}*")
        lines.extend(f"• `{w}` → `{r}`" for w, r in page)
    text = "\n".join(lines)
    if len(text) > 3800:
        text = text[:3800] + "\n… (truncated)"
    await Inbox.message.reply_text(f"📚 *Custom words* ({len(words)}):\n\n{text}", parse_mode="Markdown")


async def delword_command(Inbox: Inbox, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not Inbox.message: return
    if not context.args:
        await Inbox.message.reply_text("Usage: `/delword <word>`", parse_mode="Markdown"); return
    removed = await storage.del_word(Inbox.effective_chat.id, context.args[0])
    await Inbox.message.reply_text(
        f"🗑 Removed *{context.args[0]}*." if removed else f"⚠️ No custom mapping for *{context.args[0]}*.",
        parse_mode="Markdown",
    )


async def resetwords_command(Inbox: Inbox, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not Inbox.message: return
    await storage.reset_words(Inbox.effective_chat.id)
    await Inbox.message.reply_text("♻️ Custom words cleared.")


# ── Stats / Admin ─────────────────────────────────────────────────────────────

async def stats_command(Inbox: Inbox, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not Inbox.message: return
    _remember_admin_chat_id(Inbox)
    if not is_admin(Inbox):
        await Inbox.message.reply_text("🚫 Admin only."); return
    rows     = await storage.get_stats()
    activity = await storage.get_user_activity_all()
    if not rows:
        await Inbox.message.reply_text("No usage recorded yet."); return
    top      = rows[:12]
    mx       = top[0][1] if top else 0
    tool_lines = [f"`{t:<18}` {_bar(c, mx)} {c}" for t, c in top]
    leaders  = sorted(activity.items(), key=lambda kv: sum(kv[1].get("counts",{}).values()), reverse=True)[:10]
    user_lines = [
        f"• {_esc_md('@'+e.get('username','')) if e.get('username') else 'id:'+cid} — `{cid}` — {sum(e.get('counts',{}).values())}"
        for cid, e in leaders
    ]
    await Inbox.message.reply_text(
        f"📊 *Usage Dashboard* ({admin_role_label(Inbox)})\n\n"
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


async def useractivity_command(Inbox: Inbox, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not Inbox.message: return
    _remember_admin_chat_id(Inbox)
    if not is_admin(Inbox):
        await Inbox.message.reply_text("🚫 Admin only."); return
    if not context.args:
        await Inbox.message.reply_text("Usage: `/useractivity <chat_id or @username>`", parse_mode="Markdown"); return
    target = await _resolve_target_chat_id_async(context.args[0])
    if target is None:
        await Inbox.message.reply_text("⚠️ User not found."); return
    entry = await storage.get_user_activity(target)
    if not entry:
        await Inbox.message.reply_text(f"No activity for `{target}`.", parse_mode="Markdown"); return
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
    await Inbox.message.reply_text(
        f"👤 *{label}* — `{target}`\n"
        f"Rate limit: {used}/{limit}\n\n"
        f"*Totals:*\n{count_lines or '_none_'}\n\n"
        f"*Last {len(recent)}:*\n{recent_lines or '_none_'}",
        parse_mode="Markdown",
    )


async def admins_command(Inbox: Inbox, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not Inbox.message: return
    _remember_admin_chat_id(Inbox)
    lines = [f"*Your role:* {admin_role_label(Inbox)}\n", "👑 *Super Admin*"]
    lines += [f"• {i['name']} — [Message]({i['telegram']}) — `{i['email']}`" for i in SUPER_ADMINS.values()]
    lines += ["\n🛡 *Admin*"]
    lines += [f"• {i['name']} — [Message]({i['telegram']}) — `{i['email']}`" for i in ADMINS.values()]
    await Inbox.message.reply_text("\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True)


async def setlimit_command(Inbox: Inbox, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not Inbox.message: return
    _remember_admin_chat_id(Inbox)
    if not is_admin(Inbox):
        await Inbox.message.reply_text("🚫 Admin only."); return
    if len(context.args) < 2:
        await Inbox.message.reply_text("Usage: `/setlimit <chat_id|@user> <n>`", parse_mode="Markdown"); return
    target = await _resolve_target_chat_id_async(context.args[0])
    if target is None:
        await Inbox.message.reply_text("⚠️ User not found."); return
    try:
        n = int(context.args[1])
    except ValueError:
        await Inbox.message.reply_text("⚠️ Limit must be a number."); return
    await storage.set_custom_limit(target, n)
    await Inbox.message.reply_text(f"✅ `{target}` limited to *{n}* calls/60s.", parse_mode="Markdown")
    try:
        await context.bot.send_message(target, f"ℹ️ Your limit was Inboxd to {n} calls/60s by an admin.")
    except Exception:
        pass


async def resetlimit_command(Inbox: Inbox, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not Inbox.message: return
    _remember_admin_chat_id(Inbox)
    if not is_admin(Inbox):
        await Inbox.message.reply_text("🚫 Admin only."); return
    if not context.args:
        await Inbox.message.reply_text("Usage: `/resetlimit <chat_id|@user>`", parse_mode="Markdown"); return
    target = await _resolve_target_chat_id_async(context.args[0])
    if target is None:
        await Inbox.message.reply_text("⚠️ User not found."); return
    await storage.set_custom_limit(target, None)
    await Inbox.message.reply_text(f"♻️ `{target}` back to default limit.", parse_mode="Markdown")


async def mylimit_command(Inbox: Inbox, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not Inbox.message: return
    if is_admin(Inbox):
        await Inbox.message.reply_text(f"👑 You are *{admin_role_label(Inbox)}* — rate limits do not apply.", parse_mode="Markdown"); return
    used, limit = await storage.calls_used(Inbox.effective_chat.id)
    await Inbox.message.reply_text(
        f"📊 *{used}/{limit}* heavy-tool calls used in the last {storage.RATE_LIMIT_WINDOW_SECONDS}s.\n"
        f"Need more? `/requestlimit <number>`",
        parse_mode="Markdown",
    )


async def requestlimit_command(Inbox: Inbox, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not Inbox.message: return
    if not context.args or not context.args[0].isdigit():
        await Inbox.message.reply_text("Usage: `/requestlimit <number>`", parse_mode="Markdown"); return
    requested = min(int(context.args[0]), MAX_LIMIT_REQUEST_VALUE)
    chat_id   = Inbox.effective_chat.id
    username  = _username_of(Inbox)
    display   = _display_name(Inbox, escape_markdown=True)
    await storage.add_limit_request(chat_id, username, requested)
    used, limit = await storage.calls_used(chat_id)
    await Inbox.message.reply_text(
        f"📨 Request for *{requested}* calls/60s sent to admins. You'll be notified once reviewed.",
        parse_mode="Markdown",
    )
    await notify_admin(
        context,
        error_key=f"limitreq-{chat_id}",
        message=(
            f"🙋 *Limit increase request*\n"
            f"User: {display}\nchat\\_id: `{chat_id}`\n"
            f"Current: {limit} (used {used})\nRequested: *{requested}*"
        ),
        throttle=False,
        reply_markup=build_limit_request_keyboard(chat_id, requested),
    )


async def pendingrequests_command(Inbox: Inbox, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not Inbox.message: return
    _remember_admin_chat_id(Inbox)
    if not is_admin(Inbox):
        await Inbox.message.reply_text("🚫 Admin only."); return
    reqs = await storage.get_limit_requests()
    if not reqs:
        await Inbox.message.reply_text("✅ No pending requests."); return
    for cid, info in reqs.items():
        uname = info.get("username")
        label = _esc_md(f"@{uname}") if uname else f"id:{cid}"
        await Inbox.message.reply_text(
            f"🙋 {label} — `{cid}` — requested *{info.get('requested')}*",
            parse_mode="Markdown",
            reply_markup=build_limit_request_keyboard(int(cid), int(info.get("requested", 0))),
        )


async def blockuser_command(Inbox: Inbox, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not Inbox.message: return
    _remember_admin_chat_id(Inbox)
    if not is_super_admin(Inbox):
        await Inbox.message.reply_text("🚫 Super-admin only."); return
    if not context.args:
        await Inbox.message.reply_text("Usage: `/blockuser <chat_id|@user>`", parse_mode="Markdown"); return
    target = await _resolve_target_chat_id_async(context.args[0])
    if target is None:
        await Inbox.message.reply_text("⚠️ User not found."); return
    await storage.block_user(target)
    await Inbox.message.reply_text(f"🚫 `{target}` fully blocked.", parse_mode="Markdown")


async def unblockuser_command(Inbox: Inbox, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not Inbox.message: return
    _remember_admin_chat_id(Inbox)
    if not is_super_admin(Inbox):
        await Inbox.message.reply_text("🚫 Super-admin only."); return
    if not context.args:
        await Inbox.message.reply_text("Usage: `/unblockuser <chat_id|@user>`", parse_mode="Markdown"); return
    target = await _resolve_target_chat_id_async(context.args[0])
    if target is None:
        await Inbox.message.reply_text("⚠️ User not found."); return
    await storage.unblock_user(target)
    await Inbox.message.reply_text(f"✅ `{target}` unblocked.", parse_mode="Markdown")


# ── QR, Password, Compress, Watermark ────────────────────────────────────────

async def qr_command(Inbox: Inbox, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not Inbox.message: return
    text = " ".join(context.args) if context.args else ""
    if not text.strip():
        await Inbox.message.reply_text("Usage: `/qr <text or link>`", parse_mode="Markdown"); return
    status = await Inbox.message.reply_text("⏳ Generating QR code…")
    try:
        png = await qr_tools.generate_qr(text.strip())
        doc = BytesIO(png); doc.name = "qrcode.png"
        await Inbox.message.reply_photo(photo=doc, caption="✅ QR code ready.")
        await safe_delete(status)
        await _record(context, Inbox, "qr_generate")
    except Exception as exc:
        await safe_edit(status, _user_hint(exc))


async def genpass_command(Inbox: Inbox, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not Inbox.message: return
    if not context.args:
        await Inbox.message.reply_text("🔑 Choose a length:", reply_markup=build_genpass_keyboard()); return
    length     = password_tools.DEFAULT_LENGTH
    use_sym    = False
    no_ambig   = False
    for a in context.args:
        if a.lstrip("-").isdigit():   length  = int(a)
        elif a in ("--symbols","-s"): use_sym = True
        elif a in ("--no-ambiguous","-na"): no_ambig = True
    pw = password_tools.generate_password(length=length, use_symbols=use_sym, no_ambiguous=no_ambig)
    await Inbox.message.reply_text(f"🔑 `{pw}`", parse_mode="Markdown")
    await _record(context, Inbox, "genpass")


async def genpin_command(Inbox: Inbox, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not Inbox.message: return
    length = int(context.args[0]) if context.args and context.args[0].isdigit() else 4
    await Inbox.message.reply_text(f"🔢 `{password_tools.generate_pin(length)}`", parse_mode="Markdown")
    await _record(context, Inbox, "genpin")


async def compress_command(Inbox: Inbox, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not Inbox.message: return
    target = DEFAULT_COMPRESS_TARGET
    if context.args:
        parsed = image_extra.parse_size_to_bytes(context.args[0])
        if parsed: target = parsed
    context.user_data["awaiting"]              = "compress_image"
    context.user_data["compress_target_bytes"] = target
    await Inbox.message.reply_text(f"📉 Send the photo to compress (target: ~{target//1024} KB).")


async def watermark_command(Inbox: Inbox, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not Inbox.message: return
    context.user_data["awaiting"] = "watermark_setup"
    await Inbox.message.reply_text(
        "💧 Send your *logo/signature image* first (PNG with transparency works best).",
        parse_mode="Markdown",
    )


# ════════════════════════════════════════════════════════════════════════════
#  CALLBACK HANDLERS
# ════════════════════════════════════════════════════════════════════════════

async def handle_effect_category_callback(Inbox: Inbox, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = Inbox.callback_query; await query.answer()
    try: _, category, token = query.data.split("|", 2)
    except ValueError: await query.edit_message_text("❌ Invalid."); return
    if not _get_file_id(context.bot_data, token):
        await query.edit_message_text("❌ Request expired — please resend the photo."); return
    await query.edit_message_text(
        f"🎨 *{EFFECT_CATEGORIES.get(category, category)}* — choose an effect:",
        parse_mode="Markdown", reply_markup=build_effect_keyboard(category, token),
    )


async def handle_effect_back_callback(Inbox: Inbox, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = Inbox.callback_query; await query.answer()
    try: _, token = query.data.split("|", 1)
    except ValueError: await query.edit_message_text("❌ Invalid."); return
    if not _get_file_id(context.bot_data, token):
        await query.edit_message_text("❌ Request expired — please resend the photo."); return
    await query.edit_message_text("🎨 *Choose a category:*", parse_mode="Markdown", reply_markup=build_effect_categories_keyboard(token))


async def handle_effect_callback(Inbox: Inbox, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = Inbox.callback_query; await query.answer()
    try: _, effect_key, token = query.data.split("|", 2)
    except ValueError: await query.edit_message_text("❌ Invalid."); return
    file_id = _get_file_id(context.bot_data, token)
    if not file_id:
        await query.edit_message_text("❌ Request expired — please resend the photo."); return

    # Effects are a heavy tool — enforce the rate limit here too.
    if not await _check_heavy_rate_limit(Inbox, "effects"):
        return

    label = EFFECT_NAMES.get(effect_key, effect_key)
    status_msg = await query.edit_message_text(f"⏳ Applying *{label}*…", parse_mode="Markdown")
    try:
        await context.bot.send_chat_action(chat_id=query.message.chat_id, action=ChatAction.UPLOAD_PHOTO)
        image_bytes = await _download_file(context, file_id)
        w, h        = _get_image_dimensions(image_bytes)
        async with storage.HEAVY_JOB_SEMAPHORE:
            png_bytes = await apply_effect_to_image(image_bytes, effect_key, w, h)
        doc = BytesIO(png_bytes); doc.name = f"{effect_key}.png"
        await query.message.reply_document(
            document=doc, filename=f"{effect_key}.png",
            caption=truncate_caption(f"✅ {label} applied ({w}×{h}px)"),
        )
        await safe_edit(status_msg, f"✅ *{label}* done!", parse_mode="Markdown")
        _drop_token(context.bot_data, token)
        await _record(context, Inbox, f"effect:{effect_key}")
    except asyncio.TimeoutError:
        await safe_edit(status_msg, "⏱ Timed out — try a smaller image.")
    except Exception as exc:
        logger.warning("Effect %s failed: %s", effect_key, exc)
        await safe_edit(status_msg, _user_hint(exc))
        await notify_admin(context, "effects", f"Effect `{effect_key}` failed: {exc}")


async def handle_menu_callback(Inbox: Inbox, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = Inbox.callback_query; await query.answer()
    try: _, action = query.data.split("|", 1)
    except ValueError: await query.edit_message_text("❌ Invalid."); return

    _TOOL_PROMPTS: dict[str, tuple[str, str]] = {
        "effects":     ("effects",        "🎨 Send me a *photo* and pick an effect."),
        "bgremove":    ("bgremove",        "🧹 Send me a *photo* — I'll remove the background."),
        "sticker2png": ("sticker2png",     "😄 Send me a *static sticker* to convert to PNG."),
        "gif2frames":  ("gif2frames",      f"🎞 Send me a *GIF* — I'll extract up to {GIF_MAX_FRAMES} frames."),
        "img2pdf":     ("img2pdf",         "🖼 Send me an *image* — I'll wrap it into a PDF."),
        "pdf2img":     ("pdf2img",         f"📄 Send me a *PDF* — I'll render up to {PDF2IMG_MAX_PAGES} pages."),
        "jpg2png":     ("jpg2png",         "🔁 Send me a *JPEG* as a file for best quality."),
        "png2jpg":     ("png2jpg",         "🔁 Send me a *PNG* as a file."),
        "fiversanitize":("fiver_info",     "🛡 প্রথমে অর্ডার তথ্য দিন: `ClientName_OrderID_ProfileName_Amount`"),
        "compress":    ("compress_image",  f"📉 Send me the photo to compress (default ~{DEFAULT_COMPRESS_TARGET//1024} KB)."),
        "qrscan":      ("qrscan",          "🔍 Send me a photo containing a QR code."),
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
    if action == "effects":
        context.user_data["awaiting"] = "effects"
        await query.edit_message_text("🎨 Send me a *photo* and I'll show the effect categories.", parse_mode="Markdown"); return

    if action in _TOOL_PROMPTS:
        tool_key, msg = _TOOL_PROMPTS[action]
        context.user_data["awaiting"] = tool_key
        if tool_key == "fiver_info":
            context.user_data.pop("fiver_info", None)
        await query.edit_message_text(msg, parse_mode="Markdown")
    else:
        await query.edit_message_text("❌ Unknown option.")


async def handle_category_callback(Inbox: Inbox, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = Inbox.callback_query; await query.answer()
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


async def handle_color_callback(Inbox: Inbox, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = Inbox.callback_query; await query.answer()
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


async def handle_dev_callback(Inbox: Inbox, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = Inbox.callback_query; await query.answer()
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


async def handle_case_callback(Inbox: Inbox, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = Inbox.callback_query; await query.answer()
    try: _, mode = query.data.split("|", 1)
    except ValueError: await query.edit_message_text("❌ Invalid."); return
    text = context.user_data.pop("case_input", "")
    if not text:
        await query.edit_message_text("⚠️ No text stored. Please restart the tool."); return
    result = convert_case(text, mode)
    await query.edit_message_text(f"🔤 *{mode}:*\n\n`{result}`", parse_mode="Markdown", reply_markup=build_dev_input_keyboard("case"))


async def handle_translate_lang_callback(Inbox: Inbox, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = Inbox.callback_query; await query.answer()
    try: _, lang_code = query.data.split("|", 1)
    except ValueError: await query.edit_message_text("❌ Invalid."); return
    lang_label = next((l for c, l in TRANSLATE_LANGUAGES if c == lang_code), lang_code)
    context.user_data["awaiting"]    = "translate_text"
    context.user_data["target_lang"] = lang_code
    await query.edit_message_text(f"✏️ Send the text to translate to *{lang_label}*.", parse_mode="Markdown")


async def handle_watermark_position_callback(Inbox: Inbox, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = Inbox.callback_query; await query.answer()
    try: _, position = query.data.split("|", 1)
    except ValueError: await query.edit_message_text("❌ Invalid."); return
    context.user_data["watermark_position"] = position
    context.user_data["awaiting"]           = "watermark_apply"
    await query.edit_message_text(f"✅ Position set to *{position}*. Now send the photo to stamp.", parse_mode="Markdown")


async def handle_genpass_callback(Inbox: Inbox, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = Inbox.callback_query; await query.answer()
    try:
        _, length_str = query.data.split("|", 1)
        pw = password_tools.generate_password(length=int(length_str), use_symbols=True)
        await query.edit_message_text(f"🔑 `{pw}`", parse_mode="Markdown")
        await _record(context, Inbox, "genpass")
    except Exception:
        await query.edit_message_text("❌ Failed to generate password.")


async def handle_genpin_callback(Inbox: Inbox, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = Inbox.callback_query; await query.answer()
    try:
        _, length_str = query.data.split("|", 1)
        await query.edit_message_text(f"🔢 `{password_tools.generate_pin(int(length_str))}`", parse_mode="Markdown")
        await _record(context, Inbox, "genpin")
    except Exception:
        await query.edit_message_text("❌ Failed to generate PIN.")


async def handle_limit_request_callback(Inbox: Inbox, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = Inbox.callback_query; await query.answer()
    if not is_admin(Inbox):
        await query.answer("Admins only.", show_alert=True); return
    parts     = query.data.split("|")
    action    = parts[1]
    target_id = int(parts[2])
    if action == "approve":
        requested = int(parts[3])
        await storage.set_custom_limit(target_id, requested)
        await storage.remove_limit_request(target_id)
        await safe_edit(query.message, f"✅ Approved: `{target_id}` → {requested} calls/60s.", parse_mode="Markdown")
        try: await context.bot.send_message(target_id, f"✅ Your limit request was approved — now {requested} calls/60s.")
        except Exception: pass
    else:
        await storage.remove_limit_request(target_id)
        await safe_edit(query.message, f"❌ Denied request from `{target_id}`.", parse_mode="Markdown")
        try: await context.bot.send_message(target_id, "❌ Your limit increase request was denied.")
        except Exception: pass


# ════════════════════════════════════════════════════════════════════════════
#  MEDIA HANDLERS
# ════════════════════════════════════════════════════════════════════════════

async def _warn_wrong_media(Inbox: Inbox, context: ContextTypes.DEFAULT_TYPE, awaiting: str, got: str) -> None:
    label = _AWAITING_LABELS.get(awaiting, awaiting)
    await Inbox.message.reply_text(
        f"⚠️ I'm waiting for *{label}* input, but got a {got}.\n"
        f"Send the correct input or /cancel.",
        parse_mode="Markdown",
    )


async def handle_sticker(Inbox: Inbox, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not Inbox.message or not Inbox.message.sticker: return
    awaiting = context.user_data.get("awaiting")
    if awaiting and awaiting != "sticker2png":
        await _warn_wrong_media(Inbox, context, awaiting, "sticker"); return
    sticker = Inbox.message.sticker
    if sticker.is_animated or sticker.is_video:
        await Inbox.message.reply_text("⚠️ Animated/video stickers can't be converted to static PNG."); return
    await _run_image_tool(Inbox, context, sticker.file_id, "sticker2png")
    context.user_data.pop("awaiting", None)


async def handle_photo(Inbox: Inbox, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not Inbox.message or not Inbox.message.photo: return
    awaiting = context.user_data.get("awaiting")
    file_id  = Inbox.message.photo[-1].file_id

    if awaiting in _SINGLE_IMAGE_TOOLS:
        await _run_image_tool(Inbox, context, file_id, awaiting)
        context.user_data.pop("awaiting", None); return

    if awaiting == "watermark_setup":
        await storage.set_watermark(Inbox.effective_chat.id, file_id)
        _watermark_cache[file_id] = await _download_file(context, file_id)
        context.user_data["awaiting"] = "watermark_apply"
        await Inbox.message.reply_text(
            "✅ Logo saved. Now send the *photo* to stamp, or pick a position:",
            parse_mode="Markdown", reply_markup=build_watermark_position_keyboard(),
        ); return

    if awaiting == "watermark_apply":
        if not await _check_heavy_rate_limit(Inbox, "watermark"): return
        wm_file_id = await storage.get_watermark(Inbox.effective_chat.id)
        if not wm_file_id:
            await Inbox.message.reply_text("⚠️ No logo saved yet. Use /watermark first.")
            context.user_data.pop("awaiting", None); return
        status = await Inbox.message.reply_text("⏳ Applying watermark…")
        try:
            base_bytes = await _download_file(context, file_id)
            wm_bytes   = await _get_watermark_bytes(context, wm_file_id)
            position   = context.user_data.get("watermark_position", "bottom-right")
            async with storage.HEAVY_JOB_SEMAPHORE:
                out = await image_extra.apply_watermark(base_bytes, wm_bytes, position=position)
            doc = BytesIO(out); doc.name = "watermarked.jpg"
            await Inbox.message.reply_document(document=doc, filename="watermarked.jpg")
            await safe_delete(status)
            await _record(context, Inbox, "watermark")
        except Exception as exc:
            await safe_edit(status, _user_hint(exc))
            await notify_admin(context, "watermark", f"watermark failed: {exc}")
        return

    if awaiting == "compress_image":
        if not await _check_heavy_rate_limit(Inbox, "compress"): return
        target = context.user_data.get("compress_target_bytes", DEFAULT_COMPRESS_TARGET)
        status = await Inbox.message.reply_text("⏳ Compressing…")
        try:
            raw = await _download_file(context, file_id)
            async with storage.HEAVY_JOB_SEMAPHORE:
                out = await image_extra.compress_to_target(raw, target)
            doc = BytesIO(out); doc.name = "compressed.jpg"
            await Inbox.message.reply_document(document=doc, filename="compressed.jpg",
                caption=f"✅ {len(out)/1024:.0f} KB (target ~{target//1024} KB)")
            await safe_delete(status)
            await _record(context, Inbox, "compress")
        except Exception as exc:
            await safe_edit(status, _user_hint(exc))
        finally:
            context.user_data.pop("awaiting", None)
            context.user_data.pop("compress_target_bytes", None)
        return

    if awaiting == "qrscan":
        status = await Inbox.message.reply_text("⏳ Scanning for QR codes…")
        try:
            raw     = await _download_file(context, file_id)
            results = await qr_tools.scan_qr(raw)
            if not results:
                await safe_edit(status, "❌ No QR code found.")
            else:
                await safe_edit(status, "✅ *Found {}:*\n\n{}".format(
                    len(results), "\n\n".join(f"🔗 `{r}`" for r in results)
                ), parse_mode="Markdown")
            await _record(context, Inbox, "qr_scan")
        except Exception as exc:
            await safe_edit(status, _user_hint(exc))
        finally:
            context.user_data.pop("awaiting", None)
        return

    if awaiting and awaiting not in ("effects",):
        await _warn_wrong_media(Inbox, context, awaiting, "photo"); return

    context.user_data.pop("awaiting", None)
    token = _store_token(context.bot_data, file_id)
    await Inbox.message.reply_text(
        "🎨 *Choose a category:*", parse_mode="Markdown",
        reply_markup=build_effect_categories_keyboard(token),
    )


async def handle_animation(Inbox: Inbox, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not Inbox.message or not Inbox.message.animation: return
    awaiting = context.user_data.get("awaiting")
    if awaiting and awaiting != "gif2frames":
        await _warn_wrong_media(Inbox, context, awaiting, "GIF"); return
    if awaiting == "gif2frames":
        await _run_gif_tool(Inbox, context, Inbox.message.animation.file_id)
        context.user_data.pop("awaiting", None)
    else:
        await Inbox.message.reply_text(f"🎞 GIF detected! Extracting up to {GIF_MAX_FRAMES} frames…")
        await _run_gif_tool(Inbox, context, Inbox.message.animation.file_id)


async def handle_document(Inbox: Inbox, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not Inbox.message or not Inbox.message.document: return
    doc       = Inbox.message.document
    awaiting  = context.user_data.get("awaiting")
    file_name = (doc.file_name or "").lower()
    mime      = doc.mime_type or ""

    if not awaiting:
        hints = {
            ".pdf": "📄 PDF detected — go to /menu → *PDF Tools*.",
            ".docx":"📝 DOCX detected — go to /menu → *Documents*.",
            ".xlsx":"📊 XLSX detected — go to /menu → *Spreadsheet*.",
            ".pptx":"📽 PPTX detected — go to /menu → *Presentation*.",
            ".md":  "📝 Markdown detected — go to /menu → *Text & Markup*.",
            ".gif": f"🎞 GIF detected — go to /menu → GIF → Frames.",
        }
        for ext, hint in hints.items():
            if file_name.endswith(ext):
                await Inbox.message.reply_text(hint, parse_mode="Markdown", reply_markup=build_main_menu_keyboard()); return
        await Inbox.message.reply_text("Choose a tool first via /menu.", reply_markup=build_main_menu_keyboard()); return

    if awaiting in _SINGLE_IMAGE_TOOLS:
        if not (mime.startswith("image/") or any(file_name.endswith(e) for e in (".png",".jpg",".jpeg",".webp",".bmp",".tiff",".gif"))):
            await Inbox.message.reply_text("⚠️ Please send an image file."); return
        await _run_image_tool(Inbox, context, doc.file_id, awaiting)
        context.user_data.pop("awaiting", None); return

    if awaiting == "gif2frames":
        if not (file_name.endswith(".gif") or mime == "image/gif"):
            await Inbox.message.reply_text("⚠️ Please send a .gif file."); return
        await _run_gif_tool(Inbox, context, doc.file_id)
        context.user_data.pop("awaiting", None); return

    if awaiting == "pdf2img":
        if not await _check_heavy_rate_limit(Inbox, "pdf2img"): return
        if not (file_name.endswith(".pdf") or mime == "application/pdf"):
            await Inbox.message.reply_text("⚠️ Please send a .pdf file."); return
        status = await Inbox.message.reply_text("⏳ Rendering PDF pages…")
        try:
            pdf_bytes = await _download_file(context, doc.file_id)
            async with storage.HEAVY_JOB_SEMAPHORE:
                pages = await pdf_to_images(pdf_bytes)
            if not pages:
                await safe_edit(status, "❌ No pages could be rendered."); return
            await safe_delete(status)
            for i in range(0, len(pages), 10):
                await Inbox.message.reply_media_group([InputMediaPhoto(BytesIO(p)) for p in pages[i:i+10]])
            if len(pages) >= PDF2IMG_MAX_PAGES:
                await Inbox.message.reply_text(f"ℹ️ Only the first {PDF2IMG_MAX_PAGES} pages were rendered.")
            await _record(context, Inbox, "pdf2img")
        except Exception as exc:
            await safe_edit(status, _user_hint(exc))
            await notify_admin(context, "pdf2img", f"pdf2img failed: {exc}")
        finally:
            context.user_data.pop("awaiting", None)
        return

    if awaiting == "fiver_body":
        if file_name.endswith(".txt") or mime == "text/plain":
            status = await Inbox.message.reply_text("⏳ Sanitising…")
            try:
                raw  = await _download_file(context, doc.file_id)
                text = raw.decode("utf-8", errors="replace")
                if len(text) > FIVER_SANITIZE_MAX_CHARS:
                    await safe_edit(status, f"⚠️ Too long ({len(text)} chars). Limit: {FIVER_SANITIZE_MAX_CHARS}."); return
                custom = await storage.get_words(Inbox.effective_chat.id)
                san, changes, score = sanitize_fiver_text(text, custom)
                info = context.user_data.get("fiver_info") or {
                    "profile": "—", "client": "—", "project": "—", "order_id": "—", "amount": "—",
                }
                final_output = build_fiver_output(info, san)
                await safe_delete(status)
                await Inbox.message.reply_text(f"```\n{final_output}\n```", parse_mode="Markdown")
                await Inbox.message.reply_text(format_sanitizer_report(changes, score), parse_mode="Markdown")
                await _record(context, Inbox, "fiver_sanitize")
            except Exception as exc:
                logger.warning("fiver_sanitize (doc) failed: %s", exc)
                await safe_edit(status, _user_hint(exc))
            finally:
                context.user_data.pop("awaiting", None)
                context.user_data.pop("fiver_info", None)
        else:
            await Inbox.message.reply_text("⚠️ Please send a .txt file or type the message directly.")
        return

    if awaiting == "compress_image":
        if not (mime.startswith("image/") or any(file_name.endswith(e) for e in (".png",".jpg",".jpeg",".webp"))):
            await Inbox.message.reply_text("⚠️ Please send an image file."); return
        if not await _check_heavy_rate_limit(Inbox, "compress"): return
        target = context.user_data.get("compress_target_bytes", DEFAULT_COMPRESS_TARGET)
        status = await Inbox.message.reply_text("⏳ Compressing…")
        try:
            raw = await _download_file(context, doc.file_id)
            async with storage.HEAVY_JOB_SEMAPHORE:
                out = await image_extra.compress_to_target(raw, target)
            out_doc = BytesIO(out); out_doc.name = "compressed.jpg"
            await Inbox.message.reply_document(document=out_doc, filename="compressed.jpg",
                caption=f"✅ {len(out)/1024:.0f} KB (target ~{target//1024} KB)")
            await safe_delete(status)
            await _record(context, Inbox, "compress")
        except Exception as exc:
            await safe_edit(status, _user_hint(exc))
        finally:
            context.user_data.pop("awaiting", None)
            context.user_data.pop("compress_target_bytes", None)
        return

    if awaiting == "qrscan":
        if not (mime.startswith("image/") or any(file_name.endswith(e) for e in (".png",".jpg",".jpeg",".webp"))):
            await Inbox.message.reply_text("⚠️ Please send an image containing a QR code."); return
        status = await Inbox.message.reply_text("⏳ Scanning…")
        try:
            raw     = await _download_file(context, doc.file_id)
            results = await qr_tools.scan_qr(raw)
            if not results:
                await safe_edit(status, "❌ No QR code found.")
            else:
                await safe_edit(status, "✅ *Found {}:*\n\n{}".format(
                    len(results), "\n\n".join(f"🔗 `{r}`" for r in results)
                ), parse_mode="Markdown")
            await _record(context, Inbox, "qr_scan")
        except Exception as exc:
            await safe_edit(status, _user_hint(exc))
        finally:
            context.user_data.pop("awaiting", None)
        return

    if awaiting in _DOC_TOOLS or awaiting == "pptx2images":
        if not _doc_accepts(awaiting, mime, file_name):
            exts = " / ".join(_DOC_ACCEPTS.get(awaiting, ([], [".file"]))[1])
            await Inbox.message.reply_text(f"⚠️ Expected: `{exts}`", parse_mode="Markdown"); return
        await _run_doc_tool(Inbox, context, doc.file_id, awaiting)
        context.user_data.pop("awaiting", None); return

    await Inbox.message.reply_text("Choose a tool first via /menu.", reply_markup=build_main_menu_keyboard())


# ── Text message handler ──────────────────────────────────────────────────────

async def handle_message(Inbox: Inbox, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not Inbox.message or not Inbox.message.text: return
    raw_text = Inbox.message.text
    awaiting = context.user_data.get("awaiting")

    if GREETING_RE.match(raw_text.strip()) and awaiting not in ("fiver_info", "fiver_body"):
        context.user_data.pop("awaiting", None)
        await Inbox.message.reply_text(WELCOME_MESSAGE, parse_mode="Markdown", reply_markup=build_main_menu_keyboard()); return

    # ── Fiver: step 1 — combined info string ──
    if awaiting == "fiver_info":
        info = parse_fiver_info(raw_text)
        if info is None:
            await Inbox.message.reply_text(
                "⚠️ ফরম্যাট মিলছে না। ঠিক এভাবে দিন:\n"
                f"`{FIVER_INFO_PATTERN_HINT}`\n\n"
                "উদাহরণ: `jmbattaglia_FO41C0CAC4D84_CustomerPortal_brainflux_1000`",
                parse_mode="Markdown",
            )
            return
        context.user_data["fiver_info"] = info
        context.user_data["awaiting"] = "fiver_body"
        await Inbox.message.reply_text("✅ তথ্য সংরক্ষিত হয়েছে। এখন ক্লায়েন্ট মেসেজটি পাঠান।")
        return

    # ── Fiver: step 2 — the actual message body ──
    if awaiting == "fiver_body":
        text = raw_text.strip()
        if not text:
            await Inbox.message.reply_text("দয়া করে মেসেজ টেক্সট পাঠান।"); return
        if len(text) > FIVER_SANITIZE_MAX_CHARS:
            await Inbox.message.reply_text(f"⚠️ Too long ({len(text)} chars). Limit: {FIVER_SANITIZE_MAX_CHARS}.")
            return
        try:
            custom = await storage.get_words(Inbox.effective_chat.id)
            san, changes, score = sanitize_fiver_text(text, custom)
            info = context.user_data.get("fiver_info") or {
                "profile": "—", "client": "—", "project": "—", "order_id": "—", "amount": "—",
            }
            final_output = build_fiver_output(info, san)

            await Inbox.message.reply_text(f"```\n{final_output}\n```", parse_mode="Markdown")
            await Inbox.message.reply_text(format_sanitizer_report(changes, score), parse_mode="Markdown")

            await _record(context, Inbox, "fiver_sanitize")
        except Exception as exc:
            logger.warning("fiver_sanitize (v3) failed: %s", exc)
            await Inbox.message.reply_text(f"❌ কিছু একটা ভুল হয়েছে। আবার চেষ্টা করুন বা /cancel দিন।")
            await notify_admin(context, "fiver_sanitize", f"fiver_sanitize failed: {exc}")
        finally:
            context.user_data.pop("awaiting", None)
            context.user_data.pop("fiver_info", None)
        return

    # ── Translate ──
    if awaiting == "translate_text":
        text = raw_text.strip()
        if not text: await Inbox.message.reply_text("Please send some text."); return
        if len(text) > TRANSLATE_MAX_CHARS:
            await Inbox.message.reply_text(f"⚠️ Too long ({len(text)} chars). Limit: {TRANSLATE_MAX_CHARS}."); return
        status = await Inbox.message.reply_text("🌐 Translating…")
        try:
            translated = await translate_text(text, context.user_data.get("target_lang", "en"))
            await safe_edit(status, f"✅ *Translation:*\n\n{translated}", parse_mode="Markdown")
            await _record(context, Inbox, "translate")
        except Exception as exc:
            await safe_edit(status,
                "🚦 Service temporarily rate-limited. Please try again in a minute."
                if "rate_limited" in str(exc) else "❌ Translation failed.")
        context.user_data.pop("awaiting", None)
        context.user_data.pop("target_lang", None); return

    # ── Colour Tools ──
    if awaiting == "color_input":
        tool = context.user_data.get("color_tool", "")
        text = raw_text.strip()
        result = ""

        if tool == "convert":
            rgb = None
            if text.startswith("#"):
                rgb = hex_to_rgb(text)
            elif "," in text or text.replace(" ","").isdigit() or re.match(r"rgb\(", text, re.I):
                nums = re.findall(r"\d+", text)
                if len(nums) >= 3:
                    rgb = (int(nums[0]), int(nums[1]), int(nums[2]))
            if rgb:
                r, g, b = rgb
                h, s, l = rgb_to_hsl(r, g, b)
                c, m, y, k = rgb_to_cmyk(r, g, b)
                result = (
                    f"🎨 *Colour Conversions*\n\n"
                    f"HEX:  `{rgb_to_hex(r,g,b)}`\n"
                    f"RGB:  `rgb({r}, {g}, {b})`\n"
                    f"HSL:  `hsl({h}, {s}%, {l}%)`\n"
                    f"CMYK: `cmyk({c}%, {m}%, {y}%, {k}%)`"
                )
            else:
                result = "❌ Couldn't parse that colour. Try `#FF5733` or `255 87 51`."

        elif tool == "contrast":
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            if len(lines) >= 2:
                rgb1 = hex_to_rgb(lines[0])
                rgb2 = hex_to_rgb(lines[1])
                if rgb1 and rgb2:
                    ratio = wcag_contrast(*rgb1, *rgb2)
                    aa  = "✅ Pass" if ratio >= 4.5 else "❌ Fail"
                    aaa = "✅ Pass" if ratio >= 7.0 else "❌ Fail"
                    result = (
                        f"✅ *Contrast Ratio:* `{ratio}:1`\n\n"
                        f"WCAG AA  (4.5:1) — {aa}\n"
                        f"WCAG AAA (7.0:1) — {aaa}"
                    )
                else:
                    result = "❌ Could not parse colours. Use hex format: `#FFFFFF`"
            else:
                result = "❌ Send two hex colours, one per line."

        elif tool == "gradient":
            rgb = hex_to_rgb(text)
            result = f"```css\n{css_gradient(*rgb)}\n```" if rgb else "❌ Invalid hex colour."

        elif tool == "shadow":
            rgb = hex_to_rgb(text)
            result = f"```css\n{css_shadow(*rgb)}\n```" if rgb else "❌ Invalid hex colour."

        elif tool == "tintshade":
            rgb = hex_to_rgb(text)
            result = f"🖌 *Tints & Shades for `{text}`*\n\n```\n{tint_shade(*rgb)}\n```" if rgb else "❌ Invalid hex colour."

        await Inbox.message.reply_text(result, parse_mode="Markdown", reply_markup=build_color_input_keyboard(tool))
        context.user_data.pop("awaiting", None)
        await _record(context, Inbox, f"color:{tool}"); return

    # ── Dev Tools ──
    if awaiting == "dev_input":
        tool = context.user_data.get("dev_tool", "")
        text = raw_text.strip()
        result = ""

        if tool == "json":
            result = f"```json\n{format_json(text)}\n```"
        elif tool == "hash":
            result = f"🔑 *Hashes for your text:*\n\n{hash_text(text)}"
        elif tool == "timestamp":
            result = f"⏰ {convert_timestamp(text)}"
        elif tool == "pxrem":
            nums = re.findall(r"[\d.]+", text)
            result = px_to_rem(float(nums[0])) if nums else "❌ Send a number like `16` or `24px`."
        elif tool == "urlencode":
            result = f"🔗 `{url_encode(text)}`"
        elif tool == "urldecode":
            result = f"🔗 `{url_decode(text)}`"
        elif tool == "b64enc":
            result = f"💻 `{base64_encode(text)}`"
        elif tool == "b64dec":
            result = f"💻 `{base64_decode(text)}`"
        elif tool == "wordcount":
            result = f"📊 *Word Count*\n\n{word_count(text)}"
        elif tool == "diff":
            if "diff_text_a" not in context.user_data:
                context.user_data["diff_text_a"] = text
                await Inbox.message.reply_text("✅ Text A saved. Now send *Text B*:", parse_mode="Markdown")
                return
            else:
                result = f"```\n{diff_texts(context.user_data.pop('diff_text_a'), text)}\n```"
        elif tool == "regex":
            parts = text.split("\n", 1)
            result = regex_test(parts[0], parts[1]) if len(parts) == 2 else "❌ Send `<pattern>\\n<text>`."
        elif tool == "case_text":
            context.user_data["case_input"] = text
            context.user_data.pop("awaiting", None)
            await Inbox.message.reply_text("🔤 Choose a case style:", reply_markup=build_case_keyboard()); return

        await Inbox.message.reply_text(result or "❌ Empty result.", parse_mode="Markdown", reply_markup=build_dev_input_keyboard(tool))
        context.user_data.pop("awaiting", None)
        await _record(context, Inbox, f"dev:{tool}"); return

    if awaiting and awaiting not in ("translate_text", "fiver_info", "fiver_body", "color_input", "dev_input"):
        await _warn_wrong_media(Inbox, context, awaiting, "text message"); return

    # ── URL detection ──
    lummi_match = LUMMI_URL_RE.search(raw_text)
    url = trim_url(lummi_match.group(0)) if lummi_match else None
    platform = "lummi" if url else None

    if not url:
        generic_match = URL_RE.search(raw_text)
        if generic_match:
            candidate = trim_url(generic_match.group(0))
            detected  = detect_platform(candidate)
            if detected:
                url, platform = candidate, detected

    if not url or not platform:
        await Inbox.message.reply_text(
            "Send a Lummi.ai or Hugeicons link, a photo, sticker, or GIF — "
            "or use /FiverMessage to sanitise text. Type /help for examples."
        ); return

    await context.bot.send_chat_action(chat_id=Inbox.effective_chat.id, action=ChatAction.UPLOAD_DOCUMENT)
    status = await Inbox.message.reply_text("⏳ Processing your link…")

    if platform == "lummi":
        try:
            await safe_edit(status, "⏳ Downloading Lummi asset…")
            result = await fetch_lummi_asset(url)
            doc    = BytesIO(result["bytes"]); doc.name = result["filename"]
            await Inbox.message.reply_document(
                document=doc, filename=result["filename"],
                caption=truncate_caption(f"✅ {result['size_mb']:.2f} MB — {result['direct_url']}"),
            )
            await safe_delete(status)
            await _record(context, Inbox, "lummi")
        except Exception as exc:
            await safe_edit(status, _user_hint(exc))
    else:
        try:
            await safe_edit(status, "⏳ Fetching Hugeicons SVG…")
            result    = await fetch_hugeicons_svg(url)
            clean_svg = format_svg(result["svg"])
            filename  = f"{result['icon_name']}-{result['style']}.svg"
            await safe_delete(status)
            label_md  = f"✅ *{markdown_v2_escape(result['icon_name'])}* \\({markdown_v2_escape(result['style'])}\\)"
            await Inbox.message.reply_text(label_md, parse_mode="MarkdownV2")
            await Inbox.message.reply_text(f"```xml\n{markdown_code_escape(clean_svg)}\n```", parse_mode="MarkdownV2")
            doc = BytesIO(clean_svg.encode()); doc.name = filename
            await Inbox.message.reply_document(document=doc, filename=filename, caption=truncate_caption(f"{filename} — ready to use."))
            await _record(context, Inbox, "hugeicons")
        except Exception as exc:
            await safe_edit(status, _user_hint(exc))


async def error_handler(Inbox: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled exception:", exc_info=context.error)
    try:
        await notify_admin(context, "unhandled", f"Unhandled: {context.error}")
    except Exception:
        pass


# ── Lifecycle ─────────────────────────────────────────────────────────────────

async def _on_shutdown(app: Application) -> None:
    await close_http_client()
    app.bot_data.pop("pending_files", None)
    logger.info("Shutdown complete.")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set.")

    webhook_url = os.getenv("WEBHOOK_URL", "").strip()
    port        = int(os.getenv("PORT", 10000))

    app = (
        Application.builder()
        .token(token)
        .concurrent_Inboxs(True)
        .post_shutdown(_on_shutdown)
        .build()
    )

    for cmd, fn in [
        ("start",           start),
        ("help",            help_command),
        ("menu",            menu_command),
        ("cancel",          cancel_command),
        ("FiverMessage",    fivermessage_command),
        ("addword",         addword_command),
        ("mywords",         mywords_command),
        ("delword",         delword_command),
        ("resetwords",      resetwords_command),
        ("stats",           stats_command),
        ("useractivity",    useractivity_command),
        ("admins",          admins_command),
        ("setlimit",        setlimit_command),
        ("resetlimit",      resetlimit_command),
        ("mylimit",         mylimit_command),
        ("requestlimit",    requestlimit_command),
        ("pendingrequests", pendingrequests_command),
        ("blockuser",       blockuser_command),
        ("unblockuser",     unblockuser_command),
        ("qr",              qr_command),
        ("genpass",         genpass_command),
        ("genpin",          genpin_command),
        ("compress",        compress_command),
        ("watermark",       watermark_command),
    ]:
        app.add_handler(CommandHandler(cmd, fn))

    app.add_handler(MessageHandler(filters.PHOTO,           handle_photo))
    app.add_handler(MessageHandler(filters.Sticker.ALL,     handle_sticker))
    app.add_handler(MessageHandler(filters.ANIMATION,       handle_animation))
    app.add_handler(MessageHandler(filters.Document.ALL,    handle_document))

    app.add_handler(CallbackQueryHandler(handle_effect_category_callback,  pattern=r"^fxcat\|"))
    app.add_handler(CallbackQueryHandler(handle_effect_back_callback,      pattern=r"^fxback\|"))
    app.add_handler(CallbackQueryHandler(handle_effect_callback,           pattern=r"^fx\|"))
    app.add_handler(CallbackQueryHandler(handle_menu_callback,             pattern=r"^menu\|"))
    app.add_handler(CallbackQueryHandler(handle_category_callback,         pattern=r"^cat\|"))
    app.add_handler(CallbackQueryHandler(handle_translate_lang_callback,   pattern=r"^trlang\|"))
    app.add_handler(CallbackQueryHandler(handle_watermark_position_callback,pattern=r"^wmpos\|"))
    app.add_handler(CallbackQueryHandler(handle_genpass_callback,          pattern=r"^genpass\|"))
    app.add_handler(CallbackQueryHandler(handle_genpin_callback,           pattern=r"^genpin\|"))
    app.add_handler(CallbackQueryHandler(handle_limit_request_callback,    pattern=r"^limitreq\|"))
    app.add_handler(CallbackQueryHandler(handle_color_callback,            pattern=r"^color\|"))
    app.add_handler(CallbackQueryHandler(handle_dev_callback,              pattern=r"^dev\|"))
    app.add_handler(CallbackQueryHandler(handle_case_callback,             pattern=r"^case\|"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.add_error_handler(error_handler)

    logger.info(
        "BangaliIcon Bot v3.1 starting — "
        "Effects | Fiver Sanitizer v3 | Colour Tools | Dev Tools | "
        "Lummi + Hugeicons | QR | Passwords | Watermark | Compress"
    )

    if webhook_url:
        logger.info("Webhook mode — port %s", port)
        app.run_webhook(
            listen="0.0.0.0", port=port, url_path="/webhook",
            webhook_url=f"{webhook_url}/webhook", allowed_Inboxs=Inbox.ALL_TYPES,
        )
    else:
        logger.info("Polling mode")
        app.run_polling(allowed_Inboxs=Inbox.ALL_TYPES)


if __name__ == "__main__":
    main()
