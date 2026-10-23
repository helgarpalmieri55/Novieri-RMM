/**
 * The lists this module has to point at, as picker options.
 *
 * WHY THIS FILE (UX remediation cycles 6 and 7). Two audit findings meet in one
 * dialog:
 *
 *   Auditor A, [BLOCKER], /deploy → New deployment: "Deployments cannot be
 *   created — the Package dropdown is empty and Create silently does nothing
 *   ... The user has no way to learn that the blocker is an empty package
 *   catalog, and there is no link to a catalog/import screen to populate it."
 *
 *   Auditor A, [HIGH], same dialog: "'Target ID' requires the operator to type
 *   a raw device UUID by hand — no picker, no search, no validation ... a
 *   single mistyped character either fails opaquely or, worse, targets the
 *   wrong machine."
 *
 * Pure functions, kept out of the pages, because the SUBLABEL is the part that
 * carries the safety and it deserves a test of its own rather than an
 * inspection of a JSX tree — `EntityPicker`'s own header says the sublabel "is
 * also what stops somebody dispatching to a machine that cannot answer".
 *
 * Deliberately this module's own copy rather than an import from
 * `service-desk/pickerOptions.ts`: the hints below name deploy's surfaces and
 * the package shape has no counterpart there. If a third module needs the
 * device version, that is the moment it moves to `lib/`.
 */

import type { PickerOption } from "../../components/picker";
import { translate } from "../../i18n/core";
import { humaniseEnum } from "../../lib/labels";
import type { Device, DeviceGroup } from "../../lib/api";
import { INTERPRETERS, type Package } from "./types";

/**
 * WHAT WOULD FILL THE LIST, which is the whole of the BLOCKER fix.
 *
 * A picker that is empty on a fresh install and says nothing is the blocked
 * flow wearing a nicer control. Both hints name the console surface that
 * populates the list AND the other reason it can be empty — these reads carry
 * their own permissions (`deploy.package.manage` publishes a package,
 * `devices.read` lists endpoints), so "there are none" and "you may not see
 * them" are both live and the hint must not assert only the first.
 */
// Functions, not constants: a constant would keep the language the module
// loaded in, and the hint must follow a switch.
export const packagePickerEmptyHint = (): string => translate("deploy.picker.packageEmptyHint");

export const devicePickerEmptyHint = (): string => translate("deploy.picker.deviceEmptyHint");

export const groupPickerEmptyHint = (): string => translate("deploy.picker.groupEmptyHint");

/**
 * An endpoint, with the two facts that decide whether it is the right one.
 *
 * `os_family · agent_status` — the picker header's own example. Deploying to an
 * offline agent is not an error the server reports: the job is signed, queued,
 * and sits there. The dashboard finding ("two `patch.scan` jobs queued for over
 * 24 hours against a device that has been offline the whole time") is that
 * outcome. A device with no agent row has no status at all and says "no agent"
 * rather than rendering an empty separator — it cannot run a deployment.
 */
export function devicePickerOptions(devices: readonly Device[]): PickerOption[] {
  return devices.map((device) => {
    const status =
      device.agent_status === "online"
        ? translate("deploy.picker.agentOnline")
        : device.agent_status === "offline"
          ? translate("deploy.picker.agentOffline")
          : translate("deploy.picker.noAgent");
    const parts = [device.os_family, status].filter(
      (part): part is string => Boolean(part),
    );
    return {
      id: device.id,
      label: device.hostname ?? device.fqdn ?? device.id,
      sublabel: parts.join(" · "),
    };
  });
}

/**
 * A device group, so a target row names the group rather than a pasted id.
 *
 * ARCHIVED GROUPS ARE EXCLUDED, the same filter `GroupsPage`'s own parent
 * picker applies: an archived group is not a place to aim a new deployment,
 * even though a past deployment may still point at one (`nameForOption`
 * still resolves the name for that row from whatever the list returns).
 */
export function groupPickerOptions(groups: readonly DeviceGroup[]): PickerOption[] {
  return groups
    .filter((group) => group.archived_at === null)
    .map((group) => ({
      id: group.id,
      label: group.name,
      sublabel: translate("deploy.picker.groupDevices", { count: group.device_count }),
    }));
}

/**
 * A package, with the two facts that decide whether it can install.
 *
 * OS FAMILY, because a Windows MSI aimed at a Linux endpoint is the mistake
 * this list can prevent and the server will not; and the ARTIFACT, because a
 * package row with no uploaded bytes (`artifact_sha256 === null`) is a catalog
 * entry that dispatches a job which cannot succeed. The audit's own catalog was
 * empty, so nobody hit that second case yet — it is one upload away.
 */
export function packagePickerOptions(packages: readonly Package[]): PickerOption[] {
  return packages.map((pkg) => {
    const artifact = pkg.artifact_sha256
      ? pkg.installer_type
      : translate("deploy.picker.packageNoArtifact", { type: pkg.installer_type });
    return {
      id: pkg.id,
      label: `${pkg.name} ${pkg.version}`,
      sublabel: `${pkg.os_family} · ${artifact}`,
    };
  });
}

/**
 * The name behind an id already stored on a row.
 *
 * Auditor A rendered `package_id` and `target_id` as raw UUIDs in the
 * deployments table. Falls back to the id, never to blank: an id whose row the
 * caller cannot read is still more than nothing, and printing "—" for a
 * deployment that HAS a target would be the worse lie.
 */
export function nameForOption(options: readonly PickerOption[], id: string | null): string | null {
  if (!id) return null;
  return options.find((option) => option.id === id)?.label ?? id;
}

/**
 * The platform as a person writes it. `humaniseEnum("macos")` gives "Macos",
 * which nobody has ever written; the closed set the server accepts is small
 * enough to spell out, and anything else is shown as it came.
 */
const OS_FAMILY_LABELS: Record<string, string> = {
  windows: "Windows",
  linux: "Linux",
  macos: "macOS",
};

export function osFamilyLabel(osFamily: string): string {
  return OS_FAMILY_LABELS[osFamily.toLowerCase()] ?? osFamily;
}

/**
 * The other two wire vocabularies this module prints in a table.
 *
 * `humaniseEnum` handles the shape (`answer_file` -> "Answer file") but not
 * the spelling of a product name: it gives "Powershell", which is not how
 * Microsoft writes it, and "Macos", which nobody writes at all. The closed
 * sets are small, so they are spelled out and anything outside them falls back
 * to the general rule rather than to the raw token.
 */
export function interpreterLabel(interpreter: string): string {
  const key = interpreter.trim().toLowerCase();
  // The field takes any text; a value outside the known set is shown by the
  // general rule, from the text as it was typed.
  const known = (INTERPRETERS as readonly string[]).includes(key);
  return humaniseEnum(known ? key : interpreter, "Interpreter");
}

/**
 * WHO ASKED FOR IT, as v3's `person(ini, i, name)` needs it.
 *
 * `Deployment.requested_by` is a user id, so the row can draw a person only
 * once the directory (`useOrgUsers`) has answered. `requesterName` falls back
 * to the id -- an id whose account this caller may not read is still more
 * than nothing, and a blank BY column on a deployment somebody DID request
 * would be the worse lie.
 *
 * THE INITIALS AND THE AVATAR TONE USED TO LIVE HERE TOO, as this module's own
 * copy of what `service-desk` and `subscriptions` each also held. They are
 * `lib/people.ts` now: the four copies did not agree, and the tone exists
 * precisely so that the same person is the same colour on every page.
 */
export function requesterName(users: ReadonlyMap<string, string>, id: string | null): string | null {
  if (!id) return null;
  return users.get(id) ?? id;
}

/** `install` -> "Install". The deployment's verb, as the operator says it. */
export function deploymentActionLabel(action: string): string {
  return humaniseEnum(action, "DeploymentAction");
}
