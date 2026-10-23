"""One delivery attempt, and the retry classification around it
(docs/modules/notification/spec.md §5, §7).

FIXTURE PATTERN. `api_env`, `AsyncSession(api_env.admin_engine, ...)`, and
this module's own service functions (`create_channel`) build real rows --
same convention as `test_notification_routing.py` (Task 8) and
`test_notification_channels.py` (Task 6). `NotificationDelivery`/`DomainEvent`
rows are constructed directly via the ORM: no service function writes them
yet (that is Task 10's `drain_outbox`), so this module's own contract is
exercised by handing `attempt_delivery` a delivery row this test file built
itself, exactly as the task brief's interface implies.

WHY THE PURE FUNCTIONS ARE TESTED WITHOUT A SESSION OR NETWORK. `is_retryable`
and `next_attempt_delay` take no `AsyncSession` and open no socket -- they are
plain functions over ints/strings, so they are called directly rather than
through `attempt_delivery`.

WHY `attempt_delivery` NEVER MAKES A REAL NETWORK CALL IN THESE TESTS.
Webhook: `app.core.egress._resolve_all`/`_build_transport` are monkeypatched
exactly as `test_egress_guard.py` does (an `httpx.MockTransport` stands in
for the real socket). Email: `smtplib.SMTP` itself is monkeypatched to a
stub that records what it was called with and never opens a socket --
`aiosmtplib` is not a dependency of this project (see `delivery.py`'s module
docstring), and stdlib `smtplib.SMTP` is a plain class, trivial to replace
for the duration of one test.

THE EGRESS-REASON DRIFT TEST. `test_every_egress_blocked_reason_in_the_module_is_terminal`
does not hand-copy the four reasons the task brief's own sample code lists --
it reads `app/core/egress.py`'s SOURCE and extracts every string literal
passed as the first argument to `EgressBlocked(...)`, then asserts
`is_retryable(None, reason)` is `False` for every one of them. If a future
edit to `egress.py` adds an eighth reason, this test starts covering it
automatically instead of silently missing it the way a hand-copied list
would.
"""

import json
import re
import smtplib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from uuid_extensions import uuid7

from app.core.config import override_settings
from app.core.vault import LocalVaultService
from app.models import AuditEvent, DomainEvent, NotificationChannel, NotificationDelivery, User
from app.modules.notification.channels import create_channel
from app.modules.notification.delivery import (
    BACKOFF_SECONDS,
    MAX_ATTEMPTS,
    attempt_delivery,
    is_retryable,
    next_attempt_delay,
)
from app.modules.notification.schema import ChannelCreate

EGRESS_PY = Path(__file__).resolve().parent.parent / "app" / "core" / "egress.py"


# ---------------------------------------------------------------------------
# Pure functions -- no DB, no network (per the task brief).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", [500, 502, 503, 504, 429])
def test_transient_status_codes_retry(code: int) -> None:
    assert is_retryable(code, None) is True


@pytest.mark.parametrize("code", [400, 401, 403, 404, 422])
def test_client_errors_are_terminal(code: int) -> None:
    # Retrying a 403 five times is five more chances to be wrong, not one
    # chance to succeed.
    assert is_retryable(code, None) is False


@pytest.mark.parametrize(
    "reason", ["blocked_private", "dns_failure", "redirect_refused", "scheme_not_allowed"]
)
def test_egress_rejections_are_terminal(reason: str) -> None:
    """Spec §7 -- an allowlist-blocked host must not be retried. Five retries
    of a blocked destination are five more SSRF attempts."""
    assert is_retryable(None, reason) is False


def _every_egress_blocked_reason() -> set[str]:
    """Every reason string `EgressBlocked(...)` is raised with in
    `app/core/egress.py`, read from the source rather than hand-copied --
    see this module's docstring."""
    source = EGRESS_PY.read_text()
    return set(re.findall(r'EgressBlocked\(\s*"([a-z_]+)"', source))


def test_every_egress_blocked_reason_in_the_module_is_terminal() -> None:
    reasons = _every_egress_blocked_reason()
    # A sanity floor so a refactor of egress.py that accidentally stops
    # raising `EgressBlocked` with string literals (e.g. moves the reasons
    # into an enum this regex no longer matches) fails LOUD here instead of
    # this test silently checking zero reasons and passing vacuously.
    assert len(reasons) >= 7, reasons
    for reason in reasons:
        assert is_retryable(None, reason) is False, reason
        assert is_retryable(500, reason) is False, reason  # error_reason wins over status_code


def test_backoff_schedule_matches_the_spec() -> None:
    # Spec §7: 1m, 5m, 25m, 2h, 10h -- five gaps, six attempts (I4 fix:
    # MAX_ATTEMPTS is DERIVED as len(BACKOFF_SECONDS) + 1, not a second
    # hand-picked number that can drift out of sync with the schedule --
    # before this fix it was `len(BACKOFF_SECONDS)`, which gave five
    # attempts only FOUR gaps and left the 10h delay unreachable).
    assert BACKOFF_SECONDS == (60, 300, 1500, 7200, 36000)
    assert MAX_ATTEMPTS == len(BACKOFF_SECONDS) + 1


def test_delay_is_none_once_attempts_are_exhausted() -> None:
    assert next_attempt_delay(len(BACKOFF_SECONDS)) is None
    assert next_attempt_delay(MAX_ATTEMPTS) is None
    assert next_attempt_delay(MAX_ATTEMPTS + 1) is None


def test_delay_grows_monotonically() -> None:
    # Only `len(BACKOFF_SECONDS)` real gaps exist, one FEWER than
    # `MAX_ATTEMPTS` (I4 fix) -- see `next_attempt_delay`'s own docstring for
    # why it is bounded against `len(BACKOFF_SECONDS)`, not `MAX_ATTEMPTS`.
    delays = [next_attempt_delay(i) for i in range(len(BACKOFF_SECONDS))]
    assert delays == sorted(d for d in delays if d is not None)
    assert None not in delays


@pytest.mark.parametrize("reason", ["timeout", "connection_reset"])
def test_transport_level_failures_retry(reason: str) -> None:
    assert is_retryable(None, reason) is True


def test_smtp_permanent_reject_reason_is_terminal() -> None:
    assert is_retryable(None, "smtp_permanent_reject") is False
    # An SMTP reply code must never be classified through the HTTP status
    # table -- 500 over SMTP is PERMANENT (RFC 5321), the opposite of HTTP's
    # meaning. `is_retryable` must not accidentally launder it through
    # `status_code` once an `error_reason` is present.
    assert is_retryable(500, "smtp_permanent_reject") is False


def test_smtp_transient_reject_retries() -> None:
    assert is_retryable(None, "smtp_transient_reject") is True


def test_unknown_reason_defaults_terminal() -> None:
    """Fail-safe direction: a reason this module has never seen is terminal,
    not retryable -- see `is_retryable`'s docstring."""
    assert is_retryable(None, "some_future_reason_nobody_taught_this_function") is False


def test_channel_disabled_reason_is_terminal() -> None:
    """I2 fix -- disabling a channel is an immediate, terminal outcome for
    anything already queued against it, not a retryable one."""
    assert is_retryable(None, "channel_disabled") is False


def test_total_retry_window_matches_the_spec() -> None:
    """I4 fix -- the assertion that would have caught the original defect:
    before this fix `MAX_ATTEMPTS == len(BACKOFF_SECONDS)` gave five attempts
    only FOUR gaps, so the real window was 2.52h (1m+5m+25m+2h), not the
    12.52h all five delays sum to -- a test on each individual delay in
    isolation never noticed, because `next_attempt_delay(4)` (the unreachable
    10h gap) was still unit-tested and passed on its own. This asserts the
    TOTAL elapsed window instead."""
    total_window_s = sum(BACKOFF_SECONDS)
    assert total_window_s == 60 + 300 + 1500 + 7200 + 36000
    assert total_window_s == 45060
    assert total_window_s / 3600 == pytest.approx(12.52, abs=0.01)


# ---------------------------------------------------------------------------
# attempt_delivery -- fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _egress_settings(request: pytest.FixtureRequest):
    """`post_json` calls `get_settings()` unconditionally (spec §4.1's
    platform-host denial) against the module-level singleton `app.core.
    config._settings`, which `api_env` never populates (it builds its own
    `Settings` instance and hands it straight to `create_app`, matching every
    other test module). Every webhook test in this file needs the singleton
    populated with a harmless baseline, exactly as `test_egress_guard.py`'s
    own `_baseline_settings` autouse fixture does -- reused here rather than
    re-derived, via the same `api_env.settings` the test's own app already
    runs on. Guarded on `api_env` actually being requested so the dozen pure
    `is_retryable`/`next_attempt_delay` tests above don't pay for spinning up
    a FastAPI app they never asked for."""
    if "api_env" not in request.fixturenames:
        yield
        return
    api_env = request.getfixturevalue("api_env")
    with override_settings(api_env.settings):
        yield


async def _org_with_user(api_env) -> tuple[UUID, UUID]:
    org_id = await api_env.create_org("notif-delivery")
    sub = await api_env.create_user(org_id, "org-admin")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        user_id = (
            await session.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()
    return org_id, user_id


def _vault(tmp_path: Path) -> LocalVaultService:
    return LocalVaultService(tmp_path / "root.key")


async def _webhook_channel(
    session: AsyncSession,
    org_id: UUID,
    user_id: UUID,
    vault: LocalVaultService,
    *,
    url: str = "https://hooks.example.com/x",
    secret: str | None = None,
    name: str = "ops-webhook",
) -> NotificationChannel:
    return await create_channel(
        session,
        org_id=org_id,
        actor_id=user_id,
        data=ChannelCreate(kind="webhook", name=name, config={"url": url}),
        secret=secret,
        vault=vault,
    )


async def _email_channel(
    session: AsyncSession,
    org_id: UUID,
    user_id: UUID,
    vault: LocalVaultService,
    *,
    secret: str | None = None,
    use_tls: bool = True,
    name: str = "ops-email",
) -> NotificationChannel:
    return await create_channel(
        session,
        org_id=org_id,
        actor_id=user_id,
        data=ChannelCreate(
            kind="email",
            name=name,
            config={
                "host": "mail.internal.example.com",
                "port": 587,
                "from_address": "noreply@toeshee.test",
                "use_tls": use_tls,
            },
        ),
        secret=secret,
        vault=vault,
    )


async def _event(
    session: AsyncSession, org_id: UUID, *, event_type: str = "ticket.assigned"
) -> DomainEvent:
    now = datetime.now(UTC)
    event = DomainEvent(
        org_id=org_id,
        event_type=event_type,
        subject_type="ticket",
        subject_id=uuid7(),
        payload={"assignee_id": None},
        occurred_at=now,
        created_at=now,
    )
    session.add(event)
    await session.flush()
    return event


async def _delivery(
    session: AsyncSession,
    org_id: UUID,
    *,
    channel_id: UUID,
    event_id: UUID,
    recipient: str,
    attempts: int = 0,
    status: str = "pending",
) -> NotificationDelivery:
    delivery = NotificationDelivery(
        org_id=org_id,
        event_id=event_id,
        channel_id=channel_id,
        recipient=recipient,
        status=status,
        attempts=attempts,
    )
    session.add(delivery)
    await session.flush()
    return delivery


async def _audit_events(session: AsyncSession, org_id: UUID, action: str) -> list[AuditEvent]:
    return list(
        (
            await session.execute(
                select(AuditEvent).where(AuditEvent.org_id == org_id, AuditEvent.action == action)
            )
        )
        .scalars()
        .all()
    )


def _mock_transport(
    status_code: int, *, body: str = "", captured: list[httpx.Request] | None = None
):
    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.append(request)
        return httpx.Response(status_code, text=body)

    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# attempt_delivery -- webhook
# ---------------------------------------------------------------------------


async def test_webhook_success_marks_sent_and_carries_the_event_id_header(
    api_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    org_id, user_id = await _org_with_user(api_env)
    vault = _vault(tmp_path)
    captured: list[httpx.Request] = []
    monkeypatch.setattr("app.core.egress._resolve_all", lambda h, p: ["93.184.216.34"])
    monkeypatch.setattr(
        "app.core.egress._build_transport",
        lambda ip: _mock_transport(200, captured=captured),
    )

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await _webhook_channel(
            session, org_id, user_id, vault, secret="bearer-token-abc123"
        )
        event = await _event(session, org_id)
        delivery = await _delivery(
            session,
            org_id,
            channel_id=channel.id,
            event_id=event.id,
            recipient="hooks.example.com",
        )

        await attempt_delivery(session, delivery=delivery, vault=vault)

        assert delivery.status == "sent"
        assert delivery.attempts == 1
        assert delivery.sent_at is not None
        assert delivery.last_error is None
        assert delivery.last_status_code == 200
        assert delivery.next_attempt_at is None

    assert len(captured) == 1
    request = captured[0]
    assert request.headers["x-toeshee-event-id"] == str(event.id)
    assert request.headers["authorization"] == "Bearer bearer-token-abc123"


async def test_webhook_credential_checkout_is_audited_and_never_carries_the_secret(
    api_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    org_id, user_id = await _org_with_user(api_env)
    vault = _vault(tmp_path)
    secret = "super-secret-bearer-token-xyz"
    monkeypatch.setattr("app.core.egress._resolve_all", lambda h, p: ["93.184.216.34"])
    monkeypatch.setattr("app.core.egress._build_transport", lambda ip: _mock_transport(200))

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await _webhook_channel(session, org_id, user_id, vault, secret=secret)
        event = await _event(session, org_id)
        delivery = await _delivery(
            session,
            org_id,
            channel_id=channel.id,
            event_id=event.id,
            recipient="hooks.example.com",
        )

        with caplog.at_level("DEBUG"):
            await attempt_delivery(session, delivery=delivery, vault=vault)

        assert secret not in caplog.text

        checkouts = await _audit_events(session, org_id, "notification.credential_used")
        assert len(checkouts) == 1
        assert checkouts[0].target_id == str(channel.credential_id)
        assert secret not in json.dumps(checkouts[0].meta)
        assert secret not in str(delivery.last_error)
        # M7 fix's default: the WORKER path (no human triggered this) audits
        # as `("system", None)` -- contrast with `send_test`'s
        # `("user", ctx.user_id)` (see test_notification_api.py's
        # `test_test_send_credential_checkout_is_attributed_to_the_calling_user`).
        assert checkouts[0].actor_type == "system"
        assert checkouts[0].actor_id is None


async def test_webhook_no_credential_writes_no_audit_event(
    api_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec §8: delivery *attempts* are not audited -- only credential
    checkouts are. A channel with no credential should leave zero audit rows
    behind for this attempt."""
    org_id, user_id = await _org_with_user(api_env)
    vault = _vault(tmp_path)
    monkeypatch.setattr("app.core.egress._resolve_all", lambda h, p: ["93.184.216.34"])
    monkeypatch.setattr("app.core.egress._build_transport", lambda ip: _mock_transport(200))

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await _webhook_channel(session, org_id, user_id, vault, secret=None)
        event = await _event(session, org_id)
        delivery = await _delivery(
            session,
            org_id,
            channel_id=channel.id,
            event_id=event.id,
            recipient="hooks.example.com",
        )
        # Baseline BEFORE the attempt -- `create_channel` above already wrote
        # its own `notification.channel_created` audit row; what this test
        # asserts is that the ATTEMPT itself adds none, not that the org's
        # audit log is empty.
        before = (
            (await session.execute(select(AuditEvent).where(AuditEvent.org_id == org_id)))
            .scalars()
            .all()
        )
        await attempt_delivery(session, delivery=delivery, vault=vault)

        after = (
            (await session.execute(select(AuditEvent).where(AuditEvent.org_id == org_id)))
            .scalars()
            .all()
        )
    assert len(after) == len(before)


@pytest.mark.parametrize(
    "resolved_ip,label",
    [(["10.0.0.5"], "private"), ([], "dns_failure")],
)
async def test_webhook_egress_rejection_is_terminal_on_the_first_attempt(
    api_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resolved_ip: list[str], label: str
) -> None:
    """Spec §7 -- an egress rejection goes straight to `dead`, even on
    attempt #1; it must never be scheduled for retry, because retrying it is
    just more attempts to reach a blocked destination (or, for dns_failure,
    burning attempts on a host that plainly will not resolve any differently
    a minute from now for the reasons this deployment blocked it)."""
    org_id, user_id = await _org_with_user(api_env)
    vault = _vault(tmp_path)
    monkeypatch.setattr("app.core.egress._resolve_all", lambda h, p: resolved_ip)

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await _webhook_channel(
            session, org_id, user_id, vault, url="https://internal.example.com/hook"
        )
        event = await _event(session, org_id)
        delivery = await _delivery(
            session,
            org_id,
            channel_id=channel.id,
            event_id=event.id,
            recipient="internal.example.com",
        )
        await attempt_delivery(session, delivery=delivery, vault=vault)

        assert delivery.status == "dead", label
        assert delivery.attempts == 1
        assert delivery.next_attempt_at is None
        assert delivery.last_status_code is None
        # The reason category, never the resolved private IP itself.
        assert "10.0.0.5" not in str(delivery.last_error)


async def test_webhook_5xx_is_retryable_and_schedules_the_first_backoff(
    api_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    org_id, user_id = await _org_with_user(api_env)
    vault = _vault(tmp_path)
    monkeypatch.setattr("app.core.egress._resolve_all", lambda h, p: ["93.184.216.34"])
    monkeypatch.setattr(
        "app.core.egress._build_transport", lambda ip: _mock_transport(503, body="upstream down")
    )

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await _webhook_channel(session, org_id, user_id, vault)
        event = await _event(session, org_id)
        delivery = await _delivery(
            session,
            org_id,
            channel_id=channel.id,
            event_id=event.id,
            recipient="hooks.example.com",
        )
        before = datetime.now(UTC)
        await attempt_delivery(session, delivery=delivery, vault=vault)

        assert delivery.status == "failed"
        assert delivery.attempts == 1
        assert delivery.last_status_code == 503
        assert delivery.next_attempt_at is not None
        delta = (delivery.next_attempt_at - before).total_seconds()
        assert BACKOFF_SECONDS[0] - 5 <= delta <= BACKOFF_SECONDS[0] + 30


async def test_webhook_429_is_retryable(
    api_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    org_id, user_id = await _org_with_user(api_env)
    vault = _vault(tmp_path)
    monkeypatch.setattr("app.core.egress._resolve_all", lambda h, p: ["93.184.216.34"])
    monkeypatch.setattr("app.core.egress._build_transport", lambda ip: _mock_transport(429))

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await _webhook_channel(session, org_id, user_id, vault)
        event = await _event(session, org_id)
        delivery = await _delivery(
            session, org_id, channel_id=channel.id, event_id=event.id, recipient="hooks.example.com"
        )
        await attempt_delivery(session, delivery=delivery, vault=vault)
        assert delivery.status == "failed"
        assert delivery.next_attempt_at is not None


async def test_webhook_4xx_other_than_429_is_terminal(
    api_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    org_id, user_id = await _org_with_user(api_env)
    vault = _vault(tmp_path)
    monkeypatch.setattr("app.core.egress._resolve_all", lambda h, p: ["93.184.216.34"])
    monkeypatch.setattr(
        "app.core.egress._build_transport", lambda ip: _mock_transport(403, body="forbidden")
    )

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await _webhook_channel(session, org_id, user_id, vault)
        event = await _event(session, org_id)
        delivery = await _delivery(
            session, org_id, channel_id=channel.id, event_id=event.id, recipient="hooks.example.com"
        )
        await attempt_delivery(session, delivery=delivery, vault=vault)
        assert delivery.status == "dead"
        assert delivery.attempts == 1
        assert delivery.last_status_code == 403
        assert delivery.next_attempt_at is None


async def test_webhook_retries_exhaust_to_dead_at_the_cap(
    api_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A delivery already on its LAST allowed attempt (`MAX_ATTEMPTS - 1`
    already made) that fails again with a retryable status must go straight
    to `dead`, not be scheduled for a 6th attempt -- proves the cap through
    `attempt_delivery`, not just through `next_attempt_delay` in isolation."""
    org_id, user_id = await _org_with_user(api_env)
    vault = _vault(tmp_path)
    monkeypatch.setattr("app.core.egress._resolve_all", lambda h, p: ["93.184.216.34"])
    monkeypatch.setattr("app.core.egress._build_transport", lambda ip: _mock_transport(500))

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await _webhook_channel(session, org_id, user_id, vault)
        event = await _event(session, org_id)
        delivery = await _delivery(
            session,
            org_id,
            channel_id=channel.id,
            event_id=event.id,
            recipient="hooks.example.com",
            attempts=MAX_ATTEMPTS - 1,
            status="failed",
        )
        await attempt_delivery(session, delivery=delivery, vault=vault)
        assert delivery.attempts == MAX_ATTEMPTS
        assert delivery.status == "dead"
        assert delivery.next_attempt_at is None


async def test_webhook_retry_then_success_clears_last_error(
    api_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    org_id, user_id = await _org_with_user(api_env)
    vault = _vault(tmp_path)
    monkeypatch.setattr("app.core.egress._resolve_all", lambda h, p: ["93.184.216.34"])

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await _webhook_channel(session, org_id, user_id, vault)
        event = await _event(session, org_id)
        delivery = await _delivery(
            session, org_id, channel_id=channel.id, event_id=event.id, recipient="hooks.example.com"
        )

        monkeypatch.setattr("app.core.egress._build_transport", lambda ip: _mock_transport(500))
        await attempt_delivery(session, delivery=delivery, vault=vault)
        assert delivery.status == "failed"
        assert delivery.last_error is not None
        assert delivery.attempts == 1

        monkeypatch.setattr("app.core.egress._build_transport", lambda ip: _mock_transport(200))
        await attempt_delivery(session, delivery=delivery, vault=vault)
        assert delivery.status == "sent"
        assert delivery.last_error is None
        assert delivery.attempts == 2
        assert delivery.sent_at is not None


async def test_webhook_event_id_header_is_stable_across_retries(
    api_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec §7 -- at-least-once delivery needs a STABLE dedupe key across
    retries of the same delivery, so a consumer that already processed an
    earlier attempt's request can recognise a later retry as the same event."""
    org_id, user_id = await _org_with_user(api_env)
    vault = _vault(tmp_path)
    seen_ids: list[str] = []
    monkeypatch.setattr("app.core.egress._resolve_all", lambda h, p: ["93.184.216.34"])

    def handler(request: httpx.Request) -> httpx.Response:
        seen_ids.append(request.headers["x-toeshee-event-id"])
        return httpx.Response(500)

    monkeypatch.setattr("app.core.egress._build_transport", lambda ip: httpx.MockTransport(handler))

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await _webhook_channel(session, org_id, user_id, vault)
        event = await _event(session, org_id)
        delivery = await _delivery(
            session, org_id, channel_id=channel.id, event_id=event.id, recipient="hooks.example.com"
        )
        await attempt_delivery(session, delivery=delivery, vault=vault)
        await attempt_delivery(session, delivery=delivery, vault=vault)

    assert len(seen_ids) == 2
    assert seen_ids[0] == seen_ids[1] == str(event.id)


async def test_webhook_connection_error_is_retryable(
    api_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    org_id, user_id = await _org_with_user(api_env)
    vault = _vault(tmp_path)
    monkeypatch.setattr("app.core.egress._resolve_all", lambda h, p: ["93.184.216.34"])

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr("app.core.egress._build_transport", lambda ip: httpx.MockTransport(handler))

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await _webhook_channel(session, org_id, user_id, vault)
        event = await _event(session, org_id)
        delivery = await _delivery(
            session, org_id, channel_id=channel.id, event_id=event.id, recipient="hooks.example.com"
        )
        await attempt_delivery(session, delivery=delivery, vault=vault)
        assert delivery.status == "failed"
        assert delivery.next_attempt_at is not None


# ---------------------------------------------------------------------------
# attempt_delivery -- the channel active gate (whole-branch review finding
# I2). `NotificationChannel.active` used to be consulted by no decision
# anywhere -- `PATCH {"active": false}` flipped only the UI badge; deliveries
# kept sending. Gated here, in the delivery path (routing.py is out of scope
# for this fix), as a TERMINAL, immediate outcome.
# ---------------------------------------------------------------------------


async def test_inactive_channel_dead_letters_without_opening_a_socket(
    api_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    org_id, user_id = await _org_with_user(api_env)
    vault = _vault(tmp_path)

    def _must_not_resolve(host: str, port: int) -> list[str]:
        raise AssertionError("a disabled channel must never reach DNS/network at all")

    monkeypatch.setattr("app.core.egress._resolve_all", _must_not_resolve)

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await create_channel(
            session,
            org_id=org_id,
            actor_id=user_id,
            data=ChannelCreate(
                kind="webhook",
                name="disabled",
                config={"url": "https://hooks.example.com/x"},
                active=False,
            ),
            secret=None,
            vault=vault,
        )
        event = await _event(session, org_id)
        delivery = await _delivery(
            session, org_id, channel_id=channel.id, event_id=event.id, recipient="hooks.example.com"
        )
        await attempt_delivery(session, delivery=delivery, vault=vault)

        assert delivery.status == "dead"
        assert delivery.attempts == 1
        assert delivery.next_attempt_at is None
        assert delivery.last_error is not None


async def test_disabling_a_channel_mid_retry_dead_letters_the_next_attempt(
    api_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The kill-switch use case named in the review: a channel that was
    active (and already retrying) gets disabled once an admin notices it is
    misconfigured. The NEXT attempt against it must go straight to `dead`,
    not keep retrying against whatever destination it was pointed at."""
    org_id, user_id = await _org_with_user(api_env)
    vault = _vault(tmp_path)
    monkeypatch.setattr("app.core.egress._resolve_all", lambda h, p: ["93.184.216.34"])
    monkeypatch.setattr(
        "app.core.egress._build_transport", lambda ip: _mock_transport(500, body="down")
    )

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await _webhook_channel(session, org_id, user_id, vault)
        event = await _event(session, org_id)
        delivery = await _delivery(
            session, org_id, channel_id=channel.id, event_id=event.id, recipient="hooks.example.com"
        )
        await attempt_delivery(session, delivery=delivery, vault=vault)
        assert delivery.status == "failed"  # retrying normally so far

        channel.active = False
        session.add(channel)
        await session.flush()

        await attempt_delivery(session, delivery=delivery, vault=vault)
        assert delivery.status == "dead"
        assert delivery.attempts == 2
        assert delivery.next_attempt_at is None


async def test_active_channel_still_delivers_normally(
    api_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive control: the gate above must not affect an ordinary, active
    channel's delivery."""
    org_id, user_id = await _org_with_user(api_env)
    vault = _vault(tmp_path)
    monkeypatch.setattr("app.core.egress._resolve_all", lambda h, p: ["93.184.216.34"])
    monkeypatch.setattr("app.core.egress._build_transport", lambda ip: _mock_transport(200))

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await create_channel(
            session,
            org_id=org_id,
            actor_id=user_id,
            data=ChannelCreate(
                kind="webhook",
                name="ops-active",
                config={"url": "https://hooks.example.com/x"},
                active=True,
            ),
            secret=None,
            vault=vault,
        )
        event = await _event(session, org_id)
        delivery = await _delivery(
            session, org_id, channel_id=channel.id, event_id=event.id, recipient="hooks.example.com"
        )
        await attempt_delivery(session, delivery=delivery, vault=vault)
        assert delivery.status == "sent"


# ---------------------------------------------------------------------------
# The webhook wire format, pinned per event type (whole-branch review finding
# I5). `_event_payload` forwards `event.payload` verbatim under the outer
# envelope; this module has no projection of its own -- a per-event-type test
# capturing the ACTUAL outbound `httpx.Request` body is what stops a future
# publisher change in a DIFFERENT module (service_desk) from silently
# altering what an external consumer receives.
# ---------------------------------------------------------------------------

_EVENT_TYPE_SAMPLE_PAYLOADS: dict[str, dict[str, Any]] = {
    "ticket.assigned": {
        "ticket_number": "INC-1",
        "subject": "s",
        "queue_id": None,
        "assignee_id": None,
        "requester_id": None,
    },
    "ticket.status_changed": {
        "ticket_number": "INC-1",
        "subject": "s",
        "from_status": "open",
        "to_status": "resolved",
        "queue_id": None,
        "assignee_id": None,
        "requester_id": None,
    },
    "ticket.comment_added": {
        "ticket_number": "INC-1",
        "subject": "s",
        "body": "hi there",
        "queue_id": None,
        "assignee_id": None,
        "requester_id": None,
    },
    "ticket.internal_note_added": {
        "ticket_number": "INC-1",
        "subject": "s",
        "queue_id": None,
        "assignee_id": None,
        "requester_id": None,
    },
    "ticket.approval_requested": {
        "ticket_number": "INC-1",
        "subject": "s",
        "queue_id": None,
        "assignee_id": None,
        "requester_id": None,
    },
    "ticket.approval_decided": {
        "ticket_number": "INC-1",
        "subject": "s",
        "decision": "approved",
        "queue_id": None,
        "assignee_id": None,
        "requester_id": None,
    },
    "ticket.resolved": {
        "ticket_number": "INC-1",
        "subject": "s",
        "queue_id": None,
        "assignee_id": None,
        "requester_id": None,
    },
    # Published by `app.modules.sla` (docs/modules/sla/spec.md §6), not by
    # `service_desk` -- shape deliberately differs from every other ticket
    # event above: no `requester_id` at all (SLA breach is a staff-only
    # signal, never routed to the requester -- `_render_ticket_sla_breached`'s
    # docstring), and adds `ticket_id`/`target`/`due_at`/`policy_id`/
    # `priority`, none of which any other ticket event carries.
    # `ticket_number` uses the real, un-padded `INC-<n>` shape
    # `service_desk/numbering.py::next_number` actually produces, not a
    # zero-padded guess.
    "ticket.sla_breached": {
        "ticket_id": "11111111-1111-1111-1111-111111111111",
        "ticket_number": "INC-123",
        "subject": "s",
        "target": "response",
        "due_at": "2026-08-01T09:00:00+00:00",
        "policy_id": "22222222-2222-2222-2222-222222222222",
        "queue_id": "33333333-3333-3333-3333-333333333333",
        "priority": "high",
    },
    # Published by `app.modules.health` (docs/modules/endpoint-health-alerting/
    # spec.md §9.1), not by `service_desk`. Every key of that section's payload
    # contract is present on all four, including the ones whose value is null,
    # because §9.1 makes "present-and-null" the convention: a reader must never
    # have to distinguish "absent" from "null".
    #
    # `queue_id` IS NON-NULL AND THAT IS LOAD-BEARING, not decoration.
    # `routing.resolve_recipients` narrows rule selection to `queue_id IS NULL`
    # rules when a payload carries no queue, and `queue_members` then expands
    # against a NULL queue and returns []. A queue-less health event is
    # therefore routable only to `channel_default` -- one webhook for the whole
    # org, which is not an on-call rota.
    #
    # `observed_value` and `threshold` are STRINGS. Health alerts never put a
    # number on this wire: a numeric column is the thing someone later
    # aggregates over time, which is exactly what spec §0.12 exists to prevent.
    "health.alert_raised": {
        "alert_id": "44444444-4444-4444-4444-444444444444",
        "device_id": "55555555-5555-5555-5555-555555555555",
        "hostname": "web-01",
        "check_type": "disk.free_pct",
        "selector": "C:",
        "severity": "critical",
        "state": "active",
        "observed_value": "3.1",
        "threshold": "10",
        "comparator": "lt",
        "opened_at": "2026-08-02T09:14:00+00:00",
        "cleared_at": None,
        "cleared_reason": None,
        "flapping": False,
        "ticket_id": None,
        "ticket_number": None,
        "remediation_job_id": None,
        "remediation_action_key": None,
        "queue_id": "33333333-3333-3333-3333-333333333333",
        "assignee_id": None,
        "requester_id": None,
    },
    "health.alert_cleared": {
        "alert_id": "44444444-4444-4444-4444-444444444444",
        "device_id": "55555555-5555-5555-5555-555555555555",
        "hostname": "web-01",
        "check_type": "disk.free_pct",
        "selector": "C:",
        "severity": "critical",
        "state": "cleared",
        "observed_value": "22.7",
        "threshold": "10",
        "comparator": "lt",
        "opened_at": "2026-08-02T09:14:00+00:00",
        "cleared_at": "2026-08-02T10:02:00+00:00",
        "cleared_reason": "recovered",
        "flapping": False,
        "ticket_id": "66666666-6666-6666-6666-666666666666",
        "ticket_number": "INC-124",
        "remediation_job_id": None,
        "remediation_action_key": None,
        "queue_id": "33333333-3333-3333-3333-333333333333",
        "assignee_id": None,
        "requester_id": None,
    },
    "health.alert_escalated": {
        "alert_id": "44444444-4444-4444-4444-444444444444",
        "device_id": "55555555-5555-5555-5555-555555555555",
        "hostname": "web-01",
        "check_type": "service.state",
        "selector": "Spooler",
        "severity": "warning",
        "state": "active",
        "observed_value": "stopped",
        "threshold": "running",
        "comparator": "ne",
        "opened_at": "2026-08-02T09:14:00+00:00",
        "cleared_at": None,
        "cleared_reason": None,
        "flapping": True,
        "ticket_id": None,
        "ticket_number": None,
        "remediation_job_id": None,
        "remediation_action_key": None,
        "queue_id": "33333333-3333-3333-3333-333333333333",
        "assignee_id": None,
        "requester_id": None,
    },
    # Adds `script_name`/`script_version` to §9.1's contract -- spec §13 and
    # §2.10 require the approval prompt to identify the exact pinned body
    # ("nobody approves a body they cannot identify"). The BODY itself is never
    # on this wire: it is arbitrary code, which is why §8.4 makes `script.run`
    # destructive by default in the first place.
    "health.remediation_requires_approval": {
        "alert_id": "44444444-4444-4444-4444-444444444444",
        "device_id": "55555555-5555-5555-5555-555555555555",
        "hostname": "web-01",
        "check_type": "service.state",
        "selector": "Spooler",
        "severity": "warning",
        "state": "active",
        "observed_value": "stopped",
        "threshold": "running",
        "comparator": "ne",
        "opened_at": "2026-08-02T09:14:00+00:00",
        "cleared_at": None,
        "cleared_reason": None,
        "flapping": False,
        "ticket_id": None,
        "ticket_number": None,
        "remediation_job_id": None,
        "remediation_action_key": "restart-spooler",
        "script_name": "Restart Print Spooler",
        "script_version": 3,
        "queue_id": "33333333-3333-3333-3333-333333333333",
        "assignee_id": None,
        "requester_id": None,
    },
    # Published by `app.modules.ipam` (docs/modules/ipam/spec.md §10). BOTH
    # SHAPES DIFFER FROM EVERY TICKET AND HEALTH EVENT ABOVE, and the
    # differences are the spec's, not an omission here:
    #
    #   * NO `queue_id`, NO `assignee_id`, NO `requester_id`. §10 states the
    #     limitation plainly: `assignee`/`requester`/`queue_members` all resolve
    #     from a TICKET-shaped payload, an IPAM event carries no ticket, so in
    #     v1 these are routable only through `channel_default` rules. Adding a
    #     null `queue_id` here to look like the others would misrepresent that
    #     -- `routing.resolve_recipients` narrows to `queue_id IS NULL` rules on
    #     a queue-less payload either way.
    #   * NO `hostname`. A drift finding is about an ADDRESS, and the machine at
    #     that address is exactly what is in dispute; `device_id` is the
    #     DOCUMENTED claimant, which is why it is named that way in the mail.
    #   * `sample` is bounded to ten by the publisher (§6.4) and bounded again
    #     by the renderer -- it comes from a device just caught reporting
    #     outside its authorization.
    "ipam.drift_detected": {
        "drift_id": "77777777-7777-7777-7777-777777777777",
        "drift_class": "documented_absent",
        "severity": "medium",
        "confidence": "high",
        "address": "10.4.7.42",
        "subnet_id": "88888888-8888-8888-8888-888888888888",
        "subnet_prefix": "10.4.7.0/24",
        "subnet_name": "Manila office",
        "device_id": "55555555-5555-5555-5555-555555555555",
    },
    "ipam.sweep_rejected": {
        "device_id": "55555555-5555-5555-5555-555555555555",
        "subnet_id": "88888888-8888-8888-8888-888888888888",
        "sweep_run_id": "99999999-9999-9999-9999-999999999999",
        "reason": "address outside signed scope",
        "offending_count": 2,
        "sample": ["192.0.2.7", "192.0.2.8"],
    },
    # Published by `app.core.health.notify`. No ticket, no requester: routable
    # through `channel_default` only, like IPAM.
    "platform.probe_unhealthy": {
        "probe": "vault",
        "status": "down",
        "reason": "sealed",
        "since": "2026-09-23T03:00:00+00:00",
        "consecutive_samples": 3,
    },
    "platform.probe_recovered": {
        "probe": "vault",
        "status": "ok",
        "reason": None,
        "since": "2026-09-23T03:20:00+00:00",
        "unhealthy_since": "2026-09-23T03:00:00+00:00",
    },
    # Published by `app.modules.subscriptions.alerts`. No ticket, no requester.
    "subscription.notice_deadline_approaching": {
        "subscription_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "service_name": "Atlassian Cloud",
        "plan": "Standard",
        "renews_at": "2026-11-01",
        "notice_deadline": "2026-10-02",
        "days_to_notice": 9,
        "days_to_renewal": 39,
        "auto_renew": True,
        "action_required": False,
    },
    "subscription.renewal_approaching": {
        "subscription_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "service_name": "Atlassian Cloud",
        "plan": "Standard",
        "renews_at": "2026-10-10",
        "notice_deadline": "2026-10-10",
        "days_to_notice": 17,
        "days_to_renewal": 17,
        "auto_renew": False,
        "action_required": False,
    },
    # Published by `app.services.enrollment.enroll_via_link` (install-link
    # spec §1.5). No ticket, no requester: routable through
    # `channel_default` rules only.
    "device.joined_via_link": {
        "device_id": "55555555-5555-5555-5555-555555555555",
        "hostname": "lab-pc-07",
        "os_family": "windows",
        "source_address": "10.50.0.9",
        "joined_at": "2026-09-28T09:15:00+00:00",
    },
    # Published by `app.services.device_review` when a rejection ends with the
    # agent possibly still on the computer. No ticket, no requester.
    "device.removal_incomplete": {
        "device_id": "55555555-5555-5555-5555-555555555555",
        "hostname": "lab-pc-07",
        "rejection_outcome": "timeout",
        "link_joined_from": "10.50.0.9",
        "rejected_at": "2026-09-28T09:15:00+00:00",
    },
    "push.run.completed": {
        "run_id": "66666666-6666-6666-6666-666666666666",
        "prefix": "10.20.4.0/24",
        "target_count": 249,
        "installed": 240,
        "failed": 5,
        "refused": 4,
        "cancelled": 0,
        "finished_at": "2026-09-30T14:02:00+00:00",
    },
    "push.run.stopped": {
        "run_id": "77777777-7777-7777-7777-777777777777",
        "prefix": "10.20.4.0/24",
        "reason": "auth_failures",
        "auth_failures": 3,
        "credential_name": "Domain push account",
        "target_count": 249,
        "installed": 0,
        "waiting": 246,
        "stopped_at": "2026-09-30T14:02:00+00:00",
    },
}


@pytest.mark.parametrize("event_type", sorted(_EVENT_TYPE_SAMPLE_PAYLOADS))
async def test_webhook_wire_format_is_pinned_per_event_type(
    api_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, event_type: str
) -> None:
    payload = _EVENT_TYPE_SAMPLE_PAYLOADS[event_type]
    org_id, user_id = await _org_with_user(api_env)
    vault = _vault(tmp_path)
    captured: list[httpx.Request] = []
    monkeypatch.setattr("app.core.egress._resolve_all", lambda h, p: ["93.184.216.34"])
    monkeypatch.setattr(
        "app.core.egress._build_transport", lambda ip: _mock_transport(200, captured=captured)
    )

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await _webhook_channel(session, org_id, user_id, vault)
        event = await _event(session, org_id, event_type=event_type)
        event.payload = payload
        session.add(event)
        await session.flush()
        delivery = await _delivery(
            session, org_id, channel_id=channel.id, event_id=event.id, recipient="hooks.example.com"
        )
        await attempt_delivery(session, delivery=delivery, vault=vault)
        event_id = str(event.id)
        subject_id = str(event.subject_id)
        occurred_at = event.occurred_at.isoformat()

    assert len(captured) == 1
    body = json.loads(captured[0].content)
    assert set(body.keys()) == {
        "event_id",
        "event_type",
        "subject_type",
        "subject_id",
        "occurred_at",
        "payload",
    }
    assert body["event_id"] == event_id
    assert body["event_type"] == event_type
    assert body["subject_type"] == "ticket"
    assert body["subject_id"] == subject_id
    assert body["occurred_at"] == occurred_at
    assert body["payload"] == payload


def test_every_registered_event_type_has_a_pinned_wire_format_sample() -> None:
    """Structural guard on the parametrized test above: every event type
    `app.core.events.EVENT_TYPES` registers must have a sample payload here,
    so a future addition to the registry cannot silently skip this coverage
    the way `ticket.resolved` skipped having a publisher at all (C2)."""
    from app.core.events import EVENT_TYPES

    assert set(_EVENT_TYPE_SAMPLE_PAYLOADS) == EVENT_TYPES


# ---------------------------------------------------------------------------
# attempt_delivery -- SMTP
# ---------------------------------------------------------------------------


class _RecordingSMTP:
    """Stub for `smtplib.SMTP` -- records what it was called with, opens no
    socket. `instances` lets a test assert on multiple constructions."""

    instances: list["_RecordingSMTP"] = []
    raise_on_init: Exception | None = None
    raise_on_send: Exception | None = None

    def __init__(self, host: str, port: int, timeout: float | None = None) -> None:
        if self.__class__.raise_on_init is not None:
            raise self.__class__.raise_on_init
        self.host = host
        self.port = port
        self.timeout = timeout
        self.starttls_called = False
        self.login_args: tuple[str, str] | None = None
        self.sent: dict[str, Any] | None = None
        self.__class__.instances.append(self)

    def __enter__(self) -> "_RecordingSMTP":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def starttls(self, context: Any = None) -> None:
        self.starttls_called = True

    def login(self, username: str, password: str) -> None:
        self.login_args = (username, password)

    def send_message(self, message: Any) -> None:
        if self.__class__.raise_on_send is not None:
            raise self.__class__.raise_on_send
        self.sent = {
            "to": message["To"],
            "from": message["From"],
            "subject": message["Subject"],
            "body": message.get_content(),
            # The whole `EmailMessage`, so a caller that needs a header this
            # dict does not name (e.g. `tests/test_email_intake_reply_to.py`
            # asserting on `Reply-To`) can read it off the real message
            # rather than growing a second SMTP stub.
            "message": message,
        }


@pytest.fixture(autouse=True)
def _reset_smtp_stub() -> None:
    _RecordingSMTP.instances = []
    _RecordingSMTP.raise_on_init = None
    _RecordingSMTP.raise_on_send = None


def _stub_smtp(monkeypatch: pytest.MonkeyPatch) -> type[_RecordingSMTP]:
    monkeypatch.setattr("smtplib.SMTP", _RecordingSMTP)
    return _RecordingSMTP


async def test_email_success_uses_the_vault_credential_and_never_logs_it(
    api_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    org_id, user_id = await _org_with_user(api_env)
    vault = _vault(tmp_path)
    stub = _stub_smtp(monkeypatch)
    password = "hunter2-super-secret"

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await _email_channel(
            session,
            org_id,
            user_id,
            vault,
            secret=json.dumps({"username": "svc", "password": password}),
        )
        event = await _event(session, org_id, event_type="ticket.resolved")
        delivery = await _delivery(
            session,
            org_id,
            channel_id=channel.id,
            event_id=event.id,
            recipient="someone@example.com",
        )

        with caplog.at_level("DEBUG"):
            await attempt_delivery(session, delivery=delivery, vault=vault)

        assert delivery.status == "sent"
        assert delivery.attempts == 1
        assert delivery.sent_at is not None
        assert delivery.last_error is None

        checkouts = await _audit_events(session, org_id, "notification.credential_used")
        assert len(checkouts) == 1
        assert password not in json.dumps(checkouts[0].meta)

    assert password not in caplog.text
    assert password not in str(delivery.last_error)
    assert len(stub.instances) == 1
    sent_instance = stub.instances[0]
    assert sent_instance.starttls_called is True
    assert sent_instance.login_args == ("svc", password)
    assert sent_instance.sent is not None
    assert sent_instance.sent["to"] == "someone@example.com"


# ---------------------------------------------------------------------------
# Whole-branch review finding M6: `_send_email`'s message body used to
# ignore `event.payload` entirely -- a requester's resolution email read
# `Event: ticket.status_changed / Subject: ticket <uuid> / Occurred: ...`,
# with no ticket number, no ticket subject line, no human sentence, despite
# every publisher putting `ticket_number`/`subject` in the payload for
# exactly this purpose (spec §0.4's headline value). These tests drive the
# REAL rendered subject/body through `attempt_delivery` -> `_send_email` ->
# the stubbed `smtplib.SMTP.send_message`, and assert the ticket number and
# subject actually appear in what would be sent -- the two event types the
# task names explicitly.
# ---------------------------------------------------------------------------


async def test_resolution_email_reads_like_a_human_wrote_it(
    api_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    org_id, user_id = await _org_with_user(api_env)
    vault = _vault(tmp_path)
    stub = _stub_smtp(monkeypatch)

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await _email_channel(session, org_id, user_id, vault)
        event = await _event(session, org_id, event_type="ticket.resolved")
        event.payload = {
            "ticket_number": "INC-4471",
            "subject": "VPN client will not connect",
            "queue_id": None,
            "assignee_id": None,
            "requester_id": None,
        }
        session.add(event)
        await session.flush()
        delivery = await _delivery(
            session,
            org_id,
            channel_id=channel.id,
            event_id=event.id,
            recipient="requester@example.com",
        )
        await attempt_delivery(session, delivery=delivery, vault=vault)
        assert delivery.status == "sent"

    sent = stub.instances[0].sent
    assert sent is not None
    # The two things the task names explicitly: the ticket number and the
    # subject must actually appear, not just "some text".
    assert "INC-4471" in sent["subject"] or "INC-4471" in sent["body"]
    assert "VPN client will not connect" in sent["subject"] or (
        "VPN client will not connect" in sent["body"]
    )
    # No longer the old unreadable machine notice -- the raw event type
    # string is not what the human-facing line leads with.
    assert sent["subject"] != "[Toeshee] ticket.resolved"
    assert "resolved" in sent["subject"].lower()


def test_resolution_email_does_not_promise_reply_to_reopen() -> None:
    """Portal spec §0.4 -- inbound email intake is sub-project 4 and does not
    exist. Telling a requester to reply to reopen their ticket is an
    instruction the system cannot honour, since a reply just vanishes. The
    copy now points at the portal instead (whose routes landed in Task 5),
    with no hard-coded URL/host -- referred to by name only.

    Pure unit test of `_render_ticket_resolved` -- no session, no SMTP
    stub -- same pattern as the module's other pure-function tests."""
    from app.modules.notification.delivery import _render_ticket_resolved

    event = DomainEvent(
        org_id=uuid7(),
        event_type="ticket.resolved",
        subject_type="ticket",
        subject_id=uuid7(),
        payload={"ticket_number": "INC-1", "subject": "VPN will not connect"},
        occurred_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
    )

    _subject, body = _render_ticket_resolved(event)

    assert "reply to this ticket" not in body.lower()
    assert "portal" in body.lower(), "point the requester somewhere that works"


async def test_assignment_email_reads_like_a_human_wrote_it(
    api_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    org_id, user_id = await _org_with_user(api_env)
    vault = _vault(tmp_path)
    stub = _stub_smtp(monkeypatch)

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await _email_channel(session, org_id, user_id, vault)
        event = await _event(session, org_id, event_type="ticket.assigned")
        event.payload = {
            "ticket_number": "REQ-1029",
            "subject": "New laptop for finance hire",
            "queue_id": None,
            "assignee_id": None,
            "requester_id": None,
        }
        session.add(event)
        await session.flush()
        delivery = await _delivery(
            session,
            org_id,
            channel_id=channel.id,
            event_id=event.id,
            recipient="tech@example.com",
        )
        await attempt_delivery(session, delivery=delivery, vault=vault)
        assert delivery.status == "sent"

    sent = stub.instances[0].sent
    assert sent is not None
    assert "REQ-1029" in sent["subject"] or "REQ-1029" in sent["body"]
    assert "New laptop for finance hire" in sent["subject"] or (
        "New laptop for finance hire" in sent["body"]
    )
    assert sent["subject"] != "[Toeshee] ticket.assigned"
    assert "assigned" in sent["subject"].lower()


#: What an agent may report as its hostname: CR/LF (header injection), and the
#: rest of C0 and C1 (ESC, NUL, TAB, NEL, CSI, DEL).
_HOSTILE_HOSTNAME = "lab-pc-07\r\nBcc: x@evil.test\x00\x1b[31m\t\x7f\x85\x9b-end"


def _control_free(text: str) -> bool:
    return not any(ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F for ch in text)


def test_the_removal_incomplete_email_strips_control_characters_from_the_hostname() -> None:
    """The same hostname reaches the "rejected computer still has the agent"
    email, which landed after M3 was fixed. A CR/LF there made its subject an
    invalid header too, so the one email saying an agent is still installed
    was the one never sent."""
    from app.modules.notification.delivery import _render_device_removal_incomplete

    event = DomainEvent(
        org_id=uuid7(),
        event_type="device.removal_incomplete",
        subject_type="device",
        subject_id=uuid7(),
        payload={
            "device_id": "55555555-5555-5555-5555-555555555555",
            "hostname": _HOSTILE_HOSTNAME,
            "rejection_outcome": "agent_did_not_confirm",
            "link_joined_from": "10.50.0.9",
            "rejected_at": "2026-09-28T09:15:00+00:00",
        },
        occurred_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
    )
    subject, body = _render_device_removal_incomplete(event)
    assert _control_free(subject), repr(subject)
    assert subject == (
        "[Toeshee] Rejected computer still has the agent: lab-pc-07Bcc: x@evil.test[31m-end"
    )
    computer = next(line for line in body.split("\n") if line.startswith("Computer: "))
    assert computer == "Computer: lab-pc-07Bcc: x@evil.test[31m-end"
    assert all(_control_free(line) for line in body.split("\n")), repr(body)


def test_the_joined_via_link_email_strips_control_characters_from_the_hostname() -> None:
    """Final review M3. The hostname is whatever the enrolling agent said; a
    CR/LF in it made the email subject an invalid header, so the "waiting
    for review" email was never sent. Every C0/C1 control character is
    stripped from subject and body alike."""
    from app.modules.notification.delivery import _render_device_joined_via_link

    event = DomainEvent(
        org_id=uuid7(),
        event_type="device.joined_via_link",
        subject_type="device",
        subject_id=uuid7(),
        payload={
            "device_id": "55555555-5555-5555-5555-555555555555",
            "hostname": _HOSTILE_HOSTNAME,
            "os_family": "windows",
            "source_address": "10.50.0.9",
            "joined_at": "2026-09-28T09:15:00+00:00",
        },
        occurred_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
    )
    subject, body = _render_device_joined_via_link(event)
    assert _control_free(subject), repr(subject)
    assert subject == "[Toeshee] New computer waiting for review: lab-pc-07Bcc: x@evil.test[31m-end"
    computer = next(line for line in body.split("\n") if line.startswith("Computer: "))
    assert computer == "Computer: lab-pc-07Bcc: x@evil.test[31m-end"
    assert all(_control_free(line) for line in body.split("\n")), repr(body)


async def test_the_joined_via_link_email_is_sent_for_a_crlf_hostname(
    api_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    org_id, user_id = await _org_with_user(api_env)
    vault = _vault(tmp_path)
    stub = _stub_smtp(monkeypatch)

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await _email_channel(session, org_id, user_id, vault)
        event = await _event(session, org_id, event_type="device.joined_via_link")
        event.payload = {
            "device_id": "55555555-5555-5555-5555-555555555555",
            "hostname": "lab-pc-07\r\nBcc: x@evil.test",
            "os_family": "windows",
            "source_address": "10.50.0.9",
            "joined_at": "2026-09-28T09:15:00+00:00",
        }
        session.add(event)
        await session.flush()
        delivery = await _delivery(
            session, org_id, channel_id=channel.id, event_id=event.id, recipient="tech@example.com"
        )
        await attempt_delivery(session, delivery=delivery, vault=vault)
        assert delivery.status == "sent", delivery.last_error

    sent = stub.instances[0].sent
    assert sent is not None
    assert "lab-pc-07Bcc: x@evil.test" in sent["subject"]
    assert "\r" not in sent["subject"] and "\n" not in sent["subject"]


async def test_internal_note_email_never_carries_any_note_text(
    api_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The renderer for `ticket.internal_note_added` must render sensibly
    without ever inventing note text -- the payload never carries it (spec
    §6.4 mechanism 1), so this asserts the renderer does not, say, blow up
    reaching for a `body` key that is never there for this event type."""
    org_id, user_id = await _org_with_user(api_env)
    vault = _vault(tmp_path)
    stub = _stub_smtp(monkeypatch)

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await _email_channel(session, org_id, user_id, vault)
        event = await _event(session, org_id, event_type="ticket.internal_note_added")
        event.payload = {
            "ticket_number": "INC-9",
            "queue_id": None,
            "assignee_id": None,
            "requester_id": None,
            "author_id": None,
        }
        session.add(event)
        await session.flush()
        delivery = await _delivery(
            session, org_id, channel_id=channel.id, event_id=event.id, recipient="tech@example.com"
        )
        await attempt_delivery(session, delivery=delivery, vault=vault)
        assert delivery.status == "sent"

    sent = stub.instances[0].sent
    assert sent is not None
    assert "INC-9" in sent["subject"] or "INC-9" in sent["body"]


def test_every_registered_event_type_has_an_email_renderer() -> None:
    """Structural guard mirroring `test_every_registered_event_type_has_a_
    pinned_wire_format_sample` above: every event type `app.core.events.
    EVENT_TYPES` registers must have an explicit email renderer, so a future
    addition to the registry cannot silently fall back to `_render_generic`'s
    machine notice the way EVERY event type used to (M6) before this fix."""
    from app.core.events import EVENT_TYPES
    from app.modules.notification.delivery import _EMAIL_RENDERERS

    assert set(_EMAIL_RENDERERS) == EVENT_TYPES


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        ("timeout", "did not confirm it removed itself"),
        ("agent_did_not_confirm", "did not confirm it removed itself"),
        ("agent_cannot_uninstall", "too old to remove itself"),
    ],
)
def test_a_removal_that_did_not_happen_tells_someone_to_do_it_by_hand(
    outcome: str, reason: str
) -> None:
    from app.modules.notification.delivery import _render_email

    event = DomainEvent(
        org_id=uuid7(),
        event_type="device.removal_incomplete",
        subject_type="device",
        subject_id=uuid7(),
        payload={
            **_EVENT_TYPE_SAMPLE_PAYLOADS["device.removal_incomplete"],
            "rejection_outcome": outcome,
        },
        actor_user_id=None,
        occurred_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
    )
    subject, body = _render_email(event)
    assert "lab-pc-07" in subject
    assert reason in body
    assert "Uninstall it on the computer by hand" in body
    # The wire value is for rules, never for the person reading the mail.
    assert outcome not in body


async def test_email_with_no_credential_sends_without_login(
    api_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    org_id, user_id = await _org_with_user(api_env)
    vault = _vault(tmp_path)
    stub = _stub_smtp(monkeypatch)

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await _email_channel(session, org_id, user_id, vault, secret=None)
        event = await _event(session, org_id)
        delivery = await _delivery(
            session,
            org_id,
            channel_id=channel.id,
            event_id=event.id,
            recipient="someone@example.com",
        )
        await attempt_delivery(session, delivery=delivery, vault=vault)
        assert delivery.status == "sent"

        no_checkouts = await _audit_events(session, org_id, "notification.credential_used")
        assert no_checkouts == []

    assert stub.instances[0].login_args is None


async def test_smtp_permanent_reject_is_terminal(
    api_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    org_id, user_id = await _org_with_user(api_env)
    vault = _vault(tmp_path)
    stub = _stub_smtp(monkeypatch)
    stub.raise_on_send = smtplib.SMTPResponseException(550, b"mailbox unavailable")

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await _email_channel(session, org_id, user_id, vault)
        event = await _event(session, org_id)
        delivery = await _delivery(
            session,
            org_id,
            channel_id=channel.id,
            event_id=event.id,
            recipient="bounces@example.com",
        )
        await attempt_delivery(session, delivery=delivery, vault=vault)
        assert delivery.status == "dead"
        assert delivery.attempts == 1
        assert delivery.last_status_code == 550
        assert delivery.next_attempt_at is None
        # The raw server response bytes never land verbatim in last_error.
        assert "mailbox unavailable" not in str(delivery.last_error)


async def test_smtp_transient_reject_is_retryable(
    api_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    org_id, user_id = await _org_with_user(api_env)
    vault = _vault(tmp_path)
    stub = _stub_smtp(monkeypatch)
    stub.raise_on_send = smtplib.SMTPResponseException(421, b"try again later")

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await _email_channel(session, org_id, user_id, vault)
        event = await _event(session, org_id)
        delivery = await _delivery(
            session,
            org_id,
            channel_id=channel.id,
            event_id=event.id,
            recipient="someone@example.com",
        )
        await attempt_delivery(session, delivery=delivery, vault=vault)
        assert delivery.status == "failed"
        assert delivery.attempts == 1
        assert delivery.last_status_code == 421
        assert delivery.next_attempt_at is not None


async def test_smtp_recipients_refused_is_terminal(
    api_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    org_id, user_id = await _org_with_user(api_env)
    vault = _vault(tmp_path)
    stub = _stub_smtp(monkeypatch)
    stub.raise_on_send = smtplib.SMTPRecipientsRefused({"bad@example.com": (550, b"no such user")})

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await _email_channel(session, org_id, user_id, vault)
        event = await _event(session, org_id)
        delivery = await _delivery(
            session, org_id, channel_id=channel.id, event_id=event.id, recipient="bad@example.com"
        )
        await attempt_delivery(session, delivery=delivery, vault=vault)
        assert delivery.status == "dead"
        assert delivery.next_attempt_at is None


async def test_smtp_connection_error_is_retryable(
    api_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    org_id, user_id = await _org_with_user(api_env)
    vault = _vault(tmp_path)
    stub = _stub_smtp(monkeypatch)
    stub.raise_on_init = ConnectionRefusedError("connection refused")

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await _email_channel(session, org_id, user_id, vault)
        event = await _event(session, org_id)
        delivery = await _delivery(
            session,
            org_id,
            channel_id=channel.id,
            event_id=event.id,
            recipient="someone@example.com",
        )
        await attempt_delivery(session, delivery=delivery, vault=vault)
        assert delivery.status == "failed"
        assert delivery.attempts == 1
        assert delivery.next_attempt_at is not None


async def test_smtp_server_disconnected_is_retryable(
    api_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    org_id, user_id = await _org_with_user(api_env)
    vault = _vault(tmp_path)
    stub = _stub_smtp(monkeypatch)
    stub.raise_on_send = smtplib.SMTPServerDisconnected("connection unexpectedly closed")

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await _email_channel(session, org_id, user_id, vault)
        event = await _event(session, org_id)
        delivery = await _delivery(
            session,
            org_id,
            channel_id=channel.id,
            event_id=event.id,
            recipient="someone@example.com",
        )
        await attempt_delivery(session, delivery=delivery, vault=vault)
        assert delivery.status == "failed"
        assert delivery.next_attempt_at is not None


async def test_smtp_is_not_egress_guarded_a_private_relay_is_reachable(
    api_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec/plan's decided asymmetry: an on-prem mail relay on the customer's
    own LAN (a private address) must be reachable over SMTP even though the
    identical address would be refused for a webhook. Proven here by NEVER
    monkeypatching anything in `app.core.egress` for this test at all -- if
    the SMTP path secretly routed through the egress guard, this would hang
    or raise `EgressBlocked`/`dns_failure` instead of calling the (stubbed)
    `smtplib.SMTP`."""
    org_id, user_id = await _org_with_user(api_env)
    vault = _vault(tmp_path)
    _stub_smtp(monkeypatch)

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await create_channel(
            session,
            org_id=org_id,
            actor_id=user_id,
            data=ChannelCreate(
                kind="email",
                name="lan-relay",
                config={
                    "host": "10.0.0.25",
                    "port": 25,
                    "from_address": "noreply@toeshee.test",
                    "use_tls": False,
                },
            ),
            secret=None,
            vault=vault,
        )
        event = await _event(session, org_id)
        delivery = await _delivery(
            session,
            org_id,
            channel_id=channel.id,
            event_id=event.id,
            recipient="someone@example.com",
        )
        await attempt_delivery(session, delivery=delivery, vault=vault)
        assert delivery.status == "sent"


def test_a_finished_push_run_says_what_happened_in_numbers() -> None:
    from app.modules.notification.delivery import _render_email

    event = DomainEvent(
        org_id=uuid7(),
        event_type="push.run.completed",
        subject_type="push_run",
        subject_id=uuid7(),
        payload=_EVENT_TYPE_SAMPLE_PAYLOADS["push.run.completed"],
    )
    subject, body = _render_email(event)
    assert subject == "[Toeshee] Push install finished: 10.20.4.0/24"
    assert "Installed: 240" in body and "Failed: 5" in body
    assert "Refused before anything was sent: 4" in body


def test_a_stopped_push_run_says_why_and_what_to_do() -> None:
    from app.modules.notification.delivery import _render_email

    event = DomainEvent(
        org_id=uuid7(),
        event_type="push.run.stopped",
        subject_type="push_run",
        subject_id=uuid7(),
        payload=_EVENT_TYPE_SAMPLE_PAYLOADS["push.run.stopped"],
    )
    subject, body = _render_email(event)
    assert subject == "[Toeshee] Push install stopped: 10.20.4.0/24"
    assert "refused by 3 machines" in body and "Domain push account" in body
    assert "Machines still waiting: 246" in body and "Resume" in body
