"""Bhashini, against a stubbed `httpx.post`. The live checks behind these are in ADR-0018.

Every test here guards a fault that was real: the 404 endpoint, the one-service-ID-for-all
mistake, a hang that stalled the patient, a hard-coded sample rate, and a hosted AI call that
never reached the audit log.
"""

from __future__ import annotations

import base64
import io
import wave
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from app.core.config import SUPPORTED_LANGUAGES
from app.core.errors import UpstreamUnavailable
from app.speech import bhashini as B
from tests.test_authorization import (
    _auth,
    _login,
    _start_session,
    client,  # noqa: F401 — cross-module fixture import
)

FAKE_AUDIO = b"RIFF-not-really-but-bytes"


class _Stub:
    """Records what would have gone over the wire, and answers or fails as told."""

    def __init__(self, *, fail: Exception | None = None, body: dict | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail = fail
        self.body = body

    def __call__(self, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append({"url": url, **kwargs})
        if self.fail is not None:
            raise self.fail
        task = kwargs["json"]["pipelineTasks"][0]["taskType"]
        audio = base64.b64encode(FAKE_AUDIO).decode()
        body = self.body or (
            {"pipelineResponse": [{"audio": [{"audioContent": audio}]}]}
            if task == "tts"
            else {"pipelineResponse": [{"output": [{"source": "heard this"}]}]}
        )
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))


@pytest.fixture
def keyed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(B.settings, "bhashini_api_key", "test-inference-key")


def _stub(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> _Stub:
    stub = _Stub(**kwargs)
    monkeypatch.setattr(B.httpx, "post", stub)
    return stub


def _wav(rate: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * 160)
    return buffer.getvalue()


# ------------------------------------------------------------------ configuration


def test_the_endpoint_is_the_compute_pipeline_not_the_404() -> None:
    assert B.settings.bhashini_base_url.endswith("/services/inference/pipeline")


def test_every_language_the_kiosk_offers_can_be_spoken() -> None:
    missing = sorted(set(SUPPORTED_LANGUAGES) - set(B.load_services()["tts"]))
    assert not missing, f"no Bhashini TTS service configured for {missing}"


def test_the_service_id_depends_on_task_and_language_family() -> None:
    """The original bug: one ID for everything."""
    services = B.load_services()
    assert services["asr"]["hi"] != services["tts"]["hi"]
    assert services["tts"]["hi"] != services["tts"]["ta"]


def test_no_key_means_unavailable_so_the_registry_falls_back(monkeypatch) -> None:
    monkeypatch.setattr(B.settings, "bhashini_api_key", None)
    with pytest.raises(UpstreamUnavailable):
        B.BhashiniSpeechBackend()


# ------------------------------------------------------------------ synthesis


def test_synthesis_sends_the_right_service_and_returns_audio(keyed, monkeypatch) -> None:
    stub = _stub(monkeypatch)
    utterance = B.BhashiniSpeechBackend().synthesise("नमस्ते", language="hi")

    (call,) = stub.calls
    assert call["url"] == B.settings.bhashini_base_url
    assert call["headers"]["Authorization"] == "test-inference-key"
    assert call["timeout"] == B.settings.bhashini_timeout_seconds
    config = call["json"]["pipelineTasks"][0]["config"]
    assert config["serviceId"] == B.load_services()["tts"]["hi"]
    assert config["language"] == {"sourceLanguage": "hi"}

    assert utterance.audio == FAKE_AUDIO
    assert not utterance.client_fallback
    assert utterance.model == config["serviceId"]


def test_a_hung_call_falls_back_instead_of_stalling_the_patient(keyed, monkeypatch) -> None:
    """Dhruva fails by hanging, not by erroring. The patient must get the on-screen question,
    not a 503 — and the attempt must still be named, so it is audited."""
    _stub(monkeypatch, fail=httpx.ReadTimeout("hung"))
    utterance = B.BhashiniSpeechBackend().synthesise("Hello", language="en")
    assert utterance.client_fallback
    assert utterance.audio == b""
    assert utterance.model == B.load_services()["tts"]["en"]


def test_an_empty_response_is_a_fallback_not_silence(keyed, monkeypatch) -> None:
    _stub(monkeypatch, body={"pipelineResponse": [{"audio": [{}]}]})
    assert B.BhashiniSpeechBackend().synthesise("Hello", language="en").client_fallback


def test_an_unsupported_language_makes_no_call_and_nothing_to_audit(keyed, monkeypatch) -> None:
    stub = _stub(monkeypatch)
    utterance = B.BhashiniSpeechBackend().synthesise("Hello", language="xx")
    assert stub.calls == []
    assert utterance.client_fallback
    assert utterance.model is None


# ------------------------------------------------------------------ recognition


def test_wav_sample_rate_is_read_from_the_header() -> None:
    assert B.wav_sample_rate(_wav(22050)) == 22050
    assert B.wav_sample_rate(_wav(16000)) == 16000
    assert B.wav_sample_rate(b"not audio at all") is None


def test_recognition_labels_audio_with_its_real_rate(keyed, monkeypatch) -> None:
    """The old code claimed 16 kHz for everything; Dhruva's own TTS output is 22.05 kHz."""
    stub = _stub(monkeypatch)
    transcript = B.BhashiniSpeechBackend().transcribe(
        _wav(22050), language="ta", media_type="audio/wav"
    )
    config = stub.calls[0]["json"]["pipelineTasks"][0]["config"]
    assert config["samplingRate"] == 22050
    assert config["serviceId"] == B.load_services()["asr"]["ta"]
    assert transcript.text == "heard this"
    assert transcript.confidence is None, "Dhruva reports no confidence; none may be invented"


def test_recognition_refuses_audio_it_cannot_describe(keyed, monkeypatch) -> None:
    stub = _stub(monkeypatch)
    with pytest.raises(UpstreamUnavailable):
        B.BhashiniSpeechBackend().transcribe(b"webm", language="hi", media_type="audio/webm")
    assert stub.calls == []


def test_the_seeder_never_sends_audio_to_a_hosted_engine(monkeypatch) -> None:
    from app.modules.encounter import seed as S

    class Hosted:
        name = "hosted"
        offline = False

        def transcribe(self, *_a: Any, **_kw: Any) -> None:
            raise AssertionError("the seeder called a hosted engine at boot")

    monkeypatch.setattr("app.speech.registry.get_speech", lambda: Hosted())
    _text, confidence, status, _ms = S.transcribe_seed_voice()
    assert confidence is None
    assert status == "unavailable"


# ------------------------------------------------------------------ the route (Invariant 6)


async def _speak_audit_rows() -> list:
    from app.db.models import AuditEvent
    from app.db.session import get_sessionmaker

    async with get_sessionmaker()() as db:
        query = select(AuditEvent).where(AuditEvent.action == "speech.synthesise")
        result = await db.execute(query)
        return list(result.scalars())


@pytest.mark.parametrize(
    ("fail", "outcome"), [(None, "success"), (httpx.ReadTimeout("hung"), "failure")]
)
async def test_every_hosted_tts_call_lands_in_the_audit_chain(
    client,  # noqa: F811
    keyed,
    monkeypatch,
    fail: Exception | None,
    outcome: str,
) -> None:
    _stub(monkeypatch, fail=fail)
    backend = B.BhashiniSpeechBackend()
    monkeypatch.setattr("app.api.routes_dialogue.get_speech", lambda: backend)

    token = await _login(client, "kamala.devi@abdm")
    session_ref = await _start_session(client, token)
    response = await client.post(
        f"/api/v1/sessions/{session_ref}/dialogue/speak",
        headers=_auth(token),
        json={"text": "What is troubling you today?"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["backend"] == "bhashini"
    assert body["clientFallback"] is (fail is not None)
    assert (body["audioBase64"] is None) is (fail is not None)

    (row,) = await _speak_audit_rows()
    assert row.model_name == B.load_services()["tts"]["en"]
    assert row.outcome == outcome
    assert row.prompt_hash, "the prompt is fingerprinted, never stored"


async def test_a_backend_that_calls_no_model_writes_no_ai_row(client) -> None:  # noqa: F811
    token = await _login(client, "kamala.devi@abdm")
    session_ref = await _start_session(client, token)
    response = await client.post(
        f"/api/v1/sessions/{session_ref}/dialogue/speak",
        headers=_auth(token),
        json={"text": "What is troubling you today?"},
    )
    assert response.status_code == 200, response.text
    assert await _speak_audit_rows() == []
