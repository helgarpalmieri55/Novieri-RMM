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
        "before the scope exists (spec §2.2); on the MCP channel also the AI app's grant "
        "(the refresh family) the access token names, read and never written (own "
        "authentication phase 3, `grants.family_is_live`)"
    ),
    "app/core/auth/sessions.py::resolve": (
        "a session cookie names a row before anybody knows whose: a session of either kind "
        "-- a technician's, which has no client, or a contact's -- is read under every "
        "client, after its_auth_session gave the organisation; a contact's is touched here "
        "only as the row its own cookie's hash named, before its client is known (P2 design "
        "§6.2, §6.5; phase 2 final review, item 8)"
    ),
    "app/api/deps.py::_admit_session": (
        "door 1 for a console session (P2 design §6.5): the person's row and grants, read "
        "before the scope they define exists, as admit reads them for a token"
    ),
    "app/core/identity/door.py::read_session_identity": (
        "doors 2 and 3 (console WebSocket, remote tunnel) for a console session (P2 design "
        "§6.5): the domain rule, users.status and users.kind, and the grants the scope is "
        "computed from (spec §2.2, §2.6)"
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
    "app/core/rbac.py::scopes_by_code": (
        "COMPUTES, like clients_holding, the scope of each code the person holds, from one "
        "read of their grants: an all-clients grant of a code is that code's every-client "
        "scope. Only intersected, never used as a session's scope (the report catalogue, "
        "plan 2026-10-26 Task 14 review)"
    ),
    "app/core/auth/loopback.py::_live_session": (
        "the assistant's internal token (autenticación propia design §5.6): the session it "
        "names and its person, read by id before their scope exists -- the org comes from a "
        "token this platform signed, and the scope is then computed by admit() as for a cookie"
    ),
    # -- (d) personal: the caller's own row, which belongs to no client --------
    **_reviewed(
        "app/core/identity/own_profile.py",
        "/me is personal: the caller's OWN users row, by the user id the validated token "
        "resolved (never a request field), and G tables only (grants, roles, audit). A "
        "person with no grant has the empty scope, which 0231 hides staff rows from "
        "(Task 6b)",
        "read_own_profile",
        "update_own_profile",
    ),
    # -- (e) the platform's own sign-in (P2 phase 1): a technician's auth rows
    # (O, `users.client_id` NULL) are global rows only an every-client scope
    # may write, and they are read before -- or regardless of -- the person's
    # scope. Each helper opens ONE organization's session over those rows
    # alone; the organization comes from `its_auth_principal`, `its_auth_org`
    # (the deployment's slug, phase 2), the session's own row or the link,
    # never from a request field naming it.
    "app/core/auth/flows.py::_identity_session": (
        "sign-in where no signed-in contact is involved (P2 design §6.2; phase 2, D25, D28; "
        "Task 9b item C): the steps before a session exists -- the password, a contact's "
        "passkey alone -- and the use of a link -- reset, invitation, recovery -- over one "
        "person's own auth rows and users row; a TECHNICIAN's own steps with a session (the "
        "second factor, the enrolment, the recent confirmation, signing out: "
        "`signed_in_session`), whose rows are global; and the one read keyed by the "
        "session's own person that learns a signed-in CONTACT's client, whose steps then "
        "run under that client alone, never here. Accepting a contact's invitation also "
        "writes their portal role on their OWN client (`grant_portal`, D40) and reads that "
        "client's Microsoft tenants (`microsoft_offered`, design §4)"
    ),
    "app/core/auth/account.py::own_session": (
        "My security is personal, like /me: a TECHNICIAN's own auth rows, by the user id of "
        "the session the door admitted (P2 phase 1 design §2.4-§2.6); their rows are global, "
        "which a per-client scope may read and not change. The technician's path only: a "
        "client's contact on the same routes (phase 2, D26) runs under the scope the door "
        "pinned to their own client, and every client is refused for one, as is any other "
        "kind (plan 2026-11-06, Task 7 fix rounds 1-2, invariant 5a). Unlinking Microsoft "
        "(`entra_flows.unlink`, `DELETE /me/microsoft`, phase 2 Task 9) opens this same session"
    ),
    "app/core/auth/recover.py::issue_recovery_link": (
        "the server's own recovery command, never HTTP: one technician's recovery link, "
        "issued by whoever holds the server (P2 phase 1 design §7)"
    ),
    "app/core/auth/entra_flows.py::_entra_session": (
        "sign-in with Microsoft's own steps where no signed-in person is involved "
        "(`/auth/microsoft/start` and `/complete`, which no door admits): the sign-in before "
        "anybody is known, an invitation accepted with Microsoft -- a link's use: one "
        "person's link (tid, oid), users row and new session, a contact's portal role on "
        "their OWN client (`contact_admitted`) and that client's Microsoft tenants (read), "
        "found before their scope exists -- and the return's checkout of the platform's own "
        "key, which reads and writes no person's rows. The organization from the "
        "deployment's slug, the session or the invitation (P2 phase 2 design §4). NOT a "
        "signed-in person's link, start or return, which runs in the scope of their kind -- "
        "a contact's own client (`flows.signed_in_session`, Task 9b item C) -- and NOT "
        "unlinking, under the scope the door established (`account.own_session`, "
        "invariant 5a)"
    ),
    "app/core/auth/oauth/store.py::oauth_session": (
        "the platform's own OAuth server (P2 phase 3 design §5): an AI app's registration (G) "
        "and a technician's grants, codes and refresh tokens -- global rows only an every-client "
        "scope may write, read by a code or token hash before anybody's scope exists, or by "
        "the id of the person the console session or the verified access token named. Also "
        "holds Settings -> AI connections' revoke (Task 10): the caller's own grants, or with "
        "orgs.manage on every client everyone's"
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
    "app/core/auth/retention.py::sweep_org": (
        "worker: own authentication's retention (final review A-M6, operator decision C7; "
        "phase 3 plan 2026-11-06, Task 13) -- one organization's old sessions, forgotten "
        "devices, spent links and old challenges, and its AI apps' spent codes and ended "
        "grants with their refresh tokens, whose technician rows only an every-client scope "
        "may delete (its idle AI apps go by clients.purge_idle, under oauth_session, and "
        "when the sweep then fails a second transaction writes their audit row alone); every "
        "step carries its own org_id predicate, and the sweep is audited"
    ),
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
