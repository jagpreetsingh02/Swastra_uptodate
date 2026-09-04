"""Find the handwritten lines on a prepared page, so the recognizer never sees a whole page.

WHY THIS MODULE EXISTS AT ALL. `khedim/Medical-Prescription-OCR` is a TrOCR encoder-decoder
fine-tuned on crops of ONE handwritten prescription line. Its decoder is a language model with
a bounded output length and no concept of a newline, and its image processor resizes whatever
it is given to 384x384. Hand it a 2400px page and two things happen at once: every line is
squeezed to a few pixels tall, and the decoder answers with one fluent, plausible sentence for
the whole sheet. That failure is silent — there is no error, just a short confident string —
which is the worst shape a failure can take in a system whose whole purpose is provenance.

So the page is cut into lines first, always, and the model is only ever asked the question it
was trained on. **The recognizer is not the layout detector.**

THE METHOD IS A PROJECTION PROFILE, NOT A LEARNED DETECTOR, and that is a deliberate choice
under this repo's rule-or-LLM policy. A row of a page either carries ink or it does not; that
is a measurement. It is reproducible, it runs in milliseconds on a kiosk with no GPU, and when
it is wrong it is *visibly* wrong — a band too tall, a band too short — rather than confidently
proposing a region that is not there. A learned layout model would be better on a crumpled page
and would cost a second model download, a second thing that can hallucinate, and a dependency
this module's neighbours have explicitly refused.

NO OpenCV, for the same reason `imaging.py` gives: the morphology here is a handful of numpy
operations, and a 90 MB wheel for a sliding-window maximum is a bad trade on a kiosk image.
The pipeline below is the standard explainable one, with the cv2 call each step replaces named
against it:

    adaptive threshold      already done by imaging.prepare()   (cv2.adaptiveThreshold)
    horizontal dilation     sliding-window max over columns     (cv2.dilate, 1xN kernel)
    line banding            row projection + a measured floor   (cv2.findContours on the mask)
    column trimming         ink extent within each band         (cv2.boundingRect)
    merge and order         gap-based merge, then reading order  (contour sort)

Everything returns coordinates on the PREPARED page, which is this product's existing bbox
convention (`SourceCrop.tsx`, `render.py`). `imaging.PageGeometry.to_original()` maps them back
to the upload when something needs the original instead.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PIL import Image

from app.contracts.provenance import BoundingBox
from app.core.logging import get_logger
from app.modules.documents.imaging import Prepared

log = get_logger(__name__)

#: A band shorter than this fraction of the page is an underline, a staple hole or a speck.
MIN_LINE_HEIGHT_FRACTION = 0.006
#: A band taller than this is two lines that merged, or a table — split rather than crop.
MAX_LINE_HEIGHT_FRACTION = 0.16
#: A band must carry at least this fraction of the page width in ink to be writing. Low on
#: purpose: the "Rx" that marks where a drug list starts is two characters — about 26px on a
#: 1747px page — and dropping it loses the marker for everything under it.
MIN_INK_WIDTH_FRACTION = 0.012
#: Ignored at the left and right edges when measuring how wide a line is. `rotate(expand=True)`
#: leaves a fill wedge down two sides of the canvas and the adaptive threshold reads its
#: boundary as a vertical stroke. Measured on the handwritten fixture: a drug line whose text
#: spans columns 200-600 was reporting an extent of 0-1747, because the wedge put 239 ink
#: pixels in the first hundred columns. Every box was a full-width strip.
EDGE_MARGIN_FRACTION = 0.02
#: How far apart two inked clusters can be and still be the same written line. Wider than a
#: word space, far narrower than the distance from the text to a page-edge artefact.
MAX_WORD_GAP_FRACTION = 0.045
#: Characters closer than this fraction of the page width belong to the same line. This is the
#: dilation that joins letters into words and words into a line; too small and a prescription
#: breaks into one region per word, too large and the row profile never returns to the floor
#: and the whole page becomes one band. Measured on the handwritten fixture: 0.035 smears
#: (2 bands for 10 lines), 0.020 is marginal, 0.012 recovers all ten.
JOIN_GAP_FRACTION = 0.012
#: Crops are padded before recognition — a box fitted to the glyphs clips ascenders and
#: descenders, and TrOCR was trained on crops with margin. Vertical is generous for that
#: reason; horizontal is small because the band already spans what was written.
PAD_Y_FRACTION = 0.34
PAD_X_FRACTION = 0.012
#: Below this a crop is a sliver, not a line of text. Dropped rather than recognised.
MIN_CROP_PIXELS = 16
#: A page that segments into more bands than this is a texture, not a prescription.
MAX_REGIONS = 80


@dataclass(frozen=True, slots=True)
class TextRegion:
    """One detected handwritten line, in reading order, with everything needed to cite it."""

    #: Position in reading order. Stable for the life of one read, and what an entity stores
    #: to point back at the region it came from.
    index: int
    #: Pixel box on the PREPARED page — what gets cropped and sent to the recognizer.
    left: int
    top: int
    width: int
    height: int
    #: The same box, normalised against the prepared page. This is the product's existing
    #: bbox convention and the one that reaches `DocumentSpan`.
    bbox: BoundingBox
    #: Fraction of the band that is ink. Not a confidence — a plausibility signal, used to
    #: drop rules, borders and shadows before they ever cost an inference.
    ink_density: float

    def crop(self, prepared: Prepared, *, pad: bool = True) -> Image.Image:
        """The pixels of this region, with padding so nothing is clipped."""
        pad_y = int(self.height * PAD_Y_FRACTION) if pad else 0
        pad_x = int(prepared.width * PAD_X_FRACTION) if pad else 0
        left = max(self.left - pad_x, 0)
        top = max(self.top - pad_y, 0)
        right = min(self.left + self.width + pad_x, prepared.width)
        bottom = min(self.top + self.height + pad_y, prepared.height)
        return prepared.image.crop((left, top, right, bottom))


def _ink_mask(prepared: Prepared) -> np.ndarray:
    """True where there is ink. `imaging.prepare()` has already thresholded the page."""
    array = np.asarray(prepared.image.convert("L"), dtype=np.uint8)
    # The prepared page is bilevel-ish after the adaptive threshold, so a mid cut is safe and
    # does not need a second Otsu pass.
    return array < 128


def _dilate_rows(mask: np.ndarray, width: int) -> np.ndarray:
    """Sliding-window maximum along each row — cv2.dilate with a 1xN kernel, in numpy.

    Joining characters horizontally before measuring rows is what turns a row of separate
    letters into one continuous run of ink. Without it the column-extent step below finds the
    gaps between words and reports a region per word.

    A cumulative sum rather than an N-deep max, so the cost is independent of the kernel width.

    THE KERNEL IS `width` WIDE, NOT `2 * width`. The first version padded by `width` on each
    side and then differenced over `2 * width`, which silently doubled it — on a 1747px page a
    61px join became a 122px smear, the ink fraction went from 1% to 18%, the row profile never
    returned to the floor, and a ten-line prescription segmented into two full-width bands.
    Nothing errored; the page simply came back with eight lines missing.
    """
    if width < 2:
        return mask
    half = width // 2
    span = 2 * half + 1
    padded = np.pad(mask.astype(np.int32), ((0, 0), (half, half)))
    cumulative = np.pad(np.cumsum(padded, axis=1), ((0, 0), (1, 0)))
    windowed = cumulative[:, span:] - cumulative[:, :-span]
    return windowed[:, : mask.shape[1]] > 0


def _otsu(profile: np.ndarray) -> float:
    """The cut between a blank row and a row with writing, measured on this page.

    A fixed fraction of the page width does not survive a real photograph: after thresholding,
    a blank row of a phone photo still carries a percent or two of residual grain, and a fixed
    fraction sits below that floor, so every row passes and the whole page becomes one band.
    Otsu finds the cut that minimises within-class variance, which is exactly "put the line
    between the noise floor and the text".
    """
    if not profile.any():
        return 0.0
    top = float(profile.max())
    if top <= 0:
        return 0.0
    counts, edges = np.histogram(profile, bins=128, range=(0.0, top))
    weights = counts.astype(np.float64)
    total = weights.sum()
    if total <= 0:
        return 0.0
    centres = (edges[:-1] + edges[1:]) / 2.0
    weight_low = np.cumsum(weights)
    weight_high = total - weight_low
    sum_low = np.cumsum(weights * centres)
    sum_total = sum_low[-1]
    valid = (weight_low > 0) & (weight_high > 0)
    if not valid.any():
        return 0.0
    mean_low = np.divide(sum_low, weight_low, out=np.zeros_like(sum_low), where=weight_low > 0)
    mean_high = np.divide(
        sum_total - sum_low, weight_high, out=np.zeros_like(sum_low), where=weight_high > 0
    )
    between = weight_low * weight_high * (mean_low - mean_high) ** 2
    between[~valid] = -1.0
    return float(centres[int(np.argmax(between))])


def _floor_threshold(profile: np.ndarray, sigmas: float = 3.0) -> float:
    """Where the blank-paper population ends: its median, plus a few robust spreads.

    An outlier test, not a midpoint. Otsu's cut itself sits halfway between the two
    populations, and using it directly throws away every SHORT line on the page — on a
    prescription that is the two-character "Rx" that marks where the drug list starts.

    Median and MAD rather than mean and standard deviation, because the rows beneath Otsu's cut
    include those short text lines as well as the blank ones, and a mean is dragged upward by
    exactly the contamination this is trying to keep.
    """
    cut = _otsu(profile)
    floor = profile[profile <= cut] if cut > 0 else profile[profile <= 0]
    if floor.size == 0:
        return cut
    median = float(np.median(floor))
    # 1.4826 * MAD is the consistent estimator of sigma for a normal distribution, which puts
    # this on the same scale as the "three sigmas" the signature claims.
    spread = 1.4826 * float(np.median(np.abs(floor - median)))
    return median + sigmas * spread


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """Contiguous True runs as (start, end_exclusive)."""
    if not mask.any():
        return []
    padded = np.concatenate(([False], mask, [False]))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    return list(zip(edges[0::2].tolist(), edges[1::2].tolist(), strict=True))


def _split_tall_band(
    profile: np.ndarray, start: int, end: int, typical: float
) -> list[tuple[int, int]]:
    """Cut a merged band at its internal minima.

    Handwriting joins lines: a descender from one line touches an ascender of the next and the
    projection never returns to the floor between them. The trough is still there, it is just
    not zero, so the split point is the local MINIMUM rather than a blank row.
    """
    if typical < 2:
        return [(start, end)]
    span = profile[start:end]
    expected = max(int(round((end - start) / typical)), 2)
    cuts: list[int] = []
    for piece in range(1, expected):
        centre = int(len(span) * piece / expected)
        window = max(int(typical * 0.3), 2)
        low = max(centre - window, 1)
        high = min(centre + window, len(span) - 1)
        if low >= high:
            continue
        cuts.append(start + low + int(np.argmin(span[low:high])))
    edges = [start, *sorted(set(cuts)), end]
    return [(a, b) for a, b in zip(edges[:-1], edges[1:], strict=True) if b - a >= 4]


def _ink_extent(band: np.ndarray, margin: int = 0, join: int = 0) -> tuple[int, int] | None:
    """Left and right edge of the WRITING in a band, ignoring everything that is not it.

    Three attempts were needed here and the first two are worth recording, because both were
    vertically correct and horizontally meaningless — a full-page strip highlighted as the
    source of three words, which is a provenance failure dressed as a rendering one.

      1. `band.any(axis=0)`. One surviving noise pixel anywhere in the row makes that column
         inked, so the extent was the whole page on every line.
      2. A floor of two inked rows. Measured on a drug line: text columns carry 8-22, noise
         columns carry 1-2, so a floor of 2 still let the extent run to the far edge.

    What actually works is to stop treating the extent as "first to last" at all. A line of
    handwriting is ONE horizontal cluster; the rotation wedge down the canvas edge and the
    shadow boundary crossing the row are separate clusters that happen to share the row. So
    the columns are thresholded, dilated by the same join width that formed the line, split
    into runs, and the run carrying the most ink wins.
    """
    if band.size == 0:
        return None
    height, width = band.shape
    profile = band.sum(axis=0).astype(np.int64)
    if margin > 0 and width > 2 * margin:
        # Not cropped — zeroed for the MEASUREMENT only, so the fill wedge left by
        # `rotate(expand=True)` cannot define where a line begins.
        profile = profile.copy()
        profile[:margin] = 0
        profile[width - margin :] = 0

    # A real text column is inked through a fifth of the band's height.
    floor = max(3, int(height * 0.2))
    inked = profile >= floor
    if not inked.any():
        # Nothing cleared the floor. Fall back to any ink so a genuinely faint line is still
        # boxed rather than silently dropped — it will be low-confidence, not absent.
        inked = profile > 0
        if not inked.any():
            return None

    if join > 1:
        inked = _dilate_rows(inked[np.newaxis, :], join)[0]

    runs = _runs(inked)
    if not runs:
        return None
    totals = [int(profile[start:end].sum()) for start, end in runs]
    anchor = int(np.argmax(totals))
    left, right = runs[anchor]

    # THE DENSEST RUN ALONE IS NOT THE LINE. On "Tab Ivermectin-12mg  OD  x 3d" the double
    # spaces before OD and before x are wider than the join, so the line arrives here as three
    # runs and taking only the biggest clips the frequency and the duration off the end — the
    # two fields a prescription is least able to lose. So the anchor absorbs its neighbours
    # while they are close enough to be the same line. The rotation wedge and the shadow
    # boundary are hundreds of pixels away and are not absorbed.
    reach = max(int(width * MAX_WORD_GAP_FRACTION), join * 2, 12)
    for index in range(anchor - 1, -1, -1):
        run_start, run_end = runs[index]
        if left - run_end > reach:
            break
        left = run_start
    for index in range(anchor + 1, len(runs)):
        run_start, run_end = runs[index]
        if run_start - right > reach:
            break
        right = run_end
    return left, right


def find_regions(prepared: Prepared) -> list[TextRegion]:
    """Every handwritten line on the page, in reading order: top to bottom, left to right.

    Returns an empty list when the page has no measurable writing. That is a real answer and
    callers must treat it as one — `handwriting.py` falls back to Tesseract on it rather than
    handing the recognizer an arbitrary rectangle to be fluent about.
    """
    mask = _ink_mask(prepared)
    height, width = mask.shape
    if height < MIN_CROP_PIXELS or width < MIN_CROP_PIXELS:
        return []

    join_width = max(int(width * JOIN_GAP_FRACTION), 1)
    joined = _dilate_rows(mask, join_width)
    profile = joined.sum(axis=1).astype(np.float32)
    threshold = max(_floor_threshold(profile), width * 0.004, 1.0)

    min_height = max(int(height * MIN_LINE_HEIGHT_FRACTION), 4)
    max_height = max(int(height * MAX_LINE_HEIGHT_FRACTION), min_height + 1)
    bands = [
        (start, end) for start, end in _runs(profile >= threshold) if end - start >= min_height
    ]
    if not bands:
        log.info("segmentation.no_bands", page=f"{width}x{height}")
        return []

    typical = float(np.median([end - start for start, end in bands]))
    split: list[tuple[int, int]] = []
    for start, end in bands:
        if end - start <= max(max_height, typical * 2.0):
            split.append((start, end))
        else:
            split.extend(_split_tall_band(profile, start, end, typical))

    min_ink_width = max(int(width * MIN_INK_WIDTH_FRACTION), 6)
    edge_margin = max(int(width * EDGE_MARGIN_FRACTION), 6)
    regions: list[TextRegion] = []
    for band_top, band_bottom in split:
        # Column extent is measured on the UNDILATED mask: the dilation was only ever there to
        # decide where the line is, and using it for the box would pad every region by the
        # join width on both sides.
        band = mask[band_top:band_bottom]
        extent = _ink_extent(band, margin=edge_margin, join=join_width)
        if extent is None:
            continue
        ink_left, ink_right = extent
        if ink_right - ink_left < min_ink_width:
            continue
        if bottom_or_side_noise(band_bottom - band_top, height):
            continue
        if _is_page_edge(band_top, band_bottom, height):
            continue
        regions.append(
            _region(
                index=len(regions),
                left=ink_left,
                top=band_top,
                width=ink_right - ink_left,
                height=band_bottom - band_top,
                density=float(band[:, ink_left:ink_right].mean()),
                prepared=prepared,
            )
        )

    if len(regions) > MAX_REGIONS:
        # Not a prescription. Reading 300 crops would take minutes and produce nothing; the
        # caller treats an over-segmented page as unsegmentable and falls back in one pass.
        log.warning("segmentation.too_many_regions", found=len(regions), limit=MAX_REGIONS)
        return []

    log.info(
        "segmentation.done",
        regions=len(regions),
        page=f"{width}x{height}",
        threshold=round(threshold, 1),
    )
    return regions


def _is_page_edge(top: int, bottom: int, page_height: int) -> bool:
    """A band pressed against the very top or bottom of the canvas is the canvas, not writing.

    `rotate(expand=True)` leaves a wedge of fill along two edges and a JPEG leaves ringing
    along all four; both survive the threshold as a thin full-width band. They are not text,
    they cost an inference each, and on the handwritten fixture they produced two confident
    boxes around nothing.
    """
    margin = max(int(page_height * 0.012), 8)
    return top <= margin or bottom >= page_height - margin


def bottom_or_side_noise(band_height: int, page_height: int) -> bool:
    """A band thinner than a stroke is a rule, a fold or a scanner edge, not writing."""
    return band_height < max(int(page_height * 0.004), 3)


def _region(
    *,
    index: int,
    left: int,
    top: int,
    width: int,
    height: int,
    density: float,
    prepared: Prepared,
) -> TextRegion:
    page_w, page_h = max(prepared.width, 1), max(prepared.height, 1)
    return TextRegion(
        index=index,
        left=left,
        top=top,
        width=width,
        height=height,
        ink_density=round(density, 4),
        bbox=BoundingBox(
            x=round(min(max(left / page_w, 0.0), 1.0), 6),
            y=round(min(max(top / page_h, 0.0), 1.0), 6),
            width=round(min(max(width / page_w, 1e-4), 1.0), 6),
            height=round(min(max(height / page_h, 1e-4), 1.0), 6),
        ),
    )


__all__ = ["TextRegion", "find_regions"]
