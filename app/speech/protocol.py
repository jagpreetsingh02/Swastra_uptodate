"""The speech boundary — ASR in, TTS out, three interchangeable backends.

The design decision that matters here is what happens when ASR is *unsure*. The tempting
behaviour is to take the best hypothesis and carry on. We refuse: below
`ASR_CONFIDENCE_THRESHOLD` the question **degrades to touch** and is re-presented with its
option buttons. A wrong answer recorded confidently is worse than an answer taken by tap, and
in a noisy OPD corridor the ASR will be unsure often.

Degradation is per-question, never sticky. A patient who is misheard once is still offered the
microphone on the next question — a kiosk that silently gives up on speech after one bad turn
has failed the person it was built for.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.core.config import settings


@dataclass(frozen=True, slots=True)
class Transcript:
    text: str
    #: The engine's own score, or None when it did not give one. NEVER substituted — a
    #: confidence nobody measured, attached to a clinical fact, is fabricated provenance and
    #: is indistinguishable downstream from a measured one.
    confidence: float | None
    language: str
    backend: str
    duration_ms: int = 0
    #: Word-level confidences where the backend provides them. Used to show the physician
    #: which words in a quote were uncertain.
    word_confidences: tuple[tuple[str, float], ...] = ()
    #: True when the backend heard nothing usable at all, as opposed to hearing it badly.
    empty: bool = False

    @property
    def measured(self) -> bool:
        return self.confidence is not None

    @property
    def confidence_status(self) -> str:
        return "measured" if self.measured else "unavailable"

    @property
    def reliable(self) -> bool:
        """Confidently good. An UNMEASURED transcript is not reliable and not unreliable —
        it is unknown, and `unknown` is a different decision. Callers must branch on
        `measured` before trusting this."""
        if self.empty or self.confidence is None:
            return False
        return self.confidence >= settings.asr_confidence_threshold

    def to_dict(self) -> dict[str, object]:
        return {
            "text": self.text,
            "confidence": round(self.confidence, 4) if self.confidence is not None else None,
            "confidenceStatus": self.confidence_status,
            "language": self.language,
            "backend": self.backend,
            "durationMs": self.duration_ms,
            "reliable": self.reliable,
            "empty": self.empty,
            "threshold": settings.asr_confidence_threshold,
        }


@dataclass(frozen=True, slots=True)
class Utterance:
    """Synthesised speech. `audio` is WAV bytes; `text` is what was spoken, for the audit log."""

    audio: bytes
    media_type: str
    text: str
    language: str
    backend: str
    #: True when the backend could not synthesise and the client must use its own TTS.
    client_fallback: bool = False
    #: The hosted model that was called, whether or not the call succeeded, or None when no
    #: hosted model was involved. The route writes an audit row exactly when this is set —
    #: Invariant 6: every AI call lands in the hash-chained log.
    model: str | None = None


class SpeechBackend(Protocol):
    name: str
    offline: bool
    languages: tuple[str, ...]

    def transcribe(self, audio: bytes, *, language: str, media_type: str) -> Transcript: ...

    def synthesise(self, text: str, *, language: str) -> Utterance: ...
