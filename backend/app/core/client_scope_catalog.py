"""Every org-scoped table's relation to clients (MSP clients spec, Annex A).

C  client_id NOT NULL, set by the application.
H  client_id NOT NULL, set by the `its_client_from_parent` trigger from
   `parent` through `fk`.
O  client_id nullable: NULL means "every client" (config). When `fk` is set
   and non-null on a row, the trigger derives the client from it.
G  no client scoping. A column named client_id there means something else:
   the grant scope on `user_roles`, an OAuth client on `mcp_revocation`
   and `email_oauth_app`. On `audit_events` it is the informative client of
   the event's subject (hashed when set, read by no policy: phase 2 C6).

`PENDING_PHASE_2` are C/H tables whose module code must start writing
client_id before the column can be NOT NULL (plan "Fuera de esta fase"). The
phase-2 plan's last task deletes that set; until then those tables are exempt
from the policy check, and from nothing else.
"""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Entry:
    kind: Literal["C", "H", "O", "G"]
    parent: str | None = None
    fk: str | None = None


C, G = Entry("C"), Entry("G")


def H(parent: str, fk: str) -> Entry:
    return Entry("H", parent, fk)


def O(parent: str | None = None, fk: str | None = None) -> Entry:  # noqa: E743
    return Entry("O", parent, fk)


CATALOG: dict[str, Entry] = {
    # identity and platform
    "organizations": G, "roles": G, "permissions": G, "role_permissions": G,
    "user_roles": G, "users": O(), "audit_events": G, "keycloak_event_ingest": G,
    "agent_releases": G, "dashboard_layouts": G, "mcp_revocation": G,
    "platform_error_group": G, "platform_error_event": G, "platform_health_sample": G,
    "platform_probe_notice": G, "org_vault_keys": G, "domain_events": O(),
    # clients themselves
    "clients": G, "sites": C,
    # fleet
    "device_groups": C, "device_group_evaluation": G, "devices": C,
    "suggested_asset_match": H("devices", "device_id"),
    "device_tags": H("devices", "device_id"), "agents": H("devices", "device_id"),
    "policies": O("device_groups", "device_group_id"),
    "credentials": O("device_groups", "device_group_id"),
    "jobs": H("devices", "target_id"),  # special-cased: target_type device|group
    "job_logs": H("jobs", "job_id"),
    "enrollment_tokens": C, "install_links": C,
    # AI
    "ai_autonomy_ceiling": G, "ai_session": G, "ai_message": G, "ai_injection_flag": G,
    "ai_settings": G, "ai_action": H("devices", "target_device_id"),
    "approved_action": G, "approved_action_script_exemption": G,
    # documentation (O: NULL = Novieri's own knowledge base)
    "doc_articles": O(), "doc_records": O(), "doc_documents": O(),
    "doc_links": O("doc_articles", "article_id"),
    "doc_versions": O("doc_articles", "article_id"),  # or record_id, see 0231
    "doc_document_versions": O("doc_documents", "document_id"),
    "doc_document_links": O("doc_documents", "document_id"),
    # inventory and security
    "inventory_snapshots": H("devices", "device_id"),
    "installed_software": H("devices", "device_id"),
    "inventory_changes": H("devices", "device_id"),
    "hardware_components": H("devices", "device_id"),
    "security_events": H("devices", "device_id"),
    # push install
    "push_authorization": C, "push_run": C,
    "push_credential_scope": H("credentials", "credential_id"),
    "push_credential_realm": H("credentials", "credential_id"),
    "push_host_key": O("devices", "device_id"),
    "push_batch_target": H("jobs", "job_id"),
    "push_run_target": H("push_run", "run_id"),
    # agent update
    "agent_update_rollouts": G,
    "agent_update_rings": O("device_groups", "device_group_id"),
    "agent_update_targets": H("devices", "device_id"),
    # backup
    "backup_repositories": C,
    "backup_chunks": H("backup_repositories", "repository_id"),
    "backup_chunk_copies": H("backup_repositories", "repository_id"),
    "backup_policies": O("device_groups", "device_group_id"),
    "backup_offsite_targets": O(), "backup_recovery_keys": O(),
    "backup_runs": H("devices", "device_id"), "backup_points": H("devices", "device_id"),
    "restore_jobs": H("devices", "device_id"),
    "backup_point_copies": H("backup_points", "point_id"),
    # optional modules
    "bcp_plan": C, "bcp_test": H("bcp_plan", "plan_id"),
    "bcp_test_step": H("bcp_test", "test_id"), "bcp_finding": H("bcp_test", "test_id"),
    "budget_settings": C, "cost_center": C, "expense_category": C, "budget": C,
    "expense": C, "budget_line": H("budget", "budget_id"), "fx_rate": G,
    "change_request": C,
    "change_approval": H("change_request", "change_id"),
    "change_link": H("change_request", "change_id"),
    "change_external_ref": H("change_request", "change_id"),
    "change_gates": O("device_groups", "device_group_id"),
    # software
    "packages": G, "scripts": G, "deployments": C,
    "deployment_results": H("devices", "device_id"),
    "script_runs": H("devices", "device_id"),
    "script_policies": H("device_groups", "device_group_id"),
    # email intake
    "email_mailbox": G, "email_oauth_app": G, "email_oauth_authorization": G,
    "inbound_message": O(),
    # health
    "health_profile": G, "health_check": G, "health_org_settings": G,
    "health_baseline_seed": G,
    "health_profile_assignment": O("devices", "device_id"),  # or device_group_id, see 0231
    "health_alert": H("devices", "device_id"),
    "device_health_check_state": H("devices", "device_id"),
    "health_flap_state": H("devices", "device_id"),
    "device_health_policy_ack": H("devices", "device_id"),
    "health_alert_action": H("health_alert", "alert_id"),
    # ipam (ipam_site is merged into sites in phase 2)
    "ipam_site": C, "ipam_vrf": C, "ipam_vlan": C, "ipam_subnet": C,
    "ipam_scan_authorization": H("ipam_vrf", "vrf_id"),
    "ipam_range": H("ipam_subnet", "subnet_id"),
    "ipam_address": H("ipam_vrf", "vrf_id"),
    "ipam_observation": H("ipam_vrf", "vrf_id"),
    "ipam_drift": H("ipam_vrf", "vrf_id"),
    "ipam_sweep_run": H("ipam_subnet", "subnet_id"),
    # licenses and subscriptions
    "vendor": G, "software_product": G, "license": C,
    "license_compliance_snapshot": C, "license_allocation": H("license", "license_id"),
    "software_ignore": O(),
    "subscription": C, "seat_assignment": H("subscription", "subscription_id"),
    "subscription_event": H("subscription", "subscription_id"),
    "subscription_alert_sent": H("subscription", "subscription_id"),
    # notification
    "notification_channels": G, "notification_rules": G, "queue_members": G,
    "notification_deliveries": G, "webhook_allowlist_entries": G,
    # patch
    "patches": G, "patch_policies": O("device_groups", "device_group_id"),
    "patch_approvals": O(), "device_patch_status": H("devices", "device_id"),
    "patch_reboots": H("devices", "device_id"), "patch_runs": H("devices", "device_id"),
    # people
    "person": C, "person_login": H("person", "person_id"),
    "person_checklist": H("person", "person_id"),
    "person_checklist_step": H("person_checklist", "checklist_id"),
    "device_person": H("devices", "device_id"),
    # provisioning (module hidden)
    "os_image": G, "provision_profile": G, "provision_run": O("devices", "device_id"),
    # remote
    "remote_sessions": H("devices", "device_id"),
    "remote_policies": O("device_groups", "device_group_id"),
    # service desk and SLA
    "ticket_queue": G, "ticket_category": G, "ticket_number_counter": G,
    "ticket_canned_response": G, "ticket": C,
    "ticket_comment": H("ticket", "ticket_id"), "ticket_attachment": H("ticket", "ticket_id"),
    "ticket_approval": H("ticket", "ticket_id"),
    "ticket_status_history": H("ticket", "ticket_id"),
    "ticket_sla": H("ticket", "ticket_id"),
    "sla_calendar": G, "sla_calendar_window": G, "sla_calendar_holiday": G,
    "sla_org_settings": G, "sla_policy": O(),
    # site servers
    "site_server": H("devices", "device_id"),
}

PENDING_PHASE_2: frozenset[str] = frozenset(
    {
        "ipam_site", "ipam_vrf", "ipam_vlan", "ipam_subnet", "ipam_range", "ipam_address",
        "ipam_observation", "ipam_drift", "ipam_scan_authorization", "ipam_sweep_run",
        "push_authorization", "push_run", "push_run_target", "push_credential_scope",
        "push_credential_realm",
        "backup_repositories", "backup_chunks", "backup_chunk_copies",
    }
)

PHASE_1 = {t: e for t, e in CATALOG.items() if e.kind != "G" and t not in PENDING_PHASE_2}
