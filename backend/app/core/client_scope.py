"""Which clients a database session may see (MSP clients spec §2.2, §2.3).

A `ClientScope` is "every client" or "exactly these ids". It travels from the
request (`AuthContext.clients`) to `org_session`, which pins it in two GUCs
that the restrictive `client_isolation` policies of migration 0231 read.

NARROWING NEVER WIDENS. `narrow()` intersects; asking a list scope for a client
outside it yields the EMPTY list, which RLS answers with zero rows -- never the
original scope (spec §2.2: the header is never ignored).
"""

from collections.abc import Iterable
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class ClientScope:
    is_all: bool
    ids: frozenset[UUID]

    @classmethod
    def all(cls) -> "ClientScope":
        return cls(is_all=True, ids=frozenset())

    @classmethod
    def only(cls, ids: Iterable[UUID]) -> "ClientScope":
        return cls(is_all=False, ids=frozenset(ids))

    def narrow(self, client_id: UUID | None) -> "ClientScope":
        if client_id is None:
            return self
        if self.is_all or client_id in self.ids:
            return ClientScope.only([client_id])
        return ClientScope.only([])

    def allows(self, client_id: UUID) -> bool:
        return self.is_all or client_id in self.ids

    def guc_values(self) -> tuple[str, str]:
        """(`app.client_scope`, `app.current_clients`) for `set_config`."""
        if self.is_all:
            return "all", ""
        return "list", ",".join(sorted(str(i) for i in self.ids))


def parse_client_header(value: str | None) -> UUID | None:
    """`X-Client-Id`: absent is None; anything but one UUID raises ValueError."""
    if value is None:
        return None
    return UUID(value.strip())  # raises ValueError on "", garbage, or a list


# Files under app/ allowed to call `ClientScope.all()` (spec §2.3). Every one is
# a process acting for the whole organization, or identity resolution that
# must read the user before their scope exists. `tests/test_client_catalog.py`
# fails on any other use. A new entry is a security decision: it says why.
ALL_SCOPE_ALLOWLIST: dict[str, str] = {
    # -- (c) identity resolution: the scope is computed from what these read --
    "app/api/deps.py": "resolves the user and their grants before the scope exists (spec §2.2)",
    "app/modules/portal/principal.py": "resolves the portal user before the scope exists",
    "app/api/v1/ws.py": (
        "admission door 2 (console WebSocket): reads the org's domain rule and the user's "
        "status before the scope exists, as deps.py does (spec §2.2, §2.6)"
    ),
    "app/modules/remote/tunnel.py": (
        "admission door 3 (remote tunnel): resolves the user before the scope exists and "
        "returns the provisional scope the tunnel's sessions run under (spec §2.2, §2.6)"
    ),
    # -- (a) processes with no person behind them --------------------------
    "app/worker.py": "Celery worker and beat sweeps, one org_session per org (spec §2.3)",
    "app/gateway/dispatch.py": "gateway: handles messages from agents, not from a person",
    "app/gateway/main.py": "gateway: agent connect, disconnect and job delivery",
    "app/modules/remote/screen_relay.py": (
        "gateway: the screen-share relay joining agent and viewer legs; the viewer was "
        "authorized by the API when its ticket was minted"
    ),
    "app/modules/backup/dedup.py": "gateway: answers an agent's chunk query",
    "app/modules/backup/read.py": "gateway: answers an agent's read query",
    "app/services/cert_renewal.py": "gateway: an agent's certificate renewal",
    "app/services/enrollment.py": (
        "agent enrollment: no person; the enrollment token or install-link code is the "
        "authority, and it must be found before its client is known"
    ),
    "app/services/install_link.py": (
        "resolves a public install-link code for an unenrolled machine: no person"
    ),
    "app/modules/agent_distribution/link_page.py": (
        "the install link's public page (no sign-in by design): reads only the release catalogue"
    ),
    "app/core/ai/retention.py": "worker: AI transcript retention sweep",
    "app/modules/agent_update/reconcile.py": "worker: agent-update rollout tick",
    "app/modules/email_intake/identity.py": (
        "worker: mints a ticket's reply token for deliver_pending (send_test's synthetic "
        "event is never a ticket, so no route reaches it)"
    ),
    "app/modules/email_intake/poller.py": (
        "worker: the scheduled mailbox poll; test_mailbox, the one route-reachable entry, "
        "takes the caller's scope"
    ),
    "app/modules/health/sweep.py": "worker: health sweeps, one org_session per org",
    "app/modules/ipam/reconcile.py": "worker: IPAM daily reconcile sweep",
    "app/modules/ipam/retention.py": "worker: IPAM retention sweep",
    "app/modules/keycloak_events/sweep.py": "worker: forwards Keycloak events into the audit chain",
    "app/modules/remote/recording.py": "worker: session-recording retention sweep",
    "app/modules/security_logs/retention.py": "worker: security-log retention sweep",
    "app/modules/sla/sweep.py": "worker: SLA breach and auto-close sweep",
    "app/core/client_scope.py": "defines it",
}
