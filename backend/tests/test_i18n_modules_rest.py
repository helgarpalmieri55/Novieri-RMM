"""Spanish server messages for user_admin, provisioning, ai_admin, ai_copilot,
ai_portal, patch, inventory, reporting, security_logs and keycloak_events.

One representative failing route per module, called twice: with
`Accept-Language: es` the `detail` is the catalogue's Spanish, and with
`Accept-Language: en` the English is exactly what it was before the sweep.
The full catalogue is checked by tests/test_i18n_catalog.py; this proves a real
request reaches `tr()` with the request's language. Wire words said inside a
sentence, text handed to the AI model, and text that is stored have their own
tests below.
"""

from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from moto import mock_aws
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.i18n import CATALOG_ES, use_locale
from app.core.router_inspect import flatten
from app.core.storage import ObjectStore
from app.models import User


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
    org = await api_env.create_org("i18n-rest")
    sub = await api_env.create_user(org, "org-admin")
    return api_env, await api_env.headers(org, sub)


# --------------------------------------------------------------------------- #
# user_admin
# --------------------------------------------------------------------------- #


@pytest.fixture()
async def users_env(
    api_env: Any, monkeypatch: pytest.MonkeyPatch
) -> tuple[Any, dict[str, str], UUID]:
    """The user-lifecycle router with a provisioning config and no real identity
    provider: every call below is refused before one is made."""
    from app.modules.user_admin.api import keycloak_config
    from app.modules.user_admin.api import router as users_router
    from app.modules.user_admin.kc_client import KeycloakAdminConfig

    if "/api/v1/users" not in {path for path, _route in flatten(api_env.app.router)}:
        api_env.app.include_router(users_router, prefix="/api/v1")
    api_env.app.dependency_overrides[keycloak_config] = lambda: KeycloakAdminConfig(
        base_url="https://idp.test", realm="it-suite", client_id="c", client_secret="s"
    )
    org = await api_env.create_org("i18n-users")
    sub = await api_env.create_user(org, "org-admin")
    headers = await api_env.headers(org, sub)
    async with AsyncSession(api_env.admin_engine) as session:
        me = (
            await session.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()
    yield api_env, headers, me
    api_env.app.dependency_overrides.pop(keycloak_config, None)


async def test_user_admin_a_user_that_does_not_exist(
    users_env: tuple[Any, dict[str, str], UUID],
) -> None:
    """user_admin/service.py `_target`: `no such user`, a 404."""
    env, headers, _me = users_env
    await _both_languages(
        env, "POST", f"/api/v1/users/{uuid4()}/disable", headers, status=404, english="no such user"
    )


async def test_user_admin_you_cannot_disable_your_own_account(
    users_env: tuple[Any, dict[str, str], UUID],
) -> None:
    """The self-lockout rail keeps its meaning in Spanish; the verb is the
    console's word (`Deshabilitar`)."""
    env, headers, me = users_env
    await _both_languages(
        env,
        "POST",
        f"/api/v1/users/{me}/disable",
        headers,
        status=403,
        english="you cannot disable your own account",
        spanish="no puedes deshabilitar tu propia cuenta",
    )
    await _both_languages(
        env,
        "DELETE",
        f"/api/v1/users/{me}",
        headers,
        status=403,
        english="you cannot delete your own account",
        spanish="no puedes eliminar tu propia cuenta",
    )


async def test_user_admin_the_super_admin_rail_names_the_role_and_every_condition(
    users_env: tuple[Any, dict[str, str], UUID],
) -> None:
    """Granting `super-admin` from an organization is refused in both languages,
    and the role name survives the translation untouched."""
    env, headers, me = users_env
    async with AsyncSession(env.admin_engine) as session:
        from app.models import Role

        role_id = (
            await session.execute(
                select(Role.id).where(Role.name == "super-admin", Role.org_id.is_(None))
            )
        ).scalar_one()
    await _both_languages(
        env,
        "POST",
        f"/api/v1/users/{me}/roles",
        headers,
        status=403,
        english=(
            "super-admin is cross-org platform administration and cannot be granted "
            "from an organization"
        ),
        spanish=(
            "super-admin es administración de la plataforma entre organizaciones y no se "
            "puede conceder desde una organización"
        ),
        json_body={"role_id": str(role_id)},
    )


def test_user_admin_the_provider_failure_names_the_action_in_the_consoles_words() -> None:
    """user_admin/words.py via wire_words.say: the action a refused identity-provider
    call was attempting, said inside the sentence."""
    from app.modules.user_admin.service import UserAdminError, _translating

    def failure() -> str:
        with pytest.raises(UserAdminError) as caught, _translating("send the enrolment email"):
            raise httpx.ConnectError("down")
        return caught.value.detail

    assert failure() == (
        "the identity provider was unreachable while trying to send the enrolment email "
        "(ConnectError)"
    )
    with use_locale("es"):
        assert failure() == (
            "no se pudo contactar con el proveedor de identidad al intentar enviar el correo "
            "de invitación (ConnectError)"
        )


async def test_user_admin_the_lockout_rails_keep_every_condition_in_spanish(
    api_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real `_refuse_stranding_the_org` (disable, delete, and the revoke of the
    role) and `_refuse_touching_a_super_admin` paths, driven by a caller who holds
    `users.manage` but is not the organization's only `org-admin`: the role, the verb
    and the way out each survive in Spanish, and English is what it was.
    `_refuse_self` has its own test above."""
    from app.models import Role, UserRole
    from app.modules.user_admin.api import keycloak_config
    from app.modules.user_admin.api import router as users_router
    from app.modules.user_admin.kc_client import KeycloakAdminConfig
    from tests.ai_admin_fakes import ai_user

    if "/api/v1/users" not in {path for path, _route in flatten(api_env.app.router)}:
        api_env.app.include_router(users_router, prefix="/api/v1")
    api_env.app.dependency_overrides[keycloak_config] = lambda: KeycloakAdminConfig(
        base_url="https://idp.test", realm="it-suite", client_id="c", client_secret="s"
    )
    try:
        org = await api_env.create_org("i18n-lockout")
        only_admin = await api_env.create_user(org, "org-admin")
        other = await api_env.create_user(org)
        _actor_id, actor_sub = await ai_user(api_env, org, "users.manage")
        headers = await api_env.headers(org, actor_sub)
        async with AsyncSession(api_env.admin_engine) as session:
            admin_id = (
                await session.execute(select(User.id).where(User.external_subject == only_admin))
            ).scalar_one()
            other_id = (
                await session.execute(select(User.id).where(User.external_subject == other))
            ).scalar_one()
            admin_assignment = (
                await session.execute(
                    select(UserRole.id)
                    .join(Role, Role.id == UserRole.role_id)
                    .where(UserRole.user_id == admin_id, Role.name == "org-admin")
                )
            ).scalar_one()
            super_admin_role = (
                await session.execute(
                    select(Role.id).where(Role.name == "super-admin", Role.org_id.is_(None))
                )
            ).scalar_one()
            session.add(UserRole(org_id=org, user_id=other_id, role_id=super_admin_role))
            await session.commit()

        stranding = (
            "this is the organization's last active org-admin and cannot be {verb}; "
            "grant org-admin to somebody else first"
        )
        stranding_es = (
            "esta cuenta es el último org-admin activo de la organización y no se puede "
            "{verb}; concede org-admin a otra persona primero"
        )
        await _both_languages(
            api_env, "POST", f"/api/v1/users/{admin_id}/disable", headers, status=403,
            english=stranding.format(verb="disabled"),
            spanish=stranding_es.format(verb="deshabilitar"),
        )
        await _both_languages(
            api_env, "DELETE", f"/api/v1/users/{admin_id}", headers, status=403,
            english=stranding.format(verb="deleted"),
            spanish=stranding_es.format(verb="eliminar"),
        )
        await _both_languages(
            api_env, "DELETE", f"/api/v1/users/{admin_id}/roles/{admin_assignment}", headers,
            status=403,
            english=stranding.format(verb="stripped of the role"),
            spanish=stranding_es.format(verb="dejar sin ese rol"),
        )
        await _both_languages(
            api_env, "POST", f"/api/v1/users/{other_id}/disable", headers, status=403,
            english=(
                "this account holds super-admin, which no console surface can grant back, "
                "so it cannot be disabled from here"
            ),
            spanish=(
                "esta cuenta tiene super-admin, que ninguna pantalla de la consola puede "
                "volver a conceder, así que no se puede deshabilitar desde aquí"
            ),
        )
    finally:
        api_env.app.dependency_overrides.pop(keycloak_config, None)


# --------------------------------------------------------------------------- #
# provisioning
# --------------------------------------------------------------------------- #


async def test_provisioning_a_run_that_does_not_exist(admin: tuple[Any, dict[str, str]]) -> None:
    """provisioning/api.py: `no such run`, a 404."""
    env, headers = admin
    await _both_languages(
        env, "GET", f"/api/v1/provisioning/runs/{uuid4()}", headers, status=404,
        english="no such run",
    )


async def test_provisioning_a_wake_with_no_site_server_for_the_segment(
    admin: tuple[Any, dict[str, str]],
) -> None:
    """provisioning/dispatch.py `_resolve_wake`: the wake is refused, with the
    segment the person named, before any run exists. The 422 comes from a
    profile-less request, so the refusal under test is the first one raised.
    It names the internal client (final review C): only that client's site
    servers are candidates, so "nothing in this org" was not true."""
    env, headers = admin
    english = (
        "no enabled site server of the organization's own client serves 10.9.9.0/24: a "
        "Wake-on-LAN packet must originate on the target's own segment, and a provisioning "
        "wake goes only through that client's site servers, none of which is declared for "
        "it. Promote one of its site servers on that segment, or start the run without "
        "wake_on_lan"
    )
    spanish = (
        "ningún servidor de sitio habilitado del cliente propio de la organización atiende "
        "10.9.9.0/24: un paquete de Wake-on-LAN tiene que originarse en el propio segmento "
        "del destino, y un encendido de aprovisionamiento solo pasa por los servidores de "
        "sitio de ese cliente, ninguno de los cuales está declarado para él. Promueve uno de "
        "sus servidores de sitio en ese segmento o inicia la ejecución sin wake_on_lan"
    )
    with mock_aws():
        env.app.state.object_store = ObjectStore(env.settings)
        await _both_languages(
            env,
            "POST",
            f"/api/v1/provisioning/profiles/{uuid4()}/run",
            headers,
            status=422,
            english=english,
            spanish=spanish,
            json_body={
                "wake_on_lan": {"target_mac": "a4:bb:6d:11:22:33", "segment": "10.9.9.0/24"}
            },
        )


async def test_provisioning_a_site_servers_refusal_inside_a_wake_reads_in_the_requests_language(
    admin: tuple[Any, dict[str, str]],
) -> None:
    """dispatch.py re-raises `str(SiteServerError)` as the 422 detail. The site
    server module translates that text where it composes it, so the provisioning
    refusal reads in the request's language too, with the person's segment in it."""
    env, headers = admin
    body = {"wake_on_lan": {"target_mac": "a4:bb:6d:11:22:33", "segment": "fd00::/64"}}
    path = f"/api/v1/provisioning/profiles/{uuid4()}/run"
    with mock_aws():
        env.app.state.object_store = ObjectStore(env.settings)
        status_en, english = await _detail(env, "POST", path, headers, "en", body)
        status_es, spanish = await _detail(env, "POST", path, headers, "es", body)
    assert (status_en, status_es) == (422, 422)
    assert english.startswith("segment 'fd00::/64' is IPv6: a Wake-on-LAN magic packet")
    assert spanish.startswith("el segmento 'fd00::/64' es IPv6: un paquete mágico de Wake-on-LAN")


# --------------------------------------------------------------------------- #
# ai_admin: the console chat. Text handed to the model (the system prompt, the
# tool descriptions) is model-facing and is not translated; what the person sees
# in the chat (refusals, rate limits, warnings) is.
# --------------------------------------------------------------------------- #


async def test_ai_admin_a_conversation_that_does_not_exist(api_env: Any) -> None:
    """ai_admin/api.py: `no ai session {id}`, a 404 shown in the console chat."""
    from tests.ai_admin_fakes import ai_user

    org = await api_env.create_org("i18n-ai-admin")
    _user, sub = await ai_user(api_env, org, "ai.use")
    headers = await api_env.headers(org, sub)
    session_id = uuid4()
    await _both_languages(
        api_env,
        "GET",
        f"/api/v1/ai/sessions/{session_id}",
        headers,
        status=404,
        english=f"no ai session {session_id}",
        spanish=f"no existe la sesión de IA {session_id}",
    )


class _FakeRedis:
    """Just enough redis for the rate limiter, with each counter set by the test."""

    def __init__(self, *, user: int = 1, org: int = 1, tokens_used: int = 0) -> None:
        self.user, self.org, self.tokens_used = user, org, tokens_used

    async def incr(self, key: str) -> int:
        return self.user if ":user:" in key else self.org

    async def expire(self, key: str, seconds: int) -> None:
        return None

    async def get(self, key: str) -> int:
        return self.tokens_used


async def test_ai_admin_the_rate_limits_and_the_budget_warning_reach_the_chat_in_spanish() -> None:
    """The three 429s and the soft warning are shown to the person asking, each
    with its number."""
    from fastapi import HTTPException

    from app.core.ai.settings import EffectiveAiSettings
    from app.modules.ai_admin.api import _enforce_rate_limits

    org, user = uuid4(), uuid4()
    limits = EffectiveAiSettings(
        max_turns_per_user_per_minute=2, max_turns_per_org_per_hour=50, daily_token_budget=1000
    )

    async def refused(redis: _FakeRedis) -> str:
        with pytest.raises(HTTPException) as caught:
            await _enforce_rate_limits(redis, limits, org_id=org, user_id=user)
        assert caught.value.status_code == 429
        return str(caught.value.detail)

    cases = {
        "user": (
            _FakeRedis(user=3),
            "you may ask at most 2 questions per minute; try again shortly",
            "puedes hacer como máximo 2 preguntas por minuto; inténtalo de nuevo en breve",
        ),
        "org": (
            _FakeRedis(org=51),
            "this organization may ask at most 50 questions per hour; try again shortly",
            "esta organización puede hacer como máximo 50 preguntas por hora; inténtalo de "
            "nuevo en breve",
        ),
        "budget": (
            _FakeRedis(tokens_used=1000),
            "this organization's daily token budget of 1000 is spent",
            "se agotó el límite diario de uso de esta organización, de 1000 tokens",
        ),
    }
    for redis, english, spanish in cases.values():
        assert await refused(redis) == english
        with use_locale("es"):
            assert await refused(redis) == spanish

    near = _FakeRedis(tokens_used=900)
    assert await _enforce_rate_limits(near, limits, org_id=org, user_id=user) == [
        "900 of this organization's daily budget of 1000 tokens has been used."
    ]
    with use_locale("es"):
        assert await _enforce_rate_limits(near, limits, org_id=org, user_id=user) == [
            "se han usado 900 de los 1000 tokens del límite diario de uso de esta organización."
        ]



# --------------------------------------------------------------------------- #
# ai_copilot: the technician copilot in a ticket
# --------------------------------------------------------------------------- #


async def test_ai_copilot_a_ticket_that_does_not_exist_and_a_missing_ai_use(
    api_env: Any,
) -> None:
    """ai_copilot/api.py: the `ai.use` gate keeps its code in both languages, and
    a ticket the person cannot see is a 404 in the console chat."""
    from tests.ai_admin_fakes import ai_user

    org = await api_env.create_org("i18n-ai-copilot")
    _u, staff = await ai_user(api_env, org, "ai.use", "ticket.read")
    _v, no_ai = await ai_user(api_env, org, "ticket.read")
    ticket_id = uuid4()
    path = f"/api/v1/service-desk/tickets/{ticket_id}/ai/sessions"
    await _both_languages(
        api_env,
        "POST",
        path,
        await api_env.headers(org, staff),
        status=404,
        english=f"no ticket {ticket_id}",
        spanish=f"no existe el ticket {ticket_id}",
    )
    await _both_languages(
        api_env,
        "POST",
        path,
        await api_env.headers(org, no_ai),
        status=403,
        english="requires ai.use",
        spanish="requiere ai.use",
    )


# --------------------------------------------------------------------------- #
# ai_portal: the portal assistant, shown to a requester
# --------------------------------------------------------------------------- #


async def test_ai_portal_a_conversation_that_does_not_exist(api_env: Any) -> None:
    """ai_portal/api.py: the requester's own 404 in the portal assistant. A
    conversation of another person reads the same, by design."""
    org = await api_env.create_org("i18n-ai-portal")
    sub = await api_env.create_user(org, "requester")
    headers = await api_env.headers(org, sub)
    session_id = uuid4()
    await _both_languages(
        api_env,
        "POST",
        f"/api/v1/portal/ai/sessions/{session_id}/turns",
        headers,
        status=404,
        english=f"no ai session {session_id}",
        spanish=f"no existe la sesión de IA {session_id}",
        json_body={"question": "my printer is on fire"},
    )


# --------------------------------------------------------------------------- #
# patch
# --------------------------------------------------------------------------- #


async def test_patch_a_device_approval_and_change_that_do_not_exist(
    admin: tuple[Any, dict[str, str]],
) -> None:
    """patch/api.py: three 404s, two of them reusing words the catalogue already
    holds (`no such device`, `no such change`)."""
    env, headers = admin
    await _both_languages(
        env, "GET", f"/api/v1/patch/devices/{uuid4()}/status", headers, status=404,
        english="no such device",
    )
    await _both_languages(
        env, "DELETE", f"/api/v1/patch/approvals/{uuid4()}", headers, status=404,
        english="no such approval",
    )


async def test_patch_a_restart_the_platform_cannot_book(api_env: Any) -> None:
    """patch/reboot.py `RestartUnavailable`: shown to the operator who asked, with the
    way out kept (`approve it before ...`)."""
    from app.models import Device

    org_id = await api_env.create_org("i18n-patch")
    headers = await api_env.headers(org_id, await api_env.create_user(org_id, "org-admin"))
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        device = Device(
            org_id=org_id,
            hostname=f"wks-{uuid4().hex[:6]}",
            os_family="windows",
            client_id=org_id,
            site_id=org_id,
        )
        session.add(device)
        await session.commit()
        device_id = device.id
    await _both_languages(
        api_env,
        "POST",
        f"/api/v1/patch/devices/{device_id}/restart",
        headers,
        status=409,
        english="this device has no agent, so it cannot be restarted from here",
        spanish="este dispositivo no tiene agente, así que no se puede reiniciar desde aquí",
    )


# --------------------------------------------------------------------------- #
# inventory
# --------------------------------------------------------------------------- #


async def test_inventory_a_device_that_does_not_exist(admin: tuple[Any, dict[str, str]]) -> None:
    """inventory/api.py: `no such device`, reusing the catalogue's word."""
    env, headers = admin
    await _both_languages(
        env, "GET", f"/api/v1/devices/{uuid4()}/inventory", headers, status=404,
        english="no such device",
    )


async def test_inventory_a_rule_the_author_must_fix_is_worded_in_the_request_language(
    admin: tuple[Any, dict[str, str]],
) -> None:
    """inventory/rules.py validators: a typo'd fact under `ne` claims every device, so it
    is refused at authorship with a message the author reads. The console strips
    "Value error, " and shows the rest."""
    env, headers = admin
    typo = {"all": [{"fact": "os_familly", "op": "ne", "value": "windows"}]}
    # A client is named, so the only error in the body is the rule's.
    body = {"name": "oops", "client_id": str(uuid4()), "rules": typo}

    async def messages(lang: str) -> list[str]:
        response = await env.client.post(
            "/api/v1/device-groups", headers={**headers, "Accept-Language": lang}, json=body
        )
        assert response.status_code == 422, response.text
        return [item["msg"] for item in response.json()["detail"]]

    english, spanish = (await messages("en"))[0], (await messages("es"))[0]
    assert english.startswith("Value error, unknown fact `os_familly` — use one of hostname")
    assert spanish.startswith(
        "Value error, dato desconocido `os_familly`: usa uno de hostname"
    )
    assert "`facts.<clave>`" in spanish


# --------------------------------------------------------------------------- #
# reporting: only errors and refusals. Report titles, summaries, column names and
# CSV headers are data the console words itself (or shows as sent), so a Spanish
# request leaves them alone.
# --------------------------------------------------------------------------- #


async def test_reporting_a_window_wider_than_a_year(api_env: Any) -> None:
    """reporting/definitions/sla_attainment.py: the model validator's refusal, shown
    in the console form after the "Value error, " prefix is stripped."""
    from datetime import UTC, datetime, timedelta
    from urllib.parse import urlencode

    from tests.reporting_fixtures import reporting_user

    org = await api_env.create_org("i18n-reporting")
    _u, sub = await reporting_user(api_env, org, "reporting.read", "ticket.read", "sla.read")
    headers = await api_env.headers(org, sub)
    now = datetime.now(UTC)
    query = urlencode(
        {"from": (now - timedelta(days=400)).isoformat(), "to": now.isoformat()}
    )

    async def refusal(lang: str) -> str:
        response = await api_env.client.get(
            f"/api/v1/reports/sla-attainment?{query}",
            headers={**headers, "Accept-Language": lang},
        )
        assert response.status_code == 422, response.text
        return str(response.json()["detail"][0]["msg"])

    assert await refusal("en") == "Value error, the window may not exceed 366 days"
    assert await refusal("es") == "Value error, la ventana no puede superar 366 días"


async def test_reporting_a_timeout_says_so_in_the_request_language(
    api_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """reporting/api.py: the 504 keeps its machine `error` code and the report key;
    only `message` is worded for the person."""
    from app.modules.reporting import api as reporting_api
    from app.modules.reporting.errors import ReportTimeout
    from tests.reporting_fixtures import reporting_user

    async def too_slow(*args: Any, **kwargs: Any) -> Any:
        raise ReportTimeout("fleet-posture")

    monkeypatch.setattr(reporting_api, "execute_report", too_slow)
    org = await api_env.create_org("i18n-reporting-timeout")
    _u, sub = await reporting_user(
        api_env, org, "reporting.read", "devices.read", "health.read"
    )
    headers = await api_env.headers(org, sub)

    async def detail(lang: str) -> dict[str, Any]:
        response = await api_env.client.get(
            "/api/v1/reports/fleet-posture", headers={**headers, "Accept-Language": lang}
        )
        assert response.status_code == 504, response.text
        assert response.headers["Content-Language"] == lang
        body: dict[str, Any] = response.json()["detail"]
        return body

    english, spanish = await detail("en"), await detail("es")
    assert english["error"] == spanish["error"] == "report_timeout"
    assert english["report_key"] == spanish["report_key"] == "fleet-posture"
    assert english["message"] == (
        "the report exceeded its statement timeout; narrow the window or the filters and retry"
    )
    assert spanish["message"] == (
        "el informe superó el tiempo máximo de la consulta; acota la ventana o los filtros e "
        "inténtalo de nuevo"
    )


# --------------------------------------------------------------------------- #
# security_logs and keycloak_events: events ingested from outside are stored as
# recorded and never translated. Only the console's own refusal is.
# --------------------------------------------------------------------------- #


async def test_security_logs_an_event_that_does_not_exist(
    admin: tuple[Any, dict[str, str]],
) -> None:
    """security_logs/api.py: `no such event`, a 404."""
    env, headers = admin
    await _both_languages(
        env, "GET", f"/api/v1/security-events/{uuid4()}", headers, status=404,
        english="no such event",
    )


async def test_security_logs_a_rejected_batch_is_recorded_in_english_in_any_language() -> None:
    """security_logs/ingest.py: the gateway writes `str(exc)` into the audit chain
    (`security_logs.ingest_rejected`), which is never translated."""
    from types import SimpleNamespace

    from app.modules.security_logs.ingest import SecurityIngestError, ingest_events

    event = SimpleNamespace(event_type="coffee_machine")
    for lang in ("en", "es"):
        with use_locale(lang), pytest.raises(SecurityIngestError) as caught:
            await ingest_events(None, SimpleNamespace(), [event])  # type: ignore[arg-type]
        assert str(caught.value) == "unknown event_type 'coffee_machine'"


def test_keycloak_events_nothing_it_forwards_or_reports_is_translated(
    tmp_path: Any,
) -> None:
    """keycloak_events has no route and no person-facing sentence. What it writes
    to the audit chain is a machine action code, and its configuration errors go to
    the health probe (a reason code) and the worker log, so both stay as they are in
    a Spanish request."""
    from types import SimpleNamespace

    from app.modules.keycloak_events.client import KeycloakEventsConfig
    from app.modules.keycloak_events.mapping import map_event

    passkey = {"type": "UPDATE_CREDENTIAL", "details": {"credential_type": "webauthn"}}
    missing = SimpleNamespace(
        keycloak_admin_url="https://idp.test",
        keycloak_events_secret_path=str(tmp_path / "absent"),
    )
    for lang in ("en", "es"):
        with use_locale(lang):
            assert map_event(passkey) == "auth.passkey_registered"
            with pytest.raises(FileNotFoundError, match="does not exist"):
                KeycloakEventsConfig.from_settings(missing)


async def test_patch_a_closed_restarts_reason_is_said_in_spanish_and_stored_in_english(
    api_env: Any,
) -> None:
    """patch/words.py: `PatchReboot.reason` (and the audit) stay English; the console
    prints `restart.reason` as the explanation, so the response words it. One fixed
    reason, and the one that carries the job's status in the console's own word."""
    from app.models import Device, PatchReboot

    org_id = await api_env.create_org("i18n-patch-reason")
    headers = await api_env.headers(org_id, await api_env.create_user(org_id, "org-admin"))
    cases = {
        "cancelled": (
            "the OS no longer needs a restart",
            "el sistema operativo ya no necesita reiniciarse",
        ),
        "failed": (
            "the restart job failed three times",
            "la tarea de reinicio terminó en estado «Fallido» tres veces",
        ),
    }
    devices: dict[str, UUID] = {}
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        for state, (english, _spanish) in cases.items():
            device = Device(
                org_id=org_id,
                hostname=f"wks-{state}",
                os_family="windows",
                client_id=org_id,
                site_id=org_id,
            )
            session.add(device)
            await session.flush()
            session.add(
                PatchReboot(
                    org_id=org_id, device_id=device.id, origin="scan", state=state,
                    reason=english, attempts=3,
                )
            )
            devices[state] = device.id
        await session.commit()
    for state, (english, spanish) in cases.items():
        path = f"/api/v1/patch/devices/{devices[state]}/status"
        for lang, expected in (("en", english), ("es", spanish)):
            response = await api_env.client.get(path, headers={**headers, "Accept-Language": lang})
            assert response.status_code == 200, response.text
            assert response.json()["restart"]["reason"] == expected
    async with AsyncSession(api_env.admin_engine) as session:
        stored = set(
            (await session.execute(select(PatchReboot.reason).where(PatchReboot.org_id == org_id)))
            .scalars()
        )
    assert stored == {english for english, _ in cases.values()}


async def test_reporting_the_catalogue_and_a_report_read_in_spanish_but_openapi_stays_english(
    api_env: Any,
) -> None:
    """Ruling: the catalogue prose is UI copy the console prints, so it is worded
    server-side: each definition's summary and description, the parameter
    descriptions inside `params_schema`, and the `recency` clause of a report's
    sources. The OpenAPI document is built from the same marked text and stays
    English; report rows and CSV headers are data."""
    from datetime import UTC, datetime, timedelta
    from urllib.parse import urlencode

    from tests.reporting_fixtures import reporting_user

    org = await api_env.create_org("i18n-reporting-catalogue")
    _u, sub = await reporting_user(
        api_env, org, "reporting.read", "ticket.read", "sla.read", "devices.read", "health.read"
    )
    headers = await api_env.headers(org, sub)

    async def catalogue(lang: str) -> dict[str, Any]:
        response = await api_env.client.get(
            "/api/v1/reports", headers={**headers, "Accept-Language": lang}
        )
        assert response.status_code == 200, response.text
        return {entry["key"]: entry for entry in response.json()["reports"]}

    english, spanish = await catalogue("en"), await catalogue("es")
    assert english.keys() == spanish.keys() == {
        "fleet-posture", "patch-compliance", "backup-coverage", "sla-attainment"
    }
    sla_en, sla_es = english["sla-attainment"], spanish["sla-attainment"]
    assert sla_en["summary"] == "SLA attainment by queue and priority over a closed window."
    assert sla_es["summary"] == "Cumplimiento de SLA por cola y prioridad en una ventana cerrada."
    assert sla_es["description"].startswith("Una fila por par (cola, prioridad)")
    assert sla_en["description"].startswith("One row per (queue, priority) pair")
    props_en = sla_en["params_schema"]["properties"]
    props_es = sla_es["params_schema"]["properties"]
    assert props_en["queue_id"]["description"] == "Restrict to one queue."
    assert props_es["queue_id"]["description"] == "Limita a una cola."
    assert props_es["from"]["description"].startswith("Inicio de la ventana, incluido")
    # Nothing but the prose differs: keys, types, bounds and defaults are the same.
    assert props_en["limit"]["maximum"] == props_es["limit"]["maximum"]
    assert sla_en["params_schema"]["required"] == sla_es["params_schema"]["required"]
    for key, entry in spanish.items():
        assert entry["summary"] != english[key]["summary"], key

    now = datetime.now(UTC)
    query = urlencode(
        {"from": (now - timedelta(days=30)).isoformat(), "to": now.isoformat()}
    )

    async def sources(lang: str) -> dict[str, str]:
        response = await api_env.client.get(
            f"/api/v1/reports/sla-attainment?{query}",
            headers={**headers, "Accept-Language": lang},
        )
        assert response.status_code == 200, response.text
        return {s["module"]: s["recency"] for s in response.json()["meta"]["sources"]}

    recency_en, recency_es = await sources("en"), await sources("es")
    assert recency_en["sla"].startswith("current, and MUTABLE BY DESIGN")
    assert recency_es["sla"].startswith("actual y MODIFICABLE POR DISEÑO")
    assert recency_en["service_desk"] == (
        "current, and MUTABLE -- a ticket edited today changes this figure"
    )

    # The OpenAPI document is untouched by the request's language.
    api_env.app.openapi_schema = None
    with use_locale("es"):
        schema = api_env.app.openapi()
    api_env.app.openapi_schema = None
    operation = schema["paths"]["/api/v1/reports/sla-attainment"]["get"]
    assert operation["summary"] == "SLA attainment by queue and priority over a closed window."
