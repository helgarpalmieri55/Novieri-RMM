"""Application settings. All configuration comes from the environment (ITS_ prefix)."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ITS_", env_file=".env", extra="ignore")

    # Deployment mode. In "production" the platform fails closed on missing key
    # material and other silent-fallback conveniences that are safe only in dev.
    environment: Literal["dev", "test", "production"] = "dev"

    database_url: str
    database_admin_url: str
    # The row-scoped role used ONLY by portal_session (app/core/tenancy.py).
    # Defaults to "" (rather than being required like database_admin_url) so
    # that the many pre-existing tests which build Settings directly for
    # unrelated modules -- never touching the portal -- don't all need this
    # value added. A real deployment sets ITS_DATABASE_PORTAL_URL; anything
    # that actually calls get_portal_engine() with this unset fails loudly
    # (asyncpg rejects an empty DSN) rather than connecting with a guessable
    # default.
    database_portal_url: str = ""
    redis_url: str = "redis://localhost:6379/0"

    oidc_issuer: str
    oidc_audience: str = "it-suite"
    oidc_jwks_url: str
    oidc_org_claim: str = "org"
    # The OIDC client the console's own SPA signs in with: every non-MCP door
    # admits only a token whose `azp` is this client
    # (`app.core.security.require_console_token`), and `/mcp` refuses one that
    # is. Optional: the default is the frontend's own default client id
    # (`VITE_OIDC_CLIENT_ID ?? "it-suite-web"` in frontend/src/lib/auth.ts),
    # and nothing under deploy/ overrides either, so no `.env` change is needed.
    # Set ITS_OIDC_CONSOLE_CLIENT_ID only alongside a changed VITE_OIDC_CLIENT_ID.
    oidc_console_client_id: str = "it-suite-web"
    default_org_slug: str | None = None  # single-org on-prem fallback when the claim is absent
    # When true (multi-tenant deployments), a token missing the org claim is
    # rejected rather than falling back to default_org_slug.
    multi_tenant: bool = False

    signing_backend: Literal["local", "openbao"] = "local"
    signing_key_path: Path | None = None
    ca_backend: Literal["local", "openbao"] = "local"
    ca_dir: Path
    # HMAC key that authenticates audit events. The DB role cannot read it, so a
    # SQL-write primitive cannot forge a valid-looking event. Absent in dev.
    audit_hmac_key_path: Path | None = None
    vault_backend: Literal["local", "openbao"] = "local"
    vault_root_key_path: Path | None = None
    openbao_addr: str | None = None
    openbao_token: str | None = None

    # Where the host's backup scripts leave their results, mounted read-only
    # (deploy/docker-compose.yml). Absent means "not set up", not a failure.
    backup_status_dir: Path = Path("/var/lib/its/backup-status")

    # KEYCLOAK EVENT FORWARDING (docs/modules/console-auth/spec.md §7.1).
    #
    # Passkey registration, credential removal and identity-provider linking
    # live only in Keycloak's own event log, which is not tamper-evident and is
    # not in the hash chain. These four settings let a worker sweep copy them
    # into it.
    #
    # ABSENT BY DEFAULT, AND THAT IS A SUPPORTED STATE: an operator who has not
    # set this up has not broken anything. The sweep no-ops and the
    # `keycloak_events` probe reports `configured: false`, so the gap is
    # VISIBLE -- an audit feature that silently does nothing is worse than one
    # that does not exist, because somebody reads the chain and believes it is
    # complete.
    #
    # THE CREDENTIAL IS `view-events` ONLY, on a dedicated service-account
    # client. Not the bootstrap admin: that account can reset any password and
    # create any user, and GO-LIVE tells the operator to delete it. The secret
    # is a FILE PATH, following `audit_hmac_key_path` -- a secret in an
    # environment variable is readable from `/proc` and lands in every
    # `docker inspect`.
    keycloak_admin_url: str | None = None
    keycloak_realm: str = "it-suite"
    keycloak_events_client_id: str = "it-suite-events"
    keycloak_events_secret_path: Path | None = None

    # Provisioning: a SEPARATE service-account client from `it-suite-events`,
    # holding `manage-users`. Separate because the two credentials have wildly
    # different worst cases -- one reads an event log, the other can create any
    # user and reset any password in the realm -- and a single client holding
    # both means the smaller job carries the bigger blast radius forever.
    keycloak_admin_client_id: str = "it-suite-provisioning"
    keycloak_admin_secret_path: Path | None = None

    # The console email-domain allowlist. An address is admitted iff its domain
    # equals this or ends with "." + this.
    #
    # `None` IS LOAD-BEARING, not laziness. It means "no restriction", which is
    # today's behaviour; ANY other default would refuse every token on every
    # existing deployment the moment this ships, and would red the whole backend
    # suite besides, since conftest constructs Settings without the field. A
    # security control that arrives switched on by surprise is an outage.
    #
    # One base domain rather than a list: the operator declined a list. In
    # config rather than in source because a customer's domain compiled into a
    # platform meant to be sold is a bug for the second customer.
    console_email_domain: str | None = None

    # TWO ENDPOINTS FOR ONE BUCKET, AND THEY ARE NOT INTERCHANGEABLE.
    #
    # `s3_endpoint` is where the api/gateway/worker PUT and GET objects. In the
    # bundled compose stack that is `http://minio:9000` -- a Docker-internal
    # name, deliberately, so bulk object traffic never leaves the container
    # network.
    #
    # `s3_public_endpoint` is where an ENDPOINT reaches the same bucket, and it
    # is used for exactly one thing: computing the signature and host of a
    # presigned URL (`app.core.storage.resolve_presign_endpoint`). SigV4 signs
    # the host and the full path, so this cannot be patched up after the fact --
    # the URL an agent receives has to have been signed against the name the
    # agent will resolve. Signing against `s3_endpoint` is what made every
    # `app.deploy` artifact download unreachable: the URL named `minio`, which
    # exists in no DNS an endpoint can see.
    #
    # Unset in production this is a hard error rather than a fallback to
    # `s3_endpoint`, because the fallback's failure mode is a job that dispatches
    # cleanly and then fails on the endpoint with a DNS error nobody can
    # diagnose from the control plane. See `resolve_presign_endpoint`.
    s3_endpoint: str | None = None
    s3_public_endpoint: str | None = None
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    s3_bucket: str = "it-suite"

    # The https address people open the console at, e.g. https://itms.toeshee.com.
    # Optional: Microsoft sign-in for email intake needs it (as the OAuth
    # redirect URI) and so does the MCP server (the resource is this + "/mcp",
    # `app/core/mcp_channel.py::mcp_resource`); their routes answer 503 without
    # it. A setting, never derived from the request's Host header (host-header
    # injection).
    public_console_url: str | None = None
    # The PUBLIC half of the offline release-signing key, base64 Ed25519: the
    # `release-signing-key.pub` asset of an agent release (release-agent.yml).
    # Not a secret. The Group Policy MSI verifies the agent it wraps against it
    # (GPO MSI spec 2026-09-30 §2.2); unset, the MSI download answers 503 --
    # it never wraps bytes it could not verify.
    agent_release_public_key: str | None = None
    # The ONLY peers whose X-Forwarded-For header this API believes
    # (install-link spec 2026-09-28 §1.1). Comma-separated network prefixes.
    # The default is the compose network Traefik sits on (ITMS_NETWORK_SUBNET
    # in deploy/.env.example); from any other peer the header is ignored and
    # the TCP peer is the source address. Parsed and validated at startup by
    # app/core/client_address.py so a typo fails the process, not a request.
    trusted_proxy_cidrs: str = "172.19.240.0/24"

    gateway_url: str = "wss://localhost:8443/agent"
    # Relay endpoint the agent dials for remote-control media tunnels. Handed to
    # the agent via the signed remote.session.open job (never the raw token).
    gateway_relay_url: str = "wss://localhost:8443/relay"
    gateway_heartbeat_timeout_s: int = 180
    job_default_ttl_s: int = 3600
    job_max_attempts: int = 3

    # Screen share (spec-screen-share.md). The api's viewer leg dials the
    # gateway's internal relay listener; it is on the compose network only and
    # never published.
    relay_internal_url: str = "ws://localhost:8444/relay-internal"
    remote_screen_max_duration_s: int = 8 * 3600
    # Where the api spools a recording before the multipart upload. None is
    # the process temp directory.
    remote_recording_spool_dir: Path | None = None

    # Days ahead the daily subscription sweep looks for notice deadlines and
    # renewals to announce (app/modules/subscriptions/alerts.py).
    subscription_alert_window_days: int = Field(default=30, ge=1, le=365)

    # Ticketing for change management. "service_desk" opens a Service Desk
    # ticket for each submitted change and links it; "null" is the opt-out
    # (no ticket, no writes to change_external_ref).
    change_ticket_backend: str = "service_desk"


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


@contextmanager
def override_settings(settings: Settings) -> Iterator[Settings]:
    """Swap the cached settings instance (tests only)."""
    global _settings
    previous = _settings
    _settings = settings
    try:
        yield settings
    finally:
        _settings = previous
