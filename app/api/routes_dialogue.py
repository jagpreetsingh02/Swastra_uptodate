"""The kiosk's main loop: ask a question, take an answer, ask the next one.

Every answer route funnels into `record_fact()`. `POST /answer` handles tap and typed input;
`POST /answer/voice` handles speech and applies the degradation policy. They are separate
routes because they have genuinely different failure modes, and collapsing them would hide
the degradation path behind an optional field.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body

from app.api.deps import CurrentIdentity, DbSession, load_context, save_context
from app.audit.chain import record, record_ai_call
from app.contracts.provenance import Modality
from app.core.errors import ConsentRequired, ValidationError
from app.modules.dialogue.answers import record_answer, record_derived
from app.modules.dialogue.voice import handle_spoken_answer
from app.redflags.engine import evaluate, raise_priority
from app.speech.registry import get_client_backend, get_speech

router = APIRouter(prefix="/api/v1/sessions/{session_ref}/dialogue", tags=["dialogue"])


async def _next_payload(db, context) -> dict[str, Any]:
    """Advance the machine, recording any derived answers first."""
    for question, value, _code in context.machine.derived_questions():
        record_derived(context.machine, context.ledger, question, value)

    question = context.machine.next_question()
    if question is None:
        context.row.status = "ready_for_review"
        return {
            "complete": True,
            "question": None,
            "progress": context.machine.progress(),
            "sections": context.machine.section_progress(),
        }
    # A reopened question is being corrected, not asked for the first time. Sending the
    # answer already on file lets the kiosk show it selected, so the patient can see what
    # they are changing rather than facing a blank screen that looks like lost work.
    reopened = question.question_id in context.machine.state.reopened
    return {
        "complete": False,
        "question": question.to_dict(),
        "progress": context.machine.progress(),
        "sections": context.machine.section_progress(),
        "reopened": reopened,
        "currentAnswer": context.machine.current_answer(question.question_id) if reopened else None,
        "canGoBack": context.machine.previous_answered() is not None,
    }


@router.get("/next")
async def next_question(
    db: DbSession, session_ref: str, identity: CurrentIdentity
) -> dict[str, Any]:
    context = await load_context(db, session_ref, identity=identity)
    payload = await _next_payload(db, context)
    await save_context(db, context)
    return payload


@router.post("/answer")
async def answer(
    db: DbSession,
    session_ref: str,
    identity: CurrentIdentity,
    payload: Annotated[dict, Body()],
) -> dict[str, Any]:
    """Tapped or typed. The two modalities that always work, with or without a network."""
    context = await load_context(db, session_ref, identity=identity)
    if "history" not in context.ledger.consent_scopes:
        raise ConsentRequired("The history scope was not granted for this session.")

    turn_id = str(payload.get("turnId") or "")
    question_id = str(payload.get("questionId") or "")
    if not turn_id or not question_id:
        raise ValidationError("turnId and questionId are both required.")

    modality = Modality(str(payload.get("modality", "touch")))
    if modality is Modality.SPEECH:
        raise ValidationError("Use POST /answer/voice for spoken answers.")

    facts = record_answer(
        context.machine,
        context.ledger,
        turn_id=turn_id,
        question_id=question_id,
        value=payload.get("value"),
        modality=modality,
        language=context.row.language,
    )

    escalation = _escalate(context)
    await record(
        db,
        actor=identity.actor,
        actor_role=identity.role,
        purpose_of_use="TREATMENT",
        action="dialogue.answer",
        abha_ref=context.row.abha_ref,
        consent_ref=context.row.consent_ref,
        request_summary={"questionId": question_id, "modality": modality.value},
        response_summary={"factsRecorded": len(facts), "priority": context.row.priority},
    )

    result = await _next_payload(db, context)
    await save_context(db, context)
    return {
        **result,
        "recorded": [{"factId": f.fact_id, "path": f.path, "tier": f.tier.value} for f in facts],
        "escalation": escalation,
    }


@router.post("/answer/voice")
async def answer_voice(
    db: DbSession,
    session_ref: str,
    identity: CurrentIdentity,
    payload: Annotated[dict, Body()],
) -> dict[str, Any]:
    """A spoken answer. Below the confidence threshold this degrades to touch and records
    nothing — the response carries `degradedToTouch` and the re-presented question."""
    context = await load_context(db, session_ref, identity=identity)
    if "voice" not in context.ledger.consent_scopes:
        raise ConsentRequired(
            "The voice scope was not granted. The patient can answer everything by tapping."
        )

    turn_id = str(payload.get("turnId") or "")
    question_id = str(payload.get("questionId") or "")
    if not turn_id or not question_id:
        raise ValidationError("turnId and questionId are both required.")

    raw_confidence = payload.get("confidence")
    transcript = get_client_backend().accept(
        text=str(payload.get("transcript", "")),
        # `null` from the client stays None. See Transcript.confidence.
        confidence=None if raw_confidence is None else float(raw_confidence),
        language=context.row.language,
    )

    outcome = handle_spoken_answer(
        context.machine,
        context.ledger,
        turn_id=turn_id,
        question_id=question_id,
        transcript=transcript,
        audio_ref=payload.get("audioRef"),
        barge_in=bool(payload.get("bargeIn", False)),
    )

    if outcome.extraction and outcome.extraction.llm_response is not None:
        response = outcome.extraction.llm_response
        await record_ai_call(
            db,
            actor=identity.actor,
            actor_role=identity.role,
            action="llm.extract",
            model_name=response.model_name,
            model_version=response.model_version,
            prompt=response.prompt,
            abha_ref=context.row.abha_ref,
            consent_ref=context.row.consent_ref,
            response_summary={
                "accepted": outcome.extraction.accepted,
                "rejectedUnquoted": len(outcome.extraction.rejected_unquoted),
            },
        )

    escalation = _escalate(context)
    await record(
        db,
        actor=identity.actor,
        actor_role=identity.role,
        purpose_of_use="TREATMENT",
        action="dialogue.answer.voice",
        abha_ref=context.row.abha_ref,
        consent_ref=context.row.consent_ref,
        request_summary={
            "questionId": question_id,
            "asrConfidence": (
                round(transcript.confidence, 3) if transcript.confidence is not None else None
            ),
            "confidenceStatus": transcript.confidence_status,
        },
        response_summary={
            "accepted": outcome.accepted,
            "degraded": outcome.degraded_to_touch,
        },
    )

    result = await _next_payload(db, context)
    await save_context(db, context)
    return {**result, "voice": outcome.to_dict(), "escalation": escalation}


@router.post("/skip")
async def skip(
    db: DbSession,
    session_ref: str,
    identity: CurrentIdentity,
    payload: Annotated[dict, Body()],
) -> dict[str, Any]:
    """The patient declined. Recorded as an explicit absence, never as a value."""
    context = await load_context(db, session_ref, identity=identity)
    question_id = str(payload.get("questionId") or "")
    if not question_id:
        raise ValidationError("questionId is required.")
    context.machine.decline(question_id)
    result = await _next_payload(db, context)
    await save_context(db, context)
    return {**result, "declined": question_id}


@router.get("/review")
async def review(
    db: DbSession, session_ref: str, identity: CurrentIdentity
) -> dict[str, Any]:
    """What the patient told us, in the words they saw — for them to check before it is sent.

    This is not the doctor's summary. It is the cheapest possible guard against a mishearing
    reaching a physician: the person who said it reads it back.
    """
    context = await load_context(db, session_ref, identity=identity)
    return {
        "sessionRef": session_ref,
        "answers": context.machine.answered_summary(),
        "language": context.row.language,
    }


@router.post("/reopen")
async def reopen(
    db: DbSession,
    session_ref: str,
    identity: CurrentIdentity,
    payload: Annotated[dict, Body()],
) -> dict[str, Any]:
    """Re-present one question so the patient can correct it. The old answer is superseded,
    not deleted — the physician sees the correction and what it corrected."""
    context = await load_context(db, session_ref, identity=identity)
    question_id = str(payload.get("questionId") or "")
    if not context.machine.reopen(question_id):
        raise ValidationError(f"{question_id!r} is not a question in this interview.")
    result = await _next_payload(db, context)
    await save_context(db, context)
    return {**result, "reopened": question_id}


@router.post("/back")
async def back(
    db: DbSession, session_ref: str, identity: CurrentIdentity
) -> dict[str, Any]:
    """Reopen the previous answered question so the patient can change their answer.

    Back is `reopen()` aimed at whatever the patient last answered, which is why there is no
    new correction machinery here. Re-answering supersedes the old fact through the ordinary
    ledger path — the previous answer stays in the record, marked superseded, and the
    physician sees both it and the correction. Nothing is deleted and nothing is edited in
    place, because a patient changing their mind is clinically interesting.

    Branching recalculates itself: `next_question()` re-evaluates every condition against the
    current values on each call, so an answer that opens or closes a later section takes
    effect the moment it changes. Answers already given to questions that a new answer makes
    irrelevant stay in the ledger — superseding a fact the patient never retracted would be
    inventing a retraction.
    """
    context = await load_context(db, session_ref, identity=identity)
    target = context.machine.previous_answered()
    if target is None:
        raise ValidationError(
            "There is no earlier question to go back to — this is the first one."
        )
    context.machine.reopen(target)
    result = await _next_payload(db, context)
    await save_context(db, context)
    return {**result, "reopened": target}


@router.post("/speak")
async def speak(
    db: DbSession,
    session_ref: str,
    identity: CurrentIdentity,
    payload: Annotated[dict, Body()],
) -> dict[str, Any]:
    """Synthesise a prompt. Returns `clientFallback` when the kiosk must use its own voice."""
    context = await load_context(db, session_ref, identity=identity)
    text = str(payload.get("text", "")).strip()
    if not text:
        raise ValidationError("text is required.")
    utterance = get_speech().synthesise(text, language=context.row.language)
    if utterance.model is not None:
        # Invariant 6. A hosted TTS model is an AI call like any other, including when it
        # failed — the attempt left the building either way.
        await record_ai_call(
            db,
            actor=identity.actor,
            actor_role=identity.role,
            action="speech.synthesise",
            model_name=utterance.model,
            model_version=utterance.backend,
            prompt=text,
            abha_ref=context.row.abha_ref,
            consent_ref=context.row.consent_ref,
            outcome="failure" if utterance.client_fallback else "success",
            response_summary={"language": utterance.language, "bytes": len(utterance.audio)},
        )
    import base64

    return {
        "backend": utterance.backend,
        "language": utterance.language,
        "clientFallback": utterance.client_fallback,
        "mediaType": utterance.media_type,
        "audioBase64": (base64.b64encode(utterance.audio).decode() if utterance.audio else None),
        "text": utterance.text,
    }


def _escalate(context) -> dict[str, Any]:
    """Re-run the rule engine after every answer. Additive: priority can only go up."""
    escalation = evaluate(
        context.ledger,
        current_priority=context.row.priority,
        extra_values=context.state.values,
    )
    context.row.priority = raise_priority(context.row.priority, escalation.priority).label
    return escalation.to_dict()
