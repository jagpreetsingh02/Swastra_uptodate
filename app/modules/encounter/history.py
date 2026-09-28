"""Reading a patient's longitudinal record: timeline, medication history, similar encounters.

Everything here is a read over the durable tables. Three design decisions worth stating:

**Medication history reports provenance, not state.** `reconcile()` groups every mention of a
drug across every visit and says how each one is known — `documented`, `patient-reported-current`,
`historical`. It flags a drug as needing reconciliation when the sources disagree. It never
concludes that a medicine is currently being taken because it was once prescribed.

**Similar-encounter retrieval is deterministic and explainable.** Shared features are
computed by set intersection over recorded values, and the result *lists the features* rather
than reporting a percentage. There are no embeddings: they would be less explainable and, on
one patient's handful of encounters, no better.

**It never leaves the patient.** `similar_encounters()` filters on `patient_id` before it
compares anything. A retrieval that could surface another person's visit would be a
confidentiality breach dressed as a feature.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.durable import (
    ClinicalFactRecord,
    ContradictionRecord,
    DocumentRecord,
    Encounter,
    MedicationEvent,
    ObservationEvent,
    Patient,
    SourceEvidence,
    TimelineEventRecord,
)
from app.modules.encounter.promote import normalize_medicine as normalize

#: Paths compared when looking for a similar past visit. Deliberately short: the presenting
#: complaint and the features a clinician would actually recognise a recurrence by.
SIMILARITY_PATHS: tuple[str, ...] = (
    "chief_complaint.text",
    "hpi.site",
    "hpi.character",
    "hpi.radiation",
    "hpi.timing",
    "hpi.associated",
    "hpi.exacerbating",
    "past_medical.conditions",
)

#: Human labels for the shared-feature list, so the physician reads words not paths.
FEATURE_LABELS: dict[str, str] = {
    "chief_complaint.text": "presenting complaint",
    "hpi.site": "site",
    "hpi.character": "character",
    "hpi.radiation": "radiation",
    "hpi.timing": "timing",
    "hpi.associated": "associated symptom",
    "hpi.exacerbating": "aggravating factor",
    "past_medical.conditions": "known condition",
}


async def get_patient(db: AsyncSession, *, patient_ref: str) -> Patient | None:
    return (
        await db.execute(select(Patient).where(Patient.patient_ref == patient_ref))
    ).scalars().first()


async def get_patient_by_abha(db: AsyncSession, *, abha_ref: str) -> Patient | None:
    return (
        await db.execute(select(Patient).where(Patient.abha_ref == abha_ref))
    ).scalars().first()


async def encounters_for(db: AsyncSession, patient_id: int) -> list[Encounter]:
    return list(
        (
            await db.execute(
                select(Encounter)
                .where(Encounter.patient_id == patient_id)
                .order_by(Encounter.occurred_at.desc())
            )
        ).scalars().all()
    )


def _bounded(gate: asyncio.Semaphore):  # type: ignore[no-untyped-def]
    """Hold `gate` for the whole of the wrapped coroutine, session included.

    Acquiring around the *session* rather than around the query is the point: a connection is
    held from `async with maker()` to its close, so gating only the execute would let all four
    branches open a connection and merely queue their statements.
    """

    def decorate(fn):  # type: ignore[no-untyped-def]
        async def run():  # type: ignore[no-untyped-def]
            async with gate:
                return await fn()

        return run

    return decorate


async def overview(db: AsyncSession, patient: Patient) -> dict[str, Any]:
    """The patient home screen: what this person already has on file.

    FOUR INDEPENDENT READS, RUN CONCURRENTLY. Every query here is keyed on `patient.id` and
    none depends on another's result, but they used to run one after the other. Profiled
    against the live database that is the whole cost of this route: each query is index-backed
    and takes well under a millisecond, while the round-trip to Supabase is ~138 ms. Four
    sequential reads therefore spent about half a second doing nothing but waiting, and the
    endpoint measured 2.2 s end to end.

    BOUNDED TO TWO AT A TIME. `AsyncSession` is not safe for concurrent use — issuing four
    statements on one session raises "another operation is in progress" — so each branch
    takes its own short-lived session. Unbounded, that is four connections held by a single
    request against a 5 + 5 per-process ceiling, which is far too tight: two concurrent
    requests would exhaust the pool on their own.

    A semaphore of 2 keeps the win and drops the risk. Four sequential round-trips become
    two waves, so the latency saving is most of what full concurrency would give (2 x RTT
    instead of 4 x RTT), while a request never holds more than two connections. The sessions
    are read-only and short-lived, so they return to the pool immediately.
    """
    # The concurrent sessions are bound to the SAME engine the caller handed us, not to a
    # globally-resolved one. That distinction is load-bearing: reaching for the process-wide
    # sessionmaker made this function ignore its own `db` argument, which broke every test
    # that passes a session bound to its own in-memory engine — and, worse, would have made
    # the function silently invisible to a caller's open transaction.
    #
    # There is no fallback for an unbound session: `db.get_bind()` returns the *sync* Engine,
    # which `async_sessionmaker` cannot use. Every session here comes from an async engine.
    bind = db.bind
    if bind is None:
        raise RuntimeError("patient history needs a session bound to an async engine")
    maker = async_sessionmaker(bind, expire_on_commit=False, class_=AsyncSession)

    #: At most two of the four reads are in flight at once. See the docstring: this is the
    #: line that stops one patient-home request from claiming most of the connection pool.
    gate = asyncio.Semaphore(2)

    @_bounded(gate)
    async def _encounters() -> list[Encounter]:
        async with maker() as session:
            return await encounters_for(session, patient.id)

    @_bounded(gate)
    async def _documents() -> list[DocumentRecord]:
        async with maker() as session:
            return list(
                (
                    await session.execute(
                        select(DocumentRecord)
                        .join(Encounter, DocumentRecord.encounter_id == Encounter.id)
                        .where(Encounter.patient_id == patient.id)
                    )
                ).scalars().all()
            )

    @_bounded(gate)
    async def _medications() -> list[MedicationEvent]:
        async with maker() as session:
            return list(
                (
                    await session.execute(
                        select(MedicationEvent).where(MedicationEvent.patient_id == patient.id)
                    )
                ).scalars().all()
            )

    @_bounded(gate)
    async def _observations() -> list[ObservationEvent]:
        async with maker() as session:
            return list(
                (
                    await session.execute(
                        select(ObservationEvent).where(ObservationEvent.patient_id == patient.id)
                    )
                ).scalars().all()
            )

    encounters, documents, medications, observations = await asyncio.gather(
        _encounters(), _documents(), _medications(), _observations()
    )

    return {
        "patientRef": patient.patient_ref,
        "displayName": patient.display_name,
        "abhaMasked": _mask(patient.abha_ref),
        "ageYears": patient.age_years,
        "gender": patient.gender,
        "language": patient.preferred_language,
        "counts": {
            "encounters": len(encounters),
            "prescriptions": sum(1 for d in documents if d.document_kind == "prescription"),
            "labReports": sum(1 for d in documents if d.document_kind == "lab_report"),
            "otherDocuments": sum(
                1 for d in documents if d.document_kind not in ("prescription", "lab_report")
            ),
            "medications": len({m.normalized_name for m in medications}),
            "observations": len(observations),
        },
        "recent": [
            {
                "encounterRef": e.encounter_ref,
                "occurredOn": e.occurred_at.date().isoformat(),
                "headline": e.headline or "Clinical encounter",
                "priority": e.priority,
                "ayush": e.ayush_mode,
            }
            for e in encounters[:6]
        ],
    }


def _mask(abha_ref: str | None) -> str | None:
    """Show enough to recognise, not enough to identify."""
    if not abha_ref:
        return None
    tail = abha_ref[-4:]
    return f"**** **** {tail}"


async def timeline(
    db: AsyncSession, patient_id: int, *, kinds: list[str] | None = None
) -> list[dict[str, Any]]:
    """Every event across every confirmed encounter, newest first, undated last."""
    statement = select(TimelineEventRecord).where(TimelineEventRecord.patient_id == patient_id)
    if kinds:
        statement = statement.where(TimelineEventRecord.kind.in_(kinds))
    rows = list((await db.execute(statement)).scalars().all())

    encounters = {
        e.id: e for e in await encounters_for(db, patient_id)
    }

    def sort_key(row: TimelineEventRecord) -> tuple[int, float]:
        if row.occurred_on is None:
            return (1, 0.0)
        return (0, -row.occurred_on.toordinal())

    return [
        {
            "eventRef": row.event_ref,
            "occurredOn": row.occurred_on.isoformat() if row.occurred_on else None,
            "datePrecision": row.date_precision,
            "kind": row.kind,
            "label": row.label,
            "detail": row.detail,
            "documentRef": row.source_document_ref,
            "factRef": row.source_fact_ref,
            "lowConfidence": row.low_confidence,
            "encounterRef": (
                encounters[row.encounter_id].encounter_ref
                if row.encounter_id in encounters
                else None
            ),
        }
        for row in sorted(rows, key=sort_key)
    ]


@dataclass(slots=True)
class MedicationThread:
    """Every mention of one drug across every visit, and whether the sources agree."""

    name: str
    normalized: str
    mentions: list[dict[str, Any]] = field(default_factory=list)
    needs_reconciliation: bool = False
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "normalized": self.normalized,
            "mentions": self.mentions,
            "needsReconciliation": self.needs_reconciliation,
            "reason": self.reason,
        }


async def medication_history(
    db: AsyncSession, patient_id: int, *, live_values: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Group medications by drug across visits. Reports provenance, never current state.

    `live_values` is the ledger of a visit still in capture. Without it this function could
    only see the last *confirmed* encounter, so a patient denying medication in the session
    on screen produced a reconciliation banner at the top of the physician's view and
    "no reconciliation needed" in the medication panel underneath it — the same question
    answered two ways on one screen. Today's answers are the ones that matter for this.
    """
    rows = list(
        (
            await db.execute(
                select(MedicationEvent)
                .where(MedicationEvent.patient_id == patient_id)
                .order_by(MedicationEvent.observed_on, MedicationEvent.recorded_at)
            )
        ).scalars().all()
    )
    encounters = {e.id: e for e in await encounters_for(db, patient_id)}

    threads: dict[str, MedicationThread] = {}
    for row in rows:
        thread = threads.setdefault(
            row.normalized_name, MedicationThread(name=row.name, normalized=row.normalized_name)
        )
        encounter = encounters.get(row.encounter_id)
        thread.mentions.append(
            {
                "status": row.status,
                "dose": row.dose,
                "frequency": row.frequency,
                "observedOn": row.observed_on.isoformat() if row.observed_on else None,
                "recordedAt": row.recorded_at.isoformat() if row.recorded_at else None,
                "documentRef": row.source_document_ref,
                "encounterRef": encounter.encounter_ref if encounter else None,
                "encounterOn": (
                    encounter.occurred_at.date().isoformat() if encounter else None
                ),
                "howWeKnow": _how_we_know(row.status),
            }
        )

    if live_values is not None:
        denial = live_values.get("drug_allergy.taking_medicines") is False
    else:
        latest_encounter = max(encounters.values(), key=lambda e: e.occurred_at, default=None)
        denial = await _denies_medication(db, latest_encounter)

    for thread in threads.values():
        statuses = {m["status"] for m in thread.mentions}
        # Documented in the past, and the patient now says they take nothing.
        if denial and "documented" in statuses:
            thread.needs_reconciliation = True
            thread.reason = (
                "A document records this medicine, and the patient reported taking none at "
                f"{'this visit' if live_values is not None else 'the most recent visit'}. "
                "Needs medication reconciliation."
            )
        elif {"documented", "stopped-reported"} <= statuses:
            thread.needs_reconciliation = True
            thread.reason = "Documented, and separately reported as stopped."

    return [thread.to_dict() for thread in threads.values()]


def _how_we_know(status: str) -> str:
    return {
        "documented": "found in an uploaded document",
        "patient-reported-current": "the patient said they take this",
        "historical": "recorded at a previous visit, not mentioned since",
        "stopped-reported": "the patient said they stopped",
        "uncertain": "source unclear",
    }.get(status, status)


async def reconcile_live_session(
    db: AsyncSession, *, patient_id: int, values: dict[str, Any]
) -> list[dict[str, Any]]:
    """Compare what the patient is saying *today* against what their record already holds.

    §16: cross-visit contradiction. The single case worth catching first is the one that
    changes prescribing — a patient reporting no medicines while a prescription for
    metformin sits in their own history.

    Nothing here decides which side is right, and the wording is deliberate about that. The
    patient may have stopped the drug, the document may be stale, or the question may have
    been misunderstood. All three are common and only the physician can tell them apart, so
    the output names both sources and asks for reconciliation.

    A rule, not the LLM. The comparison is a set difference over recorded values; a model
    would add latency, a failure mode and no accuracy on a question this literal.
    """
    findings: list[dict[str, Any]] = []

    denies_now = values.get("drug_allergy.taking_medicines") is False
    reported_now = {
        normalize(str(v))
        for path, v in values.items()
        if path.startswith("medications[") and path.endswith(".name") and v
    }

    threads = await medication_history(db, patient_id, live_values=values)
    documented = [t for t in threads if any(m["status"] == "documented" for m in t["mentions"])]

    if denies_now and documented:
        findings.append(
            {
                "kind": "medication_reconciliation",
                "currentStatement": "The patient reported taking no medicines today.",
                "historicalEvidence": [
                    {
                        "name": thread["name"],
                        "mentions": [
                            m for m in thread["mentions"] if m["status"] == "documented"
                        ],
                    }
                    for thread in documented
                ],
                "status": "Needs medication reconciliation",
                "note": (
                    "Both are recorded and neither has been overridden. The patient may have "
                    "stopped the medicine, the document may be out of date, or the question "
                    "may have been misunderstood."
                ),
            }
        )

    for thread in documented:
        if denies_now or normalize(thread["name"]) in reported_now:
            continue
        findings.append(
            {
                "kind": "medication_not_mentioned",
                "currentStatement": (
                    f"{thread['name']} was not mentioned at this visit."
                ),
                "historicalEvidence": [
                    {
                        "name": thread["name"],
                        "mentions": [
                            m for m in thread["mentions"] if m["status"] == "documented"
                        ],
                    }
                ],
                "status": "Confirm whether this is still being taken",
                "note": (
                    "A past prescription is not evidence of current use. Silence is not "
                    "evidence of stopping either."
                ),
            }
        )

    return findings


async def _denies_medication(db: AsyncSession, encounter: Encounter | None) -> bool:
    if encounter is None:
        return False
    row = (
        await db.execute(
            select(ClinicalFactRecord).where(
                ClinicalFactRecord.encounter_id == encounter.id,
                ClinicalFactRecord.path == "drug_allergy.taking_medicines",
            )
        )
    ).scalars().first()
    return bool(row and (row.value_json or {}).get("v") is False)


@dataclass(slots=True)
class SimilarEncounter:
    encounter_ref: str
    occurred_on: str
    headline: str | None
    shared: list[dict[str, str]]
    #: A count of shared features, NOT a probability and NOT a clinical judgement.
    shared_count: int
    band: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "encounterRef": self.encounter_ref,
            "occurredOn": self.occurred_on,
            "headline": self.headline,
            "shared": self.shared,
            "sharedCount": self.shared_count,
            "band": self.band,
            "note": (
                "A count of features this visit shares with that one. Not a probability, "
                "not a diagnosis, and not a clinical judgement."
            ),
        }


async def _feature_set(db: AsyncSession, encounter_id: int) -> dict[str, set[str]]:
    rows = list(
        (
            await db.execute(
                select(ClinicalFactRecord).where(
                    ClinicalFactRecord.encounter_id == encounter_id,
                    ClinicalFactRecord.path.in_(SIMILARITY_PATHS),
                )
            )
        ).scalars().all()
    )
    features: dict[str, set[str]] = {}
    for row in rows:
        raw = (row.value_json or {}).get("v")
        values = raw if isinstance(raw, list) else [raw]
        cleaned = {str(v) for v in values if v not in (None, "", "none")}
        if cleaned:
            features.setdefault(row.path, set()).update(cleaned)
    return features


def _band(count: int) -> str:
    """Words, not a percentage. A number here invites being read as a probability."""
    if count >= 4:
        return "many shared features"
    if count >= 2:
        return "some shared features"
    return "one shared feature"


async def similar_encounters(
    db: AsyncSession,
    *,
    patient_id: int,
    current_features: dict[str, set[str]],
    exclude_encounter_id: int | None = None,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Prior visits of THE SAME PATIENT that share recorded features with this one."""
    candidates = [
        e
        for e in await encounters_for(db, patient_id)
        if e.id != exclude_encounter_id
    ]

    scored: list[SimilarEncounter] = []
    for candidate in candidates:
        past = await _feature_set(db, candidate.id)
        shared: list[dict[str, str]] = []
        for path, values in current_features.items():
            overlap = values & past.get(path, set())
            for value in sorted(overlap):
                shared.append(
                    {"feature": FEATURE_LABELS.get(path, path), "value": value, "path": path}
                )
        if not shared:
            continue
        scored.append(
            SimilarEncounter(
                encounter_ref=candidate.encounter_ref,
                occurred_on=candidate.occurred_at.date().isoformat(),
                headline=candidate.headline,
                shared=shared,
                shared_count=len(shared),
                band=_band(len(shared)),
            )
        )

    scored.sort(key=lambda s: (-s.shared_count, s.occurred_on))
    return [entry.to_dict() for entry in scored[:limit]]


async def current_features(db: AsyncSession, encounter_id: int) -> dict[str, set[str]]:
    return await _feature_set(db, encounter_id)


async def features_from_ledger(paths_and_values: dict[str, Any]) -> dict[str, set[str]]:
    """Feature set for a session still in capture, so 'similar visits' works before commit."""
    features: dict[str, set[str]] = {}
    for path, raw in paths_and_values.items():
        if path not in SIMILARITY_PATHS:
            continue
        values = raw if isinstance(raw, list) else [raw]
        cleaned = {str(v) for v in values if v not in (None, "", "none")}
        if cleaned:
            features.setdefault(path, set()).update(cleaned)
    return features


async def evidence_for_fact(
    db: AsyncSession, *, encounter_id: int, fact_ref: str
) -> dict[str, Any] | None:
    """Click-to-source for a durable fact, including a link to the document page."""
    fact = (
        await db.execute(
            select(ClinicalFactRecord).where(
                ClinicalFactRecord.encounter_id == encounter_id,
                ClinicalFactRecord.fact_ref == fact_ref,
            )
        )
    ).scalars().first()
    if fact is None:
        return None
    evidence = list(
        (
            await db.execute(select(SourceEvidence).where(SourceEvidence.fact_id == fact.id))
        ).scalars().all()
    )
    return {
        "factRef": fact.fact_ref,
        "path": fact.path,
        "value": (fact.value_json or {}).get("v"),
        "displayValue": fact.display_value,
        "tier": fact.tier,
        "confidence": fact.confidence,
        "confidenceStatus": fact.confidence_status,
        "confirmedByPhysician": fact.confirmed_by_physician,
        "evidence": [
            {
                "sourceType": e.source_type,
                "verbatim": e.verbatim,
                "language": e.language,
                "modality": e.modality,
                "questionId": e.question_id,
                "asrConfidence": e.asr_confidence,
                "documentRef": e.document_ref,
                "page": e.page,
                "bbox": e.bbox_json,
                "ocrConfidence": e.ocr_confidence,
                "handwritten": e.handwritten,
                # Shown beside the scrawl, never instead of it: the drawer says what OCR
                # read, what a person read it as, and whose name is on that reading.
                "humanReading": e.human_reading,
                "readBy": e.read_by,
            }
            for e in evidence
        ],
    }


async def open_contradictions(db: AsyncSession, patient_id: int) -> list[dict[str, Any]]:
    rows = list(
        (
            await db.execute(
                select(ContradictionRecord).where(
                    ContradictionRecord.patient_id == patient_id,
                    ContradictionRecord.status == "open",
                )
            )
        ).scalars().all()
    )
    return [
        {
            "contradictionRef": row.contradiction_ref,
            "ruleId": row.rule_id,
            "label": row.label,
            "patientSide": row.side_a_json,
            "documentSide": row.side_b_json,
            "clarifyingQuestion": row.clarifying_question,
            "status": row.status,
        }
        for row in rows
    ]


def latest_observation_by_analyte(rows: list[ObservationEvent]) -> dict[str, ObservationEvent]:
    latest: dict[str, ObservationEvent] = {}
    for row in rows:
        key = row.analyte_key or row.display.casefold()
        current = latest.get(key)
        if current is None or _observed(row) >= _observed(current):
            latest[key] = row
    return latest


def _observed(row: ObservationEvent) -> date:
    return row.observed_on or date.min
