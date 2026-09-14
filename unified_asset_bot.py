"""Unified Lummi AI + Hugeicons + Image Effects + Fiver Sanitizer Telegram bot.

Upgrades vs previous version:
  • Sticker & GIF support (WebP sticker → PNG, animated GIF → frames)
  • Centralised retry logic with exponential back-off for HTTP calls
  • Typed FileRouter to eliminate duplicated awaiting-state dispatch
  • connection-pooled AsyncClient (one client per process, not per call)
  • Graceful shutdown: HTTP client closed + pending_files pruned on bot shutdown (FIXED — now actually wired up)
  • Guard against stale "awaiting" state when the wrong media type is sent (FIXED)
  • Richer error messages with user-facing hints
  • NEW: /FiverMessage — sanitizes text (replaces flagged words) via command or guided prompt
  • Minor code cleanup: dead imports removed, constants consolidated
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import sys
import tempfile
import uuid
from io import BytesIO
from typing import Any, Callable, Coroutine
from urllib.parse import unquote, urlsplit

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
MAX_UPLOAD_BYTES        = 49 * 1024 * 1024
MAX_CAPTION_LENGTH      = 1024
BOT_DOWNLOAD_LIMIT_BYTES = 20 * 1024 * 1024
PDF2IMG_MAX_PAGES       = 20
MAX_PENDING_FILES       = 500
TRANSLATE_MAX_CHARS     = 4_500
FIVER_SANITIZE_MAX_CHARS = 4_500
REMBG_MAX_DIMENSION     = 1_500
REMBG_TIMEOUT_SECONDS   = 90
GIF_MAX_FRAMES          = 10   # frames extracted from animated GIF
GIF_FRAME_DELAY_MS      = 100  # default frame delay when not embedded

REQUEST_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=10.0)
HTTP_RETRY_ATTEMPTS = 3
HTTP_RETRY_BACKOFF  = 1.5  # seconds; doubles on each retry

ENGINE_SCRIPT = os.path.join(os.path.dirname(__file__), "python_engine.py")

U2NETP_MODEL_URL  = "https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2netp.onnx"
U2NETP_MODEL_PATH = os.path.join(tempfile.gettempdir(), "u2netp.onnx")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("unified_asset_bot")

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
    r"https?://(?:www\.)?lummi\.ai/(?:photo|illustration|3d)/[^\s<>]+",
    re.IGNORECASE,
)
URL_RE       = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
LUMMI_CID_RE = re.compile(r"Qm[1-9A-HJ-NP-Za-km-z]{44}")

GREETING_RE = re.compile(
    r"^(hi+|he+llo+|hey+|yo|start|salam|assalamu\s*alaikum|assalamualaikum)[!.\s]*$",
    re.IGNORECASE,
)

# ── Shared HTTP client (connection-pooled, reused across all requests) ────────
_http_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT,
            follow_redirects=True,
        )
    return _http_client


async def close_http_client() -> None:
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
        _http_client = None


# ── Retry helper ──────────────────────────────────────────────────────────────

async def with_retry(
    fn: Callable[[], Coroutine[Any, Any, Any]],
    attempts: int = HTTP_RETRY_ATTEMPTS,
    backoff: float = HTTP_RETRY_BACKOFF,
) -> Any:
    """Call `fn` up to `attempts` times, with exponential back-off on transient errors."""
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            return await fn()
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            last_exc = exc
            if attempt < attempts - 1:
                delay = backoff * (2 ** attempt)
                logger.warning("Request failed (%s), retrying in %.1fs…", exc, delay)
                await asyncio.sleep(delay)
    raise last_exc


# ── Effects ───────────────────────────────────────────────────────────────────
EFFECT_CATEGORIES: dict[str, str] = {
    "retro":   "Retro & Print",
    "tone":    "Color & Tone",
    "art":     "Artistic",
    "digital": "Digital & Glitch",
    "distort": "Distort & Special",
}
EFFECT_CATEGORY_ORDER = ["retro", "tone", "art", "digital", "distort"]

EFFECTS: list[tuple[str, str, str]] = [
    ("halftone-dots",        "Halftone",        "retro"),
    ("comic-cmyk",           "Pop-Art",         "retro"),
    ("retro-8bit",           "8-Bit CRT",       "retro"),
    ("risograph-duo",        "Risograph",       "retro"),
    ("crosshatch-engraving", "Crosshatch",      "retro"),
    ("bayer-dither",         "Dither",          "retro"),
    ("vhs-tape",             "VHS Tape",        "retro"),
    ("lomography",           "Lomography",      "retro"),
    ("duotone",              "Duotone",         "tone"),
    ("autumn-tone",          "Autumn",          "tone"),
    ("forest-green",         "Forest",          "tone"),
    ("desert-sand",          "Desert",          "tone"),
    ("cherry-blossom",       "Cherry Blossom",  "tone"),
    ("moonlight",            "Moonlight",       "tone"),
    ("frozen-ice",           "Frozen",          "tone"),
    ("watercolor",           "Watercolor",      "art"),
    ("oil-paint",            "Oil Paint",       "art"),
    ("emboss",               "Emboss",          "art"),
    ("cinematic-noir",       "Noir",            "art"),
    ("cyber-glitch",         "Cyberpunk",       "digital"),
    ("glitch-art",           "Glitch Art",      "digital"),
    ("ascii-matrix",         "ASCII",           "digital"),
    ("sobel-neon",           "Neon Edge",       "digital"),
    ("vaporwave",            "Vaporwave",       "digital"),
    ("neon-poster",          "Neon Poster",     "digital"),
    ("swirl-distort",        "Swirl",           "distort"),
    ("mirror-reflect",       "Mirror",          "distort"),
    ("pixelate",             "Pixelate",        "distort"),
    ("blueprint-cyan",       "Blueprint",       "distort"),
    ("thermal-flir",         "Thermal",         "distort"),
    ("inferno",              "Inferno",         "distort"),
    ("horror-red",           "Horror",          "distort"),
]

EFFECT_NAMES = {key: label for key, label, _ in EFFECTS}

# ── Languages ─────────────────────────────────────────────────────────────────
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

# ── Fiver message sanitizer ────────────────────────────────────────────────────
# Ported from the Text Sanitizer Pro Chrome extension's word map, with the
# malformed nested "marketing_and_spam" object removed (it broke replacement
# because its value was a dict, not a string).
FIVER_WORD_MAP: dict[str, str] = {
    "email": "ema-il", "gmail": "gma-il", "whatsapp": "wha-tsapp", "skype": "sky-pe",
    "telegram": "tele-gram", "discord": "dis-cord", "phone": "pho-ne", "mobile": "mobi-le",
    "number": "num-ber", "contact": "conta-ct", "zoom": "zo-om", "slack": "sla-ck",
    "linkedin": "link-edin", "facebook": "face-book", "instagram": "inst-agram",
    "twitter": "twit-ter", "youtube": "yout-ube", "tiktok": "tik-tok", "snapchat": "snap-chat",
    "pinterest": "pint-erest", "reddit": "red-dit", "tumblr": "tum-blr", "meeting": "mee-ting",
    "call": "ca-ll", "video call": "vid-eo c-all", "google meet": "g-meet",
    "anydesk": "any-desk", "teamviewer": "team-viewer",
    "payment": "pa-yment", "paypal": "p-ay-pal", "pay": "p-ay", "payoneer": "p-ay-oneer",
    "crypto": "cry-pto", "bitcoin": "bit-coin", "wire": "wi-re", "bank": "ba-nk",
    "transfer": "trans-fer", "cash": "ca-sh", "invoice": "invo-ice", "outside": "outsi-de",
    "direct": "dire-ct", "stripe": "str-ipe", "fee": "f-ee", "price": "pri-ce",
    "cost": "co-st", "billing": "bill-ing",
    "homework": "home-work", "assignment": "assign-ment", "essay": "ess-ay",
    "thesis": "the-sis", "exam": "ex-am", "test": "te-st", "degree": "deg-ree",
    "coursework": "course-work", "academic": "acad-emic",
    "review": "revi-ew", "feedback": "feed-back", "rating": "rat-ing",
    "trustpilot": "trust-pilot", "google review": "google-rev", "followers": "follo-wers",
    "subscribers": "subs-cribers", "five star": "5-st-ar", "positive": "posi-tive",
    "recommendation": "recomm-end",
    "password": "pass-word", "login": "lo-gin", "credential": "creden-tial",
    "address": "addr-ess", "location": "loca-tion", "private": "pri-vate",
    "access": "acc-ess", "verification": "veri-fication", "guaranteed": "guaran-teed",
    "money": "mon-ey", "income": "inco-me", "profit": "pro-fit", "fiverr": "fiv-err",
    "order": "ord-er", "cancel": "can-cel", "refund": "refu-nd",
    "portfolio": "port-folio", "website": "web-site",
}
# Sorted longest-first once at import time so overlapping phrases (e.g.
# "video call" vs "call") match correctly instead of the shorter word winning.
_FIVER_WORDS_SORTED = sorted(FIVER_WORD_MAP.keys(), key=len, reverse=True)
_FIVER_WORD_PATTERNS = [
    (word, re.compile(rf"\b{re.escape(word)}\b", re.IGNORECASE))
    for word in _FIVER_WORDS_SORTED
]


def _preserve_case(original: str, replacement: str) -> str:
    if original.islower():
        return replacement.lower()
    if original.isupper():
        return replacement.upper()
    if original[:1].isupper():
        return replacement[:1].upper() + replacement[1:].lower()
    return replacement


def sanitize_fiver_text(text: str) -> str:
    """Replace flagged words/phrases with their obfuscated equivalents."""
    result = text
    for word, pattern in _FIVER_WORD_PATTERNS:
        replacement = FIVER_WORD_MAP[word]
        result = pattern.sub(lambda m: _preserve_case(m.group(0), replacement), result)
    return result


# ── Strings ───────────────────────────────────────────────────────────────────
WELCOME_MESSAGE = (
    "```\n"
    "┌───────────────────────────────┐\n"
    "│    B A N G A L I · I C O N    │\n"
    "└───────────────────────────────┘\n"
    "```\n"
    "⚡ *@BangaliIconbot* — all modules loaded\n\n"
    "🔗 Send a *Lummi.ai* or *Hugeicons* link\n"
    "     → instant asset extraction\n"
    "🖼 Send a *photo*\n"
    "     → ✦ 32 real-time visual effects\n"
    "😄 Send a *sticker or GIF*\n"
    "     → extract frames or convert to PNG\n"
    "🛡 Use `/FiverMessage <text>`\n"
    "     → sanitize flagged words instantly\n"
    "🧰 Or tap a tool below to get started\n\n"
    "_Type_ `/menu` _anytime ·_ `/cancel` _to stop a task_\n\n"
    "✦ Design and Developed By *@YA_Rabbu*"
)

MENU_INTRO = "🧰 *Select a tool*"

# ── Keyboard builders ─────────────────────────────────────────────────────────

def build_main_menu_keyboard() -> InlineKeyboardMarkup:
    """Top-level menu — groups tools into categories."""
    rows = [
        [
            InlineKeyboardButton("🎨 Image Effects",   callback_data="menu|effects"),
            InlineKeyboardButton("🧹 Remove BG",       callback_data="menu|bgremove"),
        ],
        [
            InlineKeyboardButton("😄 Sticker → PNG",   callback_data="menu|sticker2png"),
            InlineKeyboardButton("🎞 GIF → Frames",    callback_data="menu|gif2frames"),
        ],
        [
            InlineKeyboardButton("🛡 Fiver Sanitizer",  callback_data="menu|fiversanitize"),
            InlineKeyboardButton("🌐 Translate",       callback_data="menu|translate"),
        ],
        [
            InlineKeyboardButton("🖼 Image Tools",     callback_data="cat|image"),
            InlineKeyboardButton("📄 PDF Tools",       callback_data="cat|pdf"),
        ],
        [
            InlineKeyboardButton("📝 Word / Doc",      callback_data="cat|word"),
            InlineKeyboardButton("📊 Spreadsheet",     callback_data="cat|sheet"),
        ],
        [
            InlineKeyboardButton("📽 Presentation",    callback_data="cat|ppt"),
            InlineKeyboardButton("✍️ Text & Markup",   callback_data="cat|text"),
        ],
        [
            InlineKeyboardButton("📚 More →",          callback_data="cat|more"),
        ],
    ]
    return InlineKeyboardMarkup(rows)


# ── Category sub-menus ────────────────────────────────────────────────────────

def build_category_keyboard(cat: str) -> InlineKeyboardMarkup:
    """Returns the sub-menu keyboard for a given tool category."""
    back = [InlineKeyboardButton("◀ Main Menu", callback_data="cat|back")]

    menus: dict[str, list[list[InlineKeyboardButton]]] = {
        "image": [
            [
                InlineKeyboardButton("🖼 Image → PDF",   callback_data="menu|img2pdf"),
                InlineKeyboardButton("📄 PDF → Images",  callback_data="menu|pdf2img"),
            ],
            [
                InlineKeyboardButton("🔁 JPEG → PNG",    callback_data="menu|jpg2png"),
                InlineKeyboardButton("🔁 PNG → JPEG",    callback_data="menu|png2jpg"),
            ],
            back,
        ],
        "pdf": [
            [
                InlineKeyboardButton("📄→📝 PDF → DOCX",  callback_data="menu|pdf2docx"),
                InlineKeyboardButton("📄→📃 PDF → TXT",   callback_data="menu|pdf2txt"),
            ],
            [
                InlineKeyboardButton("📄→🖼 PDF → Images", callback_data="menu|pdf2img"),
                InlineKeyboardButton("🖼→📄 Image → PDF",  callback_data="menu|img2pdf"),
            ],
            back,
        ],
        "word": [
            [
                InlineKeyboardButton("📝→📄 DOCX → PDF",  callback_data="menu|docx2pdf"),
                InlineKeyboardButton("📝→📃 DOCX → TXT",  callback_data="menu|docx2txt"),
            ],
            [
                InlineKeyboardButton("📝→🌐 DOCX → HTML", callback_data="menu|docx2html"),
                InlineKeyboardButton("📄→📝 DOC → DOCX",  callback_data="menu|doc2docx"),
            ],
            [
                InlineKeyboardButton("📄→📝 ODT → DOCX",  callback_data="menu|odt2docx"),
                InlineKeyboardButton("📄→📄 ODT → PDF",   callback_data="menu|odt2pdf"),
            ],
            back,
        ],
        "sheet": [
            [
                InlineKeyboardButton("📊→📄 XLSX → PDF",  callback_data="menu|xlsx2pdf"),
                InlineKeyboardButton("📊→📃 XLSX → CSV",  callback_data="menu|xlsx2csv"),
            ],
            [
                InlineKeyboardButton("📃→📊 CSV → XLSX",  callback_data="menu|csv2xlsx"),
                InlineKeyboardButton("📊→📊 XLS → XLSX",  callback_data="menu|xls2xlsx"),
            ],
            back,
        ],
        "ppt": [
            [
                InlineKeyboardButton("📽→📄 PPTX → PDF",  callback_data="menu|pptx2pdf"),
                InlineKeyboardButton("📽→🖼 PPTX → Imgs", callback_data="menu|pptx2images"),
            ],
            [
                InlineKeyboardButton("📽→📽 PPT → PPTX",  callback_data="menu|ppt2pptx"),
            ],
            back,
        ],
        "text": [
            [
                InlineKeyboardButton("📃→📄 TXT → PDF",   callback_data="menu|txt2pdf"),
                InlineKeyboardButton("🌐→📄 HTML → PDF",  callback_data="menu|html2pdf"),
            ],
            [
                InlineKeyboardButton("📝→📄 MD → PDF",    callback_data="menu|md2pdf"),
                InlineKeyboardButton("📝→📃 MD → TXT",    callback_data="menu|md2txt"),
            ],
            [
                InlineKeyboardButton("📝→📝 MD → DOCX",   callback_data="menu|md2docx"),
                InlineKeyboardButton("📄→📃 RTF → TXT",   callback_data="menu|rtf2txt"),
            ],
            [
                InlineKeyboardButton("📄→📄 RTF → PDF",   callback_data="menu|rtf2pdf"),
            ],
            back,
        ],
        "more": [
            [
                InlineKeyboardButton("📚→📄 EPUB → PDF",  callback_data="menu|epub2pdf"),
            ],
            back,
        ],
    }
    rows = menus.get(cat, [back])
    return InlineKeyboardMarkup(rows)


def build_effect_categories_keyboard(token: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for cat_key in EFFECT_CATEGORY_ORDER:
        row.append(InlineKeyboardButton(
            EFFECT_CATEGORIES[cat_key],
            callback_data=f"fxcat|{cat_key}|{token}",
        ))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def build_effect_keyboard(category: str, token: str) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for key, label, cat in EFFECTS:
        if cat != category:
            continue
        row.append(InlineKeyboardButton(label, callback_data=f"fx|{key}|{token}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("◀ Categories", callback_data=f"fxback|{token}")])
    return InlineKeyboardMarkup(buttons)


def build_translate_lang_keyboard() -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for code, label in TRANSLATE_LANGUAGES:
        row.append(InlineKeyboardButton(label, callback_data=f"trlang|{code}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(buttons)


# ── Utility ───────────────────────────────────────────────────────────────────

def trim_url(url: str) -> str:
    return url.rstrip(".,!?;:)]}>\"'")


def truncate_caption(caption: str) -> str:
    if len(caption) <= MAX_CAPTION_LENGTH:
        return caption
    return caption[:MAX_CAPTION_LENGTH - 3] + "..."


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
    """Convert common internal errors to friendly user-facing hints."""
    msg = str(exc).lower()
    if "timeout" in msg:
        return "⏱ The operation timed out. Please try a smaller file or try again later."
    if "too large" in msg or "size" in msg:
        return "📦 The file is too large (limit: 20 MB for download, 49 MB for upload)."
    if "memory" in msg or "oom" in msg:
        return "💾 The server ran low on memory. Try a smaller image."
    return f"❌ Something went wrong: {exc}"


# ── Safe Telegram helpers ─────────────────────────────────────────────────────

async def safe_edit(message: Any, text: str, **kwargs: Any) -> None:
    try:
        await message.edit_text(text, **kwargs)
    except BadRequest as exc:
        if "message to edit not found" in str(exc).lower():
            logger.debug("Status message gone, skipping edit: %s", exc)
        else:
            raise


async def safe_delete(message: Any) -> None:
    try:
        await message.delete()
    except BadRequest as exc:
        if "message to delete not found" in str(exc).lower():
            logger.debug("Status message gone, skipping delete: %s", exc)
        else:
            raise


# ── Pending-file token store ──────────────────────────────────────────────────

def _store_token(bot_data: dict, file_id: str) -> str:
    pending: dict[str, str] = bot_data.setdefault("pending_files", {})
    if len(pending) >= MAX_PENDING_FILES:
        oldest = next(iter(pending))
        pending.pop(oldest, None)
    token = uuid.uuid4().hex[:10]
    pending[token] = file_id
    return token


def _get_file_id(bot_data: dict, token: str) -> str | None:
    return bot_data.get("pending_files", {}).get(token)


def _drop_token(bot_data: dict, token: str) -> None:
    bot_data.get("pending_files", {}).pop(token, None)


# ── Image utilities ───────────────────────────────────────────────────────────

def _get_image_dimensions(image_bytes: bytes) -> tuple[int, int]:
    MAX_DIM = 400
    try:
        from PIL import Image
        img = Image.open(BytesIO(image_bytes))
        w, h = img.size
        if w > MAX_DIM or h > MAX_DIM:
            ratio = min(MAX_DIM / w, MAX_DIM / h)
            w, h = int(w * ratio), int(h * ratio)
        return w, h
    except Exception:
        return 300, 300


async def _image_to_rgba_b64(image_bytes: bytes, width: int, height: int) -> str:
    try:
        from PIL import Image
        img = Image.open(BytesIO(image_bytes)).convert("RGBA").resize((width, height))
        return base64.b64encode(img.tobytes()).decode()
    except ImportError:
        pass
    raw = bytearray(width * height * 4)
    for y in range(height):
        for x in range(width):
            idx = (y * width + x) * 4
            raw[idx]   = (x * 255) // max(1, width - 1)
            raw[idx+1] = (y * 255) // max(1, height - 1)
            raw[idx+2] = 128
            raw[idx+3] = 255
    return base64.b64encode(bytes(raw)).decode()


async def apply_effect_to_image(image_bytes: bytes, effect: str, width: int, height: int) -> bytes:
    rgba_b64 = await _image_to_rgba_b64(image_bytes, width, height)
    payload = json.dumps({
        "effect": effect,
        "width": width,
        "height": height,
        "pixels_rgba_b64": rgba_b64,
        "params": {
            "dotPitch": 8,
            "contrast": 1.2,
            "brightness": 1.0,
            "grainIntensity": 18,
            "vignette": 0.3,
        },
    })
    proc = await asyncio.create_subprocess_exec(
        sys.executable, ENGINE_SCRIPT,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(input=payload.encode()), timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"Engine error: {stderr.decode()[:300]}")
    result = json.loads(stdout.decode())
    if result.get("status") != "success":
        raise RuntimeError(result.get("message", "Unknown engine error"))
    b64_part = result["bmp_data_url"].split(",", 1)[1]
    bmp_bytes = base64.b64decode(b64_part)
    from PIL import Image
    with Image.open(BytesIO(bmp_bytes)) as bmp_img:
        png_buf = BytesIO()
        bmp_img.convert("RGBA").save(png_buf, format="PNG", optimize=True)
        return png_buf.getvalue()


# ── Sticker & GIF support ─────────────────────────────────────────────────────

async def sticker_to_png(sticker_bytes: bytes) -> bytes:
    """Convert a WebP (Telegram sticker) to a transparent PNG."""
    from PIL import Image

    def _run() -> bytes:
        img = Image.open(BytesIO(sticker_bytes)).convert("RGBA")
        buf = BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue()

    return await asyncio.to_thread(_run)


async def gif_to_frames(gif_bytes: bytes, max_frames: int = GIF_MAX_FRAMES) -> list[bytes]:
    """Extract up to `max_frames` evenly-spaced frames from an animated GIF as PNGs."""
    from PIL import Image, ImageSequence

    def _run() -> list[bytes]:
        img = Image.open(BytesIO(gif_bytes))
        all_frames = list(ImageSequence.Iterator(img))
        total = len(all_frames)
        if total == 0:
            raise ValueError("GIF contains no frames.")

        # Pick evenly-spaced indices so we always sample the whole animation.
        if total <= max_frames:
            indices = list(range(total))
        else:
            step = total / max_frames
            indices = [int(i * step) for i in range(max_frames)]

        out: list[bytes] = []
        for idx in indices:
            frame = all_frames[idx].convert("RGBA")
            buf = BytesIO()
            frame.save(buf, format="PNG", optimize=True)
            out.append(buf.getvalue())
        return out

    return await asyncio.to_thread(_run)


# ── Background removal ────────────────────────────────────────────────────────
_REMBG_SESSION = None


def _ensure_u2netp_model() -> str:
    if os.path.exists(U2NETP_MODEL_PATH) and os.path.getsize(U2NETP_MODEL_PATH) > 1_000_000:
        return U2NETP_MODEL_PATH
    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        r = client.get(U2NETP_MODEL_URL)
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
        _REMBG_SESSION = ort.InferenceSession(
            _ensure_u2netp_model(), providers=["CPUExecutionProvider"]
        )
    return _REMBG_SESSION


def _u2netp_predict_mask(session, img):
    import numpy as np
    from PIL import Image

    resized = img.resize((320, 320), Image.Resampling.LANCZOS)
    arr = np.array(resized).astype(np.float32)
    arr = arr / max(float(arr.max()), 1e-6)

    mean, std = (0.485, 0.456, 0.406), (0.229, 0.224, 0.225)
    normed = np.zeros((320, 320, 3), dtype=np.float32)
    for c in range(3):
        normed[:, :, c] = (arr[:, :, c] - mean[c]) / std[c]
    input_tensor = np.expand_dims(normed.transpose((2, 0, 1)), 0)

    pred = session.run(None, {session.get_inputs()[0].name: input_tensor})[0][:, 0, :, :]
    ma, mi = float(pred.max()), float(pred.min())
    pred = np.squeeze((pred - mi) / max(ma - mi, 1e-6))

    mask = Image.fromarray((pred * 255).astype("uint8"), mode="L")
    return mask.resize(img.size, Image.Resampling.LANCZOS)


async def remove_background(image_bytes: bytes) -> bytes:
    from PIL import Image, ImageOps

    def _run() -> bytes:
        img = Image.open(BytesIO(image_bytes))
        img.load()
        img = ImageOps.exif_transpose(img).convert("RGB")
        if max(img.size) > REMBG_MAX_DIMENSION:
            img.thumbnail((REMBG_MAX_DIMENSION, REMBG_MAX_DIMENSION), Image.LANCZOS)
        mask = _u2netp_predict_mask(_get_rembg_session(), img)
        out = img.convert("RGBA")
        out.putalpha(mask)
        buf = BytesIO()
        out.save(buf, format="PNG", optimize=True)
        return buf.getvalue()

    try:
        return await asyncio.wait_for(asyncio.to_thread(_run), timeout=REMBG_TIMEOUT_SECONDS)
    except asyncio.TimeoutError as exc:
        raise RuntimeError(
            "Background removal timed out. The first run downloads a model — please retry."
        ) from exc
    except Exception as exc:
        logger.exception("remove_background failed")
        raise RuntimeError(f"Background removal failed: {exc}") from exc


# ── Format conversions ────────────────────────────────────────────────────────

async def convert_image_format(image_bytes: bytes, target_format: str) -> bytes:
    from PIL import Image, ImageOps

    def _run() -> bytes:
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

    def _run() -> bytes:
        img = ImageOps.exif_transpose(Image.open(BytesIO(image_bytes))).convert("RGB")
        out = BytesIO()
        img.save(out, format="PDF", quality=100, resolution=300.0)
        return out.getvalue()

    return await asyncio.to_thread(_run)


async def pdf_to_images(pdf_bytes: bytes, max_pages: int = PDF2IMG_MAX_PAGES) -> list[bytes]:
    import fitz

    def _run() -> list[bytes]:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        try:
            return [
                page.get_pixmap(dpi=200).tobytes("png")
                for i, page in enumerate(doc)
                if i < max_pages
            ]
        finally:
            doc.close()

    return await asyncio.to_thread(_run)


# ── Translation ───────────────────────────────────────────────────────────────

async def translate_text(text: str, target_lang: str) -> str:
    from deep_translator import GoogleTranslator

    return await asyncio.to_thread(
        lambda: GoogleTranslator(source="auto", target=target_lang).translate(text)
    )


# ── Markdown → plain text ─────────────────────────────────────────────────────

def markdown_to_plain_text(md: str) -> str:
    text = md
    text = re.sub(r"!\[.*?\]\(.*?\)", "", text)
    text = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"(\*\*|__)(.*?)\1", r"\2", text, flags=re.DOTALL)
    text = re.sub(r"(?<!\*)\*(?!\*)(.*?)\*(?!\*)", r"\1", text)
    text = re.sub(r"(?<!_)_(?!_)(.*?)_(?!_)", r"\1", text)
    text = re.sub(r"`{1,3}(.*?)`{1,3}", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"^\s*>\s?", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*[-*+]\s+", "- ", text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ── Lummi ─────────────────────────────────────────────────────────────────────

def find_lummi_cid(page_html: str, slug: str) -> str | None:
    soup = BeautifulSoup(page_html, "html.parser")
    scripts = [s.string or s.get_text() for s in soup.find_all("script")]
    candidates = [s for s in scripts if slug in s] + scripts
    for script in candidates:
        for key in ("outpaintAssetPath", "path"):
            m = re.search(rf'{re.escape(key)}\\?":\\?"assets/({LUMMI_CID_RE.pattern})', script)
            if m:
                return m.group(1)
        m = LUMMI_CID_RE.search(script)
        if m:
            return m.group(0)
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        m = LUMMI_CID_RE.search(og["content"])
        if m:
            return m.group(0)
    return None


async def fetch_lummi_asset(url: str) -> dict[str, Any]:
    parsed = urlsplit(url)
    slug_match = re.match(r"^/(?:photo|illustration|3d)/([^/?#]+)", parsed.path, re.IGNORECASE)
    if not slug_match:
        raise ValueError("Unsupported Lummi URL format.")
    slug = unquote(slug_match.group(1))
    client = get_http_client()

    async def _fetch():
        page_r = await client.get(url)
        page_r.raise_for_status()
        cid = find_lummi_cid(page_r.text, slug)
        if not cid:
            raise ValueError("Could not find a direct Lummi asset on the page.")
        direct_url = f"https://assets.lummi.ai/assets/{cid}"
        asset_r = await client.get(direct_url, headers={**HEADERS, "Referer": "https://www.lummi.ai/"})
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
    return {
        "bytes": content,
        "filename": f"lummi_{cid[:8]}.{ext}",
        "direct_url": direct_url,
        "size_mb": len(content) / (1024 * 1024),
    }


# ── Hugeicons ─────────────────────────────────────────────────────────────────

async def fetch_hugeicons_svg(url: str) -> dict[str, str]:
    m = re.search(r"hugeicons\.com/icon/([^?#]+)", url, re.IGNORECASE)
    if not m:
        raise ValueError("Invalid Hugeicons URL.")
    icon_name = unquote(m.group(1)).strip("/")
    sm = re.search(r"[?&]style=([^&]+)", url, re.IGNORECASE)
    style = unquote(sm.group(1)) if sm else "stroke-rounded"
    cdn_url = f"https://cdn.hugeicons.com/icons/{icon_name}-{style}.svg?v=1.0.0"
    client  = get_http_client()

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
    svg = re.sub(r"\s+", " ", svg)
    return svg.replace("> <", ">\n  <").strip()


# ── Shared image-tool runner ──────────────────────────────────────────────────
# Maps tool key → (status label, output filename, async transform)
# Sticker/GIF tools are handled separately because they produce multiple files.

_SINGLE_IMAGE_TOOLS: dict[str, tuple[str, str, Callable]] = {
    "bgremove":    ("Removing background", "background_removed.png", remove_background),
    "img2pdf":     ("Converting to PDF",   "image.pdf",              image_to_pdf),
    "jpg2png":     ("Converting to PNG",   "converted.png",          lambda b: convert_image_format(b, "PNG")),
    "png2jpg":     ("Converting to JPEG",  "converted.jpg",          lambda b: convert_image_format(b, "JPEG")),
    "sticker2png": ("Converting sticker",  "sticker.png",            sticker_to_png),
}

# Human-readable labels for every "awaiting" state, used to warn the user
# when they send the wrong kind of media while a tool is pending.
_AWAITING_LABELS: dict[str, str] = {
    **{k: v[0] for k, v in _SINGLE_IMAGE_TOOLS.items()},
    "gif2frames":     "Extract GIF frames",
    "translate_text": "Translate text",
    "fiver_sanitize": "Sanitize Fiver message",
}


async def _download_file(context: ContextTypes.DEFAULT_TYPE, file_id: str) -> bytes:
    """Download a Telegram file by file_id and return raw bytes."""
    tg_file = await context.bot.get_file(file_id)
    if tg_file.file_size and tg_file.file_size > BOT_DOWNLOAD_LIMIT_BYTES:
        raise ValueError("File exceeds the 20 MB bot download limit.")
    buf = BytesIO()
    await tg_file.download_to_memory(buf)
    return buf.getvalue()


async def _run_image_tool(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    file_id: str,
    tool: str,
) -> None:
    label, filename, transform = _SINGLE_IMAGE_TOOLS[tool]
    status = await update.message.reply_text(f"⏳ {label}…")
    try:
        image_bytes = await _download_file(context, file_id)
        out_bytes   = await transform(image_bytes)
        doc = BytesIO(out_bytes)
        doc.name = filename
        await update.message.reply_document(document=doc, filename=filename)
        await safe_delete(status)
    except Exception as exc:
        logger.warning("%s failed: %s", tool, exc)
        await safe_edit(status, _user_hint(exc))


async def _run_gif_tool(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    file_id: str,
) -> None:
    status = await update.message.reply_text(f"⏳ Extracting GIF frames (up to {GIF_MAX_FRAMES})…")
    try:
        gif_bytes = await _download_file(context, file_id)
        frames    = await gif_to_frames(gif_bytes)
        if not frames:
            await safe_edit(status, "❌ Could not extract any frames from that GIF.")
            return
        await safe_delete(status)
        for batch_start in range(0, len(frames), 10):
            batch = frames[batch_start:batch_start + 10]
            media = [InputMediaPhoto(BytesIO(f)) for f in batch]
            await update.message.reply_media_group(media=media)
        await update.message.reply_text(
            f"✅ Extracted {len(frames)} frame(s) from the GIF."
        )
    except Exception as exc:
        logger.warning("gif2frames failed: %s", exc)
        await safe_edit(status, _user_hint(exc))


# ── Doc converter integration ─────────────────────────────────────────────────
# Imported lazily so the bot starts even if an optional dep is missing.
try:
    from doc_converters import DOC_TOOLS as _DOC_TOOLS
    from doc_converters import pptx_to_images as _pptx_to_images
    _DOC_AVAILABLE = True
except ImportError:
    _DOC_TOOLS = {}
    _DOC_AVAILABLE = False
    logger.warning("doc_converters.py not found — document tools disabled.")

# Which doc tool-keys produce multiple images instead of one file
_MULTI_IMAGE_DOC_TOOLS = {"pptx2images", "pdf2img"}

# Accepted MIME types / extensions per tool — used to validate uploads
_DOC_ACCEPTS: dict[str, tuple[list[str], list[str]]] = {
    # tool_key: ([mime_prefixes...], [extensions...])
    "pdf2docx":    (["application/pdf"],                              [".pdf"]),
    "pdf2txt":     (["application/pdf"],                              [".pdf"]),
    "pdf2img":     (["application/pdf"],                              [".pdf"]),
    "docx2pdf":    (["application/vnd.openxmlformats"],               [".docx"]),
    "docx2txt":    (["application/vnd.openxmlformats"],               [".docx"]),
    "docx2html":   (["application/vnd.openxmlformats"],               [".docx"]),
    "doc2docx":    (["application/msword"],                           [".doc"]),
    "xlsx2pdf":    (["application/vnd.openxmlformats", "application/vnd.ms-excel"], [".xlsx"]),
    "xlsx2csv":    (["application/vnd.openxmlformats", "application/vnd.ms-excel"], [".xlsx"]),
    "xls2xlsx":    (["application/vnd.ms-excel"],                     [".xls"]),
    "csv2xlsx":    (["text/csv", "text/plain"],                       [".csv"]),
    "pptx2pdf":    (["application/vnd.openxmlformats"],               [".pptx"]),
    "pptx2images": (["application/vnd.openxmlformats"],               [".pptx"]),
    "ppt2pptx":    (["application/vnd.ms-powerpoint"],                [".ppt"]),
    "txt2pdf":     (["text/plain"],                                    [".txt"]),
    "html2pdf":    (["text/html"],                                    [".html", ".htm"]),
    "md2pdf":      (["text/plain", "text/markdown"],                  [".md"]),
    "md2txt":      (["text/plain", "text/markdown"],                  [".md"]),
    "md2docx":     (["text/plain", "text/markdown"],                  [".md"]),
    "rtf2txt":     (["text/rtf", "application/rtf"],                  [".rtf"]),
    "rtf2pdf":     (["text/rtf", "application/rtf"],                  [".rtf"]),
    "odt2pdf":     (["application/vnd.oasis"],                        [".odt"]),
    "odt2docx":    (["application/vnd.oasis"],                        [".odt"]),
    "epub2pdf":    (["application/epub+zip"],                         [".epub"]),
}


def _doc_accepts(tool: str, mime: str, filename: str) -> bool:
    """Return True if this file type is acceptable for the given doc tool."""
    if tool not in _DOC_ACCEPTS:
        return True  # unknown tool — let it try
    mimes, exts = _DOC_ACCEPTS[tool]
    fname = filename.lower()
    if any(fname.endswith(e) for e in exts):
        return True
    if any(mime.startswith(m) for m in mimes):
        return True
    return False


async def _run_doc_tool(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    file_id: str,
    tool: str,
) -> None:
    """Run a document conversion tool and send the result back."""
    if not _DOC_AVAILABLE:
        await update.message.reply_text("❌ Document tools are not available on this server.")
        return

    if tool == "pptx2images":
        # Special multi-image path
        status = await update.message.reply_text("⏳ Converting slides to images…")
        try:
            raw = await _download_file(context, file_id)
            pages = await _pptx_to_images(raw)
            if not pages:
                await safe_edit(status, "❌ No slides could be rendered.")
                return
            await safe_delete(status)
            for batch_start in range(0, len(pages), 10):
                batch = pages[batch_start:batch_start + 10]
                await update.message.reply_media_group([InputMediaPhoto(BytesIO(p)) for p in batch])
            await update.message.reply_text(f"✅ {len(pages)} slide(s) converted.")
        except Exception as exc:
            logger.warning("pptx2images failed: %s", exc)
            await safe_edit(status, _user_hint(exc))
        return

    entry = _DOC_TOOLS.get(tool)
    if not entry:
        await update.message.reply_text("❌ Unknown document tool.")
        return

    label, default_filename, handler = entry
    status = await update.message.reply_text(f"⏳ {label}…")
    try:
        raw      = await _download_file(context, file_id)
        result   = await handler(raw)
        out_bytes, out_name = result
        doc = BytesIO(out_bytes)
        doc.name = out_name
        await update.message.reply_document(document=doc, filename=out_name)
        await safe_delete(status)
    except Exception as exc:
        logger.warning("%s failed: %s", tool, exc)
        await safe_edit(status, _user_hint(exc))


# ── Telegram Handlers ─────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        context.user_data.pop("awaiting", None)
        await update.message.reply_text(
            WELCOME_MESSAGE,
            parse_mode="Markdown",
            reply_markup=build_main_menu_keyboard(),
        )


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(
            MENU_INTRO, parse_mode="Markdown",
            reply_markup=build_main_menu_keyboard(),
        )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        had_task = context.user_data.pop("awaiting", None) is not None
        context.user_data.pop("target_lang", None)
        await update.message.reply_text("✅ Cancelled." if had_task else "Nothing to cancel.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(
            "📖 *Supported inputs:*\n\n"
            "*Links:*\n"
            "• `https://www.lummi.ai/photo/...`\n"
            "• `https://www.lummi.ai/illustration/...`\n"
            "• `https://www.lummi.ai/3d/...`\n"
            "• `https://hugeicons.com/icon/...`\n\n"
            "*Images:*\n"
            "Send any photo → choose from 32 effects!\n\n"
            "*Sticker / GIF:*\n"
            "Send a sticker → get a transparent PNG\n"
            "Send a GIF → extract up to 10 frames as images\n\n"
            "*Fiver Message Sanitizer:*\n"
            "`/FiverMessage <text>` → sanitize immediately\n"
            "`/FiverMessage` alone → I'll ask for the text next\n\n"
            "*Tools (via /menu):*\n"
            "🧹 Remove BG • 🌐 Translate text\n"
            "📄 PDF → Images • 🖼 Image → PDF\n"
            "🔁 JPEG ↔ PNG • 📝 MD → TXT\n"
            "😄 Sticker → PNG • 🎞 GIF → Frames\n"
            "🛡 Fiver Message Sanitizer\n\n"
            "*Effects:*\n" + "  ".join(label for _, label, _ in EFFECTS),
            parse_mode="Markdown",
        )


async def fivermessage_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /FiverMessage <text>  → sanitizes inline immediately.
    /FiverMessage (alone) → asks the user to send text next.
    """
    if not update.message:
        return
    args_text = " ".join(context.args) if context.args else ""
    if args_text.strip():
        if len(args_text) > FIVER_SANITIZE_MAX_CHARS:
            await update.message.reply_text(
                f"⚠️ Too long ({len(args_text)} chars). Limit: {FIVER_SANITIZE_MAX_CHARS}."
            )
            return
        sanitized = sanitize_fiver_text(args_text.strip())
        await update.message.reply_text(f"✅ *Sanitized:*\n\n{sanitized}", parse_mode="Markdown")
        return

    context.user_data["awaiting"] = "fiver_sanitize"
    await update.message.reply_text(
        "🛡️ Send me the message you want to sanitize (or /cancel)."
    )


# ── Sticker handler ───────────────────────────────────────────────────────────

async def handle_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Telegram sticker received — convert WebP to PNG automatically."""
    if not update.message or not update.message.sticker:
        return

    awaiting = context.user_data.get("awaiting")
    if awaiting and awaiting != "sticker2png":
        await _warn_wrong_media(update, context, awaiting, "sticker")
        return

    sticker = update.message.sticker
    if sticker.is_animated or sticker.is_video:
        await update.message.reply_text(
            "⚠️ Animated/video stickers can't be converted to a static PNG yet. "
            "Please send a regular (static) sticker."
        )
        return
    await _run_image_tool(update, context, sticker.file_id, "sticker2png")
    context.user_data.pop("awaiting", None)


# ── Photo handler ─────────────────────────────────────────────────────────────

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.photo:
        return
    awaiting = context.user_data.get("awaiting")
    file_id  = update.message.photo[-1].file_id

    if awaiting in _SINGLE_IMAGE_TOOLS:
        await _run_image_tool(update, context, file_id, awaiting)
        context.user_data.pop("awaiting", None)
        return

    if awaiting and awaiting not in ("effects",):
        # A non-image tool (doc conversion, gif, translate, fiver sanitize, …)
        # is pending — warn instead of silently launching the effect picker.
        await _warn_wrong_media(update, context, awaiting, "photo")
        return

    # Default (or explicit "effects" awaiting): 32-effect picker
    context.user_data.pop("awaiting", None)
    token = _store_token(context.bot_data, file_id)
    await update.message.reply_text(
        "🎨 *Choose a category:*",
        parse_mode="Markdown",
        reply_markup=build_effect_categories_keyboard(token),
    )


async def _warn_wrong_media(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    awaiting: str,
    got_kind: str,
) -> None:
    label = _AWAITING_LABELS.get(awaiting, awaiting)
    await update.message.reply_text(
        f"⚠️ I'm waiting for input for *{label}*, but got a {got_kind}.\n"
        f"Send the right file/text, or /cancel to stop that task.",
        parse_mode="Markdown",
    )


# ── Document handler ──────────────────────────────────────────────────────────

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.document:
        return
    doc      = update.message.document
    awaiting = context.user_data.get("awaiting")
    file_name = (doc.file_name or "").lower()
    mime      = doc.mime_type or ""

    # ── No tool selected yet: smart hint based on file type ──────────────────
    if not awaiting:
        _EXT_HINTS = {
            ".pdf":  "📄 PDF detected! Go to /menu → *PDF Tools* to convert it.",
            ".docx": "📝 DOCX detected! Go to /menu → *Word / Doc* to convert it.",
            ".doc":  "📄 DOC detected! Go to /menu → *Word / Doc* → DOC → DOCX.",
            ".xlsx": "📊 XLSX detected! Go to /menu → *Spreadsheet* to convert it.",
            ".xls":  "📊 XLS detected! Go to /menu → *Spreadsheet* → XLS → XLSX.",
            ".csv":  "📃 CSV detected! Go to /menu → *Spreadsheet* → CSV → XLSX.",
            ".pptx": "📽 PPTX detected! Go to /menu → *Presentation* to convert it.",
            ".ppt":  "📽 PPT detected! Go to /menu → *Presentation* → PPT → PPTX.",
            ".md":   "📝 Markdown detected! Go to /menu → *Text & Markup* to convert it.",
            ".txt":  "📃 TXT detected! Go to /menu → *Text & Markup* → TXT → PDF.",
            ".html": "🌐 HTML detected! Go to /menu → *Text & Markup* → HTML → PDF.",
            ".rtf":  "📄 RTF detected! Go to /menu → *Text & Markup* to convert it.",
            ".odt":  "📄 ODT detected! Go to /menu → *Word / Doc* to convert it.",
            ".epub": "📚 EPUB detected! Go to /menu → *More →* → EPUB → PDF.",
            ".gif":  f"🎞 GIF detected! Go to /menu → GIF → Frames (up to {GIF_MAX_FRAMES} frames).",
        }
        for ext, hint in _EXT_HINTS.items():
            if file_name.endswith(ext):
                await update.message.reply_text(hint, parse_mode="Markdown",
                                                reply_markup=build_main_menu_keyboard())
                return
        await update.message.reply_text(
            "Please choose a tool first with /menu, then send the file.",
            reply_markup=build_main_menu_keyboard(),
        )
        return

    # ── Image-only tools (no file-type check needed — Pillow handles it) ──────
    if awaiting in _SINGLE_IMAGE_TOOLS:
        if not (mime.startswith("image/") or any(
            file_name.endswith(e) for e in (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".gif")
        )):
            await update.message.reply_text("⚠️ That doesn't look like an image file. Please send an image.")
            return
        await _run_image_tool(update, context, doc.file_id, awaiting)
        context.user_data.pop("awaiting", None)
        return

    # ── GIF frames ────────────────────────────────────────────────────────────
    if awaiting == "gif2frames":
        if not (file_name.endswith(".gif") or mime == "image/gif"):
            await update.message.reply_text("⚠️ Please send a .gif file.")
            return
        await _run_gif_tool(update, context, doc.file_id)
        context.user_data.pop("awaiting", None)
        return

    # ── PDF → Images (existing fast path using fitz) ──────────────────────────
    if awaiting == "pdf2img":
        if not (file_name.endswith(".pdf") or mime == "application/pdf"):
            await update.message.reply_text("⚠️ Please send a .pdf file.")
            return
        status = await update.message.reply_text("⏳ Rendering PDF pages…")
        try:
            pdf_bytes = await _download_file(context, doc.file_id)
            pages     = await pdf_to_images(pdf_bytes)
            if not pages:
                await safe_edit(status, "❌ Couldn't render any pages from that PDF.")
                return
            await safe_delete(status)
            for batch_start in range(0, len(pages), 10):
                batch = pages[batch_start:batch_start + 10]
                await update.message.reply_media_group([InputMediaPhoto(BytesIO(p)) for p in batch])
            if len(pages) >= PDF2IMG_MAX_PAGES:
                await update.message.reply_text(f"ℹ️ Only the first {PDF2IMG_MAX_PAGES} pages were rendered.")
        except Exception as exc:
            logger.warning("pdf2img failed: %s", exc)
            await safe_edit(status, _user_hint(exc))
        finally:
            context.user_data.pop("awaiting", None)
        return

    # ── MD → TXT (fast pure-Python path, no subprocess needed) ───────────────
    if awaiting == "md2txt":
        if not file_name.endswith(".md"):
            await update.message.reply_text("⚠️ Please send a .md (Markdown) file.")
            return
        status = await update.message.reply_text("⏳ Converting…")
        try:
            md_bytes = await _download_file(context, doc.file_id)
            plain    = markdown_to_plain_text(md_bytes.decode("utf-8", errors="replace"))
            out_name = re.sub(r"\.md$", ".txt", doc.file_name or "output.md", flags=re.IGNORECASE)
            out_doc  = BytesIO(plain.encode("utf-8"))
            out_doc.name = out_name
            await update.message.reply_document(document=out_doc, filename=out_name)
            await safe_delete(status)
        except Exception as exc:
            logger.warning("md2txt failed: %s", exc)
            await safe_edit(status, _user_hint(exc))
        finally:
            context.user_data.pop("awaiting", None)
        return

    # ── Fiver sanitizer: a document was sent instead of plain text ───────────
    if awaiting == "fiver_sanitize":
        if file_name.endswith(".txt") or mime == "text/plain":
            status = await update.message.reply_text("⏳ Sanitizing…")
            try:
                raw = await _download_file(context, doc.file_id)
                text = raw.decode("utf-8", errors="replace")
                if len(text) > FIVER_SANITIZE_MAX_CHARS:
                    await safe_edit(status, f"⚠️ Too long ({len(text)} chars). Limit: {FIVER_SANITIZE_MAX_CHARS}.")
                    return
                sanitized = sanitize_fiver_text(text)
                out_doc = BytesIO(sanitized.encode("utf-8"))
                out_doc.name = "sanitized.txt"
                await update.message.reply_document(document=out_doc, filename="sanitized.txt")
                await safe_delete(status)
            except Exception as exc:
                logger.warning("fiver_sanitize (file) failed: %s", exc)
                await safe_edit(status, _user_hint(exc))
            finally:
                context.user_data.pop("awaiting", None)
        else:
            await update.message.reply_text("⚠️ Please send a .txt file, or just type the message directly.")
        return

    # ── All other document tools (routed through doc_converters) ──────────────
    if awaiting in _DOC_TOOLS or awaiting == "pptx2images":
        if not _doc_accepts(awaiting, mime, file_name):
            exts = " / ".join(_DOC_ACCEPTS.get(awaiting, ([], [".file"]))[1])
            await update.message.reply_text(
                f"⚠️ That file type doesn't match this tool. Expected: `{exts}`",
                parse_mode="Markdown",
            )
            return
        await _run_doc_tool(update, context, doc.file_id, awaiting)
        context.user_data.pop("awaiting", None)
        return

    await update.message.reply_text(
        "Please choose a tool first with /menu.",
        reply_markup=build_main_menu_keyboard(),
    )


# ── Animation / GIF handler ───────────────────────────────────────────────────

async def handle_animation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Telegram sends GIFs as 'animation' objects — handle them here."""
    if not update.message or not update.message.animation:
        return
    awaiting = context.user_data.get("awaiting")

    if awaiting and awaiting != "gif2frames":
        await _warn_wrong_media(update, context, awaiting, "GIF")
        return

    if awaiting == "gif2frames":
        await _run_gif_tool(update, context, update.message.animation.file_id)
        context.user_data.pop("awaiting", None)
    else:
        # Auto-extract without needing the user to select the tool first.
        await update.message.reply_text(
            f"🎞 GIF detected! Extracting up to {GIF_MAX_FRAMES} frames…"
        )
        await _run_gif_tool(update, context, update.message.animation.file_id)


# ── Callback handlers ─────────────────────────────────────────────────────────

async def handle_effect_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    try:
        _, effect_key, token = query.data.split("|", 2)
    except ValueError:
        await query.edit_message_text("❌ Invalid selection.")
        return

    file_id = _get_file_id(context.bot_data, token)
    if not file_id:
        await query.edit_message_text("❌ Request expired — please resend the photo.")
        return

    effect_label = EFFECT_NAMES.get(effect_key, effect_key)
    status_msg = await query.edit_message_text(
        f"⏳ Applying *{effect_label}*… please wait.", parse_mode="Markdown"
    )
    try:
        await context.bot.send_chat_action(chat_id=query.message.chat_id, action=ChatAction.UPLOAD_PHOTO)
        image_bytes = await _download_file(context, file_id)
        w, h = _get_image_dimensions(image_bytes)
        png_bytes = await apply_effect_to_image(image_bytes, effect_key, w, h)
        doc = BytesIO(png_bytes)
        doc.name = f"{effect_key}.png"
        await query.message.reply_document(
            document=doc, filename=f"{effect_key}.png",
            caption=truncate_caption(f"✅ {effect_label} applied ({w}×{h}px)"),
        )
        await safe_edit(status_msg, f"✅ *{effect_label}* done!", parse_mode="Markdown")
        _drop_token(context.bot_data, token)
    except asyncio.TimeoutError:
        await safe_edit(status_msg, "⏱ Timed out — try a smaller image.")
    except Exception as exc:
        logger.warning("Effect %s failed: %s", effect_key, exc)
        await safe_edit(status_msg, _user_hint(exc))


async def handle_effect_category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    try:
        _, category, token = query.data.split("|", 2)
    except ValueError:
        await query.edit_message_text("❌ Invalid selection.")
        return
    if not _get_file_id(context.bot_data, token):
        await query.edit_message_text("❌ Request expired — please resend the photo.")
        return
    cat_label = EFFECT_CATEGORIES.get(category, category)
    await query.edit_message_text(
        f"🎨 *{cat_label}* — choose an effect:",
        parse_mode="Markdown",
        reply_markup=build_effect_keyboard(category, token),
    )


async def handle_effect_back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    try:
        _, token = query.data.split("|", 1)
    except ValueError:
        await query.edit_message_text("❌ Invalid selection.")
        return
    if not _get_file_id(context.bot_data, token):
        await query.edit_message_text("❌ Request expired — please resend the photo.")
        return
    await query.edit_message_text(
        "🎨 *Choose a category:*",
        parse_mode="Markdown",
        reply_markup=build_effect_categories_keyboard(token),
    )


async def handle_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    try:
        _, action = query.data.split("|", 1)
    except ValueError:
        await query.edit_message_text("❌ Invalid selection.")
        return

    # Prompt messages for every tool (action → (awaiting_key, user_message))
    _TOOL_PROMPTS: dict[str, tuple[str, str]] = {
        # Image
        "effects":     ("effects",    "🎨 Send me a *photo* and pick an effect."),
        "bgremove":    ("bgremove",   "🧹 Send me a *photo* — I'll remove its background."),
        "sticker2png": ("sticker2png","😄 Send me a *static sticker* and I'll convert it to PNG."),
        "gif2frames":  ("gif2frames", f"🎞 Send me a *GIF* and I'll extract up to {GIF_MAX_FRAMES} frames."),
        "img2pdf":     ("img2pdf",    "🖼 Send me an *image* (photo or file) and I'll wrap it into a PDF."),
        "pdf2img":     ("pdf2img",    f"📄 Send me a *PDF file* — I'll render up to {PDF2IMG_MAX_PAGES} pages as images."),
        "jpg2png":     ("jpg2png",    "🔁 Send me a *JPEG image* as a *file* for best quality."),
        "png2jpg":     ("png2jpg",    "🔁 Send me a *PNG image* as a *file*."),
        # Fiver sanitizer
        "fiversanitize": ("fiver_sanitize", "🛡️ Send me the message you want to sanitize."),
        # PDF
        "pdf2docx":    ("pdf2docx",   "📄 Send me a *PDF file* → I'll convert it to DOCX."),
        "pdf2txt":     ("pdf2txt",    "📄 Send me a *PDF file* → I'll extract plain text."),
        # Word
        "docx2pdf":    ("docx2pdf",   "📝 Send me a *.docx file* → I'll convert it to PDF."),
        "docx2txt":    ("docx2txt",   "📝 Send me a *.docx file* → I'll extract plain text."),
        "docx2html":   ("docx2html",  "📝 Send me a *.docx file* → I'll convert it to HTML."),
        "doc2docx":    ("doc2docx",   "📄 Send me a *.doc file* → I'll convert it to DOCX."),
        "odt2pdf":     ("odt2pdf",    "📄 Send me an *.odt file* → I'll convert it to PDF."),
        "odt2docx":    ("odt2docx",   "📄 Send me an *.odt file* → I'll convert it to DOCX."),
        # Spreadsheet
        "xlsx2pdf":    ("xlsx2pdf",   "📊 Send me an *.xlsx file* → I'll convert it to PDF."),
        "xlsx2csv":    ("xlsx2csv",   "📊 Send me an *.xlsx file* → I'll export the first sheet as CSV."),
        "csv2xlsx":    ("csv2xlsx",   "📃 Send me a *.csv file* → I'll build an XLSX spreadsheet."),
        "xls2xlsx":    ("xls2xlsx",   "📊 Send me an *.xls file* → I'll convert it to XLSX."),
        # Presentation
        "pptx2pdf":    ("pptx2pdf",   "📽 Send me a *.pptx file* → I'll convert it to PDF."),
        "pptx2images": ("pptx2images","📽 Send me a *.pptx file* → I'll render each slide as an image."),
        "ppt2pptx":    ("ppt2pptx",   "📽 Send me a *.ppt file* → I'll convert it to PPTX."),
        # Text / Markup
        "txt2pdf":     ("txt2pdf",    "📃 Send me a *.txt file* → I'll convert it to PDF."),
        "html2pdf":    ("html2pdf",   "🌐 Send me an *.html file* → I'll render it as PDF."),
        "md2pdf":      ("md2pdf",     "📝 Send me a *.md file* → I'll convert it to PDF."),
        "md2txt":      ("md2txt",     "📝 Send me a *.md file* → I'll convert it to plain TXT."),
        "md2docx":     ("md2docx",    "📝 Send me a *.md file* → I'll convert it to DOCX."),
        "rtf2txt":     ("rtf2txt",    "📄 Send me a *.rtf file* → I'll strip it to plain text."),
        "rtf2pdf":     ("rtf2pdf",    "📄 Send me a *.rtf file* → I'll convert it to PDF."),
        # eBook
        "epub2pdf":    ("epub2pdf",   "📚 Send me an *.epub file* → I'll convert it to PDF."),
    }

    if action == "translate":
        await query.edit_message_text(
            "🌐 *Choose the target language:*",
            parse_mode="Markdown",
            reply_markup=build_translate_lang_keyboard(),
        )
        return

    if action == "effects":
        context.user_data["awaiting"] = "effects"
        await query.edit_message_text("🎨 Send me a *photo* and pick an effect.", parse_mode="Markdown")
        return

    if action in _TOOL_PROMPTS:
        tool_key, msg = _TOOL_PROMPTS[action]
        context.user_data["awaiting"] = tool_key
        await query.edit_message_text(msg, parse_mode="Markdown")
    else:
        await query.edit_message_text("❌ Unknown option.")


async def handle_category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """User tapped a category button in the main menu."""
    query = update.callback_query
    await query.answer()
    try:
        _, cat = query.data.split("|", 1)
    except ValueError:
        await query.edit_message_text("❌ Invalid selection.")
        return

    if cat == "back":
        await query.edit_message_text(
            MENU_INTRO, parse_mode="Markdown",
            reply_markup=build_main_menu_keyboard(),
        )
        return

    cat_labels = {
        "image": "🖼 Image Tools",
        "pdf":   "📄 PDF Tools",
        "word":  "📝 Word / Doc",
        "sheet": "📊 Spreadsheet",
        "ppt":   "📽 Presentation",
        "text":  "✍️ Text & Markup",
        "more":  "📚 More Tools",
    }
    label = cat_labels.get(cat, cat.title())
    await query.edit_message_text(
        f"*{label}* — choose a conversion:",
        parse_mode="Markdown",
        reply_markup=build_category_keyboard(cat),
    )


async def handle_translate_lang_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    try:
        _, lang_code = query.data.split("|", 1)
    except ValueError:
        await query.edit_message_text("❌ Invalid selection.")
        return
    lang_label = next((label for code, label in TRANSLATE_LANGUAGES if code == lang_code), lang_code)
    context.user_data["awaiting"]    = "translate_text"
    context.user_data["target_lang"] = lang_code
    await query.edit_message_text(
        f"✏️ Send me the text to translate to *{lang_label}*.", parse_mode="Markdown"
    )


# ── Text message handler ──────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    raw_text = update.message.text

    if GREETING_RE.match(raw_text.strip()):
        context.user_data.pop("awaiting", None)
        await update.message.reply_text(
            WELCOME_MESSAGE, parse_mode="Markdown",
            reply_markup=build_main_menu_keyboard(),
        )
        return

    # ── Fiver message sanitizer (guided flow) ─────────────────────────────────
    if context.user_data.get("awaiting") == "fiver_sanitize":
        text_to_sanitize = raw_text.strip()
        if not text_to_sanitize:
            await update.message.reply_text("Please send some text to sanitize.")
            return
        if len(text_to_sanitize) > FIVER_SANITIZE_MAX_CHARS:
            await update.message.reply_text(
                f"⚠️ Too long ({len(text_to_sanitize)} chars). Limit: {FIVER_SANITIZE_MAX_CHARS}."
            )
            return
        sanitized = sanitize_fiver_text(text_to_sanitize)
        await update.message.reply_text(f"✅ *Sanitized:*\n\n{sanitized}", parse_mode="Markdown")
        context.user_data.pop("awaiting", None)
        return

    if context.user_data.get("awaiting") == "translate_text":
        target_lang   = context.user_data.get("target_lang", "en")
        text_to_xlate = raw_text.strip()
        if not text_to_xlate:
            await update.message.reply_text("Please send some text to translate.")
            return
        if len(text_to_xlate) > TRANSLATE_MAX_CHARS:
            await update.message.reply_text(
                f"⚠️ Too long ({len(text_to_xlate)} chars). Limit: {TRANSLATE_MAX_CHARS}."
            )
            return
        status = await update.message.reply_text("🌐 Translating…")
        try:
            translated = await translate_text(text_to_xlate, target_lang)
            await safe_edit(status, f"✅ *Translation:*\n\n{translated}", parse_mode="Markdown")
        except Exception as exc:
            logger.warning("Translation failed: %s", exc)
            await safe_edit(status, "❌ Translation failed. Please try again.")
        context.user_data.pop("awaiting", None)
        context.user_data.pop("target_lang", None)
        return

    # A non-text tool (image/doc/gif) is pending and the user sent plain text
    # instead — warn rather than silently falling through to link detection.
    awaiting = context.user_data.get("awaiting")
    if awaiting and awaiting not in ("translate_text", "fiver_sanitize"):
        await _warn_wrong_media(update, context, awaiting, "text message")
        return

    # Link detection.
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
        await update.message.reply_text(
            "Please send a supported Lummi.ai or Hugeicons link, "
            "or send a photo / sticker / GIF to use an effect, "
            "or use /FiverMessage to sanitize text. "
            "Use /help for examples."
        )
        return

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_DOCUMENT
    )
    status = await update.message.reply_text("⏳ Processing your link…")

    if platform == "lummi":
        await _process_lummi(update, status, url)
    else:
        await _process_hugeicons(update, status, url)


async def _process_lummi(update: Update, status: Any, url: str) -> None:
    try:
        await safe_edit(status, "⏳ Downloading the Lummi asset…")
        result = await fetch_lummi_asset(url)
        await safe_edit(status, "📤 Sending full-size Lummi asset…")
        doc = BytesIO(result["bytes"])
        doc.name = result["filename"]
        await update.message.reply_document(
            document=doc, filename=result["filename"],
            caption=truncate_caption(
                f"✅ Full-size image ({result['size_mb']:.2f} MB)\n"
                f"Direct link: {result['direct_url']}"
            ),
        )
        await safe_delete(status)
    except Exception as exc:
        logger.warning("Lummi failed: %s", exc)
        await safe_edit(status, _user_hint(exc))


async def _process_hugeicons(update: Update, status: Any, url: str) -> None:
    try:
        await safe_edit(status, "⏳ Fetching the Hugeicons SVG…")
        result    = await fetch_hugeicons_svg(url)
        clean_svg = format_svg(result["svg"])
        icon_name = result["icon_name"]
        style     = result["style"]
        filename  = f"{icon_name}-{style}.svg"
        await safe_delete(status)
        label = f"✅ *{markdown_v2_escape(icon_name)}* \\({markdown_v2_escape(style)}\\)"
        await update.message.reply_text(label, parse_mode="MarkdownV2")
        await update.message.reply_text(
            f"```xml\n{markdown_code_escape(clean_svg)}\n```", parse_mode="MarkdownV2"
        )
        doc = BytesIO(clean_svg.encode("utf-8"))
        doc.name = filename
        await update.message.reply_document(
            document=doc, filename=filename,
            caption=truncate_caption(f"{filename} — ready to use."),
        )
    except Exception as exc:
        logger.warning("Hugeicons failed: %s", exc)
        await safe_edit(status, _user_hint(exc))


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled exception:", exc_info=context.error)


# ── Lifecycle hooks ────────────────────────────────────────────────────────────

async def _on_shutdown(app: Application) -> None:
    """Release the pooled HTTP client and drop any pending file tokens."""
    await close_http_client()
    app.bot_data.pop("pending_files", None)
    logger.info("Shutdown cleanup complete: HTTP client closed, pending_files cleared.")


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
        .concurrent_updates(True)
        .post_shutdown(_on_shutdown)
        .build()
    )

    # Commands
    app.add_handler(CommandHandler("start",  start))
    app.add_handler(CommandHandler("help",   help_command))
    app.add_handler(CommandHandler("menu",   menu_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("FiverMessage", fivermessage_command))

    # Media
    app.add_handler(MessageHandler(filters.PHOTO,     handle_photo))
    app.add_handler(MessageHandler(filters.Sticker.ALL, handle_sticker))
    app.add_handler(MessageHandler(filters.ANIMATION, handle_animation))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    # Callbacks
    app.add_handler(CallbackQueryHandler(handle_effect_category_callback, pattern=r"^fxcat\|"))
    app.add_handler(CallbackQueryHandler(handle_effect_back_callback,     pattern=r"^fxback\|"))
    app.add_handler(CallbackQueryHandler(handle_effect_callback,          pattern=r"^fx\|"))
    app.add_handler(CallbackQueryHandler(handle_menu_callback,            pattern=r"^menu\|"))
    app.add_handler(CallbackQueryHandler(handle_category_callback,        pattern=r"^cat\|"))
    app.add_handler(CallbackQueryHandler(handle_translate_lang_callback,  pattern=r"^trlang\|"))

    # Text
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Errors
    app.add_error_handler(error_handler)

    logger.info("Unified bot starting (Lummi + Hugeicons + 32 Effects + Sticker/GIF + Fiver Sanitizer)")

    if webhook_url:
        logger.info("Webhook mode on port %s", port)
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path="/webhook",
            webhook_url=f"{webhook_url}/webhook",
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        logger.info("Polling mode")
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
