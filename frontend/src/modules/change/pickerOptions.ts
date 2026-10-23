/**
 * The platform actions a change can be linked to, as picker options.
 *
 * WHY THIS FILE (UX remediation cycle 6). Auditor B, [HIGH], /changes/:id ->
 * Linked actions: "To link a change to the patch run or deployment that carried
 * it out, the user picks a type from a dropdown and then types the Action ID
 * into a bare text input. There is no picker, no search, no recent-actions
 * list... the ID format is a 36-character UUID which the user must find in
 * another module and transcribe. This is the evidence trail that makes the
 * change record auditable, so friction here means it simply won't be filled
 * in."
 *
 * WHAT THE LINK IS FOR decides how wrong a wrong id is. `change_mgmt/links.py`
 * never reads back into the linked row -- the link is advisory -- so a
 * mistyped-but-existing id does not misfire a job the way a mistyped device id
 * does. It does something quieter and, for a governance module, worse: the
 * evidence pack for this change points at somebody else's deployment, and
 * nothing in the product will ever contradict it.
 *
 * TWO OF THE FOUR TYPES HAVE NO PICKER YET. `patch_run` and `patch_approval`
 * are accepted by `LinkCreateRequest`. When this was written the patch router
 * listed neither; it now does (`GET /patch/runs`, `GET /patch/approvals`,
 * cross-module guidance doc §4), so both could be pickers here. Until they
 * are, those two keep the text field and the note under it says where the
 * console lists them.
 *
 * Pure functions, kept out of the page, because the SUBLABEL is the part
 * carrying the safety and it deserves a test rather than an inspection of a
 * JSX tree.
 */

import type { PickerOption } from "../../components/picker";
import { translate } from "../../i18n/core";
import { formatDateTime } from "../../lib/datetime";
import { humaniseEnum, humaniseStatus } from "../../lib/labels";
import { deploymentActionLabel } from "../deploy/pickerOptions";
import type { Deployment, DeploymentState, Package } from "../deploy/types";
import { provisionRunStateLabel } from "../provisioning/provisioningState";
import type { ProvisionProfile, ProvisionRun } from "../provisioning/types";

/**
 * WHAT WOULD FILL THE LIST, which is the whole of the cycle-7 half.
 *
 * Both hints name the console surface that populates the list AND the other
 * reason it can be empty: these reads carry their own permissions
 * (`deploy.read`, `provisioning.read`), so "there are none" and "you may not
 * see them" are both live and a hint asserting only the first would be wrong
 * for exactly the operator who most needs to know.
 */
// Components call these, which follow the language.
export const deploymentPickerEmptyHint = (): string => translate("change.picker.deploymentEmptyHint");

export const provisionRunPickerEmptyHint = (): string => translate("change.picker.runEmptyHint");

const DEPLOYMENT_STATES: ReadonlySet<string> = new Set<DeploymentState>([
  "pending",
  "dispatching",
  "complete",
  "failed",
]);

/** A deployment's state in lower case, for the middle of a sublabel. */
function deploymentStateWord(state: string): string {
  const label = DEPLOYMENT_STATES.has(state) ? humaniseEnum(state, "DeploymentState") : humaniseStatus(state);
  return label.toLocaleLowerCase();
}

/**
 * A deployment, with the facts that tell two of them apart.
 *
 * PACKAGE NAME IS THE LABEL, not the deployment id: an operator linking the
 * change that rolled out firmware 2.1 is looking for "firmware 2.1", and the
 * deployment row carries only `package_id`. WHEN is the sublabel because the
 * same package is deployed repeatedly -- the run being evidenced is the one
 * from this change's maintenance window -- and STATE is there because linking
 * a `failed` deployment as the implementation of a completed change is a
 * contradiction the evidence pack would otherwise carry silently.
 */
export function deploymentPickerOptions(
  deployments: readonly Deployment[],
  packages: readonly Package[],
  zone: string | null,
): PickerOption[] {
  const byId = new Map(packages.map((pkg) => [pkg.id, pkg]));
  return deployments.map((deployment) => {
    const pkg = byId.get(deployment.package_id);
    // Falls back to the id, never to blank: a package the caller cannot read
    // is still better identified by its id than by nothing.
    const name = pkg ? `${pkg.name} ${pkg.version}` : deployment.package_id;
    return {
      id: deployment.id,
      label: `${name} · ${deploymentActionLabel(deployment.action).toLocaleLowerCase()}`,
      sublabel: `${deploymentStateWord(deployment.state)} · ${formatDateTime(deployment.created_at, zone)}`,
    };
  });
}

/**
 * A provisioning run, with the facts that tell two of them apart.
 *
 * The profile name is what a person calls the run ("Standard laptop build");
 * the run's own id is the thing this picker exists to stop them typing. The
 * sublabel carries the state and when it started, because a profile is run
 * many times and the one being evidenced is a particular machine's build.
 */
export function provisionRunPickerOptions(
  runs: readonly ProvisionRun[],
  profiles: readonly ProvisionProfile[],
  zone: string | null,
): PickerOption[] {
  const byId = new Map(profiles.map((profile) => [profile.id, profile]));
  return runs.map((run) => {
    const profile = byId.get(run.profile_id);
    const target = run.device_id ?? run.target_ref;
    return {
      id: run.id,
      label: profile ? profile.name : run.profile_id,
      sublabel: [provisionRunStateLabel(run.state).toLocaleLowerCase(), formatDateTime(run.started_at, zone), target]
        .filter((part): part is string => Boolean(part))
        .join(" · "),
    };
  });
}
