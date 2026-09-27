"""
make_icons.py  -  Build all app icons from the brand logo (wifi-Photoroom.png).

    python make_icons.py

Removes the white background ("white to alpha", keeping soft anti-aliased
edges), cuts out the round signal mark, and writes into ./brand:
    logo_mark.png        transparent mark, 256 px   (dashboard header, tray)
    logo_full.png        transparent full wordmark  (README / about)
    favicon.ico          16/32/48/64/256 px         (browser tab, Windows notifications)
    favicon-32.png       32 px
    icon-192.png         192 px on a white rounded tile (phone home screen)
    icon-512.png         512 px on a white rounded tile
    apple-touch-icon.png 180 px (iOS)
"""

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
SRC = HERE / "wifi-Photoroom.png"
OUT = HERE / "brand"


def white_to_alpha(img):
    """Treat white as transparent; recover the original colour of semi-transparent edge pixels."""
    a = np.asarray(img.convert("RGBA")).astype(np.float64) / 255.0
    rgb, alpha0 = a[..., :3], a[..., 3]
    alpha = np.clip((1.0 - rgb).max(axis=2) * 1.08, 0, 1)            # distance from white
    safe = np.where(alpha > 1e-3, alpha, 1)[..., None]
    fg = np.clip((rgb - (1 - alpha[..., None])) / safe, 0, 1)        # un-premultiply against white
    out = np.dstack([fg, alpha * alpha0])
    return Image.fromarray((out * 255).round().astype(np.uint8), "RGBA")


def trim(img, pad=0.04, square=True):
    box = img.getchannel("A").point(lambda v: 255 if v > 24 else 0).getbbox()
    img = img.crop(box)
    w, h = img.size
    p = int(max(w, h) * pad)
    cw, ch = (max(w, h) + 2 * p,) * 2 if square else (w + 2 * p, h + 2 * p)
    canvas = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    canvas.paste(img, ((cw - w) // 2, (ch - h) // 2), img)
    return canvas


def tile(mark, size, radius=0.22, bg=(255, 255, 255, 255), inset=0.14):
    """Mark on a rounded white tile (home-screen icons need an opaque background)."""
    t = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(t).rounded_rectangle((0, 0, size - 1, size - 1), radius=int(size * radius), fill=bg)
    m = mark.resize((int(size * (1 - 2 * inset)),) * 2, Image.LANCZOS)
    t.alpha_composite(m, (int(size * inset), int(size * inset)))
    return t


def main():
    OUT.mkdir(exist_ok=True)
    src = Image.open(SRC)
    clear = white_to_alpha(src)
    w, h = clear.size
    # the round mark is the left part of the logo, before the "WiFi Sense" wordmark
    mark = trim(clear.crop((int(w * 0.14), int(h * 0.28), int(w * 0.36), int(h * 0.71))))
    full = trim(clear, pad=0.02, square=False)

    mark.resize((256, 256), Image.LANCZOS).save(OUT / "logo_mark.png")
    full.save(OUT / "logo_full.png")
    mark.resize((32, 32), Image.LANCZOS).save(OUT / "favicon-32.png")
    mark.save(OUT / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (256, 256)])
    tile(mark, 192).save(OUT / "icon-192.png")
    tile(mark, 512).save(OUT / "icon-512.png")
    tile(mark, 180, radius=0).convert("RGB").save(OUT / "apple-touch-icon.png")
    for p in sorted(OUT.iterdir()):
        print(f"{p.name:22s} {Image.open(p).size if p.suffix != '.ico' else 'multi-size'}")


if __name__ == "__main__":
    main()
