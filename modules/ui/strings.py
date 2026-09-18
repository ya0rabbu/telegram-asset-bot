"""All bot strings, messages and templates."""

from __future__ import annotations

WELCOME_MESSAGE = (
    "```\n"
    "┌───────────────────────────────┐\n"
    "│    B A N G A L I · I C O N    │\n"
    "└───────────────────────────────┘\n"
    "```\n"
    "⚡ *@BangaliIconBot* — all modules loaded\n\n"
    "🔗 Send a *Lummi.ai* or *Hugeicons* link → pick a size → instant asset\n"
    "🖼 Send a *photo* → resize/aspect ratio, then 38 real-time visual effects\n"
    "🎬 Send a *YouTube / X / Facebook* video link → video or audio download\n"
    "😄 Send a *sticker or GIF* → extract or convert\n"
    "🛡 `/FiverMessage` → sanitize with AI-sign detection\n"
    "🎨 `/colortools` → colour conversion & CSS helpers\n"
    "🔧 `/devtools` → JSON, UUID, hash & more\n\n"
    "_Tap_ `/menu` _anytime · /cancel to stop a task_\n\n"
    "✦ Designed & developed by *@YA\\_Rabbu*"
)

MENU_INTRO = "🧰 *Select a tool*"

FIVER_INFO_PATTERN_HINT = "ClientName_OrderID_ProjectName_ProfileName_Amount"

FIVER_TEMPLATE = (
    "==================================\n"
    "Status: Update (Updated)\n"
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

DEFAULT_CLOSING_LINE = (
    "Thank you again for your support, "
    "and I'll keep you updated on the progress."
)

TRANSLATE_MAX_CHARS  = 4_500
MYMEMORY_MAX_CHARS   = 500
FIVER_SANITIZE_MAX_CHARS = 4_500

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
EFFECTS = [
    (k, l, c) for k, l, c in EFFECTS
    if k not in _seen and not _seen.add(k)  # type: ignore
]

EFFECT_NAMES = {key: label for key, label, _ in EFFECTS}

ASPECT_RATIO_PRESETS: list[tuple[str, str]] = [
    ("original", "Original"),
    ("1:1",  "1:1 Square"),
    ("4:5",  "4:5 Portrait"),
    ("9:16", "9:16 Story"),
    ("16:9", "16:9 Wide"),
    ("4:3",  "4:3"),
    ("3:2",  "3:2"),
]

ICON_SIZE_PRESETS: list[int] = [64, 128, 256, 512, 1024]

DEFAULT_FX_PARAMS: dict = {
    "dotPitch":       8,
    "contrast":       1.2,
    "brightness":     1.0,
    "grainIntensity": 18,
    "vignette":       0.3,
    "aspectRatio":    "original",
    "cropMode":       "crop",
    "outputWidth":    None,
    "outputHeight":   None,
}

# Admins roster
SUPER_ADMINS: dict[str, dict[str, str]] = {
    "ya_rabbu": {
        "name":     "Yasir Abed Rabbu",
        "email":    "yasirabedrabbu@gmail.com",
        "telegram": "https://t.me/YA_Rabbu",
    },
}

ADMINS: dict[str, dict[str, str]] = {
    "smashik_softvence": {
        "name":     "Sheikh Muhammad Ashik",
        "email":    "smashik716@gmail.com",
        "telegram": "https://t.me/smashik_softvence",
    },
}
