"""
generate_captchas.py
────────────────────
Generates captcha .png images for the Sky.py anti-raid system.
Each image is saved as  <CODE>.png  inside the Captcha folder.
The filename (without the extension) is the answer the user must type.

Sky.py imports and calls regen() automatically on startup.
You can also run this script directly at any time to manually refresh:

    python3 generate_captchas.py

Requires Pillow:
    pip install Pillow
"""

import argparse
import os
import random
import sys

from PIL import Image, ImageDraw, ImageFilter, ImageFont

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

# Folder to save images into (always relative to THIS file's location,
# so it works regardless of where Python is invoked from)
CAPTCHA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Captcha")
COUNT       = 500
CODE_LENGTH = 6
IMG_WIDTH   = 340
IMG_HEIGHT  = 100

# No ambiguous chars (0/O, 1/I/l) so users can't fail unfairly
CHARSET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeMonoBold.ttf",
    "/Library/Fonts/Courier New Bold.ttf",   # macOS
    "C:/Windows/Fonts/courbd.ttf",           # Windows
]

FONT_SIZE     = 44
CHAR_ROTATION = 30    # ± degrees per character
CHAR_JITTER_Y = 8     # ± pixels vertical shift per character
BLUR_RADIUS   = 0.6

BG_COLOR   = (18, 18, 28)
GRID_COLOR = (30, 30, 50)
GRID_STEP  = 20
NOISE_DOTS = 420
INTER_LINES = 6

CHAR_COLORS = [
    (255,  80, 100),
    ( 80, 200, 255),
    (180, 255,  80),
    (255, 200,  50),
    (200, 100, 255),
    (255, 140,  40),
    ( 80, 255, 180),
    (255, 255, 100),
]

# ─────────────────────────────────────────────────────────────────────────────
# INTERNALS
# ─────────────────────────────────────────────────────────────────────────────

def _load_font(size: int):
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    print("[Captcha] Warning: no TrueType font found, falling back to Pillow default.")
    print("          Install DejaVu for best results: sudo apt install fonts-dejavu-core")
    return ImageFont.load_default()


def _random_code(length: int) -> str:
    return "".join(random.choices(CHARSET, k=length))


def _render(code: str, font) -> Image.Image:
    img  = Image.new("RGB", (IMG_WIDTH, IMG_HEIGHT), BG_COLOR)
    draw = ImageDraw.Draw(img)

    # Grid lines
    for x in range(0, IMG_WIDTH, GRID_STEP):
        draw.line([(x, 0), (x, IMG_HEIGHT)], fill=GRID_COLOR, width=1)
    for y in range(0, IMG_HEIGHT, GRID_STEP):
        draw.line([(0, y), (IMG_WIDTH, y)], fill=GRID_COLOR, width=1)

    # Noise dots
    for _ in range(NOISE_DOTS):
        x = random.randint(0, IMG_WIDTH  - 1)
        y = random.randint(0, IMG_HEIGHT - 1)
        c = random.randint(40, 80)
        draw.point((x, y), fill=(c, c, c + 20))

    # Interference lines
    for _ in range(INTER_LINES):
        col = (random.randint(60, 130), random.randint(60, 130), random.randint(80, 160))
        draw.line(
            [(random.randint(0, IMG_WIDTH), random.randint(0, IMG_HEIGHT)),
             (random.randint(0, IMG_WIDTH), random.randint(0, IMG_HEIGHT))],
            fill=col, width=2,
        )

    # Characters — each on its own tile so it can be individually rotated
    n       = len(code)
    slot_w  = IMG_WIDTH // (n + 1)
    start_x = slot_w // 2
    pad     = FONT_SIZE + 20

    for i, ch in enumerate(code):
        tile  = Image.new("RGBA", (pad, pad), (0, 0, 0, 0))
        tdraw = ImageDraw.Draw(tile)
        tdraw.text((10, 5), ch, font=font, fill=CHAR_COLORS[i % len(CHAR_COLORS)])
        tile  = tile.rotate(
            random.uniform(-CHAR_ROTATION, CHAR_ROTATION),
            expand=True,
            resample=Image.BICUBIC,
        )
        cx = start_x + i * slot_w - tile.width  // 2
        cy = (IMG_HEIGHT - tile.height) // 2 + random.randint(-CHAR_JITTER_Y, CHAR_JITTER_Y)
        img.paste(tile, (cx, cy), tile)

    if BLUR_RADIUS > 0:
        img = img.filter(ImageFilter.GaussianBlur(radius=BLUR_RADIUS))

    return img


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC API  —  imported and called by Sky.py
# ─────────────────────────────────────────────────────────────────────────────

def regen(verbose: bool = True) -> int:
    """
    Delete every existing .png in CAPTCHA_DIR, then generate COUNT fresh ones.
    Returns the number of images created.
    Called automatically by Sky.py on startup, and by $regencaptchas.
    """
    os.makedirs(CAPTCHA_DIR, exist_ok=True)

    # Wipe existing images
    removed = 0
    for fname in os.listdir(CAPTCHA_DIR):
        if fname.lower().endswith(".png"):
            os.remove(os.path.join(CAPTCHA_DIR, fname))
            removed += 1
    if verbose and removed:
        print(f"[Captcha] Cleared {removed} old image(s).")

    font       = _load_font(FONT_SIZE)
    seen: set  = set()
    count      = 0
    max_unique = len(CHARSET) ** CODE_LENGTH
    target     = min(COUNT, max_unique)

    if verbose:
        print(f"[Captcha] Generating {target} images (length={CODE_LENGTH}) → {CAPTCHA_DIR}")

    while count < target:
        code = _random_code(CODE_LENGTH)
        if code in seen:
            continue
        seen.add(code)
        _render(code, font).save(os.path.join(CAPTCHA_DIR, f"{code}.png"))
        count += 1

        if verbose:
            step = max(1, target // 10)
            if count % step == 0 or count == target:
                filled = count * 20 // target
                bar    = "█" * filled + "░" * (20 - filled)
                print(f"  [{bar}] {count}/{target}  ({count / target * 100:.0f}%)",
                      end="\r", flush=True)

    if verbose:
        print()
        print(f"[Captcha] ✅ Done — {count} captchas ready.")

    return count


# ─────────────────────────────────────────────────────────────────────────────
# CLI  —  run directly to manually regenerate at any time
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Regenerate all captcha images for Sky.py."
    )
    parser.add_argument(
        "--count", "-n", type=int, default=COUNT,
        help=f"Number of images to generate (default: {COUNT})",
    )
    parser.add_argument(
        "--length", "-l", type=int, default=CODE_LENGTH,
        help=f"Characters per code (default: {CODE_LENGTH})",
    )
    args = parser.parse_args()

    # Allow CLI to override the module-level defaults
    COUNT       = args.count
    CODE_LENGTH = args.length

    regen(verbose=True)
