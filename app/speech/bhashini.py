"""Bhashini / AI4Bharat backend, behind the same protocol. See ADR-0018.

Written against the Dhruva compute endpoint (`/services/inference/pipeline`) and verified live
on 2026-09-28: TTS in twelve languages, and ASR round-trips in eleven. Two faults in the first,
never-exercised version were found only by making those calls:

* it posted to `/services/inference`, which 404s; and
* it sent one `BHASHINI_PIPELINE_ID` as the `serviceId` for every task and every language,
  when Dhruva's service IDs differ per task and per language family.

Service IDs now live in `config/bhashini-services.yaml`, not here, because they drift the same
way hosted model names do (`llama-3.3-70b-versatile` was decommissioned mid-build).

It is **not** on the critical path. With no key configured it raises
:class:`UpstreamUnavailable` at construction and the registry selects the local backend. A
synthesis that fails or hangs returns `client_fallback` rather than an error, so the worst
case for a patient is the question staying on screen — what happens without Bhashini at all.
"""

from __future__ import annotations

import base64
from functools import lru_cache
from typing import Any

import httpx
import yaml

from app.core.config import settings
from app.core.errors import UpstreamUnavailable
from app.core.logging import get_logger
from app.speech.protocol import Transcript, Utterance

log = get_logger(__name__)

ASR_TASK = "asr"
TTS_TASK = "tts"
SERVICES_FILE = "config/bhashini-services.yaml"
WAV_MEDIA_TYPES = frozenset({"audio/wav", "audio/x-wav", "audio/wave"})


@lru_cache(maxsize=1)
def load_services() -> dict[str, dict[str, str]]:
    """`{task: {language: serviceId}}`. A language absent from a task is simply unsupported."""
    raw = yaml.safe_load(settings.path(SERVICES_FILE).read_text(encoding="utf-8")) or {}
    return {task: {str(k): str(v) for k, v in (raw.get(task) or {}).items()}
            for task in (ASR_TASK, TTS_TASK)}


def wav_sample_rate(audio: bytes) -> int | None:
    """The sample rate from a RIFF/WAVE `fmt ` chunk, or None if this is not a WAV.

    Read from the header rather than assumed: Dhruva's own TTS returns 22.05 kHz, and the old
    hard-coded `samplingRate: 16000` would have mislabelled exactly that audio."""
    if len(audio) < 12 or audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
        return None
    pos = 12
    while pos + 8 <= len(audio):
        size = int.from_bytes(audio[pos + 4 : pos + 8], "little")
        if audio[pos : pos + 4] == b"fmt " and pos + 16 <= len(audio):
            return int.from_bytes(audio[pos + 12 : pos + 16], "little")
        pos += 8 + size + (size & 1)
    return None


class BhashiniSpeechBackend:
    """Satisfies `SpeechBackend`."""

    name = "bhashini"
    offline = False
    languages: tuple[str, ...]

    def __init__(self) -> None:
        if not settings.bhashini_api_key:
            raise UpstreamUnavailable(
                "Bhashini is not configured (BHASHINI_API_KEY). The local backend is used instead."
            )
        self._services = load_services()
        supported = set(self._services[ASR_TASK]) | set(self._services[TTS_TASK])
        self.languages = tuple(sorted(supported))
        self._headers = {
            "Authorization": settings.bhashini_api_key,
            "userID": settings.bhashini_user_id or "",
            "Content-Type": "application/json",
        }

    def service_for(self, task: str, language: str) -> str | None:
        return self._services[task].get(language)

    def _call(self, task: str, config: dict[str, Any], input_data: dict[str, Any]) -> dict:
        try:
            response = httpx.post(
                settings.bhashini_base_url,
                json={
                    "pipelineTasks": [{"taskType": task, "config": config}],
                    "inputData": input_data,
                },
                headers=self._headers,
                timeout=settings.bhashini_timeout_seconds,
            )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise UpstreamUnavailable(f"Bhashini {task} call failed: {exc}") from exc
        if not isinstance(body, dict):
            raise UpstreamUnavailable(f"Bhashini {task} returned an unexpected body.")
        return body

    def transcribe(self, audio: bytes, *, language: str, media_type: str) -> Transcript:
        service = self.service_for(ASR_TASK, language)
        if service is None:
            raise UpstreamUnavailable(f"No verified Bhashini ASR service for {language!r}.")
        rate = wav_sample_rate(audio) if media_type in WAV_MEDIA_TYPES else None
        if rate is None:
            raise UpstreamUnavailable(f"Bhashini ASR here accepts WAV only; got {media_type!r}.")
        body = self._call(
            ASR_TASK,
            {
                "language": {"sourceLanguage": language},
                "serviceId": service,
                "audioFormat": "wav",
                "samplingRate": rate,
            },
            {"audio": [{"audioContent": base64.b64encode(audio).decode()}]},
        )
        output = (body.get("pipelineResponse") or [{}])[0].get("output") or [{}]
        text = str(output[0].get("source", "")).strip()
        # Dhruva returns no confidence, so this reports none. Assigning one — even the
        # threshold itself, as an earlier version did — would claim a measurement that was
        # never made. The caller handles `unavailable` explicitly.
        return Transcript(
            text=text,
            confidence=None,
            language=language,
            backend=self.name,
            empty=not text,
        )

    def synthesise(self, text: str, *, language: str) -> Utterance:
        service = self.service_for(TTS_TASK, language)
        if service is None:
            # No call is made, so there is nothing to audit: `model` stays None.
            return self._fallback(text, language, model=None)
        try:
            body = self._call(
                TTS_TASK,
                {
                    "language": {"sourceLanguage": language},
                    "serviceId": service,
                    "gender": "female",
                },
                {"input": [{"source": text}]},
            )
        except UpstreamUnavailable as exc:
            log.warning("speech.bhashini_tts_failed", language=language, error=str(exc)[:200])
            return self._fallback(text, language, model=service)
        output = (body.get("pipelineResponse") or [{}])[0].get("audio") or [{}]
        encoded = output[0].get("audioContent", "")
        return Utterance(
            audio=base64.b64decode(encoded) if encoded else b"",
            media_type="audio/wav",
            text=text,
            language=language,
            backend=self.name,
            client_fallback=not encoded,
            model=service,
        )

    def _fallback(self, text: str, language: str, *, model: str | None) -> Utterance:
        return Utterance(
            audio=b"",
            media_type="audio/wav",
            text=text,
            language=language,
            backend=self.name,
            client_fallback=True,
            model=model,
        )
