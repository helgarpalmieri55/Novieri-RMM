// "Before software can be deployed": what other screens have to have set up
// before a deployment from this page does anything, and where each is fixed.
//
// THE OPERATOR ASKED FOR IT (2026-09-24, `docs/design/cross-module-guidance.md`
// §2 Software, §6 #6):
//
//   1  a computer runs the agent -- a deployment is a signed job on it;
//   2  every artifact-backed package has its installer uploaded -- execute
//      answers 422 "has no uploaded artifact" otherwise (`deploy/artifacts.py`,
//      whose rule `needsUpload` below mirrors exactly);
//   3  where a change gate covers deployments, an approved change exists --
//      otherwise the deployment is refused with 409 (`change_mgmt/gates.py`).
//
// "A package is in the catalogue" is already this page's own link beside the
// package picker, and "device groups exist" is said beside the group picker,
// so neither is repeated here.
//
// NOTHING HERE GATES ANYTHING (invariant 6).

import { prerequisiteFromQuery, Prerequisites } from "../../components/prerequisites";
import { useT } from "../../i18n/context";
import { joinWords, useFleetPrerequisite } from "../../lib/detectors";
import { useMePermissions } from "../../lib/mePermissions";
import { useChangeGatePrerequisite } from "../change/gatePrerequisite";
import { usePackages } from "./api";
import type { Package } from "./types";

export const DEPLOY_READ = "deploy.read";

/** `deploy/artifacts.py::ARTIFACT_BACKED_INSTALLER_TYPES`: these ship a file the platform stores. */
export const ARTIFACT_BACKED_INSTALLER_TYPES: readonly string[] = ["msi", "exe", "pkg", "dmg", "deb", "rpm", "script"];

/** An artifact-backed package with no stored file, or no recorded hash: the server refuses to dispatch it. */
export function needsUpload(pkg: Pick<Package, "installer_type" | "artifact_ref" | "artifact_sha256">): boolean {
  return ARTIFACT_BACKED_INSTALLER_TYPES.includes(pkg.installer_type) && (!pkg.artifact_ref || !pkg.artifact_sha256);
}

export function DeployReadiness() {
  const t = useT();
  const me = useMePermissions();
  const packages = usePackages();

  const agentItem = useFleetPrerequisite("agent", {
    severity: "crit",
    missingDetail: t("deploy.readiness.agentMissing"),
  });

  const uploadItem = prerequisiteFromQuery<Package[]>({
    label: t("deploy.readiness.uploadLabel"),
    query: packages,
    permission: { code: DEPLOY_READ, me },
    isDone: (list) => !list.some(needsUpload),
    done: t("deploy.readiness.uploadDone"),
    missing: (list) => {
      const names = list.filter(needsUpload).map((pkg) => `${pkg.name} ${pkg.version}`);
      return {
        severity: "crit",
        detail: t("deploy.readiness.uploadMissing", { names: joinWords(names), count: names.length }),
        cta: { to: "/deploy/packages", label: t("deploy.readiness.uploadCta") },
      };
    },
  });

  const gateItem = useChangeGatePrerequisite("deployments", {
    label: t("deploy.readiness.gateLabel"),
    ctaLabel: t("deploy.readiness.gateCta"),
  });

  return (
    <Prerequisites
      title={t("deploy.readiness.title")}
      subtitle={t("deploy.readiness.subtitle")}
      items={[agentItem, uploadItem, gateItem]}
    />
  );
}
