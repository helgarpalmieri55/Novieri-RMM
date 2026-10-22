# Module — BCP (Business Continuity / DR)

> Scoping brief. Phase 10 (last). Depends on Backup (Phase 6) and Change (Phase
> 9). Original request: "business continuity plan test reports and management (IT-
> wise)." Expand into a full spec via the brainstorming skill before
> implementation.

## Purpose

Manage IT business-continuity / disaster-recovery **plans**, run and record
**tests** of those plans, and produce **test reports** — turning DR from a
document that rots into a tracked, evidenced practice. A **SOC 2 evidence** engine
(BCP/DR testing is a common audit requirement).

## Scope — v1

- **BCP/DR plans:** define continuity plans for IT services/systems — scope,
  covered assets (linked to `Device`/`DeviceGroup`/service records), RTO/RPO
  targets, recovery procedures (linked to documentation runbooks), and
  dependencies.
- **Test plans & execution:** schedule and record DR tests (e.g. restore-from-
  backup drills, failover exercises). Capture who ran the test, when, steps
  performed, outcome vs RTO/RPO, and issues found.
- **Backup/restore evidence linkage:** pull **restore-tested** status from the
  **backup** module as direct evidence that recovery works (not just that backups
  exist).
- **Test reports:** generate structured DR test reports (per test and per period)
  suitable for SOC 2, with pass/fail against objectives and remediation items.
- **Remediation tracking:** issues found in a test become tracked action items,
  optionally linked to **change** records.
- **Review cadence:** track plan review/approval dates and flag overdue plans.

## Later phases

- Automated DR drills (trigger a test restore and verify automatically).
- Dependency mapping and impact analysis across services.
- Continuity dashboards (coverage, last-tested, RTO/RPO attainment trends).

## Agent capability

- **None directly.** BCP consumes evidence from other modules (backup restore
  tests, inventory, change). Automated drills (later) would reuse the **backup**
  module's `backup.restore` capability rather than introduce new agent code.

## Server side

- Plan repository (scope, RTO/RPO, procedures, dependencies); test scheduling and
  execution records; evidence linkage from backup; report generation; remediation
  tracking; review-cadence flags.

## Data model additions

- `bcp_plan` (org_id, name, scope jsonb, rto, rpo, procedure_refs, dependencies
  jsonb, last_reviewed_at, approved_by)
- `bcp_test` (org_id, plan_id, scheduled_for, executed_at, executed_by, type,
  outcome, rto_met, rpo_met, notes)
- `bcp_test_step` (test_id, step, result, evidence_ref)
- `bcp_finding` (org_id, test_id, description, severity, remediation_state,
  change_ref)

## Integration points

- **Backup** (hard dependency — restore-test status is the core DR evidence).
- **Documentation** (recovery procedures/runbooks linked into plans).
- **Change management** (findings → change records; major changes → BCP re-test).
- **Inventory** (assets covered by a plan).
- **SOC 2 evidence** (DR test reports are core audit evidence).

## Open questions (resolve in spec)

- v1 scope: fully manual test recording + evidence linkage vs any automated drill
  in v1 (recommend manual first; automate later using the backup capability).
- Report format/templates required for the target SOC 2 audit.
- How RTO/RPO attainment is measured and evidenced from real test data.
