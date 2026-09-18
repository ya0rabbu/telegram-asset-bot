"""Lummi and Hugeicons asset fetching and delivery."""

from __future__ import annotations
import logging
import re
from io import BytesIO
from urllib.parse import unquote, urlsplit

import httpx

from modules import storage, image_extra
from modules.ui.keyboards import build_lummi_size_keyboard, build_hugeicons_size_keyboard
from modules.handlers.commands import is_admin, _record

logger = logging.getLogger("bangaliicon.assets")

MAX_UPLOAD_BYTES = 49 * 1024 * 1024
LUMMI_CID_RE     = re.compile(r"Qm[1-9A-HJ-NP-Za-km-z]{44}")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

REQUEST_TIMEOUT     = httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=10.0)
HTTP_RETRY_ATTEMPTS = 3
HTTP_RETRY_BACKOFF  = 1.5

_http_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            headers=HEADERS, timeout=REQUEST_TIMEOUT, follow_redirects=True
        )
    return _http_client


async def close_http_client() -> None:
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
        _http_client = None


async def with_retry(fn, attempts=HTTP_RETRY_ATTEMPTS, backoff=HTTP_RETRY_BACKOFF):
    import asyncio
    last_exc = None
    for attempt in range(attempts):
        try:
            return await fn()
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            last_exc = exc
            if attempt < attempts - 1:
                await asyncio.sleep(backoff * (2 ** attempt))
    raise last_exc


# ── Token store ───────────────────────────────────────────────────────────────

def _store_side_data(bot_data: dict, key: str, token: str, value) -> None:
    bot_data.setdefault(key, {})[token] = value


def _get_side_data(bot_data: dict, key: str, token: str):
    return bot_data.get(key, {}).get(token)


def _drop_side_data(bot_data: dict, key: str, token: str) -> None:
    bot_data.get(key, {}).pop(token, None)


# ── Lummi ─────────────────────────────────────────────────────────────────────

def find_lummi_cid(page_html: str, slug: str) -> str | None:
    from bs4 import BeautifulSoup
    soup    = BeautifulSoup(page_html, "html.parser")
    scripts = [s.string or s.get_text() for s in soup.find_all("script")]
    for script in ([s for s in scripts if slug in s] + scripts):
        for key in ("outpaintAssetPath", "path"):
            m = re.search(
                rf'{re.escape(key)}\\?":\\?"assets/({LUMMI_CID_RE.pattern})', script
            )
            if m: return m.group(1)
        m = LUMMI_CID_RE.search(script)
        if m: return m.group(0)
    from bs4 import BeautifulSoup
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        m = LUMMI_CID_RE.search(og["content"])
        if m: return m.group(0)
    return None


async def fetch_lummi_asset(url: str) -> dict:
    parsed     = urlsplit(url)
    slug_match = re.match(
        r"^/(?:photo|illustration|3d)/([^/?#]+)", parsed.path, re.IGNORECASE
    )
    if not slug_match:
        raise ValueError("Unsupported Lummi URL format.")

    slug      = unquote(slug_match.group(1))
    is_3d     = parsed.path.lower().startswith("/3d/")
    client    = get_http_client()

    async def _fetch():
        page_r = await client.get(url)
        page_r.raise_for_status()
        cid = find_lummi_cid(page_r.text, slug)
        if not cid:
            raise ValueError("Could not locate a Lummi asset on that page.")
        direct_url = f"https://assets.lummi.ai/assets/{cid}"
        asset_r    = await client.get(
            direct_url, headers={**HEADERS, "Referer": "https://www.lummi.ai/"}
        )
        asset_r.raise_for_status()
        return asset_r, cid, direct_url

    asset_r, cid, direct_url = await with_retry(_fetch)
    content = asset_r.content
    if not content:
        raise ValueError("The Lummi asset download was empty.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError("The Lummi file exceeds Telegram's upload limit (49 MB).")

    ct  = asset_r.headers.get("content-type", "image/jpeg").split(";", 1)[0].lower()

    # 3D asset detection
    if is_3d or "model/gltf" in ct or cid.endswith(".glb"):
        return {
            "bytes":      content,
            "filename":   f"lummi_{cid[:8]}.glb",
            "direct_url": direct_url,
            "size_mb":    len(content) / (1024 * 1024),
            "is_3d":      True,
            "cid":        cid,
        }

    ext = {
        "image/jpeg":  "jpg",
        "image/png":   "png",
        "image/webp":  "webp",
        "image/gif":   "gif",
        "image/tiff":  "tiff",
    }.get(ct, "jpg")

    return {
        "bytes":      content,
        "filename":   f"lummi_{cid[:8]}.{ext}",
        "direct_url": direct_url,
        "size_mb":    len(content) / (1024 * 1024),
        "is_3d":      False,
        "cid":        cid,
    }


# ── Hugeicons ─────────────────────────────────────────────────────────────────

async def fetch_hugeicons_svg(url: str) -> dict:
    m = re.search(r"hugeicons\.com/icon/([^?#]+)", url, re.IGNORECASE)
    if not m:
        raise ValueError("Invalid Hugeicons URL.")
    icon_name = unquote(m.group(1)).strip("/")
    sm        = re.search(r"[?&]style=([^&]+)", url, re.IGNORECASE)
    style     = unquote(sm.group(1)) if sm else "stroke-rounded"
    cdn_url   = f"https://cdn.hugeicons.com/icons/{icon_name}-{style}.svg?v=1.0.0"
    client    = get_http_client()

    async def _fetch():
        cdn_r = await client.get(
            cdn_url, headers={**HEADERS, "Referer": "https://hugeicons.com/"}
        )
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
    import asyncio
    import cairosvg
    return await asyncio.to_thread(
        cairosvg.svg2png,
        bytestring=svg_text.encode("utf-8"),
        output_width=size,
        output_height=size,
    )


# ── Delivery ──────────────────────────────────────────────────────────────────

async def deliver_lummi(
    update, context, token: str, size_choice: str
) -> None:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    payload = _get_side_data(context.bot_data, "icon_payload", token)
    if not payload:
        await update.callback_query.edit_message_text(
            "❌ Request expired — please resend the link."
        ); return

    # 3D GLB — always deliver as-is
    if payload.get("is_3d"):
        doc = BytesIO(payload["bytes"]); doc.name = payload["filename"]
        await update.callback_query.message.reply_document(
            document=doc,
            filename=payload["filename"],
            caption=(
                f"✅ *3D Model* — `{payload['filename']}`\n"
                f"📦 {payload['size_mb']:.2f} MB\n"
                f"🔗 [Direct link]({payload['direct_url']})"
            ),
            parse_mode="Markdown",
        )
        _cleanup_icon(context.bot_data, token)
        await _record(context, update, "lummi_3d")
        return

    # Original size
    if size_choice == "orig":
        doc = BytesIO(payload["bytes"]); doc.name = payload["filename"]
        await update.callback_query.message.reply_document(
            document=doc,
            filename=payload["filename"],
            caption=f"✅ Original — {payload['size_mb']:.2f} MB",
        )
        _cleanup_icon(context.bot_data, token)
        await _record(context, update, "lummi")
        return

    # Preset size
    size_map = {
        "small":  (516,  640),
        "medium": (967,  1200),
        "large":  (1933, 2400),
        "xlarge": (3712, 4608),
    }
    if size_choice not in size_map:
        await update.callback_query.edit_message_text("❌ Unknown size choice.")
        return

    if not await _check_rate(update, "iconrender"):
        return

    w, h = size_map[size_choice]
    async with storage.HEAVY_JOB_SEMAPHORE:
        out = await image_extra.resize_image(payload["bytes"], w, h)

    fname = f"lummi_{size_choice}_{w}x{h}.png"
    doc   = BytesIO(out); doc.name = fname
    await update.callback_query.message.reply_document(
        document=doc,
        filename=fname,
        caption=f"✅ {size_choice.title()} — {w}×{h}px",
    )
    _cleanup_icon(context.bot_data, token)
    await _record(context, update, "lummi")


async def deliver_hugeicons(
    update, context, token: str, size_choice: str
) -> None:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    payload = _get_side_data(context.bot_data, "icon_payload", token)
    if not payload:
        await update.callback_query.edit_message_text(
            "❌ Request expired — please resend the link."
        ); return

    svg_raw   = payload["svg"]
    icon_name = payload["icon_name"]
    style     = payload["style"]

    # SVG file delivery
    if size_choice == "svg":
        svg_formatted = format_svg(svg_raw)
        filename      = f"{icon_name}-{style}.svg"

        # Send SVG code block with copy hint
        code_msg = (
            f"📄 *{icon_name}* (`{style}`)\n\n"
            f"```xml\n{svg_formatted[:3500]}\n```\n\n"
            f"_Copy the code above or download the file below_ 👇"
        )
        await update.callback_query.message.reply_text(
            code_msg, parse_mode="Markdown"
        )

        # Send SVG file
        doc = BytesIO(svg_formatted.encode()); doc.name = filename
        await update.callback_query.message.reply_document(
            document=doc,
            filename=filename,
            caption=f"✅ `{filename}` — Original SVG vector file",
            parse_mode="Markdown",
        )
        _cleanup_icon(context.bot_data, token)
        await _record(context, update, "hugeicons_svg")
        return

    # PNG at selected size
    if not await _check_rate(update, "iconrender"):
        return

    size      = int(size_choice)
    svg_sized = image_extra.adjust_svg_size(svg_raw, size)
    filename  = f"{icon_name}-{style}-{size}px.svg"

    # Also send sized SVG code
    svg_formatted = format_svg(svg_sized)
    code_msg = (
        f"📐 *{icon_name}* (`{style}`) — {size}×{size}px\n\n"
        f"```xml\n{svg_formatted[:3500]}\n```\n\n"
        f"_Copy the code above or download the file below_ 👇"
    )
    await update.callback_query.message.reply_text(
        code_msg, parse_mode="Markdown"
    )

    # Send sized SVG file
    doc = BytesIO(svg_sized.encode()); doc.name = filename
    await update.callback_query.message.reply_document(
        document=doc,
        filename=filename,
        caption=f"✅ `{filename}` — {size}×{size}px SVG",
        parse_mode="Markdown",
    )

    # Also render PNG
    async with storage.HEAVY_JOB_SEMAPHORE:
        png_bytes = await render_svg_to_png(svg_raw, size)
    png_name = f"{icon_name}-{style}-{size}px.png"
    doc      = BytesIO(png_bytes); doc.name = png_name
    await update.callback_query.message.reply_document(
        document=doc,
        filename=png_name,
        caption=f"🖼 `{png_name}` — PNG render",
        parse_mode="Markdown",
    )

    _cleanup_icon(context.bot_data, token)
    await _record(context, update, "hugeicons")


def _cleanup_icon(bot_data: dict, token: str) -> None:
    for key in ("icon_kind", "icon_payload"):
        _drop_side_data(bot_data, key, token)


async def _check_rate(update, tool: str) -> bool:
    chat_id = update.effective_chat.id
    allowed, wait = await storage.check_rate_limit(
        chat_id, exempt=is_admin(update)
    )
    if not allowed:
        used, limit = await storage.calls_used(chat_id)
        await update.effective_message.reply_text(
            f"⏳ Please wait ~{int(wait)}s "
            f"({used}/{limit} used in last {storage.RATE_LIMIT_WINDOW_SECONDS}s)."
        )
        return False
    return True


# ── URL routing helpers ───────────────────────────────────────────────────────

LUMMI_URL_RE = re.compile(
    r"https?://(?:www\.)?lummi\.ai/(?:photo|illustration|3d)/[^\s<>]+",
    re.IGNORECASE,
)
HUGEICONS_URL_RE = re.compile(
    r"https?://(?:www\.)?hugeicons\.com/icon/[^\s<>]+",
    re.IGNORECASE,
)


def detect_platform(url: str) -> str | None:
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    path = urlsplit(url).path
    if host == "lummi.ai" and re.match(
        r"^/(?:photo|illustration|3d)/[^/]+", path, re.IGNORECASE
    ):
        return "lummi"
    if host == "hugeicons.com" and path.lower().startswith("/icon/"):
        return "hugeicons"
    return None
