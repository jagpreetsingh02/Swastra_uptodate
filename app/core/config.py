"""Application settings. Everything configurable lives here, nothing is hardcoded elsewhere.

Adapted from the SIH 25026 service (see docs/PORTED.md). The ICD/NAMASTE terminology settings
carry across because the coding sidecar reuses the same closed-vocabulary guard.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Canonical system URLs. Use verbatim; never construct these at a call site.
ICD_MMS_SYSTEM = "http://id.who.int/icd/release/11/mms"
NAMASTE_SYSTEM_BASE = "https://ayush.gov.in/fhir/CodeSystem/namaste"
SNOMED_SYSTEM = "http://snomed.info/sct"
LOINC_SYSTEM = "http://loinc.org"
#: Dashavidha Pariksha parameters. A Swastra-local CodeSystem until AYUSH publishes one.
DASHAVIDHA_SYSTEM = "https://medikiosk.local/fhir/CodeSystem/dashavidha-pariksha"
TEST_SYSTEM = "http://example.org/test-cs"  # fixtures ONLY

WHO_ATTRIBUTION = (
    "International Classification of Diseases, Eleventh Revision (ICD-11), "
    "World Health Organization (WHO) 2019/2021, https://icd.who.int/browse11. "
    "Licensed under Creative Commons Attribution-NoDerivatives 3.0 IGO (CC BY-ND 3.0 IGO)."
)

SUPPORTED_LANGUAGES: dict[str, str] = {
    "en": "English",
    "hi": "हिन्दी",
    "bn": "বাংলা",
    "ta": "தமிழ்",
    "te": "తెలుగు",
    "mr": "मराठी",
    "kn": "ಕನ್ನಡ",
    "ml": "മലയാളം",
    "gu": "ગુજરાતી",
    "pa": "ਪੰਜਾਬੀ",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # --- app ---
    app_name: str = "swastra"
    environment: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # --- persistence ---
    #: THERE IS NO DEFAULT, AND THAT IS THE POINT.
    #:
    #: This used to default to `sqlite+aiosqlite:///./medikiosk.db`. That one line is the
    #: root of the recurring failure in this project: a missing or misspelled DATABASE_URL
    #: produced a *working* application that wrote every patient, encounter and clinical
    #: fact to a local file, while the Supabase tables everyone was watching stayed empty.
    #: Nothing failed. Nothing warned. The bug was only visible as absence.
    #:
    #: An empty string cannot connect, so a missing value is now a startup crash with a
    #: clear message instead of a silent divergence. `require_postgres()` below is the
    #: check that actually enforces it; see docs/SUPABASE.md for which of Supabase's two
    #: connection strings to use for the app and which for migrations.
    database_url: str = ""
    db_echo: bool = False
    #: PRESENTATION FALLBACK. Selects a local Postgres instead of Supabase.
    #:
    #: This exists for one reason: outbound port 5432 is blocked on a great many venue
    #: networks — conference NAT, hotel Wi-Fi, corporate egress filtering — and when it is,
    #: NO Supabase endpoint works, pooler included. A pre-flight check that tells you the
    #: demo is doomed is not a mitigation.
    #:
    #: It is opt-in and never a fallback in the automatic sense. Nothing silently switches
    #: to it: if Supabase is unreachable and this flag is off, the application refuses to
    #: start, exactly as before. A silent switch is how a demo ends up presenting local
    #: data as though it came from Supabase, which is worse than not presenting at all.
    #:
    #: The dialect guard is unaffected — this is still PostgreSQL, so `require_postgres()`
    #: has nothing to forgive.
    #: How long a guest/demo record survives before the sweep removes it.
    #:
    #: Guest mode writes real rows and only an explicit Reset removed them, so every judge who
    #: pressed Try Demo left a patient and ~30 rows behind — permanently, on a free-tier
    #: database with a hard storage cap. 24 hours is long enough that a demo day never loses a
    #: record mid-event and short enough that the leak cannot accumulate.
    guest_ttl_hours: float = 24.0

    demo_local_db: bool = False
    #: Where that local Postgres lives. Port 5433 so it cannot collide with a Postgres the
    #: developer already runs on 5432.
    demo_local_database_url: str = (
        "postgresql+asyncpg://medikiosk:medikiosk@127.0.0.1:5433/medikiosk"
    )

    #: The ONLY way to reach SQLite. Set by `tests/conftest.py`, never by `.env`, and
    #: deliberately not named `environment=test` — that was an ordinary config value a
    #: developer could set by accident, which made the guard bypassable by typo.
    testing: bool = False
    #: Supabase project URL (`https://<ref>.supabase.co`). Used for Storage and for the
    #: preflight check, NOT for SQL: clinical reads and writes go through SQLAlchemy on
    #: `database_url`, per §4 and §23 of the brief.
    supabase_url: str | None = None
    #: The publishable (anon) key. Public by design. Unused by this backend — recorded so
    #: the preflight can prove RLS denies it, which is the check that matters.
    supabase_publishable_key: str | None = None
    #: BACKEND ONLY, and it bypasses RLS completely. Never reaches the browser —
    #: `test_no_supabase_secret_can_reach_the_browser` fails the build if it appears
    #: anywhere under frontend/. Accepts either the modern `sb_secret_…` key or a legacy
    #: service-role JWT.
    supabase_secret_key: str | None = None
    #: Where Supabase publishes the JWKS for its own Auth. Recorded for completeness;
    #: Swastra verifies its own mock-ABHA tokens and does not use Supabase Auth (§5).
    supabase_jwks_url: str | None = None
    #: Private bucket for prescription and report images.
    supabase_storage_bucket: str = "medical-documents"
    #: Set when the deployment is meant to be on Supabase. With this on, falling back to
    #: SQLite is a startup failure rather than a silent demo on an empty local file.
    require_supabase: bool = False
    redis_url: str = "redis://localhost:6379/0"
    #: When Redis is unreachable the session store falls back to an in-process dict so the
    #: demo survives a dead container. Never enable this in prod: it is single-worker only.
    session_store_allow_memory_fallback: bool = True

    # --- session lifecycle (Invariant 6) ---
    session_ttl_seconds: int = 3600
    purge_on_submit: bool = True

    # --- LLM (Modules A extraction + C prose smoothing) ---
    groq_api_key: str | None = None
    #: `llama-3.3-70b-versatile` (named in the original brief) was decommissioned by Groq;
    #: the API 404s on it. Verify against GET /openai/v1/models before changing this.
    groq_model: str = "openai/gpt-oss-120b"
    #: Groq hosts Whisper on the same key, which gives real server-side ASR for every
    #: language the kiosk offers — see app/speech/groq_whisper.py.
    groq_asr_model: str = "whisper-large-v3-turbo"
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_timeout_seconds: float = 20.0
    groq_max_retries: int = 4
    groq_retry_base_seconds: float = 1.5
    groq_max_backoff_seconds: float = 30.0
    #: "offline" is the default so a dropped demo network changes nothing. Set to "groq"
    #: (or leave "auto" with a key present) to use the hosted model.
    llm_backend: Literal["auto", "offline", "groq"] = "auto"
    llm_temperature: float = 0.0

    # --- speech (Module A voice) ---
    #: `client` is what the shipped kiosk uses (on-device Web Speech, works offline).
    #: `whisper` is real server-side ASR via Groq for clients that cannot recognise locally.
    speech_backend: Literal["local", "whisper", "bhashini", "client"] = "local"
    #: Below this ASR confidence the question degrades to touch rather than guessing.
    asr_confidence_threshold: float = 0.62
    bhashini_base_url: str = "https://dhruva-api.bhashini.gov.in/services/inference"
    bhashini_api_key: str | None = None
    bhashini_user_id: str | None = None
    bhashini_pipeline_id: str | None = None
    vosk_model_dir: str | None = None

    # --- documents (Module B) ---
    ocr_backend: Literal["textlayer", "tesseract", "handwriting"] = "textlayer"

    # --- handwriting OCR (the prescription lane) ------------------------------------
    #: The master switch. Off, `handwriting` reports itself unavailable and every photograph
    #: goes to Tesseract — which is also what happens when torch is not installed, so this is
    #: for turning the model off on a machine that COULD run it.
    handwriting_ocr_enabled: bool = True
    #: A TrOCR fine-tune on handwritten prescription LINES. Any replacement must also be a
    #: line recognizer: a page-level model fails by silently returning one fluent line.
    handwriting_model_id: str = "khedim/Medical-Prescription-OCR"
    #: Used when the fine-tune ships weights without tokenizer/image-processor configs, which
    #: community checkpoints routinely do.
    handwriting_processor_id: str = "microsoft/trocr-base-handwritten"
    #: Last resort for the tokenizer half. TrOCR's decoder is RoBERTa and shares its
    #: vocabulary, and unlike the TrOCR checkpoints this one publishes a `tokenizer.json`.
    handwriting_tokenizer_id: str = "roberta-base"
    handwriting_device: Literal["auto", "cpu", "cuda", "mps"] = "auto"
    handwriting_batch_size: int = 8
    #: One prescription line. Well above the longest real one, and it also bounds the cost of
    #: a decoder that has started to repeat itself.
    handwriting_max_new_tokens: int = 64
    #: A Hugging Face access token. `khedim/Medical-Prescription-OCR` is a GATED repo: without
    #: a token that has been granted access the download 401s and the kiosk falls back to
    #: Tesseract. Never logged.
    hf_token: str | None = None
    #: Anything at or below this goes to the handwriting lane and is never auto-merged.
    ocr_low_confidence_threshold: float = 0.72
    #: Largest upload the kiosk will accept. This value already existed and was NEVER
    #: ENFORCED — the upload route read the whole body into memory and rasterised it with no
    #: check at all, so a 40 MB burst photo produced a screen that hung and then failed
    #: without saying why. `routes_documents.upload` now enforces it, with a message that
    #: names the size and tells the patient what to do instead.
    #:
    #: 20 MiB comfortably holds a multi-page scanned PDF and a full-resolution phone photo
    #: (a 12MP JPEG is ~4 MB, HEIC ~2 MB).
    max_upload_bytes: int = 20 * 1024 * 1024

    # --- terminology (coding sidecar) ---
    namaste_version: str = "1.0"
    icd_release_id: str = "2026-01"
    dashavidha_version: str = "0.1.0"
    terminology_seed_dir: str = "data/terminology"

    # --- auth & policy (Module D) ---
    policy_file: str = "config/policy.yaml"
    auth_required: bool = False
    jwt_secret: str = "dev-only-not-a-real-secret-change-me-in-any-deployment"
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "mock-abdm-idp"
    jwt_audience: str = "medikiosk"

    # --- HIS / ABDM push ---
    his_fhir_endpoint: str = "http://localhost:8000/api/v1/stub-his/Bundle"
    his_push_timeout_seconds: float = 15.0

    # --- ontology ---
    ontology_dir: str = "data/ontology"
    ayush_mode_default: bool = False

    namaste_systems: dict[str, str] = Field(
        default_factory=lambda: {
            "ayurveda": f"{NAMASTE_SYSTEM_BASE}-ayurveda",
            "siddha": f"{NAMASTE_SYSTEM_BASE}-siddha",
            "unani": f"{NAMASTE_SYSTEM_BASE}-unani",
        }
    )

    @property
    def resolved_database_url(self) -> str:
        """The URL the application actually connects to.

        Every other database property reads this rather than the raw field, so the demo
        fallback cannot be half-applied — a code path that checked `database_url` directly
        would report Supabase while talking to localhost, which is precisely the confusion
        this whole flag is designed to prevent.
        """
        if self.demo_local_db:
            return self.demo_local_database_url
        return self.database_url

    @property
    def is_sqlite(self) -> bool:
        return self.resolved_database_url.startswith("sqlite")

    @property
    def is_postgres(self) -> bool:
        """The dialect the application requires. Checked at startup, not hoped for."""
        return self.resolved_database_url.startswith(("postgresql", "postgres+", "postgres:"))

    @property
    def is_supabase(self) -> bool:
        """True for both the direct endpoint (`db.<ref>.supabase.co`) and the pooler
        (`aws-N-<region>.pooler.supabase.com`)."""
        return (
            "supabase.co" in self.resolved_database_url
            or "supabase.com" in self.resolved_database_url
        )

    @property
    def is_pooled(self) -> bool:
        """Supavisor (Supabase's pooler) rather than the direct Postgres endpoint."""
        return "pooler" in self.resolved_database_url

    @property
    def is_transaction_pooler(self) -> bool:
        """Supavisor in TRANSACTION mode — port 6543.

        The distinction is not cosmetic. Transaction mode hands a different backend
        connection to every transaction, so a prepared statement created in one is gone by
        the next; asyncpg prepares everything by default, and the result is an intermittent
        `InvalidSQLStatementNameError: prepared statement "__asyncpg_stmt_N__" does not
        exist` under load. SESSION mode (port 5432) keeps one backend per client for the
        life of the connection, so prepared statements work normally and application-side
        pooling remains valid — which is exactly why the runtime uses session mode.
        """
        return self.is_pooled and ":6543" in self.resolved_database_url

    @property
    def database_backend(self) -> str:
        """A human label for the startup log. Never the URL — that carries the password."""
        if self.is_sqlite:
            return "SQLite (local file)"
        if self.demo_local_db:
            # Named so it cannot be mistaken for the real thing in a log or a screenshot.
            return "LOCAL DEMO PostgreSQL — NOT Supabase"
        if self.is_supabase:
            if self.is_transaction_pooler:
                return "Supabase PostgreSQL (pooler, transaction mode)"
            if self.is_pooled:
                return "Supabase PostgreSQL (pooler, session mode)"
            return "Supabase PostgreSQL (direct)"
        if self.is_postgres:
            return "PostgreSQL"
        return "unset" if not self.database_url else "unknown"

    def require_postgres(self) -> None:
        """Abort the process unless the resolved database is PostgreSQL.

        Called once at startup and once when the engine is built, because those are the two
        places a wrong value can enter: configuration, and a caller constructing an engine
        directly. There is no warn-and-continue branch on purpose — the failure this guards
        against is invisible at runtime, so it has to be loud at boot or it is not a guard.

        SQLite is reachable only when `testing` is true, which only `tests/conftest.py`
        sets. A dev or demo run cannot get there from `.env`.
        """
        if self.testing:
            return
        if not self.resolved_database_url:
            raise RuntimeError(
                "DATABASE_URL is not set. There is no default and no SQLite fallback: a "
                "fallback is what let this application write a whole consultation to a "
                "local file while the Supabase tables stayed empty. Set DATABASE_URL to "
                "the Supabase connection string (see docs/SUPABASE.md), or run under "
                "TESTING=1 if this is the test suite."
            )
        if not self.is_postgres:
            raise RuntimeError(
                f"DATABASE_URL resolves to {self.database_backend}, but this application "
                "requires PostgreSQL. Refusing to start rather than silently writing "
                "clinical data somewhere nobody is looking. Set DATABASE_URL to the "
                "Supabase connection string, or run under TESTING=1 for the test suite."
            )

    @property
    def database_host(self) -> str:
        """Host only, safe to log. Splitting on `@` is what drops the credentials."""
        if self.is_sqlite:
            return self.resolved_database_url.rsplit("/", 1)[-1]
        tail = self.resolved_database_url.rsplit("@", 1)[-1]
        return tail.split("/", 1)[0]

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def path(self, relative: str) -> Path:
        p = Path(relative)
        return p if p.is_absolute() else PROJECT_ROOT / p


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
