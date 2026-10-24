"""Set-of-Mark overlays and action crosshairs on screenshots."""

from __future__ import annotations

import io
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

_PALETTE = [
    (230, 25, 75), (60, 180, 75), (0, 130, 200), (245, 130, 48), (145, 30, 180),
    (70, 160, 160), (240, 50, 230), (128, 128, 0), (0, 0, 128), (170, 110, 40),
]


@lru_cache(maxsize=4)
def _font(size: int):
    for name in ("arialbd.ttf", "arial.ttf", "segoeui.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def draw_marks(img: Image.Image, boxes: list[tuple[int, tuple[int, int, int, int]]]) -> Image.Image:
    """``boxes``: (id, (x, y, w, h)) in the image's pixel space."""
    out = img.convert("RGB").copy()
    draw = ImageDraw.Draw(out)
    size = max(11, min(16, out.width // 110))
    font = _font(size)
    for mark_id, (x, y, w, h) in boxes:
        color = _PALETTE[mark_id % len(_PALETTE)]
        draw.rectangle([x, y, x + w, y + h], outline=color, width=2)
        label = str(mark_id)
        tw = draw.textlength(label, font=font)
        lx, ly = max(x - 1, 0), max(y - size - 3, 0)
        draw.rectangle([lx, ly, lx + tw + 4, ly + size + 2], fill=color)
        draw.text((lx + 2, ly), label, fill=(255, 255, 255), font=font)
    return out


def draw_crosshair(img: Image.Image, point: tuple[int, int], label: str = "") -> Image.Image:
    out = img.convert("RGB").copy()
    draw = ImageDraw.Draw(out)
    x, y = point
    r = 14
    draw.ellipse([x - r, y - r, x + r, y + r], outline=(255, 0, 0), width=3)
    draw.line([x - 2 * r, y, x + 2 * r, y], fill=(255, 0, 0), width=2)
    draw.line([x, y - 2 * r, x, y + 2 * r], fill=(255, 0, 0), width=2)
    if label:
        font = _font(14)
        draw.rectangle([x + r, y + r, x + r + draw.textlength(label, font=font) + 6, y + r + 18], fill=(255, 0, 0))
        draw.text((x + r + 3, y + r + 1), label, fill=(255, 255, 255), font=font)
    return out


def fit(img: Image.Image, max_w: int = 1280, max_h: int = 1280) -> tuple[Image.Image, float]:
    """Downscale for the model. Returns (image, scale) where image_px = orig_px * scale."""
    scale = min(1.0, max_w / img.width, max_h / img.height)
    if scale >= 0.999:
        return img, 1.0
    return img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS), scale


def to_jpeg(img: Image.Image, quality: int = 70) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()
