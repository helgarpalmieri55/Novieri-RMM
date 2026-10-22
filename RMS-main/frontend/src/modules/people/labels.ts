// Words on screen for the people module's wire values
// (docs/design/settings-language.md: no wire value reaches the screen).

import { translate } from "../../i18n/core";
import { lazyLabels } from "../../i18n/lazy";
import type { Kind } from "../../shell/viz";
import type { ChecklistKind, PersonStatus } from "./types";

// The label tables read the catalogue when a field is read, so a held table
// follows a language switch.
export const STATUS_LABEL: Readonly<Record<PersonStatus, string>> = lazyLabels({
  starting: "people.status.starting",
  active: "people.status.active",
  leaving: "people.status.leaving",
  left: "people.status.left",
});

export const STATUS_KIND: Record<PersonStatus, Kind> = {
  starting: "run",
  active: "ok",
  leaving: "warn",
  left: "mut",
};

export const CHECKLIST_LABEL: Readonly<Record<ChecklistKind, string>> = lazyLabels({
  starter: "people.checklistKind.starter",
  leaver: "people.checklistKind.leaver",
});

/** How we know who uses a computer. */
export function linkSourceLabel(source: string | null | undefined): string {
  if (source === "operator") return translate("people.linkSource.operator");
  if (source === "inventory") return translate("people.linkSource.inventory");
  return translate("people.linkSource.unknown");
}

/**
 * The note after a device's "Assigned to", lower case because it follows a
 * name: "Assigned to: Ana Ruiz · set by hand". Null when there is nothing to
 * add -- inventory that resolved nobody already reads "nobody yet".
 */
export function assignedSourceNote(
  source: string | null | undefined,
  hasPerson: boolean,
): string | null {
  if (source === "operator") return translate("people.assignedNote.operator");
  if (source === "inventory" && hasPerson) return translate("people.assignedNote.inventory");
  return null;
}

const ACCOUNT_STATUS: Readonly<Record<string, string>> = lazyLabels({
  active: "people.accountStatus.active",
  disabled: "people.accountStatus.disabled",
  deleted: "people.accountStatus.deleted",
});

export function accountStatusLabel(status: string): string {
  return ACCOUNT_STATUS[status] ?? translate("people.accountStatus.unknown");
}

const LIFECYCLE: Readonly<Record<string, string>> = lazyLabels({
  in_service: "people.lifecycle.in_service",
  stock: "people.lifecycle.stock",
  unmanageable: "people.lifecycle.unmanageable",
  retired: "people.lifecycle.retired",
});

export function lifecycleLabel(lifecycle: string): string {
  return LIFECYCLE[lifecycle] ?? translate("people.lifecycle.in_service");
}
