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

    def intersect(self, other: "ClientScope") -> "ClientScope":
        """The clients both scopes allow: never wider than either. How a door
        combines the person's (already narrowed) scope with the clients where
        they hold one particular code."""
        if self.is_all:
            return other
        if other.is_all:
            return self
        return ClientScope.only(self.ids & other.ids)

    @property
    def is_empty(self) -> bool:
        """No client at all: RLS answers such a session with zero rows."""
        return not self.is_all and not self.ids

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


def _reviewed(path: str, reason: str, *functions: str) -> dict[str, str]:
    return {f"{path}::{function}": reason for function in functions}


_WORKER = "worker/beat: a sweep with no person behind it, one org_session per org"
_GATEWAY = "gateway: acts on an agent's message or connection, not a person's request"
_RELAY = (
    "gateway: the screen-share relay joining agent and viewer legs; the viewer was "
    "admitted by the API when its ticket was minted"
)

# The FUNCTIONS under app/ allowed to call `ClientScope.all()` (spec §2.3), keyed
# `"<path>::<qualified name>"`: the path from backend/, then the dotted names of
# the classes and functions enclosing the call (`"<module>"` at module level).
# Every one is a process acting for the whole organization, or identity
# resolution that must read the person before their scope exists. Function
# granularity is the point: a route-reachable function in a file that also
# holds a sweep (`email_intake/poller.py::test_mailbox`) is NOT covered by its
# neighbours' entries. `tests/test_client_catalog.py` fails on any other use,
# and on an entry that no longer uses it. A new entry is a security decision:
# it says why.
ALL_SCOPE_ALLOWLIST: dict[str, str] = {
    # -- (c) identity resolution: the scope is computed from what these read --
    "app/api/deps.py::admit": (
        "door 1 (HTTP, MCP and the portal's sign-in): resolves the user and their grants "
        "before the scope exists (spec §2.2)"
    ),
    "app/core/identity/door.py::read_door_identity": (
        "doors 2 and 3 (console WebSocket, remote tunnel): the domain rule, users.status and "
        "users.kind, and the grants the scope is computed from (spec §2.2, §2.6)"
    ),
    "app/core/identity/door.py::person_scope": (
        "an admitted person's grants for one code (require_permission, the portal's gate, "
        "MCP): read before the scope they define exists, and only ever intersected with "
        "the person's own scope (spec §2.2)"
    ),
    "app/core/rbac.py::clients_holding": (
        "COMPUTES a person's scope rather than using one: an all-clients grant "
        "(user_roles.client_id NULL) is the every-client scope (spec §2.1-2.2)"
    ),
    # -- (a) processes with no person behind them --------------------------
    **_reviewed(
        "app/worker.py",
        _WORKER,
        "sweep_org",
        "drain_outbox",
        "deliver_pending",
        "ai_action_expiry_all_orgs",
        "patch_scan_sweep_org",
        "inventory_collect_sweep_org",
        "logs_ship_sweep_org",
        "ipam_sweep_dispatch_org",
        "patch_apply_sweep_all_orgs",
        "patch_reboot_sweep_all_orgs",
        "script_policy_sweep_all_orgs",
        "deploy_window_sweep_all_orgs",
        "backup_window_sweep_all_orgs",
        "backup_reconcile_all_orgs",
        "backup_replication_all_orgs",
        "backup_offsite_test_all_orgs",
        "backup_offsite_gc_all_orgs",
        "backup_test_restore_all_orgs",
        "backup_health_all_orgs",
        "backup_retention_gc_all_orgs",
        "backup_verification_sweep_all_orgs",
        "provision_enroll_dispatch_sweep_all_orgs",
        "provision_timeout_sweep_all_orgs",
        "licenses_compliance_sweep_all_orgs",
        "push_run_tick_all_orgs",
        "device_group_reevaluate_all_orgs",
        "platform_health_notify_all_orgs",
        "subscription_alerts_all_orgs",
        "install_link_auto_off_all_orgs",
        "review_rejections_all_orgs",
    ),
    **_reviewed(
        "app/gateway/dispatch.py",
        _GATEWAY,
        "_handle_security_events",
        "_handle_health_report",
        "_touch_agent",
        "handle_agent_message",
    ),
    **_reviewed(
        "app/gateway/main.py",
        _GATEWAY,
        "GatewayServer._queue_first_inventory",
        "GatewayServer._on_delivered",
        "GatewayServer._drain_queued",
        "GatewayServer._mark_online",
        "GatewayServer._mark_offline",
    ),
    **_reviewed(
        "app/modules/remote/screen_relay.py",
        _RELAY,
        "ScreenRelayHub._audit",
        "ScreenRelayHub.handle_agent",
        "ScreenRelayHub._session_state",
        "ScreenRelayHub._on_agent_message",
        "ScreenRelayHub._session_event",
        "ScreenRelayHub._on_state",
        "ScreenRelayHub._expire_unconnected",
        "ScreenRelayHub._on_viewer_message",
        "ScreenRelayHub._teardown",
    ),
    "app/modules/remote/screen_tunnel.py::_finish": (
        "system act: stores the recording (evidence) and ends the session (cleanup); "
        "neither may fail because the viewer's grants changed mid-session"
    ),
    "app/modules/backup/dedup.py::handle_chunk_query": "gateway: answers an agent's chunk query",
    "app/modules/backup/read.py::handle_read_query": "gateway: answers an agent's read query",
    "app/services/cert_renewal.py::advance_for_device": "gateway: an agent's certificate renewal",
    **_reviewed(
        "app/modules/agent_distribution/link_page.py",
        "the install link's public page (no sign-in by design): reads only the release "
        "catalogue",
        "_releases_for",
        "install_link_windows_signature",
    ),
    "app/core/ai/retention.py::sweep_all_orgs": "worker: AI transcript retention sweep",
    "app/modules/agent_update/reconcile.py::tick_all_orgs": "worker: agent-update rollout tick",
    "app/modules/email_intake/identity.py::_mint_email_token_committed": (
        "worker: mints a ticket's reply token for deliver_pending (send_test's synthetic "
        "event is never a ticket, so no route reaches it)"
    ),
    **_reviewed(
        "app/modules/email_intake/poller.py",
        "worker: the scheduled mailbox poll (test_mailbox, the route-reachable entry, "
        "takes the caller's scope and is not listed)",
        "_claim",
        "_mark_needs_reconsent",
        "_release",
        "_process_one",
        "poll_mailbox",
        "poll_all_mailboxes",
    ),
    "app/modules/health/sweep.py::_for_each_org": "worker: health sweeps, one org_session per org",
    "app/modules/ipam/reconcile.py::reconcile_sweep_all_orgs": "worker: IPAM daily reconcile",
    "app/modules/ipam/retention.py::sweep_all_orgs": "worker: IPAM retention sweep",
    "app/modules/keycloak_events/sweep.py::_forward": (
        "worker: forwards Keycloak events into the audit chain"
    ),
    **_reviewed(
        "app/modules/remote/recording.py",
        "worker: session-recording retention sweep",
        "_resolve_retention_days",
        "apply_recording_retention",
    ),
    "app/modules/security_logs/retention.py::apply_retention": (
        "worker: security-log retention sweep"
    ),
    "app/modules/sla/sweep.py::sweep_all_orgs": "worker: SLA breach and auto-close sweep",
}
