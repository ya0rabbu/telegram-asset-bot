"""All bot command handlers."""

from __future__ import annotations
import logging
import os
from io import BytesIO

from telegram import Update
from telegram.ext import ContextTypes

from modules import storage
from modules.ui.strings import (
    WELCOME_MESSAGE, MENU_INTRO, FIVER_INFO_PATTERN_HINT,
    SUPER_ADMINS, ADMINS, DEFAULT_COMPRESS_TARGET,
)
from modules.ui.keyboards import (
    build_main_menu_keyboard, build_genpass_keyboard,
    build_limit_request_keyboard,
)
from modules import password_tools, qr_tools, image_extra

logger = logging.getLogger("bangaliicon.commands")

MAX_LIMIT_REQUEST_VALUE = 100
DEFAULT_COMPRESS_TARGET = 1 * 1024 * 1024


# ── Helpers ───────────────────────────────────────────────────────────────────

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


def _esc_md(text: str) -> str:
    import re
    return re.sub(r"([_*`\[])", r"\\\1", text) if text else text


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


_admin_chat_ids: dict[str, int] = {}


def remember_admin_chat_id(update: Update) -> None:
    uname = _username_of(update)
    if uname and (uname in SUPER_ADMINS or uname in ADMINS) and update.effective_chat:
        _admin_chat_ids[uname] = update.effective_chat.id


def get_admin_chat_ids() -> dict[str, int]:
    return _admin_chat_ids


async def _download_file(context: ContextTypes.DEFAULT_TYPE, file_id: str) -> bytes:
    tg_file = await context.bot.get_file(file_id)
    buf = BytesIO()
    await tg_file.download_to_memory(buf)
    return buf.getvalue()


async def _get_profile_photo_bytes(
    context: ContextTypes.DEFAULT_TYPE, user_id: int
) -> bytes | None:
    try:
        photos = await context.bot.get_user_profile_photos(user_id, limit=1)
        if not photos.photos:
            return None
        return await _download_file(context, photos.photos[0][-1].file_id)
    except Exception:
        return None


async def _resolve_target_chat_id_async(arg: str) -> int | None:
    arg = arg.strip().lstrip("@")
    if arg.lstrip("-").isdigit():
        return int(arg)
    direct = _admin_chat_ids.get(arg.lower())
    if direct is not None:
        return direct
    return await storage.find_chat_id_by_username(arg)


async def _record(
    context: ContextTypes.DEFAULT_TYPE, update: Update, tool: str
) -> None:
    chat_id  = update.effective_chat.id if update.effective_chat else None
    username = _username_of(update)
    await storage.record_usage(tool, chat_id=chat_id, username=username)


def _bar(count: int, max_count: int, width: int = 12) -> str:
    filled = (
        max(1, round((count / max_count) * width))
        if max_count > 0 and count > 0 else 0
    )
    return "█" * filled + "░" * (width - filled)


# ── Commands ──────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return
    remember_admin_chat_id(update)
    context.user_data.pop("awaiting", None)
    await update.message.reply_text(
        WELCOME_MESSAGE, parse_mode="Markdown",
        reply_markup=build_main_menu_keyboard(),
    )


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return
    await update.message.reply_text(
        MENU_INTRO, parse_mode="Markdown",
        reply_markup=build_main_menu_keyboard(),
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return
    had = context.user_data.pop("awaiting", None)
    for key in (
        "target_lang", "compress_target_bytes", "watermark_position",
        "color_tool", "dev_tool", "dev_tool_step", "fiver_info",
        "diff_text_a", "case_input", "fx_token", "fx_params",
        "resize_wh_token", "media_url", "media_token",
    ):
        context.user_data.pop(key, None)
    await update.message.reply_text("✅ Cancelled." if had else "Nothing to cancel.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return
    await update.message.reply_text(
        "📖 *BangaliIcon Bot — Help*\n\n"
        "*Asset extraction:*\n"
        "Send a Lummi.ai or Hugeicons link → choose a size → instant download\n\n"
        "*Image toolbox:*\n"
        "Send any photo → Effects / Resize / Aspect Ratio / Watermark Remove / "
        "Copy / Convert / Compress\n\n"
        "*Video/Audio download:*\n"
        "Send a YouTube, X or Facebook link → Video or Audio-only (MP3)\n\n"
        "*Fiver Sanitizer:*\n"
        "`/FiverMessage` → send order info, then the message body\n"
        f"Order info format: `{FIVER_INFO_PATTERN_HINT}`\n\n"
        "*Colour Tools:* /menu → Colour Tools\n"
        "*Dev Tools:* /menu → Dev Tools\n\n"
        "*Other:* `/qr` · `/genpass` · `/genpin` · `/compress` · `/watermark`",
        parse_mode="Markdown",
    )


async def admins_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return
    remember_admin_chat_id(update)

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    async def _send_admin_card(info: dict, role: str, user_id: int | None) -> None:
        photo_bytes = await _get_profile_photo_bytes(context, user_id) if user_id else None
        name        = _esc_md(info["name"])
        email       = info["email"]
        tg_url      = info["telegram"]
        username    = tg_url.split("/")[-1]

        caption = (
            f"{'👑' if role == 'Super Admin' else '🛡'} *{role}*\n"
            f"👤 {name}\n"
            f"📧 `{email}`"
        )
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("💬 Message", url=tg_url),
            InlineKeyboardButton("📧 Email",   url=f"mailto:{email}"),
        ]])

        if photo_bytes:
            await update.message.reply_photo(
                photo=BytesIO(photo_bytes),
                caption=caption,
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
        else:
            await update.message.reply_text(
                caption, parse_mode="Markdown", reply_markup=keyboard,
            )

    await update.message.reply_text(
        f"👥 *BangaliIcon Bot — Team*\n_Your role: {admin_role_label(update)}_",
        parse_mode="Markdown",
    )

    for uname, info in SUPER_ADMINS.items():
        uid = _admin_chat_ids.get(uname)
        await _send_admin_card(info, "Super Admin", uid)

    for uname, info in ADMINS.items():
        uid = _admin_chat_ids.get(uname)
        await _send_admin_card(info, "Admin", uid)


async def fivermessage_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not update.message: return
    context.user_data.pop("fiver_info", None)
    context.user_data["awaiting"] = "fiver_info"
    await update.message.reply_text(
        "🛡 প্রথমে অর্ডার তথ্য দিন এই ফরম্যাটে:\n\n"
        f"`{FIVER_INFO_PATTERN_HINT}`\n\n"
        "উদাহরণ: `jmbattaglia_FO41C0CAC4D84_CustomerPortal_brainflux_1000`\n\n"
        "অথবা /cancel করে বাদ দিন।",
        parse_mode="Markdown",
    )


async def addword_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not update.message: return
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: `/addword <word> <replacement>`", parse_mode="Markdown"
        ); return
    ok, info = await storage.add_word(
        update.effective_chat.id, context.args[0], " ".join(context.args[1:])
    )
    if not ok:
        await update.message.reply_text(f"❌ {info}"); return
    verb = "Updated" if info == "updated" else "Added"
    await update.message.reply_text(
        f"✅ {verb}: *{context.args[0]}* → *{' '.join(context.args[1:])}*",
        parse_mode="Markdown",
    )


async def mywords_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not update.message: return
    words = await storage.get_words(update.effective_chat.id)
    if not words:
        await update.message.reply_text(
            "No custom words yet. Add with `/addword <word> <replacement>`.",
            parse_mode="Markdown",
        ); return
    items = sorted(words.items())
    lines = []
    for pi, page in enumerate(
        [items[i:i+25] for i in range(0, len(items), 25)], 1
    ):
        lines.append(f"*Page {pi}*")
        lines.extend(f"• `{w}` → `{r}`" for w, r in page)
    text = "\n".join(lines)
    if len(text) > 3800:
        text = text[:3800] + "\n… (truncated)"
    await update.message.reply_text(
        f"📚 *Custom words* ({len(words)}):\n\n{text}",
        parse_mode="Markdown",
    )


async def delword_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not update.message: return
    if not context.args:
        await update.message.reply_text(
            "Usage: `/delword <word>`", parse_mode="Markdown"
        ); return
    removed = await storage.del_word(update.effective_chat.id, context.args[0])
    await update.message.reply_text(
        f"🗑 Removed *{context.args[0]}*." if removed
        else f"⚠️ No custom mapping for *{context.args[0]}*.",
        parse_mode="Markdown",
    )


async def resetwords_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not update.message: return
    await storage.reset_words(update.effective_chat.id)
    await update.message.reply_text("♻️ Custom words cleared.")


async def stats_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not update.message: return
    remember_admin_chat_id(update)
    if not is_admin(update):
        await update.message.reply_text("🚫 Admin only."); return
    rows     = await storage.get_stats()
    activity = await storage.get_user_activity_all()
    if not rows:
        await update.message.reply_text("No usage recorded yet."); return
    top  = rows[:12]
    mx   = top[0][1] if top else 0
    tool_lines = [f"`{t:<18}` {_bar(c, mx)} {c}" for t, c in top]
    leaders = sorted(
        activity.items(),
        key=lambda kv: sum(kv[1].get("counts", {}).values()),
        reverse=True,
    )[:10]
    user_lines = [
        f"• {_esc_md('@'+e.get('username','')) if e.get('username') else 'id:'+cid}"
        f" — `{cid}` — {sum(e.get('counts',{}).values())}"
        for cid, e in leaders
    ]
    await update.message.reply_text(
        f"📊 *Usage Dashboard* ({admin_role_label(update)})\n\n"
        f"*By tool:*\n" + "\n".join(tool_lines) +
        "\n\n*Top users:*\n" + ("\n".join(user_lines) or "_No per-user data yet._"),
        parse_mode="Markdown",
    )


async def qr_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not update.message: return
    text = " ".join(context.args) if context.args else ""
    if not text.strip():
        await update.message.reply_text(
            "Usage: `/qr <text or link>`", parse_mode="Markdown"
        ); return
    status = await update.message.reply_text("⏳ Generating QR code…")
    try:
        png = await qr_tools.generate_qr(text.strip())
        doc = BytesIO(png); doc.name = "qrcode.png"
        await update.message.reply_photo(photo=doc, caption="✅ QR code ready.")
        await status.delete()
        await _record(context, update, "qr_generate")
    except Exception as exc:
        await status.edit_text(f"❌ {exc}")


async def genpass_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not update.message: return
    if not context.args:
        await update.message.reply_text(
            "🔑 Choose a length:", reply_markup=build_genpass_keyboard()
        ); return
    length  = password_tools.DEFAULT_LENGTH
    use_sym = False
    no_ambig = False
    for a in context.args:
        if a.lstrip("-").isdigit():         length   = int(a)
        elif a in ("--symbols", "-s"):      use_sym  = True
        elif a in ("--no-ambiguous", "-na"): no_ambig = True
    pw = password_tools.generate_password(
        length=length, use_symbols=use_sym, no_ambiguous=no_ambig
    )
    await update.message.reply_text(f"🔑 `{pw}`", parse_mode="Markdown")
    await _record(context, update, "genpass")


async def genpin_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not update.message: return
    length = int(context.args[0]) if context.args and context.args[0].isdigit() else 4
    await update.message.reply_text(
        f"🔢 `{password_tools.generate_pin(length)}`", parse_mode="Markdown"
    )
    await _record(context, update, "genpin")


async def compress_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not update.message: return
    target = DEFAULT_COMPRESS_TARGET
    if context.args:
        parsed = image_extra.parse_size_to_bytes(context.args[0])
        if parsed: target = parsed
    context.user_data["awaiting"]              = "compress_image"
    context.user_data["compress_target_bytes"] = target
    await update.message.reply_text(
        f"📉 Send the photo to compress (target: ~{target//1024} KB)."
    )


async def watermark_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not update.message: return
    context.user_data["awaiting"] = "watermark_setup"
    await update.message.reply_text(
        "💧 Send your *logo/signature image* first (PNG with transparency works best).",
        parse_mode="Markdown",
    )


async def setlimit_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not update.message: return
    remember_admin_chat_id(update)
    if not is_admin(update):
        await update.message.reply_text("🚫 Admin only."); return
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: `/setlimit <chat_id|@user> <n>`", parse_mode="Markdown"
        ); return
    target = await _resolve_target_chat_id_async(context.args[0])
    if target is None:
        await update.message.reply_text("⚠️ User not found."); return
    try:
        n = int(context.args[1])
    except ValueError:
        await update.message.reply_text("⚠️ Limit must be a number."); return
    await storage.set_custom_limit(target, n)
    await update.message.reply_text(
        f"✅ `{target}` limited to *{n}* calls/60s.", parse_mode="Markdown"
    )
    try:
        await context.bot.send_message(
            target, f"ℹ️ Your limit was updated to {n} calls/60s by an admin."
        )
    except Exception:
        pass


async def blockuser_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not update.message: return
    remember_admin_chat_id(update)
    if not is_super_admin(update):
        await update.message.reply_text("🚫 Super-admin only."); return
    if not context.args:
        await update.message.reply_text(
            "Usage: `/blockuser <chat_id|@user>`", parse_mode="Markdown"
        ); return
    target = await _resolve_target_chat_id_async(context.args[0])
    if target is None:
        await update.message.reply_text("⚠️ User not found."); return
    await storage.block_user(target)
    await update.message.reply_text(
        f"🚫 `{target}` fully blocked.", parse_mode="Markdown"
    )


async def unblockuser_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not update.message: return
    remember_admin_chat_id(update)
    if not is_super_admin(update):
        await update.message.reply_text("🚫 Super-admin only."); return
    if not context.args:
        await update.message.reply_text(
            "Usage: `/unblockuser <chat_id|@user>`", parse_mode="Markdown"
        ); return
    target = await _resolve_target_chat_id_async(context.args[0])
    if target is None:
        await update.message.reply_text("⚠️ User not found."); return
    await storage.unblock_user(target)
    await update.message.reply_text(
        f"✅ `{target}` unblocked.", parse_mode="Markdown"
    )


async def mylimit_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not update.message: return
    if is_admin(update):
        await update.message.reply_text(
            f"👑 You are *{admin_role_label(update)}* — rate limits do not apply.",
            parse_mode="Markdown",
        ); return
    used, limit = await storage.calls_used(update.effective_chat.id)
    await update.message.reply_text(
        f"📊 *{used}/{limit}* heavy-tool calls used in the last "
        f"{storage.RATE_LIMIT_WINDOW_SECONDS}s.\n"
        f"Need more? `/requestlimit <number>`",
        parse_mode="Markdown",
    )


async def requestlimit_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not update.message: return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(
            "Usage: `/requestlimit <number>`", parse_mode="Markdown"
        ); return
    requested = min(int(context.args[0]), MAX_LIMIT_REQUEST_VALUE)
    chat_id   = update.effective_chat.id
    username  = _username_of(update)
    display   = _display_name(update, escape_markdown=True)
    await storage.add_limit_request(chat_id, username, requested)
    used, limit = await storage.calls_used(chat_id)
    await update.message.reply_text(
        f"📨 Request for *{requested}* calls/60s sent to admins.",
        parse_mode="Markdown",
    )
    caption = (
        f"🙋 *Limit increase request*\n"
        f"User: {display}\nchat\\_id: `{chat_id}`\n"
        f"Current: {limit} (used {used})\nRequested: *{requested}*"
    )
    photo_bytes = await _get_profile_photo_bytes(context, chat_id)
    keyboard    = build_limit_request_keyboard(chat_id, requested)
    from modules.handlers.commands import get_admin_chat_ids
    targets = set(get_admin_chat_ids().values())
    admin_chat_id = int(os.getenv("ADMIN_CHAT_ID", "0") or 0)
    if admin_chat_id:
        targets.add(admin_chat_id)
    for aid in targets:
        try:
            if photo_bytes:
                await context.bot.send_photo(
                    aid, photo=BytesIO(photo_bytes),
                    caption=caption, parse_mode="Markdown", reply_markup=keyboard,
                )
            else:
                await context.bot.send_message(
                    aid, caption, parse_mode="Markdown", reply_markup=keyboard,
                )
        except Exception:
            logger.exception("Failed to notify admin %s", aid)
