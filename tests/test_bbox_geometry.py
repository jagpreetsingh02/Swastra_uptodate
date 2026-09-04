"""Bounding boxes must land WHERE THE TEXT IS. Position, not presence.

⛔ THREE BUGS, ONE CLASS, NONE CAUGHT BY AN EXISTING TEST.

All three were the same mistake wearing different clothes — measuring in one coordinate system
and drawing in another — and not one of them would fail a test that asserts "a bbox exists" or
"a crop rendered":

  1. `_parse_tsv` derived page dimensions from the EXTENT OF THE DETECTED TEXT
     (`max(left+width)`, `max(top+height)`) instead of the image size. On a prescription whose
     lower half is blank that gave 1168x1072 for an image that is 1797x2470, so every
     normalised coordinate was inflated by a different factor on each axis.

  2. `render_page_png` served the RAW UPLOAD for images while OCR read the PREPARED page.
     Deskew runs with `expand=True` and changes the canvas size, so the two coordinate spaces
     disagreed by construction.

  3. `SourceCrop` positioned the page with `margin-top: -y%`. Percentage margins resolve
     against the container's WIDTH, not its height, so every crop was offset vertically and
     landed on blank paper.

Every one produced a box that EXISTED, had plausible numbers in [0,1], and pointed at nothing.
`render.py`'s own header states the standard: "a box drawn in the wrong place is worse than no
box, because it tells a physician the system read a line it did not read."

So these tests assert geometry against ground truth: text is drawn at a KNOWN pixel location,
and the resulting bbox must come back within a tight tolerance of it.
"""

from __future__ import annotations

import io
import shutil
from pathlib import Path

import pytest

pytest.importorskip("PIL")
pytest.importorskip("numpy")

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from app.modules.documents import imaging  # noqa: E402

HAS_TESSERACT = shutil.which("tesseract") is not None
needs_tesseract = pytest.mark.skipif(not HAS_TESSERACT, reason="tesseract not installed")

#: How far a returned box may sit from the truth, as a fraction of the page. 3% is roughly one
#: line height on an A4 page — tight enough that any of the three bugs above blows through it
#: (they were off by 35-80%), loose enough to tolerate the padding Tesseract puts around glyphs.
TOLERANCE = 0.03


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """A real TrueType face if one is available; Pillow's bitmap default otherwise.

    The default font is tiny and Tesseract reads it poorly, so tests that need reliable
    recognition skip when no scalable font exists rather than asserting against noise.
    """
    for candidate in (
        "/System/Library/Fonts/Supplemental/Courier New.ttf",
        "/System/Library/Fonts/Menlo.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    ):
        if Path(candidate).exists():
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _page_with_word_at(
    word: str, *, at: tuple[int, int], size: tuple[int, int] = (1700, 2400)
) -> tuple[bytes, tuple[float, float]]:
    """A blank page with one word drawn at a known pixel position.

    Returns the PNG bytes and the word's NORMALISED centre — the ground truth the extracted
    bbox is compared against.
    """
    page = Image.new("L", size, color=255)
    draw = ImageDraw.Draw(page)
    font = _font(64)
    draw.text(at, word, fill=0, font=font)

    left, top, right, bottom = draw.textbbox(at, word, font=font)
    centre = ((left + right) / 2 / size[0], (top + bottom) / 2 / size[1])

    buffer = io.BytesIO()
    page.save(buffer, format="PNG")
    return buffer.getvalue(), centre


def _boxes(data: bytes, filename: str):
    from app.modules.documents.pipeline import read_and_extract

    # PINNED TO TESSERACT, DELIBERATELY. This file tests TESSERACT's own bbox geometry against
    # synthetic machine-printed fixtures — the three historical bugs named in the module
    # docstring are all Tesseract/render/crop coordinate bugs, and predate the handwriting lane
    # entirely.
    #
    # Leaving this on default dispatch broke the moment a real handwriting recognizer became
    # available: `backend_for()` now tries `handwriting` first for any image, and
    # `khedim/Medical-Prescription-OCR` — verified in ADR-0017 to answer fluently and
    # confidently for ANY input, including a blank page — never raises `UpstreamUnavailable`
    # for these clean printed-word fixtures. It "succeeds" with unrelated text, so the
    # fallback to Tesseract this file actually wants to exercise never fires. Naming the
    # engine is what these tests were always implicitly asserting; it is now explicit.
    ocr, _confident, _needs = read_and_extract(
        data, filename=filename, media_type="image/png", backend_name="tesseract"
    )
    return ocr


# ------------------------------------------------------- the page dimensions


@needs_tesseract
def test_page_dimensions_are_the_image_not_the_text_extent() -> None:
    """Bug 1, pinned directly.

    The word sits in the TOP-LEFT QUARTER on purpose. If page size were derived from the text
    extent it would come back as roughly the word's own bounding box — a few hundred pixels —
    rather than the full page, and that is precisely the shape of the original bug.
    """
    data, _centre = _page_with_word_at("METFORMIN", at=(200, 300), size=(1700, 2400))
    prepared = imaging.prepare(data, filename="known.png")
    ocr = _boxes(data, "known.png")

    page = ocr.pages[0]
    assert (page.width, page.height) == (prepared.width, prepared.height), (
        f"OCR normalised against {page.width}x{page.height} but the prepared image is "
        f"{prepared.width}x{prepared.height}. Coordinates measured against one and drawn on "
        "the other is the bug this test exists for."
    )
    # And, independently: the page must not have collapsed to the text's own extent.
    assert page.height > 1500, (
        f"page height {page.height} looks like the text extent, not the page"
    )


@needs_tesseract
def test_a_word_in_the_top_left_reports_a_top_left_box() -> None:
    """The simplest possible position assertion, and it would have failed on bug 1."""
    data, centre = _page_with_word_at("METFORMIN", at=(180, 260), size=(1700, 2400))
    ocr = _boxes(data, "topleft.png")

    blocks = [b for b in ocr.pages[0].blocks if "METFORMIN" in b.text.upper()]
    assert blocks, f"the word was not read at all: {ocr.text!r}"
    box = blocks[0].bbox

    found = (box.x + box.width / 2, box.y + box.height / 2)
    assert abs(found[0] - centre[0]) < TOLERANCE, (
        f"x centre {found[0]:.3f} vs truth {centre[0]:.3f}"
    )
    assert abs(found[1] - centre[1]) < TOLERANCE, (
        f"y centre {found[1]:.3f} vs truth {centre[1]:.3f}"
    )


@needs_tesseract
@pytest.mark.parametrize(
    ("label", "at"),
    [
        ("top-left", (180, 240)),
        ("bottom-right", (900, 2050)),
        ("middle", (620, 1150)),
    ],
)
def test_boxes_land_on_the_word_wherever_it_is(label: str, at: tuple[int, int]) -> None:
    """Bug 1 scaled each axis by a DIFFERENT wrong factor, so a single position could pass by
    luck. Three positions spread across the page cannot."""
    data, centre = _page_with_word_at("AMLODIPINE", at=at, size=(1700, 2400))
    ocr = _boxes(data, f"{label}.png")

    blocks = [b for b in ocr.pages[0].blocks if "AMLODIPINE" in b.text.upper()]
    assert blocks, f"{label}: not read — {ocr.text!r}"
    box = blocks[0].bbox
    found = (box.x + box.width / 2, box.y + box.height / 2)

    assert abs(found[0] - centre[0]) < TOLERANCE, f"{label}: x {found[0]:.3f} vs {centre[0]:.3f}"
    assert abs(found[1] - centre[1]) < TOLERANCE, f"{label}: y {found[1]:.3f} vs {centre[1]:.3f}"


@needs_tesseract
def test_every_box_stays_inside_the_page() -> None:
    """A box outside [0,1] is proof the divisor was wrong, whatever else looks right."""
    data, _ = _page_with_word_at("OMEPRAZOLE", at=(300, 1800), size=(1700, 2400))
    ocr = _boxes(data, "bounds.png")

    for block in ocr.pages[0].blocks:
        box = block.bbox
        assert 0.0 <= box.x <= 1.0 and 0.0 <= box.y <= 1.0, f"{block.text!r} origin {box}"
        assert box.x + box.width <= 1.001, f"{block.text!r} runs off the right edge: {box}"
        assert box.y + box.height <= 1.001, f"{block.text!r} runs off the bottom: {box}"


# ---------------------------------------------- what the viewer is served


def test_the_evidence_viewer_is_served_the_page_ocr_read() -> None:
    """Bug 2, pinned byte-for-byte.

    The viewer draws boxes measured on the PREPARED page. If it is handed anything else — the
    raw upload, a re-encode, a different DPI — the boxes are wrong by construction, and no
    amount of correct maths downstream can recover it.
    """
    from app.modules.documents.render import render_page_png

    data, _ = _page_with_word_at("ATORVASTATIN", at=(220, 500), size=(1700, 2400))

    served = render_page_png(data, media_type="image/png", page=1)
    expected = imaging.to_png(imaging.prepare(data, filename="page-1"))

    assert served == expected, (
        "the evidence viewer is not being served the same prepared page OCR read — "
        "boxes measured on one image and drawn on another point at nothing"
    )
    assert served != data, (
        "the viewer is being served the RAW upload. OCR does not read those bytes: the page "
        "is EXIF-rotated, scaled, deskewed (expand=True, which changes the canvas size) and "
        "thresholded before a single coordinate is measured."
    )


@needs_tesseract
def test_a_deskewed_page_still_maps_its_boxes_correctly() -> None:
    """The hardest case, because deskew is what makes the two coordinate spaces diverge.

    `expand=True` grows the canvas, so a box measured after rotation cannot be drawn on the
    original at all. This asserts the whole chain survives it: rotate a page by a known angle,
    and the word must still be found near where it visibly is on the SERVED image.
    """
    from app.modules.documents.render import render_page_png

    data, _ = _page_with_word_at("METFORMIN", at=(300, 900), size=(1700, 2400))
    tilted = Image.open(io.BytesIO(data)).rotate(
        -4.0, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=255
    )
    buffer = io.BytesIO()
    tilted.save(buffer, format="PNG")
    raw = buffer.getvalue()

    prepared = imaging.prepare(raw, filename="tilted.png")
    assert prepared.deskewed_degrees != 0.0, "the fixture is not actually skewed"

    ocr = _boxes(raw, "tilted.png")
    served = render_page_png(raw, media_type="image/png", page=1)
    served_image = Image.open(io.BytesIO(served))

    assert (served_image.width, served_image.height) == (prepared.width, prepared.height)
    assert (ocr.pages[0].width, ocr.pages[0].height) == (served_image.width, served_image.height)

    blocks = [b for b in ocr.pages[0].blocks if "METFORMIN" in b.text.upper()]
    assert blocks, f"the word was lost through deskew: {ocr.text!r}"

    # Crop the SERVED page at the reported box and check there is ink in it. This is the
    # end-to-end statement: the coordinates, applied to the image the viewer gets, find text.
    box = blocks[0].bbox
    region = served_image.convert("L").crop(
        (
            int(box.x * served_image.width),
            int(box.y * served_image.height),
            int((box.x + box.width) * served_image.width),
            int((box.y + box.height) * served_image.height),
        )
    )
    assert region.width > 4 and region.height > 4, f"degenerate crop {region.size}"
    darkest = min(region.getdata())
    assert darkest < 128, (
        "the reported box contains no ink on the served page — the coordinates and the image "
        "still disagree"
    )


# ------------------------------------------------------------ the crop maths


def test_crop_positioning_uses_a_percentage_model_that_works_on_both_axes() -> None:
    """Bug 3, pinned in the only place it can be: the source of the component.

    The failure was `margin-top: -y%`, which silently resolves against the container's WIDTH.
    A test that renders the component and checks a crop "appears" passes with the bug present —
    the element is there, it is just showing the wrong part of the page. So this asserts the
    technique instead, and names the trap.
    """
    source = (
        Path(__file__).resolve().parents[1]
        / "frontend"
        / "src"
        / "kiosk"
        / "SourceCrop.tsx"
    ).read_text(encoding="utf-8")

    assert "backgroundPositionX" in source and "backgroundPositionY" in source, (
        "the crop no longer positions each axis independently"
    )
    assert "marginTop" not in source, (
        "marginTop is back. Percentage margins resolve against the container's WIDTH, so a "
        "vertical offset expressed that way is wrong for every non-square container — which "
        "is how every crop came to land on blank paper."
    )
    assert "aspectRatio" in source, (
        "the crop no longer takes its shape from the real pixel aspect, so a wide text line "
        "will be squashed into the container's shape"
    )


# ─────────────────────────────────────── a unit-less reading must not 500 ───


def test_a_reading_with_no_unit_still_produces_a_valid_observation() -> None:
    """⛔ The physician's commit returned 500 on a lab report read from a photograph.

    FHIR requires `Quantity.unit` to match `[ \\r\\n\\t\\S]+` — at least one non-whitespace
    character — and the bundle builder sent `unit: ""` whenever OCR read a value but not its
    unit. That is an ORDINARY outcome on a photographed report: "ESR 41" with the mm/hr
    smudged is a perfectly good reading.

    The failure mode was the worst shape available. The physician ticks the attestation,
    presses Confirm and commit, and gets nothing at all — a 500, a pydantic traceback in the
    log, and no indication on screen that the encounter did not reach the record.
    """
    from app.fhir.bundle import _clean_unit, _quantity

    assert _clean_unit(None) is None
    assert _clean_unit("") is None
    assert _clean_unit("   ") is None, "whitespace is an absent unit, not a valid one"
    assert _clean_unit(" mg/dL ") == "mg/dL"

    # An absent unit means the key is ABSENT, not empty — that is both valid FHIR and the
    # only thing pydantic will accept.
    assert _quantity(41.0, None) == {"value": 41.0}
    assert _quantity(41.0, "") == {"value": 41.0}
    assert "unit" not in _quantity(9.1, "   ")
    assert _quantity(9.1, "%") == {"value": 9.1, "unit": "%"}


def test_the_observation_model_rejects_what_used_to_be_sent() -> None:
    """Proof the guard above is load-bearing rather than defensive.

    If `Observation` accepted an empty unit, `_clean_unit` would be dead code and nobody would
    know until it was deleted.
    """
    import pytest as _pytest
    from pydantic import ValidationError as PydanticValidationError

    from app.fhir.r4 import Observation

    with _pytest.raises(PydanticValidationError):
        Observation(
            status="final",
            code={"text": "ESR"},
            valueQuantity={"value": 41.0, "unit": ""},
        )

    # And the shape we now send is accepted.
    Observation(status="final", code={"text": "ESR"}, valueQuantity={"value": 41.0})
