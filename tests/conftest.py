"""Shared fixtures. Every test runs on SQLite in memory: no Docker, no network, no Redis."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

# TESTING=1 is the ONE key that unlocks SQLite. Set here and nowhere else — not in `.env`,
# not in a Makefile target, not in a shell profile. `Settings.require_postgres()` reads it
# and every other path into the database refuses a non-Postgres URL, so this line is the
# entire reason the suite can run with no network.
#
# It replaces `ENVIRONMENT=test`, which was an ordinary config value: anyone who set it in
# `.env` to quiet something down silently disabled the guard for their whole machine.
os.environ["TESTING"] = "1"
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("LLM_BACKEND", "offline")
# Same reason as LLM_BACKEND: `.env` may set SPEECH_BACKEND=bhashini with a live key, and the
# suite must never reach a hosted engine. Bhashini is tested against a stubbed httpx.post.
os.environ.setdefault("SPEECH_BACKEND", "local")
os.environ["REQUIRE_SUPABASE"] = "false"

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def project_root() -> Path:
    return PROJECT_ROOT


@pytest.fixture
async def db_session() -> AsyncIterator:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db import models  # noqa: F401
    from app.db.base import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    # ⛔ SQLITE IGNORES `ON DELETE CASCADE` UNLESS THIS PRAGMA IS SET, per connection.
    #
    # Every cascade in this schema was declared, relied on in production, and never exercised
    # by this suite. It surfaced when the guest-sweep test failed: the patient was deleted and
    # its encounter survived — behaviour that PostgreSQL does not have. A suite that tests a
    # different database from the deployed one is describing something else.
    from sqlalchemy import event

    @event.listens_for(engine.sync_engine, "connect")
    def _fk_on(dbapi_connection, _record) -> None:  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


@pytest.fixture
async def seeded_session(db_session):
    from app.terminology.store import seed_all

    await seed_all(db_session)
    await db_session.commit()
    return db_session


@pytest.fixture
def ledger():
    from app.contracts.record import FactLedger

    return FactLedger("sess_test", consent_scopes={"history", "documents", "voice"})


@pytest.fixture
def machine(ledger):
    from app.modules.dialogue.machine import DialogueMachine, DialogueState

    state = DialogueState(session_id="sess_test", language="en")
    return DialogueMachine(state, ledger)


@pytest.fixture
async def seeded_patient(db_session):
    """The demo patient with two historical encounters, a prescription and a lab report."""
    from app.modules.encounter.history import get_patient_by_abha
    from app.modules.encounter.seed import demo_abha_ref, seed_demo_patient

    await seed_demo_patient(db_session)
    await db_session.commit()
    patient = await get_patient_by_abha(db_session, abha_ref=demo_abha_ref())
    assert patient is not None
    return db_session, patient
