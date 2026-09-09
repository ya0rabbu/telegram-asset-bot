"""Image effects engine.

Protocol (stdin -> stdout, both JSON):

  stdin:
    {
      "effect": "halftone-dots",
      "width": 300,
      "height": 200,
      "pixels_rgba_b64": "<base64 of raw RGBA bytes, width*height*4 long>",
      "params": {
        "dotPitch": 8,
        "contrast": 1.2,
        "brightness": 1.0,
        "grainIntensity": 18,
        "vignette": 0.3
      }
    }

  stdout (success):
    {"status": "success", "bmp_data_url": "data:image/bmp;base64,...."}

  stdout (failure):
    {"status": "error", "message": "..."}

This script has no dependency on the rest of the bot and can be tested
standalone: `echo '<payload json>' | python python_engine.py`.
"""

from __future__ import annotations

import base64
import json
import sys
from io import BytesIO

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter, ImageOps


# ── I/O helpers ──────────────────────────────────────────────────────────────

def _decode_input(payload: dict) -> tuple[Image.Image, dict]:
    width = int(payload["width"])
    height = int(payload["height"])
    raw = base64.b64decode(payload["pixels_rgba_b64"])
    img = Image.frombytes("RGBA", (width, height), raw)
    params = payload.get("params") or {}
    return img, params


def _encode_output(img: Image.Image) -> str:
    if img.mode == "RGBA":
        img = img.convert("RGB")
    buf = BytesIO()
    img.save(buf, format="BMP")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/bmp;base64,{b64}"


# ── Generic post-processing (grain / vignette / base tone) ─────────────────

def _apply_base_adjustments(img: Image.Image, params: dict) -> Image.Image:
    contrast = float(params.get("contrast", 1.0))
    brightness = float(params.get("brightness", 1.0))
    if contrast != 1.0:
        img = ImageEnhance.Contrast(img).enhance(contrast)
    if brightness != 1.0:
        img = ImageEnhance.Brightness(img).enhance(brightness)
    return img


def _add_grain(img: Image.Image, intensity: float) -> Image.Image:
    if intensity <= 0:
        return img
    arr = np.array(img.convert("RGB")).astype(np.int16)
    noise = np.random.randint(-int(intensity), int(intensity) + 1, arr.shape, dtype=np.int16)
    arr = np.clip(arr + noise, 0, 255).astype(np.uint8)
    return Image.fromarray(arr, mode="RGB")


def _add_vignette(img: Image.Image, strength: float) -> Image.Image:
    if strength <= 0:
        return img
    w, h = img.size
    y, x = np.ogrid[:h, :w]
    cx, cy = w / 2.0, h / 2.0
    max_dist = np.sqrt(cx ** 2 + cy ** 2)
    dist = np.sqrt((x - cx) ** 2 + (y - cy) ** 2) / max_dist
    mask = 1.0 - strength * (dist ** 2)
    mask = np.clip(mask, 0.0, 1.0)
    arr = np.array(img.convert("RGB")).astype(np.float32)
    arr *= mask[..., None]
    arr = np.clip(arr, 0, 255).astype(np.uint8)
    return Image.fromarray(arr, mode="RGB")


# ── Shared building blocks ──────────────────────────────────────────────────

def _duotone(img: Image.Image, dark: tuple[int, int, int], light: tuple[int, int, int]) -> Image.Image:
    gray = np.array(ImageOps.grayscale(img)).astype(np.float32) / 255.0
    dark_a = np.array(dark, dtype=np.float32)
    light_a = np.array(light, dtype=np.float32)
    out = dark_a[None, None, :] + (light_a - dark_a)[None, None, :] * gray[..., None]
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8), mode="RGB")


def _channel_shift(img: Image.Image, dx_r: int, dx_g: int, dx_b: int) -> Image.Image:
    r, g, b = img.convert("RGB").split()

    def _shift(chan: Image.Image, dx: int) -> Image.Image:
        if dx == 0:
            return chan
        return chan.transform(chan.size, Image.AFFINE, (1, 0, -dx, 0, 1, 0), fillcolor=0)

    r = _shift(r, dx_r)
    g = _shift(g, dx_g)
    b = _shift(b, dx_b)
    return Image.merge("RGB", (r, g, b))


def _scanlines(img: Image.Image, spacing: int = 3, darkness: float = 0.35) -> Image.Image:
    arr = np.array(img.convert("RGB")).astype(np.float32)
    h = arr.shape[0]
    mask = np.ones((h, 1, 1), dtype=np.float32)
    mask[::spacing] *= (1.0 - darkness)
    arr *= mask
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), mode="RGB")


def _thermal_palette() -> np.ndarray:
    """Black -> purple -> red -> orange -> yellow -> white, 256 entries."""
    stops = [
        (0, (0, 0, 0)),
        (64, (60, 0, 110)),
        (128, (200, 0, 20)),
        (180, (255, 120, 0)),
        (220, (255, 220, 0)),
        (255, (255, 255, 255)),
    ]
    lut = np.zeros((256, 3), dtype=np.uint8)
    for (x0, c0), (x1, c1) in zip(stops, stops[1:]):
        for i in range(x0, x1 + 1):
            t = (i - x0) / max(1, (x1 - x0))
            lut[i] = [
                int(c0[ch] + (c1[ch] - c0[ch]) * t) for ch in range(3)
            ]
    return lut


_THERMAL_LUT = _thermal_palette()


def _swirl(img: Image.Image, strength: float = 3.0) -> Image.Image:
    arr = np.array(img.convert("RGB"))
    h, w = arr.shape[:2]
    cx, cy = w / 2.0, h / 2.0
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    dx, dy = x - cx, y - cy
    r = np.sqrt(dx ** 2 + dy ** 2)
    max_r = np.sqrt(cx ** 2 + cy ** 2)
    theta = np.arctan2(dy, dx) + strength * (1 - r / max_r).clip(0, 1)
    src_x = (cx + r * np.cos(theta)).astype(np.float32)
    src_y = (cy + r * np.sin(theta)).astype(np.float32)
    src_x = np.clip(src_x, 0, w - 1).astype(np.int32)
    src_y = np.clip(src_y, 0, h - 1).astype(np.int32)
    out = arr[src_y, src_x]
    return Image.fromarray(out, mode="RGB")


def _bayer_matrix() -> np.ndarray:
    m = np.array([[0, 8, 2, 10],
                  [12, 4, 14, 6],
                  [3, 11, 1, 9],
                  [15, 7, 13, 5]], dtype=np.float32)
    return (m + 0.5) / 16.0


_BAYER = _bayer_matrix()


# ── Effect implementations ──────────────────────────────────────────────────

def fx_halftone_dots(img: Image.Image, params: dict) -> Image.Image:
    pitch = max(2, int(params.get("dotPitch", 8)))
    gray = ImageOps.grayscale(img)
    w, h = gray.size
    out = Image.new("RGB", (w, h), (255, 255, 255))
    gray_arr = np.array(gray)
    from PIL import ImageDraw
    draw = ImageDraw.Draw(out)
    for by in range(0, h, pitch):
        for bx in range(0, w, pitch):
            block = gray_arr[by:by + pitch, bx:bx + pitch]
            if block.size == 0:
                continue
            avg = 255 - block.mean()
            radius = (avg / 255.0) * (pitch / 2.0)
            cx, cy = bx + pitch / 2, by + pitch / 2
            if radius > 0.4:
                draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], fill=(0, 0, 0))
    return out


def fx_comic_cmyk(img: Image.Image, params: dict) -> Image.Image:
    rgb = img.convert("RGB")
    rgb = ImageEnhance.Color(rgb).enhance(1.8)
    posterized = ImageOps.posterize(rgb, 3)
    edges = rgb.convert("L").filter(ImageFilter.FIND_EDGES)
    edges = edges.point(lambda p: 255 if p > 40 else 0)
    edges_rgb = ImageOps.invert(edges).convert("RGB")
    return Image.composite(posterized, Image.new("RGB", img.size, (0, 0, 0)), edges.convert("L").point(lambda p: 255 - p))


def fx_retro_8bit(img: Image.Image, params: dict) -> Image.Image:
    w, h = img.size
    small = img.convert("RGB").resize((max(1, w // 10), max(1, h // 10)), Image.NEAREST)
    small = ImageOps.posterize(small, 3)
    return small.resize((w, h), Image.NEAREST)


def fx_cinematic_noir(img: Image.Image, params: dict) -> Image.Image:
    gray = ImageOps.grayscale(img).convert("RGB")
    gray = ImageEnhance.Contrast(gray).enhance(1.4)
    return gray


def fx_cyber_glitch(img: Image.Image, params: dict) -> Image.Image:
    shifted = _channel_shift(img, dx_r=6, dx_g=0, dx_b=-6)
    return _scanlines(shifted, spacing=4, darkness=0.25)


def fx_thermal_flir(img: Image.Image, params: dict) -> Image.Image:
    gray = np.array(ImageOps.grayscale(img))
    colored = _THERMAL_LUT[gray]
    return Image.fromarray(colored, mode="RGB")


def fx_blueprint_cyan(img: Image.Image, params: dict) -> Image.Image:
    edges = img.convert("L").filter(ImageFilter.FIND_EDGES)
    edges_arr = np.array(edges)
    out = np.zeros((*edges_arr.shape, 3), dtype=np.uint8)
    out[..., :] = (10, 30, 80)
    mask = edges_arr > 25
    out[mask] = (255, 255, 255)
    return Image.fromarray(out, mode="RGB")


def fx_ascii_matrix(img: Image.Image, params: dict) -> Image.Image:
    """Real character-based ASCII art (Matrix-style green-on-black),
    not just colored noise: each cell is brightness-mapped to an actual
    monospace character and drawn with PIL, like a true ASCII render.
    """
    from PIL import ImageDraw, ImageFont

    w, h = img.size
    cell = max(6, int(params.get("dotPitch", 8)))
    ramp = " .:-=+*#%@"  # dark -> light

    gray = ImageOps.grayscale(img)
    cols = max(1, w // cell)
    rows = max(1, h // cell)
    small = gray.resize((cols, rows), Image.BILINEAR)
    brightness = np.array(small).astype(np.float32) / 255.0

    out = Image.new("RGB", (w, h), (0, 0, 0))
    draw = ImageDraw.Draw(out)
    try:
        font = ImageFont.truetype("DejaVuSansMono.ttf", size=max(8, int(cell * 1.15)))
    except Exception:
        font = ImageFont.load_default()

    rng = np.random.default_rng()
    for row in range(rows):
        for col in range(cols):
            level = brightness[row, col]
            char = ramp[min(len(ramp) - 1, int(level * (len(ramp) - 1)))]
            if char == " ":
                continue
            # slight per-character brightness jitter for a "live" matrix feel
            g = int(np.clip(80 + level * 175 + rng.integers(-15, 15), 40, 255))
            draw.text((col * cell, row * cell), char, font=font, fill=(0, g, 0))

    return out



def fx_sobel_neon(img: Image.Image, params: dict) -> Image.Image:
    edges = img.convert("L").filter(ImageFilter.FIND_EDGES)
    edges = ImageEnhance.Contrast(edges).enhance(2.0)
    arr = np.array(edges).astype(np.float32) / 255.0
    out = np.zeros((*arr.shape, 3), dtype=np.uint8)
    out[..., 0] = (arr * 60).astype(np.uint8)
    out[..., 1] = (arr * 255).astype(np.uint8)
    out[..., 2] = (arr * 220).astype(np.uint8)
    return Image.fromarray(out, mode="RGB")


def fx_risograph_duo(img: Image.Image, params: dict) -> Image.Image:
    base = _duotone(img, dark=(20, 20, 90), light=(255, 90, 40))
    return _channel_shift(base, dx_r=2, dx_g=0, dx_b=-2)


def fx_crosshatch_engraving(img: Image.Image, params: dict) -> Image.Image:
    edges = img.convert("L").filter(ImageFilter.FIND_EDGES)
    edges = ImageOps.invert(edges)
    edges = ImageEnhance.Contrast(edges).enhance(1.6)
    return edges.convert("RGB")


def fx_bayer_dither(img: Image.Image, params: dict) -> Image.Image:
    gray = np.array(ImageOps.grayscale(img)).astype(np.float32) / 255.0
    h, w = gray.shape
    tiled = np.tile(_BAYER, (h // 4 + 1, w // 4 + 1))[:h, :w]
    out = (gray > tiled).astype(np.uint8) * 255
    return Image.fromarray(out, mode="L").convert("RGB")


def fx_watercolor(img: Image.Image, params: dict) -> Image.Image:
    """Painterly watercolor wash with soft-bled edges and paper grain."""
    rgb = img.convert("RGB")
    w, h = rgb.size

    # Soften detail into color "pools" (bigger blur = more of a wash).
    blurred = rgb.filter(ImageFilter.GaussianBlur(3.5))
    blurred = ImageEnhance.Color(blurred).enhance(1.6)
    blurred = ImageEnhance.Brightness(blurred).enhance(1.05)

    # Soft, blurred edge-darkening (like ink bleeding at color boundaries)
    # instead of a single hard-edged FIND_EDGES pass.
    edges = rgb.convert("L").filter(ImageFilter.FIND_EDGES)
    edges = edges.filter(ImageFilter.GaussianBlur(1.5))
    edge_mask = edges.point(lambda p: min(255, int(p * 2.2)))
    darkened = ImageEnhance.Brightness(blurred).enhance(0.75)
    result = Image.composite(darkened, blurred, edge_mask)

    # Subtle paper-grain texture for a hand-painted feel.
    arr = np.array(result).astype(np.int16)
    grain = (np.random.default_rng().normal(0, 6, arr.shape[:2])[..., None]).astype(np.int16)
    arr = np.clip(arr + grain, 0, 255).astype(np.uint8)
    return Image.fromarray(arr, mode="RGB")


def fx_oil_paint(img: Image.Image, params: dict) -> Image.Image:
    """Chunky brush-stroke oil-paint look using a stronger mode filter
    (clusters pixels into flat color patches like real brushwork) plus
    light edge redraw so shapes stay readable."""
    rgb = img.convert("RGB")
    w, h = rgb.size
    # Work at reduced size so ModeFilter clusters read as brush strokes
    # rather than fine noise, then upscale back — this is what gives the
    # "chunky paint daub" look instead of a barely-visible smooth blur.
    small = rgb.resize((max(1, w // 2), max(1, h // 2)), Image.BILINEAR)
    painted = small.filter(ImageFilter.ModeFilter(size=9))
    painted = painted.filter(ImageFilter.ModeFilter(size=7))
    painted = painted.resize((w, h), Image.BILINEAR)
    painted = painted.filter(ImageFilter.SMOOTH_MORE)
    painted = ImageEnhance.Color(painted).enhance(1.25)
    painted = ImageEnhance.Contrast(painted).enhance(1.1)
    return painted



def fx_vaporwave(img: Image.Image, params: dict) -> Image.Image:
    base = _duotone(img, dark=(30, 0, 60), light=(0, 255, 255))
    tint = Image.new("RGB", img.size, (255, 0, 200))
    blended = Image.blend(base, tint, 0.15)
    return _scanlines(blended, spacing=3, darkness=0.15)


def fx_vhs_tape(img: Image.Image, params: dict) -> Image.Image:
    shifted = _channel_shift(img, dx_r=3, dx_g=0, dx_b=-3)
    blurred = shifted.filter(ImageFilter.GaussianBlur(0.6))
    return _scanlines(blurred, spacing=3, darkness=0.3)


def fx_autumn_tone(img: Image.Image, params: dict) -> Image.Image:
    return _duotone(img, dark=(45, 20, 10), light=(255, 170, 60))


def fx_frozen_ice(img: Image.Image, params: dict) -> Image.Image:
    return _duotone(img, dark=(0, 20, 45), light=(180, 230, 255))


def fx_moonlight(img: Image.Image, params: dict) -> Image.Image:
    return _duotone(img, dark=(5, 10, 30), light=(180, 200, 255))


def fx_duotone(img: Image.Image, params: dict) -> Image.Image:
    return _duotone(img, dark=(25, 15, 60), light=(255, 200, 120))


def fx_emboss(img: Image.Image, params: dict) -> Image.Image:
    return img.convert("RGB").filter(ImageFilter.EMBOSS)


def fx_pixelate(img: Image.Image, params: dict) -> Image.Image:
    pitch = max(2, int(params.get("dotPitch", 8)))
    w, h = img.size
    small = img.convert("RGB").resize((max(1, w // pitch), max(1, h // pitch)), Image.BILINEAR)
    return small.resize((w, h), Image.NEAREST)


def fx_swirl_distort(img: Image.Image, params: dict) -> Image.Image:
    return _swirl(img, strength=3.0)


def fx_lomography(img: Image.Image, params: dict) -> Image.Image:
    rgb = ImageEnhance.Color(img.convert("RGB")).enhance(1.6)
    rgb = ImageEnhance.Contrast(rgb).enhance(1.3)
    return _add_vignette(rgb, 0.55)


def fx_glitch_art(img: Image.Image, params: dict) -> Image.Image:
    arr = np.array(img.convert("RGB"))
    h, w = arr.shape[:2]
    out = arr.copy()
    rng = np.random.default_rng()
    n_slices = max(3, h // 20)
    for _ in range(n_slices):
        y0 = rng.integers(0, h)
        slice_h = rng.integers(2, max(3, h // 15))
        dx = rng.integers(-15, 15)
        y1 = min(h, y0 + slice_h)
        out[y0:y1] = np.roll(arr[y0:y1], dx, axis=1)
    return Image.fromarray(out, mode="RGB")


def fx_forest_green(img: Image.Image, params: dict) -> Image.Image:
    return _duotone(img, dark=(5, 20, 10), light=(120, 200, 90))


def fx_inferno(img: Image.Image, params: dict) -> Image.Image:
    return _duotone(img, dark=(10, 0, 0), light=(255, 120, 0))


def fx_horror_red(img: Image.Image, params: dict) -> Image.Image:
    base = _duotone(img, dark=(10, 0, 0), light=(190, 15, 15))
    return _add_vignette(base, 0.6)


def fx_cherry_blossom(img: Image.Image, params: dict) -> Image.Image:
    return _duotone(img, dark=(40, 10, 30), light=(255, 180, 210))


def fx_desert_sand(img: Image.Image, params: dict) -> Image.Image:
    return _duotone(img, dark=(40, 25, 10), light=(230, 190, 130))


def fx_neon_poster(img: Image.Image, params: dict) -> Image.Image:
    posterized = ImageOps.posterize(ImageEnhance.Color(img.convert("RGB")).enhance(2.0), 3)
    edges = img.convert("L").filter(ImageFilter.FIND_EDGES).point(lambda p: 255 if p > 30 else 0)
    glow = Image.merge("RGB", (edges, edges, edges))
    return Image.blend(posterized, glow, 0.25)


def fx_mirror_reflect(img: Image.Image, params: dict) -> Image.Image:
    rgb = img.convert("RGB")
    w, h = rgb.size
    top = rgb.crop((0, 0, w, h // 2))
    flipped = ImageOps.flip(top)
    out = rgb.copy()
    out.paste(flipped, (0, h - top.size[1]))
    return out


EFFECT_FUNCS = {
    "halftone-dots": fx_halftone_dots,
    "comic-cmyk": fx_comic_cmyk,
    "retro-8bit": fx_retro_8bit,
    "cinematic-noir": fx_cinematic_noir,
    "cyber-glitch": fx_cyber_glitch,
    "thermal-flir": fx_thermal_flir,
    "blueprint-cyan": fx_blueprint_cyan,
    "ascii-matrix": fx_ascii_matrix,
    "sobel-neon": fx_sobel_neon,
    "risograph-duo": fx_risograph_duo,
    "crosshatch-engraving": fx_crosshatch_engraving,
    "bayer-dither": fx_bayer_dither,
    "watercolor": fx_watercolor,
    "oil-paint": fx_oil_paint,
    "vaporwave": fx_vaporwave,
    "vhs-tape": fx_vhs_tape,
    "autumn-tone": fx_autumn_tone,
    "frozen-ice": fx_frozen_ice,
    "moonlight": fx_moonlight,
    "duotone": fx_duotone,
    "emboss": fx_emboss,
    "pixelate": fx_pixelate,
    "swirl-distort": fx_swirl_distort,
    "lomography": fx_lomography,
    "glitch-art": fx_glitch_art,
    "forest-green": fx_forest_green,
    "inferno": fx_inferno,
    "horror-red": fx_horror_red,
    "cherry-blossom": fx_cherry_blossom,
    "desert-sand": fx_desert_sand,
    "neon-poster": fx_neon_poster,
    "mirror-reflect": fx_mirror_reflect,
}

# Effects that already produce a stylized/graphic look where extra film
# grain would just muddy the result.
_SKIP_GRAIN = {
    "halftone-dots", "ascii-matrix", "bayer-dither", "pixelate",
    "blueprint-cyan", "crosshatch-engraving", "retro-8bit", "glitch-art",
}

# Effects that already bake in their own vignette — applying the global
# one on top double-darkens the corners (this was a real bug: horror-red
# and lomography came out almost black at the edges).
_SKIP_VIGNETTE = _SKIP_GRAIN | {"horror-red", "lomography"}


def apply_effect(img: Image.Image, effect: str, params: dict) -> Image.Image:
    func = EFFECT_FUNCS.get(effect)
    if func is None:
        raise ValueError(f"Unknown effect: {effect}")

    working = _apply_base_adjustments(img.convert("RGB"), params)
    result = func(working, params)

    if effect not in _SKIP_GRAIN:
        result = _add_grain(result, float(params.get("grainIntensity", 0)) * 0.3)
    if effect not in _SKIP_VIGNETTE:
        result = _add_vignette(result, float(params.get("vignette", 0)) * 0.5)

    return result


def main() -> None:
    try:
        raw_stdin = sys.stdin.buffer.read()
        payload = json.loads(raw_stdin.decode("utf-8"))
        img, params = _decode_input(payload)
        effect = payload["effect"]
        result_img = apply_effect(img, effect, params)
        data_url = _encode_output(result_img)
        print(json.dumps({"status": "success", "bmp_data_url": data_url}))
    except Exception as exc:  # noqa: BLE001 - report all failures to caller
        print(json.dumps({"status": "error", "message": str(exc)}), file=sys.stdout)
        sys.exit(1)


if __name__ == "__main__":
    main()
