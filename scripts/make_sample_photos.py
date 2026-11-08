"""Generates the illustrative "buyer photos" attached to demo helpdesk tickets (data/attachments/).

They are drawn, not photographed, and carry an "illustrative sample" label. Run once:
    pip install pillow && python scripts/make_sample_photos.py
"""

import math
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT = Path(__file__).resolve().parent.parent / "data" / "attachments"
W, H = 900, 640
random.seed(7)


def font(size: int):
    for name in ("arialbd.ttf", "DejaVuSans-Bold.ttf", "Arial Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def wood_table() -> Image.Image:
    img = Image.new("RGB", (W, H), (150, 104, 66))
    d = ImageDraw.Draw(img)
    for y in range(0, H, 3):
        shade = int(18 * math.sin(y / 23.0) + random.randint(-6, 6))
        d.line([(0, y), (W, y + random.randint(-4, 4))], fill=(150 + shade, 104 + shade // 2, 66 + shade // 3), width=3)
    return img.filter(ImageFilter.GaussianBlur(1.2))


def label(img: Image.Image, text: str = "illustrative sample - not a real photo") -> None:
    d = ImageDraw.Draw(img)
    f = font(16)
    w = d.textlength(text, font=f)
    d.rectangle([W - w - 24, H - 34, W - 8, H - 8], fill=(0, 0, 0))
    d.text((W - w - 16, H - 30), text, font=f, fill=(255, 255, 255))


def crack(d: ImageDraw.ImageDraw, start, angle, length, width=4, branches=2):
    x, y = start
    pts = [(x, y)]
    for _ in range(int(length / 14)):
        angle += random.uniform(-0.45, 0.45)
        x += 14 * math.cos(angle)
        y += 14 * math.sin(angle)
        pts.append((x, y))
    d.line(pts, fill=(235, 232, 225), width=width, joint="curve")
    d.line([(px + 2, py + 2) for px, py in pts], fill=(25, 25, 28), width=max(1, width - 2))
    for _ in range(branches):
        i = random.randint(1, len(pts) - 1)
        crack(d, pts[i], angle + random.choice([-1, 1]) * random.uniform(0.6, 1.1), length * 0.35, max(2, width - 2), 0)


def masked_crack(img: Image.Image, shape, start, angle, length, width=5, branches=2):
    """Draw a crack that stays inside `shape` (a callable drawing the clip region on a mask)."""
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    crack(ImageDraw.Draw(layer), start, angle, length, width, branches)
    mask = Image.new("L", img.size, 0)
    shape(ImageDraw.Draw(mask))
    alpha = Image.composite(layer.getchannel("A"), Image.new("L", img.size, 0), mask)
    layer.putalpha(alpha)
    img.paste(layer, (0, 0), layer)


def bowl_top(d, cx, cy, r, glaze=(52, 54, 58)):
    d.ellipse([cx - r - 10, cy - r * 0.78 + 14, cx + r + 10, cy + r * 0.78 + 24], fill=(90, 60, 38))  # shadow
    d.ellipse([cx - r, cy - r * 0.75, cx + r, cy + r * 0.75], fill=glaze)
    d.ellipse([cx - r + 14, cy - r * 0.75 + 11, cx + r - 14, cy + r * 0.75 - 11], fill=(38, 40, 44))
    d.arc([cx - r, cy - r * 0.75, cx + r, cy + r * 0.75], 200, 330, fill=(120, 122, 128), width=4)


def bowls_crack_1():
    img = wood_table()
    d = ImageDraw.Draw(img)
    bowl_top(d, 380, 320, 230)
    masked_crack(img, lambda m: m.ellipse([150, 147.5, 610, 492.5], fill=255), (175, 250), 0.35, 420, width=6)
    d = ImageDraw.Draw(img)
    # broken-off shard lying next to the bowl
    d.polygon([(680, 470), (790, 430), (820, 500), (720, 540)], fill=(52, 54, 58), outline=(235, 232, 225))
    d.line([(680, 470), (720, 540)], fill=(205, 190, 170), width=5)
    label(img)
    img.save(OUT / "bowls-crack-1.png")


def bowls_crack_2():
    img = wood_table()
    d = ImageDraw.Draw(img)
    # side view: two bowls, the small one cracked from the rim down
    for cx, w, h, cracked in ((300, 300, 170, True), (650, 220, 125, False)):
        rim = 450 - h
        d.ellipse([cx - w / 2 - 10, rim + h - 22, cx + w / 2 + 10, rim + h + 18], fill=(95, 64, 40))  # shadow
        body = [cx - w / 2, rim - h, cx + w / 2, rim + h]
        d.chord(body, 0, 180, fill=(55, 57, 62))  # lower half-ellipse = bowl body
        d.ellipse([cx - w / 2, rim - 22, cx + w / 2, rim + 22], fill=(40, 42, 46), outline=(120, 122, 128), width=3)
        if cracked:
            masked_crack(img, lambda m, b=body: m.chord(b, 0, 180, fill=255), (cx - 20, rim + 4), 1.45, 200, width=6, branches=1)
            d = ImageDraw.Draw(img)
            d.polygon([(cx + 40, rim - 20), (cx + 82, rim - 19), (cx + 62, rim + 26)], fill=(150, 104, 66))  # chip out of rim
    label(img)
    img.save(OUT / "bowls-crack-2.png")


def box_corner():
    img = Image.new("RGB", (W, H), (205, 205, 200))
    d = ImageDraw.Draw(img)
    d.polygon([(130, 200), (640, 170), (770, 260), (760, 560), (150, 590)], fill=(196, 160, 110))  # box body
    d.polygon([(130, 200), (640, 170), (770, 260), (260, 290)], fill=(214, 180, 128))  # open top
    d.polygon([(640, 170), (770, 260), (700, 330), (600, 250)], fill=(160, 124, 80))  # crushed corner
    for _ in range(14):
        x, y = random.randint(600, 760), random.randint(180, 330)
        d.line([(x, y), (x + random.randint(-40, 40), y + random.randint(-30, 30))], fill=(120, 90, 55), width=3)
    # crumpled paper inside, no dividers
    for _ in range(9):
        x, y = random.randint(260, 600), random.randint(205, 280)
        d.ellipse([x, y, x + random.randint(50, 90), y + random.randint(25, 40)], fill=(238, 236, 228), outline=(200, 198, 190))
    d.text((300, 420), "FRAGILE", font=font(64), fill=(170, 30, 30))
    d.text((300, 495), "UPS  1Z999AA10123456784", font=font(26), fill=(40, 40, 40))
    label(img)
    img.save(OUT / "box-corner.png")


def mugs_colour():
    img = wood_table()
    d = ImageDraw.Draw(img)
    glazes = [(96, 160, 196), (88, 152, 190), (20, 42, 92), (100, 166, 200)]  # third mug much darker
    for i, glaze in enumerate(glazes):
        x = 90 + i * 200
        d.ellipse([x - 10, 470, x + 170, 510], fill=(100, 68, 42))
        d.rounded_rectangle([x, 220, x + 150, 490], radius=26, fill=glaze)
        d.ellipse([x, 205, x + 150, 240], fill=tuple(max(0, c - 30) for c in glaze), outline=(230, 230, 225), width=3)
        d.arc([x + 130, 280, x + 200, 400], 270, 90, fill=glaze, width=18)
        for _ in range(6):  # glaze drips
            dx = random.randint(x + 10, x + 140)
            d.line([(dx, 240), (dx, 240 + random.randint(30, 90))], fill=tuple(min(255, c + 40) for c in glaze), width=6)
    label(img)
    img.save(OUT / "mugs-colour.png")


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    bowls_crack_1()
    bowls_crack_2()
    box_corner()
    mugs_colour()
    print("wrote", sorted(p.name for p in OUT.glob("*.png")))
