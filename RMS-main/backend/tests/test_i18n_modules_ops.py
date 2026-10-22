"""Spanish server messages for ipam, health, agent_push, agent_distribution and
agent_update.

One representative failing route per module, called twice: with
`Accept-Language: es` the `detail` is the catalogue's Spanish, and with
`Accept-Language: en` the English is exactly what it was before the sweep.
The full catalogue is checked by tests/test_i18n_catalog.py; this proves a real
request reaches `tr()` with the request's language.
"""

import ast
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from app.core.i18n import CATALOG_ES, use_locale
from app.modules.agent_distribution import msi as msi_svc
from app.modules.agent_push import preflight as push_preflight
from app.services import msi_build
from tests.i18n_ops_support import (
    MSI_URL,
    PUSH,
    RELEASES,
    active_rollout_release,
    msi_world,
    push_world,
)


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
    org = await api_env.create_org("i18n-ops")
    sub = await api_env.create_user(org, "org-admin")
    return api_env, await api_env.headers(org, sub)


async def test_ipam_a_subnet_that_does_not_exist(admin: tuple[Any, dict[str, str]]) -> None:
    """ipam/subnets.py: IpamNotFoundError, mapped to 404 by ipam/api.py."""
    env, headers = admin
    subnet_id = uuid4()
    await _both_languages(
        env,
        "GET",
        f"/api/v1/ipam/subnets/{subnet_id}",
        headers,
        status=404,
        english=f"subnet {subnet_id} not found",
        spanish=f"no se encontró la subred {subnet_id}",
    )


def test_ipam_a_count_and_its_noun_agree_in_both_languages() -> None:
    """ipam/registry.py `counted`: English adds an "s", Spanish has its own plurals."""
    from app.modules.ipam.registry import counted

    assert counted(1, "drift finding") == "1 drift finding"
    assert counted(3, "scan authorization") == "3 scan authorizations"
    with use_locale("es"):
        assert counted(1, "drift finding") == "1 hallazgo de deriva"
        assert counted(3, "scan authorization") == "3 autorizaciones de escaneo"
        assert counted(2, "sweep run") == "2 ejecuciones de barrido"


async def test_health_a_profile_that_does_not_exist(admin: tuple[Any, dict[str, str]]) -> None:
    """health/profiles.py: HealthNotFoundError, mapped to 404 by health/api.py."""
    env, headers = admin
    profile_id = uuid4()
    await _both_languages(
        env,
        "GET",
        f"/api/v1/health/profiles/{profile_id}",
        headers,
        status=404,
        english=f"health profile {profile_id} not found",
        spanish=f"no se encontró el perfil de estado {profile_id}",
    )


async def test_agent_update_a_rollout_that_does_not_exist(
    admin: tuple[Any, dict[str, str]],
) -> None:
    """agent_update/service.py: RolloutNotFound, mapped to 404 by agent_update/api.py."""
    env, headers = admin
    await _both_languages(
        env,
        "GET",
        f"/api/v1/agent/update-rollouts/{uuid4()}",
        headers,
        status=404,
        english="no such rollout",
    )


async def test_agent_distribution_a_release_that_does_not_exist(
    admin: tuple[Any, dict[str, str]],
) -> None:
    """agent_distribution/api.py: the 404 for an unknown release id."""
    env, headers = admin
    await _both_languages(
        env,
        "POST",
        f"/api/v1/agent/releases/{uuid4()}/install-command",
        headers,
        status=404,
        english="no such agent release",
        json_body={},
    )


# --- the MSI builder: a stdlib-only module, worded by msi.py from its error codes ---


def _tool_missing() -> msi_build.MsiBuildError:
    return _raised(lambda: msi_build._tool("itms-no-such-tool"))


def _raised(call: Callable[[], object]) -> msi_build.MsiBuildError:
    with pytest.raises(msi_build.MsiBuildError) as caught:
        call()
    return caught.value


def _verify_with(
    monkeypatch: pytest.MonkeyPatch, *, template: str | None = None, **tables: list[list[str]]
) -> msi_build.MsiBuildError:
    """`verify_msi` against a fake msiinfo whose tables are the good ones, with
    `tables` replaced: each way the built file can differ from the source."""
    good: dict[str, list[list[str]]] = {
        "CustomAction": [list(row) for row in sorted(msi_build.EXPECTED_CUSTOM_ACTIONS)],
        "InstallExecuteSequence": _sequence({}),
        "File": [["a", "b", msi_build.AGENT_FILE], ["c", "d", msi_build.SETTINGS_FILE]],
        "ServiceControl": [["StopAgent", "its-agent", "34", "", "1", "Agent"]],
    }
    good.update(tables)
    monkeypatch.setattr(msi_build, "_tool", lambda name: name)
    monkeypatch.setattr(msi_build, "_table", lambda _info, _msi, table, _deadline: good[table])
    monkeypatch.setattr(msi_build, "_template", lambda *_: template or msi_build.TEMPLATES["amd64"])
    return _raised(lambda: msi_build.verify_msi(Path("agent.msi"), arch="amd64", deadline=1e12))


def _build_errors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[msi_build.MsiBuildError]:
    """One real error per code, raised by the builder's own code."""
    late = time.monotonic() - 1
    python = sys.executable
    found = [
        _raised(lambda: msi_build.product_version("nope")),
        _raised(lambda: msi_build.product_version("256.0.0")),
        _raised(lambda: msi_build.settings_json("http://itms.example.com", "a" * 32)),
        _raised(lambda: msi_build.settings_json("https://itms.example.com", "short")),
        _tool_missing(),
        _raised(lambda: msi_build._run(["x"], cwd=tmp_path, deadline=late)),
        _raised(
            lambda: msi_build._run(
                [python, "-c", "import time; time.sleep(30)"],
                cwd=tmp_path,
                deadline=time.monotonic() + 0.3,
            )
        ),
        _raised(
            lambda: msi_build._run(
                [python, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"],
                cwd=tmp_path,
                deadline=time.monotonic() + 30,
            )
        ),
    ]
    monkeypatch.setattr(msi_build, "_run", lambda *_a, **_k: "Subject: nothing")
    found.append(_raised(lambda: msi_build._template("msiinfo", tmp_path / "a.msi", 1e12)))
    monkeypatch.undo()
    found.append(_verify_with(monkeypatch, CustomAction=[]))
    found.append(
        _verify_with(
            monkeypatch,
            InstallExecuteSequence=[["ItsMsiInstall", "X", "1"], ["ItsMsiUninstall", "Y", "2"]],
        )
    )
    found.append(_verify_with(monkeypatch, InstallExecuteSequence=_without("InstallInitialize")))
    found.append(_verify_with(monkeypatch, InstallExecuteSequence=_moved("RemoveFiles", "100")))
    found.append(_verify_with(monkeypatch, File=[["a", "b", msi_build.AGENT_FILE]]))
    found.append(_verify_with(monkeypatch, ServiceControl=[]))
    found.append(_verify_with(monkeypatch, template="x86;1033"))
    build = {
        "exe": b"",
        "console": "https://itms.example.com",
        "code": "a" * 32,
        "org_id": uuid4(),
        "version": "1.7.0",
    }
    found.append(_raised(lambda: msi_build.build_agent_msi(arch="x86", **build)))  # type: ignore[arg-type]
    monkeypatch.setattr(msi_build, "_tool", lambda name: name)
    held = threading.BoundedSemaphore(1)
    held.acquire()
    monkeypatch.setattr(msi_build, "_BUILDS", held)
    found.append(
        _raised(lambda: msi_build.build_agent_msi(arch="amd64", timeout_s=0.05, **build))  # type: ignore[arg-type]
    )
    return found


def _sequence(replace: dict[str, str | None]) -> list[list[str]]:
    rows = [
        ["ItsMsiInstall", msi_build.INSTALL_CONDITION, str(msi_build.INSTALL_SEQUENCE)],
        ["ItsMsiUninstall", msi_build.UNINSTALL_CONDITION, str(msi_build.UNINSTALL_SEQUENCE)],
        ["InstallInitialize", "", "1500"],
        ["RemoveFiles", "", "3500"],
        ["InstallFiles", "", "4000"],
        ["InstallFinalize", "", "6600"],
    ]
    return [
        [row[0], row[1], replace.get(row[0]) or row[2]]
        for row in rows
        if replace.get(row[0], row[2]) is not None
    ]


def _without(name: str) -> list[list[str]]:
    return _sequence({name: None})


def _moved(name: str, number: str) -> list[list[str]]:
    return _sequence({name: number})


def _codes_in_the_builder() -> set[str]:
    """Every first argument of a `MsiBuildError(...)` call in msi_build.py."""
    tree = ast.parse(Path(msi_build.__file__).read_text())
    return {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "MsiBuildError"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    }


def test_every_builder_error_has_a_spanish_sentence_and_an_english_one_that_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """msi_build.py cannot import tr(); msi.py words each error by its code."""
    errors = _build_errors(monkeypatch, tmp_path)
    assert {error.code for error in errors} == _codes_in_the_builder()
    assert len(errors) == len(_codes_in_the_builder()) == 18
    for error in errors:
        # English: the template reproduces the builder's own sentence, byte for byte.
        assert msi_svc.build_error_text(error) == str(error), error.code
        with use_locale("es"):
            spanish = msi_svc.build_error_text(error)
        assert spanish != str(error), error.code
        assert "{" not in spanish and "}" not in spanish, error.code


def test_the_builder_errors_carry_plain_parameters_and_never_the_link_code() -> None:
    error = _raised(lambda: msi_build.settings_json("https://itms.example.com", "k" * 31))
    assert error.code == "code_malformed" and error.params == {}
    assert "k" * 31 not in str(error)
    error = _raised(lambda: msi_build.product_version("999.0.0"))
    assert error.params == {"version": "999.0.0"}


async def test_agent_distribution_the_msi_503_is_whole_in_spanish(
    api_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The builder's refusal reaches an administrator inside a 503. Under `es` the
    WHOLE detail is Spanish: msi.py's wrapper sentence and the builder's reason."""
    _org, code, _release, _store, admin = await msi_world(api_env, monkeypatch)
    monkeypatch.setattr(msi_build.shutil, "which", lambda _name: None)
    body = {"code": code, "arch": "amd64"}

    english = await api_env.client.post(
        MSI_URL, json=body, headers={**admin, "Accept-Language": "en"}
    )
    assert english.status_code == 503
    assert english.json()["detail"] == (
        "The MSI could not be built: wixl is not installed; building the MSI needs "
        "msitools (wixl, msibuild, msiinfo)"
    )

    spanish = await api_env.client.post(
        MSI_URL, json=body, headers={**admin, "Accept-Language": "es"}
    )
    assert spanish.status_code == 503
    assert spanish.headers["Content-Language"] == "es"
    assert spanish.json()["detail"] == (
        "No se pudo crear el MSI: wixl no está instalado; para crear el MSI hacen falta "
        "msitools (wixl, msibuild, msiinfo)"
    )
    assert code not in spanish.text


async def test_agent_distribution_the_build_thread_words_its_refusals_in_the_requests_language(
    api_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The build runs on its own pool thread, which starts with an empty context;
    without a copied context its tr() would answer in English to a Spanish request."""
    _org, code, release, _store, admin = await msi_world(api_env, monkeypatch)
    api_env.app.state.object_store.objects[(release.version, "windows", "amd64")] = b"tampered"
    spanish = await api_env.client.post(
        MSI_URL,
        json={"code": code, "arch": "amd64"},
        headers={**admin, "Accept-Language": "es"},
    )
    assert spanish.status_code == 409
    assert spanish.json()["detail"].startswith("El agente 1.7.0 almacenado para Windows")
    assert "SHA-256" in spanish.json()["detail"]

    # And the language does not stick to the pool thread: the next request, in
    # English, on the same thread, is worded in English.
    english = await api_env.client.post(
        MSI_URL,
        json={"code": code, "arch": "amd64"},
        headers={**admin, "Accept-Language": "en"},
    )
    assert english.status_code == 409
    assert english.json()["detail"].startswith("The stored agent 1.7.0 for Windows")
    again = await api_env.client.post(
        MSI_URL,
        json={"code": code, "arch": "amd64"},
        headers={**admin, "Accept-Language": "es"},
    )
    assert again.json()["detail"].startswith("El agente 1.7.0 almacenado para Windows")


def test_agent_distribution_the_install_page_is_worded_in_the_requests_language() -> None:
    """agent_distribution/link_page.py: the public page a new computer's user reads."""
    from app.modules.agent_distribution.link_page import render_page

    def page() -> str:
        return render_page(
            console="https://itms.example.com",
            code="c" * 32,
            releases=RELEASES,
            windows_first=True,
            arch="arm64",
            nonce="n",
        )

    english = page()
    assert 'lang="en"' in english and "<h1>Install the ITMS agent</h1>" in english
    assert "Download for Windows (ARM)" in english
    assert "Download for Windows (64-bit Intel or AMD) instead" in english
    with use_locale("es"):
        spanish = page()
    assert 'lang="es"' in spanish and "<h1>Instalar el agente de ITMS</h1>" in spanish
    assert "Descargar para Windows (ARM)" in spanish
    assert "Descargar para Windows (Intel o AMD de 64 bits) en su lugar" in spanish
    assert "macOS y Linux" in spanish
    assert "Download" not in spanish and "Install the" not in spanish


async def test_agent_push_a_run_that_does_not_exist(admin: tuple[Any, dict[str, str]]) -> None:
    """agent_push/runs.py: PushNotFoundError, mapped to 404 by agent_push/api.py."""
    env, headers = admin
    run_id = uuid4()
    await _both_languages(
        env,
        "GET",
        f"{PUSH}/runs/{run_id}",
        headers,
        status=404,
        english=f"push run {run_id} not found",
        spanish=f"no se encontró la ejecución de instalación remota {run_id}",
    )


async def test_agent_push_a_refused_push_keeps_its_codes_and_translates_its_words(
    api_env: Any,
) -> None:
    """Invariant 2a's refusal, through the route: the structured 422 names the
    failing address and its machine code (which the console maps to its own
    words) in BOTH languages, and the sentences around them are the request's
    language -- the message and each code's remedy in `reason_help`."""
    _org, headers, credential_id = await push_world(api_env, "i18n-push")
    body = {
        "mode": "probe",
        "credential_id": str(credential_id),
        "targets": ["10.90.0.17", "10.90.0.99"],
    }
    details = {}
    for lang in ("en", "es"):
        response = await api_env.client.post(
            f"{PUSH}/requests", json=body, headers={**headers, "Accept-Language": lang}
        )
        assert response.status_code == 422
        assert response.headers["Content-Language"] == lang
        details[lang] = response.json()["detail"]
    english, spanish = details["en"], details["es"]
    # What is refused and why (the codes) does not depend on the language.
    assert english["targets"] == spanish["targets"]
    assert spanish["targets"] == [{"address": "10.90.0.99", "reasons": ["no_asset"]}]
    assert set(english["reason_help"]) == set(spanish["reason_help"])
    # The words do.
    no_asset = push_preflight.REFUSAL_REASONS["no_asset"]
    assert english["reason_help"]["no_asset"] == no_asset
    assert spanish["reason_help"]["no_asset"] == CATALOG_ES[no_asset] != no_asset
    assert english["message"].startswith("this push was refused and nothing was created")
    assert spanish["message"].startswith("esta instalación remota se rechazó y no se creó nada")
    # Every remedy keeps its permission codes: translated, not softened.
    assert "assets.manage" in spanish["reason_help"]["no_asset"]
    assert "agent.push.scope.manage" in spanish["reason_help"]["no_authorization"]
    assert "site.manage" in spanish["reason_help"]["no_site_server"]


def test_agent_push_the_spanish_keeps_every_code_number_and_clause_of_the_english() -> None:
    """Invariant 2a's refusals guard a security path. A translation may reword,
    never drop a permission code, a number, a clause letter or a spec reference:
    every such token of the English is still there in the Spanish."""
    import re

    from tests.test_i18n_catalog import _rel, _tr_literals

    token = re.compile(r"[a-z_]+(?:\.[a-z_]+)+|§?\d+(?:\.\d+)*[a-z]?|\([a-z]\)|ITS_[A-Z_]+")
    checked = 0
    for path, _line, english in _tr_literals():
        if not _rel(path).startswith("modules/agent_push/"):
            continue
        spanish = CATALOG_ES[english]
        # "your.domain" is a placeholder in an example, and the Spanish names its own.
        missing = [t for t in token.findall(english) if t not in spanish and t != "your.domain"]
        assert missing == [], (english[:60], missing)
        checked += 1
    assert checked > 60


async def test_agent_distribution_an_unpublish_refused_by_a_rollout_is_worded_in_spanish(
    api_env: Any,
) -> None:
    """agent_distribution/api.py `_IN_USE`: a module constant returned as the 409
    detail, which the console shows raw."""
    super_admin, release_id = await active_rollout_release(api_env)
    await _both_languages(
        api_env,
        "DELETE",
        f"/api/v1/agent/releases/{release_id}",
        super_admin,
        status=409,
        english=(
            "an active agent update rollout still uses this release; complete or cancel it first"
        ),
    )


def test_agent_distribution_the_deployment_guide_is_worded_in_spanish() -> None:
    """agent_distribution/managed.py: step titles and details, the integrity notes
    (N_ tuples) and the blast radius. English is what it always was."""
    from app.modules.agent_distribution import managed

    def package(channel: str) -> managed.DeploymentPackage:
        return managed.deployment_package(
            channel=channel,
            os_name="windows",
            arch="amd64",
            sha256="a" * 64,
            download_url="https://x.example/f",
        )

    english = package("gpo")
    assert english.staging[2].title == "Stage it on a distribution point"
    assert english.integrity.notes[1].startswith("The endpoint does not verify anything.")
    with use_locale("es"):
        spanish = package("gpo")
        radius = managed.blast_radius(channel="intune", max_uses=5, hours=12)
    assert spanish.staging[2].title == "Colócalo en un punto de distribución"
    assert spanish.integrity.notes[1].startswith("El equipo no verifica nada.")
    # The commands are code, not prose: identical in both languages.
    assert [step.command for step in spanish.staging] == [s.command for s in english.staging]
    assert spanish.install_command == english.install_command
    assert "5 equipos" in radius[1] and "12 horas" in radius[1]
    assert not any("A holder can enrol" in line for line in radius)


async def test_health_check_types_describe_their_selector_in_the_requests_language(
    admin: tuple[Any, dict[str, str]],
) -> None:
    """health/registry.py `selector_meaning` is an N_() table the profile editor
    renders raw: translated where CheckTypeOut is built."""
    env, headers = admin

    async def meaning(lang: str) -> dict[str, str]:
        response = await env.client.get(
            "/api/v1/health/check-types", headers={**headers, "Accept-Language": lang}
        )
        assert response.status_code == 200
        return {row["name"]: row["selector_meaning"] for row in response.json()}

    english, spanish = await meaning("en"), await meaning("es")
    assert english["disk.free_pct"] == "mount/volume, '*' = worst"
    assert spanish["disk.free_pct"] == "punto de montaje o volumen; '*' = el peor"
    assert spanish["service.state"] == "nombre del servicio"
    assert spanish["script.policy_failed"] == (
        "nombre de la programación; vacío = cualquier programación"
    )
    assert english["cpu.busy_pct"] == spanish["cpu.busy_pct"] == ""


def test_ipam_counts_are_correct_english_and_correct_spanish() -> None:
    """The old code built the plural by appending "s" to the noun and wrote
    "2 addresss"; `counted` spells it "2 addresses" (an accepted correction)."""
    from app.modules.ipam.registry import counted

    assert counted(2, "address") == "2 addresses"
    assert counted(1, "address") == "1 address"
    assert counted(2, "drift finding") == "2 drift findings"
    with use_locale("es"):
        assert counted(2, "address") == "2 direcciones"
        assert counted(1, "address") == "1 dirección"


class _Rows:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _Session:
    """Answers every query with one recent job, so the cooldown fuse trips."""

    async def execute(self, _statement: Any) -> _Rows:
        from datetime import UTC, datetime

        return _Rows([(uuid4(), datetime.now(UTC))])


async def test_health_a_stored_refusal_stays_english_while_the_answer_takes_the_language() -> None:
    """`_refuse_reason` and `_fuse_breach` feed `last_error`, a database value;
    `_refusal` and `_fuse_breach_worded` answer a person."""
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from app.modules.health import actions

    inactive = SimpleNamespace(active=False, key="restart-spooler")
    fused = SimpleNamespace(
        cooldown_s=3600,
        job_type="service.restart",
        max_per_device_per_day=3,
        max_per_org_per_hour=20,
    )
    session: Any = _Session()
    org, device, now = uuid4(), uuid4(), datetime.now(UTC)
    with use_locale("es"):
        assert await actions._refuse_reason(session, org, inactive) == (
            "approved action 'restart-spooler' is inactive"
        )
        assert await actions._refusal(session, org, inactive) == (
            "la acción aprobada 'restart-spooler' está inactiva"
        )
        stored = await actions._fuse_breach(session, org, fused, device_id=device, now=now)
        worded = await actions._fuse_breach_worded(session, org, fused, device_id=device, now=now)
    assert stored is not None and stored.startswith("cooldown: service.restart last ran")
    assert worded is not None and worded.startswith("cooldown: service.restart se ejecutó")
