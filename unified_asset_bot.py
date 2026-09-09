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

# Telegram bots (standard Bot API, not a self-hosted local server) can only
# download files up to this size via get_file(). Files larger than this
# will fail to download even if the upload itself succeeded.
BOT_DOWNLOAD_LIMIT_BYTES = 20 * 1024 * 1024

# Cap on how many pages of a PDF we render to images, to keep things fast
# and avoid flooding the chat.
PDF2IMG_MAX_PAGES = 20

# Common languages offered in the /translate quick-picker.
TRANSLATE_LANGUAGES: list[tuple[str, str]] = [
    ("en", "🇬🇧 English"),
    ("bn", "🇧🇩 বাংলা"),
    ("hi", "🇮🇳 हिन्दी"),
    ("ar", "🇸🇦 العربية"),
    ("es", "🇪🇸 Español"),
    ("fr", "🇫🇷 Français"),
    ("zh-CN", "🇨🇳 中文"),
    ("ja", "🇯🇵 日本語"),
]

# Simple greeting matcher so "hi"/"hello"/etc. also open the main menu.
GREETING_RE = re.compile(
    r"^(hi+|he+llo+|hey+|yo|start|salam|assalamu\s*alaikum|assalamualaikum)[!.\s]*$",
    re.IGNORECASE,
)

# ── 32 Effects definition (key, label, category) ───────────────────────────────
EFFECT_CATEGORIES: dict[str, str] = {
    "retro":   "Retro & Print",
    "tone":    "Color & Tone",
    "art":     "Artistic",
    "digital": "Digital & Glitch",
    "distort": "Distort & Special",
}
EFFECT_CATEGORY_ORDER = ["retro", "tone", "art", "digital", "distort"]

EFFECTS: list[tuple[str, str, str]] = [
    # Retro & Print
    ("halftone-dots",        "Halftone",        "retro"),
    ("comic-cmyk",           "Pop-Art",         "retro"),
    ("retro-8bit",           "8-Bit CRT",       "retro"),
    ("risograph-duo",        "Risograph",       "retro"),
    ("crosshatch-engraving", "Crosshatch",      "retro"),
    ("bayer-dither",         "Dither",          "retro"),
    ("vhs-tape",             "VHS Tape",        "retro"),
    ("lomography",           "Lomography",      "retro"),
    # Color & Tone
    ("duotone",              "Duotone",         "tone"),
    ("autumn-tone",          "Autumn",          "tone"),
    ("forest-green",         "Forest",          "tone"),
    ("desert-sand",          "Desert",          "tone"),
    ("cherry-blossom",       "Cherry Blossom",  "tone"),
    ("moonlight",            "Moonlight",       "tone"),
    ("frozen-ice",           "Frozen",          "tone"),
    # Artistic
    ("watercolor",           "Watercolor",      "art"),
    ("oil-paint",            "Oil Paint",       "art"),
    ("emboss",               "Emboss",          "art"),
    ("cinematic-noir",       "Noir",            "art"),
    # Digital & Glitch
    ("cyber-glitch",         "Cyberpunk",       "digital"),
    ("glitch-art",           "Glitch Art",      "digital"),
    ("ascii-matrix",         "ASCII",           "digital"),
    ("sobel-neon",           "Neon Edge",       "digital"),
    ("vaporwave",            "Vaporwave",       "digital"),
    ("neon-poster",          "Neon Poster",     "digital"),
    # Distort & Special
    ("swirl-distort",        "Swirl",           "distort"),
    ("mirror-reflect",       "Mirror",          "distort"),
    ("pixelate",             "Pixelate",        "distort"),
    ("blueprint-cyan",       "Blueprint",       "distort"),
    ("thermal-flir",         "Thermal",         "distort"),
    ("inferno",              "Inferno",         "distort"),
    ("horror-red",           "Horror",          "distort"),
]

EFFECT_NAMES = {key: label for key, label, _ in EFFECTS}

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
    "🧰 Or tap a tool below to get started\n\n"
    "_Type_ `/menu` _anytime ·_ `/cancel` _to stop a task_\n\n"
    "✦ Design and Developed By *@YA_Rabbu*"
)

MENU_INTRO = "🧰 *Select a tool*"


def build_main_menu_keyboard() -> InlineKeyboardMarkup:
    """Main feature menu shown on /start, /menu, and greetings.

    Grouped by purpose: image ops, PDF ops, format conversion, text ops.
    """
    rows = [
        [
            InlineKeyboardButton("🎨 Image Effects", callback_data="menu|effects"),
            InlineKeyboardButton("🧹 Remove BG", callback_data="menu|bgremove"),
        ],
        [
            InlineKeyboardButton("🖼 Image → PDF", callback_data="menu|img2pdf"),
            InlineKeyboardButton("📄 PDF → Images", callback_data="menu|pdf2img"),
        ],
        [
            InlineKeyboardButton("🔁 JPEG → PNG", callback_data="menu|jpg2png"),
            InlineKeyboardButton("🔁 PNG → JPEG", callback_data="menu|png2jpg"),
        ],
        [
            InlineKeyboardButton("🌐 Translate", callback_data="menu|translate"),
            InlineKeyboardButton("📝 MD → TXT", callback_data="menu|md2txt"),
        ],
    ]
    return InlineKeyboardMarkup(rows)


def build_effect_categories_keyboard(token: str) -> InlineKeyboardMarkup:
    """Top-level effect picker: 5 categories, 2 per row."""
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for cat_key in EFFECT_CATEGORY_ORDER:
        row.append(
            InlineKeyboardButton(
                EFFECT_CATEGORIES[cat_key], callback_data=f"fxcat|{cat_key}|{token}"
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def build_translate_lang_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    row = []
    for code, label in TRANSLATE_LANGUAGES:
        row.append(InlineKeyboardButton(label, callback_data=f"trlang|{code}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(buttons)


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

def build_effect_keyboard(category: str, token: str) -> InlineKeyboardMarkup:
    """Build a 2-column inline keyboard with the effects in one category,
    plus a back button to return to the category picker.

    `token` is a short opaque id referencing the photo's real file_id,
    which is stored separately (see handle_photo). This keeps
    callback_data well under Telegram's 64-byte limit, since raw
    Telegram file_ids are frequently 80-100+ characters long and would
    otherwise trigger `Button_data_invalid` once combined with an
    effect key.
    """
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
    bmp_bytes = base64.b64decode(b64_part)

    # Convert the engine's raw BMP output to PNG (smaller file, lossless,
    # and consistent with the rest of the bot's image outputs).
    from PIL import Image  # type: ignore
    with Image.open(BytesIO(bmp_bytes)) as bmp_img:
        png_buf = BytesIO()
        bmp_img.convert("RGBA").save(png_buf, format="PNG", optimize=True)
        return png_buf.getvalue()


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


# ── Background removal (direct ONNX — no `rembg` package) ──────────────────
#
# We deliberately do NOT use the `rembg` package here. Importing it pulls in
# scipy + scikit-image + pymatting + every other model's session class
# (~270 MB of RSS) even though we only ever use one small model. On a
# low-RAM host like Render's free tier that import alone can push the
# process over its memory limit and get it silently OOM-killed — which is
# exactly what was happening (status message stuck forever, no error, no
# timeout message, because the whole process died before either could fire).
#
# Calling onnxruntime directly with the same u2netp model costs ~35 MB
# instead, which comfortably fits.

_REMBG_SESSION = None  # lazy-loaded onnxruntime.InferenceSession
REMBG_MAX_DIMENSION = 1500  # downscale before inference; keeps RAM/time bounded
REMBG_TIMEOUT_SECONDS = 90  # first call downloads the ~4.5MB model, so give it room
U2NETP_MODEL_URL = "https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2netp.onnx"
U2NETP_MODEL_PATH = os.path.join(tempfile.gettempdir(), "u2netp.onnx")


def _ensure_u2netp_model() -> str:
    """Download the u2netp model once and cache it on disk."""
    if os.path.exists(U2NETP_MODEL_PATH) and os.path.getsize(U2NETP_MODEL_PATH) > 1_000_000:
        return U2NETP_MODEL_PATH
    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        r = client.get(U2NETP_MODEL_URL)
        r.raise_for_status()
        tmp_path = U2NETP_MODEL_PATH + ".part"
        with open(tmp_path, "wb") as f:
            f.write(r.content)
        os.replace(tmp_path, U2NETP_MODEL_PATH)
    return U2NETP_MODEL_PATH


def _get_rembg_session():
    """Lazily create a plain onnxruntime session for the u2netp model."""
    global _REMBG_SESSION
    if _REMBG_SESSION is None:
        import onnxruntime as ort
        model_path = _ensure_u2netp_model()
        _REMBG_SESSION = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
    return _REMBG_SESSION


def _u2netp_predict_mask(session, img):
    """Run u2netp inference and return an 'L'-mode alpha mask the size of `img`.

    This replicates rembg's own U2netpSession.predict()/normalize() logic
    exactly, just without importing the rembg package to get it.
    """
    from PIL import Image

    resized = img.resize((320, 320), Image.Resampling.LANCZOS)
    arr = np.array(resized).astype(np.float32)
    arr = arr / max(float(np.max(arr)), 1e-6)

    mean = (0.485, 0.456, 0.406)
    std = (0.229, 0.224, 0.225)
    normed = np.zeros((320, 320, 3), dtype=np.float32)
    for c in range(3):
        normed[:, :, c] = (arr[:, :, c] - mean[c]) / std[c]
    normed = normed.transpose((2, 0, 1))
    input_tensor = np.expand_dims(normed, 0).astype(np.float32)

    input_name = session.get_inputs()[0].name
    outputs = session.run(None, {input_name: input_tensor})
    pred = outputs[0][:, 0, :, :]

    ma, mi = float(np.max(pred)), float(np.min(pred))
    pred = (pred - mi) / max(ma - mi, 1e-6)
    pred = np.squeeze(pred)

    mask = Image.fromarray((pred * 255).astype("uint8"), mode="L")
    return mask.resize(img.size, Image.Resampling.LANCZOS)


async def remove_background(image_bytes: bytes) -> bytes:
    """Remove the background from an image, returning transparent PNG bytes."""
    from PIL import Image, ImageOps

    def _run() -> bytes:
        img = Image.open(BytesIO(image_bytes))
        img.load()
        img = ImageOps.exif_transpose(img).convert("RGB")

        # Downscale very large photos first — keeps RAM/time bounded on
        # low-resource hosts.
        if max(img.size) > REMBG_MAX_DIMENSION:
            img.thumbnail((REMBG_MAX_DIMENSION, REMBG_MAX_DIMENSION), Image.LANCZOS)

        session = _get_rembg_session()
        mask = _u2netp_predict_mask(session, img)

        out = img.convert("RGBA")
        out.putalpha(mask)
        buf = BytesIO()
        out.save(buf, format="PNG", optimize=True)
        return buf.getvalue()

    try:
        return await asyncio.wait_for(asyncio.to_thread(_run), timeout=REMBG_TIMEOUT_SECONDS)
    except asyncio.TimeoutError as exc:
        raise RuntimeError(
            "Background removal timed out. The first request after a deploy "
            "downloads a small AI model and can be slow — please try again."
        ) from exc
    except Exception as exc:
        logger.exception("remove_background failed")
        raise RuntimeError(f"Background removal failed: {exc}") from exc


# ── Format conversion (JPEG/PNG/PDF) ────────────────────────────────────────

async def convert_image_format(image_bytes: bytes, target_format: str) -> bytes:
    """Convert raw image bytes to JPEG or PNG bytes, preserving original quality.

    Pillow's default JPEG quality is only 75 and it silently re-samples
    chroma unless told not to — both degrade the image. We explicitly
    request maximum quality, disable chroma subsampling, and keep PNG
    conversion truly lossless.
    """
    from PIL import Image, ImageOps

    def _run() -> bytes:
        img = Image.open(BytesIO(image_bytes))
        # Respect EXIF orientation so the output isn't rotated/mirrored.
        img = ImageOps.exif_transpose(img)
        out = BytesIO()

        if target_format.upper() == "JPEG":
            img = img.convert("RGB")
            img.save(
                out,
                format="JPEG",
                quality=100,
                subsampling=0,   # 4:4:4, no chroma downsampling
                optimize=True,
            )
        else:
            img = img.convert("RGBA")
            img.save(out, format="PNG", optimize=True)  # PNG is lossless regardless

        return out.getvalue()

    return await asyncio.to_thread(_run)


async def image_to_pdf(image_bytes: bytes) -> bytes:
    """Wrap a single image into a one-page PDF at full quality.

    Pillow embeds RGB images in PDFs as JPEG internally and — same as
    above — defaults to quality 75 unless told otherwise. We force
    maximum quality so the PDF doesn't visibly degrade the source image.
    """
    from PIL import Image, ImageOps

    def _run() -> bytes:
        img = Image.open(BytesIO(image_bytes))
        img = ImageOps.exif_transpose(img).convert("RGB")
        out = BytesIO()
        img.save(out, format="PDF", quality=100, resolution=300.0)
        return out.getvalue()

    return await asyncio.to_thread(_run)


async def pdf_to_images(pdf_bytes: bytes, max_pages: int = PDF2IMG_MAX_PAGES) -> list[bytes]:
    """Render each page of a PDF to PNG bytes (capped at max_pages).

    Rendered at 200 DPI (up from 150) for sharper, more print-quality output
    while staying well within Telegram's per-file size limits.
    """
    import fitz  # PyMuPDF

    def _run() -> list[bytes]:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        try:
            pages = []
            for i, page in enumerate(doc):
                if i >= max_pages:
                    break
                pix = page.get_pixmap(dpi=200)
                pages.append(pix.tobytes("png"))
            return pages
        finally:
            doc.close()

    return await asyncio.to_thread(_run)


# ── Translation ──────────────────────────────────────────────────────────────

TRANSLATE_MAX_CHARS = 4500  # keep comfortably under the free API's limit


async def translate_text(text: str, target_lang: str) -> str:
    from deep_translator import GoogleTranslator

    def _run() -> str:
        return GoogleTranslator(source="auto", target=target_lang).translate(text)

    return await asyncio.to_thread(_run)


# ── Markdown → plain text ────────────────────────────────────────────────────

def markdown_to_plain_text(md: str) -> str:
    """Strip common Markdown syntax, leaving readable plain text."""
    text = md
    text = re.sub(r"!\[.*?\]\(.*?\)", "", text)                    # images
    text = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", text)                 # links
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)      # headers
    text = re.sub(r"(\*\*|__)(.*?)\1", r"\2", text, flags=re.DOTALL)  # bold
    text = re.sub(r"(?<!\*)\*(?!\*)(.*?)\*(?!\*)", r"\1", text)     # italics *
    text = re.sub(r"(?<!_)_(?!_)(.*?)_(?!_)", r"\1", text)          # italics _
    text = re.sub(r"`{1,3}(.*?)`{1,3}", r"\1", text, flags=re.DOTALL)  # code
    text = re.sub(r"^\s*>\s?", "", text, flags=re.MULTILINE)        # blockquotes
    text = re.sub(r"^\s*[-*+]\s+", "- ", text, flags=re.MULTILINE)  # bullets
    text = re.sub(r"\n{3,}", "\n\n", text)                          # extra blank lines
    return text.strip()


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
        context.user_data.pop("awaiting", None)
        await update.message.reply_text(
            WELCOME_MESSAGE,
            parse_mode="Markdown",
            reply_markup=build_main_menu_keyboard(),
        )


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(
            MENU_INTRO,
            parse_mode="Markdown",
            reply_markup=build_main_menu_keyboard(),
        )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        had_task = context.user_data.pop("awaiting", None) is not None
        context.user_data.pop("target_lang", None)
        await update.message.reply_text(
            "✅ Cancelled." if had_task else "Nothing to cancel."
        )


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
            "*Tools (via /menu):*\n"
            "🧹 Remove background • 🌐 Translate text\n"
            "📄 PDF → Images • 🖼 Image → PDF\n"
            "🔁 JPEG ↔ PNG • 📝 MD → TXT\n\n"
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
    """User sent a photo (compressed). Routes to whichever tool is pending,
    defaulting to the 32-effect picker if nothing is pending."""
    if not update.message or not update.message.photo:
        return

    awaiting = context.user_data.get("awaiting")
    photo = update.message.photo[-1]  # highest resolution
    file_id = photo.file_id

    if awaiting in ("bgremove", "img2pdf", "jpg2png", "png2jpg"):
        await _run_image_tool(update, context, file_id, awaiting)
        context.user_data.pop("awaiting", None)
        return

    # Default behaviour: show the effect picker (backwards compatible).
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

    keyboard = build_effect_categories_keyboard(token)
    await update.message.reply_text(
        "🎨 *Choose a category:*",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


async def _run_image_tool(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    file_id: str,
    tool: str,
) -> None:
    """Shared implementation for bgremove / img2pdf / jpg2png / png2jpg,
    usable from both photo and document uploads."""
    labels = {
        "bgremove": "Removing background",
        "img2pdf": "Converting to PDF",
        "jpg2png": "Converting to PNG",
        "png2jpg": "Converting to JPEG",
    }
    status_message = await update.message.reply_text(f"⏳ {labels.get(tool, 'Processing')}…")
    try:
        tg_file = await context.bot.get_file(file_id)
        if tg_file.file_size and tg_file.file_size > BOT_DOWNLOAD_LIMIT_BYTES:
            await safe_edit(status_message, "❌ That file is too large for me to download (20 MB limit).")
            return
        buf = BytesIO()
        await tg_file.download_to_memory(buf)
        image_bytes = buf.getvalue()

        if tool == "bgremove":
            out_bytes = await remove_background(image_bytes)
            filename = "background_removed.png"
        elif tool == "img2pdf":
            out_bytes = await image_to_pdf(image_bytes)
            filename = "image.pdf"
        elif tool == "jpg2png":
            out_bytes = await convert_image_format(image_bytes, "PNG")
            filename = "converted.png"
        elif tool == "png2jpg":
            out_bytes = await convert_image_format(image_bytes, "JPEG")
            filename = "converted.jpg"
        else:
            await safe_edit(status_message, "❌ Unknown tool.")
            return

        document = BytesIO(out_bytes)
        document.name = filename
        await update.message.reply_document(document=document, filename=filename)
        await safe_delete(status_message)
    except Exception as exc:
        logger.warning("%s failed: %s", tool, exc)
        await safe_edit(status_message, f"❌ Failed: {exc}")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles file uploads for pdf2img, img2pdf, jpg2png, png2jpg, md2txt.

    Sending as a "File" (rather than a compressed photo) is required for
    PNG round-trips and PDFs, since Telegram auto-converts compressed
    photos to JPEG.
    """
    if not update.message or not update.message.document:
        return

    doc = update.message.document
    awaiting = context.user_data.get("awaiting")
    file_name = (doc.file_name or "").lower()

    if not awaiting:
        await update.message.reply_text(
            "Please choose a tool first with /menu, then send the file."
        )
        return

    if awaiting in ("bgremove", "img2pdf", "jpg2png", "png2jpg"):
        await _run_image_tool(update, context, doc.file_id, awaiting)
        context.user_data.pop("awaiting", None)
        return

    if awaiting == "pdf2img":
        if not (file_name.endswith(".pdf") or doc.mime_type == "application/pdf"):
            await update.message.reply_text("⚠️ That doesn't look like a PDF. Please send a .pdf file.")
            return
        status_message = await update.message.reply_text("⏳ Rendering PDF pages…")
        try:
            tg_file = await context.bot.get_file(doc.file_id)
            if tg_file.file_size and tg_file.file_size > BOT_DOWNLOAD_LIMIT_BYTES:
                await safe_edit(status_message, "❌ That PDF is too large for me to download (20 MB limit).")
                return
            buf = BytesIO()
            await tg_file.download_to_memory(buf)
            pages = await pdf_to_images(buf.getvalue())
            if not pages:
                await safe_edit(status_message, "❌ Couldn't render any pages from that PDF.")
                return
            await safe_delete(status_message)
            # Send in batches of 10 (Telegram media group limit).
            for batch_start in range(0, len(pages), 10):
                batch = pages[batch_start:batch_start + 10]
                media = [InputMediaPhoto(BytesIO(p)) for p in batch]
                await update.message.reply_media_group(media=media)
            if len(pages) >= PDF2IMG_MAX_PAGES:
                await update.message.reply_text(
                    f"ℹ️ Only the first {PDF2IMG_MAX_PAGES} pages were rendered."
                )
        except Exception as exc:
            logger.warning("pdf2img failed: %s", exc)
            await safe_edit(status_message, f"❌ Failed to convert PDF: {exc}")
        finally:
            context.user_data.pop("awaiting", None)
        return

    if awaiting == "md2txt":
        if not file_name.endswith(".md"):
            await update.message.reply_text("⚠️ Please send a .md (Markdown) file.")
            return
        status_message = await update.message.reply_text("⏳ Converting…")
        try:
            tg_file = await context.bot.get_file(doc.file_id)
            buf = BytesIO()
            await tg_file.download_to_memory(buf)
            md_text = buf.getvalue().decode("utf-8", errors="replace")
            plain_text = markdown_to_plain_text(md_text)
            out_name = re.sub(r"\.md$", ".txt", doc.file_name or "output.md", flags=re.IGNORECASE)
            document = BytesIO(plain_text.encode("utf-8"))
            document.name = out_name
            await update.message.reply_document(document=document, filename=out_name)
            await safe_delete(status_message)
        except Exception as exc:
            logger.warning("md2txt failed: %s", exc)
            await safe_edit(status_message, f"❌ Failed to convert file: {exc}")
        finally:
            context.user_data.pop("awaiting", None)
        return

    await update.message.reply_text("Please choose a tool first with /menu.")


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
        png_bytes = await apply_effect_to_image(image_bytes, effect_key, w, h)

        document = BytesIO(png_bytes)
        document.name = f"{effect_key}.png"

        await query.message.reply_document(
            document=document,
            filename=f"{effect_key}.png",
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


async def handle_effect_category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """User tapped a category button; show that category's effects."""
    query = update.callback_query
    await query.answer()

    try:
        _, category, token = query.data.split("|", 2)
    except ValueError:
        await query.edit_message_text("❌ Invalid selection.")
        return

    pending: dict[str, str] = context.bot_data.get("pending_files", {})
    if token not in pending:
        await query.edit_message_text("❌ This request has expired. Please resend the photo.")
        return

    cat_label = EFFECT_CATEGORIES.get(category, category)
    await query.edit_message_text(
        f"🎨 *{cat_label}* — choose an effect:",
        parse_mode="Markdown",
        reply_markup=build_effect_keyboard(category, token),
    )


async def handle_effect_back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """User tapped '◀ Categories'; return to the top-level category picker."""
    query = update.callback_query
    await query.answer()

    try:
        _, token = query.data.split("|", 1)
    except ValueError:
        await query.edit_message_text("❌ Invalid selection.")
        return

    pending: dict[str, str] = context.bot_data.get("pending_files", {})
    if token not in pending:
        await query.edit_message_text("❌ This request has expired. Please resend the photo.")
        return

    await query.edit_message_text(
        "🎨 *Choose a category:*",
        parse_mode="Markdown",
        reply_markup=build_effect_categories_keyboard(token),
    )


async def handle_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """User tapped a button in the main tool menu."""
    query = update.callback_query
    await query.answer()

    try:
        _, action = query.data.split("|", 1)
    except ValueError:
        await query.edit_message_text("❌ Invalid selection.")
        return

    if action == "effects":
        context.user_data.pop("awaiting", None)
        await query.edit_message_text("🎨 Send me a *photo* and pick an effect from the keyboard.", parse_mode="Markdown")
    elif action == "bgremove":
        context.user_data["awaiting"] = "bgremove"
        await query.edit_message_text("🧹 Send me a *photo* and I'll remove its background.", parse_mode="Markdown")
    elif action == "translate":
        await query.edit_message_text(
            "🌐 *Choose the target language:*",
            parse_mode="Markdown",
            reply_markup=build_translate_lang_keyboard(),
        )
    elif action == "pdf2img":
        context.user_data["awaiting"] = "pdf2img"
        await query.edit_message_text(
            f"📄 Send me a *PDF file* — I'll render up to {PDF2IMG_MAX_PAGES} pages as images.",
            parse_mode="Markdown",
        )
    elif action == "img2pdf":
        context.user_data["awaiting"] = "img2pdf"
        await query.edit_message_text("🖼 Send me an *image* (photo or file) and I'll wrap it into a PDF.", parse_mode="Markdown")
    elif action == "jpg2png":
        context.user_data["awaiting"] = "jpg2png"
        await query.edit_message_text(
            "🔁 Send me a *JPEG image* — for best quality, send it as a *file* (📎 → File), not a compressed photo.",
            parse_mode="Markdown",
        )
    elif action == "png2jpg":
        context.user_data["awaiting"] = "png2jpg"
        await query.edit_message_text(
            "🔁 Send me a *PNG image* as a *file* (📎 → File) — compressed photos are auto-converted to JPEG by Telegram already.",
            parse_mode="Markdown",
        )
    elif action == "md2txt":
        context.user_data["awaiting"] = "md2txt"
        await query.edit_message_text("📝 Send me a *.md file* and I'll convert it to plain *.txt*.", parse_mode="Markdown")
    else:
        await query.edit_message_text("❌ Unknown option.")


async def handle_translate_lang_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """User picked a target language for translation."""
    query = update.callback_query
    await query.answer()

    try:
        _, lang_code = query.data.split("|", 1)
    except ValueError:
        await query.edit_message_text("❌ Invalid selection.")
        return

    lang_label = next((label for code, label in TRANSLATE_LANGUAGES if code == lang_code), lang_code)
    context.user_data["awaiting"] = "translate_text"
    context.user_data["target_lang"] = lang_code
    await query.edit_message_text(
        f"✏️ Send me the text you'd like translated to *{lang_label}*.",
        parse_mode="Markdown",
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    raw_text = update.message.text

    # Greetings (hi/hello/hey/etc.) open the main menu, same as /start.
    if GREETING_RE.match(raw_text.strip()):
        context.user_data.pop("awaiting", None)
        await update.message.reply_text(
            WELCOME_MESSAGE,
            parse_mode="Markdown",
            reply_markup=build_main_menu_keyboard(),
        )
        return

    # Pending translation request takes priority over link detection.
    if context.user_data.get("awaiting") == "translate_text":
        target_lang = context.user_data.get("target_lang", "en")
        text_to_translate = raw_text.strip()
        if not text_to_translate:
            await update.message.reply_text("Please send some text to translate.")
            return
        if len(text_to_translate) > TRANSLATE_MAX_CHARS:
            await update.message.reply_text(
                f"⚠️ That's too long ({len(text_to_translate)} chars). "
                f"Please send under {TRANSLATE_MAX_CHARS} characters."
            )
            return
        status_message = await update.message.reply_text("🌐 Translating…")
        try:
            translated = await translate_text(text_to_translate, target_lang)
            await safe_edit(status_message, f"✅ *Translation:*\n\n{translated}", parse_mode="Markdown")
        except Exception as exc:
            logger.warning("Translation failed: %s", exc)
            await safe_edit(status_message, "❌ Sorry, translation failed. Please try again.")
        context.user_data.pop("awaiting", None)
        context.user_data.pop("target_lang", None)
        return

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
    application.add_handler(CommandHandler("menu", menu_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(CallbackQueryHandler(handle_effect_category_callback, pattern=r"^fxcat\|"))
    application.add_handler(CallbackQueryHandler(handle_effect_back_callback, pattern=r"^fxback\|"))
    application.add_handler(CallbackQueryHandler(handle_effect_callback, pattern=r"^fx\|"))
    application.add_handler(CallbackQueryHandler(handle_menu_callback, pattern=r"^menu\|"))
    application.add_handler(CallbackQueryHandler(handle_translate_lang_callback, pattern=r"^trlang\|"))
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
