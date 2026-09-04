"""The 5-to-S substitution, kept as a permanent fixture.

`data/fixtures/documents/prescription_photo_handheld.jpg` reliably reproduces:

    on the paper   TAB. AMLODIPINE 5MG OD x 30 days
    OCR reads      AMLODIPINE SMG        confidence 0.94

⛔ 0.94 IS THE POINT. It is HIGH confidence. The engine is not hedging — it is confident and
wrong, which is exactly the failure a confidence threshold cannot catch. Everything downstream
that routes "uncertain" readings to a human would let this one straight through.

That is the argument for the verification lane in one row: not "OCR is sometimes unsure" (a
threshold handles that) but "OCR is sometimes CERTAIN and WRONG", and the only thing that
catches it is a person comparing the reading against the source. Amlodipine 5mg and 10mg are
both ordinary doses, so a misread digit here is a different prescription, not a typo.

DO NOT TUNE THE PIPELINE UNTIL THIS READS 5MG. That would be tuning to one image. If a
preprocessing change happens to fix it, good — relax the assertion, keep the fixture, and keep
the demo case, because the next photograph will do the same thing somewhere else.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "fixtures"
    / "documents"
    / "prescription_photo_handheld.jpg"
)
needs_tesseract = pytest.mark.skipif(
    shutil.which("tesseract") is None, reason="tesseract not installed"
)


def test_the_fixture_and_its_explanation_are_both_present() -> None:
    """A fixture whose reason for existing is not written down gets deleted as clutter."""
    assert FIXTURE.exists(), "the 5-to-S fixture is gone"
    readme = FIXTURE.with_suffix(".README.md")
    assert readme.exists(), "the fixture lost the README explaining why it is kept"
    text = readme.read_text(encoding="utf-8")
    assert "5" in text and "S" in text
    assert "confident" in text.lower(), "the README no longer explains why 0.94 matters"


@needs_tesseract
def test_the_misreading_is_high_confidence_not_low() -> None:
    """The assertion that carries the argument.

    If this ever fails because the reading became CORRECT, that is a genuine improvement:
    relax it and keep the fixture. If it fails because the confidence dropped below the
    verification threshold, the example has stopped demonstrating the thing it is here for
    and a new one is needed.

    PINNED TO TESSERACT. "AMLODIPINE SMG at 0.94" is Tesseract's own, specific misread of this
    specific photograph — it is what makes this fixture worth keeping, and it predates the
    handwriting lane. On default dispatch this fixture (a handheld photo) now tries
    `khedim/Medical-Prescription-OCR` first, which — per ADR-0017 — answers fluently for any
    input and never raises, so Tesseract's 5-to-S never runs and this test would silently stop
    testing the thing it exists to pin.
    """
    from app.core.config import settings
    from app.modules.documents.pipeline import read_and_extract

    _ocr, confident, needs_check = read_and_extract(
        FIXTURE.read_bytes(),
        filename=FIXTURE.name,
        media_type="image/jpeg",
        backend_name="tesseract",
    )

    amlodipine = [e for e in [*confident, *needs_check] if "amlodipine" in e.text.lower()]
    assert amlodipine, "the fixture no longer yields an amlodipine line at all"
    entity = amlodipine[0]

    # The paper says 5MG. Whatever OCR made of it, the SOURCE must still be the real line —
    # this is the provenance half, and it is what the patient is shown the crop of.
    assert "AMLODIPINE" in entity.source_text.upper()

    if "SMG" in entity.text.upper():
        assert entity.confidence > settings.ocr_low_confidence_threshold, (
            f"the misreading now scores {entity.confidence:.2f}, at or below the "
            f"{settings.ocr_low_confidence_threshold} verification threshold. It would be "
            "caught by confidence alone, so it no longer demonstrates a CONFIDENT error — "
            "find a new example rather than deleting this test."
        )
    else:
        pytest.skip(
            f"the pipeline now reads this correctly as {entity.text!r} — a real improvement. "
            "Keep the fixture; relax this assertion."
        )


def test_a_judge_is_shown_this_case() -> None:
    """It is in the demo script, because seeing it beats being told about it."""
    from app.api.routes_demo import CASES

    case = next((c for c in CASES if c.id == "photo-misread"), None)
    assert case is not None, "the 5-to-S demo case is gone"
    assert case.document == FIXTURE.name
    joined = " ".join(case.watch_for).lower()
    assert "5" in joined and "smg" in joined
    assert "0.94" in joined or "confident" in joined, (
        "the demo case no longer tells the judge that the error was a CONFIDENT one, which "
        "is the whole reason it is worth showing"
    )
