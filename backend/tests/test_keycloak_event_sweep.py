"""The sweep that carries Keycloak credential events into the audit chain.

THE FAILURE MODE THIS FILE IS BUILT AROUND IS SILENCE. Every obvious bug in a
forwarder -- wrong org, duplicate rows, an event type that should not be here --
is loud once you look. The one that is not loud is a sweep that runs, raises
nothing, returns, and wrote nothing: a green worker, an empty audit trail, and
an operator who believes the chain is complete. So every test here asserts on
what is IN the database rather than on the call returning, and the counts the
sweep returns are asserted alongside, because "it ran" is satisfied by a sweep
that did nothing at all.

The second theme is the org. Keycloak's realm is shared by every tenant, and a
`userId` this platform cannot resolve is not a reason to pick a default: filing
one tenant's credential change into another tenant's evidence is worse than not
forwarding it. `test_an_unknown_keycloak_user_is_counted_never_filed_under_a
_guessed_org` is the one that would go red if somebody added a fallback.

No HTTP: `fetch` is injected. The client is thin by design (see its docstring)
and what is worth testing is everything downstream of it.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.models import AuditEvent, KeycloakEventIngest, Organization, User
from app.modules.keycloak_events.sweep import (
    LOOKBACK_CEILING_DAYS,
    SweepCounts,
    sweep_once,
)

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
EVENT_TIME_MS = 1_766_000_000_000


def _subject() -> str:
    """A Keycloak subject nothing else in this file uses.

    TWO SHARED-STATE HAZARDS MAKE THIS NECESSARY, and both are properties of
    the production design rather than of the fixtures:

    * `keycloak_event_ingest.fingerprint` is unique GLOBALLY -- one Keycloak
      event is forwarded once however many tenants exist -- and the subject is
      part of the fingerprint. A shared subject would mean the second test to
      run found its event already forwarded by the first.
    * `users` is unique on `(org_id, external_subject)`, so a subject reused
      across tests accumulates users in several orgs and every later test
      resolves it as ambiguous.

    The test database is shared across this file, so both bite by test order.
    """
    return f"kc-subject-{uuid4().hex[:12]}"


def _passkey_event(subject: str, **overrides: Any) -> dict[str, Any]:
    return {
        "time": EVENT_TIME_MS,
        "type": "UPDATE_CREDENTIAL",
        "realmId": "it-suite",
        "userId": subject,
        "sessionId": "sess-1",
        "details": {"credential_type": "webauthn-passwordless"},
        **overrides,
    }


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


async def _seed_org_with_user(engine: AsyncEngine, subject: str) -> tuple[UUID, UUID]:
    slug = f"kc-{uuid4().hex[:8]}"
    async with AsyncSession(engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.flush()
        user = User(org_id=org.id, external_subject=subject, email=f"{slug}@test.local")
        session.add(user)
        await session.commit()
        return org.id, user.id


def _settings(tmp_path: Any, *, configured: bool = True) -> SimpleNamespace:
    if not configured:
        return SimpleNamespace(
            keycloak_admin_url=None,
            keycloak_realm="it-suite",
            keycloak_events_client_id="it-suite-events",
            keycloak_events_secret_path=None,
        )
    secret = tmp_path / "keycloak-events.secret"
    secret.write_text("not-a-real-secret")
    return SimpleNamespace(
        keycloak_admin_url="https://keycloak.test",
        keycloak_realm="it-suite",
        keycloak_events_client_id="it-suite-events",
        keycloak_events_secret_path=str(secret),
    )


def _fetch(*events: dict[str, Any]) -> Any:
    """A stand-in for `client.fetch_events` that records the window asked for."""
    calls: list[Any] = []

    async def fetch(config: Any, *, since: Any, limit: int = 500) -> list[dict[str, Any]]:
        calls.append(since)
        return [dict(e) for e in events]

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


async def _audit_actions(engine: AsyncEngine, org_id: UUID) -> list[str]:
    """Scoped to ONE org deliberately. The admin engine bypasses RLS, so a
    global count passes whatever the sweep chose for `org_id` -- which is the
    single thing several of these tests exist to pin."""
    async with AsyncSession(engine) as session:
        rows = await session.execute(
            select(AuditEvent.action)
            .where(AuditEvent.org_id == org_id)
            .order_by(AuditEvent.id)
        )
        return list(rows.scalars())


async def _audit_rows_everywhere(engine: AsyncEngine, action: str) -> int:
    """DELIBERATELY UNSCOPED, and always read as a before/after delta.

    "Nothing was filed under a guessed org" is a claim about every org, so it
    cannot be asserted org-by-org -- but the test database is shared across
    this file, so an absolute count would be measuring the other tests. The
    delta is the only form of the assertion that is both global and honest."""
    async with AsyncSession(engine) as session:
        return int(
            (
                await session.execute(
                    select(func.count()).select_from(AuditEvent).where(
                        AuditEvent.action == action
                    )
                )
            ).scalar_one()
        )


async def _ingest_count(engine: AsyncEngine) -> int:
    """Also a delta, and for a second reason: `keycloak_event_ingest`'s
    uniqueness is GLOBAL rather than per-org, so there is no org to scope by."""
    async with AsyncSession(engine) as session:
        return int(
            (
                await session.execute(select(func.count()).select_from(KeycloakEventIngest))
            ).scalar_one()
        )


async def _clear_ledger(engine: AsyncEngine) -> None:
    """The window tests read the ledger's high-water mark, which is one number
    for the whole deployment -- so unlike everything else here they cannot be
    isolated by seeding their own org."""
    async with AsyncSession(engine) as session:
        await session.execute(delete(KeycloakEventIngest))
        await session.commit()


# --------------------------------------------------------------------------- #
# The unconfigured deployment
# --------------------------------------------------------------------------- #


async def test_no_credential_configured_is_a_no_op_that_does_not_raise(
    app_engine: AsyncEngine, admin_engine: AsyncEngine, tmp_path: Any
) -> None:
    """An operator who has not set this up has not broken anything, and the
    sweep must not take the worker's beat down every hour to say so. The gap is
    made VISIBLE by the `keycloak_events` probe, not by an exception here."""

    async def never(*a: Any, **kw: Any) -> list[dict[str, Any]]:
        raise AssertionError("fetched Keycloak with no credential configured")

    before = await _ingest_count(admin_engine)
    counts = await sweep_once(
        engine=app_engine,
        admin_engine=admin_engine,
        settings=_settings(tmp_path, configured=False),
        now=NOW,
        fetch=never,
    )

    assert counts == SweepCounts()
    assert await _ingest_count(admin_engine) == before


async def test_a_configured_secret_that_is_missing_raises_rather_than_reporting_zero(
    app_engine: AsyncEngine, admin_engine: AsyncEngine, tmp_path: Any
) -> None:
    """THE DIFFERENCE BETWEEN THE TWO SILENCES. "Not configured" is a choice;
    "configured, and the secret file is gone" is a broken deployment whose
    operator believes forwarding is running. Degrading the second into the
    first would hide exactly the case that needs to be loud."""
    settings = _settings(tmp_path)
    (tmp_path / "keycloak-events.secret").unlink()

    with pytest.raises(FileNotFoundError):
        await sweep_once(
            engine=app_engine,
            admin_engine=admin_engine,
            settings=settings,
            now=NOW,
            fetch=_fetch(),
        )


# --------------------------------------------------------------------------- #
# Resolving the org
# --------------------------------------------------------------------------- #


async def test_an_unknown_keycloak_user_is_counted_never_filed_under_a_guessed_org(
    app_engine: AsyncEngine, admin_engine: AsyncEngine, tmp_path: Any
) -> None:
    """THE ONE TO DEFEND. Keycloak's realm is shared by every tenant, so an
    unresolvable `userId` is not an invitation to pick the first org, the
    default org, or the org with the most users -- it is one tenant's
    credential change landing in another tenant's evidence, which is a tenancy
    breach written into an append-only log that cannot be corrected afterwards.

    The org seeded here exists precisely so a fallback would have somewhere to
    go: with no orgs at all, a buggy sweep would fail rather than mis-file."""
    org_id, _ = await _seed_org_with_user(admin_engine, _subject())
    event = _passkey_event(_subject())
    audit_before = await _audit_rows_everywhere(admin_engine, "auth.passkey_registered")
    ingest_before = await _ingest_count(admin_engine)

    counts = await sweep_once(
        engine=app_engine,
        admin_engine=admin_engine,
        settings=_settings(tmp_path),
        now=NOW,
        fetch=_fetch(event),
    )

    assert counts.fetched == 1
    assert counts.unresolved == 1
    assert counts.written == 0
    assert await _audit_actions(admin_engine, org_id) == []
    # NOT "not in this org" -- in NO org. The whole failure being guarded
    # against is the row appearing somewhere this test did not think to look.
    assert (
        await _audit_rows_everywhere(admin_engine, "auth.passkey_registered")
        == audit_before
    )
    assert await _ingest_count(admin_engine) == ingest_before


async def test_the_event_lands_in_the_org_of_the_user_it_belongs_to(
    app_engine: AsyncEngine, admin_engine: AsyncEngine, tmp_path: Any
) -> None:
    """Two orgs, and only one of them owns this Keycloak subject. Asserted on
    the BYPASSRLS admin engine and scoped per org: a global count would be
    satisfied by the row landing in either one."""
    subject = _subject()
    bystander_id, _ = await _seed_org_with_user(admin_engine, _subject())
    owner_id, _ = await _seed_org_with_user(admin_engine, subject)
    event = _passkey_event(subject)

    counts = await sweep_once(
        engine=app_engine,
        admin_engine=admin_engine,
        settings=_settings(tmp_path),
        now=NOW,
        fetch=_fetch(event),
    )

    assert counts.written == 1
    assert await _audit_actions(admin_engine, owner_id) == ["auth.passkey_registered"]
    assert await _audit_actions(admin_engine, bystander_id) == []


async def test_a_subject_that_two_orgs_both_claim_is_unresolved_not_arbitrary(
    app_engine: AsyncEngine, admin_engine: AsyncEngine, tmp_path: Any
) -> None:
    """`users` is unique on `(org_id, external_subject)`, NOT on the subject
    alone -- so two orgs holding the same subject is a legal database state,
    and "which one" has no answer the sweep is entitled to invent. Ambiguity
    resolves the same way an unknown user does: counted, reported, not filed."""
    subject = _subject()
    first_id, _ = await _seed_org_with_user(admin_engine, subject)
    second_id, _ = await _seed_org_with_user(admin_engine, subject)
    event = _passkey_event(subject)

    counts = await sweep_once(
        engine=app_engine,
        admin_engine=admin_engine,
        settings=_settings(tmp_path),
        now=NOW,
        fetch=_fetch(event),
    )

    assert counts.unresolved == 1
    assert counts.written == 0
    assert await _audit_actions(admin_engine, first_id) == []
    assert await _audit_actions(admin_engine, second_id) == []


# --------------------------------------------------------------------------- #
# What is written, and what is refused
# --------------------------------------------------------------------------- #


async def test_the_written_event_names_the_keycloak_user_and_the_event_type(
    app_engine: AsyncEngine, admin_engine: AsyncEngine, tmp_path: Any
) -> None:
    """An audit row saying only `auth.passkey_registered` is not evidence: the
    question an auditor asks is "whose, and on the strength of what". The
    Keycloak subject and the raw event type are what make this row traceable
    back to the source system that observed it."""
    subject = _subject()
    org_id, user_id = await _seed_org_with_user(admin_engine, subject)
    event = _passkey_event(subject)

    await sweep_once(
        engine=app_engine,
        admin_engine=admin_engine,
        settings=_settings(tmp_path),
        now=NOW,
        fetch=_fetch(event),
    )

    async with AsyncSession(admin_engine) as session:
        row = (
            await session.execute(select(AuditEvent).where(AuditEvent.org_id == org_id))
        ).scalar_one()

    assert row.action == "auth.passkey_registered"
    assert row.actor_type == "user"
    assert row.actor_id == user_id
    assert row.target_type == "user"
    assert row.target_id == str(user_id)
    assert row.meta["source"] == "keycloak"
    assert row.meta["keycloak_user_id"] == subject
    assert row.meta["keycloak_event_type"] == "UPDATE_CREDENTIAL"
    assert row.meta["realm"] == "it-suite"
    # The time KEYCLOAK observed it, which is not the time this row was
    # written: a sweep that has been down for a day writes yesterday's events
    # today, and the row must still say when the thing actually happened.
    assert row.meta["event_time_ms"] == EVENT_TIME_MS


async def test_a_login_is_fetched_and_refused(
    app_engine: AsyncEngine, admin_engine: AsyncEngine, tmp_path: Any
) -> None:
    """The firehose. Keycloak emits a LOGIN for every token exchange in the
    realm; forwarding them would bury the four events that matter under
    thousands that do not, and `auth.login` already exists in this chain
    written by the API itself."""
    subject = _subject()
    org_id, _ = await _seed_org_with_user(admin_engine, subject)
    login = _passkey_event(subject, type="LOGIN", details={})
    ingest_before = await _ingest_count(admin_engine)

    counts = await sweep_once(
        engine=app_engine,
        admin_engine=admin_engine,
        settings=_settings(tmp_path),
        now=NOW,
        fetch=_fetch(login),
    )

    assert counts.fetched == 1
    assert counts.skipped == 1
    assert counts.written == 0
    assert await _audit_actions(admin_engine, org_id) == []
    # Not recorded in the ledger either: the ledger exists to stop the same
    # event being WRITTEN twice, and an event that is never written has no
    # business consuming a row per login in the realm.
    assert await _ingest_count(admin_engine) == ingest_before


async def test_the_same_event_seen_twice_is_written_once(
    app_engine: AsyncEngine, admin_engine: AsyncEngine, tmp_path: Any
) -> None:
    """Keycloak's `dateFrom` is DAY-GRANULAR, so an hourly sweep re-reads the
    same day's events twenty-four times. A duplicate does not corrupt the hash
    chain; it pollutes it, and an auditor counting passkey registrations would
    count each one once per sweep."""
    subject = _subject()
    org_id, _ = await _seed_org_with_user(admin_engine, subject)
    event = _passkey_event(subject)
    settings = _settings(tmp_path)
    ingest_before = await _ingest_count(admin_engine)

    first = await sweep_once(
        engine=app_engine,
        admin_engine=admin_engine,
        settings=settings,
        now=NOW,
        fetch=_fetch(event),
    )
    second = await sweep_once(
        engine=app_engine,
        admin_engine=admin_engine,
        settings=settings,
        now=NOW + timedelta(hours=1),
        fetch=_fetch(event),
    )

    assert (first.written, second.written) == (1, 0)
    assert second.skipped == 1
    assert await _audit_actions(admin_engine, org_id) == ["auth.passkey_registered"]
    assert await _ingest_count(admin_engine) == ingest_before + 1


async def test_one_bad_event_does_not_take_the_rest_of_the_batch_down(
    app_engine: AsyncEngine, admin_engine: AsyncEngine, tmp_path: Any
) -> None:
    """These dicts come off an HTTP response from a system this project pins
    but does not build. A malformed row in the middle of a batch must cost that
    row, not the sweep -- otherwise one odd event stops forwarding for every
    tenant until somebody notices, which is the silence again."""
    subject = _subject()
    org_id, _ = await _seed_org_with_user(admin_engine, subject)

    counts = await sweep_once(
        engine=app_engine,
        admin_engine=admin_engine,
        settings=_settings(tmp_path),
        now=NOW,
        fetch=_fetch({"type": None}, {"nonsense": True}, _passkey_event(subject)),
    )

    assert counts.fetched == 3
    assert counts.written == 1
    assert await _audit_actions(admin_engine, org_id) == ["auth.passkey_registered"]


async def test_an_event_with_no_user_is_unresolved_not_written(
    app_engine: AsyncEngine, admin_engine: AsyncEngine, tmp_path: Any
) -> None:
    """A forwardable type carrying no `userId` cannot be attributed to anyone.
    There is no org for it and no actor for it, so it is counted like any other
    unresolvable event rather than written against nobody."""
    subject = _subject()
    org_id, _ = await _seed_org_with_user(admin_engine, subject)
    orphan = {k: v for k, v in _passkey_event(subject).items() if k != "userId"}

    counts = await sweep_once(
        engine=app_engine,
        admin_engine=admin_engine,
        settings=_settings(tmp_path),
        now=NOW,
        fetch=_fetch(orphan),
    )

    assert counts.unresolved == 1
    assert counts.written == 0
    assert await _audit_actions(admin_engine, org_id) == []


# --------------------------------------------------------------------------- #
# The window
# --------------------------------------------------------------------------- #


async def test_the_first_sweep_reaches_back_the_full_ceiling(
    app_engine: AsyncEngine, admin_engine: AsyncEngine, tmp_path: Any
) -> None:
    """An empty ledger means this deployment has never forwarded anything, and
    the events already sitting in Keycloak are the ones an operator turned this
    on to capture. Bounded at the ceiling so a first run is a backfill, not an
    unbounded scan of a realm's whole history."""
    await _clear_ledger(admin_engine)
    fetch = _fetch()

    await sweep_once(
        engine=app_engine,
        admin_engine=admin_engine,
        settings=_settings(tmp_path),
        now=NOW,
        fetch=fetch,
    )

    assert fetch.calls == [(NOW - timedelta(days=LOOKBACK_CEILING_DAYS)).date()]


async def test_the_window_catches_up_after_downtime_and_stays_bounded(
    app_engine: AsyncEngine, admin_engine: AsyncEngine, tmp_path: Any
) -> None:
    """A WATERMARK, NOT A FIXED LOOKBACK, and this is why. A worker down over a
    long weekend must not lose the events it was down for -- that is the audit
    gap nobody notices, arriving by way of an outage nobody connected to it.

    Bounded at the ceiling in the other direction: a worker down for six months
    catches up thirty days and no more, because an unbounded first fetch after
    a long outage is how a catch-up becomes an incident of its own."""
    settings = _settings(tmp_path)
    await _clear_ledger(admin_engine)
    subject = _subject()
    await _seed_org_with_user(admin_engine, subject)
    first = await sweep_once(
        engine=app_engine,
        admin_engine=admin_engine,
        settings=settings,
        now=NOW,
        fetch=_fetch(_passkey_event(subject)),
    )
    # The watermark this test then reads back only exists because something was
    # ingested. Asserted rather than assumed: a first sweep that quietly wrote
    # nothing would leave an empty ledger, and the ceiling branch below would
    # then be what the next two assertions measured -- passing for the wrong
    # reason and telling nobody the watermark had stopped moving.
    assert first.written == 1

    three_days_later = _fetch()
    await sweep_once(
        engine=app_engine,
        admin_engine=admin_engine,
        settings=settings,
        now=NOW + timedelta(days=3),
        fetch=three_days_later,
    )
    # One day BEHIND the last ingest, not from it: Keycloak filters by day, and
    # an event written late on the watermark's own day would otherwise sit just
    # outside every subsequent window.
    assert three_days_later.calls == [(NOW - timedelta(days=1)).date()]

    a_year_later = _fetch()
    await sweep_once(
        engine=app_engine,
        admin_engine=admin_engine,
        settings=settings,
        now=NOW + timedelta(days=365),
        fetch=a_year_later,
    )
    assert a_year_later.calls == [
        (NOW + timedelta(days=365) - timedelta(days=LOOKBACK_CEILING_DAYS)).date()
    ]


# --------------------------------------------------------------------------- #
# The record the probe reads, and the beat entry that makes any of this run
# --------------------------------------------------------------------------- #


async def test_the_last_sweep_record_round_trips() -> None:
    """What the `keycloak_events` probe reads. Through Redis because the sweep
    runs in the worker and the probe answers in the API process."""
    from app.modules.keycloak_events.sweep import read_last_sweep, write_last_sweep

    class _Redis:
        value: str | None = None

        async def set(self, _key: str, value: str) -> None:
            self.value = value

        async def get(self, _key: str) -> str | None:
            return self.value

    redis = _Redis()
    assert await read_last_sweep(redis) is None

    await write_last_sweep(redis, SweepCounts(fetched=9, written=2, skipped=7), now=1000.0)
    record = await read_last_sweep(redis)
    assert record == {
        "ts": 1000.0,
        "fetched": 9,
        "written": 2,
        "unresolved": 0,
        "skipped": 7,
    }


async def test_an_unreadable_record_is_never_run_not_a_crash() -> None:
    """A key somebody hand-edited, or one written by a version that framed the
    record differently. The probe must degrade, not raise: a probe that raised
    would report the status endpoint broken rather than the forwarding."""
    from app.modules.keycloak_events.sweep import read_last_sweep

    class _Redis:
        async def get(self, _key: str) -> str:
            return "{not json"

    assert await read_last_sweep(_Redis()) is None


async def test_the_sweep_is_actually_scheduled(
    pg_cluster: Any, tmp_path: Any
) -> None:
    """A SWEEP NOTHING SCHEDULES IS THE SILENCE THIS MODULE EXISTS TO PREVENT,
    arriving one layer up: every test above would still pass, the probe would
    report `configured, never run` for ever, and the audit chain would stay
    empty. Read from the module's own constant rather than a literal, so the
    beat interval and the staleness threshold the probe measures against cannot
    drift apart."""
    from app import worker
    from app.core.config import Settings, override_settings
    from app.modules.keycloak_events.sweep import SWEEP_INTERVAL_S

    settings = Settings(
        database_url=pg_cluster.app_url,
        database_admin_url=pg_cluster.admin_url,
        oidc_issuer="https://kc.example/realms/it-suite",
        oidc_jwks_url="https://kc.example/realms/it-suite/certs",
        ca_dir=tmp_path / "ca",
        signing_key_path=tmp_path / "job-signing.key",
        redis_url="redis://localhost:6379/9",
    )
    with override_settings(settings):
        schedule = worker.create_celery().conf.beat_schedule
    entry = schedule["keycloak-event-sweep"]
    assert entry["task"] == "app.worker.keycloak_event_sweep"
    assert entry["schedule"] == SWEEP_INTERVAL_S
