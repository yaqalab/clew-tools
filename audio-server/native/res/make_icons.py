"""
Composes the native server's icons (res/icons/*.ico) from Clew's own brand files
(packages/kit/brand, the source of truth; nothing is drawn here):

- app.ico (the program, its window, the Start menu, the installer): the "c" mark on the tile's cream,
  at every size, which stays visible on a light or a dark background. (The full labyrinth tile was
  tried at every size on 2026-10-05 and set aside: below 64 px its lines cannot be read.)
- tray-{on,off,busy}[-dark].ico: the "c" mark, ink for a light taskbar and cream for a dark one
  (-dark). Running: the mark as it is. Stopped: faded and grey. Converting: the mark with a small
  badge in the accent colour.

    python tools/audio-server/native/res/make_icons.py      (needs Pillow; only to regenerate)
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

HERE = Path(__file__).resolve().parent
BRAND = HERE.parents[3] / "packages" / "kit" / "brand"
OUT = HERE / "icons"

CREAM = (0xF7, 0xF2, 0xE9)
INK = (0x16, 0x32, 0x3F)
ACCENT = {"light": (0x16, 0x53, 0x66), "dark": (0x8F, 0xBD, 0xD0)}  # --ar-accent, theme.css
APP_SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]
TRAY_SIZES = [16, 20, 24, 32, 40, 48, 64]


def mark(variant):
    ico = Image.open(BRAND / ("favicon.ico" if variant == "light" else "favicon-dark.ico"))
    return ico.ico.getimage((256, 256)).convert("RGBA")


def rounded(img, radius_share=0.2):
    n = img.size[0] * 4
    mask = Image.new("L", (n, n), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, n - 1, n - 1), radius=int(n * radius_share), fill=255)
    out = img.convert("RGBA")
    out.putalpha(mask.resize(img.size, Image.LANCZOS))
    return out


def app_icon(size):
    big = 256
    base = Image.new("RGBA", (big, big), CREAM + (255,))
    m = mark("light").resize((int(big * 0.84),) * 2, Image.LANCZOS)
    base.alpha_composite(m, ((big - m.size[0]) // 2, (big - m.size[1]) // 2))
    edge = ImageDraw.Draw(base)
    edge.rounded_rectangle((2, 2, big - 3, big - 3), radius=int(big * 0.2), outline=INK + (255,), width=10)
    return rounded(base).resize((size, size), Image.LANCZOS)


def tray_icon(size, variant, state):
    m = mark(variant)
    if state == "off":
        grey = ImageOps.grayscale(m.convert("RGB")).convert("RGBA")
        grey.putalpha(m.getchannel("A").point(lambda a: a * 45 // 100))
        m = grey
    elif state == "busy":
        n = m.size[0]
        r = int(n * 0.22)
        cx, cy = n - r - 2, n - r - 2
        # A gap around the badge so it reads apart from the mark.
        hole = Image.new("L", m.size, 255)
        ImageDraw.Draw(hole).ellipse((cx - r - 18, cy - r - 18, cx + r + 18, cy + r + 18), fill=0)
        alpha = Image.composite(m.getchannel("A"), Image.new("L", m.size, 0), hole)
        m.putalpha(alpha)
        ImageDraw.Draw(m).ellipse((cx - r, cy - r, cx + r, cy + r), fill=ACCENT[variant] + (255,))
    return m.resize((size, size), Image.LANCZOS)


def save(name, images):
    images = sorted(images, key=lambda i: i.size[0], reverse=True)
    images[0].save(OUT / name, format="ICO", sizes=[i.size for i in images], append_images=images[1:])
    print(name)


def main():
    OUT.mkdir(exist_ok=True)
    save("app.ico", [app_icon(s) for s in APP_SIZES])
    for variant in ("light", "dark"):
        for state in ("on", "off", "busy"):
            suffix = "" if variant == "light" else "-dark"
            save(f"tray-{state}{suffix}.ico", [tray_icon(s, variant, state) for s in TRAY_SIZES])


if __name__ == "__main__":
    main()
