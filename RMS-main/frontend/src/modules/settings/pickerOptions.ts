/**
 * The device group a role grant's scope can name (cycle 9).
 *
 * WHY THIS EXISTS. The grant form took a device group as a pasted id with a
 * help line explaining why -- "no endpoint serves device groups yet" was
 * true when it was written and stopped being true once `GET
 * /api/v1/device-groups` shipped for the Device groups page. The picker
 * below points at the same endpoint that page already lists from.
 */

import type { PickerOption } from "../../components/picker";
import { translate } from "../../i18n/core";
import type { DeviceGroup } from "../../lib/api";

/** Worded when it is read (a constant would freeze the language the page started in). */
export function groupPickerEmptyHint(): string {
  return translate("settings.groupPicker.emptyHint");
}

/**
 * Archived groups are excluded -- see
 * `modules/deploy/pickerOptions.ts::groupPickerOptions` for why.
 */
export function groupPickerOptions(groups: readonly DeviceGroup[]): PickerOption[] {
  return groups
    .filter((group) => group.archived_at === null)
    .map((group) => ({
      id: group.id,
      label: group.name,
      sublabel: translate("settings.groupPicker.devices", { count: group.device_count }),
    }));
}
