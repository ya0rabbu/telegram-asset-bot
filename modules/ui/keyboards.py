"""All InlineKeyboardMarkup builders."""

from __future__ import annotations
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from modules.ui.strings import (
    EFFECTS, EFFECT_CATEGORIES, EFFECT_CATEGORY_ORDER,
    ASPECT_RATIO_PRESETS, TRANSLATE_LANGUAGES, DEFAULT_FX_PARAMS,
)
from modules.image_extra import LUMMI_SIZE_LABELS, HUGEICONS_SIZES


# ── Main Menu ─────────────────────────────────────────────────────────────────

def build_main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎨 Image Effects",   callback_data="menu|effects"),
            InlineKeyboardButton("🧹 Remove BG",       callback_data="menu|bgremove"),
        ],
        [
            InlineKeyboardButton("🚫 Remove Watermark",callback_data="menu|watermark_remove"),
            InlineKeyboardButton("📐 Resize / Ratio",  callback_data="menu|resizetool"),
        ],
        [
            InlineKeyboardButton("📋 Copy Image",      callback_data="menu|copyimage"),
            InlineKeyboardButton("😄 Sticker → PNG",   callback_data="menu|sticker2png"),
        ],
        [
            InlineKeyboardButton("🎞 GIF → Frames",    callback_data="menu|gif2frames"),
            InlineKeyboardButton("🛡 Fiver Sanitizer", callback_data="menu|fiversanitize"),
        ],
        [
            InlineKeyboardButton("🌐 Translate",       callback_data="menu|translate"),
            InlineKeyboardButton("💧 Watermark",       callback_data="menu|watermark"),
        ],
        [
            InlineKeyboardButton("📉 Compress",        callback_data="menu|compress"),
            InlineKeyboardButton("🎬 Video/Audio DL",  callback_data="menu|mediadownload"),
        ],
        [
            InlineKeyboardButton("🔳 QR Generate",     callback_data="menu|qrgen"),
            InlineKeyboardButton("🔍 QR Scan",         callback_data="menu|qrscan"),
        ],
        [
            InlineKeyboardButton("🔑 Password Gen",    callback_data="menu|genpass"),
            InlineKeyboardButton("📚 My Words",        callback_data="menu|mywords"),
        ],
        [
            InlineKeyboardButton("🖼 Image Tools",     callback_data="cat|image"),
            InlineKeyboardButton("📄 PDF Tools",       callback_data="cat|pdf"),
        ],
        [
            InlineKeyboardButton("📝 Documents",       callback_data="cat|word"),
            InlineKeyboardButton("📊 Spreadsheet",     callback_data="cat|sheet"),
        ],
        [
            InlineKeyboardButton("📽 Presentation",    callback_data="cat|ppt"),
            InlineKeyboardButton("✍️ Text & Markup",   callback_data="cat|text"),
        ],
        [
            InlineKeyboardButton("🎨 Colour Tools",    callback_data="cat|color"),
            InlineKeyboardButton("🔧 Dev Tools",       callback_data="cat|dev"),
        ],
        [
            InlineKeyboardButton("📚 More →",          callback_data="cat|more"),
        ],
    ])


# ── Category keyboards ────────────────────────────────────────────────────────

def build_category_keyboard(cat: str) -> InlineKeyboardMarkup:
    back = [InlineKeyboardButton("◀ Main Menu", callback_data="cat|back")]
    menus: dict[str, list] = {
        "image": [
            [InlineKeyboardButton("🖼→📄 Image → PDF",    callback_data="menu|img2pdf"),
             InlineKeyboardButton("📄→🖼 PDF → Images",   callback_data="menu|pdf2img")],
            [InlineKeyboardButton("🔁 JPEG → PNG",        callback_data="menu|jpg2png"),
             InlineKeyboardButton("🔁 PNG → JPEG",        callback_data="menu|png2jpg")],
            [InlineKeyboardButton("📐 Resize / Ratio",    callback_data="menu|resizetool"),
             InlineKeyboardButton("📋 Copy",              callback_data="menu|copyimage")],
            [InlineKeyboardButton("💧 Watermark",         callback_data="menu|watermark"),
             InlineKeyboardButton("📉 Compress",          callback_data="menu|compress")],
            [InlineKeyboardButton("🚫 Remove Watermark",  callback_data="menu|watermark_remove")],
            back,
        ],
        "pdf": [
            [InlineKeyboardButton("📄→📝 PDF → DOCX",    callback_data="menu|pdf2docx"),
             InlineKeyboardButton("📄→📃 PDF → TXT",     callback_data="menu|pdf2txt")],
            [InlineKeyboardButton("📄→🖼 PDF → Images",  callback_data="menu|pdf2img"),
             InlineKeyboardButton("🖼→📄 Image → PDF",   callback_data="menu|img2pdf")],
            back,
        ],
        "word": [
            [InlineKeyboardButton("📝→📄 DOCX → PDF",    callback_data="menu|docx2pdf"),
             InlineKeyboardButton("📝→📃 DOCX → TXT",    callback_data="menu|docx2txt")],
            [InlineKeyboardButton("📝→🌐 DOCX → HTML",   callback_data="menu|docx2html"),
             InlineKeyboardButton("📄→📝 DOC → DOCX",    callback_data="menu|doc2docx")],
            [InlineKeyboardButton("📄→📝 ODT → DOCX",    callback_data="menu|odt2docx"),
             InlineKeyboardButton("📄→📄 ODT → PDF",     callback_data="menu|odt2pdf")],
            back,
        ],
        "sheet": [
            [InlineKeyboardButton("📊→📄 XLSX → PDF",    callback_data="menu|xlsx2pdf"),
             InlineKeyboardButton("📊→📃 XLSX → CSV",    callback_data="menu|xlsx2csv")],
            [InlineKeyboardButton("📃→📊 CSV → XLSX",    callback_data="menu|csv2xlsx"),
             InlineKeyboardButton("📊→📊 XLS → XLSX",    callback_data="menu|xls2xlsx")],
            back,
        ],
        "ppt": [
            [InlineKeyboardButton("📽→📄 PPTX → PDF",    callback_data="menu|pptx2pdf"),
             InlineKeyboardButton("📽→🖼 PPTX → Images", callback_data="menu|pptx2images")],
            [InlineKeyboardButton("📽→📽 PPT → PPTX",    callback_data="menu|ppt2pptx")],
            back,
        ],
        "text": [
            [InlineKeyboardButton("📃→📄 TXT → PDF",     callback_data="menu|txt2pdf"),
             InlineKeyboardButton("🌐→📄 HTML → PDF",    callback_data="menu|html2pdf")],
            [InlineKeyboardButton("📝→📄 MD → PDF",      callback_data="menu|md2pdf"),
             InlineKeyboardButton("📝→📃 MD → TXT",      callback_data="menu|md2txt")],
            [InlineKeyboardButton("📝→📝 MD → DOCX",     callback_data="menu|md2docx"),
             InlineKeyboardButton("📄→📃 RTF → TXT",     callback_data="menu|rtf2txt")],
            [InlineKeyboardButton("📄→📄 RTF → PDF",     callback_data="menu|rtf2pdf")],
            back,
        ],
        "color": [
            [InlineKeyboardButton("🔄 Color Convert",    callback_data="color|convert"),
             InlineKeyboardButton("✅ Contrast Check",   callback_data="color|contrast")],
            [InlineKeyboardButton("🌈 Gradient CSS",     callback_data="color|gradient"),
             InlineKeyboardButton("💧 Shadow CSS",       callback_data="color|shadow")],
            [InlineKeyboardButton("🖌 Tint & Shade",     callback_data="color|tintshade")],
            back,
        ],
        "dev": [
            [InlineKeyboardButton("📋 JSON Format",      callback_data="dev|json"),
             InlineKeyboardButton("🔑 Hash Gen",         callback_data="dev|hash")],
            [InlineKeyboardButton("🆔 UUID Gen",         callback_data="dev|uuid"),
             InlineKeyboardButton("⏰ Timestamp",        callback_data="dev|timestamp")],
            [InlineKeyboardButton("📝 Lorem Ipsum",      callback_data="dev|lorem"),
             InlineKeyboardButton("🔤 Case Convert",     callback_data="dev|case")],
            [InlineKeyboardButton("📐 px → rem",         callback_data="dev|pxrem"),
             InlineKeyboardButton("🔗 URL Encode",       callback_data="dev|urlencode")],
            [InlineKeyboardButton("🔗 URL Decode",       callback_data="dev|urldecode"),
             InlineKeyboardButton("💻 Base64 Encode",    callback_data="dev|b64enc")],
            [InlineKeyboardButton("💻 Base64 Decode",    callback_data="dev|b64dec"),
             InlineKeyboardButton("📊 Word Count",       callback_data="dev|wordcount")],
            [InlineKeyboardButton("🔀 Diff Checker",     callback_data="dev|diff"),
             InlineKeyboardButton("🔍 Regex Test",       callback_data="dev|regex")],
            back,
        ],
        "more": [
            [InlineKeyboardButton("📚→📄 EPUB → PDF",    callback_data="menu|epub2pdf")],
            back,
        ],
    }
    return InlineKeyboardMarkup(menus.get(cat, [back]))


# ── Hugeicons size keyboard ───────────────────────────────────────────────────

def build_hugeicons_size_keyboard(token: str) -> InlineKeyboardMarkup:
    rows, row = [], []
    for size in HUGEICONS_SIZES:
        row.append(InlineKeyboardButton(str(size), callback_data=f"hisz|{size}|{token}"))
        if len(row) == 4:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("🔺 All sizes (SVG file)", callback_data=f"hisz|svg|{token}")])
    return InlineKeyboardMarkup(rows)


# ── Lummi size keyboard ───────────────────────────────────────────────────────

def build_lummi_size_keyboard(token: str) -> InlineKeyboardMarkup:
    rows = []
    size_map = {
        "small":  "🔹 Small   (516 × 640)",
        "medium": "🔸 Medium  (967 × 1200)",
        "large":  "🔶 Large   (1933 × 2400)",
        "xlarge": "🔴 XLarge  (3712 × 4608)",
    }
    for key, label in size_map.items():
        rows.append([InlineKeyboardButton(label, callback_data=f"lmsz|{key}|{token}")])
    rows.append([InlineKeyboardButton("📦 Original file", callback_data=f"lmsz|orig|{token}")])
    return InlineKeyboardMarkup(rows)


# ── Image toolbox ─────────────────────────────────────────────────────────────

def build_effect_toolbox_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎨 Pick an Effect",       callback_data=f"fxtool|effect|{token}")],
        [InlineKeyboardButton("📐 Aspect Ratio",          callback_data=f"fxtool|aspect|{token}"),
         InlineKeyboardButton("📏 Resize (custom)",       callback_data=f"fxtool|resize|{token}")],
        [InlineKeyboardButton("🎛 Customize Parameters",  callback_data=f"fxtool|params|{token}")],
        [InlineKeyboardButton("📋 Copy (as-is)",          callback_data=f"fxtool|copy|{token}"),
         InlineKeyboardButton("🔁 Convert Format",        callback_data=f"fxtool|convert|{token}")],
        [InlineKeyboardButton("📉 Compress",              callback_data=f"fxtool|compress|{token}"),
         InlineKeyboardButton("🚫 Remove Watermark",      callback_data=f"fxtool|wmremove|{token}")],
        [InlineKeyboardButton("✅ Apply & Continue",       callback_data=f"fxtool|go|{token}")],
    ])


# ── Effect keyboards ──────────────────────────────────────────────────────────

def build_effect_categories_keyboard(token: str) -> InlineKeyboardMarkup:
    rows, row = [], []
    for cat_key in EFFECT_CATEGORY_ORDER:
        row.append(InlineKeyboardButton(
            EFFECT_CATEGORIES[cat_key], callback_data=f"fxcat|{cat_key}|{token}"
        ))
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
    presets = [
