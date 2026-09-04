"""Upload → OCR → entities → facts → timeline. Module B, end to end.

Every fact written here is `document` tier and carries page + bounding box, so clicking a
line on the physician screen scrolls the scan to the exact region it came from.

The handwriting lane is enforced structurally: `ingest()` writes facts for confident entities
only. Low-confidence ones are returned separately and become facts *only* when a human calls
`verify_entity()`. There is no code path from a handwritten scrawl to the record without a
person in between.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.contracts.history import DocumentRef, TimelineEvent, api_dump
from app.contracts.provenance import DocumentSpan, Fact, SourceTier
from app.contracts.record import FactLedger, record_fact
from app.core.config import settings
from app.core.errors import ValidationError
from app.core.logging import get_logger
from app.modules.documents.backends import OCRResult, get_ocr_backend, read_document
from app.modules.documents.entities import ExtractedEntity, extract_entities, page_summary
from app.modules.documents.timeline import build_timeline, order_timeline

log = get_logger(__name__)

#: Where each entity kind lands in the history. Repeating groups, so index-addressed.
_GROUP_FOR = {
    "medication": ("medications", "name"),
    "diagnosis": ("problems", "reported_term"),
    "investigation": ("investigations", "analyte"),
    "procedure": ("procedures", "name"),
}


@dataclass(slots=True)
class IngestResult:
    document_id: str
    filename: str
    backend: str
    pages: list[dict[str, Any]]
    mean_confidence: float
    facts: list[Fact] = field(default_factory=list)
    timeline: list[TimelineEvent] = field(default_factory=list)
    #: The handwriting lane. Never merged without a human.
    needs_verification: list[dict[str, Any]] = field(default_factory=list)
    entities: list[ExtractedEntity] = field(default_factory=list)
    ocr: OCRResult | None = None

    def extracted_items(self) -> list[dict[str, Any]]:
        """Everything OCR found, in one list the patient can read back.

        The response used to carry only `needsVerification`, which is why no screen could
        ever show a patient what was read off their prescription: the *successful*
        extractions were dropped on the way out of the API. A verification screen that shows
        only the failures teaches a patient that the machine got everything else right.

        `itemId` addresses an item across both lanes, because "the third medicine" means
        nothing once the two lists are interleaved by confidence.
        """
        items: list[dict[str, Any]] = [
            {
                **entity.to_dict(),
                "itemId": f"recorded:{position}",
                "pending": False,
                "confidenceBand": confidence_band(entity.confidence, entity.handwritten),
            }
            for position, entity in enumerate(self.entities)
        ]
        items += [
            {
                **raw,
                "itemId": f"pending:{raw['entityIndex']}",
                "pending": True,
                # Anything in this lane is by definition below the threshold or handwritten.
                "confidenceBand": "verify",
            }
            for raw in self.needs_verification
        ]
        return items

    def to_dict(self) -> dict[str, Any]:
        return {
            "documentId": self.document_id,
            "filename": self.filename,
            "backend": self.backend,
            "pages": self.pages,
            "meanConfidence": round(self.mean_confidence, 4),
            "factsRecorded": len(self.facts),
            "timeline": [api_dump(e) for e in self.timeline],
            "needsVerification": self.needs_verification,
            "lowConfidenceCount": len(self.needs_verification),
            "extracted": self.extracted_items(),
            "documentKind": classify_document(self.extracted_items()),
            # UNDER-RESOLUTION IS A MORE ACTIONABLE CAUSE THAN "nothing found".
            #
            # Both produce an empty extraction, and the UI would otherwise say "we could not
            # find any printed writing" — true, but it sends the patient looking for a
            # different paper when the fix is to stand closer. The imaging step already knows
            # the page was below the resolution text can be resolved at; this carries that
            # forward so the failure screen can say the useful thing instead of the accurate
            # one. Null when the reading was not from an image at all.
            "tooSmall": (
                self.ocr.preparation.too_small
                if self.ocr is not None and self.ocr.preparation is not None
                else None
            ),
            # THE REGIONS ARE THE EVIDENCE, and they ship whether or not anything was
            # extracted from them. A page where OCR read nine lines and the entity rules
            # matched two of them is not an empty result — it is seven lines a doctor can
            # still look at, boxed, on the page they came from.
            "ocrRegions": self.ocr_regions(),
            "quality": (
                self.ocr.quality.to_dict()
                if self.ocr is not None and self.ocr.quality is not None
                else None
            ),
        }

    def ocr_regions(self) -> list[dict[str, Any]]:
        """Every detected region, with the text read from it and where it sits on the page.

        This is what the review screen draws its overlay from, and it is deliberately the
        SAME data the facts were built from rather than a parallel description of it — a
        frontend-only visualisation would drift from the evidence the moment either changed.
        """
        if self.ocr is None:
            return []
        # Which entities came from which region, so clicking a box can name the medicines it
        # produced and clicking a medicine can find its box.
        by_region: dict[int, list[str]] = {}
        for position, entity in enumerate(self.entities):
            if entity.region_id is not None:
                by_region.setdefault(entity.region_id, []).append(f"recorded:{position}")
        for raw in self.needs_verification:
            region_id = raw.get("regionId")
            if region_id is not None:
                by_region.setdefault(int(region_id), []).append(
                    f"pending:{raw['entityIndex']}"
                )

        regions: list[dict[str, Any]] = []
        for page in self.ocr.pages:
            for block in page.blocks:
                if block.region_id is None:
                    continue
                regions.append(
                    {
                        "regionId": block.region_id,
                        "documentId": self.document_id,
                        "page": page.page,
                        "bbox": block.bbox.model_dump(),
                        "text": block.text,
                        # `null`, never a stand-in number, when the recogniser exposed no
                        # trustworthy score. See OCRBlock.reported_confidence.
                        "confidence": block.reported_confidence,
                        "confidenceBand": region_band(block),
                        "backend": block.engine or self.backend,
                        "cropWidth": block.crop_width,
                        "cropHeight": block.crop_height,
                        "failure": block.failure,
                        "itemIds": by_region.get(block.region_id, []),
                    }
                )
        return regions

    def document_ref(self) -> DocumentRef:
        return DocumentRef(
            document_id=self.document_id,
            filename=self.filename,
            pages=len(self.pages),
            ocr_backend=self.backend,
            mean_confidence=self.mean_confidence,
            low_confidence_pages=sorted({int(e["page"]) for e in self.needs_verification}),
            uploaded_at=datetime.now(UTC),
        )


def region_band(block: Any) -> str:
    """Three states a region can be in, and they are three different claims.

    `unreadable` is not `verify` with a lower number: one says "there is writing here and
    nobody has read it", the other says "here is a reading, check it". Collapsing them tells a
    doctor the page held less than it does.
    """
    if block.failure is not None or not block.text.strip():
        return "unreadable"
    if not block.confidence_measured:
        # Not measured is not the same as low. It still needs a human, and it must not render
        # as 0%.
        return "verify"
    return confidence_band(block.confidence, block.handwritten)


def confidence_band(confidence: float, handwritten: bool) -> str:
    """How sure OCR is, in the three words a patient screen can actually use.

    Deliberately coarse. A patient reading their own prescription back does not benefit from
    "0.8137", and a percentage invites them to treat 81% as an 81% chance the medicine is
    right — which is not what an OCR confidence means.
    """
    if handwritten or confidence <= settings.ocr_low_confidence_threshold:
        return "verify"
    return "high" if confidence >= 0.90 else "medium"


def classify_document(items: list[dict[str, Any]]) -> str:
    """Prescription, lab report or something else — from what was found, not the filename."""
    kinds = {item.get("kind") for item in items}
    if "medication" in kinds:
        return "prescription"
    if "investigation" in kinds:
        return "lab_report"
    if "diagnosis" in kinds:
        return "discharge_summary"
    return "other"


def _next_index(ledger: FactLedger, group: str) -> int:
    used = {
        int(path.split("[")[1].split("]")[0])
        for path in ledger.paths()
        if path.startswith(f"{group}[")
    }
    return max(used) + 1 if used else 0


def _record_entity(
    ledger: FactLedger,
    entity: ExtractedEntity,
    document_id: str,
    known_paths: set[str],
    backend: str,
    *,
    human_reading: str | None = None,
    read_by: str | None = None,
) -> list[Fact]:
    """Write one entity's facts. Every one is document-tier with page and bbox.

    `human_reading` carries a verifier's correction. It travels *with* the OCR line rather
    than replacing it, so the evidence drawer can show the scrawl and the reading together.
    """
    group_field = _GROUP_FOR.get(entity.kind)
    if group_field is None:
        return []
    group, primary = group_field
    index = _next_index(ledger, group)

    def span() -> DocumentSpan:
        return DocumentSpan(
            verbatim=entity.source_text,
            document_id=document_id,
            page=entity.page,
            bbox=entity.bbox,
            ocr_confidence=entity.confidence,
            ocr_backend=backend,
            handwritten=entity.handwritten,
            confidence_measured=entity.confidence_measured,
            region_id=entity.region_id,
            human_reading=human_reading,
            read_by=read_by,
        )

    written: list[Fact] = []

    def write(field_name: str, value: Any) -> None:
        path = f"{group}[{index}].{field_name}"
        if path not in known_paths:
            return
        try:
            written.append(
                record_fact(
                    ledger,
                    path=path,
                    value=value,
                    tier=SourceTier.DOCUMENT,
                    source=span(),
                    confidence=entity.confidence,
                    provenance_note=f"ocr:{backend}",
                    known_paths=known_paths,
                    supersede=False,
                )
            )
        except Exception as exc:  # a single unparseable field must not lose the document
            log.warning("document.fact_rejected", path=path, error=str(exc)[:160])

    write(primary, entity.text)
    detail = entity.detail
    if entity.kind == "medication":
        if detail.get("dose"):
            write("dose", detail["dose"])
        if detail.get("frequencyRaw"):
            write("frequency", detail["frequencyRaw"])
        if detail.get("route"):
            write("route", detail["route"])
    elif entity.kind == "investigation":
        if detail.get("value") is not None:
            write("value", str(detail["value"]))
    elif entity.kind == "diagnosis" and entity.observed_on:
        write("reported_year", str(entity.observed_on.year))
    return written


def read_and_extract(
    data: bytes,
    *,
    filename: str,
    media_type: str,
    sex: str | None = None,
    backend_name: str | None = None,
) -> tuple[OCRResult, list[ExtractedEntity], list[ExtractedEntity]]:
    """Read a document and extract its entities — THE ONLY WAY IN.

    ⛔ Every OCR read in this application goes through here or through `ingest()`. Nothing
    else may import an OCR backend, and `test_ocr_has_one_front_door` fails the build if
    anything does.

    THIS EXISTS BECAUSE OF A SPECIFIC FAILURE. `seed.py` imported `get_ocr_backend` and
    `extract_entities` directly, so the three dated lab reports in the demo patient's history
    were produced by an in-process Python call that skipped the upload route, the multipart
    handling, the consent gate, the size limit and this module entirely. On that basis the
    claim "these went through the actual OCR pipeline" was made, and it was not true in any
    sense that mattered — no browser, no route, no gate. A private back door into OCR is how
    a project convinces itself a feature works when only a fragment of it does.

    Returns the raw result alongside the confident and needs-checking entity lists, because
    the seed writes durable rows directly while `ingest()` writes session facts. They are
    different destinations for the same reading, which is exactly what should be shared.
    """
    ocr = read_document(data, filename=filename, media_type=media_type, requested=backend_name)
    confident, needs_check = extract_entities(ocr, sex=sex)
    return ocr, list(confident), list(needs_check)


def ingest(
    ledger: FactLedger,
    data: bytes,
    *,
    filename: str,
    media_type: str,
    known_paths: set[str],
    backend_name: str | None = None,
    sex: str | None = None,
    document_id: str | None = None,
) -> IngestResult:
    """Run the whole Module B pipeline for one uploaded file."""
    if len(data) > settings.max_upload_bytes:
        raise ValidationError(
            f"{filename} is {len(data) // 1024} KB; the limit is "
            f"{settings.max_upload_bytes // 1024} KB."
        )
    if not data:
        raise ValidationError(f"{filename} is empty.")

    # Which engine reads this file is decided by what the file IS, not by a setting. A
    # patient photographing a prescription gets an image engine; a digital PDF gets the exact
    # text-layer reader. See backends.read_document().
    ocr = read_document(
        data, filename=filename, media_type=media_type, requested=backend_name
    )
    backend = get_ocr_backend(ocr.backend)
    doc_id = document_id or f"doc_{uuid.uuid4().hex[:10]}"

    confident, needs_check = extract_entities(ocr, sex=sex)

    facts: list[Fact] = []
    fact_index: dict[int, str] = {}
    for position, entity in enumerate(confident):
        written = _record_entity(ledger, entity, doc_id, known_paths, backend.name)
        facts.extend(written)
        if written:
            fact_index[position] = written[0].fact_id

    timeline = order_timeline(build_timeline(confident, document_id=doc_id, fact_ids=fact_index))

    log.info(
        "document.ingested",
        document=doc_id,
        backend=backend.name,
        pages=len(ocr.pages),
        entities=len(confident),
        needs_verification=len(needs_check),
        facts=len(facts),
    )

    return IngestResult(
        document_id=doc_id,
        filename=filename,
        backend=backend.name,
        pages=[page_summary(p) for p in ocr.pages],
        mean_confidence=ocr.mean_confidence,
        facts=facts,
        timeline=timeline,
        needs_verification=[{**e.to_dict(), "entityIndex": i} for i, e in enumerate(needs_check)],
        entities=confident,
        ocr=ocr,
    )


def verify_entity(
    ledger: FactLedger,
    result: IngestResult,
    *,
    entity_index: int,
    accepted: bool,
    verified_by: str,
    known_paths: set[str],
    corrected_text: str | None = None,
) -> list[Fact]:
    """A human accepted (or rejected) a low-confidence entity. The only way into the record.

    A correction is recorded against the *original* OCR span, not a fabricated one: the
    physician's screen then shows the scrawl the value came from next to the value a person
    read it as, which is exactly what a later reviewer needs to see.
    """
    pending = [e for e in result.needs_verification if e["entityIndex"] == entity_index]
    if not pending:
        raise ValidationError(f"No pending entity at index {entity_index}.")
    if not accepted:
        log.info("document.entity_rejected", document=result.document_id, index=entity_index)
        return []

    raw = pending[0]
    entity = ExtractedEntity(
        kind=raw["kind"],
        text=corrected_text or raw["text"],
        page=int(raw["page"]),
        bbox=type(result.entities[0].bbox)(**raw["bbox"])
        if result.entities
        else __import__("app.contracts.provenance", fromlist=["BoundingBox"]).BoundingBox(
            **raw["bbox"]
        ),
        confidence=float(raw["confidence"]),
        handwritten=bool(raw["handwritten"]),
        source_text=raw["sourceText"],
        detail=dict(raw["detail"]),
    )
    facts = _record_entity(
        ledger,
        entity,
        result.document_id,
        known_paths,
        result.backend,
        human_reading=corrected_text or None,
        read_by=verified_by if corrected_text else None,
    )
    for fact in facts:
        log.info(
            "document.entity_verified",
            document=result.document_id,
            by=verified_by,
            fact=fact.fact_id,
            corrected=bool(corrected_text),
        )
    return facts
