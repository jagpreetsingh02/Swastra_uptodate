"""Clinical entity extraction from OCR output — Module B's second stage.

Rule, not LLM, for the structured parts. A prescription line is *structured text*:
"TAB. METFORMIN 500MG 1-0-1 x 30 days" has a grammar, and a regex reads it identically every
time, at zero latency, with an exact character offset for the bounding box. A model reading
the same line would be slower, non-reproducible, and would occasionally read "500" as "50".

The LLM is offered as an optional second pass (`use_llm=True`) for lines the rules could not
parse, and the eval harness reports what it adds. As of the current numbers in
docs/EVALUATION.md it adds recall on free-text discharge summaries and nothing at all on
printed prescriptions and lab reports, which is the expected shape of that result.

Every entity carries the exact source block, so a `document`-tier fact gets its page and bbox
for free.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from app.contracts.provenance import BoundingBox
from app.modules.documents.backends import OCRBlock, OCRPage, OCRResult
from app.modules.documents.ranges import assess, match_analyte

EntityKind = Literal["diagnosis", "medication", "investigation", "procedure"]


@dataclass(slots=True)
class ExtractedEntity:
    kind: EntityKind
    text: str
    page: int
    bbox: BoundingBox
    confidence: float
    handwritten: bool
    #: The exact OCR line this came from. Becomes the DocumentSpan verbatim.
    source_text: str
    #: WHICH DETECTED REGION PRODUCED THIS. Carried so the physician screen can highlight the
    #: exact strip of paper a medicine was read from, rather than re-deriving a box from
    #: coordinates and hoping the two still agree. `None` for engines that do not segment
    #: (the text layer, and Tesseract's own whole-page blocks).
    region_id: int | None = None
    #: False when the recogniser exposed no trustworthy score. `confidence` is then a
    #: placeholder, and the API reports `confidence: null`.
    confidence_measured: bool = True
    detail: dict[str, Any] = field(default_factory=dict)
    observed_on: date | None = None
    date_precision: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "text": self.text,
            "page": self.page,
            "bbox": self.bbox.model_dump(),
            "confidence": round(self.confidence, 4),
            "handwritten": self.handwritten,
            "sourceText": self.source_text,
            "regionId": self.region_id,
            "confidenceMeasured": self.confidence_measured,
            "detail": self.detail,
            "observedOn": self.observed_on.isoformat() if self.observed_on else None,
            "datePrecision": self.date_precision,
        }


# ---------------------------------------------------------------- patterns
#
# Indian prescription conventions specifically: 1-0-1 dosing notation, TAB/CAP/SYP prefixes,
# OD/BD/TDS/QID/HS/SOS frequencies. A parser built for US prescriptions reads none of these.

# Spelled-out forms as well as the abbreviations. A prescription that says "Syrup" rather than
# "SYP" was otherwise read as a medicine called "Syrup Paracetamol" — the form became part of
# the drug name, which is wrong in the record and wrong on the patient's read-back screen.
_FORM = (
    r"(?:TABS?|TABLETS?|CAPS?|CAPSULES?|SYP|SYRUPS?|INJ|INJECTIONS?|OINT|OINTMENTS?"
    r"|DROPS?|POWDERS?|SUSP|SUSPENSIONS?|CREAMS?|GEL|LOTION|SACHETS?)\.?"
)
_STRENGTH = r"\d+(?:\.\d+)?\s*(?:mg|mcg|g|ml|iu|units?|%)"
# Word boundaries are load-bearing: without them the OD in AMLODIPINE reads as a frequency
# and the drug name is truncated to "AML".
_FREQ_WORD = r"\b(?:OD|BD|BID|TDS|TID|QID|QDS|HS|SOS|STAT|PRN|OW|Q\d+H)\b"
_FREQ_NUMERIC = r"\d\s*-\s*\d\s*-\s*\d(?:\s*-\s*\d)?"

MEDICATION_LINE = re.compile(
    rf"(?P<form>{_FORM})?\s*"
    rf"(?P<name>[A-Za-z][A-Za-z0-9\-/'\s]{{2,40}}?)\s*"
    rf"(?P<strength>{_STRENGTH})?\s*"
    rf"(?P<freq>{_FREQ_NUMERIC}|{_FREQ_WORD})"
    # Abbreviated durations too — `x 5d`, `x 2w`, `x 1m`. This is READING what is written,
    # not inferring it: a prescription that says "x 5d" has stated its duration, and dropping
    # it because it was not spelled "days" loses a field the doctor wrote down.
    rf"(?:\s*(?:x|X|×|for)\s*(?P<duration>\d+\s*(?:days?|weeks?|months?|d|w|m)\b))?",
    re.IGNORECASE,
)

#: A medication line must show at least a strength or a recognisable frequency. A bare
#: capitalised word is a heading, not a drug.
_ROUTE_WORDS = {
    "oral": "oral",
    "po": "oral",
    "iv": "intravenous",
    "im": "intramuscular",
    "sc": "subcutaneous",
    "topical": "topical",
    "inhaled": "inhalation",
    "nebulis": "inhalation",
    "drops": "ophthalmic",
}

# The separator between label and value is REQUIRED. Without it the non-greedy label
# matches "HbA" and reads the "1" of "HbA1c" as the result value.
INVESTIGATION_LINE = re.compile(
    r"^(?P<label>[A-Za-z][A-Za-z0-9\s\.\(\)/\-]{1,45}?)[\s:–-]+"
    r"(?P<value>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>%|g/dL|mg/dL|mmol/L|U/L|IU/L|uIU/mL|ng/mL|pg/mL|mm/hr|cells?/cu\.?mm|/cumm|lakh(?:s)?/cumm)?"
    r"\s*(?P<rest>.*)$",
    re.IGNORECASE,
)

DIAGNOSIS_CUE = re.compile(
    r"(?:diagnosis|dx|impression|k/c/o|known case of|c/o|complaint of|provisional)\s*[:\-–]?\s*"
    r"(?P<text>.+)",
    re.IGNORECASE,
)

PROCEDURE_CUE = re.compile(
    r"(?:procedure|surgery|operation|s/p|status post|done|underwent)\s*[:\-–]?\s*(?P<text>.+)",
    re.IGNORECASE,
)

_DATE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})\b"), "dmy"),
    (re.compile(r"\b(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})\b"), "ymd"),
    (
        re.compile(
            r"\b(\d{1,2})\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+(\d{4})\b",
            re.IGNORECASE,
        ),
        "dmonthy",
    ),
    (
        re.compile(
            r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+(\d{4})\b",
            re.IGNORECASE,
        ),
        "monthy",
    ),
    (re.compile(r"\b(19|20)(\d{2})\b"), "year"),
)

_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


def parse_date(text: str) -> tuple[date | None, str]:
    """Best-effort date with an explicit precision. `unknown` is a normal outcome."""
    for pattern, kind in _DATE_PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        try:
            if kind == "dmy":
                d, m, y = (int(match.group(i)) for i in (1, 2, 3))
                return date(y, m, d), "exact"
            if kind == "ymd":
                y, m, d = (int(match.group(i)) for i in (1, 2, 3))
                return date(y, m, d), "exact"
            if kind == "dmonthy":
                return (
                    date(
                        int(match.group(3)),
                        _MONTHS[match.group(2)[:3].lower()],
                        int(match.group(1)),
                    ),
                    "exact",
                )
            if kind == "monthy":
                return date(int(match.group(2)), _MONTHS[match.group(1)[:3].lower()], 1), "month"
            if kind == "year":
                return date(int(match.group(0)), 1, 1), "year"
        except (ValueError, KeyError):
            continue
    return None, "unknown"


def _normalise_frequency(raw: str) -> str:
    """Turn dosing shorthand into words a patient-facing screen can render."""
    cleaned = raw.upper().replace(" ", "")
    numeric = re.fullmatch(r"(\d)-(\d)-(\d)(?:-(\d))?", cleaned)
    if numeric:
        slots = [g for g in numeric.groups() if g is not None]
        times = sum(1 for s in slots if s != "0")
        labels = ["morning", "afternoon", "night", "bedtime"][: len(slots)]
        taken = [label for label, s in zip(labels, slots, strict=False) if s != "0"]
        return f"{times}× daily ({', '.join(taken)})" if taken else "as directed"
    return {
        "OD": "once daily",
        "BD": "twice daily",
        "BID": "twice daily",
        "TDS": "three times daily",
        "TID": "three times daily",
        "QID": "four times daily",
        "QDS": "four times daily",
        "HS": "at bedtime",
        "SOS": "as needed",
        "PRN": "as needed",
        "STAT": "immediately, once",
        "OW": "once weekly",
    }.get(cleaned, raw.strip())


#: When to take it relative to food or the day. Kept apart from frequency because a line
#: routinely carries both ("BD bf" is twice daily AND before food) and one field loses one of
#: them. Matched only as whole words — `bf` inside a drug name is not an instruction.
_TIMING_WORDS: tuple[tuple[str, str], ...] = (
    (r"\bbefore\s+food\b", "before food"),
    (r"\bafter\s+food\b", "after food"),
    (r"\bbefore\s+meals?\b", "before meals"),
    (r"\bafter\s+meals?\b", "after meals"),
    (r"\bempty\s+stomach\b", "on an empty stomach"),
    (r"\bbf\b", "before food"),
    (r"\baf\b", "after food"),
    (r"\ba\.?c\.?\b", "before meals"),
    (r"\bp\.?c\.?\b", "after meals"),
    (r"\bh\.?s\.?\b", "at bedtime"),
)


def _timing_from(text: str) -> str | None:
    """The food/time-of-day instruction written on the line, or nothing.

    Returns `None` rather than a default. A prescription that does not say when to take a
    medicine has not said it, and filling that in would be inventing a clinical instruction —
    which is the one thing this extractor must never do.
    """
    for pattern, reading in _TIMING_WORDS:
        if re.search(pattern, text, re.IGNORECASE):
            return reading
    return None


def _route_from(text: str) -> str | None:
    lowered = text.casefold()
    for cue, route in _ROUTE_WORDS.items():
        if cue in lowered:
            return route
    return None


def extract_from_block(
    block: OCRBlock, page: int, *, sex: str | None = None
) -> list[ExtractedEntity]:
    """Parse one OCR line. May return several entities; usually returns none."""
    text = block.text.strip()
    if len(text) < 3:
        return []

    found: list[ExtractedEntity] = []
    observed, precision = parse_date(text)

    def _entity(kind: EntityKind, label: str, detail: dict[str, Any]) -> ExtractedEntity:
        return ExtractedEntity(
            kind=kind,
            text=label,
            page=page,
            bbox=block.bbox,
            confidence=block.confidence,
            handwritten=block.handwritten,
            source_text=text,
            region_id=block.region_id,
            confidence_measured=block.confidence_measured,
            detail=detail,
            observed_on=observed,
            date_precision=precision,
        )

    diagnosis = DIAGNOSIS_CUE.search(text)
    if diagnosis:
        term = diagnosis.group("text").strip(" .;,")
        if term:
            found.append(_entity("diagnosis", term, {"cue": diagnosis.group(0)[:24]}))
            return found

    procedure = PROCEDURE_CUE.search(text)
    if procedure:
        term = procedure.group("text").strip(" .;,")
        if term:
            found.append(_entity("procedure", term, {}))
            return found

    investigation = INVESTIGATION_LINE.match(text)
    if investigation and match_analyte(investigation.group("label")):
        label = investigation.group("label").strip(" .:-")
        value = float(investigation.group("value"))
        rest = investigation.group("rest") or ""
        judgement = assess(label, value, printed_range=rest or None, sex=sex)
        found.append(
            _entity(
                "investigation",
                label,
                {
                    "value": value,
                    "unit": investigation.group("unit") or judgement.unit,
                    "rangeFlag": judgement.flag,
                    "referenceLow": judgement.low,
                    "referenceHigh": judgement.high,
                    "rangeSource": judgement.source,
                    "analyteKey": judgement.analyte_key,
                    "display": judgement.display or label,
                },
            )
        )
        return found

    medication = MEDICATION_LINE.search(text)
    if medication and (medication.group("strength") or medication.group("freq")):
        name = (medication.group("name") or "").strip(" .:-")
        # Reject headings and section labels that happen to sit next to a number.
        if (
            name
            and len(name) >= 3
            and not name.casefold().startswith(
                ("date", "name", "age", "sex", "patient", "ref", "dr", "reg", "opd", "uhid")
            )
        ):
            found.append(
                _entity(
                    "medication",
                    name,
                    {
                        "form": (medication.group("form") or "").strip(". ").upper() or None,
                        "dose": medication.group("strength"),
                        "frequencyRaw": medication.group("freq"),
                        "frequency": _normalise_frequency(medication.group("freq") or ""),
                        "duration": medication.group("duration"),
                        "route": _route_from(text),
                        "timing": _timing_from(text),
                    },
                )
            )
    return found


#: Header lines that carry the document's own date. A prescription dates every drug on it;
#: only the header says when.
_DOC_DATE_CUE = re.compile(
    r"\b(?:date|dated|dt|on|report(?:ed)? on|collected(?: on)?|sample date|visit date)\b",
    re.IGNORECASE,
)


def document_date(result: OCRResult) -> tuple[date | None, str, str | None]:
    """Find the document's own date. Returns (date, precision, the line it came from).

    A cued line ("Date: 14/03/2026") beats an uncued one, because a bare 2019 in the body of a
    discharge summary is far more likely to be a past event than the document's date.
    """
    fallback: tuple[date | None, str, str | None] = (None, "unknown", None)
    for page in result.pages:
        for block in page.blocks:
            parsed, precision = parse_date(block.text)
            if parsed is None:
                continue
            if _DOC_DATE_CUE.search(block.text):
                return parsed, precision, block.text
            if fallback[0] is None:
                fallback = (parsed, precision, block.text)
    return fallback


def extract_entities(
    result: OCRResult, *, sex: str | None = None
) -> tuple[list[ExtractedEntity], list[ExtractedEntity]]:
    """Return (confident entities, low-confidence entities needing human verification).

    The split is the handwriting lane. A low-confidence entity is **never** merged into the
    record silently; it appears on the physician screen in a separate, visually distinct
    block that must be accepted or rejected before it becomes a fact.
    """
    from app.core.config import settings

    confident: list[ExtractedEntity] = []
    needs_check: list[ExtractedEntity] = []
    header_date, header_precision, header_line = document_date(result)

    for page in result.pages:
        for block in page.blocks:
            for entity in extract_from_block(block, page.page, sex=sex):
                if entity.observed_on is None and header_date is not None:
                    # Inherited from the document header, and *labelled* as inherited so
                    # nobody later mistakes it for a date printed against this line.
                    entity.observed_on = header_date
                    entity.date_precision = header_precision
                    entity.detail["dateSource"] = "document_header"
                    entity.detail["dateSourceLine"] = header_line
                elif entity.observed_on is not None:
                    entity.detail["dateSource"] = "own_line"
                unmeasured = not entity.confidence_measured
                if (
                    entity.handwritten
                    or unmeasured
                    or entity.confidence <= settings.ocr_low_confidence_threshold
                ):
                    needs_check.append(entity)
                else:
                    confident.append(entity)
    return confident, needs_check


def page_summary(page: OCRPage) -> dict[str, Any]:
    return {
        "page": page.page,
        "blocks": len(page.blocks),
        "meanConfidence": round(page.mean_confidence, 4),
        "handwrittenBlocks": sum(1 for b in page.blocks if b.handwritten),
    }
