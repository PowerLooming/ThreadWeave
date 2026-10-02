"""Generate the GitHub social preview / og:image (1280x630).

GitHub exposes no API for the social preview image, so this is uploaded by
hand once (repo Settings -> Social preview). Regenerate with:
    uv run python scripts/make_social_preview.py
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1280, 630
BG = (13, 16, 28)
TEXT = (232, 236, 245)
MUTED = (150, 160, 184)
ACCENT = (129, 140, 248)
GREEN = (52, 211, 153)

MARGIN = 76
MAX_X = W - MARGIN

SEGOE = "C:/Windows/Fonts/segoeui.ttf"
SEGOE_BOLD = "C:/Windows/Fonts/segoeuib.ttf"
SEGOE_LIGHT = "C:/Windows/Fonts/segoeuil.ttf"


def font(path: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(path, size)
    except OSError:  # non-Windows machine: fall back to the default face
        return ImageFont.load_default(size)


def main() -> None:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    f_mark = font(SEGOE_BOLD, 32)
    f_big = font(SEGOE_BOLD, 68)
    f_sub = font(SEGOE, 29)
    f_chip = font(SEGOE, 18)
    f_tiny = font(SEGOE_LIGHT, 19)

    def text(x, y, s, f, colour):
        """Draw and assert the line stays inside the card."""
        assert x + d.textlength(s, font=f) <= MAX_X, f"overflows: {s}"
        d.text((x, y), s, font=f, fill=colour)

    # Accent edge
    d.rectangle([0, 0, 7, H], fill=ACCENT)

    # Wordmark, with the palace glyph as a small accent block
    d.rounded_rectangle([MARGIN, 74, MARGIN + 30, 104], radius=8, fill=ACCENT)
    text(MARGIN + 46, 74, "ThreadWeave", f_mark, TEXT)

    # Headline
    text(MARGIN, 176, "Every thread,", f_big, TEXT)
    text(MARGIN, 260, "woven into memory.", f_big, ACCENT)

    # Subline
    text(MARGIN, 386, "Self-hosted organizational memory system.",
         f_sub, TEXT)
    text(MARGIN, 428,
         "Decisions, answers and runbooks captured from the tools you",
         f_sub, MUTED)
    text(MARGIN, 466, "already use, filed into one searchable palace.",
         f_sub, MUTED)

    # Palace chips: wing / room
    chips = [
        ("engineering/database", (99, 102, 241)),
        ("sales/pricing", (56, 189, 248)),
        ("support/escalation", (52, 211, 153)),
        ("operations/vendors", (251, 191, 36)),
    ]
    x, y = MARGIN, 540
    for label, colour in chips:
        w = d.textlength(label, font=f_chip) + 32
        assert x + w <= MAX_X, f"chip overflows: {label}"
        d.rounded_rectangle([x, y, x + w, y + 38], radius=19,
                            outline=colour, width=2)
        d.text((x + 16, y + 9), label, font=f_chip, fill=TEXT)
        x += w + 14

    text(MARGIN, H - 44, "MIT licensed  ·  github.com/PowerLooming/ThreadWeave",
         f_tiny, MUTED)

    out = Path(__file__).resolve().parent.parent / "assets" / "social-preview.png"
    img.save(out, "PNG")
    print(f"wrote {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
