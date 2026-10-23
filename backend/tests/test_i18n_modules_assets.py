"""Spanish server messages for budget, change_mgmt, documentation, backup, bcp,
deploy, remote, site_server, licenses and subscriptions.

One representative failing route per module, called twice: with
`Accept-Language: es` the `detail` is the catalogue's Spanish, and with
`Accept-Language: en` the English is exactly what it was before the sweep.
The full catalogue is checked by tests/test_i18n_catalog.py; this proves a real
request reaches `tr()` with the request's language. Wire words said inside a
sentence, and text that is stored, have their own tests below.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from app.core.client_scope import ClientScope
from app.core.i18n import CATALOG_ES, use_locale
from tests.test_backup_offsite import aws, vault  # noqa: F401  (fixtures)
from tests.test_backup_result import object_store  # noqa: F401  (a fixture)


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
    org = await api_env.create_org("i18n-assets")
    sub = await api_env.create_user(org, "org-admin")
    return api_env, await api_env.headers(org, sub)


async def test_budget_a_budget_that_does_not_exist(admin: tuple[Any, dict[str, str]]) -> None:
    """budget/budgets.py: BudgetNotFoundError, mapped to 404 by budget/api.py."""
    env, headers = admin
    budget_id = uuid4()
    await _both_languages(
        env,
        "GET",
        f"/api/v1/budget/budgets/{budget_id}",
        headers,
        status=404,
        english=f"budget {budget_id} not found",
        spanish=f"no se encontró el presupuesto {budget_id}",
    )


def test_budget_wire_words_are_the_consoles_in_spanish_and_unchanged_in_english() -> None:
    """budget/words.py via app/core/wire_words.py: a granularity, a source and a link
    type said inside a sentence."""
    from app.modules.budget.errors import BudgetValidationError
    from app.modules.budget.periods import period_bounds

    def refusal(granularity: str) -> str:
        with pytest.raises(BudgetValidationError) as caught:
            period_bounds(2026, 1, granularity, 9)
        return str(caught.value)

    assert refusal("quarter") == "period_index 9 out of range 1..4 for quarter"
    # A value no table knows is printed as it arrived, in both languages.
    assert refusal("week") == "unknown granularity 'week'"
    with use_locale("es"):
        assert refusal("quarter") == (
            "period_index 9 fuera de rango 1..4 para la granularidad trimestre"
        )
        assert refusal("week") == "granularidad desconocida 'week'"

    from app.core.wire_words import say
    from app.modules.budget.words import (
        EXPENSE_SOURCE_WORDS,
        EXPENSE_STATUS_WORDS,
        LINK_TYPE_WORDS,
    )

    assert say(EXPENSE_SOURCE_WORDS, "subscription", quoted=True) == "'subscription'"
    assert say(EXPENSE_STATUS_WORDS, "void") == "void"
    assert say(LINK_TYPE_WORDS, "license") == "license"
    with use_locale("es"):
        assert say(EXPENSE_SOURCE_WORDS, "subscription", quoted=True) == "«Suscripción»"
        assert say(EXPENSE_STATUS_WORDS, "void") == "anulado"
        assert say(LINK_TYPE_WORDS, "license") == "la licencia"
        assert say(LINK_TYPE_WORDS, "spaceship") == "spaceship"


async def test_change_mgmt_a_change_that_does_not_exist(
    admin: tuple[Any, dict[str, str]],
) -> None:
    """change_mgmt/api.py: `no such change`, a 404."""
    env, headers = admin
    await _both_languages(
        env,
        "GET",
        f"/api/v1/changes/{uuid4()}",
        headers,
        status=404,
        english="no such change",
        spanish="no existe ese cambio",
    )


def test_change_mgmt_an_illegal_transition_names_the_consoles_words() -> None:
    """change_mgmt/state.py via words.py: the action and the state a person knows
    from the buttons and the status pill."""
    from app.modules.change_mgmt.state import ChangeValidationError, next_state

    def refusal() -> str:
        with pytest.raises(ChangeValidationError) as caught:
            next_state(
                current="draft",
                action="complete",
                type_="normal",
                created_by=uuid4(),
                actor_id=uuid4(),
                has_retrospective_approval=False,
            )
        return str(caught.value)

    assert refusal() == "illegal transition: complete from draft"
    with use_locale("es"):
        assert refusal() == "transición no permitida: marcar como completado desde «Borrador»"


async def test_change_mgmt_a_gate_refusal_is_said_in_spanish_and_audited_in_english(
    api_env: Any,
) -> None:
    """change_mgmt/gates.py GateRefusal: the person who asks in Spanish is told in
    Spanish; the `change.gate_refused` audit entry keeps the English sentence."""
    from tests.test_change_gates import _actions, _fleet, _gate

    f = await _fleet(api_env)
    await _gate(api_env, f, f["servers"], applies_to_patches=True, applies_to_deployments=False)
    headers = await api_env.headers(f["org_id"], f["admin"])
    body = {"patch_id": str(f["patch"]), "ring_name": "broad"}
    english = (
        "Approving updates for the broad ring reaches the Servers group, which needs an "
        "approved change. Choose an approved change to link, or have one approved first."
    )
    spanish = (
        "Aprobar actualizaciones para el anillo broad alcanza el grupo Servers, que exige un "
        "cambio aprobado. Elige un cambio aprobado para vincular, o haz que se apruebe uno primero."
    )
    en = await api_env.client.post(
        "/api/v1/patch/approvals", json=body, headers={**headers, "Accept-Language": "en"}
    )
    es = await api_env.client.post(
        "/api/v1/patch/approvals", json=body, headers={**headers, "Accept-Language": "es"}
    )
    assert (en.status_code, en.json()["detail"]) == (409, english)
    assert (es.status_code, es.json()["detail"]) == (409, spanish)
    refused = await _actions(api_env, f["org_id"], "change.gate_refused")
    assert [e.meta["reason"] for e in refused] == [english, english]


async def test_documentation_an_article_that_does_not_exist(
    admin: tuple[Any, dict[str, str]],
) -> None:
    """documentation/api.py: `no such article`, a 404."""
    env, headers = admin
    await _both_languages(
        env,
        "GET",
        f"/api/v1/docs/articles/{uuid4()}",
        headers,
        status=404,
        english="no such article",
        spanish="no existe ese artículo",
    )


async def test_documentation_a_document_that_does_not_exist(
    admin: tuple[Any, dict[str, str]],
) -> None:
    """documentation/documents.py: DocumentNotFound, a 404 with the id in it."""
    env, headers = admin
    document_id = uuid4()
    await _both_languages(
        env,
        "GET",
        f"/api/v1/docs/documents/{document_id}",
        headers,
        status=404,
        english=f"no document {document_id} in this organization",
        spanish=f"no hay ningún documento {document_id} en esta organización",
    )


def test_documentation_the_accepted_list_and_the_link_target_are_worded() -> None:
    """documentation/filetypes.py (an N_ constant inside a template) and
    documentation/service.py (the link-target words, with their article)."""
    from app.modules.documentation.filetypes import UnsupportedFileType, sniff
    from app.modules.documentation.words import LINK_TARGET_WORDS

    def refusal() -> str:
        with pytest.raises(UnsupportedFileType) as caught:
            sniff(b"\xff\xfe\x00\x01 not utf-8 \x80", "x.bin")
        return str(caught.value)

    english = refusal()
    assert english.startswith("unsupported file type. This library accepts: PDF; PNG,")
    with use_locale("es"):
        spanish = refusal()
    assert spanish.startswith("tipo de archivo no admitido. Esta biblioteca acepta: PDF; imágenes")
    assert "UTF-16" in spanish and "libraries" not in spanish

    from app.core.wire_words import say

    assert say(LINK_TARGET_WORDS, "ipam_subnet") == "ipam_subnet"
    with use_locale("es"):
        assert say(LINK_TARGET_WORDS, "ipam_subnet") == "la subred"


async def test_backup_a_recovery_key_that_does_not_exist(
    admin: tuple[Any, dict[str, str]],
) -> None:
    """backup/offsite_api.py: `no such recovery key`, a 404."""
    env, headers = admin
    await _both_languages(
        env,
        "POST",
        f"/api/v1/backup/recovery-keys/{uuid4()}/challenge",
        headers,
        status=404,
        english="no such recovery key",
        spanish="no existe esa clave de recuperación",
    )


async def test_backup_off_site_copies_are_refused_without_a_recovery_key_in_both_languages(
    admin: tuple[Any, dict[str, str]],
) -> None:
    """backup/offsite.py: the security condition survives translation whole -- the
    key is needed, it must be confirmed first, and without it the copy cannot be read
    if this server is lost."""
    from tests.test_backup_offsite_api import _TARGET

    env, headers = admin
    repo = await env.client.post("/api/v1/backup/repositories", json={"name": "p"}, headers=headers)
    target = await env.client.post("/api/v1/backup/offsite-targets", json=_TARGET, headers=headers)
    path = f"/api/v1/backup/repositories/{repo.json()['id']}/offsite"
    body = {"target_id": target.json()["id"]}
    english = (
        "Set up a recovery key and confirm it before turning on off-site copies. "
        "Without it, the off-site copy cannot be read if this server is lost."
    )
    assert await _detail(env, "PUT", path, headers, "en", body) == (409, english)
    status, spanish = await _detail(env, "PUT", path, headers, "es", body)
    assert status == 409 and spanish == CATALOG_ES[english]
    assert "clave de recuperación" in spanish and "confírmala" in spanish
    assert "si se pierde este servidor" in spanish
    # No credential value is ever in the answer.
    assert _TARGET["secret_access_key"] not in spanish


def test_backup_the_object_lock_refusal_keeps_every_condition_in_spanish() -> None:
    """backup/offsite.py LOCK_REQUIRED_MESSAGE: Object Lock, a bucket created with it,
    nobody (even whoever takes over the server) can delete or change a backup before its
    lock runs out, and a new bucket is the remedy."""
    from app.core.i18n import tr_marked
    from app.modules.backup.offsite import LOCK_REQUIRED_MESSAGE, OffsiteError

    with use_locale("es"):
        text = str(OffsiteError(tr_marked(LOCK_REQUIRED_MESSAGE)))
    for part in (
        "Object Lock",
        "creado con Object Lock",
        "ni siquiera quien tome el control de este servidor",
        "borrar ni modificar una copia de seguridad antes de que termine su bloqueo",
        "Crea un bucket nuevo",
    ):
        assert part in text
    assert str(OffsiteError(tr_marked(LOCK_REQUIRED_MESSAGE))) == LOCK_REQUIRED_MESSAGE


async def test_backup_a_failed_off_site_copy_is_stored_in_english_whatever_the_language(
    admin_engine: Any,
    app_engine: Any,
    vault: Any,  # noqa: F811
    aws: Any,  # noqa: F811
) -> None:
    """backup/offsite.py replicate_point (stored_in_english): the worker stores the
    refusal as `last_error`; a Spanish context must not leak into it. Fails without the
    decorator."""
    from sqlalchemy import select

    from app.core.tenancy import org_session
    from app.models import BackupPointCopy
    from app.modules.backup.offsite import LOCK_REQUIRED_MESSAGE
    from tests.backup_offsite_fixtures import (
        PLAIN_BUCKET,
        add_point,
        add_proven_key,
        add_target,
        seed_org,
    )
    from tests.test_backup_offsite import _replicate_all

    store, _s3 = aws
    org = await seed_org(admin_engine, app_engine, vault, "i18n-assets-lock")
    await add_proven_key(app_engine, org)
    await add_target(app_engine, vault, org, bucket=PLAIN_BUCKET)
    point_id, _ = await add_point(app_engine, store, org, {"/a": [b"x" * 10]})
    with use_locale("es"):
        assert await _replicate_all(app_engine, vault, store, org, datetime.now(UTC)) == [False]
    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        copy = (
            await session.execute(
                select(BackupPointCopy).where(BackupPointCopy.point_id == point_id)
            )
        ).scalar_one()
    assert copy.last_error == LOCK_REQUIRED_MESSAGE


async def test_backup_an_unusable_manifest_ref_is_stored_in_english_whatever_the_language(
    admin_engine: Any,
    app_engine: Any,
    object_store: Any,  # noqa: F811
) -> None:
    """backup/dispatch.py ingest_backup_result (stored_in_english): the run's `error`
    embeds the text of a translated ValueError from the object-store helper."""
    from sqlalchemy import select

    from app.core.tenancy import org_session
    from app.models import BackupRun
    from app.modules.backup.dispatch import ingest_backup_result
    from app.modules.backup.schema import BackupRunResult
    from tests.test_backup_result import _setup_job

    org_id, _repo_id, _device_id, job = await _setup_job(admin_engine)
    result = BackupRunResult(
        state="success",
        manifest_ref="manifests/../x",
        bytes_protected=1,
        bytes_stored=1,
        chunk_count=1,
        dedup_stats={},
    )
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        with use_locale("es"):
            await ingest_backup_result(session, lambda: object_store, job, result)
        run = (
            await session.execute(select(BackupRun).where(BackupRun.job_id == job.id))
        ).scalar_one()
    assert run.error == (
        "agent reported an unusable manifest_ref: "
        "object suffix must not contain a '.' or '..' segment"
    )


async def test_bcp_a_plan_that_does_not_exist(admin: tuple[Any, dict[str, str]]) -> None:
    """bcp/api.py: `no such bcp plan`, a 404."""
    env, headers = admin
    await _both_languages(
        env,
        "GET",
        f"/api/v1/bcp/plans/{uuid4()}",
        headers,
        status=404,
        english="no such bcp plan",
        spanish="no existe ese plan BCP",
    )


async def test_bcp_a_scope_that_points_nowhere_keeps_the_wire_kind_in_its_identifier(
    admin: tuple[Any, dict[str, str]],
) -> None:
    """bcp/service.py: `scope ref not found: group:<id>`. `kind:id` is the identifier
    the API accepts, so the kind stays the wire word in Spanish too."""
    env, headers = admin
    group_id = uuid4()
    body = {
        "name": "Plan",
        "rto_minutes": 60,
        "rpo_minutes": 15,
        "scope": [{"kind": "group", "id": str(group_id)}],
    }
    assert await _detail(env, "POST", "/api/v1/bcp/plans", headers, "en", body) == (
        400,
        f"scope ref not found: group:{group_id}",
    )
    assert await _detail(env, "POST", "/api/v1/bcp/plans", headers, "es", body) == (
        400,
        f"no se encontró la referencia del ámbito: group:{group_id}",
    )


async def test_deploy_a_deployment_that_does_not_exist(
    admin: tuple[Any, dict[str, str]],
) -> None:
    """deploy/api.py: `no such deployment`, a 404."""
    env, headers = admin
    await _both_languages(
        env,
        "GET",
        f"/api/v1/deploy/deployments/{uuid4()}",
        headers,
        status=404,
        english="no such deployment",
        spanish="no existe ese despliegue",
    )


async def test_deploy_a_gated_deployment_is_refused_in_spanish_and_audited_in_english(
    api_env: Any,
) -> None:
    """change_mgmt/gates.py GateRefusal through deploy/api.py `gate_said`."""
    from tests.test_change_gates import _actions, _fleet, _gate

    f = await _fleet(api_env)
    headers = await api_env.headers(f["org_id"], f["admin"])
    await _gate(api_env, f, f["servers"], applies_to_patches=False, applies_to_deployments=True)
    body = {
        "package_id": str(f["package"]),
        "action": "install",
        "target_type": "group",
        "target_id": str(f["servers"]),
    }
    english = (
        "Deployments to the Servers group need an approved change. Choose an approved "
        "change to link, or have one approved first."
    )
    spanish = (
        "Desplegar en el grupo Servers exige un cambio aprobado. Elige un cambio aprobado "
        "para vincular, o haz que se apruebe uno primero."
    )
    assert await _detail(
        api_env, "POST", "/api/v1/deploy/deployments", headers, "en", body
    ) == (409, english)
    assert await _detail(
        api_env, "POST", "/api/v1/deploy/deployments", headers, "es", body
    ) == (409, spanish)
    refused = await _actions(api_env, f["org_id"], "change.gate_refused")
    assert [e.meta["reason"] for e in refused] == [english, english]


def test_deploy_an_artifact_less_package_names_the_installer_as_it_is() -> None:
    """deploy/artifacts.py: installer types (msi, deb ...) are file formats the console
    has no Spanish for, so they stay as the wire spells them inside a Spanish sentence."""
    from app.modules.deploy.artifacts import artifact_url_for
    from app.modules.deploy.packages import DeployValidationError

    package = type(
        "P",
        (),
        {"id": uuid4(), "installer_type": "msi", "artifact_ref": None, "artifact_sha256": None},
    )()

    def refusal() -> str:
        with pytest.raises(DeployValidationError) as caught:
            artifact_url_for(None, org_id=uuid4(), package=package, expires_in=60)  # type: ignore[arg-type]
        return str(caught.value)

    assert refusal() == f"package {package.id} is 'msi' but has no uploaded artifact"
    with use_locale("es"):
        assert refusal() == f"el paquete {package.id} es 'msi' pero no tiene ningún archivo subido"


async def test_remote_a_session_that_does_not_exist(admin: tuple[Any, dict[str, str]]) -> None:
    """remote/api.py: `no such session`, a 404."""
    env, headers = admin
    await _both_languages(
        env,
        "GET",
        f"/api/v1/remote/sessions/{uuid4()}",
        headers,
        status=404,
        english="no such session",
        spanish="no existe esa sesión",
    )


async def test_remote_a_retired_protocol_is_refused_and_named_as_it_is(
    admin: tuple[Any, dict[str, str]],
) -> None:
    """remote/api.py: a protocol is a name (rdp, vnc, ssh), not a console word."""
    env, headers = admin
    body = {"device_id": str(uuid4()), "protocol": "rdp", "mode": "attended", "reason": "ticket 1"}
    assert await _detail(env, "POST", "/api/v1/remote/sessions", headers, "en", body) == (
        422,
        "rdp is retired; start a screen session instead",
    )
    assert await _detail(env, "POST", "/api/v1/remote/sessions", headers, "es", body) == (
        422,
        "rdp está retirado; inicia una sesión remota de pantalla en su lugar",
    )


def test_remote_a_refused_state_change_names_the_consoles_session_states() -> None:
    """remote/service.py via words.py: `ended` never goes anywhere, said with the console's
    words for the session state."""
    from app.modules.remote.service import InvalidSessionState, RemoteSessionService

    class Row:
        state = "ended"

    transition = RemoteSessionService._transition

    def refusal() -> str:
        with pytest.raises(InvalidSessionState) as caught:
            transition(object(), Row(), "active")  # type: ignore[arg-type]
        return str(caught.value)

    assert refusal() == "ended -> active"
    with use_locale("es"):
        assert refusal() == "finalizada -> activa"


async def test_site_server_a_site_server_that_does_not_exist(
    admin: tuple[Any, dict[str, str]],
) -> None:
    """site_server/registry.py: SiteServerNotFoundError, a 404 with the id in it."""
    env, headers = admin
    site_server_id = uuid4()
    await _both_languages(
        env,
        "GET",
        f"/api/v1/site-servers/{site_server_id}",
        headers,
        status=404,
        english=f"site server {site_server_id} not found",
        spanish=f"no se encontró el servidor de sitio {site_server_id}",
    )


def test_site_server_a_refused_role_keeps_its_reason_and_names_roles_as_the_console_does() -> None:
    """site_server/registry.py via words.say_list: the reason (a listening socket is
    forbidden by invariant #2, the amendment has not been accepted) survives translation
    whole, and roles read as the console's words in Spanish, as a list in English."""
    from app.modules.site_server.errors import SiteServerValidationError
    from app.modules.site_server.registry import _validated_roles

    def refusal() -> str:
        with pytest.raises(SiteServerValidationError) as caught:
            _validated_roles(["wake", "pxe", "cache"])
        return str(caught.value)

    english = refusal()
    assert english.startswith("roles ['cache', 'pxe'] are not available in v1 — only ['wake'] is")
    with use_locale("es"):
        spanish = refusal()
    assert spanish.startswith(
        "las funciones caché de contenido, arranque por PXE no están disponibles en la v1: "
        "solo se admite Wake-on-LAN."
    )
    for part in ("invariante n.º 2 de `CLAUDE.md`", "NO SE HA ACEPTADO", "ningún socket"):
        assert part in spanish


def test_site_server_a_bad_mac_is_quoted_back_in_both_languages() -> None:
    """site_server/net.py: the value the person typed is echoed as typed."""
    from app.modules.site_server.errors import SiteServerValidationError
    from app.modules.site_server.net import normalize_mac

    def refusal() -> str:
        with pytest.raises(SiteServerValidationError) as caught:
            normalize_mac("zz:11")
        return str(caught.value)

    assert refusal() == (
        "invalid MAC address 'zz:11': expected 12 hex digits, optionally separated by "
        "':', '-' or '.'"
    )
    with use_locale("es"):
        assert refusal() == (
            "dirección MAC no válida 'zz:11': se esperaban 12 dígitos hexadecimales, "
            "opcionalmente separados por ':', '-' o '.'"
        )


async def test_licenses_a_vendor_that_does_not_exist(admin: tuple[Any, dict[str, str]]) -> None:
    """licenses/api.py: `no such vendor`, a 404."""
    env, headers = admin
    await _both_languages(
        env,
        "GET",
        f"/api/v1/vendors/{uuid4()}",
        headers,
        status=404,
        english="no such vendor",
        spanish="no existe ese proveedor",
    )


def test_licenses_an_allocation_target_that_is_missing_is_named_in_the_consoles_word() -> None:
    """licenses/service.py via words.py: the kind of destination, and no key anywhere."""
    from app.core.wire_words import say
    from app.modules.licenses.words import ALLOCATION_TARGET_WORDS

    assert say(ALLOCATION_TARGET_WORDS, "person") == "person"
    with use_locale("es"):
        assert say(ALLOCATION_TARGET_WORDS, "person") == "persona"
        assert say(ALLOCATION_TARGET_WORDS, "device") == "dispositivo"


async def test_subscriptions_a_subscription_that_does_not_exist(
    admin: tuple[Any, dict[str, str]],
) -> None:
    """subscriptions/api.py: `no such subscription`, a 404."""
    env, headers = admin
    await _both_languages(
        env,
        "GET",
        f"/api/v1/subscriptions/{uuid4()}",
        headers,
        status=404,
        english="no such subscription",
        spanish="no existe esa suscripción",
    )


def test_change_mgmt_a_gate_refusal_copies_and_pickles_as_its_english_sentence() -> None:
    """`str` hands its text back to `__new__` on copy and pickle; a refusal must
    survive both, as the English sentence the audit chain stores."""
    import copy
    import pickle

    from app.core.i18n import tr
    from app.modules.change_mgmt.gates import GateRefusal

    english = "no such change"
    with use_locale("es"):
        refusal = GateRefusal(lambda: tr(english))
    assert str(refusal) == english
    assert copy.copy(refusal) == english
    assert copy.deepcopy(refusal) == english
    assert pickle.loads(pickle.dumps(refusal)) == english


def test_site_server_declared_segments_read_as_a_list_in_spanish() -> None:
    """dispatch.py: the declared segments print as the old list in English and as
    a plain comma-separated list in Spanish, never a Python list repr."""
    from app.modules.site_server.dispatch import _resolve_broadcast
    from app.modules.site_server.errors import SiteServerValidationError

    declared = ["10.20.0.0/16", "10.30.0.0/16"]
    for lang, shown in (("en", str(declared)), ("es", "10.20.0.0/16, 10.30.0.0/16")):
        with use_locale(lang), pytest.raises(SiteServerValidationError) as caught:
            _resolve_broadcast(declared, segment=None, target_mac="00:11:22:33:44:55")
        assert f"({shown})" in str(caught.value)
