/**
 * The lists a continuity plan points at, as picker options.
 *
 * WHY THIS FILE (UX remediation cycles 6 and 7). The New plan dialog asked for
 * two sets of ids as free text, and both were worse than the audit could see
 * from the outside:
 *
 *   Auditor B, [MEDIUM], /bcp → New plan: "procedure refs are a comma-separated
 *   free-text string rather than a repeatable field or a link picker...
 *   Comma-separated free text is a classic source of dirty data and cannot be
 *   validated or linked. Make procedure refs a repeatable field or a picker
 *   over /docs articles, which already exist."
 *
 * THE FIELD WAS NOT MERELY DIRTY, IT WAS DEAD. `bcp/service.py`'s
 * `_validate_procedure_refs` resolves EVERY entry against `doc_articles.id`
 * org-scoped and raises `BcpValidationError` -- HTTP 400 -- for anything that
 * is not a UUID of an article in this org. So the operator who typed
 * "Runbook A, Failover checklist" into a field labelled "Procedure refs
 * (comma-separated)" could not save the plan at all, and the label invited
 * exactly that. The same applies to the Scope rows: `_validate_scope` accepts
 * `kind` of only "device" or "group" and an `id` that resolves under the org,
 * while the dialog seeded new rows with the kind "device_group" -- a value the
 * server has never accepted.
 *
 * Pure functions, kept out of the dialog, because the SUBLABEL is the part that
 * carries the safety and it deserves a test of its own rather than an
 * inspection of a JSX tree.
 */

import type { PickerOption } from "../../components/picker";
import type { Device, DeviceGroup, DocArticleListItem } from "../../lib/api";
import { translate } from "../../i18n/core";
import { formatDate } from "../../lib/datetime";

// WHERE EACH LIST IS FILLED is a link beside the picker (`PlanEditor`), not a
// menu path in this sentence: the paths these hints used to name ("Devices →
// Enrollment", "Devices → Groups", "Governance → IT documentation") stopped
// matching the navigation, and a hint cannot carry a link.
export const scopeDevicePickerEmptyHint = (): string => translate("bcp.picker.deviceEmptyHint");

export const scopeGroupPickerEmptyHint = (): string => translate("bcp.picker.groupEmptyHint");

export const procedurePickerEmptyHint = (): string => translate("bcp.picker.procedureEmptyHint");

/**
 * An endpoint, with the facts that decide whether it is the right one.
 *
 * `os_family · agent_status` -- the picker header's own example, and the same
 * pair `modules/deploy/pickerOptions.ts` shows. It reads differently here:
 * scoping a plan does not dispatch anything, but the scope is what
 * `record_test` resolves to freeze the backup-evidence snapshot, so the wrong
 * machine in the scope is a DR evidence pack that proves the wrong thing.
 */
export function scopeDevicePickerOptions(devices: readonly Device[]): PickerOption[] {
  return devices.map((device) => {
    const agent =
      device.agent_status === "online"
        ? translate("bcp.picker.agentOnline")
        : device.agent_status === "offline"
          ? translate("bcp.picker.agentOffline")
          : (device.agent_status ?? translate("bcp.picker.noAgent"));
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
 * A device group, for a plan's scope. Archived groups are excluded -- see
 * `modules/deploy/pickerOptions.ts::groupPickerOptions` for why.
 */
export function scopeGroupPickerOptions(groups: readonly DeviceGroup[]): PickerOption[] {
  return groups
    .filter((group) => group.archived_at === null)
    .map((group) => ({
      id: group.id,
      label: group.name,
      sublabel: translate("bcp.picker.groupDevices", { count: group.device_count }),
    }));
}

/**
 * A documentation article, with the facts that decide whether it is the
 * procedure this plan means.
 *
 * CATEGORY AND LAST UPDATE, because "Failover" exists in three categories in
 * any real docs tree and the one a continuity plan cites is the current one. An
 * ARCHIVED article is called out: `docs` keeps archived articles resolvable, so
 * the server accepts the reference happily and a plan can end up pointing at a
 * procedure nobody maintains any more.
 */
export function procedurePickerOptions(
  articles: readonly DocArticleListItem[],
  zone: string | null,
): PickerOption[] {
  return articles.map((article) => ({
    id: article.id,
    label: article.title,
    sublabel: [
      article.category,
      article.archived ? translate("bcp.picker.archived") : null,
      formatDate(article.updated_at, zone),
    ]
      .filter((part): part is string => Boolean(part))
      .join(" · "),
  }));
}

/**
 * The title behind an id already stored on a plan.
 *
 * Falls back to the id, never to blank: an article the caller cannot read is
 * still better identified by its id than by nothing, and a plan that HAS a
 * procedure reference must never render as though it has none.
 */
export function labelForOption(options: readonly PickerOption[], id: string): string {
  return options.find((option) => option.id === id)?.label ?? id;
}
