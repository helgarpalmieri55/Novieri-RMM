from pathlib import Path

import pytest
from pydantic import ValidationError
from pydantic_settings import SettingsConfigDict

from app.core.config import Settings, get_settings, override_settings

#: Every `.env` key own authentication phase 4 made obsolete, with a value a
#: deployment upgraded from the Keycloak era could hold (deploy/.env.example
#: before 2026-11-15). The api must start on such a `.env` as it is.
KEYCLOAK_ERA_ENV = {
    "ITS_OIDC_ISSUER": "https://itms.example.com/realms/it-suite",
    "ITS_OIDC_JWKS_URL": "http://keycloak:8080/realms/it-suite/protocol/openid-connect/certs",
    "ITS_OIDC_AUDIENCE": "it-suite",
    "ITS_OIDC_ORG_CLAIM": "org",
    "ITS_OIDC_CONSOLE_CLIENT_ID": "it-suite-web",
    "ITS_MULTI_TENANT": "true",
    "ITS_KEYCLOAK_ADMIN_URL": "http://keycloak:8080",
    "ITS_KEYCLOAK_REALM": "it-suite",
    "ITS_KEYCLOAK_EVENTS_CLIENT_ID": "it-suite-events",
    "ITS_KEYCLOAK_EVENTS_SECRET_PATH": "/var/lib/its/secrets/keycloak-events.secret",
    "ITS_KEYCLOAK_ADMIN_CLIENT_ID": "it-suite-provisioning",
    "ITS_KEYCLOAK_ADMIN_SECRET_PATH": "/var/lib/its/secrets/keycloak-admin.secret",
    "KEYCLOAK_ADMIN_USER": "admin",
    "KEYCLOAK_ADMIN_PASSWORD": "not-a-real-password",
    "ENTRA_TENANT_ID": "",
    "ENTRA_CLIENT_ID": "",
    "ENTRA_CLIENT_SECRET": "",
}


def _required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ITS_DATABASE_URL", "postgresql+asyncpg://app_user:app@localhost/x")
    monkeypatch.setenv("ITS_DATABASE_ADMIN_URL", "postgresql+asyncpg://admin:a@localhost/x")
    monkeypatch.setenv("ITS_CA_DIR", "/tmp/ca")


def test_settings_load_from_env(monkeypatch) -> None:
    _required(monkeypatch)
    s = Settings()
    assert s.database_url.endswith("/x")
    assert s.signing_backend == "local"
    assert s.ca_dir == Path("/tmp/ca")
    assert s.job_max_attempts == 3
    assert s.gateway_relay_url == "wss://localhost:8443/relay"  # default
    assert s.default_org_slug is None  # default
    # guacd is retired: nothing reads its settings, and an old .env naming
    # them is ignored rather than fatal.
    assert not hasattr(s, "remote_guacd_host")


def test_a_keycloak_era_env_is_read_and_ignored(monkeypatch) -> None:
    """Own authentication phase 4 removed every Keycloak and OIDC setting. A
    `.env` that still holds them -- every deployment upgraded from before --
    must start exactly as one without them. Compose hands `.env` to the
    containers as environment variables, and pydantic-settings reads one only
    for a field that exists; what this pins is that no such field is left."""
    _required(monkeypatch)
    for key, value in KEYCLOAK_ERA_ENV.items():
        monkeypatch.setenv(key, value)
    s = Settings()
    fields = set(Settings.model_fields)
    assert {f for f in fields if f.startswith("oidc_")} == set()
    assert "multi_tenant" not in fields
    assert not hasattr(s, "oidc_issuer")
    assert s.default_org_slug is None


def test_a_keycloak_era_dotenv_file_is_read_and_ignored(monkeypatch, tmp_path) -> None:
    """The same keys from a dotenv file, which is where `extra` matters: with
    `extra="forbid"` this construction raises, and `extra="ignore"` is what
    keeps an upgraded `.env` read this way from stopping the process.

    The falsifier is the second half: the same file, read by a `Settings`
    whose only difference is `extra="forbid"`, raises, and raises only for
    the extra keys -- so the file does carry keys no field reads, and it is
    `extra="ignore"` that lets the first construction through."""
    _required(monkeypatch)
    dotenv = tmp_path / "keycloak-era.env"
    dotenv.write_text("".join(f"{key}={value}\n" for key, value in KEYCLOAK_ERA_ENV.items()))
    s = Settings(_env_file=dotenv)
    assert s.default_org_slug is None
    assert not hasattr(s, "oidc_issuer")

    class ForbiddingSettings(Settings):
        model_config = SettingsConfigDict(extra="forbid")

    assert ForbiddingSettings.model_config["env_prefix"] == "ITS_"  # inherited
    with pytest.raises(ValidationError) as refused:
        ForbiddingSettings(_env_file=dotenv)
    assert {error["type"] for error in refused.value.errors()} == {"extra_forbidden"}


def test_override_settings_swaps_cached_instance(monkeypatch) -> None:
    _required(monkeypatch)
    base = Settings()
    with override_settings(base.model_copy(update={"default_org_slug": "other"})):
        assert get_settings().default_org_slug == "other"
    assert get_settings().default_org_slug is None
