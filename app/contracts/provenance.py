"""⛔ PROVENANCE — Invariant 2, expressed as types.

Every clinical fact carries the thing it came from. There are exactly **three** source tiers
and there is no fourth:

* ``stated``     — the patient said it, unprompted, in their own words.
* ``confirmed``  — the patient affirmed a direct closed question.
* ``document``   — extracted from an uploaded record, with page and bounding box.

A field with no source is ``not_asked`` or ``declined``. Those are *absences*, modelled by
:class:`Absence`, and they are structurally incapable of holding a value — see
``Absence.value`` not existing. Nothing is ever inferred, and nothing is ever filled in from
what "usually" accompanies a symptom.

The tier is not a string on a general-purpose object. ``DocumentSpan`` and ``UtteranceSpan``
are different classes with different required fields, so "a document fact with no page number"
is a construction error, not a review comment.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SourceTier(StrEnum):
    """The complete set. Adding a member is an architectural change, not a tweak."""

    STATED = "stated"
    CONFIRMED = "confirmed"
    DOCUMENT = "document"


class AbsenceReason(StrEnum):
    """Why a field has no value. Never a substitute for a source — an absence has no value."""

    NOT_ASKED = "not_asked"
    DECLINED = "declined"


class Modality(StrEnum):
    """How the patient answered. They may switch between these mid-answer."""

    SPEECH = "speech"
    TOUCH = "touch"
    TYPED = "typed"
    DOCUMENT = "document"


class _Span(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The exact text the fact came from. Verbatim — never paraphrased, never translated
    #: away from what the patient said. Translation, if any, lives in `verbatim_translated`.
    verbatim: str = Field(min_length=1)
    verbatim_translated: str | None = None
    language: str = "en"

    @field_validator("verbatim")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("verbatim source text cannot be blank or whitespace")
        return v


class UtteranceSpan(_Span):
    """A span of something the patient said or typed, during the dialogue."""

    kind: Literal["utterance"] = "utterance"
    turn_id: str
    question_id: str
    #: Character offsets into the full turn transcript. Half-open [start, end).
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)
    modality: Modality = Modality.SPEECH
    #: For a tapped answer: the exact option values the kiosk rendered and the patient
    #: pressed. This is a *stronger* proof than a text match, not a way around one — "the
    #: patient pressed the button whose value is `breathlessness`" leaves nothing to infer.
    #: `verbatim` still holds the label they read, so click-to-source shows human words.
    selected_values: tuple[str, ...] | None = None
    #: ASR confidence for a spoken turn; 1.0 for typed and tapped input.
    asr_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    audio_ref: str | None = None
    audio_start_ms: int | None = Field(default=None, ge=0)
    audio_end_ms: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _selection_needs_touch(self) -> UtteranceSpan:
        if self.selected_values is not None and self.modality is not Modality.TOUCH:
            raise ValueError(
                "selected_values is evidence of a tap; it cannot accompany speech or typing"
            )
        if self.selected_values is not None and not self.selected_values:
            raise ValueError("selected_values cannot be empty — nothing was pressed")
        return self

    @model_validator(mode="after")
    def _offsets_ordered(self) -> UtteranceSpan:
        if self.char_end <= self.char_start:
            raise ValueError("char_end must be greater than char_start")
        if (
            self.audio_start_ms is not None
            and self.audio_end_ms is not None
            and self.audio_end_ms <= self.audio_start_ms
        ):
            raise ValueError("audio_end_ms must be greater than audio_start_ms")
        return self


class BoundingBox(BaseModel):
    """Normalised page coordinates, origin top-left, each in [0, 1]."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    width: float = Field(gt=0.0, le=1.0)
    height: float = Field(gt=0.0, le=1.0)


class DocumentSpan(_Span):
    """A span of an uploaded document. Page and bounding box are mandatory, per Invariant 2."""

    kind: Literal["document"] = "document"
    document_id: str
    page: int = Field(ge=1)
    bbox: BoundingBox
    ocr_confidence: float = Field(ge=0.0, le=1.0)
    ocr_backend: str
    #: Handwriting goes to the low-confidence lane and is never silently merged (Module B).
    handwritten: bool = False
    #: FALSE means `ocr_confidence` is a placeholder, not a measurement — the recogniser
    #: exposed no trustworthy score for this region. Additive and defaulted, so every existing
    #: span keeps its meaning: the contract is unchanged, it can now just also say "unknown".
    #:
    #: `ocr_confidence` stays a required float in [0,1] rather than becoming optional. A
    #: provenance field that may be absent is one every reader has to branch on, and the
    #: honest reading of an unmeasured span is "assume the worst and send it to a human",
    #: which 0.0 already encodes. The flag is what stops a UI printing that 0.0 as if it were
    #: measured certainty.
    confidence_measured: bool = True
    #: Which detected OCR region this span came from, when the engine segmented the page.
    #: This is what lets a physician click an extracted medicine and have the exact strip of
    #: the prescription highlighted, instead of the box being re-derived downstream.
    region_id: int | None = None
    #: What a *named human* read the span as, when OCR got it wrong. The scrawl stays in
    #: `verbatim`; this is the reading, and the two sit side by side in the evidence drawer.
    #:
    #: Before this existed, a correction was recorded with the corrected text as the value
    #: and the OCR line as the span — so `record_fact()` looked for "Metformin" inside
    #: "TAB. METFARMIN 500mg", failed to find it, and refused the fact. The verification lane
    #: appeared to work and quietly dropped every correction that actually changed a word.
    #: The fix is to carry the human's reading as evidence in its own right, not to relax the
    #: echo check.
    human_reading: str | None = None
    #: Who read it. A correction with no name attached is exactly what Invariant 2 refuses.
    read_by: str | None = None

    @model_validator(mode="after")
    def _reading_is_attributed(self) -> DocumentSpan:
        if self.human_reading is not None and not (self.read_by or "").strip():
            raise ValueError(
                "human_reading must name who read it — an unattributed correction is not "
                "provenance, it is an anonymous edit"
            )
        return self


Span = Annotated[UtteranceSpan | DocumentSpan, Field(discriminator="kind")]

#: Which spans may back which tier. A `document` fact cannot be backed by an utterance and
#: vice versa — the tier and the span class are two views of the same truth, kept in step here.
TIER_SPAN_CLASS: dict[SourceTier, type[_Span]] = {
    SourceTier.STATED: UtteranceSpan,
    SourceTier.CONFIRMED: UtteranceSpan,
    SourceTier.DOCUMENT: DocumentSpan,
}


class Fact(BaseModel):
    """One recorded clinical fact. Constructible only through ``record_fact()``.

    ``Fact`` deliberately has no default for ``source``: there is no way to instantiate one
    without a span. The tier/span agreement is enforced in ``_tier_matches_span``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    fact_id: str
    session_id: str
    #: Dotted path into ClinicalHistory, e.g. "hpi.severity" or "medications[0].dose".
    path: str = Field(min_length=1)
    value: Any
    tier: SourceTier
    source: Span
    confidence: float = Field(ge=0.0, le=1.0)
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    #: Set when a later fact supersedes this one (patient contradicts themselves).
    superseded_by: str | None = None
    #: Free-form, non-clinical. Extraction model name, rule id, etc.
    provenance_note: str | None = None

    @model_validator(mode="after")
    def _tier_matches_span(self) -> Fact:
        expected = TIER_SPAN_CLASS[self.tier]
        if not isinstance(self.source, expected):
            raise ValueError(
                f"tier {self.tier.value!r} requires a {expected.__name__}, "
                f"got {type(self.source).__name__}"
            )
        return self

    @property
    def active(self) -> bool:
        return self.superseded_by is None

    def content_hash(self) -> str:
        payload = f"{self.path}|{self.value!r}|{self.tier.value}|{self.source.verbatim}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class Absence(BaseModel):
    """A field that has no value, and *why*. Has no ``value`` attribute, by design.

    This is the only sanctioned way to represent "we don't know". Writing ``None`` into a
    clinical field is indistinguishable from "normal" at the physician's screen; an explicit
    ``not_asked`` is not.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str
    path: str = Field(min_length=1)
    reason: AbsenceReason
    question_id: str | None = None
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def span_digest(span: Span) -> str:
    """Stable identifier for a span, used to detect duplicate recordings of one utterance."""
    if isinstance(span, DocumentSpan):
        key = f"doc|{span.document_id}|{span.page}|{span.bbox.x},{span.bbox.y}|{span.verbatim}"
    else:
        key = f"utt|{span.turn_id}|{span.char_start}:{span.char_end}|{span.verbatim}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
