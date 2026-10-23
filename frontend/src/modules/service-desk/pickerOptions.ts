/**
 * Turning the two lists this module has to point at into picker options.
 *
 * WHY THIS FILE (UX remediation cycle 6). Auditor A, HIGH, on this exact
 * dialog: "Three raw ID fields in one dialog — Requester ID, Assign to,
 * Device ID — all free text with no picker ... No human knows a colleague's
 * UUID or a laptop's UUID, and there is no in-product way to look one up
 * short of navigating elsewhere and copying from an address bar. In practice
 * these fields will be left blank — which for the mandatory Requester ID
 * means tickets simply cannot be created."
 *
 * Pure functions, separate from the pages, because the SUBLABEL is the part
 * that carries the safety and it deserves to be pinned by a test rather than
 * inspected in a JSX tree. `EntityPicker`'s own header: the sublabel "is
 * also what stops somebody dispatching to a machine that cannot answer".
 */

import type { PickerOption } from "../../components/picker";
import { translate } from "../../i18n/core";
import type { Device } from "../../lib/api";
import { humaniseEnum, humaniseStatus } from "../../lib/labels";
import type { UserOut } from "../settings/types";

/**
 * WHAT WOULD FILL THESE LISTS (cycle 7). A picker that is empty on a fresh
 * install and says nothing is the blocked-flow finding wearing a nicer
 * control. Both hints name the console surface that populates the list AND
 * the other reason a list can be empty here — these calls carry their own
 * permissions (`/users`, `devices.read`), independent of `ticket.write`, so
 * "there are none" and "you may not see them" are both live possibilities
 * and the hint must not assert the first.
 */
export const userPickerEmptyHint = (): string => translate("serviceDesk.picker.userEmptyHint");

export const devicePickerEmptyHint = (): string => translate("serviceDesk.picker.deviceEmptyHint");

const ACCOUNT_STATUSES: ReadonlySet<string> = new Set(["active", "disabled", "deleted"]);

/** An account's status in lower case, for the end of a sublabel. */
function accountStatusWord(status: string): string {
  const label = ACCOUNT_STATUSES.has(status) ? humaniseEnum(status, "UserStatus") : humaniseStatus(status);
  return label.toLocaleLowerCase();
}

/**
 * A person, as a technician would recognise them.
 *
 * The sublabel is the email because two colleagues share a display name far
 * more often than they share an address, and because a `disabled` or
 * `deleted` account is one you should not be assigning work to — `UserOut.
 * status` is retained for deleted rows precisely so it can be seen.
 */
export function userPickerOptions(users: readonly UserOut[]): PickerOption[] {
  return users.map((user) => ({
    id: user.id,
    label: user.display_name || user.email || user.id,
    sublabel: user.status === "active" ? user.email : `${user.email} · ${accountStatusWord(user.status)}`,
  }));
}

/**
 * An endpoint, with the two facts that decide whether it is the right one.
 *
 * `os_family · agent_status` — the picker header's own example, and the
 * reason it is not decoration: linking a ticket to an offline machine is how
 * a technician spends twenty minutes waiting on a device that was never
 * going to answer. A device with no agent row has neither field, and says
 * so rather than rendering an empty separator.
 */
export function devicePickerOptions(devices: readonly Device[]): PickerOption[] {
  return devices.map((device) => {
    const agent =
      device.agent_status === "online"
        ? translate("serviceDesk.picker.agentOnline")
        : device.agent_status === "offline"
          ? translate("serviceDesk.picker.agentOffline")
          : translate("serviceDesk.picker.noAgent");
    const parts = [device.os_family, agent].filter((part): part is string => Boolean(part));
    return {
      id: device.id,
      label: device.hostname ?? device.fqdn ?? device.id,
      sublabel: parts.join(" · "),
    };
  });
}

/**
 * The name behind an id that is already stored on a ticket.
 *
 * Auditor B, HIGH, on the same shape elsewhere: "The audit log identifies
 * every actor and target by raw UUID only — you cannot tell who did what."
 * The ticket detail page rendered `requester_id` and `assigned_to` the same
 * way. Falls back to the id, never to blank: an id whose row the caller
 * cannot read is still more than nothing, and silently printing "—" for a
 * ticket that IS assigned would be the worse lie.
 */
export function nameForOption(options: readonly PickerOption[], id: string | null): string | null {
  if (!id) return null;
  return options.find((option) => option.id === id)?.label ?? id;
}
