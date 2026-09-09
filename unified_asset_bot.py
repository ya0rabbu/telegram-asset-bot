"""Unified Lummi AI + Hugeicons + Image Effects Telegram bot."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import uuid
from io import BytesIO
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx
from bs4 import BeautifulSoup
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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
MAX_UPLOAD_BYTES = 49 * 1024 * 1024
MAX_CAPTION_LENGTH = 1024
REQUEST_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=10.0)

ENGINE_SCRIPT = os.path.join(os.path.dirname(__file__), "python_engine.py")

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
URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
LUMMI_CID_RE = re.compile(r"Qm[1-9A-HJ-NP-Za-km-z]{44}")

# Max number of pending file tokens to keep in memory at once (simple bound
# to avoid unbounded growth if users request photos but never tap a button).
MAX_PENDING_FILES = 500

# ── 32 Effects definition ─────────────────────────────────────────────────────
EFFECTS: list[tuple[str, str]] = [
    ("halftone-dots",       "🔴 Halftone"),
    ("comic-cmyk",          "🎨 Pop-Art"),
    ("retro-8bit",          "📺 8-Bit CRT"),
    ("cinematic-noir",      "🎬 Noir"),
    ("cyber-glitch",        "⚡ Cyberpunk"),
    ("thermal-flir",        "🌡 Thermal"),
    ("blueprint-cyan",      "📐 Blueprint"),
    ("ascii-matrix",        "💻 ASCII"),
    ("sobel-neon",          "✨ Neon Edge"),
    ("risograph-duo",       "🖨 Risograph"),
    ("crosshatch-engraving","✏️ Crosshatch"),
    ("bayer-dither",        "🎲 Dither"),
    ("watercolor",          "🌊 Watercolor"),
    ("oil-paint",           "🖼 Oil Paint"),
    ("vaporwave",           "🌈 Vaporwave"),
    ("vhs-tape",            "🎞 VHS Tape"),
    ("autumn-tone",         "🍂 Autumn"),
    ("frozen-ice",          "❄️ Frozen"),
    ("moonlight",           "🌙 Moonlight"),
    ("duotone",             "🎭 Duotone"),
    ("emboss",              "🪨 Emboss"),
    ("pixelate",            "🔬 Pixelate"),
    ("swirl-distort",       "🌀 Swirl"),
    ("lomography",          "📸 Lomography"),
    ("glitch-art",          "🎪 Glitch Art"),
    ("forest-green",        "🌿 Forest"),
    ("inferno",             "🔥 Inferno"),
    ("horror-red",          "🩸 Horror"),
    ("cherry-blossom",      "🌸 Cherry"),
    ("desert-sand",         "🏜 Desert"),
    ("neon-poster",         "🎨 Neon Poster"),
    ("mirror-reflect",      "🪞 Mirror"),
]

EFFECT_NAMES = {key: label for key, label in EFFECTS}

WELCOME_MESSAGE = (
    "👋 Welcome!\n\n"
    "Send me:\n"
    "🔗 A *Lummi.ai* photo/illustration/3D link\n"
    "🔗 A *Hugeicons* icon link\n"
    "🖼 A *photo* to apply one of 32 image effects\n\n"
    "Use /help for more info."
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def trim_url(url: str) -> str:
    return url.rstrip(".,!?;:)]}>\"'")


def truncate_caption(caption: str) -> str:
    if len(caption) <= MAX_CAPTION_LENGTH:
        return caption
    return caption[:MAX_CAPTION_LENGTH - 3] + "..."


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


def detect_platform(url: str) -> str | None:
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    if host == "lummi.ai" and re.match(
        r"^/(?:photo|illustration|3d)/[^/]+", urlsplit(url).path, re.IGNORECASE
    ):
        return "lummi"
    if host == "hugeicons.com" and urlsplit(url).path.lower().startswith("/icon/"):
        return "hugeicons"
    return None


def markdown_v2_escape(text: str) -> str:
    return re.sub(r"([_\*\[\]\(\)~`>#+\-=|{}.!\\])", r"\\\1", text)


def markdown_code_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("`", "\\`")


# ── Effect Keyboard ──────────────────────────────────────────────────────────

def build_effect_keyboard(token: str) -> InlineKeyboardMarkup:
    """Build 4-column inline keyboard with all 32 effects.

    `token` is a short opaque id referencing the photo's real file_id,
    which is stored separately (see handle_photo). This keeps
    callback_data well under Telegram's 64-byte limit, since raw
    Telegram file_ids are frequently 80-100+ characters long and would
    otherwise trigger `Button_data_invalid` once combined with an
    effect key.
    """
    buttons = []
    row = []
    for i, (key, label) in enumerate(EFFECTS):
        row.append(InlineKeyboardButton(label, callback_data=f"fx|{key}|{token}"))
        if len(row) == 4:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(buttons)


# ── Image Effect Engine ──────────────────────────────────────────────────────

async def apply_effect_to_image(
    image_bytes: bytes,
    effect: str,
    width: int,
    height: int,
) -> bytes:
    """
    Converts image bytes to RGBA, calls python_engine.py via subprocess,
    and returns BMP bytes.
    """
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
    stdout, stderr = await asyncio.wait_for(
        proc.communicate(input=payload.encode()),
        timeout=120,
    )

    if proc.returncode != 0:
        raise RuntimeError(f"Engine error: {stderr.decode()[:300]}")

    result = json.loads(stdout.decode())
    if result.get("status") != "success":
        raise RuntimeError(result.get("message", "Unknown engine error"))

    data_url: str = result["bmp_data_url"]
    b64_part = data_url.split(",", 1)[1]
    return base64.b64decode(b64_part)


async def _image_to_rgba_b64(image_bytes: bytes, width: int, height: int) -> str:
    """
    Use Pillow if available (most servers have it), otherwise fall back
    to a gradient test pattern. Returns base64-encoded raw RGBA bytes.
    """
    try:
        from PIL import Image  # type: ignore
        img = Image.open(BytesIO(image_bytes)).convert("RGBA").resize((width, height))
        rgba_bytes = img.tobytes()
        return base64.b64encode(rgba_bytes).decode()
    except ImportError:
        pass

    raw = bytearray(width * height * 4)
    for y in range(height):
        for x in range(width):
            idx = (y * width + x) * 4
            raw[idx] = (x * 255) // max(1, width - 1)
            raw[idx+1] = (y * 255) // max(1, height - 1)
            raw[idx+2] = 128
            raw[idx+3] = 255
    return base64.b64encode(bytes(raw)).decode()


def _get_image_dimensions(image_bytes: bytes) -> tuple[int, int]:
    """Return (width, height) capped at 400px, preserving aspect ratio."""
    MAX_DIM = 400
    try:
        from PIL import Image  # type: ignore
        img = Image.open(BytesIO(image_bytes))
        w, h = img.size
        if w > MAX_DIM or h > MAX_DIM:
            ratio = min(MAX_DIM / w, MAX_DIM / h)
            w, h = int(w * ratio), int(h * ratio)
        return w, h
    except Exception:
        return 300, 300


# ── Lummi ────────────────────────────────────────────────────────────────────

def find_lummi_cid(page_html: str, slug: str) -> str | None:
    soup = BeautifulSoup(page_html, "html.parser")
    scripts = [s.string or s.get_text() for s in soup.find_all("script")]
    candidates = [s for s in scripts if slug in s] + scripts
    for script in candidates:
        for key in ("outpaintAssetPath", "path"):
            m = re.search(
                rf'{re.escape(key)}\\?":\\?"assets/({LUMMI_CID_RE.pattern})', script
            )
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
        raise ValueError("The Lummi URL format is not supported.")
    slug = unquote(slug_match.group(1))
    async with httpx.AsyncClient(headers=HEADERS, timeout=REQUEST_TIMEOUT, follow_redirects=True) as client:
        page_r = await client.get(url)
        page_r.raise_for_status()
        cid = find_lummi_cid(page_r.text, slug)
        if not cid:
            raise ValueError("Could not find a direct Lummi asset on the page.")
        direct_url = f"https://assets.lummi.ai/assets/{cid}"
        asset_r = await client.get(direct_url, headers={**HEADERS, "Referer": "https://www.lummi.ai/"})
        asset_r.raise_for_status()
        content = asset_r.content
    if not content:
        raise ValueError("The Lummi asset download was empty.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError("The Lummi file is larger than Telegram's upload limit.")
    ct = asset_r.headers.get("content-type", "image/jpeg").split(";", 1)[0].lower()
    ext = {"image/jpeg":"jpg","image/png":"png","image/webp":"webp","image/gif":"gif","image/tiff":"tiff"}.get(ct, "jpg")
    return {
        "bytes": content,
        "filename": f"lummi_{cid[:8]}.{ext}",
        "direct_url": direct_url,
        "size_mb": len(content) / (1024 * 1024),
    }


# ── Hugeicons ────────────────────────────────────────────────────────────────

async def fetch_hugeicons_svg(url: str) -> dict[str, str]:
    m = re.search(r"hugeicons\.com/icon/([^?#]+)", url, re.IGNORECASE)
    if not m:
        raise ValueError("Invalid Hugeicons URL.")
    icon_name = unquote(m.group(1)).strip("/")
    sm = re.search(r"[?&]style=([^&]+)", url, re.IGNORECASE)
    style = unquote(sm.group(1)) if sm else "stroke-rounded"
    cdn_url = f"https://cdn.hugeicons.com/icons/{icon_name}-{style}.svg?v=1.0.0"
    async with httpx.AsyncClient(headers=HEADERS, timeout=REQUEST_TIMEOUT, follow_redirects=True) as client:
        cdn_r = await client.get(cdn_url, headers={**HEADERS, "Referer": "https://hugeicons.com/"})
        if cdn_r.status_code == 200 and "<svg" in cdn_r.text.lower():
            svg = cdn_r.text.strip()
        else:
            page_r = await client.get(url, headers=HEADERS)
            page_r.raise_for_status()
            svg_m = re.search(r"<svg[\s\S]*?</svg>", page_r.text, re.IGNORECASE)
            if not svg_m:
                raise ValueError("SVG was not found on Hugeicons.")
            svg = svg_m.group(0).strip()
    if len(svg.encode("utf-8")) > MAX_UPLOAD_BYTES:
        raise ValueError("The SVG is larger than Telegram's upload limit.")
    return {"svg": svg, "icon_name": icon_name, "style": style}


def format_svg(svg: str) -> str:
    svg = re.sub(r"\s+", " ", svg)
    return svg.replace("> <", ">\n  <").strip()


# ── Telegram Handlers ─────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(WELCOME_MESSAGE, parse_mode="Markdown")


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
            "*Effects:*\n"
            + "  ".join(label for _, label in EFFECTS),
            parse_mode="Markdown",
        )


async def process_lummi(update: Update, status_message: Any, url: str) -> None:
    try:
        await safe_edit(status_message, "⏳ Downloading the Lummi asset…")
        result = await fetch_lummi_asset(url)
        await safe_edit(status_message, "📤 Sending full-size Lummi asset…")
        document = BytesIO(result["bytes"])
        document.name = result["filename"]
        await update.message.reply_document(
            document=document,
            filename=result["filename"],
            caption=truncate_caption(
                f"✅ Full-size image ({result['size_mb']:.2f} MB)\n"
                f"Direct link: {result['direct_url']}"
            ),
        )
        await safe_delete(status_message)
    except Exception as exc:
        logger.warning("Lummi request failed: %s", exc)
        await safe_edit(
            status_message,
            "❌ Sorry, I could not retrieve that Lummi asset. "
            "It may be unavailable, unsupported, or too large.",
        )


async def process_hugeicons(update: Update, status_message: Any, url: str) -> None:
    try:
        await safe_edit(status_message, "⏳ Fetching the Hugeicons SVG…")
        result = await fetch_hugeicons_svg(url)
        clean_svg = format_svg(result["svg"])
        icon_name = result["icon_name"]
        style = result["style"]
        filename = f"{icon_name}-{style}.svg"
        await safe_delete(status_message)
        label = f"✅ *{markdown_v2_escape(icon_name)}* \\({markdown_v2_escape(style)}\\)"
        await update.message.reply_text(label, parse_mode="MarkdownV2")
        await update.message.reply_text(
            f"```xml\n{markdown_code_escape(clean_svg)}\n```",
            parse_mode="MarkdownV2",
        )
        document = BytesIO(clean_svg.encode("utf-8"))
        document.name = filename
        await update.message.reply_document(
            document=document,
            filename=filename,
            caption=truncate_caption(f"{filename} — ready to download and use."),
        )
    except Exception as exc:
        logger.warning("Hugeicons request failed: %s", exc)
        await safe_edit(
            status_message,
            "❌ Sorry, I could not retrieve that Hugeicons SVG. "
            "Check that the link is valid and the icon exists.",
        )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """User sent a photo — ask which effect to apply."""
    if not update.message or not update.message.photo:
        return
    photo = update.message.photo[-1]  # highest resolution
    file_id = photo.file_id

    # Store the real (long) file_id under a short token, and only ever
    # put the token in callback_data. This avoids Telegram's 64-byte
    # callback_data limit, which raw file_ids blow past on their own.
    pending: dict[str, str] = context.bot_data.setdefault("pending_files", {})
    if len(pending) >= MAX_PENDING_FILES:
        # Drop the oldest entry to keep memory bounded.
        oldest_key = next(iter(pending))
        pending.pop(oldest_key, None)
    token = uuid.uuid4().hex[:10]
    pending[token] = file_id

    keyboard = build_effect_keyboard(token)
    await update.message.reply_text(
        "🎨 *Choose an effect to apply:*",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


async def handle_effect_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """User tapped an effect button."""
    query = update.callback_query
    await query.answer()

    try:
        _, effect_key, token = query.data.split("|", 2)
    except ValueError:
        await query.edit_message_text("❌ Invalid selection.")
        return

    pending: dict[str, str] = context.bot_data.get("pending_files", {})
    file_id = pending.get(token)
    if not file_id:
        await query.edit_message_text(
            "❌ This request has expired. Please resend the photo."
        )
        return

    effect_label = EFFECT_NAMES.get(effect_key, effect_key)
    status_msg = await query.edit_message_text(
        f"⏳ Applying *{effect_label}*… please wait.",
        parse_mode="Markdown",
    )

    try:
        await context.bot.send_chat_action(
            chat_id=query.message.chat_id,
            action=ChatAction.UPLOAD_PHOTO,
        )

        # Download the original photo
        tg_file = await context.bot.get_file(file_id)
        file_bytes_io = BytesIO()
        await tg_file.download_to_memory(file_bytes_io)
        image_bytes = file_bytes_io.getvalue()

        w, h = _get_image_dimensions(image_bytes)
        bmp_bytes = await apply_effect_to_image(image_bytes, effect_key, w, h)

        document = BytesIO(bmp_bytes)
        document.name = f"{effect_key}.bmp"

        await query.message.reply_document(
            document=document,
            filename=f"{effect_key}.bmp",
            caption=truncate_caption(f"✅ {effect_label} applied ({w}×{h}px)"),
        )
        await safe_edit(status_msg, f"✅ *{effect_label}* done!", parse_mode="Markdown")

        # Clean up the token now that it's been used successfully.
        pending.pop(token, None)

    except asyncio.TimeoutError:
        await safe_edit(status_msg, "⏱ Timed out — try a smaller image.")
    except Exception as exc:
        logger.warning("Effect %s failed: %s", effect_key, exc)
        await safe_edit(status_msg, f"❌ Failed to apply effect: {exc}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    raw_text = update.message.text
    lummi_match = LUMMI_URL_RE.search(raw_text)
    url = trim_url(lummi_match.group(0)) if lummi_match else None
    platform = "lummi" if url else None

    if not url:
        generic_match = URL_RE.search(raw_text)
        if generic_match:
            candidate = trim_url(generic_match.group(0))
            detected = detect_platform(candidate)
            if detected:
                url, platform = candidate, detected

    if not url or not platform:
        await update.message.reply_text(
            "Please send a supported Lummi.ai or Hugeicons link, "
            "or send a photo to apply an image effect. Use /help for examples."
        )
        return

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id,
        action=ChatAction.UPLOAD_DOCUMENT,
    )
    status_message = await update.message.reply_text("⏳ Processing your link…")

    if platform == "lummi":
        await process_lummi(update, status_message, url)
    else:
        await process_hugeicons(update, status_message, url)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled exception:", exc_info=context.error)


# ── Entry Point ───────────────────────────────────────────────────────────────

def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set.")

    webhook_url = os.getenv("WEBHOOK_URL", "").strip()
    port = int(os.getenv("PORT", 10000))

    application = Application.builder().token(token).concurrent_updates(True).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(CallbackQueryHandler(handle_effect_callback, pattern=r"^fx\|"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_error_handler(error_handler)

    logger.info("Unified bot starting (Lummi + Hugeicons + 32 Effects)")

    if webhook_url:
        logger.info("Webhook mode on port %s", port)
        application.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path="/webhook",
            webhook_url=f"{webhook_url}/webhook",
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        logger.info("Polling mode")
        application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
