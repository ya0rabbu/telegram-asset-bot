"""All CallbackQueryHandler functions."""

from __future__ import annotations
import asyncio
import logging
import time
from io import BytesIO

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from modules import storage, image_extra
from modules.ui.strings import (
    EFFECT_NAMES, DEFAULT_FX_PARAMS, MENU_INTRO,
    TRANSLATE_LANGUAGES,
)
from modules.ui.keyboards import (
    build_main_menu_keyboard, build_category_keyboard,
    build_effect_toolbox_keyboard, build_effect_categories_keyboard,
    build_effect_keyboard, build_aspect_ratio_keyboard,
    build_resize_presets_keyboard, build_param_keyboard,
    build_translate_lang_keyboard, build_watermark_position_keyboard,
    build_genpass_keyboard, build_limit_request_keyboard,
    build_dev_input_keyboard, build_case_keyboard,
    build_media_format_keyboard,
)
from modules.handlers.commands import (
    is_admin, is_super_admin, _record,
    _download_file, _get_profile_photo_bytes,
    _resolve_target_chat_id_async, get_admin_chat_ids,
    admin_role_label, _esc_md,
)
from modules.handlers.assets import (
    deliver_lummi, deliver_hugeicons,
    _store_side_data, _get_side_data, _drop_side_data,
)
from modules import password_tools
from modules.tools.dev_tools import (
    generate_uuid, lorem_ipsum, convert_case,
)

logger = logging.getLogger("bangaliicon.callbacks")

DEFAULT_COMPRESS_TARGET = 1 * 1024 * 1024
IMAGE_MAX_DIM           = 1_200
MAX_UPLOAD_BYTES        = 49 * 1024 * 1024
TOKEN_TTL_SECONDS       = 1_800


# ── Token helpers ─────────────────────────────────────────────────────────────

def _store_token(bot_data: dict, file_id: str) -> str:
    import uuid
    pending: dict = bot_data.setdefault("pending_files", {})
    now     = time.monotonic()
    expired = [k for k, (_, ts) in pending.items() if now - ts > TOKEN_TTL_SECONDS]
    for k in expired:
        pending.pop(k, None)
    token = uuid.uuid4().hex[:10]
    pending[token] = (file_id, now)
    return token


def _get_file_id(bot_data: dict, token: str) -> str | None:
    entry = bot_data.get("pending_files", {}).get(token)
    if not entry:
        return None
    file_id, ts = entry
    if time.monotonic() - ts > TOKEN_TTL_SECONDS:
        bot_data["pending_files"].pop(token, None)
        return None
    return file_id


def _drop_token(bot_data: dict, token: str) -> None:
    bot_data.get("pending_files", {}).pop(token, None)


# ── Safe helpers ──────────────────────────────────────────────────────────────

async def safe_edit(message, text: str, **kwargs) -> None:
    from telegram.error import BadRequest
    try:
        await message.edit_text(text, **kwargs)
    except BadRequest as exc:
        if "message to edit not found" not in str(exc).lower():
            raise


async def safe_delete(message) -> None:
    from telegram.error import BadRequest
    try:
        await message.delete()
    except BadRequest:
        pass


def _user_hint(exc: Exception) -> str:
    msg = str(exc).lower()
    if "timeout"   in msg: return "⏱ The operation timed out. Try a smaller file."
    if "too large" in msg: return "📦 File exceeds the size limit."
    if "memory"    in msg: return "💾 Server ran low on memory. Try a smaller image."
    return f"❌ Something went wrong: {exc}"


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


# ── Menu & category callbacks ─────────────────────────────────────────────────

async def handle_menu_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query; await query.answer()
    try: _, action = query.data.split("|", 1)
    except ValueError: await query.edit_message_text("❌ Invalid."); return

    _TOOL_PROMPTS = {
        "effects":          ("effects",           "🎨 Send me a *photo* — I'll show the toolbox."),
        "bgremove":         ("bgremove",           "🧹 Send me a *photo* — I'll remove the background."),
        "sticker2png":      ("sticker2png",        "😄 Send me a *static sticker* to convert to PNG."),
        "gif2frames":       ("gif2frames",         "🎞 Send me a *GIF* — I'll extract frames."),
        "img2pdf":          ("img2pdf",            "🖼 Send me an *image* — I'll wrap it into a PDF."),
        "pdf2img":          ("pdf2img",            "📄 Send me a *PDF* — I'll render the pages."),
        "jpg2png":          ("jpg2png",            "🔁 Send me a *JPEG* as a file."),
        "png2jpg":          ("png2jpg",            "🔁 Send me a *PNG* as a file."),
        "resizetool":       ("effects",            "📐 Send me a *photo* — you'll get the Resize toolbox."),
        "copyimage":        ("copyimage",          "📋 Send me a *photo* — I'll send it back as a document."),
        "watermark_remove": ("watermark_remove",   "🚫 Send me a *photo* — I'll try to remove the watermark."),
        "compress":         ("compress_image",     "📉 Send me the photo to compress."),
        "qrscan":           ("qrscan",             "🔍 Send me a photo containing a QR code."),
        "mediadownload":    ("mediadownload",      "🎬 Send me a *YouTube, X or Facebook* video link."),
        "fiversanitize":    ("fiver_info",         "🛡 প্রথমে অর্ডার তথ্য দিন: `ClientName_OrderID_ProjectName_ProfileName_Amount`"),
    }

    if action == "translate":
        await query.edit_message_text(
            "🌐 *Choose target language:*", parse_mode="Markdown",
            reply_markup=build_translate_lang_keyboard(),
        ); return

    if action == "watermark":
        context.user_data["awaiting"] = "watermark_setup"
        await query.edit_message_text(
            "💧 Send your *logo/signature image* first.", parse_mode="Markdown"
        ); return

    if action == "genpass":
        await query.edit_message_text(
            "🔑 Choose a length:", reply_markup=build_genpass_keyboard()
        ); return

    if action == "qrgen":
        await query.edit_message_text(
            "🔳 Send `/qr <text or link>` to generate a QR code.",
            parse_mode="Markdown",
        ); return

    if action == "mywords":
        from modules import storage
        words = await storage.get_words(query.message.chat_id)
        if not words:
            await query.edit_message_text(
                "No custom words yet. Use `/addword <word> <replacement>`.",
                parse_mode="Markdown",
            )
        else:
            preview = "\n".join(
                f"• `{w}` → `{r}`"
                for w, r in list(sorted(words.items()))[:20]
            )
            await query.edit_message_text(
                f"📚 *Custom words* ({len(words)} total):\n\n{preview}",
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


async def handle_category_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query; await query.answer()
    try: _, cat = query.data.split("|", 1)
    except ValueError: await query.edit_message_text("❌ Invalid."); return

    if cat == "back":
        await query.edit_message_text(
            MENU_INTRO, parse_mode="Markdown",
            reply_markup=build_main_menu_keyboard(),
        ); return

    labels = {
        "image": "🖼 Image Tools", "pdf": "📄 PDF Tools",
        "word":  "📝 Documents",   "sheet": "📊 Spreadsheet",
        "ppt":   "📽 Presentation","text":  "✍️ Text & Markup",
        "color": "🎨 Colour Tools","dev":   "🔧 Dev Tools",
        "more":  "📚 More",
    }
    await query.edit_message_text(
        f"*{labels.get(cat, cat.title())}* — choose a tool:",
        parse_mode="Markdown",
        reply_markup=build_category_keyboard(cat),
    )


# ── Asset size callbacks ──────────────────────────────────────────────────────

async def handle_lummi_size_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query; await query.answer()
    try: _, size_choice, token = query.data.split("|", 2)
    except ValueError: await query.edit_message_text("❌ Invalid."); return
    await safe_edit(query.message, "⏳ Preparing your asset…")
    await deliver_lummi(update, context, token, size_choice)


async def handle_hugeicons_size_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query; await query.answer()
    try: _, size_choice, token = query.data.split("|", 2)
    except ValueError: await query.edit_message_text("❌ Invalid."); return
    await safe_edit(query.message, "⏳ Preparing your icon…")
    await deliver_hugeicons(update, context, token, size_choice)


# ── Effect toolbox callbacks ──────────────────────────────────────────────────

async def handle_effect_toolbox_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query; await query.answer()
    try: _, action, token = query.data.split("|", 2)
    except ValueError: await query.edit_message_text("❌ Invalid."); return

    file_id = _get_file_id(context.bot_data, token)
    if not file_id:
        await query.edit_message_text("❌ Request expired — please resend the photo."); return

    if action == "effect":
        await query.edit_message_text(
            "🎨 *Choose a category:*", parse_mode="Markdown",
            reply_markup=build_effect_categories_keyboard(token),
        ); return

    if action == "aspect":
        await query.edit_message_text(
            "📐 *Choose an aspect ratio:*", parse_mode="Markdown",
            reply_markup=build_aspect_ratio_keyboard(token),
        ); return

    if action == "resize":
        await query.edit_message_text(
            "📏 *Choose a size preset:*", parse_mode="Markdown",
            reply_markup=build_resize_presets_keyboard(token),
        ); return

    if action == "params":
        params = _get_side_data(context.bot_data, "fx_params", token) or dict(DEFAULT_FX_PARAMS)
        _store_side_data(context.bot_data, "fx_params", token, params)
        await query.edit_message_text(
            "🎛 *Customize parameters:*", parse_mode="Markdown",
            reply_markup=build_param_keyboard(token, params),
        ); return

    if action == "copy":
        raw = await _download_file(context, file_id)
        doc = BytesIO(raw); doc.name = "copy.jpg"
        await query.message.reply_document(document=doc, filename="copy.jpg")
        _drop_token(context.bot_data, token)
        return

    if action == "convert":
        await query.edit_message_text(
            "🔁 Convert to:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("→ PNG",  callback_data=f"fxconvert|png|{token}"),
                 InlineKeyboardButton("→ JPEG", callback_data=f"fxconvert|jpeg|{token}")],
                [InlineKeyboardButton("→ PDF",  callback_data=f"fxconvert|pdf|{token}")],
                [InlineKeyboardButton("◀ Back", callback_data=f"fxtoolback|{token}")],
            ]),
        ); return

    if action == "compress":
        if not await _check_heavy_rate_limit(update, "compress"):
            return
        status = await query.message.reply_text("⏳ Compressing…")
        try:
            raw = await _download_file(context, file_id)
            async with storage.HEAVY_JOB_SEMAPHORE:
                out = await image_extra.compress_to_target(raw, DEFAULT_COMPRESS_TARGET)
            doc = BytesIO(out); doc.name = "compressed.jpg"
            await query.message.reply_document(
                document=doc, filename="compressed.jpg",
                caption=f"✅ {len(out)/1024:.0f} KB",
            )
            await safe_delete(status)
            await _record(context, update, "compress")
        except Exception as exc:
            await safe_edit(status, _user_hint(exc))
        return

    if action == "wmremove":
        if not await _check_heavy_rate_limit(update, "watermark_remove"):
            return
        status = await query.message.reply_text("⏳ Removing watermark…")
        try:
            raw = await _download_file(context, file_id)
            async with storage.HEAVY_JOB_SEMAPHORE:
                out = await image_extra.remove_watermark_cv2(raw)
            doc = BytesIO(out); doc.name = "clean.jpg"
            await query.message.reply_document(
                document=doc, filename="clean.jpg",
                caption="✅ Watermark removal attempted.",
            )
            await safe_delete(status)
            await _record(context, update, "watermark_remove")
        except Exception as exc:
            await safe_edit(status, _user_hint(exc))
        return

    if action == "go":
        await query.edit_message_text(
            "🎨 *Choose a category:*", parse_mode="Markdown",
            reply_markup=build_effect_categories_keyboard(token),
        ); return


async def handle_fx_toolback_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query; await query.answer()
    try: _, token = query.data.split("|", 1)
    except ValueError: return
    if not _get_file_id(context.bot_data, token):
        await query.edit_message_text("❌ Request expired."); return
    await query.edit_message_text(
        "🧰 *Image Toolbox* — pick an option:", parse_mode="Markdown",
        reply_markup=build_effect_toolbox_keyboard(token),
    )


async def handle_fx_convert_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query; await query.answer()
    try: _, fmt, token = query.data.split("|", 2)
    except ValueError: return
    file_id = _get_file_id(context.bot_data, token)
    if not file_id:
        await query.edit_message_text("❌ Request expired."); return
    await query.edit_message_text(f"⏳ Converting to {fmt.upper()}…")
    try:
        from modules.handlers.messages import (
            convert_image_format, image_to_pdf,
        )
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


async def handle_effect_category_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query; await query.answer()
    try: _, category, token = query.data.split("|", 2)
    except ValueError: return
    if not _get_file_id(context.bot_data, token):
        await query.edit_message_text("❌ Request expired."); return
    from modules.ui.strings import EFFECT_CATEGORIES
    await query.edit_message_text(
        f"🎨 *{EFFECT_CATEGORIES.get(category, category)}* — choose an effect:",
        parse_mode="Markdown",
        reply_markup=build_effect_keyboard(category, token),
    )


async def handle_effect_back_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query; await query.answer()
    try: _, token = query.data.split("|", 1)
    except ValueError: return
    if not _get_file_id(context.bot_data, token):
        await query.edit_message_text("❌ Request expired."); return
    await query.edit_message_text(
        "🎨 *Choose a category:*", parse_mode="Markdown",
        reply_markup=build_effect_categories_keyboard(token),
    )


async def handle_effect_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query; await query.answer()
    try: _, effect_key, token = query.data.split("|", 2)
    except ValueError: return
    file_id = _get_file_id(context.bot_data, token)
    if not file_id:
        await query.edit_message_text("❌ Request expired."); return
    if not await _check_heavy_rate_limit(update, "effects"):
        return

    label      = EFFECT_NAMES.get(effect_key, effect_key)
    status_msg = await query.edit_message_text(
        f"⏳ Applying *{label}*…", parse_mode="Markdown"
    )
    t_start = time.monotonic()
    try:
        from telegram.constants import ChatAction
        await context.bot.send_chat_action(
            chat_id=query.message.chat_id, action=ChatAction.UPLOAD_PHOTO
        )
        image_bytes = await _download_file(context, file_id)
        w, h        = _get_image_dimensions(image_bytes)
        params      = _get_side_data(context.bot_data, "fx_params", token) or dict(DEFAULT_FX_PARAMS)

        from modules.handlers.messages import apply_effect_to_image
        async with storage.HEAVY_JOB_SEMAPHORE:
            png_bytes = await apply_effect_to_image(image_bytes, effect_key, w, h, params)

        elapsed = time.monotonic() - t_start
        doc     = BytesIO(png_bytes); doc.name = f"{effect_key}.png"
        await query.message.reply_document(
            document=doc,
            filename=f"{effect_key}.png",
            caption=f"✅ *{label}* applied in {elapsed:.1f}s",
            parse_mode="Markdown",
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


# ── Aspect / resize callbacks ─────────────────────────────────────────────────

async def handle_aspect_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query; await query.answer()
    try: _, ratio_key, token = query.data.split("|", 2)
    except ValueError: return
    file_id = _get_file_id(context.bot_data, token)
    if not file_id:
        await query.edit_message_text("❌ Request expired."); return
    if ratio_key == "original":
        await query.answer("Already original — pick another ratio.", show_alert=False)
        return
    crop_mode = _get_side_data(context.bot_data, "fx_cropmode", token) or "crop"
    await query.edit_message_text(f"⏳ Applying {ratio_key} ({crop_mode})…")
    from modules.handlers.messages import run_resize_tool
    await run_resize_tool(update, context, file_id, "ratio", (ratio_key, crop_mode))


async def handle_cropmode_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query; await query.answer()
    try: _, mode, token = query.data.split("|", 2)
    except ValueError: return
    _store_side_data(context.bot_data, "fx_cropmode", token, mode)
    await query.answer(f"Mode set: {mode}", show_alert=False)


async def handle_resize_preset_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query; await query.answer()
    try: _, px, token = query.data.split("|", 2)
    except ValueError: return
    file_id = _get_file_id(context.bot_data, token)
    if not file_id:
        await query.edit_message_text("❌ Request expired."); return
    await query.edit_message_text(f"⏳ Resizing to {px}px wide…")
    from modules.handlers.messages import run_resize_tool
    await run_resize_tool(update, context, file_id, "width", int(px))


async def handle_resize_custom_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query; await query.answer()
    try: _, token = query.data.split("|", 1)
    except ValueError: return
    if not _get_file_id(context.bot_data, token):
        await query.edit_message_text("❌ Request expired."); return
    context.user_data["awaiting"]        = "resize_custom_wh"
    context.user_data["resize_wh_token"] = token
    await query.edit_message_text(
        "✏️ Type the target size as `WIDTHxHEIGHT`, e.g. `1080x1350`.",
        parse_mode="Markdown",
    )


# ── Param callbacks ───────────────────────────────────────────────────────────

async def handle_fx_param_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    try: _, key, delta_str, token = query.data.split("|", 3)
    except ValueError: await query.answer(); return
    params = _get_side_data(context.bot_data, "fx_params", token) or dict(DEFAULT_FX_PARAMS)
    delta  = float(delta_str)
    bounds = {
        "contrast": (0.2, 3.0), "brightness": (0.2, 3.0),
        "grainIntensity": (0, 100), "vignette": (0, 1.0), "dotPitch": (2, 40),
    }
    lo, hi  = bounds.get(key, (0, 999))
    new_val = max(lo, min(hi, params.get(key, 0) + delta))
    params[key] = (
        round(new_val) if key in ("grainIntensity", "dotPitch")
        else round(new_val, 2)
    )
    _store_side_data(context.bot_data, "fx_params", token, params)
    await query.answer()
    await safe_edit(
        query.message, "🎛 *Customize parameters:*", parse_mode="Markdown",
        reply_markup=build_param_keyboard(token, params),
    )


async def handle_fx_param_reset_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query; await query.answer("Reset to defaults.")
    try: _, token = query.data.split("|", 1)
    except ValueError: return
    _store_side_data(context.bot_data, "fx_params", token, dict(DEFAULT_FX_PARAMS))
    await safe_edit(
        query.message, "🎛 *Customize parameters:*", parse_mode="Markdown",
        reply_markup=build_param_keyboard(token, dict(DEFAULT_FX_PARAMS)),
    )


async def handle_fx_noop_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await update.callback_query.answer()


# ── Misc callbacks ────────────────────────────────────────────────────────────

async def handle_translate_lang_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query; await query.answer()
    try: _, lang_code = query.data.split("|", 1)
    except ValueError: return
    lang_label = next(
        (l for c, l in TRANSLATE_LANGUAGES if c == lang_code), lang_code
    )
    context.user_data["awaiting"]    = "translate_text"
    context.user_data["target_lang"] = lang_code
    await query.edit_message_text(
        f"✏️ Send the text to translate to *{lang_label}*.",
        parse_mode="Markdown",
    )


async def handle_watermark_position_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query; await query.answer()
    try: _, position = query.data.split("|", 1)
    except ValueError: return
    context.user_data["watermark_position"] = position
    context.user_data["awaiting"]           = "watermark_apply"
    await query.edit_message_text(
        f"✅ Position: *{position}*. Now send the photo to stamp.",
        parse_mode="Markdown",
    )


async def handle_genpass_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query; await query.answer()
    try:
        _, length_str = query.data.split("|", 1)
        from modules import password_tools
        pw = password_tools.generate_password(length=int(length_str), use_symbols=True)
        await query.edit_message_text(f"🔑 `{pw}`", parse_mode="Markdown")
        await _record(context, update, "genpass")
    except Exception:
        await query.edit_message_text("❌ Failed.")


async def handle_genpin_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query; await query.answer()
    try:
        _, length_str = query.data.split("|", 1)
        from modules import password_tools
        pin = password_tools.generate_pin(int(length_str))
        await query.edit_message_text(f"🔢 `{pin}`", parse_mode="Markdown")
        await _record(context, update, "genpin")
    except Exception:
        await query.edit_message_text("❌ Failed.")


async def handle_media_download_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query; await query.answer()
    try: _, fmt, token = query.data.split("|", 2)
    except ValueError: return
    url = _get_file_id(context.bot_data, token)
    if not url:
        await query.edit_message_text("❌ Request expired."); return
    await safe_edit(query.message, "⏳ Starting download…")
    _drop_token(context.bot_data, token)
    from modules.handlers.messages import run_media_download
    await run_media_download(update, context, url, fmt)


async def handle_color_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query; await query.answer()
    try: _, tool = query.data.split("|", 1)
    except ValueError: return
    prompts = {
        "convert":   "🔄 Send a colour:\n`#FF5733`  or  `255 87 51`  or  `rgb(255,87,51)`",
        "contrast":  "✅ Send two colours, one per line:\n`#FFFFFF`\n`#333333`",
        "gradient":  "🌈 Send a hex colour:\n`#FF5733`",
        "shadow":    "💧 Send a hex colour:\n`#FF5733`",
        "tintshade": "🖌 Send a hex colour:\n`#FF5733`",
    }
    context.user_data["awaiting"]   = "color_input"
    context.user_data["color_tool"] = tool
    from modules.ui.keyboards import build_color_input_keyboard
    await query.edit_message_text(
        prompts.get(tool, "Send colour input:"),
        parse_mode="Markdown",
        reply_markup=build_color_input_keyboard(tool),
    )


async def handle_dev_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query; await query.answer()
    try: _, tool = query.data.split("|", 1)
    except ValueError: return

    if tool == "uuid":
        await query.edit_message_text(
            f"🆔 {generate_uuid()}", parse_mode="Markdown",
            reply_markup=build_dev_input_keyboard(tool),
        ); return
    if tool == "lorem":
        await query.edit_message_text(
            f"📝 {lorem_ipsum(50)}",
            reply_markup=build_dev_input_keyboard(tool),
        ); return
    if tool == "case":
        context.user_data["awaiting"] = "dev_input"
        context.user_data["dev_tool"] = "case_text"
        await query.edit_message_text(
            "🔤 Send the text to convert:",
            reply_markup=build_dev_input_keyboard(tool),
        ); return

    prompts = {
        "json":      "📋 Send the JSON to format/validate:",
        "hash":      "🔑 Send the text to hash:",
        "timestamp": "⏰ Send a Unix timestamp or ISO date:",
        "pxrem":     "📐 Send a pixel value (e.g. `16` or `24px`):",
        "urlencode": "🔗 Send the text to URL-encode:",
        "urldecode": "🔗 Send the encoded URL to decode:",
        "b64enc":    "💻 Send the text to Base64-encode:",
        "b64dec":    "💻 Send the Base64 string to decode:",
        "wordcount": "📊 Send the text to count:",
        "diff":      "🔀 Send *Text A* first:",
        "regex":     "🔍 Send `<pattern>\\n<text>`:",
    }
    context.user_data["awaiting"] = "dev_input"
    context.user_data["dev_tool"] = tool
    await query.edit_message_text(
        prompts.get(tool, "Send input:"),
        parse_mode="Markdown",
        reply_markup=build_dev_input_keyboard(tool),
    )


async def handle_case_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query; await query.answer()
    try: _, mode = query.data.split("|", 1)
    except ValueError: return
    text = context.user_data.pop("case_input", "")
    if not text:
        await query.edit_message_text("⚠️ No text stored. Please restart."); return
    result = convert_case(text, mode)
    await query.edit_message_text(
        f"🔤 *{mode}:*\n\n`{result}`",
        parse_mode="Markdown",
        reply_markup=build_dev_input_keyboard("case"),
    )


async def handle_limit_request_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
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
            await query.message.edit_caption(
                caption=f"✅ Approved: `{target_id}` → {requested} calls/60s.",
                parse_mode="Markdown",
            )
        except Exception:
            await safe_edit(
                query.message,
                f"✅ Approved: `{target_id}` → {requested} calls/60s.",
                parse_mode="Markdown",
            )
        try:
            await context.bot.send_message(
                target_id,
                f"✅ Your limit request was approved — now {requested} calls/60s."
            )
        except Exception:
            pass
    else:
        await storage.remove_limit_request(target_id)
        try:
            await query.message.edit_caption(
                caption=f"❌ Denied: `{target_id}`.", parse_mode="Markdown"
            )
        except Exception:
            await safe_edit(
                query.message, f"❌ Denied: `{target_id}`.", parse_mode="Markdown"
            )
        try:
            await context.bot.send_message(
                target_id, "❌ Your limit increase request was denied."
            )
        except Exception:
            pass


# ── Rate limit helper ─────────────────────────────────────────────────────────

async def _check_heavy_rate_limit(update: Update, tool: str) -> bool:
    if tool not in storage.HEAVY_TOOLS:
        return True
    chat_id = update.effective_chat.id
    allowed, wait = await storage.check_rate_limit(
        chat_id, exempt=is_admin(update)
    )
    if not allowed:
        used, limit = await storage.calls_used(chat_id)
        await update.effective_message.reply_text(
            f"⏳ Please wait ~{int(wait)}s "
            f"({used}/{limit} used in the last {storage.RATE_LIMIT_WINDOW_SECONDS}s).\n"
            f"Need more? `/requestlimit <number>`",
            parse_mode="Markdown",
        )
        return False
    return True
