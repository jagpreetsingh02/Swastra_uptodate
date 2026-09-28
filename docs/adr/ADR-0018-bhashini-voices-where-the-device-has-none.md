# ADR-0018 — Bhashini reads the prompt only where the device has no voice for the language

**Context.** Bhashini credentials arrived (an Inference API key and a Udyat key). The
`bhashini` speech backend had existed since the first build but had never made a live call —
its own docstring said so, and said to "treat the first live call as an integration test". It
was. The first live calls, on 2026-09-28, found that the backend could never have worked:

1. **Wrong endpoint.** It posted to `…/services/inference`, which returns **404**. The compute
   endpoint is `…/services/inference/pipeline`.
2. **Wrong model of service IDs.** It sent one `BHASHINI_PIPELINE_ID` as the `serviceId` for
   ASR *and* TTS in *every* language. Dhruva's IDs differ per task and per language family
   (`conformer-hi`, `conformer-multilingual-dravidian`, `indic-tts-coqui-indo_aryan`, …). One
   value could have matched at most one cell of a 2 × 12 table.
3. **Hard-coded 16 kHz.** Dhruva's own TTS returns 22.05 kHz float WAV; the old ASR call would
   have labelled audio like that as 16 kHz.

And the kiosk never called it anyway: `api.speak` existed in `shared/api.ts` with no caller.
Every prompt was read by the browser's `speechSynthesis`, which `shared/tts.ts` documents as
often having **no voice at all** for `hi-IN` or `ta-IN` — so the patient got silence, or an
English voice reading Devanagari.

**Decision.** Rule, not LLM — no language model is involved anywhere in this change.

* **Service IDs are data**, in `config/bhashini-services.yaml`, per task and language, each one
  verified by a live call (TTS in 12 languages; ASR round trips in 11 — Gujarati ASR is left out
  as unverified rather than guessed). They drift the way Groq model names drift, so they belong
  where they can be changed without a code review of the client.
* **Only the Inference API key is required.** It is the `Authorization` header and the only
  credential the compute call needs. The Udyat key is kept as `BHASHINI_USER_ID` and sent as
  the `userID` header, but no call tested here depended on it.
* **The browser voice stays first** (ADR-0010: offline is the default). The kiosk asks the
  server to read a prompt only when the device has no voice *of that language* — an English
  fallback does not count — and only once a session exists. The consent screen and the done
  screen stay browser-only: before consent there is no session, and after submit it is purged.
* **Fail fast, fall back.** Measured latency: 0.3–1.3 s on success (median 0.44 s, n=10), but
  failures **hang for 20 s or more** rather than erroring — Gujarati TTS failed about 3 in 7.
  The client timeout is 5 s (`BHASHINI_TIMEOUT_SECONDS`), and a failure returns
  `clientFallback` instead of a 503. The frontend then runs the browser path exactly as before,
  so the worst case for a patient is today's behaviour.
* **Every hosted call is audited** (Invariant 6). `Utterance.model` names the service that was
  called; `/speak` writes a `speech.synthesise` row through `record_ai_call` exactly when it is
  set — including on failure, because the text left the building either way. Only a
  fingerprint of the prompt is stored.
* **The demo seeder never sends audio to a hosted engine.** At boot there is no session to
  audit the call against, and Bhashini reports no confidence, so the result would be the same
  `unavailable` the fixture already records.

**Alternatives considered.** *Bhashini first for every prompt* — better voice consistency, but
a network round trip and an external call on every question, and ADR-0010 already settled which
path is the reference. *Fetching service IDs at runtime from the ULCA pipeline-config API* —
the documented flow, but it needs a ULCA user ID we were not given, and adds a second network
dependency before the first prompt can play. *Bhashini ASR for spoken answers* — not done here:
answers use on-device recognition, and moving them to the server means recording and uploading
patient audio, which is a consent-scope and audit change of its own.

**Consequences.**

* A Hindi, Tamil or Bengali patient on a device with no voice for their language now hears the
  question, in their language, where they previously heard nothing.
* `BHASHINI_PIPELINE_ID` is gone. `SPEECH_BACKEND=bhashini` and `BHASHINI_API_KEY` must be set
  on the deployed backend for any of this to reach production.
* The test suite pins `SPEECH_BACKEND=local` in `tests/conftest.py` so a developer `.env` with a
  live key can never make the suite call Dhruva; `tests/test_bhashini.py` stubs `httpx.post`.
* Unverified: behaviour on real Indian kiosk hardware, Safari's autoplay rules for audio fetched
  after a tap, and ASR on real speakers rather than synthesised speech.
