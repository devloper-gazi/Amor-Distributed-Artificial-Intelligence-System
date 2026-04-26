"""
Generate an Amor-branded favicon as PNG.

One-shot script. Run inside the app container so the Pillow + DejaVu
font dependencies are guaranteed:

    docker exec amor-app-1 python /app/scripts/generate_favicon.py

Outputs:
    /app/web_ui/static/img/favicon-256.png   (browser tab + apple-touch)
    /app/web_ui/static/img/favicon-32.png    (classic favicon size)
    /app/web_ui/static/img/favicon-16.png    (small size for some browsers)

Design: rounded-square dark background (Tokyo Night-ish), bright cyan
uppercase "A" centered with a tiny accent dot (echoing the welcome
screen's sparkle motif). Single source of truth for size + colors lives
at the top of this file — edit + re-run to regenerate.
"""

from __future__ import annotations

import os
import sys

from PIL import Image, ImageDraw, ImageFont

# ─── Brand constants ────────────────────────────────────────────────────────
BG_COLOR = (26, 27, 38, 255)        # Tokyo Night background
FG_COLOR = (125, 207, 255, 255)     # bright cyan (matches dashboard accent)
ACCENT_COLOR = (187, 154, 247, 255) # purple sparkle dot
CORNER_RADIUS_RATIO = 0.22          # rounded-square corner softness

# Font candidates — first one that exists wins.
FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",  # Windows fallback if run on host
]

OUT_DIR = "/app/web_ui/static/img"
SIZES = [256, 32, 16]

# ─── Drawing ────────────────────────────────────────────────────────────────


def _pick_font(size: int) -> ImageFont.FreeTypeFont:
    for p in FONT_PATHS:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    # Last-resort built-in (bitmap, ugly at large sizes but better than crash)
    return ImageFont.load_default()


def render(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Rounded-square background.
    radius = int(size * CORNER_RADIUS_RATIO)
    d.rounded_rectangle(
        [(0, 0), (size - 1, size - 1)],
        radius=radius,
        fill=BG_COLOR,
    )

    # Centered "A".
    # Letter cap height ~= 0.62× canvas — readable even at 16×16.
    font_size = int(size * 0.72)
    font = _pick_font(font_size)
    text = "A"

    # Pillow ≥9.2 prefers textbbox; fall back to textsize on older.
    try:
        bbox = d.textbbox((0, 0), text, font=font, anchor="lt")
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
        text_x = (size - text_w) // 2 - bbox[0]
        # Pull up slightly so optical center matches geometric center
        # (capital "A" sits low because the baseline excludes the apex).
        text_y = (size - text_h) // 2 - bbox[1] - int(size * 0.04)
    except AttributeError:                                      # pragma: no cover
        text_w, text_h = font.getsize(text)
        text_x = (size - text_w) // 2
        text_y = (size - text_h) // 2 - int(size * 0.04)

    d.text((text_x, text_y), text, font=font, fill=FG_COLOR)

    # Sparkle dot in the upper-right quadrant — only at sizes large
    # enough for it to read (skip the 16×16 to avoid mush).
    if size >= 32:
        dot_r = max(2, int(size * 0.045))
        cx = int(size * 0.78)
        cy = int(size * 0.24)
        d.ellipse(
            [(cx - dot_r, cy - dot_r), (cx + dot_r, cy + dot_r)],
            fill=ACCENT_COLOR,
        )

    return img


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    for s in SIZES:
        out = os.path.join(OUT_DIR, f"favicon-{s}.png")
        img = render(s)
        img.save(out, "PNG", optimize=True)
        print(f"wrote {out}  ({s}x{s}, {os.path.getsize(out)} bytes)")
    print("done.")


if __name__ == "__main__":
    sys.exit(main())
