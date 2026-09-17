"""
BangaliIcon Image Effects Engine
=================================
Standalone subprocess worker — communicates via stdin/stdout JSON.
"""

from __future__ import annotations

import base64
import json
import sys
from io import BytesIO
from typing import Callable

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps


def _decode_input(payload: dict) -> tuple[Image.Image, dict]:
    width  = int(payload["width"])
    height = int(payload["height"])
    raw    = base64.b64decode(payload["pixels_rgba_b64"])
    img    = Image.frombytes("RGBA", (width, height), raw)
    return img, payload.get("params") or {}


def _encode_output(img: Image.Image) -> str:
    if img.mode == "RGBA":
        img = img.convert("RGB")
    buf = BytesIO()
    img.save(buf, format="BMP")
    return "data:image/bmp;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _apply_base_adjustments(img: Image.Image, params: dict) -> Image.Image:
    contrast   = float(params.get("contrast",   1.0))
    brightness = float(params.get("brightness", 1.0))
    if contrast   != 1.0:
        img = ImageEnhance.Contrast(img).enhance(contrast)
    if brightness != 1.0:
        img = ImageEnhance.Brightness(img).enhance(brightness)
    return img


def _add_grain(img: Image.Image, intensity: float) -> Image.Image:
    if intensity <= 0:
        return img
    arr   = np.array(img.convert("RGB")).astype(np.int16)
    noise = np.random.randint(-int(intensity), int(intensity) + 1, arr.shape, dtype=np.int16)
    return Image.fromarray(np.clip(arr + noise, 0, 255).astype(np.uint8), "RGB")


def _add_vignette(img: Image.Image, strength: float) -> Image.Image:
    if strength <= 0:
        return img
    w, h  = img.size
    cx, cy = w / 2.0, h / 2.0
    y, x  = np.ogrid[:h, :w]
    dist  = np.sqrt((x - cx) ** 2 + (y - cy) ** 2) / np.sqrt(cx ** 2 + cy ** 2)
    mask  = np.clip(1.0 - strength * dist ** 2, 0.0, 1.0)
    arr   = np.array(img.convert("RGB")).astype(np.float32)
    arr  *= mask[..., None]
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB")


ASPECT_RATIOS: dict[str, tuple[int, int] | None] = {
    "original": None,
    "1:1":  (1, 1),
    "4:5":  (4, 5),
    "5:4":  (5, 4),
    "16:9": (16, 9),
    "9:16": (9, 16),
    "4:3":  (4, 3),
    "3:4":  (3, 4),
    "3:2":  (3, 2),
    "2:3":  (2, 3),
    "21:9": (21, 9),
}


def _apply_aspect_ratio(img: Image.Image, ratio_key: str | None, mode: str = "crop") -> Image.Image:
    if not ratio_key or ratio_key not in ASPECT_RATIOS:
        return img
    ratio = ASPECT_RATIOS[ratio_key]
    if ratio is None:
        return img
    w, h = img.size
    target_ratio = ratio[0] / ratio[1]
    cur_ratio    = w / h

    if abs(cur_ratio - target_ratio) < 1e-3:
        return img

    if mode == "pad":
        if cur_ratio > target_ratio:
            new_h  = max(1, int(round(w / target_ratio)))
            canvas = Image.new("RGB", (w, new_h), (0, 0, 0))
            canvas.paste(img.convert("RGB"), (0, (new_h - h) // 2))
            return canvas
        else:
            new_w  = max(1, int(round(h * target_ratio)))
            canvas = Image.new("RGB", (new_w, h), (0, 0, 0))
            canvas.paste(img.convert("RGB"), ((new_w - w) // 2, 0))
            return canvas

    if cur_ratio > target_ratio:
        new_w = max(1, int(round(h * target_ratio)))
        left  = (w - new_w) // 2
        return img.crop((left, 0, left + new_w, h))
    else:
        new_h = max(1, int(round(w / target_ratio)))
        top   = (h - new_h) // 2
        return img.crop((0, top, w, top + new_h))


def _apply_resize(img: Image.Image, out_w: int | None, out_h: int | None) -> Image.Image:
    if not out_w or not out_h:
        return img
    out_w, out_h = max(1, int(out_w)), max(1, int(out_h))
    if (out_w, out_h) == img.size:
        return img
    return img.resize((out_w, out_h), Image.LANCZOS)


def _duotone(
    img: Image.Image,
    dark: tuple[int, int, int],
    light: tuple[int, int, int],
) -> Image.Image:
    gray   = np.array(ImageOps.grayscale(img)).astype(np.float32) / 255.0
    dark_a  = np.array(dark,  dtype=np.float32)
    light_a = np.array(light, dtype=np.float32)
    out    = dark_a + (light_a - dark_a) * gray[..., None]
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8), "RGB")


def _channel_shift(
    img: Image.Image,
    dx_r: int,
    dx_g: int,
    dx_b: int,
) -> Image.Image:
    r, g, b = img.convert("RGB").split()

    def _shift(chan: Image.Image, dx: int) -> Image.Image:
        if dx == 0:
            return chan
        return chan.transform(chan.size, Image.AFFINE, (1, 0, -dx, 0, 1, 0), fillcolor=0)

    return Image.merge("RGB", (_shift(r, dx_r), _shift(g, dx_g), _shift(b, dx_b)))


def _scanlines(img: Image.Image, spacing: int = 3, darkness: float = 0.35) -> Image.Image:
    arr  = np.array(img.convert("RGB")).astype(np.float32)
    mask = np.ones((arr.shape[0], 1, 1), dtype=np.float32)
    mask[::spacing] *= (1.0 - darkness)
    return Image.fromarray(np.clip(arr * mask, 0, 255).astype(np.uint8), "RGB")


def _thermal_palette() -> np.ndarray:
    stops = [
        (0,   (0,   0,   0)),
        (64,  (60,  0,   110)),
        (128, (200, 0,   20)),
        (180, (255, 120, 0)),
        (220, (255, 220, 0)),
        (255, (255, 255, 255)),
    ]
    lut = np.zeros((256, 3), dtype=np.uint8)
    for (x0, c0), (x1, c1) in zip(stops, stops[1:]):
        for i in range(x0, x1 + 1):
            t = (i - x0) / max(1, x1 - x0)
            lut[i] = [int(c0[ch] + (c1[ch] - c0[ch]) * t) for ch in range(3)]
    return lut


_THERMAL_LUT = _thermal_palette()


def _swirl(img: Image.Image, strength: float = 3.0) -> Image.Image:
    arr     = np.array(img.convert("RGB"))
    h, w    = arr.shape[:2]
    cx, cy  = w / 2.0, h / 2.0
    y, x    = np.mgrid[0:h, 0:w].astype(np.float32)
    dx, dy  = x - cx, y - cy
    r       = np.sqrt(dx ** 2 + dy ** 2)
    theta   = np.arctan2(dy, dx) + strength * np.clip(1 - r / np.sqrt(cx**2 + cy**2), 0, 1)
    src_x   = np.clip(cx + r * np.cos(theta), 0, w - 1).astype(np.int32)
    src_y   = np.clip(cy + r * np.sin(theta), 0, h - 1).astype(np.int32)
    return Image.fromarray(arr[src_y, src_x], "RGB")


def _bayer_matrix() -> np.ndarray:
    m = np.array([[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]], dtype=np.float32)
    return (m + 0.5) / 16.0


_BAYER = _bayer_matrix()


def fx_original(img: Image.Image, params: dict) -> Image.Image:
    return img.convert("RGB")


def fx_halftone_dots(img: Image.Image, params: dict) -> Image.Image:
    pitch    = max(2, int(params.get("dotPitch", 8)))
    gray     = ImageOps.grayscale(img)
    w, h     = gray.size
    out      = Image.new("RGB", (w, h), (255, 255, 255))
    draw     = ImageDraw.Draw(out)
    gray_arr = np.array(gray)

    pad_h = (pitch - h % pitch) % pitch
    pad_w = (pitch - w % pitch) % pitch
    padded = np.pad(gray_arr, ((0, pad_h), (0, pad_w)), mode="edge")
    ph, pw = padded.shape
    blocks  = padded.reshape(ph // pitch, pitch, pw // pitch, pitch)
    avg     = 255 - blocks.mean(axis=(1, 3))

    rows, cols = avg.shape
    for row in range(rows):
        for col in range(cols):
            radius = (avg[row, col] / 255.0) * (pitch / 2.0)
            if radius < 0.4:
                continue
            cx = col * pitch + pitch / 2
            cy = row * pitch + pitch / 2
            draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], fill=(0, 0, 0))
    return out


def fx_comic_cmyk(img: Image.Image, params: dict) -> Image.Image:
    rgb        = ImageEnhance.Color(img.convert("RGB")).enhance(1.8)
    posterized = ImageOps.posterize(rgb, 3)
    edges      = rgb.convert("L").filter(ImageFilter.FIND_EDGES)
    edge_mask  = edges.point(lambda p: 255 if p > 40 else 0)
    dark       = Image.new("RGB", img.size, (0, 0, 0))
    return Image.composite(dark, posterized, edge_mask)


def fx_retro_8bit(img: Image.Image, params: dict) -> Image.Image:
    w, h  = img.size
    small = ImageOps.posterize(img.convert("RGB").resize((max(1, w // 10), max(1, h // 10)), Image.NEAREST), 3)
    return small.resize((w, h), Image.NEAREST)


def fx_risograph_duo(img: Image.Image, params: dict) -> Image.Image:
    return _channel_shift(_duotone(img, (20, 20, 90), (255, 90, 40)), 2, 0, -2)


def fx_crosshatch_engraving(img: Image.Image, params: dict) -> Image.Image:
    edges = ImageOps.invert(img.convert("L").filter(ImageFilter.FIND_EDGES))
    return ImageEnhance.Contrast(edges).enhance(1.6).convert("RGB")


def fx_bayer_dither(img: Image.Image, params: dict) -> Image.Image:
    gray = np.array(ImageOps.grayscale(img)).astype(np.float32) / 255.0
    h, w = gray.shape
    tiled = np.tile(_BAYER, (h // 4 + 1, w // 4 + 1))[:h, :w]
    return Image.fromarray(((gray > tiled) * 255).astype(np.uint8), "L").convert("RGB")


def fx_vhs_tape(img: Image.Image, params: dict) -> Image.Image:
    return _scanlines(_channel_shift(img, 3, 0, -3).filter(ImageFilter.GaussianBlur(0.6)), 3, 0.3)


def fx_lomography(img: Image.Image, params: dict) -> Image.Image:
    rgb = ImageEnhance.Contrast(ImageEnhance.Color(img.convert("RGB")).enhance(1.6)).enhance(1.3)
    return _add_vignette(rgb, 0.55)


def fx_duotone(img: Image.Image,        params: dict) -> Image.Image: return _duotone(img, (25, 15, 60),   (255, 200, 120))
def fx_autumn_tone(img: Image.Image,    params: dict) -> Image.Image: return _duotone(img, (45, 20, 10),   (255, 170, 60))
def fx_forest_green(img: Image.Image,   params: dict) -> Image.Image: return _duotone(img, (5,  20, 10),   (120, 200, 90))
def fx_desert_sand(img: Image.Image,    params: dict) -> Image.Image: return _duotone(img, (40, 25, 10),   (230, 190, 130))
def fx_cherry_blossom(img: Image.Image, params: dict) -> Image.Image: return _duotone(img, (40, 10, 30),   (255, 180, 210))
def fx_moonlight(img: Image.Image,      params: dict) -> Image.Image: return _duotone(img, (5,  10, 30),   (180, 200, 255))
def fx_frozen_ice(img: Image.Image,     params: dict) -> Image.Image: return _duotone(img, (0,  20, 45),   (180, 230, 255))


def fx_watercolor(img: Image.Image, params: dict) -> Image.Image:
    rgb     = img.convert("RGB")
    blurred = ImageEnhance.Brightness(
        ImageEnhance.Color(rgb.filter(ImageFilter.GaussianBlur(3.5))).enhance(1.6)
    ).enhance(1.05)
    edges     = rgb.convert("L").filter(ImageFilter.FIND_EDGES).filter(ImageFilter.GaussianBlur(1.5))
    edge_mask = edges.point(lambda p: min(255, int(p * 2.2)))
    result    = Image.composite(ImageEnhance.Brightness(blurred).enhance(0.75), blurred, edge_mask)
    arr       = np.array(result).astype(np.int16)
    grain     = np.random.default_rng().normal(0, 6, arr.shape[:2])[..., None].astype(np.int16)
    return Image.fromarray(np.clip(arr + grain, 0, 255).astype(np.uint8), "RGB")


def fx_oil_paint(img: Image.Image, params: dict) -> Image.Image:
    rgb   = img.convert("RGB")
    w, h  = rgb.size
    small = rgb.resize((max(1, w // 2), max(1, h // 2)), Image.BILINEAR)
    painted = small.filter(ImageFilter.ModeFilter(9)).filter(ImageFilter.ModeFilter(7))
    painted = painted.resize((w, h), Image.BILINEAR).filter(ImageFilter.SMOOTH_MORE)
    return ImageEnhance.Contrast(ImageEnhance.Color(painted).enhance(1.25)).enhance(1.1)


def fx_cinematic_noir(img: Image.Image, params: dict) -> Image.Image:
    return ImageEnhance.Contrast(ImageOps.grayscale(img).convert("RGB")).enhance(1.4)


def fx_emboss(img: Image.Image, params: dict) -> Image.Image:
    return img.convert("RGB").filter(ImageFilter.EMBOSS)


def fx_pencil_sketch(img: Image.Image, params: dict) -> Image.Image:
    gray     = ImageOps.grayscale(img)
    inverted = ImageOps.invert(gray)
    blurred  = inverted.filter(ImageFilter.GaussianBlur(radius=21))
    gray_arr = np.array(gray).astype(np.float32)
    blur_arr = np.array(blurred).astype(np.float32) / 255.0
    denom    = np.clip(1.0 - blur_arr, 0.01, 1.0)
    sketch   = np.clip(gray_arr / denom, 0, 255).astype(np.uint8)
    return Image.fromarray(sketch, "L").convert("RGB")


def fx_color_splash(img: Image.Image, params: dict) -> Image.Image:
    rgb  = img.convert("RGB")
    arr  = np.array(rgb).astype(np.float32)
    gray = np.array(ImageOps.grayscale(rgb)).astype(np.float32)
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    keep = (r > 120) & (r > g * 1.3) & (r > b * 1.2)
    out  = np.stack([gray, gray, gray], axis=-1)
    out[keep] = arr[keep]
    return Image.fromarray(out.clip(0, 255).astype(np.uint8), "RGB")


def fx_stained_glass(img: Image.Image, params: dict) -> Image.Image:
    rgb   = img.convert("RGB")
    w, h  = rgb.size
    arr   = np.array(rgb)
    n     = max(30, int(params.get("dotPitch", 8)) * 12)
    rng   = np.random.default_rng(42)
    px    = rng.integers(0, w, n)
    py    = rng.integers(0, h, n)
    yg, xg = np.mgrid[0:h, 0:w]
    dx   = xg[:, :, None] - px[None, None, :]
    dy   = yg[:, :, None] - py[None, None, :]
    nearest = np.argmin(dx ** 2 + dy ** 2, axis=2)
    out  = np.zeros_like(arr)
    for i in range(n):
        mask = nearest == i
        if mask.any():
            out[mask] = arr[mask].mean(axis=0).astype(np.uint8)
    result = Image.fromarray(out, "RGB")
    edges  = result.convert("L").filter(ImageFilter.FIND_EDGES)
    lead   = edges.point(lambda p: 180 if p > 20 else 0)
    dark   = Image.new("RGB", (w, h), (15, 15, 15))
    return Image.composite(dark, result, lead)


def fx_cyber_glitch(img: Image.Image, params: dict) -> Image.Image:
    return _scanlines(_channel_shift(img, 6, 0, -6), 4, 0.25)


def fx_glitch_art(img: Image.Image, params: dict) -> Image.Image:
    arr  = np.array(img.convert("RGB"))
    h, w = arr.shape[:2]
    out  = arr.copy()
    rng  = np.random.default_rng()
    for _ in range(max(3, h // 20)):
        y0  = rng.integers(0, h)
        y1  = min(h, y0 + rng.integers(2, max(3, h // 15)))
        dx  = rng.integers(-15, 15)
        out[y0:y1] = np.roll(arr[y0:y1], dx, axis=1)
    return Image.fromarray(out, "RGB")


def fx_ascii_matrix(img: Image.Image, params: dict) -> Image.Image:
    w, h  = img.size
    cell  = max(6, int(params.get("dotPitch", 8)))
    ramp  = " .:-=+*#%@"
    gray  = ImageOps.grayscale(img)
    cols  = max(1, w // cell)
    rows  = max(1, h // cell)
    small = gray.resize((cols, rows), Image.BILINEAR)
    bright = np.array(small).astype(np.float32) / 255.0
    out   = Image.new("RGB", (w, h), (0, 0, 0))
    draw  = ImageDraw.Draw(out)
    try:
        font = ImageFont.truetype("DejaVuSansMono.ttf", size=max(8, int(cell * 1.15)))
    except Exception:
        font = ImageFont.load_default()
    rng = np.random.default_rng()
    for row in range(rows):
        for col in range(cols):
            level = bright[row, col]
            char  = ramp[min(len(ramp) - 1, int(level * (len(ramp) - 1)))]
            if char == " ":
                continue
            g = int(np.clip(80 + level * 175 + rng.integers(-15, 15), 40, 255))
            draw.text((col * cell, row * cell), char, font=font, fill=(0, g, 0))
    return out


def fx_sobel_neon(img: Image.Image, params: dict) -> Image.Image:
    edges = ImageEnhance.Contrast(img.convert("L").filter(ImageFilter.FIND_EDGES)).enhance(2.0)
    arr   = np.array(edges).astype(np.float32) / 255.0
    out   = np.zeros((*arr.shape, 3), dtype=np.uint8)
    out[..., 0] = (arr * 60).astype(np.uint8)
    out[..., 1] = (arr * 255).astype(np.uint8)
    out[..., 2] = (arr * 220).astype(np.uint8)
    return Image.fromarray(out, "RGB")


def fx_vaporwave(img: Image.Image, params: dict) -> Image.Image:
    base   = _duotone(img, (30, 0, 60), (0, 255, 255))
    blended = Image.blend(base, Image.new("RGB", img.size, (255, 0, 200)), 0.15)
    return _scanlines(blended, 3, 0.15)


def fx_neon_poster(img: Image.Image, params: dict) -> Image.Image:
    posterized = ImageOps.posterize(ImageEnhance.Color(img.convert("RGB")).enhance(2.0), 3)
    edges      = img.convert("L").filter(ImageFilter.FIND_EDGES).point(lambda p: 255 if p > 30 else 0)
    glow       = Image.merge("RGB", (edges, edges, edges))
    return Image.blend(posterized, glow, 0.25)


def fx_neon_glow(img: Image.Image, params: dict) -> Image.Image:
    rgb   = img.convert("RGB")
    edges = ImageEnhance.Contrast(rgb.convert("L").filter(ImageFilter.FIND_EDGES)).enhance(3.0)
    arr   = np.array(edges).astype(np.float32) / 255.0
    out   = np.zeros((*arr.shape, 3), dtype=np.float32)
    out[..., 0] = arr * 255
    out[..., 1] = arr * 50
    out[..., 2] = arr * 255
    neon  = Image.fromarray(out.clip(0, 255).astype(np.uint8), "RGB")
    glow  = neon.filter(ImageFilter.GaussianBlur(3))
    return Image.blend(neon, glow, 0.6)


def fx_swirl_distort(img: Image.Image,  params: dict) -> Image.Image: return _swirl(img, 3.0)


def fx_mirror_reflect(img: Image.Image, params: dict) -> Image.Image:
    rgb  = img.convert("RGB")
    w, h = rgb.size
    top  = rgb.crop((0, 0, w, h // 2))
    out  = rgb.copy()
    out.paste(ImageOps.flip(top), (0, h - top.size[1]))
    return out


def fx_pixelate(img: Image.Image, params: dict) -> Image.Image:
    pitch = max(2, int(params.get("dotPitch", 8)))
    w, h  = img.size
    small = img.convert("RGB").resize((max(1, w // pitch), max(1, h // pitch)), Image.BILINEAR)
    return small.resize((w, h), Image.NEAREST)


def fx_blueprint_cyan(img: Image.Image, params: dict) -> Image.Image:
    edges = np.array(img.convert("L").filter(ImageFilter.FIND_EDGES))
    out   = np.full((*edges.shape, 3), (10, 30, 80), dtype=np.uint8)
    out[edges > 25] = (255, 255, 255)
    return Image.fromarray(out, "RGB")


def fx_thermal_flir(img: Image.Image, params: dict) -> Image.Image:
    return Image.fromarray(_THERMAL_LUT[np.array(ImageOps.grayscale(img))], "RGB")


def fx_inferno(img: Image.Image,    params: dict) -> Image.Image: return _duotone(img, (10,  0,  0),  (255, 120, 0))


def fx_horror_red(img: Image.Image, params: dict) -> Image.Image:
    return _add_vignette(_duotone(img, (10, 0, 0), (190, 15, 15)), 0.6)


def fx_tilt_shift(img: Image.Image, params: dict) -> Image.Image:
    rgb     = img.convert("RGB")
    w, h    = rgb.size
    blurred = rgb.filter(ImageFilter.GaussianBlur(8))
    cy      = h / 2
    band    = h * 0.15
    y_coords = np.arange(h, dtype=np.float32)
    dist    = np.abs(y_coords - cy)
    alpha   = np.clip(1.0 - (dist - band) / (h * 0.25), 0.0, 1.0)
    mask    = Image.fromarray((alpha[:, None] * np.ones((1, w)) * 255).astype(np.uint8), "L")
    result  = Image.composite(rgb, blurred, mask)
    return ImageEnhance.Color(result).enhance(1.4)


def fx_kaleidoscope(img: Image.Image, params: dict) -> Image.Image:
    rgb  = img.convert("RGB")
    w, h = rgb.size
    hw, hh = w // 2, h // 2
    quad = rgb.crop((0, 0, hw, hh))
    out  = Image.new("RGB", (w, h))
    out.paste(quad,                          (0,  0))
    out.paste(quad.transpose(Image.FLIP_LEFT_RIGHT), (hw, 0))
    out.paste(quad.transpose(Image.FLIP_TOP_BOTTOM), (0,  hh))
    out.paste(quad.transpose(Image.ROTATE_180),      (hw, hh))
    return out


EFFECT_FUNCS: dict[str, Callable[[Image.Image, dict], Image.Image]] = {
    "original":              fx_original,
    "halftone-dots":        fx_halftone_dots,
    "comic-cmyk":           fx_comic_cmyk,
    "retro-8bit":           fx_retro_8bit,
    "risograph-duo":        fx_risograph_duo,
    "crosshatch-engraving": fx_crosshatch_engraving,
    "bayer-dither":         fx_bayer_dither,
    "vhs-tape":             fx_vhs_tape,
    "lomography":           fx_lomography,
    "duotone":              fx_duotone,
    "autumn-tone":          fx_autumn_tone,
    "forest-green":         fx_forest_green,
    "desert-sand":          fx_desert_sand,
    "cherry-blossom":       fx_cherry_blossom,
    "moonlight":            fx_moonlight,
    "frozen-ice":           fx_frozen_ice,
    "watercolor":           fx_watercolor,
    "oil-paint":            fx_oil_paint,
    "cinematic-noir":       fx_cinematic_noir,
    "emboss":               fx_emboss,
    "pencil-sketch":        fx_pencil_sketch,
    "color-splash":         fx_color_splash,
    "stained-glass":        fx_stained_glass,
    "cyber-glitch":         fx_cyber_glitch,
    "glitch-art":           fx_glitch_art,
    "ascii-matrix":         fx_ascii_matrix,
    "sobel-neon":           fx_sobel_neon,
    "vaporwave":            fx_vaporwave,
    "neon-poster":          fx_neon_poster,
    "neon-glow":            fx_neon_glow,
    "swirl-distort":        fx_swirl_distort,
    "mirror-reflect":       fx_mirror_reflect,
    "pixelate":             fx_pixelate,
    "blueprint-cyan":       fx_blueprint_cyan,
    "thermal-flir":         fx_thermal_flir,
    "inferno":              fx_inferno,
    "horror-red":           fx_horror_red,
    "tilt-shift":           fx_tilt_shift,
    "kaleidoscope":         fx_kaleidoscope,
}

_SKIP_GRAIN: frozenset[str] = frozenset({
    "original", "halftone-dots", "ascii-matrix", "bayer-dither", "pixelate",
    "blueprint-cyan", "crosshatch-engraving", "retro-8bit",
    "glitch-art", "pencil-sketch", "kaleidoscope",
})

_SKIP_VIGNETTE: frozenset[str] = _SKIP_GRAIN | frozenset({"horror-red", "lomography", "tilt-shift"})


def apply_effect(img: Image.Image, effect: str, params: dict) -> Image.Image:
    func = EFFECT_FUNCS.get(effect)
    if func is None:
        raise ValueError(f"Unknown effect: '{effect}'. "
                         f"Available: {', '.join(sorted(EFFECT_FUNCS))}")

    working = _apply_base_adjustments(img.convert("RGB"), params)
    result  = func(working, params)

    if effect not in _SKIP_GRAIN:
        result = _add_grain(result, float(params.get("grainIntensity", 0)) * 0.3)
    if effect not in _SKIP_VIGNETTE:
        result = _add_vignette(result, float(params.get("vignette", 0)) * 0.5)

    result = _apply_aspect_ratio(result, params.get("aspectRatio", "original"), params.get("cropMode", "crop"))
    result = _apply_resize(result, params.get("outputWidth"), params.get("outputHeight"))

    return result


def main() -> None:
    try:
        payload    = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        img, params = _decode_input(payload)
        effect     = payload["effect"]
        result_img = apply_effect(img, effect, params)
        print(json.dumps({"status": "success", "bmp_data_url": _encode_output(result_img)}))
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"status": "error", "message": str(exc)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
