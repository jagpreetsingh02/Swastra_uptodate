"""`khedim/Medical-Prescription-OCR` over one detected line at a time.

THE FAILURE THIS REPLACES. A handwritten prescription arriving at this kiosk was routed to
Tesseract, which is an excellent printed-text engine and reads handwriting essentially not at
all. It returned zero blocks, `extracted` came back empty, and the upload screen picked between
two sentences — "the photo is too small to read" if the page happened to be under
`MIN_LONG_EDGE`, "we could not find any printed writing" otherwise. Neither was the reason.
A 2480x3508 photograph of a perfectly legible prescription was told to stand closer.

THE MODEL IS A LINE RECOGNIZER AND IS USED AS ONE. `khedim/Medical-Prescription-OCR` is a
TrOCR encoder-decoder fine-tuned on crops of a single handwritten prescription line. Its image
processor resizes its input to 384x384 and its decoder has no concept of a newline. Given a
whole page it does not error — it squeezes every line to a few pixels tall and answers with one
fluent sentence for the sheet. So `segmentation.find_regions()` decides WHERE the lines are and
this module only ever asks WHAT one line says. The recognizer is never the layout detector.

WHAT MAKES THIS SAFE RATHER THAN JUST BETTER:

  * **A failed region is a failed region.** One crop that throws, times out, or comes back
    empty does not end the page. It is recorded as unreadable, with its box, and the other
    lines carry on. Partial success is the normal outcome on real handwriting and the pipeline
    reports it as a count rather than collapsing it to a single failure.
  * **Confidence is measured or it is absent.** Where the generation exposes usable token
    log-probabilities the confidence is the geometric mean of them; where it does not, it is
    `None` and is reported as null all the way to the screen. There is no invented 0.99.
  * **Every path out ends at Tesseract.** Missing dependencies, a gated download, an inference
    error, a page that will not segment — all raise `UpstreamUnavailable`, which
    `read_document()` already knows how to fall back from.

The weights are a GATED Hugging Face repo. Without an authorised `HF_TOKEN` the download 401s,
which is treated as one more flavour of unavailable.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from typing import Any, Protocol

from PIL import Image

from app.core.config import settings
from app.core.errors import UpstreamUnavailable
from app.core.logging import get_logger
from app.modules.documents import imaging, segmentation

log = get_logger(__name__)

#: Cached across uploads. Loading the weights takes seconds and a kiosk session may upload
#: three documents; paying that three times is three visible stalls.
_LOADED: dict[str, Any] = {}


@dataclass(frozen=True, slots=True)
class RegionReading:
    """What the recognizer made of one region, and how sure it was allowed to claim to be."""

    region: segmentation.TextRegion
    text: str
    #: `None` means NOT MEASURED — the generation exposed nothing trustworthy. It does not
    #: mean zero, and the two must never render the same way.
    confidence: float | None
    #: Set when the region was attempted and produced nothing usable. The box is still kept:
    #: an unreadable line is evidence that there is writing there nobody has read.
    failure: str | None = None

    @property
    def usable(self) -> bool:
        return self.failure is None and bool(self.text.strip())


class Recognizer(Protocol):
    """One line-image in, one (text, confidence) out.

    A protocol rather than a concrete call so the pipeline can be exercised end to end without
    the gated weights — `tests/` substitutes a deterministic stand-in and asserts on the
    plumbing, which is the part that was broken.
    """

    name: str

    def __call__(self, crops: list[Image.Image]) -> list[tuple[str, float | None]]: ...


def dependencies_available() -> bool:
    """Whether torch and transformers can be imported at all.

    Deliberately does NOT touch the network or the model cache: `/about` reads this on every
    request, and an availability probe that tries a download turns a status endpoint into a
    multi-minute hang the first time anyone opens it.
    """
    return all(importlib.util.find_spec(name) is not None for name in ("torch", "transformers"))


def _auth() -> dict[str, str]:
    """The Hugging Face token as a kwarg, or nothing.

    Returned as a dict so the token never appears in a call signature or in a traceback frame
    that renders its arguments.
    """
    token = getattr(settings, "hf_token", None)
    return {"token": token} if token else {}


def _resolve(kind: str, loader: Any, candidates: list[str]) -> Any:
    """Load one half of the processor from the first checkpoint that actually ships it.

    A `TrOCRProcessor` is a tokenizer plus an image processor, and community fine-tunes publish
    them inconsistently. Asking for both at once fails on whichever half is missing and throws
    away the half that is present, so they are resolved independently — the fine-tune first,
    because a fine-tune that DID change its tokenizer must be read with its own.
    """
    errors: list[str] = []
    for candidate in candidates:
        try:
            return loader.from_pretrained(candidate, **_auth())
        except Exception as exc:  # noqa: BLE001 — every failure here means "try the next one"
            errors.append(f"{candidate}: {str(exc)[:70]}")
    raise UpstreamUnavailable(f"No {kind} for the handwriting model ({'; '.join(errors)}).")


def load_model() -> tuple[Any, Any, str]:
    """Processor, model and device. Cached. Every failure becomes `UpstreamUnavailable`."""
    if "model" in _LOADED:
        return _LOADED["processor"], _LOADED["model"], _LOADED["device"]

    if not dependencies_available():
        raise UpstreamUnavailable(
            "Handwriting recognition needs torch and transformers "
            "(pip install -r requirements-handwriting.txt)."
        )

    model_id = settings.handwriting_model_id
    try:
        import torch
        from transformers import (
            AutoImageProcessor,
            AutoTokenizer,
            TrOCRProcessor,
            VisionEncoderDecoderModel,
        )

        tokenizer = _resolve(
            "tokenizer",
            AutoTokenizer,
            [model_id, settings.handwriting_processor_id, settings.handwriting_tokenizer_id],
        )
        image_processor = _resolve(
            "image processor", AutoImageProcessor, [model_id, settings.handwriting_processor_id]
        )
        processor = TrOCRProcessor(image_processor=image_processor, tokenizer=tokenizer)
        # `**_auth()` is a token or nothing, and transformers' overloads do not describe a
        # kwargs splat; the alternative is repeating the whole call under an `if`.
        model = VisionEncoderDecoderModel.from_pretrained(model_id, **_auth())  # type: ignore[arg-type]
        device = _device()
        model.to(device)  # type: ignore[arg-type]
        model.eval()
        torch.set_grad_enabled(False)
    except UpstreamUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        # Gated repo, no token, no network, corrupt cache, incompatible transformers. All of
        # them mean the same thing to the caller: use Tesseract.
        log.error("ocr.handwriting_unavailable", model=model_id, error=str(exc)[:200])
        raise UpstreamUnavailable(
            f"The handwriting model could not be loaded: {str(exc)[:160]}"
        ) from exc

    _LOADED.update({"processor": processor, "model": model, "device": device})
    log.info("ocr.handwriting_loaded", model=model_id, device=device)
    return processor, model, device


def _device() -> str:
    import torch

    choice = settings.handwriting_device
    if choice != "auto":
        return choice
    if torch.cuda.is_available():
        return "cuda"
    mps = getattr(torch.backends, "mps", None)
    if mps is not None and mps.is_available():
        return "mps"
    return "cpu"


def fit_for_model(crop: Image.Image, *, side: int = 384) -> Image.Image:
    """Letterbox a line crop into the square the processor wants, WITHOUT distorting it.

    The processor would otherwise resize straight to 384x384, which on a 900x60 prescription
    line means squashing the width by 15x and stretching the height by 6x. Handwriting put
    through that is not hard to read, it is a different shape — and the model was trained on
    correctly-proportioned lines.

    So the crop is scaled by ONE factor to fit, and the remainder is padded with paper white.
    Aspect ratio is preserved exactly; the model sees a line on a page, which is what it saw
    in training.
    """
    source = crop.convert("RGB")
    if source.width < 1 or source.height < 1:
        return Image.new("RGB", (side, side), (255, 255, 255))
    ratio = min(side / source.width, side / source.height)
    # Never upscale beyond 4x: past that the interpolation is inventing stroke detail, and a
    # confident reading of invented strokes is exactly the failure this pipeline exists to
    # avoid.
    ratio = min(ratio, 4.0)
    target = (max(1, round(source.width * ratio)), max(1, round(source.height * ratio)))
    resized = source.resize(target, Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (side, side), (255, 255, 255))
    # Left-aligned, vertically centred — a line of text starts at the left margin, and
    # centring it horizontally puts whitespace where the model expects the first character.
    canvas.paste(resized, (0, (side - target[1]) // 2))
    return canvas


def _khedim_recognizer() -> Recognizer:
    """The real thing: a batched call into the fine-tune, with confidence from its own scores."""
    processor, model, device = load_model()

    class _Khedim:
        name = settings.handwriting_model_id

        def __call__(self, crops: list[Image.Image]) -> list[tuple[str, float | None]]:
            images = [fit_for_model(crop) for crop in crops]
            pixels = processor(images=images, return_tensors="pt").pixel_values.to(device)
            generated = model.generate(
                pixels,
                max_new_tokens=settings.handwriting_max_new_tokens,
                num_beams=1,  # greedy: reproducible, and the scores mean what they say
                output_scores=True,
                return_dict_in_generate=True,
            )
            texts = processor.batch_decode(generated.sequences, skip_special_tokens=True)
            return list(zip(texts, _confidences(model, generated), strict=True))

    return _Khedim()


def _confidences(model: Any, generated: Any) -> list[float | None]:
    """Per-line confidence: the geometric mean of the model's own token probabilities.

    `compute_transition_scores(..., normalize_logits=True)` gives the log-probability actually
    assigned to each token that was emitted. Exponentiating their mean is the geometric mean of
    those probabilities — the joint likelihood of the line, length-normalised.

    Geometric rather than arithmetic because it is the harsher of the two: one token the model
    was unsure about drags the line down instead of being averaged away by a run of easy ones.
    In a dosage, that uncertain token is the one that matters.

    Returns `None` per line, not a fallback number, when the generation did not expose usable
    scores — a model configuration that suppresses them is a reason to say "not measured",
    never a reason to invent a value.
    """
    try:
        import torch

        scores = model.compute_transition_scores(
            generated.sequences, generated.scores, normalize_logits=True
        )
    except Exception as exc:  # noqa: BLE001 — an unscored generation is a normal outcome
        log.info("ocr.handwriting_confidence_unavailable", error=str(exc)[:120])
        return [None] * len(generated.sequences)

    out: list[float | None] = []
    for row in scores:
        kept = row[torch.isfinite(row)]
        out.append(round(float(kept.mean().exp()), 4) if kept.numel() else None)
    return out


class TesseractLineRecognizer:
    """The fallback recognizer: Tesseract, but on ONE LINE CROP at a time.

    WHY THIS EXISTS RATHER THAN JUST FALLING BACK TO THE WHOLE-PAGE PATH. Those are two
    different things and the difference is the entire feature. Whole-page Tesseract returns
    its own blocks with its own boxes, which are word- and paragraph-shaped rather than
    prescription-line-shaped, and on genuinely difficult handwriting it returns nothing at all
    — which is what produced "the photo is too small to read" for a 2480x3508 page.

    Reading the SAME segmented regions keeps everything the lane is for: one box per written
    line, a per-region confidence, per-region failure, partial-success counts, and an overlay
    a physician can click. The recognizer is swapped; the provenance is not.

    Measured on `prescription_handwritten.jpg`: whole page at `--psm 3` gives 42 words in
    Tesseract's own layout; the same page as 9 line crops at `--psm 7` gives 41 words in 9
    boxes that each correspond to one written line. Nearly the same text, and the difference
    in what can be shown to a doctor is the whole point.

    `--psm 7` is "treat this image as a single text line", which is exactly the promise
    segmentation has already made.
    """

    name = "tesseract-line"

    def __init__(self, binary: str) -> None:
        self.binary = binary

    def __call__(self, crops: list[Image.Image]) -> list[tuple[str, float | None]]:
        return [self._one(crop) for crop in crops]

    def _one(self, crop: Image.Image) -> tuple[str, float | None]:
        import subprocess
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "line.png"
            crop.save(path)
            try:
                completed = subprocess.run(
                    [self.binary, str(path), "stdout", "-l", "eng", "--psm", "7", "tsv"],
                    check=True,
                    capture_output=True,
                    timeout=30,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                # One line's failure, not the page's. `read_regions` records it as unreadable
                # and carries on.
                raise RuntimeError(f"tesseract failed on a line crop: {exc}") from exc
        return _parse_line_tsv(completed.stdout.decode("utf-8", "replace"))


def _parse_line_tsv(tsv: str) -> tuple[str, float | None]:
    """Words and a confidence out of Tesseract's TSV for one line.

    The line's confidence is the MINIMUM of its words, not the mean — matching what
    `TesseractOCR._parse_tsv` already does on the printed lane, and for the same reason: one
    badly-read word in a dosage is what should send the whole line to a human, and averaging
    is precisely how that gets hidden.
    """
    words: list[str] = []
    confidences: list[float] = []
    for row in tsv.splitlines()[1:]:
        columns = row.split("\t")
        if len(columns) < 12:
            continue
        text = columns[11].strip()
        if not text:
            continue
        try:
            confidence = float(columns[10]) / 100.0
        except ValueError:
            continue
        if confidence < 0:
            continue
        words.append(text)
        confidences.append(confidence)
    if not words:
        return "", None
    return " ".join(words), round(min(confidences), 4)


def default_recognizer() -> Recognizer:
    """Khedim if it will load, per-line Tesseract if it will not.

    The order is the whole routing decision for this lane, and both arms keep the segmentation,
    the boxes and the partial-success counts. Only if BOTH are unavailable does the caller fall
    all the way back to whole-page Tesseract.
    """
    import shutil

    try:
        return _khedim_recognizer()
    except UpstreamUnavailable as unavailable:
        binary = shutil.which("tesseract")
        if binary is None:
            raise
        log.info(
            "ocr.handwriting_recognizer_fallback",
            wanted=settings.handwriting_model_id,
            using="tesseract-line",
            reason=str(unavailable)[:160],
        )
        return TesseractLineRecognizer(binary)


def read_regions(
    prepared: imaging.Prepared,
    regions: list[segmentation.TextRegion],
    recognizer: Recognizer,
) -> list[RegionReading]:
    """Recognise every region, in batches, and never let one bad crop end the page.

    The batch is the unit of efficiency and the REGION is the unit of failure: if a batch
    throws, each of its regions is retried alone, so a single malformed crop costs its own line
    and not the seven around it.
    """
    readings: list[RegionReading] = []
    batch_size = max(settings.handwriting_batch_size, 1)

    for start in range(0, len(regions), batch_size):
        batch = regions[start : start + batch_size]
        crops = [region.crop(prepared) for region in batch]
        try:
            results = recognizer(crops)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "ocr.handwriting_batch_failed", size=len(batch), error=str(exc)[:160]
            )
            results = _retry_individually(batch, crops, recognizer)

        for region, (text, confidence) in zip(batch, results, strict=True):
            cleaned = (text or "").strip()
            if not cleaned or not any(ch.isalnum() for ch in cleaned):
                # Attempted and produced nothing. The box is kept — a line nobody could read
                # is evidence that there is writing there, and hiding it would tell a
                # physician the page held less than it does.
                readings.append(
                    RegionReading(region=region, text="", confidence=None, failure="unreadable")
                )
                continue
            readings.append(
                RegionReading(region=region, text=cleaned, confidence=confidence)
            )
    return readings


def _retry_individually(
    batch: list[segmentation.TextRegion],
    crops: list[Image.Image],
    recognizer: Recognizer,
) -> list[tuple[str, float | None]]:
    """One crop at a time, so a batch failure costs at most the crops that actually fail."""
    out: list[tuple[str, float | None]] = []
    for region, crop in zip(batch, crops, strict=True):
        try:
            out.append(recognizer([crop])[0])
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "ocr.handwriting_region_failed", region=region.index, error=str(exc)[:120]
            )
            out.append(("", None))
    return out
