"""Spanish server messages for core/, services/, jobs/ and api/.

One representative failing route per area, called twice: with
`Accept-Language: es` the `detail` is the catalogue's Spanish, and with
`Accept-Language: en` the English is exactly what it was before the sweep.
The full catalogue is checked by tests/test_i18n_catalog.py; this proves a real
request reaches `tr()` with the request's language.
"""

from typing import Any
from uuid import uuid4

import pytest

from app.core.i18n import CATALOG_ES


async def _detail(env: Any, method: str, path: str, headers: dict[str, str], lang: str) -> Any:
    response = await env.client.request(
        method, path, headers={**headers, "Accept-Language": lang}
    )
    assert response.headers["Content-Language"] == lang
    return response.status_code, response.json()["detail"]


async def _both_languages(
    env: Any, method: str, path: str, headers: dict[str, str], *, status: int, english: str
) -> None:
    assert await _detail(env, method, path, headers, "en") == (status, english)
    assert await _detail(env, method, path, headers, "es") == (status, CATALOG_ES[english])
    assert CATALOG_ES[english] != english


@pytest.fixture()
async def admin(api_env: Any) -> tuple[Any, dict[str, str]]:
    org = await api_env.create_org("i18n-sweep")
    sub = await api_env.create_user(org, "org-admin")
    return api_env, await api_env.headers(org, sub)


async def test_core_a_role_that_does_not_exist(admin: tuple[Any, dict[str, str]]) -> None:
    """core/rbac_admin.py: RoleAdminError carries the composed message."""
    env, headers = admin
    await _both_languages(
        env, "DELETE", f"/api/v1/roles/{uuid4()}", headers, status=404, english="no such role"
    )


async def test_services_a_device_group_that_does_not_exist(
    admin: tuple[Any, dict[str, str]],
) -> None:
    """services/device_groups.py: DeviceGroupError carries the composed message."""
    env, headers = admin
    await _both_languages(
        env,
        "GET",
        f"/api/v1/device-groups/{uuid4()}",
        headers,
        status=404,
        english="no such device group",
    )


async def test_jobs_cancelling_a_job_that_does_not_exist(
    admin: tuple[Any, dict[str, str]],
) -> None:
    """jobs/cancel.py: JobNotFound carries the composed message."""
    env, headers = admin
    await _both_languages(
        env,
        "POST",
        f"/api/v1/jobs/{uuid4()}/cancel",
        headers,
        status=404,
        english="no such job",
    )


async def test_api_a_request_without_credentials(api_env: Any) -> None:
    """api/deps.py: the dependency every route shares."""
    await _both_languages(
        api_env,
        "GET",
        "/api/v1/devices",
        {},
        status=401,
        english="missing bearer token",
    )


def test_the_http_admission_refusals_are_translated_but_the_audited_reason_is_not() -> None:
    """The reasons that go to the log and the audit chain stay the English
    constants; api/deps.py translates only the HTTP body, from the same text."""
    from app.core.identity import status_refusal
    from app.core.identity.email_domain import domain_refusal

    status_reason = status_refusal("disabled")
    domain_reason = domain_refusal("someone@elsewhere.example", "toeshee.com")
    assert status_reason == "user account is not active"
    assert domain_reason == "email domain not permitted for this console"
    assert status_reason in CATALOG_ES
    assert domain_reason in CATALOG_ES


def test_a_sentence_assembled_from_fragments_translates_every_fragment() -> None:
    """jobs/cancel.py builds 'this job <outcome> at <time> and ...' from parts;
    none of the English parts may survive into the Spanish sentence."""
    from datetime import UTC, datetime
    from types import SimpleNamespace
    from typing import cast

    from app.core.i18n import use_locale
    from app.jobs.cancel import _finished_refusal
    from app.models import Job
    from app.models.core import JobStatus

    job = cast(
        Job,
        SimpleNamespace(
            status=JobStatus.failed, finished_at=datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
        ),
    )
    english = _finished_refusal(job)
    assert english.startswith("this job failed at 2026-10-01T12:00:00+00:00 and cannot be")
    with use_locale("es"):
        spanish = _finished_refusal(job)
    assert spanish.startswith("esta tarea falló el 2026-10-01T12:00:00+00:00 y no se puede")
    assert "failed" not in spanish and " at " not in spanish


async def test_api_a_job_for_a_device_under_review_is_refused_in_the_requests_language(
    api_env: Any,
) -> None:
    """main.py's DeviceAwaitingReview handler: a 409 body a person reads."""
    from app.jobs.review_gate import WAITING_FOR_REVIEW
    from tests.ipam_fixtures import seed_agent, seed_device, seed_org

    org = await seed_org(api_env.admin_engine, "i18n-gate")
    device_id = await seed_device(api_env.admin_engine, org.org_id, review_state="pending")
    await seed_agent(api_env.admin_engine, org.org_id, device_id)
    sub = await api_env.create_user(org.org_id, "org-admin")
    headers = await api_env.headers(org.org_id, sub)
    body = {"type": "hello", "device_id": str(device_id), "params": {}}

    english = await api_env.client.post(
        "/api/v1/jobs", json=body, headers={**headers, "Accept-Language": "en"}
    )
    spanish = await api_env.client.post(
        "/api/v1/jobs", json=body, headers={**headers, "Accept-Language": "es"}
    )
    assert english.status_code == spanish.status_code == 409
    assert english.json()["detail"] == WAITING_FOR_REVIEW == "waiting for review"
    assert spanish.json()["detail"] == CATALOG_ES[WAITING_FOR_REVIEW] == "esperando revisión"
    assert spanish.json()["device_id"] == str(device_id)


async def test_jobs_a_governed_job_type_is_refused_with_the_marked_reason_translated(
    admin: tuple[Any, dict[str, str]],
) -> None:
    """jobs/direct_dispatch.py: the 403 detail is a value of an N_()-marked table."""
    from app.jobs.direct_dispatch import MODULE_GOVERNED_REASONS

    env, headers = admin
    body = {"type": "patch.scan", "device_id": str(uuid4()), "params": {}}
    reason = MODULE_GOVERNED_REASONS["patch.scan"]
    for lang, expected in (("en", reason), ("es", CATALOG_ES[reason])):
        response = await env.client.post(
            "/api/v1/jobs", json=body, headers={**headers, "Accept-Language": lang}
        )
        assert response.status_code == 403
        assert response.json()["detail"] == expected
    assert CATALOG_ES[reason] != reason
    assert "(patch.scan)" in CATALOG_ES[reason]  # the permission code is kept verbatim


def test_tr_marked_looks_a_message_up_verbatim_and_never_formats_it() -> None:
    from app.core.approved_actions import NON_APPROVABLE_REASONS
    from app.core.i18n import tr_marked, use_locale

    message = NON_APPROVABLE_REASONS["agent.uninstall"]
    assert "{device_id}" in message  # the one marked value with a literal brace
    assert tr_marked(message) == message
    with use_locale("es"):
        spanish = tr_marked(message)
        assert tr_marked("not in the catalogue {x}") == "not in the catalogue {x}"
    assert spanish == CATALOG_ES[message] != message
    assert "POST /api/v1/devices/{device_id}/review/reject" in spanish
