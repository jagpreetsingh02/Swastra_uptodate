"""The handwriting lane: segmentation, per-region recognition, and where the boxes point.

THE BUG THIS FILE PINS. A 2480x3508 photograph of a legible handwritten prescription was
rejected with "the photo is too small to read". Nothing about it was too small. Handwriting
was routed to Tesseract, which reads it essentially not at all, so `extracted` came back
empty — and the upload screen chose between two whole-document sentences on one boolean. The
resolution message won whenever the page happened to be under `MIN_LONG_EDGE`, and the
"no printed writing" one otherwise. Neither was the reason, and both threw away the page.

Every test here is either about not doing that again, or about the geometry that lets a
physician click a medicine and see the strip of paper it was read from.

The recognizer is INJECTED throughout. `khedim/Medical-Prescription-OCR` is a gated repo and
is not downloadable in CI, and the part that was broken was never the model — it was
everything around it. A stand-in that returns known text makes the plumbing assertable.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from app.contracts.provenance import BoundingBox, DocumentSpan
from app.core.errors import UpstreamUnavailable
from app.modules.documents import handwriting, imaging, segmentation
from app.modules.documents.backends import HandwritingOCR, OCRBlock, read_document
from app.modules.documents.entities import extract_from_block

FIXTURES = Path(__file__).resolve().parents[1] / "data" / "fixtures" / "documents"
HANDWRITTEN = FIXTURES / "prescription_handwritten.jpg"

#: What is written on the fixture, in order. Nine of the ten non-blank lines are detected —
#: the missing one is the two-character "Rx" marker, which on a page with this much sensor
#: grain sits below the noise floor. It carries no clinical content, and the alternative
#: (dropping the row threshold until it appears) admits noise bands on every other page.
WRITTEN_LINES = 9


class StubRecognizer:
    """Returns known text per crop, so assertions are about the plumbing, not the model."""

    name = "stub"

    def __init__(self, texts: list[str], confidence: float | None = 0.88) -> None:
        self.texts = texts
        self.confidence = confidence
        self.crops_seen: list[Image.Image] = []

    def __call__(self, crops: list[Image.Image]) -> list[tuple[str, float | None]]:
        out: list[tuple[str, float | None]] = []
        for crop in crops:
            index = len(self.crops_seen)
            self.crops_seen.append(crop)
            text = self.texts[index] if index < len(self.texts) else "line"
            out.append((text, self.confidence))
        return out


def _prepared():
    return imaging.prepare(HANDWRITTEN.read_bytes(), filename="hw.jpg")


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


# ------------------------------------------------------- 1. the reported bug


def test_a_full_page_handwritten_photo_is_not_called_too_small() -> None:
    """The bug, at the level it was reported. 2480x3508 is not a small photograph."""
    prepared = _prepared()
    assert prepared.geometry.original_size == (2480, 3508)
    assert prepared.too_small is False, (
        "a 2480x3508 page was flagged under-resolution; the check must read the ORIGINAL "
        "upload, never the size it becomes after being fitted to a model's input"
    )


def test_resolution_is_judged_on_the_original_not_on_the_model_input_size() -> None:
    """A crop fitted to TrOCR's 384x384 says nothing about whether the page was readable.

    The recognizer's input size is a property of the recognizer. Letting it reach a quality
    gate is how a perfectly good page gets rejected for being 384 pixels wide.
    """
    prepared = _prepared()
    regions = segmentation.find_regions(prepared)
    fitted = handwriting.fit_for_model(regions[0].crop(prepared))
    assert fitted.size == (384, 384)
    # The page is still, and independently, not too small.
    assert prepared.too_small is False
    assert max(prepared.geometry.original_size) >= imaging.MIN_LONG_EDGE


def test_the_quality_report_counts_regions_instead_of_passing_a_verdict() -> None:
    prepared = _prepared()
    result = HandwritingOCR().read_prepared(
        prepared, recognizer=StubRecognizer(["Tab Augmentin 625mg BD x 5d"] * WRITTEN_LINES)
    )
    quality = result.quality
    assert quality is not None
    assert quality.regions_detected == WRITTEN_LINES
    assert quality.original_long_edge == 3508
    assert quality.under_resolution is False
    assert quality.median_line_height and quality.median_line_height > 0
    # Every region is accounted for in exactly one bucket.
    assert (
        quality.regions_recognised
        + quality.regions_needing_review
        + quality.regions_unreadable
        == quality.regions_detected
    )


# ------------------------------------------------------- 2, 3. segmentation


def test_segmentation_produces_one_region_per_written_line() -> None:
    regions = segmentation.find_regions(_prepared())
    assert len(regions) == WRITTEN_LINES
    tops = [region.top for region in regions]
    assert tops == sorted(tops), "regions must come back in reading order"
    assert [region.index for region in regions] == list(range(len(regions)))


def test_a_region_is_the_width_of_its_line_not_the_width_of_the_page() -> None:
    """The failure that made every box useless while looking correct.

    Boxes were vertically right and horizontally full-page, because a single noise pixel makes
    a column "inked" and `rotate(expand=True)` leaves a wedge down the canvas edge. A
    full-width strip presented as the source of three words is a provenance failure.
    """
    prepared = _prepared()
    for region in segmentation.find_regions(prepared):
        assert region.width < prepared.width * 0.5, (
            f"region {region.index} spans {region.width}px of a {prepared.width}px page — "
            "the extent is being set by noise or by the rotation wedge, not by the writing"
        )
        assert region.ink_density > 0.05


def test_every_region_carries_a_bbox_inside_the_page() -> None:
    for region in segmentation.find_regions(_prepared()):
        box = region.bbox
        assert 0.0 <= box.x <= 1.0 and 0.0 <= box.y <= 1.0
        assert 0.0 < box.width <= 1.0 and 0.0 < box.height <= 1.0
        assert box.x + box.width <= 1.001


def test_a_blank_page_yields_no_regions_rather_than_one_page_sized_one() -> None:
    """Returning nothing is what routes the page to Tesseract. A single page-sized "line"
    would instead be handed to the recognizer, which answers confidently about nothing."""
    prepared = imaging.prepare(_png(Image.new("L", (2000, 1400), 255)), filename="blank.png")
    assert segmentation.find_regions(prepared) == []


# ------------------------------------------------------- 4. coordinate mapping


def test_a_region_maps_back_onto_the_mark_it_came_from() -> None:
    """The round trip, against a known target, through the real `prepare()`.

    A page is built with ONE black bar at a known place, run through EXIF-less decode,
    downscale and deskew, segmented, and the resulting box is mapped back with
    `PageGeometry.to_original()`. If the inverse is wrong — and the rotation sign convention
    is genuinely easy to get backwards — the mapped box misses the bar.
    """
    width, height = 2600, 3400
    page = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(page)
    target = (700, 1500, 1900, 1580)  # left, top, right, bottom
    draw.rectangle(target, fill=15)

    prepared = imaging.prepare(_png(page), filename="mark.png")
    regions = segmentation.find_regions(prepared)
    assert len(regions) == 1, f"expected one bar, segmented {len(regions)}"

    mapped = prepared.geometry.to_original(regions[0].bbox)
    left = mapped.x * width
    top = mapped.y * height
    right = (mapped.x + mapped.width) * width
    bottom = (mapped.y + mapped.height) * height

    # Generous but meaningful: a wrong inverse is out by hundreds of pixels or lands on the
    # opposite side of the page, not by twenty.
    assert abs(left - target[0]) < 60, f"left {left:.0f} vs {target[0]}"
    assert abs(right - target[2]) < 60, f"right {right:.0f} vs {target[2]}"
    assert abs(top - target[1]) < 60, f"top {top:.0f} vs {target[1]}"
    assert abs(bottom - target[3]) < 60, f"bottom {bottom:.0f} vs {target[3]}"


def test_the_mapping_survives_a_page_that_was_scaled_and_deskewed() -> None:
    prepared = _prepared()
    geometry = prepared.geometry
    # This fixture genuinely exercises both transforms, or the test above proves less.
    assert geometry.scale < 1.0, "expected the 2480px page to be downscaled"
    assert abs(geometry.deskew_degrees) > 0.2, "expected the 1.8-degree skew to be corrected"

    for region in segmentation.find_regions(prepared):
        mapped = geometry.to_original(region.bbox)
        assert 0.0 <= mapped.x < 1.0
        assert 0.0 <= mapped.y < 1.0
        assert mapped.x + mapped.width <= 1.0001
        assert mapped.y + mapped.height <= 1.0001


@pytest.mark.parametrize("orientation", [1, 3, 6, 8])
def test_the_mapping_undoes_every_exif_rotation_it_claims_to(orientation: int) -> None:
    geometry = imaging.PageGeometry(
        original_size=(1000, 2000) if orientation in (1, 3) else (2000, 1000),
        upright_size=(1000, 2000),
        exif_orientation=orientation,
        scale=1.0,
        scaled_size=(1000, 2000),
        deskew_degrees=0.0,
        prepared_size=(1000, 2000),
    )
    mapped = geometry.to_original(BoundingBox(x=0.1, y=0.2, width=0.3, height=0.1))
    assert 0.0 <= mapped.x <= 1.0 and 0.0 <= mapped.y <= 1.0
    assert mapped.width > 0 and mapped.height > 0


# ------------------------------------------------------- 5, 9, 10. per-region behaviour


def test_each_region_keeps_the_raw_text_read_from_it() -> None:
    prepared = _prepared()
    texts = [f"line number {index}" for index in range(WRITTEN_LINES)]
    result = HandwritingOCR().read_prepared(prepared, recognizer=StubRecognizer(texts))
    blocks = result.pages[0].blocks
    assert [block.text for block in blocks] == texts
    assert [block.region_id for block in blocks] == list(range(WRITTEN_LINES))
    assert all(block.crop_width and block.crop_height for block in blocks)


def test_one_failing_region_does_not_fail_the_document() -> None:
    """The requirement stated plainly. A crop that throws costs its own line and no other."""
    prepared = _prepared()

    doomed = segmentation.find_regions(prepared)[2].crop(prepared).size

    class OneBadCrop:
        """Fails on ONE crop, identified by its size, so the per-region retry fails too.

        An earlier version of this stub raised on its third *call*, which the retry path then
        sailed past — the test was asserting on the stub's counter rather than on the code.
        """

        name = "flaky"

        def __call__(self, crops):
            results = []
            for crop in crops:
                if crop.size == doomed:
                    raise RuntimeError("this crop explodes")
                results.append(("Tab Metformin 500mg TDS", 0.9))
            return results

    result = HandwritingOCR().read_prepared(prepared, recognizer=OneBadCrop())
    blocks = result.pages[0].blocks
    assert len(blocks) == WRITTEN_LINES, "the page must survive one bad region"
    assert result.quality is not None
    assert result.quality.regions_unreadable >= 1
    assert result.quality.regions_recognised >= 1, "the good regions must still be read"


def test_an_unmeasured_confidence_is_reported_as_null_never_as_zero() -> None:
    prepared = _prepared()
    result = HandwritingOCR().read_prepared(
        prepared, recognizer=StubRecognizer(["Tab Augmentin 625mg BD"], confidence=None)
    )
    block = result.pages[0].blocks[0]
    assert block.confidence_measured is False
    assert block.reported_confidence is None, "unmeasured must not surface as a number"
    # And it still reaches a human rather than being dropped.
    assert result.quality is not None
    assert result.quality.regions_needing_review >= 1


def test_an_unreadable_region_keeps_its_box() -> None:
    """A line nobody could read is evidence that there IS writing there. Hiding it tells a
    physician the page held less than it does."""
    prepared = _prepared()
    result = HandwritingOCR().read_prepared(
        prepared, recognizer=StubRecognizer(["", "", ""] + ["Tab Metformin 500mg TDS"] * 6)
    )
    unreadable = [b for b in result.pages[0].blocks if b.failure == "unreadable"]
    assert len(unreadable) == 3
    assert all(block.bbox.width > 0 for block in unreadable)


def test_every_handwritten_block_is_flagged_handwritten_whatever_it_scored() -> None:
    """The lane is structural, not a threshold comparison. A handwritten reading must never
    reach the record without a person, and that cannot depend on a float going the right way."""
    result = HandwritingOCR().read_prepared(
        _prepared(), recognizer=StubRecognizer(["Tab X 1mg OD"] * WRITTEN_LINES, confidence=0.99)
    )
    assert all(block.handwritten for block in result.pages[0].blocks)


# ------------------------------------------------------- crop preprocessing


def test_a_line_crop_is_letterboxed_rather_than_stretched() -> None:
    """A 900x60 line squashed into 384x384 is not a hard line to read, it is a different
    shape — and the model was trained on correctly-proportioned ones."""
    line = Image.new("RGB", (900, 60), (255, 255, 255))
    ImageDraw.Draw(line).rectangle((10, 20, 880, 40), fill=(0, 0, 0))
    fitted = handwriting.fit_for_model(line)
    assert fitted.size == (384, 384)

    # The ink must still be wide and short, i.e. the aspect ratio survived.
    import numpy as np

    ink = np.asarray(fitted.convert("L")) < 128
    rows = np.flatnonzero(ink.any(axis=1))
    columns = np.flatnonzero(ink.any(axis=0))
    drawn_aspect = (columns[-1] - columns[0]) / max(rows[-1] - rows[0], 1)
    assert drawn_aspect > 8, f"aspect collapsed to {drawn_aspect:.1f}; the crop was stretched"


def test_a_crop_is_padded_so_ascenders_are_not_clipped() -> None:
    prepared = _prepared()
    region = segmentation.find_regions(prepared)[0]
    padded = region.crop(prepared)
    tight = region.crop(prepared, pad=False)
    assert padded.height > tight.height


# ------------------------------------------------------- 6, 7, 8. provenance


def test_a_region_reaches_a_document_span_with_its_box_and_identity() -> None:
    """The end of the trace: fact -> entity -> source evidence -> document, page, bbox."""
    block = OCRBlock(
        text="Tab Augmentin 625mg BD x 5d",
        bbox=BoundingBox(x=0.11, y=0.22, width=0.3, height=0.02),
        confidence=0.81,
        handwritten=True,
        region_id=4,
        engine="stub",
    )
    entities = extract_from_block(block, 1)
    assert entities, "the line must produce a medication"
    entity = entities[0]
    assert entity.region_id == 4

    span = DocumentSpan(
        verbatim=entity.source_text,
        document_id="doc_x",
        page=entity.page,
        bbox=entity.bbox,
        ocr_confidence=entity.confidence,
        ocr_backend="handwriting",
        handwritten=entity.handwritten,
        confidence_measured=entity.confidence_measured,
        region_id=entity.region_id,
    )
    assert span.region_id == 4
    assert span.bbox == block.bbox
    assert span.verbatim == "Tab Augmentin 625mg BD x 5d"


def test_selecting_an_entity_identifies_exactly_one_region() -> None:
    """Clicking a medicine has to resolve to a box, and clicking a box has to name the
    medicines it produced. Both directions, over the real ingest output."""
    from app.contracts.record import FactLedger
    from app.modules.dialogue.ontology import load_ontology
    from app.modules.documents.pipeline import ingest

    ledger = FactLedger(session_id="s_regions", consent_scopes={"documents"})
    result = ingest(
        ledger,
        HANDWRITTEN.read_bytes(),
        filename="hw.jpg",
        media_type="image/jpeg",
        known_paths=load_ontology().known_paths,
    )
    payload = result.to_dict()
    regions = {region["regionId"]: region for region in payload["ocrRegions"]}
    assert regions, "the review screen has nothing to draw without regions"

    items = payload["extracted"]
    assert items, "this fixture must yield at least one extracted item"
    for item in items:
        region_id = item.get("regionId")
        assert region_id in regions, f"item {item['itemId']} points at no region"
        # …and the region points back.
        assert item["itemId"] in regions[region_id]["itemIds"]


def test_the_region_payload_carries_what_the_drawer_has_to_show() -> None:
    from app.contracts.record import FactLedger
    from app.modules.dialogue.ontology import load_ontology
    from app.modules.documents.pipeline import ingest

    ledger = FactLedger(session_id="s_payload", consent_scopes={"documents"})
    payload = ingest(
        ledger,
        HANDWRITTEN.read_bytes(),
        filename="hw.jpg",
        media_type="image/jpeg",
        known_paths=load_ontology().known_paths,
    ).to_dict()

    assert payload["quality"] is not None
    for region in payload["ocrRegions"]:
        assert set(region) >= {
            "regionId",
            "documentId",
            "page",
            "bbox",
            "text",
            "confidence",
            "confidenceBand",
            "backend",
            "itemIds",
        }
        assert region["page"] >= 1
        assert region["confidenceBand"] in {"high", "medium", "verify", "unreadable"}
        assert region["confidence"] is None or 0.0 <= region["confidence"] <= 1.0


# ------------------------------------------------------- 11-13. the other lanes


def test_a_printed_scan_still_reads() -> None:
    result = read_document(
        (FIXTURES / "prescription_scan.png").read_bytes(),
        filename="prescription_scan.png",
        media_type="image/png",
    )
    assert result.text.strip(), "the printed lane must still produce text"
    assert "METFORMIN" in result.text.upper()


def test_a_digital_pdf_still_uses_its_text_layer() -> None:
    result = read_document(
        (FIXTURES / "prescription.pdf").read_bytes(),
        filename="prescription.pdf",
        media_type="application/pdf",
    )
    assert result.backend == "textlayer"
    assert "METFORMIN" in result.text.upper()


def test_the_handwriting_lane_declining_falls_through_to_tesseract() -> None:
    """A page with no detectable handwriting must not become an error. Before the fallback
    arm existed, putting this engine in front of Tesseract turned every printed photograph
    into one."""
    blank = _png(Image.new("L", (2000, 1400), 255))
    with pytest.raises(UpstreamUnavailable):
        HandwritingOCR().read_prepared(
            imaging.prepare(blank, filename="blank.png"), recognizer=StubRecognizer([])
        )
    # Through the front door it is a Tesseract read, not an exception.
    result = read_document(blank, filename="blank.png", media_type="image/png")
    assert result.backend == "tesseract"


def test_the_backend_is_unavailable_rather_than_failing_at_read_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(handwriting, "dependencies_available", lambda: False)
    backend = HandwritingOCR()
    assert backend.available is False
    with pytest.raises(UpstreamUnavailable):
        backend.read(b"x", filename="a.png", media_type="image/png")


def test_the_availability_probe_never_touches_the_network() -> None:
    """`/about` calls this on every request. A probe that downloads hangs the endpoint."""
    import inspect

    source = inspect.getsource(handwriting.dependencies_available)
    assert "find_spec" in source
    for forbidden in ("from_pretrained", "requests", "httpx", "hf_hub"):
        assert forbidden not in source


# ------------------------------------------------------- entity content


@pytest.mark.parametrize(
    ("line", "name", "dose", "frequency"),
    [
        ("Tab Ivermectin-12mg  OD  x 3d", "Ivermectin", "12mg", "OD"),
        ("Tab Augmentin 625mg  BD  x 5d", "Augmentin", "625mg", "BD"),
        ("Cap Omeprazole 20mg  OD  bf", "Omeprazole", "20mg", "OD"),
        ("Syrup Paracetamol 250mg  SOS", "Paracetamol", "250mg", "SOS"),
        ("Tab Metformin 500mg  TDS  after food", "Metformin", "500mg", "TDS"),
    ],
)
def test_the_shorthand_actually_written_on_a_prescription_is_read(
    line: str, name: str, dose: str, frequency: str
) -> None:
    block = OCRBlock(
        text=line,
        bbox=BoundingBox(x=0.1, y=0.1, width=0.4, height=0.02),
        confidence=0.9,
        region_id=0,
    )
    entities = extract_from_block(block, 1)
    assert entities, f"no medication found in {line!r}"
    entity = entities[0]
    assert entity.text == name
    assert entity.detail["dose"] == dose
    assert entity.detail["frequencyRaw"] == frequency


def test_nothing_absent_from_the_line_is_invented() -> None:
    """"Tab Ivermectin-12mg" states a drug and a strength and nothing else. A frequency or a
    duration appearing here would be a fabricated clinical instruction."""
    block = OCRBlock(
        text="Tab Ivermectin-12mg BD",
        bbox=BoundingBox(x=0.1, y=0.1, width=0.4, height=0.02),
        confidence=0.9,
        region_id=0,
    )
    entity = extract_from_block(block, 1)[0]
    assert entity.detail["duration"] is None
    assert entity.detail["timing"] is None


@pytest.mark.parametrize("orientation", [1, 2, 3, 4, 5, 6, 7, 8])
def test_every_exif_orientation_inverts_against_pillow_itself(orientation: int) -> None:
    """Check the inverse against `ImageOps.exif_transpose`, not against my arithmetic.

    Three of the eight were wrong on the first attempt — the quarter-turns swap the axes, so
    the inverse is bounded by the OTHER upright dimension, and using the wrong one still
    returns a plausible number that happens to be off the page. Pillow performs the forward
    transform here and the mapping has to undo whatever Pillow actually did.
    """
    from PIL import ImageOps

    width, height = 400, 260
    stored = Image.new("L", (width, height), 255)
    # One asymmetric mark, so no rotation can be confused with any other.
    ImageDraw.Draw(stored).rectangle((40, 30, 120, 70), fill=0)

    exif = Image.Exif()
    exif[274] = orientation
    buffer = io.BytesIO()
    stored.save(buffer, format="JPEG", exif=exif)
    reloaded = Image.open(io.BytesIO(buffer.getvalue()))
    upright = ImageOps.exif_transpose(reloaded) or reloaded

    geometry = imaging.PageGeometry(
        original_size=(width, height),
        upright_size=(upright.width, upright.height),
        exif_orientation=orientation,
        scale=1.0,
        scaled_size=(upright.width, upright.height),
        deskew_degrees=0.0,
        prepared_size=(upright.width, upright.height),
    )

    # Where is the mark on the UPRIGHT image? Measure it, map it back, and it must land on
    # the mark's real position in the stored file.
    import numpy as np

    ink = np.asarray(upright.convert("L")) < 128
    rows = np.flatnonzero(ink.any(axis=1))
    columns = np.flatnonzero(ink.any(axis=0))
    box = BoundingBox(
        x=columns[0] / upright.width,
        y=rows[0] / upright.height,
        width=max(columns[-1] - columns[0], 1) / upright.width,
        height=max(rows[-1] - rows[0], 1) / upright.height,
    )

    mapped = geometry.to_original(box)
    left, top = mapped.x * width, mapped.y * height
    right = (mapped.x + mapped.width) * width
    bottom = (mapped.y + mapped.height) * height
    assert abs(left - 40) < 8 and abs(top - 30) < 8, (
        f"orientation {orientation}: mark maps to ({left:.0f},{top:.0f}), expected (40,30)"
    )
    assert abs(right - 120) < 8 and abs(bottom - 70) < 8
