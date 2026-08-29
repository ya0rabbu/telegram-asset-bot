"""Unified Lummi AI + Hugeicons Telegram bot."""

from __future__ import annotations

import logging
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BytesIO
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx
from bs4 import BeautifulSoup
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

MAX_UPLOAD_BYTES = 49 * 1024 * 1024
REQUEST_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=10.0)

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

WELCOME_MESSAGE = (
    "Welcome. Send a Lummi.ai photo, illustration, or 3D link, or a Hugeicons "
    "icon link. I will detect the source automatically and return the asset."
)


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        pass


def run_health_server():
    port = int(os.getenv("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()


def trim_url(url: str) -> str:
    return url.rstrip(".,!?;:)]}>\"'")


def detect_platform(url: str) -> str | None:
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    if host == "lummi.ai" and re.match(
        r"^/(?:photo|illustration|3d)/[^/]+", urlsplit(url).path, re.IGNORECASE
    ):
        return "lummi"
    if host == "hugeicons.com" and urlsplit(url).path.lower().startswith("/icon/"):
        return "hugeicons"
    return None


def find_lummi_cid(page_html: str, slug: str) -> str | None:
    soup = BeautifulSoup(page_html, "html.parser")
    scripts = [script.string or script.get_text() for script in soup.find_all("script")]
    relevant_scripts = [script for script in scripts if slug in script]
    candidates = relevant_scripts + scripts

    for script in candidates:
        for key in ("outpaintAssetPath", "path"):
            escaped_pattern = rf'{re.escape(key)}\\?":\\?"assets/({LUMMI_CID_RE.pattern})'
            match = re.search(escaped_pattern, script)
            if match:
                return match.group(1)
        match = LUMMI_CID_RE.search(script)
        if match:
            return match.group(0)

    og_image = soup.find("meta", property="og:image")
    if og_image and og_image.get("content"):
        match = LUMMI_CID_RE.search(og_image["content"])
        if match:
            return match.group(0)
    return None


async def fetch_lummi_asset(url: str) -> dict[str, Any]:
    parsed = urlsplit(url)
    slug_match = re.match(r"^/(?:photo|illustration|3d)/([^/?#]+)", parsed.path, re.IGNORECASE)
    if not slug_match:
        raise ValueError("The Lummi URL format is not supported.")
    slug = unquote(slug_match.group(1))

    async with httpx.AsyncClient(headers=HEADERS, timeout=REQUEST_TIMEOUT, follow_redirects=True) as client:
        page_response = await client.get(url)
        page_response.raise_for_status()
        cid = find_lummi_cid(page_response.text, slug)
        if not cid:
            raise ValueError("Could not find a direct Lummi asset on the page.")

        direct_url = f"https://assets.lummi.ai/assets/{cid}"
        asset_response = await client.get(
            direct_url,
            headers={**HEADERS, "Referer": "https://www.lummi.ai/"},
        )
        asset_response.raise_for_status()
        content = asset_response.content

    if not content:
        raise ValueError("The Lummi asset download was empty.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError("The Lummi file is larger than Telegram's upload limit.")

    content_type = asset_response.headers.get("content-type", "image/jpeg").split(";", 1)[0].lower()
    extension = {
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
        "image/gif": "gif",
        "image/tiff": "tiff",
    }.get(content_type, "jpg")
    return {
        "bytes": content,
        "filename": f"lummi_{cid[:8]}.{extension}",
        "direct_url": direct_url,
        "size_mb": len(content) / (1024 * 1024),
    }


async def fetch_hugeicons_svg(url: str) -> dict[str, str]:
    match = re.search(r"hugeicons\.com/icon/([^?#]+)", url, re.IGNORECASE)
    if not match:
        raise ValueError("Invalid Hugeicons URL.")

    icon_name = unquote(match.group(1)).strip("/")
    style_match = re.search(r"[?&]style=([^&]+)", url, re.IGNORECASE)
    style = unquote(style_match.group(1)) if style_match else "stroke-rounded"
    cdn_url = f"https://cdn.hugeicons.com/icons/{icon_name}-{style}.svg?v=1.0.0"

    async with httpx.AsyncClient(headers=HEADERS, timeout=REQUEST_TIMEOUT, follow_redirects=True) as client:
        cdn_response = await client.get(
            cdn_url,
            headers={**HEADERS, "Referer": "https://hugeicons.com/"},
        )
        if cdn_response.status_code == 200 and "<svg" in cdn_response.text.lower():
            svg = cdn_response.text.strip()
        else:
            page_response = await client.get(url, headers=HEADERS)
            page_response.raise_for_status()
            svg_match = re.search(r"<svg[\s\S]*?</svg>", page_response.text, re.IGNORECASE)
            if not svg_match:
                raise ValueError("SVG was not found on Hugeicons.")
            svg = svg_match.group(0).strip()

    if len(svg.encode("utf-8")) > MAX_UPLOAD_BYTES:
        raise ValueError("The SVG is larger than Telegram's upload limit.")
    return {"svg": svg, "icon_name": icon_name, "style": style}


def format_svg(svg: str) -> str:
    svg = re.sub(r"\s+", " ", svg)
    return svg.replace("> <", ">\n  <").strip()


def markdown_v2_escape(text: str) -> str:
    return re.sub(r"([_\*\[\]\(\)~`>#+\-=|{}.!\\])", r"\\\1", text)


def markdown_code_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("`", "\\`")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(WELCOME_MESSAGE)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(
            "Supported links:\n"
            "• Lummi: https://www.lummi.ai/photo/...\n"
            "• Lummi: https://www.lummi.ai/illustration/...\n"
            "• Lummi: https://www.lummi.ai/3d/...\n"
            "• Hugeicons: https://hugeicons.com/icon/..."
        )


async def process_lummi(update: Update, status_message: Any, url: str) -> None:
    try:
        await status_message.edit_text("Downloading the Lummi asset…")
        result = await fetch_lummi_asset(url)
        await status_message.edit_text("Sending the full-size Lummi asset…")
        document = BytesIO(result["bytes"])
        document.name = result["filename"]
        await update.message.reply_document(
            document=document,
            filename=result["filename"],
            caption=(
                f"Full-size image ({result['size_mb']:.2f} MB)\n"
                f"Direct link: {result['direct_url']}"
            ),
        )
        await status_message.delete()
    except Exception as exc:
        logger.warning("Lummi request failed: %s", exc)
        await status_message.edit_text(
            "Sorry, I could not retrieve that Lummi asset. It may be unavailable, "
            "unsupported, or larger than Telegram's upload limit."
        )


async def process_hugeicons(update: Update, status_message: Any, url: str) -> None:
    try:
        await status_message.edit_text("Fetching the Hugeicons SVG…")
        result = await fetch_hugeicons_svg(url)
        clean_svg = format_svg(result["svg"])
        icon_name = result["icon_name"]
        style = result["style"]
        filename = f"{icon_name}-{style}.svg"

        await status_message.delete()
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
            caption=f"{filename} — ready to download and use.",
        )
    except Exception as exc:
        logger.warning("Hugeicons request failed: %s", exc)
        await status_message.edit_text(
            "Sorry, I could not retrieve that Hugeicons SVG. Check that the link is "
            "valid and that the icon still exists."
        )


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
            "Please send a supported Lummi.ai or Hugeicons link. Use /help for examples."
        )
        return

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id,
        action=ChatAction.UPLOAD_DOCUMENT,
    )
    status_message = await update.message.reply_text("Processing your link…")

    if platform == "lummi":
        await process_lummi(update, status_message, url)
    else:
        await process_hugeicons(update, status_message, url)


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set.")

    webhook_url = os.getenv("WEBHOOK_URL", "").strip()
    port = int(os.getenv("PORT", 10000))

    application = Application.builder().token(token).concurrent_updates(True).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Unified Lummi/Hugeicons bot is starting")

    if webhook_url:
        logger.info("Running in webhook mode on port %s", port)
        application.run_webhook(
            listen="0.0.0.0",
            port=port,
            webhook_url=f"{webhook_url}/webhook",
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        logger.info("Running in polling mode")
        application.run_polling(allowed_updates=Update.ALL_TYPES)
