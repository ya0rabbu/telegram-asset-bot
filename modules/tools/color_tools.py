"""Color conversion and CSS helper tools."""

from __future__ import annotations


def hex_to_rgb(hex_str: str) -> tuple[int, int, int] | None:
    hex_str = hex_str.strip().lstrip("#")
    if len(hex_str) == 3:
        hex_str = "".join(c * 2 for c in hex_str)
    if len(hex_str) != 6:
        return None
    try:
        return int(hex_str[0:2], 16), int(hex_str[2:4], 16), int(hex_str[4:6], 16)
    except ValueError:
        return None


def rgb_to_hex(r: int, g: int, b: int) -> str:
    return f"#{r:02X}{g:02X}{b:02X}"


def rgb_to_hsl(r: int, g: int, b: int) -> tuple[float, float, float]:
    r_, g_, b_ = r / 255, g / 255, b / 255
    cmax, cmin  = max(r_, g_, b_), min(r_, g_, b_)
    delta = cmax - cmin
    l = (cmax + cmin) / 2
    s = 0.0 if delta == 0 else delta / (1 - abs(2 * l - 1))
    if delta == 0:
        h = 0.0
    elif cmax == r_:
        h = 60 * (((g_ - b_) / delta) % 6)
    elif cmax == g_:
        h = 60 * ((b_ - r_) / delta + 2)
    else:
        h = 60 * ((r_ - g_) / delta + 4)
    return round(h, 1), round(s * 100, 1), round(l * 100, 1)


def rgb_to_cmyk(r: int, g: int, b: int) -> tuple[int, int, int, int]:
    if r == g == b == 0:
        return 0, 0, 0, 100
    r_, g_, b_ = r / 255, g / 255, b / 255
    k = 1 - max(r_, g_, b_)
    c = (1 - r_ - k) / (1 - k)
    m = (1 - g_ - k) / (1 - k)
    y = (1 - b_ - k) / (1 - k)
    return round(c * 100), round(m * 100), round(y * 100), round(k * 100)


def wcag_contrast(r1, g1, b1, r2, g2, b2) -> float:
    def lum(r, g, b):
        vals = [v / 255 for v in (r, g, b)]
        vals = [
            v / 12.92 if v <= 0.03928
            else ((v + 0.055) / 1.055) ** 2.4
            for v in vals
        ]
        return 0.2126 * vals[0] + 0.7152 * vals[1] + 0.0722 * vals[2]
    l1, l2 = lum(r1, g1, b1), lum(r2, g2, b2)
    if l1 < l2:
        l1, l2 = l2, l1
    return round((l1 + 0.05) / (l2 + 0.05), 2)


def tint_shade(r: int, g: int, b: int) -> str:
    lines = []
    for pct in (90, 75, 50, 25, 10):
        tr = int(r + (255 - r) * pct / 100)
        tg = int(g + (255 - g) * pct / 100)
        tb = int(b + (255 - b) * pct / 100)
        lines.append(f"  Tint  {pct:3d}%: {rgb_to_hex(tr, tg, tb)}")
    for pct in (10, 25, 50, 75, 90):
        sr = int(r * (100 - pct) / 100)
        sg = int(g * (100 - pct) / 100)
        sb = int(b * (100 - pct) / 100)
        lines.append(f"  Shade {pct:3d}%: {rgb_to_hex(sr, sg, sb)}")
    return "\n".join(lines)


def css_gradient(r: int, g: int, b: int) -> str:
    r2 = min(255, r + 60)
    g2 = min(255, g + 60)
    b2 = min(255, b + 60)
    c1, c2 = rgb_to_hex(r, g, b), rgb_to_hex(r2, g2, b2)
    return (
        f"/* Linear */\n"
        f"background: linear-gradient(135deg, {c1}, {c2});\n\n"
        f"/* Radial */\n"
        f"background: radial-gradient(circle, {c1}, {c2});"
    )


def css_shadow(r: int, g: int, b: int) -> str:
    return (
        f"/* Soft shadow */\n"
        f"box-shadow: 0 4px 16px rgba({r}, {g}, {b}, 0.25);\n\n"
        f"/* Hard shadow */\n"
        f"box-shadow: 4px 4px 0px rgba({r}, {g}, {b}, 0.8);\n\n"
        f"/* Inner glow */\n"
        f"box-shadow: inset 0 0 12px rgba({r}, {g}, {b}, 0.4);"
    )


def parse_color(s: str) -> tuple[int, int, int] | None:
    import re
    s = s.strip()
    m = re.match(r"rgb\(?\s*(\d+)[,\s]+(\d+)[,\s]+(\d+)\s*\)?", s, re.IGNORECASE)
    if m:
        return tuple(int(x) for x in m.groups())  # type: ignore
    m = re.match(r"^(\d+)\s+(\d+)\s+(\d+)$", s)
    if m:
        return tuple(int(x) for x in m.groups())  # type: ignore
    return hex_to_rgb(s)


async def handle_color_tool(tool: str, text: str) -> str:
    """Process color tool input and return result string."""
    if tool == "contrast":
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        if len(lines) < 2:
            return "⚠️ Send two colours, one per line."
        c1, c2 = parse_color(lines[0]), parse_color(lines[1])
        if not c1 or not c2:
            return "⚠️ Couldn't parse one of those colours."
        ratio   = wcag_contrast(*c1, *c2)
        verdict = (
            "✅ Passes AA" if ratio >= 4.5
            else ("⚠️ AA Large only" if ratio >= 3 else "❌ Fails AA")
        )
        return f"Contrast ratio: *{ratio}:1* — {verdict}"

    rgb = parse_color(text)
    if not rgb:
        return "⚠️ Couldn't parse that colour."
    r, g, b = rgb

    if tool == "convert":
        h, s, l    = rgb_to_hsl(r, g, b)
        c, m, y, k = rgb_to_cmyk(r, g, b)
        return (
            f"HEX:  `{rgb_to_hex(r, g, b)}`\n"
            f"RGB:  `{r}, {g}, {b}`\n"
            f"HSL:  `{h}, {s}%, {l}%`\n"
            f"CMYK: `{c}, {m}, {y}, {k}`"
        )
    if tool == "gradient":
        return f"```css\n{css_gradient(r, g, b)}\n```"
    if tool == "shadow":
        return f"```css\n{css_shadow(r, g, b)}\n```"
    if tool == "tintshade":
        return f"```\n{tint_shade(r, g, b)}\n```"
    return "⚠️ Unknown colour tool."
