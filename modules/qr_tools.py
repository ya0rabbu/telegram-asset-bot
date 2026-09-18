"""QR code generation and scanning."""

from __future__ import annotations
import asyncio
from io import BytesIO


async def generate_qr(text: str) -> bytes:
    import qrcode
    from qrcode.constants import ERROR_CORRECT_M

    def _run() -> bytes:
        qr = qrcode.QRCode(
            version=None,
            error_correction=ERROR_CORRECT_M,
            box_size=10,
            border=4,
        )
        qr.add_data(text)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        buf = BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue()

    return await asyncio.to_thread(_run)


async def scan_qr(image_bytes: bytes) -> list[str]:
    import cv2
    import numpy as np

    def _run() -> list[str]:
        arr = np.frombuffer(image_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError("Could not decode image.")
        detector = cv2.QRCodeDetector()
        found: list[str] = []
        try:
            ok, decoded_info, _, _ = detector.detectAndDecodeMulti(img)
            if ok:
                found = [s for s in decoded_info if s]
        except cv2.error:
            pass
        if not found:
            data, _, _ = detector.detectAndDecode(img)
            if data:
                found = [data]
        return found

    return await asyncio.to_thread(_run)
