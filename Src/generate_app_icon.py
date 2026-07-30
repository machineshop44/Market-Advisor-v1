"""
Generate Market Advisor app_icon.png + multi-size app_icon.ico
Brand: forest green squircle + teal/mint candles (matches UI tokens).
Run: py -3.12 generate_app_icon.py
"""
from __future__ import annotations

import math
import os
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.abspath(__file__))
PNG_PATH = os.path.join(ROOT, "app_icon.png")
ICO_PATH = os.path.join(ROOT, "app_icon.ico")

# Match splash / UI brand
BG = (13, 59, 46)           # #0D3B2E
TEAL = (31, 138, 112)       # #1F8A70
MINT = (165, 214, 167)      # #A5D6A7
WHITE = (248, 250, 249)
GOLD = (255, 213, 79)       # peak wick tip
TRANSPARENT = (0, 0, 0, 0)


def _round_rect_mask(size: int, radius: float) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
    return mask


def _candle(draw: ImageDraw.ImageDraw, cx, body_top, body_bot, body_w, wick_top, wick_bot, fill, scale):
    half = body_w / 2
    # wick
    draw.line([(cx, wick_top), (cx, wick_bot)], fill=fill, width=max(1, int(2 * scale)))
    # body
    draw.rounded_rectangle(
        [cx - half, body_top, cx + half, body_bot],
        radius=max(1, int(2 * scale)),
        fill=fill,
    )


def render(size: int) -> Image.Image:
    s = size / 256.0
    img = Image.new("RGBA", (size, size), TRANSPARENT)
    layer = Image.new("RGBA", (size, size), TRANSPARENT)
    d = ImageDraw.Draw(layer)

    # Soft squircle background
    pad = int(8 * s)
    radius = int(56 * s)
    d.rounded_rectangle(
        [pad, pad, size - 1 - pad, size - 1 - pad],
        radius=radius,
        fill=BG + (255,),
    )

    # Subtle inner edge (modern depth without glow soup)
    inset = pad + max(1, int(3 * s))
    d.rounded_rectangle(
        [inset, inset, size - 1 - inset, size - 1 - inset],
        radius=max(1, radius - int(4 * s)),
        outline=TEAL + (70,),
        width=max(1, int(2 * s)),
    )

    # Trend line under candles
    pts = [
        (int(42 * s), int(188 * s)),
        (int(88 * s), int(158 * s)),
        (int(128 * s), int(168 * s)),
        (int(176 * s), int(108 * s)),
        (int(214 * s), int(78 * s)),
    ]
    d.line(pts, fill=MINT + (220,), width=max(2, int(5 * s)), joint="curve")

    # Three ascending candles
    candles = [
        # cx, body_top, body_bot, body_w, wick_top, wick_bot, color
        (78, 128, 172, 28, 112, 188, WHITE),
        (128, 96, 152, 30, 78, 168, TEAL),
        (178, 58, 128, 32, 42, 148, WHITE),
    ]
    for cx, bt, bb, bw, wt, wb, color in candles:
        _candle(
            d,
            int(cx * s),
            int(bt * s),
            int(bb * s),
            int(bw * s),
            int(wt * s),
            int(wb * s),
            color + (255,),
            s,
        )

    # Gold tip on tallest wick
    tip_x = int(178 * s)
    tip_y = int(42 * s)
    r = max(2, int(4 * s))
    d.ellipse([tip_x - r, tip_y - r, tip_x + r, tip_y + r], fill=GOLD + (255,))

    # Apply soft alpha mask so Windows tray corners stay clean
    mask = _round_rect_mask(size, radius + pad * 0.35)
    out = Image.new("RGBA", (size, size), TRANSPARENT)
    out.paste(layer, (0, 0))
    out.putalpha(mask)
    return out


def main():
    master = render(512)
    master.save(PNG_PATH, "PNG")

    # Multi-resolution ICO for Windows taskbar / shortcuts / tray
    sizes = [16, 24, 32, 48, 64, 128, 256]
    icons = [render(sz) for sz in sizes]
    icons[0].save(
        ICO_PATH,
        format="ICO",
        sizes=[(im.width, im.height) for im in icons],
        append_images=icons[1:],
    )
    print(f"Wrote {PNG_PATH}")
    print(f"Wrote {ICO_PATH} sizes={sizes}")


if __name__ == "__main__":
    main()
