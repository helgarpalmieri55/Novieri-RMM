// "Before these reports have something to show": for each report, whether the
// module it reads from has anything in it (docs/design/cross-module-guidance.md
// §2, Reports).
//
// WHY. Every report here runs happily on an empty module and answers with an
// empty table, which an operator cannot tell from a clean estate: a patch
// compliance report with no approved updates says "nothing outstanding", and
// an SLA attainment report over no SLA policies says nothing at all
// (`ReportView`'s empty state cannot tell "not set up" from "no rows"). The
// catalogue already says which reports this account may RUN
// (`missing_permissions`); this says which ones would have something to show.
//
//   fleet-posture      a computer runs the agent               devices.read
//   patch-compliance   a scan ran and an update is approved    patch.read
//   backup-coverage    a backup policy is enabled              backup.read
//   sla-attainment     a response-target policy is active      sla.read
//
// All advisory: each report still runs. One row per report the catalogue
// lists, so a deployment that does not publish a report is not asked about it.
//
// NOTHING HERE GATES ANYTHING (invariant 6).

import { useQuery } from "@tanstack/react-query";

import {
  combineQueries,
  prerequisiteFromQuery,
  Prerequisites,
  type Prerequisite,
} from "../../components/prerequisites";
import { useT } from "../../i18n/context";
import type { ApiError } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { mayCheck, useFleetPrerequisite } from "../../lib/detectors";
import { useMePermissions } from "../../lib/mePermissions";
import type { BackupPolicy } from "../backup/types";
import { PATCH_APPROVALS_QUERY_KEY } from "../patch/api";
import type { ApprovalListFilters, ApprovalListResponse, FleetReport } from "../patch/types";
import type { PolicyOut as SlaPolicy } from "../sla/types";
import { reportName } from "./reportText";

const PATCH_READ = "patch.read";
const BACKUP_READ = "backup.read";
const SLA_READ = "sla.read";

/** In force only, one row: "is any update approved at all". */
const LIVE_APPROVAL: ApprovalListFilters = { revoked: false, limit: 1 };

/** The reports this panel knows how to check, in catalogue order. */
export const CHECKED_REPORTS = ["fleet-posture", "patch-compliance", "backup-coverage", "sla-attainment"] as const;

export function ReportReadiness({ keys }: { keys: readonly string[] | undefined }) {
  const t = useT();
  const api = useApi();
  const me = useMePermissions();

  // Same keys and shapes as the owning modules' hooks, so their mutations
  // refresh these and a visit to either page is a cache hit.
  const fleetReport = useQuery<FleetReport, ApiError>({
    queryKey: ["patch-fleet"],
    queryFn: () => api.getPatchFleetReport(),
    enabled: mayCheck(me, PATCH_READ),
    retry: false,
  });
  const approvals = useQuery<ApprovalListResponse, ApiError>({
    queryKey: [PATCH_APPROVALS_QUERY_KEY, LIVE_APPROVAL],
    queryFn: () => api.listPatchApprovals(LIVE_APPROVAL),
    enabled: mayCheck(me, PATCH_READ),
    retry: false,
  });
  const policies = useQuery<BackupPolicy[], ApiError>({
    queryKey: ["backup-policies"],
    queryFn: () => api.listBackupPolicies(),
    enabled: mayCheck(me, BACKUP_READ),
    retry: false,
  });
  const slaPolicies = useQuery<SlaPolicy[], ApiError>({
    queryKey: ["sla-policies"],
    queryFn: () => api.listSlaPolicies(),
    enabled: mayCheck(me, SLA_READ),
    retry: false,
  });

  const fleetItem = useFleetPrerequisite("agent", {
    label: t("reporting.readiness.fleetLabel", { report: reportName("fleet-posture") }),
    missingDetail: t("reporting.readiness.fleetMissing"),
  });

  const patchItem = prerequisiteFromQuery({
    label: t("reporting.readiness.patchLabel", { report: reportName("patch-compliance") }),
    query: combineQueries(fleetReport, approvals),
    permission: { code: PATCH_READ, me },
    isDone: ([report, approved]) => report.devices_scanned > 0 && approved.total > 0,
    done: ([report, approved]) =>
      t("reporting.readiness.patchDone", {
        scanned: t("reporting.readiness.scanned", { count: report.devices_scanned }),
        approvals: t("reporting.readiness.approvals", { count: approved.total }),
      }),
    missing: ([report]) => ({
      severity: "warn",
      detail:
        report.devices_scanned === 0
          ? t("reporting.readiness.patchNoScan")
          : t("reporting.readiness.patchNoApproval"),
      cta: { to: "/patch", label: t("reporting.readiness.patchCta") },
    }),
  });

  const backupItem = prerequisiteFromQuery({
    label: t("reporting.readiness.backupLabel", { report: reportName("backup-coverage") }),
    query: policies,
    permission: { code: BACKUP_READ, me },
    isDone: (list) => list.some((policy) => policy.enabled),
    done: (list) =>
      t("reporting.readiness.backupDone", { count: list.filter((policy) => policy.enabled).length }),
    missing: {
      severity: "warn",
      detail: t("reporting.readiness.backupMissing"),
      cta: { to: "/backup/policies", label: t("reporting.readiness.backupCta") },
    },
  });

  const slaItem = prerequisiteFromQuery({
    label: t("reporting.readiness.slaLabel", { report: reportName("sla-attainment") }),
    query: slaPolicies,
    permission: { code: SLA_READ, me },
    isDone: (list) => list.some((policy) => policy.active),
    done: (list) =>
      t("reporting.readiness.slaDone", { count: list.filter((policy) => policy.active).length }),
    missing: {
      severity: "warn",
      detail: t("reporting.readiness.slaMissing"),
      cta: { to: "/sla", label: t("reporting.readiness.slaCta") },
    },
  });

  const byKey: Record<(typeof CHECKED_REPORTS)[number], Prerequisite> = {
    "fleet-posture": fleetItem,
    "patch-compliance": patchItem,
    "backup-coverage": backupItem,
    "sla-attainment": slaItem,
  };
  // Until the catalogue answers (or if it fails) every known report is asked
  // about; once it answers, only the ones this deployment publishes.
  const items = CHECKED_REPORTS.filter((key) => keys === undefined || keys.includes(key)).map((key) => byKey[key]);

  return (
    <Prerequisites
      title={t("reporting.readiness.title")}
      subtitle={t("reporting.readiness.subtitle")}
      items={items}
    />
  );
}
