"""Talking to Keycloak's admin API, and nothing else.

DELIBERATELY THIN. This module fetches a token and fetches events; the
judgement about which events matter is in `mapping.py` and the decisions about
orgs and duplicates are in `sweep.py`. That split is what lets the interesting
parts be tested without an HTTP server.

THE CREDENTIAL IS `view-events` ONLY, on a dedicated service-account client
(`deploy/README.md`, "Forwarding Keycloak's credential events"). It is not the
bootstrap admin, which can reset any password and create any user. If this
module ever needs a second Keycloak role to function, that is a change to what
the credential can do and it is the operator's decision, not a patch here.

EVERY CALL HAS A TIMEOUT. A Keycloak that is slow or wedged must not hold a
worker task open; the sweep reports the failure and the next run tries again.
Errors are raised rather than swallowed, because a forwarding feature that
fails quietly is the exact shape of an audit gap nobody notices.
"""

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import httpx

#: Seconds. Generous enough for a busy Keycloak, short enough that a wedged one
#: does not occupy a worker slot until the task's own timeout.
_TIMEOUT = 15.0


@dataclass(frozen=True)
class KeycloakEventsConfig:
    base_url: str
    realm: str
    client_id: str
    client_secret: str

    @classmethod
    def from_settings(cls, settings: Any) -> "KeycloakEventsConfig | None":
        """The config, or `None` when this deployment has not set it up.

        `None` IS A SUPPORTED ANSWER, not an error. An operator who has not
        configured event forwarding has not broken anything -- but the gap must
        be VISIBLE, which is the `keycloak_events` probe's job, not this
        function's.
        """
        url = getattr(settings, "keycloak_admin_url", None)
        secret_path = getattr(settings, "keycloak_events_secret_path", None)
        if not url or not secret_path:
            return None
        path = Path(secret_path)
        if not path.is_file():
            # Configured but the secret is missing: that IS worth failing on
            # rather than silently degrading to "not configured", because the
            # operator believes this is running.
            raise FileNotFoundError(  # i18n-exempt: probe reports a code, sweep logs it
                f"keycloak_events_secret_path points at {path}, which does not exist"
            )
        secret = path.read_text().strip()
        if not secret:
            raise ValueError(  # i18n-exempt: probe reports a code, sweep logs it
                f"{path} is empty"
            )
        return cls(
            base_url=url.rstrip("/"),
            realm=getattr(settings, "keycloak_realm", "it-suite"),
            client_id=getattr(settings, "keycloak_events_client_id", "it-suite-events"),
            client_secret=secret,
        )


async def fetch_events(
    config: KeycloakEventsConfig, *, since: date, limit: int = 500
) -> list[dict[str, Any]]:
    """User events from `since` (inclusive), newest first.

    `dateFrom` IS A DATE, NOT A TIMESTAMP -- Keycloak's events endpoint filters
    by day. That coarseness is why `sweep.py` deduplicates on a fingerprint
    rather than trusting a high-water mark: an hourly sweep re-reads the same
    day's events every time.

    `limit` bounds one fetch. A realm that generated more credential events in
    a day than this is either enormous or under attack, and in both cases a
    truncated fetch that the sweep REPORTS is better than an unbounded one that
    exhausts the worker.
    """
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        token_response = await client.post(
            f"{config.base_url}/realms/{config.realm}/protocol/openid-connect/token",
            data={
                "grant_type": "client_credentials",
                "client_id": config.client_id,
                "client_secret": config.client_secret,
            },
        )
        token_response.raise_for_status()
        token = token_response.json()["access_token"]

        response = await client.get(
            f"{config.base_url}/admin/realms/{config.realm}/events",
            params={"dateFrom": since.isoformat(), "max": limit},
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        events = response.json()
    return events if isinstance(events, list) else []
