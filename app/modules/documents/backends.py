"""Module B — the `OCRBackend` protocol and two implementations.

Two, deliberately, so they can be *benchmarked* rather than argued about. `eval/ocr_bench.py`
runs both over the same fixture set and reports character accuracy, entity recall and mean
confidence per backend. Choosing an OCR engine on a slide is a guess; choosing one on a table
is a decision.

* **TextLayerOCR** — pulls the embedded text layer out of a digital PDF. Perfect accuracy when
  there is one (a lab portal printout, an e-prescription), useless when there is not. Zero
  dependencies beyond `pypdf`.
* **TesseractOCR** — real OCR over rasterised pages. Handles scans and photos. Needs the
  `tesseract` binary; degrades to a clear error rather than silently returning nothing.

Both return the same `OCRPage` shape, and both must supply a per-block confidence and bounding
box, because Invariant 2 requires a page and a bbox for every `document`-tier fact.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol

from app.contracts.provenance import BoundingBox
from app.core.errors import UpstreamUnavailable, ValidationError
from app.core.logging import get_logger
from app.modules.documents import imaging

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class OCRBlock:
    """One recognised region. `bbox` is normalised to the page, origin top-left."""

    text: str
    bbox: BoundingBox
    confidence: float
    #: True when the block came from a handwriting-shaped region. Routed to the
    #: low-confidence lane and never auto-merged into the record.
    handwritten: bool = False
    #: Stable identity for this region within one read, in reading order. An extracted entity
    #: stores it so the physician screen can highlight the exact strip of paper the medicine
    #: came from, rather than re-deriving a box from coordinates and hoping they still match.
    region_id: int | None = None
    #: FALSE MEANS THE CONFIDENCE IS NOT KNOWN, and `confidence` is then a placeholder rather
    #: than a measurement. Kept as a separate flag rather than making `confidence` optional
    #: because `DocumentSpan.ocr_confidence` is a required float in [0,1] and loosening a
    #: provenance contract to carry a display concern is the wrong trade. The API renders
    #: `confidence: null` when this is false; nothing renders 0.0 as if it were measured.
    confidence_measured: bool = True
    #: Pixel size of the crop that was actually recognised, on the prepared page. This is what
    #: makes "was this region big enough to read?" answerable per line instead of per page.
    crop_width: int | None = None
    crop_height: int | None = None
    #: Which engine produced this text. With three backends and a fallback chain between them,
    #: "what read this line" is a question the evidence drawer has to be able to answer.
    engine: str = ""
    #: Set when the region was attempted and produced nothing usable. The box survives anyway:
    #: an unreadable line is evidence that there is writing there nobody has read.
    failure: str | None = None

    @property
    def reported_confidence(self) -> float | None:
        """What the API should show. `None` when unmeasured — never a stand-in number."""
        return self.confidence if self.confidence_measured else None


@dataclass(frozen=True, slots=True)
class QualityReport:
    """What was found on the page, as counts rather than as a single verdict.

    THE POINT OF THIS TYPE. The upload screen used to choose between two sentences — "the
    photo is too small to read" and "we could not find any printed writing" — from one boolean
    and an empty list. Both are whole-document verdicts, and a handwritten prescription is
    almost never wholly readable or wholly not: it is nine lines of which seven read cleanly,
    one is uncertain and one is a scrawl. Reporting that as a total failure throws away the
    seven, and reporting it as a success hides the two.

    So the page reports counts, the screen can say "some handwriting needs review", and the
    doctor still gets everything that was legible.
    """

    regions_detected: int = 0
    regions_recognised: int = 0
    regions_needing_review: int = 0
    regions_unreadable: int = 0
    #: The long edge of the ORIGINAL upload, before any resizing for a model's input. This is
    #: the number a resolution judgement must be made against.
    original_long_edge: int | None = None
    under_resolution: bool = False
    #: Median detected line height in prepared-page pixels — the honest per-line answer to
    #: "is there enough detail here to read?", which a page-level pixel count cannot give.
    median_line_height: int | None = None

    @property
    def any_usable(self) -> bool:
        return self.regions_recognised > 0

    def to_dict(self) -> dict[str, object]:
        return {
            "regionsDetected": self.regions_detected,
            "regionsRecognised": self.regions_recognised,
            "regionsNeedingReview": self.regions_needing_review,
            "regionsUnreadable": self.regions_unreadable,
            "originalLongEdge": self.original_long_edge,
            "underResolution": self.under_resolution,
            "medianLineHeight": self.median_line_height,
        }


@dataclass(frozen=True, slots=True)
class OCRPage:
    page: int
    blocks: tuple[OCRBlock, ...]
    width: int
    height: int

    @property
    def text(self) -> str:
        return "\n".join(b.text for b in self.blocks)

    @property
    def mean_confidence(self) -> float:
        scored = [
            b.confidence for b in self.blocks if b.text.strip() and b.confidence_measured
        ]
        return sum(scored) / len(scored) if scored else 0.0


@dataclass(frozen=True, slots=True)
class OCRResult:
    backend: str
    pages: tuple[OCRPage, ...] = field(default_factory=tuple)
    #: What the imaging step had to do to the page, when there was one. Carried through so the
    #: failure UX can say something specific — "the photo is too small to read" rather than
    #: "we could not read that paper" — and so a physician can see that a source image was
    #: rotated or deskewed before the text they are reading was extracted from it.
    preparation: imaging.Prepared | None = None
    #: Per-page counts, so the caller can report partial success instead of a single verdict.
    quality: QualityReport | None = None

    @property
    def blocks(self) -> tuple[OCRBlock, ...]:
        """Every block across every page, in reading order."""
        return tuple(block for page in self.pages for block in page.blocks)

    @property
    def mean_confidence(self) -> float:
        # Unmeasured blocks are EXCLUDED rather than counted as zero. A page read by an engine
        # that exposes no scores would otherwise report a mean confidence of 0.0, which reads
        # as "certainly wrong" when the truth is "not known".
        scored = [p.mean_confidence for p in self.pages if p.blocks]
        return sum(scored) / len(scored) if scored else 0.0

    @property
    def text(self) -> str:
        return "\n".join(p.text for p in self.pages)


class OCRBackend(Protocol):
    name: str
    available: bool

    def read(self, data: bytes, *, filename: str, media_type: str) -> OCRResult: ...


# ---------------------------------------------------------------- text layer


class TextLayerOCR:
    """Extracts the embedded text layer. Exact where one exists; honest where one does not."""

    name = "textlayer"
    available = True

    #: A digital text layer is a transcription, not a recognition — there is nothing to be
    #: uncertain about. Anything less than 1.0 here would be theatre.
    LAYER_CONFIDENCE = 0.99

    def read(self, data: bytes, *, filename: str, media_type: str) -> OCRResult:
        if media_type == "text/plain" or filename.endswith(".txt"):
            return self._read_text(data)
        if media_type == "application/pdf" or filename.lower().endswith(".pdf"):
            return self._read_pdf(data)
        # NO BACKEND NAME. This message reaches a patient's screen, and it used to read
        # "textlayer reads PDFs and plain text, not 'application/zip'." — which names an
        # engine they have never heard of and a MIME type they did not choose. The engine and
        # the media type go to the log, where the operator is.
        log.info("ocr.unsupported_media", backend=self.name, media_type=media_type)
        raise UnsupportedMedia(
            "This kiosk can read photographs and PDF files. Taking a photo of the paper is "
            "usually the easiest way.",
            reason="unsupported_type",
        )

    def _read_text(self, data: bytes) -> OCRResult:
        text = data.decode("utf-8", errors="replace")
        return OCRResult(backend=self.name, pages=(self._page_from_text(text, 1),))

    def _read_pdf(self, data: bytes) -> OCRResult:
        from pypdf import PdfReader

        with tempfile.NamedTemporaryFile(suffix=".pdf") as handle:
            handle.write(data)
            handle.flush()
            reader = PdfReader(handle.name)
            pages: list[OCRPage] = []
            for index, page in enumerate(reader.pages, start=1):
                measured = self._page_with_geometry(page, index)
                if measured is not None:
                    pages.append(measured)
                    continue
                pages.append(self._page_from_text(page.extract_text() or "", index))
        if not any(p.blocks for p in pages):
            # A scan wearing a PDF extension. `read_document()` catches this and retries
            # on an image-capable engine rather than surfacing it.
            # Caught by `read_document`, which retries on the image engine — a patient never
            # sees this one. Kept technical deliberately: it is a routing signal, not copy.
            raise UnsupportedMedia(
                "This PDF carries no text layer — it is a scan, and needs image OCR.",
                reason="scanned_pdf_needs_image_ocr",
            )
        return OCRResult(backend=self.name, pages=tuple(pages))

    def _page_with_geometry(self, page: object, page_number: int) -> OCRPage | None:
        """One block per text run, positioned where the run actually is on the page.

        The bounding box is the whole point of click-to-source: a physician clicks a
        medication and expects to see the line it was read from, highlighted. Deriving the
        box from a line's index among the non-blank lines — which is what `_page_from_text`
        does — ignores blank lines and real layout, so on the prescription fixture the box
        for the diagnosis landed four lines lower, over the advice. A box in the wrong place
        is worse than no box: it tells a physician the system read a line it did not read.

        `pypdf`'s text visitor reports the text matrix for each run, which is the real
        position in PDF user space. PDF's origin is bottom-left and `BoundingBox` is
        top-left, so y is flipped here rather than anywhere downstream.

        Returns `None` if the page yields no positioned text, so the derived-layout path
        stays as the fallback it always was.
        """
        runs: list[tuple[float, float, float, str]] = []

        def visit(text: str, _cm: object, tm: list[float], _font: object, size: float) -> None:
            if text.strip():
                runs.append((float(tm[4]), float(tm[5]), float(size or 11.0), text.strip()))

        try:
            page.extract_text(visitor_text=visit)  # type: ignore[attr-defined]
            box = page.mediabox  # type: ignore[attr-defined]
            width = float(box.width)
            height = float(box.height)
        except Exception as exc:  # a malformed content stream must not lose the document
            log.warning("textlayer.geometry_unavailable", page=page_number, error=str(exc)[:120])
            return None

        if not runs or width <= 0 or height <= 0:
            return None

        # Runs on one baseline are one line. Rounding to the point absorbs the sub-pixel
        # drift that kerning puts on a shared baseline.
        lines: dict[int, list[tuple[float, float, float, str]]] = {}
        for x, y, size, text in runs:
            lines.setdefault(round(y), []).append((x, y, size, text))

        blocks: list[OCRBlock] = []
        for baseline in sorted(lines, reverse=True):  # top of the page first
            parts = sorted(lines[baseline], key=lambda run: run[0])
            text = " ".join(part[3] for part in parts).strip()
            if not text:
                continue
            left = min(part[0] for part in parts)
            size = max(part[2] for part in parts)
            # The baseline sits at the bottom of the glyphs; the box is padded to the
            # ascender and a little below, which is what makes a highlight look right.
            top = height - (baseline + size)
            blocks.append(
                OCRBlock(
                    text=text,
                    bbox=BoundingBox(
                        x=round(max(left / width, 0.0), 4),
                        y=round(min(max(top / height, 0.0), 1.0), 4),
                        width=round(min(1.0 - (left / width), 1.0), 4),
                        height=round(min((size * 1.35) / height, 1.0), 4),
                    ),
                    confidence=self.LAYER_CONFIDENCE,
                    handwritten=False,
                )
            )
        return OCRPage(
            page=page_number, blocks=tuple(blocks), width=int(width), height=int(height)
        )

    def _page_from_text(self, text: str, page_number: int) -> OCRPage:
        """One block per line, with a synthetic bbox laid out down the page.

        The fallback for a page whose geometry could not be measured (see
        `_page_with_geometry`, which is tried first). The bbox here is *derived* from the
        line's index among the non-blank lines, so it is approximate and it drifts wherever
        the page has blank lines. It is a position, not a measurement, and the only reason to
        prefer it to nothing is that a document with no geometry still needs its text.
        """
        lines = [line for line in (raw.strip() for raw in text.splitlines()) if line]
        blocks: list[OCRBlock] = []
        count = max(len(lines), 1)
        for index, line in enumerate(lines):
            blocks.append(
                OCRBlock(
                    text=line,
                    bbox=BoundingBox(
                        x=0.04,
                        y=round(index / count, 4),
                        width=0.92,
                        height=round(1 / count, 4),
                    ),
                    confidence=self.LAYER_CONFIDENCE,
                    handwritten=False,
                )
            )
        return OCRPage(page=page_number, blocks=tuple(blocks), width=612, height=792)


# ---------------------------------------------------------------- tesseract


def _image_size(path: Path) -> tuple[int, int] | None:
    """The real pixel size of a rendered page, for normalising bounding boxes.

    Returning None rather than raising: a missing size falls back to the old text-extent
    behaviour, which is wrong but survivable, whereas failing the whole read because Pillow
    could not stat a file would lose the extraction entirely.
    """
    try:
        from PIL import Image  # noqa: PLC0415

        with Image.open(path) as image:
            return (image.width, image.height)
    except Exception:  # noqa: BLE001
        log.warning("ocr.page_size_unavailable", path=str(path))
        return None


class TesseractOCR:
    """Real OCR over rasterised pages, with per-word confidence from Tesseract's TSV output."""

    name = "tesseract"

    #: Tesseract reports low confidence on handwriting rather than flagging it. This
    #: threshold is what routes a block into the verification lane.
    HANDWRITING_CONFIDENCE = 0.72

    def __init__(self) -> None:
        self.binary = shutil.which("tesseract")
        self.available = self.binary is not None

    #: Rasterisation DPI for a scanned PDF. 300 is what Tesseract's own guidance asks for
    #: and what the brief specifies; the earlier 200 was chosen before any image had been
    #: through this path, and it loses thin digits in dosages on a genuine scan.
    PDF_DPI = 300

    def read(self, data: bytes, *, filename: str, media_type: str) -> OCRResult:
        if not self.available:
            # The install hint is for whoever runs the kiosk, and it goes to the log where
            # they will look. The patient gets a sentence they can act on: take a photo
            # again, or hand the paper to the doctor. Telling them to install a binary is
            # noise at best and alarming at worst.
            log.error(
                "ocr.tesseract_missing",
                remedy="brew install tesseract (add tesseract-lang for Devanagari)",
            )
            raise UpstreamUnavailable("This kiosk cannot read photographs at the moment.")
        is_pdf = media_type == "application/pdf" or filename.lower().endswith(".pdf")
        with tempfile.TemporaryDirectory() as tmp:
            if is_pdf:
                # Tesseract cannot read PDFs; leptonica refuses them outright. Rasterise
                # first. A scanned prescription arrives as a PDF far more often than as a
                # PNG, so this is the common path, not an edge case.
                return self._read_rasterised(data, Path(tmp))
            # EVERY IMAGE IS CONDITIONED FIRST. Handing raw camera bytes to Tesseract is
            # what made image OCR untested-and-broken: a HEIC it cannot open, an EXIF
            # rotation it does not apply, a photo too small to resolve, and uneven corridor
            # light it has no answer for. See `imaging.py` for why each step is there.
            prepared = imaging.prepare(data, filename=filename)
            source = Path(tmp) / "prepared.png"
            source.write_bytes(imaging.to_png(prepared))
            result = self._run(source)
            return replace(result, preparation=prepared)

    def _read_rasterised(self, data: bytes, workdir: Path) -> OCRResult:
        try:
            import pypdfium2  # noqa: PLC0415
        except ImportError as exc:
            raise UpstreamUnavailable(
                "Rasterising a PDF for OCR needs `pypdfium2` (in requirements.txt)."
            ) from exc

        source = workdir / "input.pdf"
        source.write_bytes(data)
        document = pypdfium2.PdfDocument(str(source))
        pages: list[OCRPage] = []
        try:
            for index in range(len(document)):
                # A rasterised page is an image and gets the same conditioning as a photo.
                # It arrives square and correctly sized, so deskew and scaling are no-ops —
                # but the adaptive threshold still earns its place on a grey scan.
                raw = workdir / f"page_{index + 1}_raw.png"
                document[index].render(scale=self.PDF_DPI / 72).to_pil().save(raw)
                prepared = imaging.prepare(raw.read_bytes(), filename=raw.name)
                image_path = workdir / f"page_{index + 1}.png"
                image_path.write_bytes(imaging.to_png(prepared))
                rendered = self._run(image_path)
                for page in rendered.pages:
                    pages.append(
                        OCRPage(
                            page=index + 1,
                            blocks=page.blocks,
                            width=page.width,
                            height=page.height,
                        )
                    )
        finally:
            document.close()
        return OCRResult(backend=self.name, pages=tuple(pages))

    def _run(self, source: Path) -> OCRResult:
        try:
            completed = subprocess.run(
                [str(self.binary), str(source), "stdout", "-l", "eng+hin", "tsv"],
                check=True,
                capture_output=True,
                timeout=120,
            )
        except subprocess.CalledProcessError as exc:
            raise UpstreamUnavailable(
                f"tesseract failed: {exc.stderr.decode('utf-8', 'replace')[:200]}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise UpstreamUnavailable("tesseract timed out after 120s.") from exc

        return OCRResult(
            backend=self.name,
            pages=tuple(
                self._parse_tsv(
                    completed.stdout.decode("utf-8", "replace"),
                    page_size=_image_size(source),
                )
            ),
        )

    def _parse_tsv(
        self, tsv: str, page_size: tuple[int, int] | None = None
    ) -> list[OCRPage]:
        """Tesseract TSV -> pages of blocks, grouped by (block, paragraph, line).

        Word-level rows are merged into line-level blocks: a bounding box around one word is
        useless for click-to-source, and the physician needs to see the line the value sits on.
        The line's confidence is the *minimum* of its words, not the mean — one badly read
        word in a dosage is what sends the whole line to the verification lane, and averaging
        would hide exactly that.

        ⛔ `page_size` IS THE REAL IMAGE SIZE, AND PASSING IT MATTERS.

        This used to derive the page dimensions from the words themselves —
        `max(left + width), max(top + height)` — which is the extent of the DETECTED TEXT, not
        the page. On a prescription whose lower half is blank that came out as 1168x1072 for
        an image that is actually 1797x2470, and every bbox was then normalised by the wrong
        divisor. Every coordinate was inflated, so:

          * the patient's verification-lane crop showed a patch of empty desk instead of the
            line it claimed to be evidence for, and
          * the physician's evidence overlay drew its box somewhere the text is not.

        `render.py`'s own header states the standard this violated: "a box drawn in the wrong
        place is worse than no box, because it tells a physician the system read a line it did
        not read."

        The fallback to the text extent remains for callers that genuinely cannot supply a
        size, but every caller in this module now can.
        """
        raw_lines: dict[
            tuple[int, int, int, int], list[tuple[str, float, tuple[int, int, int, int]]]
        ] = {}
        dimensions: dict[int, tuple[int, int]] = {}

        for line in tsv.splitlines()[1:]:
            row = line.split("\t")
            if len(row) < 12:
                continue
            try:
                page, block_no, para_no, line_no = (
                    int(row[1]),
                    int(row[2]),
                    int(row[3]),
                    int(row[4]),
                )
                left, top, width, height = (int(row[6]), int(row[7]), int(row[8]), int(row[9]))
                confidence = float(row[10]) / 100.0
            except ValueError:
                continue
            text = row[11].strip()
            if not text or confidence < 0:
                continue
            if page_size is not None:
                dimensions[page] = page_size
            else:
                page_w, page_h = dimensions.get(page, (0, 0))
                dimensions[page] = (max(page_w, left + width), max(page_h, top + height))
            raw_lines.setdefault((page, block_no, para_no, line_no), []).append(
                (text, confidence, (left, top, width, height))
            )

        by_page: dict[int, list[OCRBlock]] = {}
        for (page, _b, _p, _l), words in sorted(raw_lines.items()):
            page_w, page_h = dimensions.get(page, (1, 1))
            page_w, page_h = max(page_w, 1), max(page_h, 1)
            text = " ".join(w[0] for w in words)
            confidence = min(w[1] for w in words)
            lefts = [w[2][0] for w in words]
            tops = [w[2][1] for w in words]
            rights = [w[2][0] + w[2][2] for w in words]
            bottoms = [w[2][1] + w[2][3] for w in words]
            by_page.setdefault(page, []).append(
                OCRBlock(
                    text=text,
                    bbox=_normalise(
                        (min(lefts), min(tops), max(rights) - min(lefts), max(bottoms) - min(tops)),
                        page_w,
                        page_h,
                    ),
                    confidence=confidence,
                    handwritten=confidence < self.HANDWRITING_CONFIDENCE,
                )
            )

        return [
            OCRPage(
                page=page,
                blocks=tuple(by_page[page]),
                width=dimensions.get(page, (1, 1))[0],
                height=dimensions.get(page, (1, 1))[1],
            )
            for page in sorted(by_page)
        ]


def _normalise(raw: tuple[int, int, int, int], page_w: int, page_h: int) -> BoundingBox:
    left, top, width, height = raw
    return BoundingBox(
        x=min(max(left / page_w, 0.0), 1.0),
        y=min(max(top / page_h, 0.0), 1.0),
        width=min(max(width / page_w, 1e-4), 1.0),
        height=min(max(height / page_h, 1e-4), 1.0),
    )




# ---------------------------------------------------------------- handwriting


class HandwritingOCR:
    """A handwritten prescription, one detected line at a time.

    The only backend here that reads handwriting, and the only one that segments before it
    recognises. See `handwriting.py` for why the recognizer is never shown a whole page, and
    `segmentation.py` for how the lines are found.

    It is `available` only when torch and transformers are importable AND the master switch is
    on. Everything past that point which can go wrong — a gated download, an inference error,
    a page with no detectable lines — raises `UpstreamUnavailable`, and `read_document()`
    falls through to Tesseract exactly as it already does for every other engine.
    """

    name = "handwriting"

    #: A region the recognizer scored below this is shown, kept, boxed — and never merged
    #: without a person. Matched to the product's existing low-confidence threshold rather
    #: than invented here, so the handwriting lane and the printed lane agree on what "needs
    #: a human" means.
    @property
    def review_threshold(self) -> float:
        from app.core.config import settings as _settings

        return _settings.ocr_low_confidence_threshold

    def __init__(self) -> None:
        from app.core.config import settings as _settings
        from app.modules.documents import handwriting

        self.available = (
            _settings.handwriting_ocr_enabled and handwriting.dependencies_available()
        )

    def read(self, data: bytes, *, filename: str, media_type: str) -> OCRResult:
        from app.modules.documents import handwriting

        if not self.available:
            raise UpstreamUnavailable(
                "Handwriting recognition is not installed on this kiosk."
            )
        if media_type == "application/pdf" or filename.lower().endswith(".pdf"):
            # A PDF is rasterised by the printed lane, which then hands each page back here if
            # it turns out to be handwritten. Refusing rather than duplicating that machinery.
            raise UnsupportedMedia(
                f"{self.name} reads photographs, not {media_type!r}.",
                reason="unsupported_type",
            )
        return self.read_prepared(
            imaging.prepare(data, filename=filename),
            recognizer=handwriting.default_recognizer(),
        )

    def read_prepared(self, prepared: imaging.Prepared, *, recognizer: object) -> OCRResult:
        """The whole lane, with the recognizer injected.

        Split out from `read()` so the pipeline can be exercised end to end against a
        deterministic stand-in — the gated weights are not available in CI, and the part that
        was broken was never the model anyway. `tests/test_handwriting_regions.py` drives this.
        """
        from app.modules.documents import handwriting, segmentation

        regions = segmentation.find_regions(prepared)
        if not regions:
            # No detectable writing. This is the ONE whole-page failure this lane allows, and
            # it is a routing decision rather than a verdict on the document: Tesseract may
            # still find printed text the segmenter did not see as handwriting.
            raise UpstreamUnavailable(
                "No handwritten lines could be detected on this page."
            )

        readings = handwriting.read_regions(prepared, regions, recognizer)  # type: ignore[arg-type]
        engine = getattr(recognizer, "name", self.name)

        blocks: list[OCRBlock] = []
        recognised = review = unreadable = 0
        for reading in readings:
            region = reading.region
            crop = region.crop(prepared)
            # `confidence` is the float that reaches `DocumentSpan`; `measured` is what says
            # whether that float means anything. An unmeasured region carries 0.0 and is
            # reported as null — see OCRBlock.confidence_measured for why it is not Optional.
            measured = reading.confidence is not None
            confidence: float = reading.confidence if reading.confidence is not None else 0.0
            if not reading.usable:
                unreadable += 1
            elif not measured or confidence <= self.review_threshold:
                review += 1
            else:
                recognised += 1
            blocks.append(
                OCRBlock(
                    text=reading.text,
                    bbox=region.bbox,
                    confidence=confidence,
                    # EVERY handwritten block is flagged handwritten, whatever it scored.
                    # This is what keeps the lane structural: the record cannot receive a
                    # handwritten reading without a person, and that must not depend on a
                    # threshold comparison going the right way.
                    handwritten=True,
                    region_id=region.index,
                    confidence_measured=measured,
                    crop_width=crop.width,
                    crop_height=crop.height,
                    engine=engine,
                    failure=reading.failure,
                )
            )

        heights = sorted(region.height for region in regions)
        quality = QualityReport(
            regions_detected=len(regions),
            # A region that read AND scored above the review bar. Everything else is counted
            # honestly in one of the other two buckets rather than rounded up to success.
            regions_recognised=recognised,
            regions_needing_review=review,
            regions_unreadable=unreadable,
            original_long_edge=max(prepared.geometry.original_size),
            under_resolution=prepared.too_small,
            median_line_height=heights[len(heights) // 2] if heights else None,
        )
        log.info(
            "ocr.handwriting_page",
            engine=engine,
            detected=quality.regions_detected,
            recognised=quality.regions_recognised,
            review=quality.regions_needing_review,
            unreadable=quality.regions_unreadable,
            median_line_px=quality.median_line_height,
        )

        if not quality.any_usable and unreadable == len(regions):
            # Lines were found and none of them could be read. Tesseract gets a turn before
            # the patient is told anything.
            raise UpstreamUnavailable(
                f"All {len(regions)} detected handwriting regions were unreadable."
            )

        return OCRResult(
            backend=self.name,
            pages=(
                OCRPage(
                    page=1,
                    blocks=tuple(blocks),
                    width=prepared.width,
                    height=prepared.height,
                ),
            ),
            preparation=prepared,
            quality=quality,
        )


#: The registry. Declared AFTER every backend class, so adding one is a single edit here
#: rather than a forward reference that only fails at import time.
_BACKENDS: dict[str, type] = {
    "textlayer": TextLayerOCR,
    "tesseract": TesseractOCR,
    "handwriting": HandwritingOCR,
}



class UnsupportedMedia(ValidationError):
    """This engine cannot read this kind of file. Another one might.

    Its own class so `read_document()` can tell "wrong engine for this file" apart from
    "this file is broken", and retry rather than give up.
    """


#: Which engine reads which kind of file. Dispatch on the media type, because the alternative
#: — a single configured default — is what made photographs unreadable: `textlayer` was the
#: default, a phone photo is a PNG, and the patient was told to set an environment variable.
#: A patient cannot set an environment variable.
_IMAGE_TYPES = ("image/",)
_TEXT_TYPES = ("application/pdf", "text/plain")


def backend_for(media_type: str, filename: str, *, requested: str | None = None) -> OCRBackend:
    """Choose the engine from what the file actually is.

    An explicit `requested` still wins: the OCR benchmark compares engines on identical
    inputs, and the physician lane may re-run a page on a different one. This only decides
    what happens when nobody asked for anything, which is every patient upload.
    """
    if requested:
        return get_ocr_backend(requested)

    lowered = filename.lower()
    is_image = media_type.startswith(_IMAGE_TYPES) or lowered.endswith(
        (".png", ".jpg", ".jpeg", ".webp", ".heic")
    )
    if is_image:
        # A PHOTOGRAPH IS TRIED ON HANDWRITING FIRST. Most photographs taken at this kiosk are
        # of prescriptions, and a prescription is handwritten more often than not. The
        # handwriting lane refuses cleanly on a page with no detectable handwritten lines, so
        # a photograph of a PRINTED report costs one segmentation pass and then falls through
        # to Tesseract, which is the engine that reads it well.
        #
        # The reverse order is what the bug report describes: Tesseract read a handwritten
        # page, found nothing, and the patient was told the photo was too small.
        for name in ("handwriting", "tesseract"):
            engine = get_ocr_backend(name)
            if engine.available:
                return engine
        raise UpstreamUnavailable(
            "This kiosk cannot read photographs at the moment."
        )
    return get_ocr_backend("textlayer")


def read_document(data: bytes, *, filename: str, media_type: str, requested: str | None = None):
    """Read a document, retrying on an image-capable engine when the file turns out to be one.

    A PDF exported from a phone scanner app has no text layer, and `textlayer` is right to
    refuse it — but refusing is not the patient's problem to solve. The retry is what makes
    "Upload PDF" work for the documents people actually have.
    """
    backend = backend_for(media_type, filename, requested=requested)
    try:
        return backend.read(data, filename=filename, media_type=media_type)
    except UpstreamUnavailable as unavailable:
        # THE HANDWRITING LANE DECLINING IS A ROUTING EVENT, NOT A FAILURE. It raises this for
        # a page with no detectable handwritten lines, a gated model, or an inference error —
        # and in every one of those cases Tesseract should still get the page. Without this
        # arm, adding the handwriting engine in front of Tesseract would have turned every
        # printed photograph into an error.
        if requested or backend.name == "tesseract":
            raise
        fallback = get_ocr_backend("tesseract")
        if not fallback.available:
            raise
        log.info(
            "ocr.handwriting_declined",
            first=backend.name,
            then=fallback.name,
            reason=str(unavailable)[:160],
        )
        return fallback.read(data, filename=filename, media_type=media_type)
    except UnsupportedMedia as unsupported:
        if requested:
            raise
        fallback = get_ocr_backend("tesseract")
        if not fallback.available or fallback.name == backend.name:
            raise
        log.info(
            "ocr.retrying_on_image_engine",
            first=backend.name,
            then=fallback.name,
            media_type=media_type,
        )
        try:
            return fallback.read(data, filename=filename, media_type=media_type)
        except ValidationError as image_error:
            # THE RETRY MUST NOT REWRITE THE DIAGNOSIS.
            #
            # This fallback exists for one case: a PDF exported by a phone scanner app, which
            # has no text layer and IS an image. For a file that is simply not a document —
            # an archive, an audio file, something renamed by accident — the image engine also
            # fails, and reporting ITS error tells the patient "we could not open that photo"
            # about a file that was never a photo. They then retake a picture, which cannot
            # possibly help.
            #
            # The first refusal was the correct one. Keep it, and keep the image engine's
            # detail in the log for whoever is debugging rather than in the patient's face.
            log.info(
                "ocr.fallback_also_failed",
                media_type=media_type,
                image_error=str(image_error)[:120],
            )
            raise unsupported from image_error


def get_ocr_backend(name: str | None = None) -> OCRBackend:
    from app.core.config import settings

    chosen = name or settings.ocr_backend
    backend_class = _BACKENDS.get(chosen)
    if backend_class is None:
        raise ValidationError(f"Unknown OCR backend {chosen!r}. Known: {sorted(_BACKENDS)}.")
    return backend_class()  # type: ignore[return-value]


def available_backends() -> list[dict[str, object]]:
    """What `/about` reports, so a demo audience can see which engines are actually live."""
    out = []
    for name, backend_class in _BACKENDS.items():
        instance = backend_class()
        out.append({"name": name, "available": instance.available})
    return out
