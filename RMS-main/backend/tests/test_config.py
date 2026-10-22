from pathlib import Path

from app.core.config import Settings, get_settings, override_settings


def test_settings_load_from_env(monkeypatch) -> None:
    monkeypatch.setenv("ITS_DATABASE_URL", "postgresql+asyncpg://app_user:app@localhost/x")
    monkeypatch.setenv("ITS_DATABASE_ADMIN_URL", "postgresql+asyncpg://admin:a@localhost/x")
    monkeypatch.setenv("ITS_OIDC_ISSUER", "https://kc.example/realms/it-suite")
    monkeypatch.setenv("ITS_OIDC_JWKS_URL", "https://kc.example/realms/it-suite/certs")
    monkeypatch.setenv("ITS_CA_DIR", "/tmp/ca")
    s = Settings()
    assert s.database_url.endswith("/x")
    assert s.oidc_audience == "it-suite"  # default
    assert s.signing_backend == "local"
    assert s.ca_dir == Path("/tmp/ca")
    assert s.job_max_attempts == 3
    assert s.gateway_relay_url == "wss://localhost:8443/relay"  # default
    # guacd is retired: nothing reads its settings, and an old .env naming
    # them is ignored rather than fatal.
    assert not hasattr(s, "remote_guacd_host")


def test_override_settings_swaps_cached_instance(monkeypatch) -> None:
    monkeypatch.setenv("ITS_DATABASE_URL", "postgresql+asyncpg://app_user:app@localhost/x")
    monkeypatch.setenv("ITS_DATABASE_ADMIN_URL", "postgresql+asyncpg://admin:a@localhost/x")
    monkeypatch.setenv("ITS_OIDC_ISSUER", "https://kc.example/realms/it-suite")
    monkeypatch.setenv("ITS_OIDC_JWKS_URL", "https://kc.example/realms/it-suite/certs")
    monkeypatch.setenv("ITS_CA_DIR", "/tmp/ca")
    base = Settings()
    with override_settings(base.model_copy(update={"oidc_audience": "other"})):
        assert get_settings().oidc_audience == "other"
    assert get_settings().oidc_audience == "it-suite"
