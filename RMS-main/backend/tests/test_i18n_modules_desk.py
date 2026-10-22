"""Spanish server messages for email_intake, service_desk, sla, notification,
portal and people.

One representative failing route per module, called twice: with
`Accept-Language: es` the `detail` is the catalogue's Spanish, and with
`Accept-Language: en` the English is exactly what it was before the sweep.
The full catalogue is checked by tests/test_i18n_catalog.py; this proves a real
request reaches `tr()` with the request's language.
"""

from typing import Any
from uuid import uuid4

import pytest

from app.core.i18n import CATALOG_ES, use_locale
from tests.test_email_intake_intake import email_env  # noqa: F401  (a fixture)


async def _detail(
    env: Any,
    method: str,
    path: str,
    headers: dict[str, str],
    lang: str,
    json_body: Any = None,
) -> Any:
    response = await env.client.request(
        method, path, headers={**headers, "Accept-Language": lang}, json=json_body
    )
    assert response.headers["Content-Language"] == lang
    return response.status_code, response.json()["detail"]


async def _both_languages(
    env: Any,
    method: str,
    path: str,
    headers: dict[str, str],
    *,
    status: int,
    english: str,
    spanish: str | None = None,
    json_body: Any = None,
) -> None:
    """`english` is the exact English detail; `spanish` defaults to its catalogue
    entry (pass the formatted Spanish when the template has fields)."""
    assert await _detail(env, method, path, headers, "en", json_body) == (status, english)
    expected = spanish if spanish is not None else CATALOG_ES[english]
    assert await _detail(env, method, path, headers, "es", json_body) == (status, expected)
    assert expected != english


@pytest.fixture()
async def admin(api_env: Any) -> tuple[Any, dict[str, str]]:
    org = await api_env.create_org("i18n-desk")
    sub = await api_env.create_user(org, "org-admin")
    return api_env, await api_env.headers(org, sub)


async def test_email_intake_a_mailbox_that_does_not_exist(
    admin: tuple[Any, dict[str, str]],
) -> None:
    """email_intake/mailboxes.py: MailboxNotFoundError, mapped to 404 by email_intake/api.py."""
    env, headers = admin
    mailbox_id = uuid4()
    await _both_languages(
        env,
        "GET",
        f"/api/v1/email-intake/mailboxes/{mailbox_id}",
        headers,
        status=404,
        english=f"mailbox {mailbox_id} not found",
        spanish=f"no se encontró el buzón {mailbox_id}",
    )


def test_email_intake_the_test_result_is_worded_for_the_reader_and_stored_in_english() -> None:
    """poller.py: `_describe` answers the person who ran the test; `_describe_stored`
    is what `last_error` and the audit chain keep."""
    from app.modules.email_intake.poller import MailboxNotConnected, _describe, _describe_stored
    from app.modules.email_intake.tokens import TokenRevoked

    for exc in (TokenRevoked("consent"), TokenRevoked("application"), MailboxNotConnected()):
        english = _describe("connect", exc)
        with use_locale("es"):
            assert _describe("connect", exc) != english
            assert _describe_stored("connect", exc) == english


async def test_service_desk_a_ticket_that_does_not_exist(
    admin: tuple[Any, dict[str, str]],
) -> None:
    """service_desk/tickets.py: TicketNotFoundError, mapped to 404 by service_desk/api.py."""
    env, headers = admin
    ticket_id = uuid4()
    await _both_languages(
        env,
        "GET",
        f"/api/v1/service-desk/tickets/{ticket_id}",
        headers,
        status=404,
        english=f"ticket {ticket_id} not found",
        spanish=f"no se encontró el ticket {ticket_id}",
    )


async def test_service_desk_scope_mine_cannot_name_another_assignee(
    admin: tuple[Any, dict[str, str]],
) -> None:
    """service_desk/api.py: a plain HTTPException(400) in the route itself."""
    env, headers = admin
    await _both_languages(
        env,
        "GET",
        f"/api/v1/service-desk/tickets?scope=mine&assigned_to={uuid4()}",
        headers,
        status=400,
        english="scope=mine cannot be combined with another assignee",
    )


def test_service_desk_the_noun_in_a_refusal_agrees_with_its_sentence() -> None:
    """threads.py: `kind` is translated at the call site, so the Spanish noun
    ("comentarios") reads inside the Spanish sentence and the English is unchanged."""
    from app.core.i18n import tr

    template = "ticket {ticket_id} is {status} and accepts no new {kind}"
    english = tr(template, ticket_id="T-1", status="'closed'", kind=tr("comments"))
    assert english == "ticket T-1 is 'closed' and accepts no new comments"
    with use_locale("es"):
        spanish = tr(template, ticket_id="T-1", status="'closed'", kind=tr("attachments"))
    assert spanish == "el ticket T-1 está 'closed' y no acepta archivos adjuntos nuevos"


async def test_sla_a_policy_that_does_not_exist(admin: tuple[Any, dict[str, str]]) -> None:
    """sla/policies.py: SlaNotFoundError, mapped to 404 by sla/api.py."""
    env, headers = admin
    policy_id = uuid4()
    await _both_languages(
        env,
        "GET",
        f"/api/v1/sla/policies/{policy_id}",
        headers,
        status=404,
        english=f"policy {policy_id} not found",
        spanish=f"no se encontró la política {policy_id}",
    )


def test_sla_the_policy_name_the_console_prints_is_worded_for_the_reader() -> None:
    """sla/views.py `describe_policy`: `policy_name` is shown as it arrives."""
    from app.modules.sla.views import describe_policy

    assert describe_policy(queue_name=None, priority=None) == "Every ticket"
    assert describe_policy(queue_name="Helpdesk", priority="urgent") == (
        "Urgent tickets in the Helpdesk queue"
    )
    assert describe_policy(queue_name=None, priority="high") == "High priority tickets in any queue"
    with use_locale("es"):
        assert describe_policy(queue_name=None, priority=None) == "Todos los tickets"
        assert describe_policy(queue_name="Helpdesk", priority="urgent") == (
            "Tickets urgentes en la cola Helpdesk"
        )
        assert describe_policy(queue_name=None, priority="high") == (
            "Tickets de prioridad alta en cualquier cola"
        )


async def test_notification_a_channel_that_does_not_exist(
    admin: tuple[Any, dict[str, str]],
) -> None:
    """notification/channels.py: NotificationNotFoundError, mapped to 404 by notification/api.py."""
    env, headers = admin
    channel_id = uuid4()
    await _both_languages(
        env,
        "GET",
        f"/api/v1/notifications/channels/{channel_id}",
        headers,
        status=404,
        english=f"notification channel {channel_id} not found",
        spanish=f"no se encontró el canal de notificación {channel_id}",
    )


# One full payload per event type: every key a renderer reads is present, so the
# Spanish cannot hide behind a fallback.
_EMAIL_PAYLOADS: dict[str, dict[str, Any]] = {
    "ticket.assigned": {"ticket_number": "T-1", "subject": "Printer"},
    "ticket.status_changed": {
        "ticket_number": "T-1",
        "subject": "Printer",
        "from_status": "open",
        "to_status": "resolved",
    },
    "ticket.comment_added": {"ticket_number": "T-1", "body": "Hello"},
    "ticket.internal_note_added": {"ticket_number": "T-1"},
    "ticket.approval_requested": {"ticket_number": "T-1", "subject": "Laptop"},
    "ticket.approval_decided": {"ticket_number": "T-1", "decision": "approved"},
    "ticket.resolved": {"ticket_number": "T-1", "subject": "Printer"},
    "ticket.sla_breached": {
        "ticket_number": "T-1",
        "subject": "Printer",
        "target": "response",
        "due_at": "2026-01-01T00:00:00+00:00",
    },
    "health.alert_raised": {
        "check_type": "disk_free",
        "hostname": "pc-01",
        "severity": "critical",
        "observed_value": "3",
        "comparator": "<",
        "threshold": "10",
        "opened_at": "2026-01-01T00:00:00+00:00",
    },
    "health.alert_cleared": {"hostname": "pc-01", "severity": "warning", "cleared_at": "x"},
    "health.alert_escalated": {"hostname": "pc-01", "severity": "critical"},
    "health.remediation_requires_approval": {
        "hostname": "pc-01",
        "remediation_action_key": "restart_service",
        "script_name": "fix",
        "script_version": 2,
    },
    "ipam.drift_detected": {
        "drift_class": "conflict",
        "address": "10.0.0.5",
        "severity": "warning",
        "subnet_prefix": "10.0.0.0/24",
    },
    "ipam.sweep_rejected": {"device_id": "d1", "offending_count": 3, "sample": ["10.9.9.9"]},
    "platform.probe_unhealthy": {"probe": "vault", "status": "down", "reason": "timeout"},
    "platform.probe_recovered": {"probe": "vault"},
    "subscription.notice_deadline_approaching": {
        "service_name": "Office",
        "auto_renew": True,
        "notice_deadline": "2026-02-01",
    },
    "subscription.renewal_approaching": {"service_name": "Office", "renews_at": "2026-03-01"},
    "device.joined_via_link": {"hostname": "pc-02", "os_family": "windows"},
    "device.removal_incomplete": {"hostname": "pc-02", "rejection_outcome": "other"},
    "push.run.completed": {"prefix": "10.0.0.0/24", "target_count": 4, "installed": 3},
    "push.run.stopped": {"prefix": "10.0.0.0/24", "credential_name": "svc", "auth_failures": 3},
}


def _email_event(event_type: str) -> Any:
    from datetime import UTC, datetime

    from app.models import DomainEvent

    return DomainEvent(
        id=uuid4(),
        org_id=uuid4(),
        event_type=event_type,
        subject_type="ticket",
        subject_id=uuid4(),
        payload=_EMAIL_PAYLOADS.get(event_type, {}),
        actor_user_id=None,
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_notification_an_email_is_worded_in_the_recipients_language() -> None:
    """notification/delivery.py: `_render_email` under `use_locale("es")`. Task 18 adds
    the locale switch around it; this is the renderer it will switch."""
    from app.modules.notification.delivery import _render_email

    event = _email_event("ticket.sla_breached")
    english = _render_email(event)
    assert english == (
        "[Toeshee] SLA breached: ticket #T-1 — Printer",
        "The response SLA for ticket #T-1 — Printer was due at 2026-01-01T00:00:00+00:00 "
        "and has been breached.\nSign in to Toeshee to review it.\n",
    )
    with use_locale("es"):
        subject, body = _render_email(event)
    assert subject == "[Toeshee] SLA incumplido: ticket #T-1 — Printer"
    assert body == (
        "El SLA de respuesta del ticket #T-1 — Printer vencía el 2026-01-01T00:00:00+00:00 "
        "y se ha incumplido.\nInicia sesión en Toeshee para ver el detalle.\n"
    )
    # The worker has no request: outside use_locale the email is English again.
    assert _render_email(event) == english


def _template_words(line: str, payload: dict[str, Any]) -> bool:
    """True when `line` still has a word that is not payload data: a template word."""
    import re

    rest = line.replace("[Toeshee]", "")
    for value in sorted(_flatten(payload), key=len, reverse=True):
        rest = rest.replace(value, "")
    return re.search(r"[A-Za-z]{3,}", rest) is not None


def _flatten(payload: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for value in payload.values():
        if isinstance(value, list):
            out.extend(str(v) for v in value)
        elif value is not None and str(value):
            out.append(str(value))
    return out


def test_notification_every_email_renderer_has_a_spanish_subject_and_body() -> None:
    """No English line survives: every line that still has a template word differs
    from the English line at the same place."""
    from app.modules.notification.delivery import _EMAIL_RENDERERS, _render_email

    assert set(_EMAIL_RENDERERS) == set(_EMAIL_PAYLOADS)
    for event_type in _EMAIL_RENDERERS:
        event = _email_event(event_type)
        payload = _EMAIL_PAYLOADS[event_type]
        english_subject, english_body = _render_email(event)
        with use_locale("es"):
            subject, body = _render_email(event)
        assert subject.startswith("[Toeshee] "), event_type
        english_lines = [english_subject, *english_body.split("\n")]
        spanish_lines = [subject, *body.split("\n")]
        assert len(english_lines) == len(spanish_lines), event_type
        for en, es in zip(english_lines, spanish_lines, strict=True):
            if _template_words(en, payload):
                assert es != en, (event_type, en)
            assert "{" not in es and "}" not in es, (event_type, es)


def test_notification_the_spanish_emails_keep_what_a_reader_must_act_on() -> None:
    """The permission that approves a remediation, the user's own words in a
    comment and the screen names the install-link mail points to survive translation."""
    from app.modules.notification.delivery import _render_email

    with use_locale("es"):
        _, remediation = _render_email(_email_event("health.remediation_requires_approval"))
        _, comment = _render_email(_email_event("ticket.comment_added"))
        _, removal = _render_email(_email_event("device.removal_incomplete"))
        _, stopped = _render_email(_email_event("push.run.stopped"))
    assert "`health.remediate`" in remediation
    assert "restart_service" in remediation and "fix (versión 2)" in remediation
    assert comment.endswith("\n\nHello\n")  # a comment is the author's text, as written
    assert "«Ya lo quité»" in removal and "Pendientes de revisión" in removal
    assert "Ejecuciones de instalación remota" in stopped and "Reanudar" in stopped
    assert "svc" in stopped  # the credential's name, never a secret


def test_notification_a_platform_check_is_named_in_the_recipients_language() -> None:
    """api/probes.py `probe_label`: the platform e-mails name the failing check."""
    from app.api.probes import probe_label

    assert probe_label("vault") == "Secret vault"
    assert probe_label("not-a-probe") == "not-a-probe"
    with use_locale("es"):
        assert probe_label("vault") == "Bóveda"
        assert probe_label("signing") == "Firma de tareas"
        assert probe_label("not-a-probe") == "not-a-probe"


async def test_portal_a_ticket_the_requester_cannot_see(api_env: Any) -> None:
    """portal/tickets.py: PortalNotFoundError, a 404 whether the ticket is absent or
    someone else's (no existence oracle). The Spanish is plain, for an end user."""
    org_id = await api_env.create_org("i18n-portal")
    sub = await api_env.create_user(org_id, "requester")
    headers = await api_env.headers(org_id, sub)
    ticket_id = uuid4()
    await _both_languages(
        api_env,
        "GET",
        f"/api/v1/portal/tickets/{ticket_id}",
        headers,
        status=404,
        english=f"ticket {ticket_id} not found",
        spanish=f"no se encontró el ticket {ticket_id}",
    )


async def test_portal_a_person_without_portal_access_is_told_so_plainly(api_env: Any) -> None:
    """portal/principal.py: the 403 for an account that holds no portal permission."""
    org_id = await api_env.create_org("i18n-portal-403")
    sub = await api_env.create_user(org_id)  # no role at all
    headers = await api_env.headers(org_id, sub)
    await _both_languages(
        api_env,
        "GET",
        "/api/v1/portal/tickets",
        headers,
        status=403,
        english="not a portal user",
        spanish="tu cuenta no tiene acceso al portal",
    )


async def test_people_a_person_who_does_not_exist(admin: tuple[Any, dict[str, str]]) -> None:
    """people/service.py: PeopleNotFoundError, mapped to 404 by people/api.py."""
    env, headers = admin
    await _both_languages(
        env,
        "GET",
        f"/api/v1/people/{uuid4()}",
        headers,
        status=404,
        english="person not found",
        spanish="no se encontró a la persona",
    )


def test_people_a_checklist_step_says_what_it_found_in_the_readers_language() -> None:
    """people/checklists.py: the title, help and evidence the console prints beside
    each step are computed per request, with the singular and plural spelled out."""
    from app.modules.people.checklists import (
        STEPS,
        PersonFacts,
        _devices_grouped,
        _no_tickets,
    )

    one = PersonFacts(account_status="active", devices=[(uuid4(), "in_service", None)])
    two = PersonFacts(
        account_status="active",
        devices=[(uuid4(), "in_service", None), (uuid4(), "in_service", None)],
        open_tickets=2,
    )
    assert _devices_grouped(one)[1] == "1 computer is not in a group"
    assert _devices_grouped(two)[1] == "2 computers are not in a group"
    assert _no_tickets(two)[1] == "2 tickets still open"
    with use_locale("es"):
        assert _devices_grouped(one)[1] == "1 equipo no está en un grupo"
        assert _devices_grouped(two)[1] == "2 equipos no están en un grupo"
        assert _no_tickets(two)[1] == "2 tickets siguen abiertos"

    from app.core.i18n import tr_marked

    leaver_titles = [step.title for step in STEPS["leaver"]]
    assert leaver_titles[0] == "Turn off their sign-in"
    with use_locale("es"):
        assert tr_marked(leaver_titles[0]) == "Desactiva su inicio de sesión"


async def test_health_an_alert_ticket_refusal_is_stored_in_english_under_a_spanish_request(
    admin_engine: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """health/actions.py `_execute_open_ticket` stores the ticket core's refusal in
    `last_error`. The ticket core now words its refusals in the request's language,
    and an approved action can run inside a request, so the call is made in English:
    a stored value is never translated."""
    from datetime import UTC, datetime

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.i18n import tr
    from app.models import HealthAlertAction
    from app.modules.health.actions import execute_pending_actions
    from app.modules.service_desk import tickets as ticket_core
    from app.modules.service_desk.errors import TicketValidationError
    from tests.test_health_actions import _armed_with_ticketing, _engine
    from tests.test_health_clear_predicate import _open_one

    async def refuse(*args: Any, **kwargs: Any) -> Any:
        raise TicketValidationError(tr("{field} cannot be null", field="queue_id"))

    monkeypatch.setattr(ticket_core, "create_ticket", refuse)
    fixture, _ = await _armed_with_ticketing(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)
    with use_locale("es"):
        async with AsyncSession(admin_engine, expire_on_commit=False) as session:
            await execute_pending_actions(
                session, fixture.org_id, job_engine=_engine(tmp_path), now=now
            )
            await session.commit()
    async with AsyncSession(admin_engine) as session:
        row = (
            await session.execute(
                select(HealthAlertAction).where(
                    HealthAlertAction.alert_id == alert_id,
                    HealthAlertAction.kind == "open_ticket",
                )
            )
        ).scalar_one()
    assert row.state == "dead"
    assert row.last_error == "queue_id cannot be null"


async def test_email_intake_a_quarantine_reason_is_stored_in_english_under_any_locale(
    email_env: Any,  # noqa: F811
) -> None:
    """email_intake/intake.py: the mailbox is pointed at a category with no queue, so the
    ticket core refuses; the refusal becomes the stored `inbound_message.reason`. The core
    words its refusals in the request's language, so the call is made in English."""
    from tests.test_email_intake_intake import _seed_queueless_category

    email_env.mailbox.default_category_id = await _seed_queueless_category(
        email_env.api_env, email_env.org_id
    )
    with use_locale("es"):
        row = await email_env.dispose(body="printer broken again")
    assert row.disposition == "quarantined"
    assert row.reason.startswith("ticket creation refused: category ")
    assert "has no default_queue_id and no queue_id was supplied" in row.reason


def test_notification_an_approval_decision_agrees_in_gender() -> None:
    """"aprobación" is feminine and "el ticket fue" masculine, so each decision has its
    own subject and body template (not one shared word)."""
    from app.modules.notification.delivery import _render_email

    event = _email_event("ticket.approval_decided")  # approved
    assert _render_email(event)[0] == "[Toeshee] Ticket #T-1: approval approved"
    with use_locale("es"):
        subject, body = _render_email(event)
        assert subject == "[Toeshee] Ticket #T-1: aprobación aprobada"
        assert body == "El ticket #T-1 fue aprobado.\n"
        event.payload = {"ticket_number": "T-1", "decision": "rejected"}
        subject, body = _render_email(event)
        assert subject == "[Toeshee] Ticket #T-1: aprobación rechazada"
        assert body == "El ticket #T-1 fue rechazado.\n"
        event.payload = {"ticket_number": "T-1", "decision": "odd"}
        assert _render_email(event)[0] == "[Toeshee] Ticket #T-1: aprobación odd"


def test_notification_a_closed_health_alert_says_closed_and_why_in_the_consoles_words() -> None:
    """Most cleared reasons mean nothing was fixed, so the mail says "cerrada" (the
    console's word), never "resuelta", and words the reason as the console does with
    the raw code kept beside it."""
    from app.modules.notification.delivery import _render_email

    event = _email_event("health.alert_cleared")
    event.payload = {**_EMAIL_PAYLOADS["health.alert_cleared"], "cleared_reason": "manual"}
    english_subject, english_body = _render_email(event)
    assert "Cleared: x (manual)" in english_body
    with use_locale("es"):
        subject, body = _render_email(event)
    assert "cerrada" in subject and "resuelta" not in subject.lower()
    assert "La alerta de salud de" in body and "se ha cerrado." in body
    assert "Cerrada: x (Cerrada por una persona (manual))" in body


def test_notification_identifiers_are_worded_as_the_console_words_them() -> None:
    """A check type, comparator, drift class and confidence keep the raw code beside the word."""
    from app.modules.notification.delivery import _render_email

    health = _email_event("health.alert_raised")
    health.payload = {
        **_EMAIL_PAYLOADS["health.alert_raised"],
        "check_type": "disk_free_pct",
        "comparator": "lt",
    }
    drift = _email_event("ipam.drift_detected")
    drift.payload = {**_EMAIL_PAYLOADS["ipam.drift_detected"], "confidence": "high"}
    # English: exactly the identifiers that arrived.
    en_subject, en_body = _render_email(health)
    assert "disk_free_pct on pc-01" in en_subject and "Threshold: lt 10" in en_body
    with use_locale("es"):
        subject, body = _render_email(health)
        drift_subject, drift_body = _render_email(drift)
    assert "Disco libre (disk_free_pct) en pc-01" in subject
    assert "Umbral: por debajo de (lt) 10" in body
    assert "Conflicto (conflict) en 10.0.0.5" in drift_subject
    assert "Confianza: alta (high)" in drift_body


def test_service_desk_a_status_is_named_the_way_the_console_names_it() -> None:
    """core/ticket_words.py: English keeps the repr that was always printed; Spanish
    puts the console's `TicketStatus` word in «», as the console does."""
    from app.modules.service_desk.errors import TicketValidationError
    from app.modules.service_desk.lifecycle import assert_transition

    with pytest.raises(TicketValidationError) as english:
        assert_transition("incident", "closed", "new")
    assert str(english.value) == "illegal incident transition: 'closed' -> 'new'"
    with use_locale("es"), pytest.raises(TicketValidationError) as spanish:
        assert_transition("incident", "closed", "new")
    assert str(spanish.value) == "transición no permitida en incidente: «Cerrado» -> «Nuevo»"


async def test_email_intake_the_mailbox_test_answers_in_the_requests_language(
    api_env: Any,
) -> None:
    """email_intake/api.py `POST /mailboxes/{id}/test`: the sentence in `detail` is worded
    for the reader; the audit entry keeps the English the poller always wrote."""
    from sqlalchemy import select, update
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models import AuditEvent, EmailMailbox
    from tests.test_email_intake_api import EMAIL_INTAKE_PREFIX, _seed_mailbox

    env = await _seed_mailbox(api_env, host="127.0.0.1", port=1)
    async with AsyncSession(api_env.admin_engine) as session:
        await session.execute(
            update(EmailMailbox)
            .where(EmailMailbox.id == env.mailbox_id)
            .values(auth_kind="microsoft_oauth", credential_id=None, active=False)
        )
        await session.commit()
    path = f"{EMAIL_INTAKE_PREFIX}/mailboxes/{env.mailbox_id}/test"
    english = await api_env.client.post(
        path, headers={**env.admin_headers, "Accept-Language": "en"}
    )
    spanish = await api_env.client.post(
        path, headers={**env.admin_headers, "Accept-Language": "es"}
    )
    assert english.json() == {
        "ok": False,
        "detail": "sign-in failed: not connected to Microsoft yet",
    }
    assert spanish.json() == {
        "ok": False,
        "detail": "el inicio de sesión falló: todavía no está conectado con Microsoft",
    }
    async with AsyncSession(api_env.admin_engine) as session:
        events = (
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == env.org_id,
                        AuditEvent.action == "email_intake.mailbox_tested",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(events) == 2
    assert {e.meta["detail"] for e in events} == {"sign-in failed: not connected to Microsoft yet"}
