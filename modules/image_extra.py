"""Image processing helpers: resize, crop, watermark, compress, icon sizing.

This module was referenced across the codebase (keyboards.py, assets.py,
callbacks.py, commands.py, messages.py) but was missing from the repo,
which caused the ModuleNotFoundError on deploy.
"""

from __future__ import annotations

import asyncio
import re
from io import BytesIO

# ── Constants (used by keyboards.py) ──────────────────────────────────────────

LUMMI_SIZE_LABELS: dict[str, str] = {
    "small":  "🔹 Small   (516 × 640)",
    "medium": "🔸 Medium  (967 × 1200)",
    "large":  "🔶 Large   (1933 × 2400)",
    "xlarge": "🔴 XLarge  (3712 × 4608)",
}

HUGEICONS_SIZES: list[int] = [16, 24, 32, 48, 64, 96, 128, 256, 512]


# ── Size parsing (used by commands.py /compress) ──────────────────────────────

_SIZE_UNIT_RE = re.compile(
    r"^\s*([\d.]+)\s*(kb|mb|k|m|b)?\s*$", re.IGNORECASE
)

_UNIT_MULTIPLIERS = {
    "b":  1,
    "k":  1024,
    "kb": 1024,
    "m":  1024 * 1024,
    "mb": 1024 * 1024,
}


def parse_size_to_bytes(value: str) -> int | None:
    """Parse strings like '500kb', '1.5mb', '200000' into a byte count."""
    m = _SIZE_UNIT_RE.match(value or "")
    if not m:
        return None
    num_str, unit = m.groups()
    try:
        num = float(num_str)
    except ValueError:
        return None
    unit = (unit or "mb").lower()
    multiplier = _UNIT_MULTIPLIERS.get(unit)
    if multiplier is None:
        return None
    size = int(num * multiplier)
    return size if size > 0 else None


# ── Resize / crop ──────────────────────────────────────────────────────────────

async def resize_image(raw: bytes, width: int, height: int) -> bytes:
    """Resize image bytes to exact width x height (stretch/fit)."""
    def _run() -> bytes:
        from PIL import Image
        img = Image.open(BytesIO(raw))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        resized = img.resize((max(1, int(width)), max(1, int(height))), Image.LANCZOS)
        buf = BytesIO()
        resized.save(buf, format="JPEG", quality=92)
        return buf.getvalue()

    return await asyncio.to_thread(_run)


async def crop_to_aspect(
    raw: bytes, ratio_w: float, ratio_h: float, crop_mode: str = "crop"
) -> bytes:
    """Fit an image to a target aspect ratio, either by center-cropping
    (crop_mode='crop') or by padding with letterbox bars (crop_mode='pad')."""
    def _run() -> bytes:
        from PIL import Image
        img = Image.open(BytesIO(raw))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        w0, h0 = img.size
        target_ratio = ratio_w / ratio_h
        current_ratio = w0 / h0

        if crop_mode == "pad":
            if current_ratio > target_ratio:
                new_w = w0
                new_h = int(round(w0 / target_ratio))
            else:
                new_h = h0
                new_w = int(round(h0 * target_ratio))
            canvas = Image.new("RGB", (new_w, new_h), (0, 0, 0))
            offset = ((new_w - w0) // 2, (new_h - h0) // 2)
            canvas.paste(img, offset)
            result = canvas
        else:  # crop mode (default)
            if current_ratio > target_ratio:
                new_w = int(round(h0 * target_ratio))
                new_h = h0
            else:
                new_w = w0
                new_h = int(round(w0 / target_ratio))
            left = max(0, (w0 - new_w) // 2)
            top = max(0, (h0 - new_h) // 2)
            result = img.crop((left, top, left + new_w, top + new_h))

        buf = BytesIO()
        result.save(buf, format="JPEG", quality=92)
        return buf.getvalue()

    return await asyncio.to_thread(_run)


# ── SVG sizing (used by assets.py for Hugeicons) ──────────────────────────────

def adjust_svg_size(svg_text: str, size: int) -> str:
    """Return the SVG markup with width/height set to `size` px."""
    svg = svg_text

    if re.search(r'\swidth="[^"]*"', svg):
        svg = re.sub(r'\swidth="[^"]*"', f' width="{size}"', svg, count=1)
    else:
        svg = re.sub(r"<svg", f'<svg width="{size}"', svg, count=1)

    if re.search(r'\sheight="[^"]*"', svg):
        svg = re.sub(r'\sheight="[^"]*"', f' height="{size}"', svg, count=1)
    else:
        svg = re.sub(r"<svg", f'<svg height="{size}"', svg, count=1)

    return svg


# ── Watermark ─────────────────────────────────────────────────────────────────

_POSITION_MAP = {
    "top-left":     ("left", "top"),
    "top-right":    ("right", "top"),
    "bottom-left":  ("left", "bottom"),
    "bottom-right": ("right", "bottom"),
    "center":       ("center", "center"),
}


async def apply_watermark(
    base_bytes: bytes, watermark_bytes: bytes, position: str = "bottom-right"
) -> bytes:
    """Overlay a watermark image onto the base image at the given position."""
    def _run() -> bytes:
        from PIL import Image
        base = Image.open(BytesIO(base_bytes)).convert("RGBA")
        wm = Image.open(BytesIO(watermark_bytes)).convert("RGBA")

        # Scale watermark to ~20% of base image width, preserving aspect ratio.
        target_w = max(1, int(base.width * 0.2))
        scale = target_w / wm.width
        target_h = max(1, int(wm.height * scale))
        wm = wm.resize((target_w, target_h), Image.LANCZOS)

        margin = max(8, int(base.width * 0.02))
        h_pos, v_pos = _POSITION_MAP.get(position, ("right", "bottom"))

        if h_pos == "left":
            x = margin
        elif h_pos == "center":
            x = (base.width - wm.width) // 2
        else:
            x = base.width - wm.width - margin

        if v_pos == "top":
            y = margin
        elif v_pos == "center":
            y = (base.height - wm.height) // 2
        else:
            y = base.height - wm.height - margin

        composed = base.copy()
        composed.alpha_composite(wm, dest=(x, y))
        result = composed.convert("RGB")

        buf = BytesIO()
        result.save(buf, format="JPEG", quality=92)
        return buf.getvalue()

    return await asyncio.to_thread(_run)


async def remove_watermark_cv2(raw: bytes) -> bytes:
    """Best-effort watermark removal using OpenCV inpainting.

    Detects likely watermark regions (bright/semi-transparent overlays in
    low-detail image areas near the edges) and inpaints over them. This is a
    heuristic best-effort filter, not perfect watermark removal.
    """
    def _run() -> bytes:
        import cv2
        import numpy as np

        arr = np.frombuffer(raw, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError("Could not decode image for watermark removal.")

        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        # Heuristic mask: bright, low-texture regions likely to be overlay text/logo.
        _, bright_mask = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY)
        edges = cv2.Canny(gray, 50, 150)
        edges_dilated = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)

        mask = cv2.bitwise_and(bright_mask, cv2.bitwise_not(edges_dilated))
        mask = cv2.dilate(mask, np.ones((5, 5), np.uint8), iterations=2)

        inpainted = cv2.inpaint(img, mask, inpaintRadius=5, flags=cv2.INPAINT_TELEA)

        ok, buf = cv2.imencode(".jpg", inpainted, [cv2.IMWRITE_JPEG_QUALITY, 92])
        if not ok:
            raise ValueError("Failed to encode result image.")
        return buf.tobytes()

    return await asyncio.to_thread(_run)


# ── Compress ──────────────────────────────────────────────────────────────────

async def compress_to_target(raw: bytes, target_bytes: int) -> bytes:
    """Compress a JPEG to be at or under target_bytes by lowering quality,
    then downscaling if quality reduction alone isn't enough."""
    def _run() -> bytes:
        from PIL import Image
        img = Image.open(BytesIO(raw))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")

        # First pass: reduce quality.
        for quality in (85, 75, 65, 55, 45, 35, 25):
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=quality, optimize=True)
            data = buf.getvalue()
            if len(data) <= target_bytes:
                return data

        # Second pass: downscale progressively if still too large.
        current = img
        data = None
        for scale in (0.85, 0.7, 0.55, 0.4, 0.3, 0.2):
            w = max(1, int(img.width * scale))
            h = max(1, int(img.height * scale))
            resized = img.resize((w, h), Image.LANCZOS)
            buf = BytesIO()
            resized.save(buf, format="JPEG", quality=45, optimize=True)
            data = buf.getvalue()
            if len(data) <= target_bytes:
                return data

        # Fall back to the smallest version we produced.
        return data if data is not None else raw

    return await asyncio.to_thread(_run)
