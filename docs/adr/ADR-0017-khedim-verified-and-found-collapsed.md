# ADR-0017 — `khedim/Medical-Prescription-OCR` was run for real, and found to be collapsed

**Status:** accepted. This is a record of a verification result, not a design decision — it
exists because the finding changes how the handwriting lane must be operated, and because
"we tried the real model and it did not work" needs to be as durable and as findable as any
other architectural fact in this repo.

## Context

`app/modules/documents/handwriting.py` calls `khedim/Medical-Prescription-OCR`, a gated
Hugging Face checkpoint, as the primary recognizer for the handwriting OCR lane. Until now it
had never been run with real, authorised weights in this repo — every prior verification used
the ungated `microsoft/trocr-base-handwritten` as a stand-in, and the report on that work said
so plainly. With `HF_TOKEN` configured and access confirmed (`GET .../config.json` → 200), it
was run for real.

## What was verified

**The model loads and runs.** `VisionEncoderDecoderModel.from_pretrained("khedim/Medical-Prescription-OCR", token=...)`
succeeds in ~23s cold, ~9.5s per 9-region page thereafter, on `device=mps`. The checkpoint is
`microsoft/trocr-small-handwritten` fine-tuned for 6 epochs (`training_summary.json`), and it
ships its own `generation_config.json` (`num_beams: 4`, `max_new_tokens: 128`) and its own
`processor_config.json` / `tokenizer_config.json` — the repo is self-contained and needed none
of `handwriting.py`'s fallback processor/tokenizer IDs.

One real bug was found and fixed in getting here: `_khedim_recognizer()` hardcoded
`num_beams=1`, overriding the checkpoint's own trained decoding strategy. Running a fine-tune
with a beam width it was never evaluated at is not running the actual model. Fixed by
deferring to `model.generation_config` unless `settings.handwriting_num_beams` is explicitly
set, and by passing `beam_indices` into `compute_transition_scores` so the per-token
confidence is computed for the sequence the beam search actually returned rather than for an
arbitrary beam slot (`transformers` requires `beam_indices` for this to be correct under
`num_beams > 1`; omitting it silently scores the wrong sequence).

**The model's output is not connected to its input.** On the synthetic handwritten fixture
(`prescription_handwritten.jpg`, 9 detected lines), the checkpoint returns fluent, well-formed,
plausible-looking text at confidences from 0.65 to 0.85 — and the text is a memorized template
unrelated to what is actually written:

```
region  what is actually written                    what khedim returned          confidence
#0      "Dr S. Menon   MBBS MD"                      "medications: Amlodipine …"      0.686
#1      "City Clinic, Chennai"                       "date: 2024-12-16"               0.850
#2      "Date 04/09/2026"                             "patient: John Doe Age: 48"      0.763
#5      "Cap Omeprazole 20mg  OD  bf"                 "medications: Losartan …"        0.678
#6      "Syrup Paracetamol 250mg  SOS"                "medications: Ibuprofen …"       0.689
```

The proof this is genuine model collapse and not a defect in this repo's crop pipeline: **a
blank white 384×384 image, with no ink on it at all, returns `"date: 2024-12-16"` at
confidence 0.997**, and a blank mid-grey crop returns the same string. The nine crops sent to
the model were individually inspected (`fit_for_model()` output, saved to disk) and are
correct — distinct, legible, correctly letterboxed, one per written line. The same collapse
appears on `prescription_scan.png`, a clean *printed* prescription with none of the synthetic
fixture's handwriting noise. The output is drawn from a small closed pool —
`{Amlodipine, Losartan, Ibuprofen, Metformin} × {mg doses} × {"As directed" | "Take twice
daily" | "At bedtime"}`, `"date: 2024-12-16"`, `"patient: John Doe Age: {20,37,48}"` — which is
consistent with the checkpoint having memorized the surface structure of a narrow synthetic
training template (`training_summary.json` reports `test_full_cer: 0.0128`,
`test_line_word_accuracy: 0.986` — measured against its *own* held-out split of the same
synthetic distribution it was trained on) rather than having learned general handwriting
recognition. It has not been established whether this is a property of the published
checkpoint itself or of how these particular weights were fine-tuned; only the observed
behaviour is a finding this repo can state with confidence.

## Consequence for the handwriting lane

None of this required a change to the safety architecture, and that is the point of having
built it the way it was built. Every region from the real Khedim run landed in the review lane
(`confidenceBand: "verify"`), zero facts were auto-recorded, and the entity extractor correctly
found nothing to extract because the hallucinated text matches none of `entities.py`'s
patterns. A model that is fluently, confidently wrong is exactly the failure case
`record_fact()`'s echo check, the handwriting-always-needs-a-human rule, and the
never-invent-a-reading design in `medications.py` all exist for. The provenance is also
unaffected and independently correct: the bounding box drawn under region #6 is still,
verifiably, the exact pixels of "Syrup Paracetamol 250mg SOS" — a physician looking at the
overlay sees a box over the right line captioned with the wrong drug, which is a *visible,
checkable* mismatch, not a silent one.

**The fallback chain is therefore load-bearing, not decorative, on this checkpoint as
currently published.** `default_recognizer()` tries Khedim first and falls through to
per-line Tesseract on any raised `UpstreamUnavailable` — but a checkpoint that loads and
returns fluent nonsense does not raise anything; it succeeds. `Recognizer` has no built-in
notion of "this text was too plausible to be real," and inventing one — e.g., rejecting output
that matches a memorized-template heuristic — would itself be a fragile, unprincipled patch
bolted onto a real architectural problem, so none was added.

## What this ADR does not claim

This is a verification of the *published checkpoint*, run as documented. It is not a claim
about what `khedim/Medical-Prescription-OCR` could do with different inference settings this
repo did not try (temperature, repetition penalty, a shorter or longer `max_new_tokens`, a
non-letterboxed crop), nor a claim that no configuration of it can ever read this style of
input. It is a report of one specific, faithful, reproducible run of the actual model as
downloaded, and the finding is that on this input distribution, at the settings the checkpoint
itself ships, it does not read the page.

## Alternatives considered

**Silently keep routing to Khedim and call the lane done.** Rejected — this is precisely what
the task that produced this ADR forbade, and it would put a model that hallucinates confident
structured text in front of the recognizer that Tesseract-line actually reads correctly.

**Patch around it with a confidence penalty or a template-detector.** Rejected. A heuristic
built to catch "this looks like the memorized template" is guessing at the shape of one
checkpoint's failure mode and would need to be re-tuned, or would silently stop working, the
moment the checkpoint is retrained or replaced. The correct layer for this is the one already
in place: route everything handwritten to a human, unconditionally, regardless of what any
engine reports.

**Remove Khedim from the chain entirely.** Rejected — not asked for, and the brief is explicit
that Khedim remains the primary handwriting engine architecturally. What this ADR changes is
the operational fact on record: *as currently published*, expect it to defer to Tesseract-line
in practice, and do not read a "backend": "handwriting" with `engine: "khedim/..."` in the API
response as evidence the content is trustworthy — the confidence band already carries that
signal, and it says `verify` on every region regardless.
