"""Draws app.ico (and app.png for the window header): a navy-to-teal rounded tile with a
white gear (operations) around a speech bubble (collaboration).

    py tools\\make_icon.py
"""

import math
import os

from PIL import Image, ImageChops, ImageDraw, ImageFilter

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
S = 1024                       # master size; every icon size is scaled down from this
NAVY, TEAL = (31, 56, 100), (14, 124, 134)


def tile():
    grad = Image.new("RGB", (S, S))
    px = grad.load()
    for y in range(S):
        for x in range(S):
            t = (x + y) / (2 * S - 2)            # diagonal blend, top-left navy -> bottom-right teal
            px[x, y] = tuple(int(a + (b - a) * t) for a, b in zip(NAVY, TEAL))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle((24, 24, S - 24, S - 24), radius=210, fill=255)
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    img.paste(grad, (0, 0), mask)
    return img


def gear_mask(cx, cy, r_out, r_in, r_hole, teeth=8):
    m = Image.new("L", (S, S), 0)
    d = ImageDraw.Draw(m)
    d.ellipse((cx - r_in, cy - r_in, cx + r_in, cy + r_in), fill=255)
    half = math.pi / teeth * 0.52                 # tooth half-width (angle)
    for i in range(teeth):
        a = 2 * math.pi * i / teeth
        pts = [(cx + r * math.cos(a + s * w), cy + r * math.sin(a + s * w))
               for r, w, s in ((r_in - 10, half * 1.25, -1), (r_out, half, -1),
                               (r_out, half, 1), (r_in - 10, half * 1.25, 1))]
        d.polygon(pts, fill=255)
    d.ellipse((cx - r_hole, cy - r_hole, cx + r_hole, cy + r_hole), fill=0)
    return m


def bubble_mask(cx, cy, r):
    m = Image.new("L", (S, S), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle((cx - r, cy - r * 0.78, cx + r, cy + r * 0.62), radius=r * 0.42, fill=255)
    d.polygon([(cx - r * 0.45, cy + r * 0.5), (cx - r * 0.62, cy + r * 1.0), (cx - r * 0.05, cy + r * 0.6)],
              fill=255)
    for dx in (-0.45, 0, 0.45):                   # three "typing" dots cut out of the bubble
        rr = r * 0.13
        x, y = cx + dx * r, cy - r * 0.08
        d.ellipse((x - rr, y - rr, x + rr, y + rr), fill=0)
    return m


def master():
    img = tile()
    c = S // 2
    shapes = ImageChops.lighter(gear_mask(c, c, 400, 318, 228), bubble_mask(c, c + 6, 150))
    # soft shadow under the white shapes
    shadow = shapes.filter(ImageFilter.GaussianBlur(14)).point(lambda v: v * 0.35)
    img.paste((0, 20, 40), (0, 14), shadow)
    img.paste((255, 255, 255), (0, 0), shapes)
    return img


def simple(size):
    """16/20/24 px: gear only, thicker, so it stays readable."""
    img = tile()
    c = S // 2
    img.paste((255, 255, 255), (0, 0), gear_mask(c, c, 430, 330, 170, teeth=8))
    return img.resize((size, size), Image.LANCZOS)


def main():
    big = master()
    sizes = [16, 20, 24, 32, 40, 48, 64, 96, 128, 256]
    frames = [simple(s) if s <= 24 else big.resize((s, s), Image.LANCZOS) for s in sizes]
    ico = os.path.join(HERE, "app.ico")
    frames[-1].save(ico, format="ICO", sizes=[(s, s) for s in sizes], append_images=frames[:-1])
    big.resize((32, 32), Image.LANCZOS).save(os.path.join(HERE, "app.png"))     # header logo
    big.resize((256, 256), Image.LANCZOS).save(os.path.join(HERE, "tools", "icon_preview_256.png"))
    print("wrote", ico)


if __name__ == "__main__":
    main()
