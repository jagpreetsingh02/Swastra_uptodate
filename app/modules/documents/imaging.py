"""Prepare a photograph so Tesseract can actually read it.

No image had ever been through this pipeline. `TesseractOCR` handed the raw bytes to the
binary and hoped, which works for the clean synthetic scans in `data/fixtures/` and fails
quietly on the thing patients actually bring: a phone photo of a creased prescription, taken
at an angle, in corridor light, saved as HEIC and rotated by an EXIF tag nobody looks at.

Every step below exists because of a specific, silent failure mode. Silent is the operative
word — Tesseract does not report "this image is sideways" or "this is too small to read". It
returns empty output, or worse, plausible-looking garbage, and the patient is told their paper
could not be read.

THE ORDER MATTERS and is not arbitrary:

  1. DECODE (HEIC included)   — an iPhone photo is HEIC by default and Tesseract cannot open
                                one at all. Converting server-side is the difference between
                                "works" and "rejects most iPhone users".
  2. EXIF ORIENTATION         — must come before anything that measures the image. A photo
                                rotated 90° by metadata is read as 90°-rotated text, and OCR
                                on rotated text returns garbage with high confidence.
  3. GREYSCALE                — colour carries no information Tesseract uses, and it makes
                                every subsequent step slower and noisier.
  4. RESOLUTION NORMALISATION — under-resolution is the single largest cause of empty output.
                                Tesseract wants roughly 300 DPI of text; below ~1600px on the
                                longest edge, 10pt print stops being resolvable. We scale DOWN
                                to 2400 (faster, no recall gain above it) but never UP: an
                                upscaled 800px photo is still an 800px photo, and pretending
                                otherwise produces confident nonsense instead of an honest
                                "too small to read".
  5. DESKEW                   — a hand-held photo is rarely square. Tesseract tolerates a
                                degree or two and degrades sharply past about five.
  6. ADAPTIVE THRESHOLD       — a global threshold destroys a page lit unevenly, which is
                                every photo taken under one overhead light. Local thresholds
                                keep text in the shadow AND in the glare.

ILLUMINATION NORMALISATION WAS TRIED HERE AND REMOVED, because it measured worse. The
reasoning for adding it is sound and standard — divide the page by a blurred estimate of its
own lighting before deciding what is ink — and on a simulated phone-shadow photo it made the
target case dramatically worse, not better:

    hard shadow edge      no flattening   62 words, 3 of 4 drug lines
                          radius 1/6      25 words, 0 of 4
                          radius 1/12     17 words, 0 of 4
                          1/6, gain<=1.5  35 words, 2 of 4     <- best variant, still worse

Six parameter combinations were swept; none beat doing nothing. The cause is that in deep
shadow the divisor is small, so the gain is large (~4.5x at 22% illumination), and it
amplifies JPEG noise into structures the threshold then reads as ink. The adaptive threshold
does not need the absolute level corrected — it compares each pixel to its own neighbourhood,
which is already illumination-invariant at the scale that matters.

Anyone reaching for CLAHE or background division here should measure first, on
`h_hard_shadow`-style input, against these numbers.

NEVER CROP. Not at any step. A cropped prescription is a prescription with medicines missing
from it, and the failure is invisible — the extraction simply does not mention the drug that
was on the part we threw away.

Everything here is deliberately dependency-light: Pillow and numpy, no OpenCV. The deskew is a
projection-profile search rather than a Hough transform, which is a few lines instead of a
dependency and is more robust on sparse text.
"""

from __future__ import annotations

import io
import math
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageFilter, ImageOps

from app.contracts.provenance import BoundingBox
from app.core.errors import ValidationError
from app.core.logging import get_logger

log = get_logger(__name__)

#: Below this, text is not resolvable and we say so rather than guessing. A 10pt character at
#: 150 DPI is about 20px tall; Tesseract's own guidance is ~30px, which puts a full page at
#: roughly 1600px on the long edge.
MIN_LONG_EDGE = 1600
#: Above this there is no measured recall gain and every step costs more. Downscaling to it is
#: safe; upscaling TO it from below MIN_LONG_EDGE is not, and is refused.
TARGET_LONG_EDGE = 2400
#: Deskew search. Beyond ±8° a photo is not "slightly tilted", it is a different problem, and
#: rotating that far starts destroying edge text.
MAX_SKEW_DEGREES = 8.0
SKEW_COARSE_STEP = 1.0
SKEW_FINE_STEP = 0.2
#: How much better than square a candidate rotation must score before it is applied. A
#: rotation costs a resample, and resampling text that is already straight makes it worse.
MIN_SKEW_IMPROVEMENT = 1.05
#: Adaptive threshold window as a fraction of the long edge. ~1/40 covers a few characters,
#: which is the scale unevenness actually varies at.
THRESHOLD_WINDOW_FRACTION = 1 / 40
#: How far below the local mean a pixel must sit to be called ink. Too small and paper texture
#: becomes text; too large and thin strokes vanish.
THRESHOLD_BIAS = 10


@dataclass(frozen=True)
class PageGeometry:
    """Every geometric step `prepare()` took, and the arithmetic to undo them.

    THE PROBLEM THIS SOLVES. A bounding box measured on the prepared page is measured on an
    image that has been EXIF-rotated, downscaled and rotated-with-expand. Those three
    together can change both the size and the origin of the canvas — a 3024x4276 photo
    deskewed by 2 degrees comes out 2417x2497 with the content shifted. A box that is correct
    against the prepared page is therefore NOT correct against the file the patient uploaded,
    and anything that draws it over the original lands it on blank paper.

    The product's own convention (see `SourceCrop.tsx`) is that a bbox is normalised against
    the PREPARED page, and `render.py` serves that same prepared page so the two always
    agree. That convention is kept — inventing a second coordinate system is how the two
    silently drift. What this adds is the *inverse*, so a region can also be located on the
    original when something needs to: an export, a native-resolution crop, or a caller that
    only has the uploaded bytes.

    `to_original()` is exercised by a round-trip test that marks a known spot on a synthetic
    page, runs the real `prepare()`, and asserts the mapped-back box lands back on the mark.
    The rotation sign convention below was settled by that test rather than by reading
    Pillow's documentation, because the two disagree about which way is positive.
    """

    #: The uploaded file's pixel size, before anything was done to it.
    original_size: tuple[int, int]
    #: Size after EXIF transpose — swapped from `original_size` on a 90/270 rotation.
    upright_size: tuple[int, int]
    #: The EXIF orientation tag that was applied, if any.
    exif_orientation: int | None
    #: Uniform downscale factor applied after EXIF, 1.0 when the page was already small
    #: enough. Never below 1.0 in the other direction — this module refuses to upscale.
    scale: float
    #: Size handed to `rotate()`, i.e. after EXIF and scaling.
    scaled_size: tuple[int, int]
    #: Degrees passed to `Image.rotate`, positive anticlockwise. Zero when deskew was skipped.
    deskew_degrees: float
    #: Final canvas, after `rotate(..., expand=True)` grew it.
    prepared_size: tuple[int, int]

    def to_original(self, bbox: BoundingBox) -> BoundingBox:
        """A prepared-page box, mapped back to normalised coordinates on the uploaded file.

        Undoes the chain in reverse: expand-rotation, then scale, then EXIF. All four corners
        are mapped and re-bounded rather than just the origin, because a rotated rectangle is
        not a rectangle and the axis-aligned box that contains it is the only honest answer.
        """
        pw, ph = self.prepared_size
        corners = [
            (bbox.x * pw, bbox.y * ph),
            ((bbox.x + bbox.width) * pw, bbox.y * ph),
            (bbox.x * pw, (bbox.y + bbox.height) * ph),
            ((bbox.x + bbox.width) * pw, (bbox.y + bbox.height) * ph),
        ]
        mapped = [self._prepared_point_to_original(x, y) for x, y in corners]
        xs = [point[0] for point in mapped]
        ys = [point[1] for point in mapped]
        ow, oh = self.original_size
        left, right = max(min(xs), 0.0), min(max(xs), float(ow))
        top, bottom = max(min(ys), 0.0), min(max(ys), float(oh))
        return BoundingBox(
            x=round(left / ow, 6),
            y=round(top / oh, 6),
            # A degenerate box would fail BoundingBox's `gt=0`, and a region mapped entirely
            # off the page is a bug worth surfacing as a visible sliver rather than a crash.
            width=round(max(right - left, 1.0) / ow, 6),
            height=round(max(bottom - top, 1.0) / oh, 6),
        )

    def _prepared_point_to_original(self, x: float, y: float) -> tuple[float, float]:
        # --- undo rotate(expand=True) ---
        theta = math.radians(self.deskew_degrees)
        cos_t, sin_t = math.cos(theta), math.sin(theta)
        pw, ph = self.prepared_size
        sw, sh = self.scaled_size
        dx, dy = x - pw / 2.0, y - ph / 2.0
        # Pillow rotates anticlockwise in a y-down raster, which makes the forward map
        # [[cos, sin], [-sin, cos]]; this is its inverse.
        ux = dx * cos_t - dy * sin_t + sw / 2.0
        uy = dx * sin_t + dy * cos_t + sh / 2.0

        # --- undo the uniform downscale ---
        if self.scale not in (0, 1.0):
            ux /= self.scale
            uy /= self.scale

        # --- undo EXIF transpose ---
        return self._undo_exif(ux, uy)

    def _undo_exif(self, x: float, y: float) -> tuple[float, float]:
        """Map a point on the upright image back onto the file's own pixel grid.

        Only the four rotations and the three mirrors EXIF can specify. Orientation 1 (and a
        missing tag) is the identity, which is the overwhelmingly common case.
        """
        orientation = self.exif_orientation
        if orientation in (None, 1):
            return x, y
        uw, uh = self.upright_size
        if orientation == 2:  # mirrored horizontally
            return uw - x, y
        if orientation == 3:  # 180
            return uw - x, uh - y
        if orientation == 4:  # mirrored vertically
            return x, uh - y
        # The three quarter-turns swap the axes, so the inverse of a coordinate on the
        # UPRIGHT image is bounded by the upright dimension of the OTHER axis. Getting that
        # backwards is silent: the arithmetic still returns a number, it is just off the page.
        # `test_every_exif_orientation_inverts_against_pillow_itself` checks all eight against
        # `ImageOps.exif_transpose` rather than against this reasoning.
        if orientation == 5:  # transpose — reflected in the main diagonal
            return y, x
        if orientation == 6:  # rotated 90 CW — the common portrait phone capture
            return y, uw - x
        if orientation == 7:  # transverse — reflected in the anti-diagonal
            return uh - y, uw - x
        if orientation == 8:  # rotated 90 CCW
            return uh - y, x
        return x, y


@dataclass(frozen=True)
class Prepared:
    """A page ready for OCR, plus what had to be done to it.

    The record is not diagnostics for its own sake: `too_small` is what lets the failure UX
    tell a patient "the photo is too small to read, take another closer" instead of the
    useless "we could not read that paper", and `rotated_by_exif` is the first thing to look
    at when output is garbage.
    """

    image: Image.Image
    width: int
    height: int
    rotated_by_exif: bool
    deskewed_degrees: float
    scaled_from: tuple[int, int] | None
    too_small: bool
    source_format: str
    #: The full transform chain, so a region measured here can be located on the upload.
    geometry: PageGeometry


def _register_heif() -> None:
    """Teach Pillow to open HEIC/HEIF. Idempotent and safe to call per request.

    iPhones capture HEIC by default. Without this every photo straight from an iPhone is
    rejected at decode with an unhelpful error, which would exclude a large fraction of the
    people this kiosk is for.
    """
    try:
        import pillow_heif  # noqa: PLC0415
    except ImportError:
        log.warning(
            "imaging.heif_unavailable",
            remedy="pip install pillow-heif — iPhone photos are HEIC by default",
        )
        return
    pillow_heif.register_heif_opener()


def decode(data: bytes, *, filename: str) -> Image.Image:
    """Open an image, HEIC included, or raise something a patient can act on."""
    _register_heif()
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception as exc:  # noqa: BLE001 — Pillow raises a wide variety here
        lowered = filename.lower()
        if lowered.endswith((".heic", ".heif")):
            # Named separately because the remedy is specific and the patient can do it:
            # every iPhone can be told to shoot JPEG instead.
            raise ValidationError(
                "This looks like an iPhone photo in HEIC format and this kiosk could not "
                "open it. Please take the photo again using the camera button on this "
                "screen, or change your iPhone's camera setting to 'Most Compatible'.",
                reason="heic_unreadable",
            ) from exc
        raise ValidationError(
            "This file could not be opened as a photograph. Please take a photo of the "
            "paper, or upload a PDF.",
            reason="unreadable_image",
        ) from exc
    return image


def prepare(data: bytes, *, filename: str) -> Prepared:
    """Decode and condition one photograph for OCR. See the module docstring for the order."""
    image = decode(data, filename=filename)
    source_format = str(getattr(image, "format", "") or "unknown")

    # --- 2. EXIF orientation, before anything measures the image ------------
    #
    # `exif_transpose` applies the tag and strips it, so nothing downstream can apply it a
    # second time. Checking the tag first is only so we can report it: a photo that came in
    # sideways is the first thing to suspect when the text comes out as noise.
    original_size = (image.width, image.height)
    orientation = _exif_orientation(image)
    image = ImageOps.exif_transpose(image) or image
    rotated = orientation not in (None, 1)

    # --- 3. Greyscale ------------------------------------------------------
    image = image.convert("L")

    # --- 4. Resolution -----------------------------------------------------
    upright_size = (image.width, image.height)
    long_edge = max(image.width, image.height)
    too_small = long_edge < MIN_LONG_EDGE
    scaled_from: tuple[int, int] | None = None
    scale = 1.0
    if long_edge > TARGET_LONG_EDGE:
        ratio = TARGET_LONG_EDGE / long_edge
        scaled_from = (image.width, image.height)
        scale = ratio
        image = image.resize(
            (max(1, round(image.width * ratio)), max(1, round(image.height * ratio))),
            Image.Resampling.LANCZOS,
        )
    elif too_small:
        # DELIBERATELY NOT UPSCALED. Interpolation invents no detail; it only makes the image
        # bigger and the OCR slower, while producing output confident enough to look real.
        # The honest answer is to record it and let the failure UX ask for a closer photo.
        log.info("imaging.under_resolution", long_edge=long_edge, minimum=MIN_LONG_EDGE)

    # --- 5. Deskew ---------------------------------------------------------
    scaled_size = (image.width, image.height)
    angle = _estimate_skew(image)
    applied_angle = angle if abs(angle) >= SKEW_FINE_STEP else 0.0
    if abs(angle) >= SKEW_FINE_STEP:
        # `expand=True` so rotation never pushes text off the canvas — cropping is the one
        # thing this module must never do. White fill because the page is white.
        image = image.rotate(
            angle, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=255
        )

    # --- 6. Adaptive threshold --------------------------------------------
    image = _adaptive_threshold(image)

    prepared = Prepared(
        image=image,
        width=image.width,
        height=image.height,
        rotated_by_exif=rotated,
        deskewed_degrees=round(angle, 2),
        scaled_from=scaled_from,
        too_small=too_small,
        source_format=source_format,
        # Recorded from the values actually used, not re-derived afterwards: a geometry that
        # is reconstructed from the before and after sizes cannot tell a 90-degree EXIF
        # rotation apart from a portrait crop, and gets the inverse wrong for both.
        geometry=PageGeometry(
            original_size=original_size,
            upright_size=upright_size,
            exif_orientation=orientation,
            scale=scale,
            scaled_size=scaled_size,
            deskew_degrees=applied_angle,
            prepared_size=(image.width, image.height),
        ),
    )
    log.info(
        "imaging.prepared",
        source=source_format,
        original=f"{original_size[0]}x{original_size[1]}",
        prepared=f"{prepared.width}x{prepared.height}",
        exif_rotated=rotated,
        deskew_deg=prepared.deskewed_degrees,
        too_small=too_small,
    )
    return prepared


def to_png(prepared: Prepared) -> bytes:
    """Serialise for the OCR binary. PNG because it is lossless — a JPEG round-trip here
    would add exactly the compression artefacts the thresholding just removed."""
    buffer = io.BytesIO()
    prepared.image.save(buffer, format="PNG")
    return buffer.getvalue()


# ------------------------------------------------------------------ internals


def _exif_orientation(image: Image.Image) -> int | None:
    """The raw EXIF orientation tag (0x0112), or None when absent."""
    try:
        exif = image.getexif()
    except Exception:  # noqa: BLE001
        return None
    if not exif:
        return None
    value = exif.get(0x0112)
    return int(value) if value is not None else None


def _estimate_skew(image: Image.Image) -> float:
    """Find the rotation that makes text lines most horizontal, or 0.0 when there is none.

    METHOD: a projection profile. Rotate by a candidate angle, sum ink per row, and score how
    sharply that profile alternates between text rows and gaps. When lines are horizontal the
    profile is a comb of peaks; when tilted, ink smears across rows and it flattens.

    TWO CORRECTIONS THAT ARE NOT OPTIONAL, both found by measurement rather than reasoning —
    the naive version of this function reduced a real fixture from 65 words to ZERO:

      1. MEASURE INSIDE AN INSCRIBED BOX. Rotating introduces fill at the corners, and that
         fill changes the total ink mass, which changes the variance. The naive score
         therefore rose monotonically toward whichever boundary was furthest from square —
         an artefact of the rotation, read as evidence of skew. Cropping every candidate to
         the same centred box that stays inside the page at any angle in range makes the
         scores comparable, because every one is computed over the same amount of page.

      2. REQUIRE AN INTERIOR PEAK. If the best angle sits at the edge of the search range,
         the search did not find a maximum — it ran out of room. Real skew produces a peak
         with the profile falling away on both sides. A boundary result means "no skew
         detected", and the correct action is to leave the image alone.

    A gradient score (squared differences between adjacent rows) rather than plain variance:
    it responds to the sharpness of the line/gap alternation instead of to overall contrast,
    which is what actually distinguishes aligned text.
    """
    work = image.copy()
    work.thumbnail((800, 800), Image.Resampling.BILINEAR)
    array = 255 - np.asarray(work, dtype=np.float32)  # ink is high, paper is ~0
    if array.max() <= 0:
        return 0.0

    height, width = array.shape
    # The largest centred box that stays inside the page for any angle in the search range.
    # Every candidate is scored over exactly this region, so no candidate is rewarded merely
    # for having rotated more page out of frame.
    inset = math.sin(math.radians(MAX_SKEW_DEGREES))
    margin_y = int(height * inset) + 1
    margin_x = int(width * inset) + 1
    if height - 2 * margin_y < 16 or width - 2 * margin_x < 16:
        return 0.0

    source = Image.fromarray(array.astype(np.uint8))

    def score(angle: float) -> float:
        rotated = (
            array
            if angle == 0.0
            else np.asarray(
                source.rotate(angle, resample=Image.Resampling.BILINEAR, fillcolor=0),
                dtype=np.float32,
            )
        )
        window = rotated[margin_y : height - margin_y, margin_x : width - margin_x]
        profile = window.sum(axis=1)
        # Sharpness of the alternation between text rows and gaps.
        return float(np.sum(np.diff(profile) ** 2))

    coarse = _argmax_angle(score, -MAX_SKEW_DEGREES, MAX_SKEW_DEGREES, SKEW_COARSE_STEP)
    if abs(abs(coarse) - MAX_SKEW_DEGREES) < 1e-9:
        # Ran to the edge of the range: no peak was found. Leave the page as it is.
        log.info("imaging.skew_search_hit_boundary", angle=coarse, action="not rotating")
        return 0.0

    fine = _argmax_angle(
        score,
        coarse - SKEW_COARSE_STEP,
        coarse + SKEW_COARSE_STEP,
        SKEW_FINE_STEP,
    )

    # A rotation must earn itself. Below this the page is square enough, and rotating costs a
    # resample — which is not free on text that is already legible.
    if score(fine) < score(0.0) * MIN_SKEW_IMPROVEMENT:
        return 0.0
    return fine


def _argmax_angle(
    score: Callable[[float], float], low: float, high: float, step: float
) -> float:
    """The angle in [low, high] with the highest score. Ties go to the first, which is the
    most anticlockwise — arbitrary, but deterministic, which matters for a reproducible
    extraction."""
    best_angle, best_score = low, -1.0
    steps = int(round((high - low) / step)) + 1
    for index in range(steps):
        angle = low + index * step
        value = score(angle)
        if value > best_score:
            best_angle, best_score = angle, value
    return best_angle


def _adaptive_threshold(image: Image.Image) -> Image.Image:
    """Local mean threshold — the classic Bradley/Sauvola shape, integral-image free.

    WHY NOT A GLOBAL THRESHOLD. A photo taken under one overhead light has a bright patch and
    a dark corner. Any single cutoff either fills the dark corner with black or bleaches the
    bright patch to nothing, and in both cases the text there is simply gone. Comparing each
    pixel against ITS OWN NEIGHBOURHOOD keeps both.

    The local mean is computed with a box blur, which Pillow does in C — meaningfully faster
    than a numpy sliding window at this size and one line instead of ten.
    """
    from PIL import Image  # noqa: PLC0415

    radius = max(3, int(max(image.width, image.height) * THRESHOLD_WINDOW_FRACTION) // 2)  # type: ignore[attr-defined]
    local_mean = image.filter(ImageFilter.BoxBlur(radius))  # type: ignore[attr-defined]

    pixels = np.asarray(image, dtype=np.int16)  # type: ignore[arg-type]
    means = np.asarray(local_mean, dtype=np.int16)
    # Ink is darker than its surroundings by more than the bias.
    binary = np.where(pixels < (means - THRESHOLD_BIAS), 0, 255).astype(np.uint8)
    return Image.fromarray(binary, mode="L")


def describe_skew(degrees: float) -> str:
    """Human phrasing for the log and the physician's provenance note."""
    if abs(degrees) < SKEW_FINE_STEP:
        return "square"
    return f"{abs(degrees):.1f}° {'anticlockwise' if degrees > 0 else 'clockwise'}"


__all__ = [
    "MIN_LONG_EDGE",
    "TARGET_LONG_EDGE",
    "PageGeometry",
    "Prepared",
    "decode",
    "describe_skew",
    "prepare",
    "to_png",
]
