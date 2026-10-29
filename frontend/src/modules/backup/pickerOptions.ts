/**
 * The lists this module makes somebody choose from, as picker options.
 *
 * WHY THIS FILE (UX remediation cycles 6 and 7). Auditor A, HIGH, on this
 * module's only targeting control: "A fifth hand-typed raw ID field — 'Device
 * ID' is the only way to target a backup ... The stat card directly beside it
 * reads 'Protected endpoints 0 / 1', so the system knows there is exactly one
 * device — but will not offer it."
 *
 * Pure functions in a file of their own, because the SUBLABEL is the part
 * that carries the safety and it deserves to be pinned by a test rather than
 * inspected in a JSX tree. `components/picker.tsx`'s own header: the sublabel
 * "is also what stops somebody dispatching to a machine that cannot answer".
 */

import type { PickerOption } from "../../components/picker";
import { translate } from "../../i18n/core";
import type { Device, DeviceGroup } from "../../lib/api";
import type { BackupPolicy, Repository } from "./types";

/**
 * WHAT WOULD FILL THESE LISTS. Each names the console surface that populates
 * it, and each admits the second reason a list can be empty here: `/devices`
 * and `/backup/repositories` carry their own permissions (`devices.read`,
 * `backup.read`), independent of `backup.run` and `backup.policy.manage`, so
 * "there are none" and "you may not see them" are both live possibilities and
 * a hint must not assert only the first.
 */
export const devicePickerEmptyHint = (): string => translate("backup.picker.deviceEmptyHint");

export const repositoryPickerEmptyHint = (): string => translate("backup.picker.repositoryEmptyHint");

/**
 * The same empty list for a policy of a client the Repositories page cannot
 * file a repository under: until the client selector exists it files under
 * the internal client only (PHASE 3), so the hint names where that client's
 * repository is made instead, and the editor offers no link.
 */
export const repositoryPickerEmptyHintElsewhere = (): string =>
  translate("backup.picker.repositoryEmptyHintElsewhere");

/**
 * The same two sentences as load-time snapshots, kept only because the
 * module's unit tests import the names. Components call the functions above,
 * which follow the language.
 */
export const DEVICE_PICKER_EMPTY_HINT = devicePickerEmptyHint();
export const REPOSITORY_PICKER_EMPTY_HINT = repositoryPickerEmptyHint();

/**
 * A device group, for a policy's scope. Archived groups are excluded -- see
 * `modules/deploy/pickerOptions.ts::groupPickerOptions` for why. This picker
 * always has one more entry than the fetched list: the caller prepends a
 * "Client default" option for the policy with no group, which has no group id.
 */
export function groupPickerOptions(groups: readonly DeviceGroup[]): PickerOption[] {
  return groups
    .filter((group) => group.archived_at === null)
    .map((group) => ({
      id: group.id,
      label: group.name,
      sublabel: translate("backup.picker.groupDevices", { count: group.device_count }),
    }));
}

/**
 * An endpoint, with the two facts that decide whether it is the right one.
 *
 * `os_family · agent_status` — `EntityPicker`'s own example, and on THIS
 * module it is not decoration: a backup runs as a signed job on the endpoint,
 * so a device whose agent is offline queues one that will not start, and a
 * device with no agent row at all (a switch, stock in a box, an asset entered
 * by hand) can never run one. Saying so in the list is cheaper than saying it
 * in an incident review.
 */
export function devicePickerOptions(devices: readonly Device[]): PickerOption[] {
  return devices.map((device) => {
    const agent =
      device.agent_status === "online"
        ? translate("backup.picker.agentOnline")
        : device.agent_status === "offline"
          ? translate("backup.picker.agentOffline")
          : (device.agent_status ?? translate("backup.picker.noAgent"));
    const parts = [device.os_family, agent].filter(
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
 * A repository, with what tells two of them apart.
 *
 * The storage ref is the distinguishing fact — two repositories named for
 * their intent ("primary", "offsite") differ by the bucket they write to, and
 * pointing a policy at the wrong one is a backup that exists somewhere nobody
 * looks. `state` is the second: a `disabled` repository is one somebody
 * retired, and binding a policy to it is a schedule that writes nowhere.
 */
export function repositoryPickerOptions(repositories: readonly Repository[]): PickerOption[] {
  return repositories.map((repository) => ({
    id: repository.id,
    label: repository.name || repository.id,
    sublabel: [
      repository.storage_ref,
      repository.state === "active"
        ? translate("backup.picker.repoActive")
        : repository.state === "disabled"
          ? translate("backup.picker.repoDisabled")
          : repository.state,
    ]
      .filter(Boolean)
      .join(" · "),
  }));
}

/**
 * The name behind an id that is already stored on a row.
 *
 * Falls back to the id, never to blank: a point that HAS a repository must not
 * render as one that does not, and an id whose row the caller cannot read is
 * still more than nothing.
 */
export function nameForOption(
  options: readonly PickerOption[],
  id: string | null | undefined,
): string | null {
  if (!id) return null;
  return options.find((option) => option.id === id)?.label ?? id;
}

/**
 * THE CLIENT A POLICY IS (MSP clients phase 2, Task 13), which is the client
 * whose repositories it may name: a repository serves one client, because its
 * key travels to every device that backs up into it.
 *
 * Taken from the row wherever there is one. With a group chosen, the group's
 * client -- the stored policy's own when it is that group's; with none, the
 * client default being edited is its own client's. A NEW client default has
 * no row to take one from, and is `fallback`'s: the caller's internal client
 * until the client selector exists (phase 3). `null` while the group's client
 * is not known (the groups have not loaded), so nothing is offered.
 */
export function policyClient(
  existing: Pick<BackupPolicy, "client_id" | "device_group_id"> | null,
  deviceGroupId: string,
  groups: readonly Pick<DeviceGroup, "id" | "client_id">[],
  fallback: string | null,
): string | null {
  if (deviceGroupId !== "") {
    if (existing?.device_group_id === deviceGroupId && existing.client_id) {
      return existing.client_id;
    }
    return groups.find((group) => group.id === deviceGroupId)?.client_id ?? null;
  }
  if (existing && existing.device_group_id === null && existing.client_id) {
    return existing.client_id;
  }
  return fallback;
}

/** The repositories a policy of `client` may name: that client's, and only those. */
export function repositoriesOfClient(
  repositories: readonly Repository[],
  client: string | null,
): Repository[] {
  return client === null ? [] : repositories.filter((repository) => repository.client_id === client);
}
