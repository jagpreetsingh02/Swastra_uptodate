#!/usr/bin/env python3
"""Generate the handwritten prescription fixture. `python scripts/make_handwritten_fixture.py`

WHY A SYNTHETIC ONE. Every prescription fixture in this repo until now was rendered in Courier
and photographed — printed text, which Tesseract reads well. That is exactly the wrong thing to
test a handwriting lane against: it passes for the wrong reason. A real photograph of a real
prescription cannot be committed here either, because the repo's rule is synthetic data only.

So this draws one, and the drawing is where the difficulty lives. Each property below breaks a
printed-text OCR engine in a different way, and each is applied PER CHARACTER rather than per
line, because that is how handwriting actually varies:

    baseline drift          a written line sags and recovers; it is not a line
    per-glyph rotation      no two letters share a slant
    variable pen pressure   rendered as ink darkness — this is what makes a global threshold
                            fail and a local one necessary
    irregular advance       letter spacing is never uniform
    page skew               the sheet was not square to the camera
    illumination gradient   one side of the page is nearer the light
    sensor noise + blur     a phone camera, indoors

The content is deliberately the shorthand a pharmacist reads without pausing — `Tab`, `12mg`,
`OD`, `BD`, `TDS`, `SOS`, `bf` — because that is what `entities.py` has to survive.

The output is committed; this script only needs to run when the fixture changes, and only on a
machine with one of the handwriting faces below.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT = Path(__file__).resolve().parents[1] / "data" / "fixtures" / "documents"
NAME = "prescription_handwritten"
random.seed(20260904)

#: Real handwriting faces, best first. A "handwritten" fixture rendered in Arial tests nothing
#: — Tesseract reads Arial perfectly, which is precisely the result this must not produce.
HAND_FONTS = (
    "/System/Library/Fonts/Supplemental/Bradley Hand Bold.ttf",
    "/System/Library/Fonts/Supplemental/Chalkduster.ttf",
    "/System/Library/Fonts/Supplemental/Comic Sans MS.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf",
)

LINES = [
    "Dr S. Menon   MBBS MD",
    "City Clinic, Chennai",
    "Date 04/09/2026",
    "",
    "Rx",
    "Tab Ivermectin-12mg  OD  x 3d",
    "Tab Augmentin 625mg  BD  x 5d",
    "Cap Omeprazole 20mg  OD  bf",
    "Syrup Paracetamol 250mg  SOS",
    "Tab Metformin 500mg  TDS  after food",
    "",
    "Review after 5 days",
]

#: What is actually written, for the eval harness to score against. The point of keeping this
#: beside the image is that "what the paper says" and "what the pipeline read" are two
#: different columns, and only one of them is ground truth.
TRUTH = {
    "medications": [
        {"name": "Ivermectin", "dose": "12mg", "frequency": "OD", "duration": "3d"},
        {"name": "Augmentin", "dose": "625mg", "frequency": "BD", "duration": "5d"},
        {"name": "Omeprazole", "dose": "20mg", "frequency": "OD", "timing": "bf"},
        {"name": "Paracetamol", "dose": "250mg", "frequency": "SOS"},
        {"name": "Metformin", "dose": "500mg", "frequency": "TDS", "timing": "after food"},
    ],
    "investigations": [],
    "diagnoses": [],
    "written_lines": [line for line in LINES if line],
    "note": (
        "Synthetic handwriting: real handwriting face, per-character rotation and jitter, "
        "baseline drift, variable pen pressure, page skew, illumination gradient, sensor "
        "noise. No real doctor, no real patient, no real registration number."
    ),
}


def _font(size: int) -> ImageFont.FreeTypeFont | None:
    for candidate in HAND_FONTS:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    return None


def main() -> int:
    font = _font(38)
    if font is None:
        print(
            "SKIPPED: no handwriting font found. Install one of:\n  "
            + "\n  ".join(HAND_FONTS)
            + "\nThe committed fixture is unchanged."
        )
        return 0

    # 2480x3508 is A4 at 300 DPI — a realistic phone capture of a full page, and comfortably
    # above MIN_LONG_EDGE so this fixture also proves a large page is not called "too small".
    width, height = 2480, 3508
    page = Image.new("L", (width, height), 250)

    y = 320
    for line in LINES:
        if not line:
            y += 92
            continue
        x = 240.0
        drift = 0.0
        for character in line:
            box = max(int(font.size * 2.0), 12)
            glyph = Image.new("L", (box, box), 0)
            ImageDraw.Draw(glyph).text(
                (box // 4, box // 4), character, font=font, fill=random.randint(150, 240)
            )
            glyph = glyph.rotate(random.uniform(-7, 7), resample=Image.BICUBIC, fillcolor=0)
            drift = max(min(drift + random.uniform(-1.0, 1.0), 8.0), -8.0)
            # Pasted through itself as a mask so the strokes darken the paper rather than
            # stamping an opaque box over it — a rectangle of background around every letter
            # would hand the segmenter a grid no real page has.
            page.paste(
                Image.new("L", glyph.size, 0),
                (int(x) - box // 4, int(y + drift) - box // 4),
                glyph,
            )
            advance = font.getlength(character) if character != " " else font.size * 0.42
            x += advance * random.uniform(0.9, 1.04)
        y += 118

    page = page.rotate(-1.8, resample=Image.BICUBIC, fillcolor=250, expand=False)
    page = page.filter(ImageFilter.GaussianBlur(radius=0.7))
    shade = Image.linear_gradient("L").rotate(38, resample=Image.BICUBIC, fillcolor=128)
    page = Image.blend(page, shade.resize((width, height)), 0.11)
    pixels = page.load()
    assert pixels is not None
    for _ in range(int(width * height * 0.010)):
        px, py = random.randrange(width), random.randrange(height)
        pixels[px, py] = max(0, min(255, pixels[px, py] + random.randint(-40, 40)))

    page.convert("RGB").save(OUT / f"{NAME}.jpg", quality=84)
    (OUT / f"{NAME}.truth.json").write_text(json.dumps(TRUTH, indent=2) + "\n")
    print(f"wrote {NAME}.jpg ({width}x{height}) and {NAME}.truth.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
