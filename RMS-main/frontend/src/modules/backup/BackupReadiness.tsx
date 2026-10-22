// "Before backups can run": what another screen has to have set up before
// this module backs anything up, and where each piece is fixed.
//
// THE OPERATOR ASKED FOR IT (2026-09-24, `docs/design/cross-module-guidance.md`
// §2 Backup, §6 #3). The case that made it a priority: with no enabled policy
// covering a device, "Run backup" answers 200 with no job at all
// (`backup/dispatch.py`: "Empty when there is no enabled policy"), so the
// first an operator learned of the gap was a run that never ran.
//
// Three panels, each on the screen where its gap bites:
//
//   BackupReadiness       /backup, /backup/coverage -- a repository, an
//                         enabled policy, a computer running the agent.
//   PolicyReadiness       /backup/policies -- a repository to write into,
//                         and a device group for a policy narrower than
//                         every device.
//   OffsiteReadiness      /backup/repositories -- a confirmed recovery key
//                         and tested off-site storage, before a repository
//                         can be copied off-site.
//
// NOTHING HERE GATES ANYTHING (invariant 6). These read lists the server
// already serves; the server decides every run and every refusal itself.

import { useQuery } from "@tanstack/react-query";

import { prerequisiteFromQuery, Prerequisites } from "../../components/prerequisites";
import { useT } from "../../i18n/context";
import { lazyFields } from "../../i18n/lazy";
import { mayCheck, useFleetPrerequisite, useGroupsPrerequisite } from "../../lib/detectors";
import { useApi } from "../../lib/ApiContext";
import { useMePermissions } from "../../lib/mePermissions";
import type { BackupPolicy, OffsiteTarget, RecoveryKey, Repository } from "./types";

export const BACKUP_READ = "backup.read";

// The labels are read from the catalogue when they are read, so a held
// constant follows a language switch.
export const CREATE_REPOSITORY_CTA = lazyFields({ to: "/backup/repositories" }, { label: "backup.readiness.createRepoCta" });
export const CREATE_POLICY_CTA = lazyFields({ to: "/backup/policies" }, { label: "backup.readiness.createPolicyCta" });

/*
 * The queries, on the keys the owning pages already use (`api.ts`), so a
 * panel shares their cache entry and their mutations' invalidations. Each is
 * asked only once `/me` says the account may read backups.
 */

function useRepositoriesCheck() {
  const api = useApi();
  const me = useMePermissions();
  return useQuery<Repository[]>({
    queryKey: ["backup-repositories"],
    queryFn: () => api.listBackupRepositories(),
    enabled: mayCheck(me, BACKUP_READ),
  });
}

function usePoliciesCheck() {
  const api = useApi();
  const me = useMePermissions();
  return useQuery<BackupPolicy[]>({
    queryKey: ["backup-policies"],
    queryFn: () => api.listBackupPolicies(),
    enabled: mayCheck(me, BACKUP_READ),
  });
}

function useRepositoryItem() {
  const t = useT();
  const me = useMePermissions();
  const repositories = useRepositoriesCheck();
  const item = prerequisiteFromQuery<Repository[]>({
    label: t("backup.readiness.repoLabel"),
    query: repositories,
    permission: { code: BACKUP_READ, me },
    isDone: (list) => list.length > 0,
    done: (list) => t("backup.readiness.repoDone", { count: list.length }),
    missing: {
      severity: "crit",
      detail: t("backup.readiness.repoMissing"),
      cta: CREATE_REPOSITORY_CTA,
    },
  });
  const none = repositories.isSuccess && repositories.data.length === 0;
  return { item, none };
}

/** On /backup and /backup/coverage. */
export function BackupReadiness() {
  const t = useT();
  const me = useMePermissions();
  const repository = useRepositoryItem();
  const policies = usePoliciesCheck();

  const policyItem = prerequisiteFromQuery<BackupPolicy[]>({
    label: t("backup.readiness.policyLabel"),
    query: policies,
    permission: { code: BACKUP_READ, me },
    waitingOn: repository.none ? t("backup.readiness.createRepoFirst") : null,
    isDone: (list) => list.some((policy) => policy.enabled),
    done: (list) => t("backup.readiness.policyDone", { count: list.filter((p) => p.enabled).length }),
    missing: (list) => ({
      severity: "crit",
      detail:
        list.length === 0
          ? t("backup.readiness.policyNone")
          : t("backup.readiness.policyNoneEnabled", { count: list.length }),
      cta:
        list.length === 0
          ? CREATE_POLICY_CTA
          : { to: "/backup/policies?enabled=false", label: t("backup.readiness.enablePolicyCta") },
    }),
  });

  const agentItem = useFleetPrerequisite("agent", {
    severity: "crit",
    missingDetail: t("backup.readiness.agentMissing"),
  });

  return (
    <Prerequisites
      title={t("backup.readiness.backupTitle")}
      subtitle={t("backup.readiness.backupSubtitle")}
      items={[repository.item, policyItem, agentItem]}
    />
  );
}

/** On /backup/policies. */
export function PolicyReadiness() {
  const t = useT();
  const repository = useRepositoryItem();
  const groupItem = useGroupsPrerequisite({
    label: t("backup.readiness.groupLabel"),
    missingDetail: t("backup.readiness.groupMissing"),
  });
  return (
    <Prerequisites
      title={t("backup.readiness.policyTitle")}
      subtitle={t("backup.readiness.policySubtitle")}
      items={[repository.item, groupItem]}
    />
  );
}

/** On /backup/repositories. */
export function OffsiteReadiness() {
  const t = useT();
  const api = useApi();
  const me = useMePermissions();
  const allowed = mayCheck(me, BACKUP_READ);
  const keys = useQuery<RecoveryKey[]>({
    queryKey: ["backup-recovery-keys"],
    queryFn: () => api.listRecoveryKeys(),
    enabled: allowed,
  });
  const targets = useQuery<OffsiteTarget[]>({
    queryKey: ["backup-offsite-targets"],
    queryFn: () => api.listOffsiteTargets(),
    enabled: allowed,
  });

  const keyItem = prerequisiteFromQuery<RecoveryKey[]>({
    label: t("backup.readiness.keyLabel"),
    query: keys,
    permission: { code: BACKUP_READ, me },
    isDone: (list) => list.some((key) => key.proven_at != null && key.retired_at == null),
    done: t("backup.readiness.keyDone"),
    missing: (list) => ({
      severity: "warn",
      detail: list.some((key) => key.retired_at == null)
        ? t("backup.readiness.keyUnconfirmed")
        : t("backup.readiness.keyNone"),
      cta: { to: "/backup/offsite", label: t("backup.readiness.keyCta") },
    }),
  });

  const storageItem = prerequisiteFromQuery<OffsiteTarget[]>({
    label: t("backup.readiness.storageLabel"),
    query: targets,
    permission: { code: BACKUP_READ, me },
    isDone: (list) => list.some((target) => target.state === "ok"),
    done: t("backup.readiness.storageDone"),
    missing: (list) => ({
      severity: "warn",
      detail:
        list.length === 0
          ? t("backup.readiness.storageNone")
          : t("backup.readiness.storageUntested"),
      cta: { to: "/backup/offsite", label: t("backup.readiness.storageCta") },
    }),
  });

  return (
    <Prerequisites
      title={t("backup.readiness.offsiteTitle")}
      subtitle={t("backup.readiness.offsiteSubtitle")}
      items={[keyItem, storageItem]}
    />
  );
}
