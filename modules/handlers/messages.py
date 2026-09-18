"""All MessageHandler functions."""

from __future__ import annotations
import asyncio
import base64
import json
import logging
import re
import sys
import time
from io import BytesIO

from telegram import Update, InputMediaPhoto
from telegram.ext import ContextTypes

from modules import storage, image_extra, qr_tools
from modules.ui.strings import (
    WELCOME_MESSAGE, FIVER_INFO_PATTERN_HINT,
    TRANSLATE_MAX_CHARS, MYMEMORY_MAX_CHARS,
)
from modules.ui.keyboards import (
    build_main_menu_keyboard, build_effect_toolbox_keyboard,
    build_media_format_keyboard, build_watermark_position_keyboard,
    build_case_keyboard,
)
from modules.handlers.commands import (
    is_admin, _record, _download_file, _esc_md,
)
from modules.handlers.callbacks import (
    _store_token, _get_file_id, _drop_token,
    _store_side_data, _get_side_data,
    safe_edit, safe_delete, _user_hint,
    _check_heavy_rate_limit,
)
from modules.handlers.assets import (
    LUMMI_URL_RE, HUGEICONS_URL_RE, detect_platform,
    fetch_lummi_asset, fetch_hugeicons_svg,
)
from modules.tools.fiver_tools import (
    parse_fiver_info, sanitize_fiver_text,
    build_fiver_output, format_sanitizer_report,
)
from modules.tools.color_tools import handle_color_tool
from modules.tools.dev_tools import (
    format_json, hash_text, convert_timestamp,
    px_to_rem, url_encode, url_decode,
    base64_encode, base64_decode, word_count,
    diff_texts, regex_test, convert_case,
)
from modules import media_downloader

logger = logging.getLogger("bangaliicon.messages")

ENGINE_SCRIPT           = "python_engine.py"
IMAGE_MAX_DIM           = 1_200
GIF_MAX_FRAMES          = 10
DEFAULT_COMPRESS_TARGET = 1 * 1024 * 1024
MAX_UPLOAD_BYTES        = 49 * 1024 * 1024
BOT_DOWNLOAD_LIMIT      = 20 * 1024 * 1024
PDF2IMG_MAX_PAGES       = 20

GREETING_RE = re.compile(
    r"^(hi+|he+llo+|hey+|yo|start|salam|assalamu\s*alaikum|assalamualaikum)[!.\s]*$",
    re.IGNORECASE,
)
MEDIA_URL_RE = re.compile(
    r"https?://[^\s<>]*(?:youtube\.com|youtu\.be|x\.com|twitter\.com"
    r"|facebook\.com|fb\.watch)[^\s<>]*",
    re.IGNORECASE,
)


# ── Image processing helpers ──────────────────────────────────────────────────

def _get_image_dimensions(image_bytes: bytes) -> tuple[int, int]:
    try:
        from PIL import Image
        img  = Image.open(BytesIO(image_bytes))
        w, h = img.size
        if w > IMAGE_MAX_DIM or h > IMAGE_MAX_DIM:
            ratio = min(IMAGE_MAX_DIM / w, IMAGE_MAX_DIM / h)
            w, h  = int(w * ratio), int(h * ratio)
        return w, h
    except Exception:
        return 800, 600


async def _image_to_rgba_b64(image_bytes: bytes, width: int, height: int) -> str:
    from PIL import Image
    img = Image.open(BytesIO(image_bytes)).convert("RGBA").resize((width, height))
    return base64.b64encode(img.tobytes()).decode()


async def apply_effect_to_image(
    image_bytes: bytes, effect: str, width: int, height: int, params: dict | None = None
) -> bytes:
    from modules.ui.strings import DEFAULT_FX_PARAMS
    rgba_b64 = await _image_to_rgba_b64(image_bytes, width, height)
    payload  = json.dumps({
        "effect":           effect,
        "width":            width,
        "height":           height,
        "pixels_rgba_b64":  rgba_b64,
        "params":           params or DEFAULT_FX_PARAMS,
    })
    proc = await asyncio.create_subprocess_exec(
        sys.executable, ENGINE_SCRIPT,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(
        proc.communicate(payload.encode()), timeout=120
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Engine error: {stderr.decode()[:300]}")
    result = json.loads(stdout.decode())
    if result.get("status") != "success":
        raise RuntimeError(result.get("message", "Unknown engine error"))
    bmp_bytes = base64.b64decode(result["bmp_data_url"].split(",", 1)[1])
    from PIL import Image
    png_buf = BytesIO()
    Image.open(BytesIO(bmp_bytes)).convert("RGBA").save(
        png_buf, format="PNG", optimize=True
    )
    return png_buf.getvalue()


async def convert_image_format(image_bytes: bytes, target_format: str) -> bytes:
    from PIL import Image, ImageOps

    def _run():
        img = ImageOps.exif_transpose(Image.open(BytesIO(image_bytes)))
        out = BytesIO()
        if target_format.upper() == "JPEG":
            img.convert("RGB").save(
                out, format="JPEG", quality=100, subsampling=0, optimize=True
            )
        else:
            img.convert("RGBA").save(out, format="PNG", optimize=True)
        return out.getvalue()

    return await asyncio.to_thread(_run)


async def image_to_pdf(image_bytes: bytes) -> bytes:
    from PIL import Image, ImageOps

    def _run():
        out = BytesIO()
        ImageOps.exif_transpose(
            Image.open(BytesIO(image_bytes))
        ).convert("RGB").save(out, format="PDF", quality=100, resolution=300.0)
        return out.getvalue()

    return await asyncio.to_thread(_run)


async def pdf_to_images(pdf_bytes: bytes, max_pages: int = PDF2IMG_MAX_PAGES) -> list[bytes]:
    import fitz

    def _run():
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        try:
            return [
                p.get_pixmap(dpi=200).tobytes("png")
                for i, p in enumerate(doc) if i < max_pages
            ]
        finally:
            doc.close()

    return await asyncio.to_thread(_run)


async def sticker_to_png(sticker_bytes: bytes) -> bytes:
    from PIL import Image

    def _run():
        buf = BytesIO()
        Image.open(BytesIO(sticker_bytes)).convert("RGBA").save(
            buf, format="PNG", optimize=True
        )
        return buf.getvalue()

    return await asyncio.to_thread(_run)


async def gif_to_frames(gif_bytes: bytes, max_frames: int = GIF_MAX_FRAMES) -> list[bytes]:
    from PIL import Image, ImageSequence

    def _run():
        img     = Image.open(BytesIO(gif_bytes))
        frames  = list(ImageSequence.Iterator(img))
        total   = len(frames)
        if total == 0:
            raise ValueError("GIF contains no frames.")
        indices = (
            list(range(total)) if total <= max_frames
            else [int(i * total / max_frames) for i in range(max_frames)]
        )
        out = []
        for idx in indices:
            buf = BytesIO()
            frames[idx].convert("RGBA").save(buf, format="PNG", optimize=True)
            out.append(buf.getvalue())
        return out

    return await asyncio.to_thread(_run)


# ── Resize helper ─────────────────────────────────────────────────────────────

def _ratio_key_to_wh(ratio_key: str) -> tuple[int, int]:
    mapping = {
        "1:1": (1,1), "4:5": (4,5), "9:16": (9,16),
        "16:9": (16,9), "4:3": (4,3), "3:2": (3,2),
    }
    return mapping.get(ratio_key, (1, 1))


async def run_resize_tool(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    file_id: str, mode: str, value
) -> None:
    if not await _check_heavy_rate_limit(update, "effects"):
        return
    status = await update.effective_message.reply_text("⏳ Resizing…")
    try:
        raw = await _download_file(context, file_id)
        if mode == "width":
            from PIL import Image
            img     = Image.open(BytesIO(raw))
            w0, h0  = img.size
            new_w   = int(value)
            new_h   = max(1, int(h0 * (new_w / w0)))
            out     = await image_extra.resize_image(raw, new_w, new_h)
            fname   = f"resized_{new_w}x{new_h}.jpg"
        elif mode == "wh":
            w, h  = value
            out   = await image_extra.resize_image(raw, w, h)
            fname = f"resized_{w}x{h}.jpg"
        else:  # ratio
            ratio_key, crop_mode = value
            rw, rh = _ratio_key_to_wh(ratio_key)
            out    = await image_extra.crop_to_aspect(raw, rw, rh, crop_mode)
            fname  = f"aspect_{ratio_key.replace(':','-')}.jpg"

        doc = BytesIO(out); doc.name = fname
        await update.effective_message.reply_document(document=doc, filename=fname)
        await safe_delete(status)
        await _record(context, update, "resizetool")
    except Exception as exc:
        logger.warning("resizetool failed: %s", exc)
        await safe_edit(status, _user_hint(exc))


# ── Media download ────────────────────────────────────────────────────────────

async def run_media_download(
    update: Update, context: ContextTypes.DEFAULT_TYPE, url: str, fmt: str
) -> None:
    if not await _check_heavy_rate_limit(update, "mediadownload"):
        return
    status = await update.effective_message.reply_text(
        "⏳ Fetching media (this can take a couple of minutes)…"
    )
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
            await safe_edit(
                status,
                "📦 The downloaded file exceeds Telegram's 49 MB limit. "
                "Try a shorter clip or audio-only.",
            ); return

        doc = BytesIO(data); doc.name = fname
        await update.effective_message.reply_document(
            document=doc, filename=fname,
            caption=f"✅ {title[:200]}",
        )
        await safe_delete(status)
        await _record(context, update, "mediadownload")
    except Exception as exc:
        logger.warning("mediadownload failed: %s", exc)
        await safe_edit(status, _user_hint(exc))


# ── Translation ───────────────────────────────────────────────────────────────

async def translate_text(text: str, target_lang: str) -> str:
    _RATE_MARKERS = ("429", "too many requests", "rate limit", "quota")

    async def _google():
        from deep_translator import GoogleTranslator
        return await asyncio.to_thread(
            lambda: GoogleTranslator(source="auto", target=target_lang).translate(text)
        )

    async def _mymemory():
        from deep_translator import MyMemoryTranslator
        if len(text) <= MYMEMORY_MAX_CHARS:
            return await asyncio.to_thread(
                lambda: MyMemoryTranslator(source="auto", target=target_lang).translate(text)
            )
        chunks, cur = [], ""
        for word in text.split(" "):
            cand = f"{cur} {word}".strip()
            if len(cand) > MYMEMORY_MAX_CHARS and cur:
                chunks.append(cur); cur = word
            else:
                cur = cand
        if cur:
            chunks.append(cur)
        return " ".join([
            await asyncio.to_thread(
                lambda c=c: MyMemoryTranslator(source="auto", target=target_lang).translate(c)
            ) for c in chunks
        ])

    last_exc = None
    for delay in (0, 2, 4):
        if delay: await asyncio.sleep(delay)
        try:
            return await _google()
        except Exception as exc:
            last_exc = exc
            if not any(m in str(exc).lower() for m in _RATE_MARKERS):
                break
    try:
        return await _mymemory()
    except Exception as exc:
        raise RuntimeError("rate_limited") from (last_exc or exc)


# ── Single image tool runner ──────────────────────────────────────────────────

_SINGLE_IMAGE_TOOLS = {
    "bgremove":    ("Removing background",  "background_removed.png"),
    "img2pdf":     ("Converting to PDF",    "image.pdf"),
    "jpg2png":     ("Converting to PNG",    "converted.png"),
    "png2jpg":     ("Converting to JPEG",   "converted.jpg"),
    "sticker2png": ("Converting sticker",   "sticker.png"),
    "copyimage":   ("Copying file",         "copy.jpg"),
}

_TOOL_TRANSFORMS = {
    "bgremove":    lambda b: _run_bgremove(b),
    "img2pdf":     lambda b: image_to_pdf(b),
    "jpg2png":     lambda b: convert_image_format(b, "PNG"),
    "png2jpg":     lambda b: convert_image_format(b, "JPEG"),
    "sticker2png": lambda b: sticker_to_png(b),
    "copyimage":   lambda b: asyncio.coroutine(lambda: b)(),
}


async def _run_bgremove(image_bytes: bytes) -> bytes:
    """Background removal using u2netp model."""
    from PIL import Image, ImageOps
    import numpy as np
    import onnxruntime as ort
    import os, tempfile, httpx

    MODEL_URL  = "https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2netp.onnx"
    MODEL_PATH = os.path.join(tempfile.gettempdir(), "u2netp.onnx")
    REMBG_MAX  = 2_000

    def _ensure_model():
        if os.path.exists(MODEL_PATH) and os.path.getsize(MODEL_PATH) > 1_000_000:
            return
        with httpx.Client(timeout=60.0, follow_redirects=True) as c:
            r = c.get(MODEL_URL); r.raise_for_status()
            tmp = MODEL_PATH + ".part"
            with open(tmp, "wb") as f: f.write(r.content)
            os.replace(tmp, MODEL_PATH)

    def _run():
        _ensure_model()
        session = ort.InferenceSession(MODEL_PATH, providers=["CPUExecutionProvider"])
        img = ImageOps.exif_transpose(
            Image.open(BytesIO(image_bytes))
        ).convert("RGB")
        if max(img.size) > REMBG_MAX:
            img.thumbnail((REMBG_MAX, REMBG_MAX), Image.LANCZOS)

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

        from PIL import ImageFilter
        mask = Image.fromarray(
            (pred * 255).astype("uint8"), "L"
        ).resize(img.size, Image.Resampling.LANCZOS).filter(
            ImageFilter.GaussianBlur(1.0)
        )
        out = img.convert("RGBA"); out.putalpha(mask)
        buf = BytesIO()
        out.save(buf, format="PNG", optimize=True)
        return buf.getvalue()

    return await asyncio.wait_for(asyncio.to_thread(_run), timeout=90)


async def _run_image_tool(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    file_id: str, tool: str,
) -> None:
    if not await _check_heavy_rate_limit(update, tool):
        return
    label, filename = _SINGLE_IMAGE_TOOLS[tool]
    status = await update.message.reply_text(f"⏳ {label}…")
    try:
        raw = await _download_file(context, file_id)
        if tool in storage.HEAVY_TOOLS:
            async with storage.HEAVY_JOB_SEMAPHORE:
                out = await _TOOL_TRANSFORMS[tool](raw)
        else:
            out = await _TOOL_TRANSFORMS[tool](raw)
        doc = BytesIO(out); doc.name = filename
        await update.message.reply_document(document=doc, filename=filename)
        await safe_delete(status)
        await _record(context, update, tool)
    except Exception as exc:
        logger.warning("%s failed: %s", tool, exc)
        await safe_edit(status, _user_hint(exc))


# ── Photo handler ─────────────────────────────────────────────────────────────

async def handle_photo(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not update.message or not update.message.photo:
        return
    awaiting = context.user_data.get("awaiting")
    file_id  = update.message.photo[-1].file_id

    if awaiting == "watermark_setup":
        await storage.set_watermark(update.effective_chat.id, file_id)
        context.user_data["awaiting"] = None
        await update.message.reply_text(
            "✅ Watermark saved!\n\n"
            "Now choose a position:",
            reply_markup=build_watermark_position_keyboard(),
        ); return

    if awaiting == "watermark_apply":
        wm_file_id = await storage.get_watermark(update.effective_chat.id)
        if not wm_file_id:
            await update.message.reply_text(
                "⚠️ No watermark saved. Send `/watermark` first."
            ); return
        status = await update.message.reply_text("⏳ Applying watermark…")
        try:
            base_bytes = await _download_file(context, file_id)
            wm_bytes   = await _download_file(context, wm_file_id)
            position   = context.user_data.get("watermark_position", "bottom-right")
            out        = await image_extra.apply_watermark(base_bytes, wm_bytes, position=position)
            doc = BytesIO(out); doc.name = "watermarked.jpg"
            await update.message.reply_document(document=doc, filename="watermarked.jpg",
                caption="✅ Watermark applied!")
            await safe_delete(status)
            await _record(context, update, "watermark")
        except Exception as exc:
            await safe_edit(status, _user_hint(exc))
        context.user_data["awaiting"] = None
        return

    if awaiting == "watermark_remove":
        if not await _check_heavy_rate_limit(update, "watermark_remove"):
            return
        status = await update.message.reply_text("⏳ Removing watermark…")
        try:
            raw = await _download_file(context, file_id)
            async with storage.HEAVY_JOB_SEMAPHORE:
                out = await image_extra.remove_watermark_cv2(raw)
            doc = BytesIO(out); doc.name = "clean.jpg"
            await update.message.reply_document(
                document=doc, filename="clean.jpg",
                caption="✅ Watermark removal attempted.",
            )
            await safe_delete(status)
            await _record(context, update, "watermark_remove")
        except Exception as exc:
            await safe_edit(status, _user_hint(exc))
        context.user_data["awaiting"] = None
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
            await update.message.reply_document(
                document=doc, filename="compressed.jpg",
                caption=f"✅ {len(out)/1024:.0f} KB (target ~{target//1024} KB)",
            )
            await safe_delete(status)
            await _record(context, update, "compress")
        except Exception as exc:
            await safe_edit(status, _user_hint(exc))
        context.user_data["awaiting"] = None
        return

    if awaiting == "qrscan":
        status = await update.message.reply_text("🔍 Scanning…")
        try:
            raw     = await _download_file(context, file_id)
            results = await qr_tools.scan_qr(raw)
            if not results:
                await safe_edit(status, "❌ No QR code found.")
            else:
                await safe_edit(
                    status,
                    "✅ Found:\n" + "\n".join(f"`{r}`" for r in results),
                    parse_mode="Markdown",
                )
            await _record(context, update, "qrscan")
        except Exception as exc:
            await safe_edit(status, _user_hint(exc))
        context.user_data["awaiting"] = None
        return

    if awaiting in _SINGLE_IMAGE_TOOLS:
        await _run_image_tool(update, context, file_id, awaiting)
        context.user_data["awaiting"] = None
        return

    # Default → show toolbox
    token = _store_token(context.bot_data, file_id)
    await update.message.reply_text(
        "🧰 *Image Toolbox* — pick an option:",
        parse_mode="Markdown",
        reply_markup=build_effect_toolbox_keyboard(token),
    )


# ── Document handler ──────────────────────────────────────────────────────────

async def handle_document(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not update.message or not update.message.document:
        return
    doc      = update.message.document
    awaiting = context.user_data.get("awaiting")

    if awaiting in _SINGLE_IMAGE_TOOLS:
        await _run_image_tool(update, context, doc.file_id, awaiting)
        context.user_data["awaiting"] = None
        return

    if awaiting == "gif2frames":
        await _run_gif_tool(update, context, doc.file_id)
        context.user_data["awaiting"] = None
        return

    if (doc.mime_type or "").startswith("image/"):
        token = _store_token(context.bot_data, doc.file_id)
        await update.message.reply_text(
            "🧰 *Image Toolbox* — pick an option:",
            parse_mode="Markdown",
            reply_markup=build_effect_toolbox_keyboard(token),
        )
    else:
        await update.message.reply_text(
            "📎 Got your file — use /menu to pick a tool, or send after choosing one."
        )


async def _run_gif_tool(
    update: Update, context: ContextTypes.DEFAULT_TYPE, file_id: str
) -> None:
    status = await update.message.reply_text(
        f"⏳ Extracting GIF frames (up to {GIF_MAX_FRAMES})…"
    )
    try:
        gif_bytes = await _download_file(context, file_id)
        frames    = await gif_to_frames(gif_bytes)
        if not frames:
            await safe_edit(status, "❌ No frames could be extracted."); return
        await safe_delete(status)
        for i in range(0, len(frames), 10):
            await update.message.reply_media_group(
                [InputMediaPhoto(BytesIO(f)) for f in frames[i:i+10]]
            )
        await update.message.reply_text(f"✅ {len(frames)} frame(s) extracted.")
        await _record(context, update, "gif2frames")
    except Exception as exc:
        await safe_edit(status, _user_hint(exc))


# ── Sticker / GIF handlers ────────────────────────────────────────────────────

async def handle_sticker(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not update.message or not update.message.sticker:
        return
    if update.message.sticker.is_animated or update.message.sticker.is_video:
        await update.message.reply_text(
            "⚠️ Only static stickers can be converted to PNG."
        ); return
    await _run_image_tool(update, context, update.message.sticker.file_id, "sticker2png")


async def handle_gif(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not update.message or not update.message.animation:
        return
    await _run_gif_tool(update, context, update.message.animation.file_id)


# ── Text handler ──────────────────────────────────────────────────────────────

async def handle_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not update.message or not update.message.text:
        return
    text     = update.message.text.strip()
    awaiting = context.user_data.get("awaiting")

    # Media URL detection
    media_match = MEDIA_URL_RE.search(text)
    if media_match and awaiting in (None, "mediadownload"):
        url   = media_match.group(0).rstrip(".,!?;:)]}>\"'")
        token = _store_token(context.bot_data, url)
        await update.message.reply_text(
            "🎬 Choose a format:",
            reply_markup=build_media_format_keyboard(token),
        )
        context.user_data["awaiting"] = None
        return

    # Lummi / Hugeicons URL detection
    lummi_match = LUMMI_URL_RE.search(text)
    huge_match  = HUGEICONS_URL_RE.search(text)

    if lummi_match or huge_match:
        url      = (lummi_match or huge_match).group(0).rstrip(".,!?;:")
        platform = detect_platform(url)
        status   = await update.message.reply_text("⏳ Fetching asset…")
        try:
            if platform == "hugeicons":
                from modules.ui.keyboards import build_hugeicons_size_keyboard
                data  = await fetch_hugeicons_svg(url)
                token = _store_token(context.bot_data, "icon")
                _store_side_data(context.bot_data, "icon_kind",    token, "hugeicons")
                _store_side_data(context.bot_data, "icon_payload", token, data)
                await safe_edit(
                    status,
                    f"🎨 *{data['icon_name']}* (`{data['style']}`)\n\n"
                    f"📐 Choose a size:",
                    parse_mode="Markdown",
                    reply_markup=build_hugeicons_size_keyboard(token),
                )
            elif platform == "lummi":
                from modules.ui.keyboards import build_lummi_size_keyboard
                data  = await fetch_lummi_asset(url)
                token = _store_token(context.bot_data, "icon")
                _store_side_data(context.bot_data, "icon_kind",    token, "lummi")
                _store_side_data(context.bot_data, "icon_payload", token, data)

                if data.get("is_3d"):
                    # 3D asset — deliver directly
                    doc = BytesIO(data["bytes"]); doc.name = data["filename"]
                    await safe_edit(status, f"📦 3D asset found — delivering `{data['filename']}`…")
                    await update.message.reply_document(
                        document=doc,
                        filename=data["filename"],
                        caption=(
                            f"✅ *3D Model* — `{data['filename']}`\n"
                            f"📦 {data['size_mb']:.2f} MB"
                        ),
                        parse_mode="Markdown",
                    )
                    await _record(context, update, "lummi_3d")
                else:
                    await safe_edit(
                        status,
                        f"🖼 *Lummi Asset*\n"
                        f"📦 {data['size_mb']:.2f} MB original\n\n"
                        f"Choose a size:",
                        parse_mode="Markdown",
                        reply_markup=build_lummi_size_keyboard(token),
                    )
            else:
                await safe_edit(status, "⚠️ Unsupported link format.")
        except Exception as exc:
            await safe_edit(status, _user_hint(exc))
        return

    # Greeting
    if GREETING_RE.match(text) and awaiting is None:
        await update.message.reply_text(
            WELCOME_MESSAGE, parse_mode="Markdown",
            reply_markup=build_main_menu_keyboard(),
        ); return

    # Custom WxH resize
    if awaiting == "resize_custom_wh":
        import re as _re
        m = _re.match(r"^\s*(\d{1,5})\s*[xX,]\s*(\d{1,5})\s*$", text)
        if not m:
            await update.message.reply_text(
                "⚠️ Format: `WIDTHxHEIGHT`, e.g. `1080x1350`.",
                parse_mode="Markdown",
            ); return
        w, h    = int(m.group(1)), int(m.group(2))
        token   = context.user_data.get("resize_wh_token")
        file_id = _get_file_id(context.bot_data, token) if token else None
        if not file_id:
            await update.message.reply_text("❌ Request expired — please resend the photo.")
            context.user_data["awaiting"] = None
            return
        await run_resize_tool(update, context, file_id, "wh", (w, h))
        context.user_data["awaiting"] = None
        return

    # Fiver info
    if awaiting == "fiver_info":
        info = parse_fiver_info(text)
        if not info:
            await update.message.reply_text(
                f"❌ ফরম্যাট মিলছে না: `{FIVER_INFO_PATTERN_HINT}`",
                parse_mode="Markdown",
            ); return
        context.user_data["fiver_info"] = info
        context.user_data["awaiting"]   = "fiver_body"
        await update.message.reply_text(
            "✅ পাওয়া গেছে! এখন মেসেজের বডি পাঠান।"
        ); return

    # Fiver body
    if awaiting == "fiver_body":
        info = context.user_data.get("fiver_info")
        if not info:
            await update.message.reply_text(
                "⚠️ Order info missing — `/FiverMessage` দিয়ে আবার শুরু করুন।"
            )
            context.user_data["awaiting"] = None
            return
        custom_words          = await storage.get_words(update.effective_chat.id)
        sanitized, changes, score = sanitize_fiver_text(text, custom_words)
        output                = build_fiver_output(info, sanitized)
        report                = format_sanitizer_report(changes, score)
        await update.message.reply_text(f"```\n{output}\n```", parse_mode="Markdown")
        await update.message.reply_text(report, parse_mode="Markdown")
        context.user_data["awaiting"] = None
        context.user_data.pop("fiver_info", None)
        await _record(context, update, "fiver_sanitize")
        return

    # Translate
    if awaiting == "translate_text":
        lang = context.user_data.get("target_lang", "en")
        if len(text) > TRANSLATE_MAX_CHARS:
            await update.message.reply_text(
                f"⚠️ Max {TRANSLATE_MAX_CHARS} characters."
            ); return
        status = await update.message.reply_text("⏳ Translating…")
        try:
            translated = await translate_text(text, lang)
            await safe_edit(status, translated)
            await _record(context, update, "translate")
        except Exception:
            await safe_edit(
                status,
                "⏳ Translation service is rate-limited — please retry shortly."
            )
        context.user_data["awaiting"] = None
        return

    # Color tool
    if awaiting == "color_input":
        tool   = context.user_data.get("color_tool", "convert")
        result = await handle_color_tool(tool, text)
        await update.message.reply_text(result, parse_mode="Markdown")
        context.user_data["awaiting"] = None
        return

    # Dev tools
    if awaiting == "dev_input":
        await _handle_dev_input(update, context, text)
        return

    if awaiting == "qrscan":
        await update.message.reply_text(
            "📸 Please send the QR code as a *photo*.", parse_mode="Markdown"
        ); return

    await update.message.reply_text("🤔 Not sure what to do — try /menu.")


async def _handle_dev_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
) -> None:
    tool = context.user_data.get("dev_tool")

    if tool == "case_text":
        context.user_data["case_input"] = text
        context.user_data["awaiting"]   = None
        await update.message.reply_text(
            "🔤 Choose a case:", reply_markup=build_case_keyboard()
        ); return

    if tool == "diff" and "diff_text_a" not in context.user_data:
        context.user_data["diff_text_a"] = text
        await update.message.reply_text(
            "✅ Got Text A. Now send *Text B*:", parse_mode="Markdown"
        ); return

    handlers = {
        "json":      format_json,
        "hash":      hash_text,
        "timestamp": convert_timestamp,
        "pxrem":     lambda t: px_to_rem(
            float(re.sub(r"[^\d.]", "", t) or 0)
        ),
        "urlencode": url_encode,
        "urldecode": url_decode,
        "b64enc":    base64_encode,
        "b64dec":    base64_decode,
        "wordcount": word_count,
        "regex":     lambda t: (
            regex_test(*t.split("\n", 1)) if "\n" in t
            else "⚠️ Need pattern + text on separate lines."
        ),
    }

    if tool == "diff":
        a      = context.user_data.pop("diff_text_a", "")
        result = diff_texts(a, text)
        await update.message.reply_text(
            f"```diff\n{result}\n```", parse_mode="Markdown"
        )
    elif tool in handlers:
        try:
            result = handlers[tool](text)
        except Exception as exc:
            result = f"❌ {exc}"
        await update.message.reply_text(
            f"`{result}`" if len(result) < 100 else f"```\n{result}\n```",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text("⚠️ Unknown tool.")

    context.user_data["awaiting"] = None


# ── Error handler ─────────────────────────────────────────────────────────────

async def on_error(
    update: object, context: ContextTypes.DEFAULT_TYPE
) -> None:
    logger.exception("Unhandled error", exc_info=context.error)
