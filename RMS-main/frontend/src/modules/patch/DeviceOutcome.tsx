import { NoValue } from "../../components/emptystate";
import { useT } from "../../i18n/context";
import { translate, type MessageKey } from "../../i18n/core";
import { formatDateTime } from "../../lib/datetime";
import { StatusPill } from "../../shell/components";
import type { PatchUpdate } from "./types";
import { patchStateKind } from "./patchState";

/**
 * WHAT HAPPENED TO THIS UPDATE ON THIS MACHINE — the cell the module did not
 * have, and the reason an operator who applied patches could not tell from the
 * patch page whether they installed.
 *
 * `DevicePatchStatus.state` distinguishes the four outcomes and always has.
 * What no surface did was SHOW it: the device drill-down and the device Patch
 * tab both listed Update / Severity and nothing else, so a `failed` row and a
 * `missing` row were the same three words — and `last_error`, the agent's own
 * account of the failure, was reachable only by downloading the CSV export.
 *
 * THE LABELS ARE NOT THE ENUM. In this column the backend's `missing` means
 * "nothing has tried this yet" and `approved` means "cleared, waiting for its
 * window" — both are "outstanding", and telling them apart from a run that
 * errored is the entire point of the cell. `patchState.ts`'s `patchStateLabel`
 * stays as it is; it labels the *state*, this labels the *outcome*, and the
 * device tab's "By state" card still wants the former.
 */
const OUTCOME_LABELS: Record<string, MessageKey> = {
  missing: "patch.outcome.missing",
  approved: "patch.outcome.approved",
  installed: "patch.outcome.installed",
  failed: "patch.outcome.failed",
  not_applicable: "patch.outcome.not_applicable",
};

export function deviceOutcomeLabel(state: string): string {
  const key = OUTCOME_LABELS[state];
  return key ? translate(key) : state;
}

/**
 * `null` state renders an em dash rather than nothing: the server sends `null`
 * only on a FLEET-scoped row, where no single machine's outcome exists, and a
 * blank cell there would read as missing data rather than as "not applicable
 * to this row".
 */
export function DeviceOutcome({
  update,
  timeZone = null,
}: {
  update: Pick<PatchUpdate, "device_state" | "device_last_error" | "device_applied_at">;
  timeZone?: string | null;
}) {
  const t = useT();
  if (update.device_state == null) return <NoValue label={t("patch.outcome.none")} />;
  return (
    <div>
      <StatusPill
        value={update.device_state}
        kind={patchStateKind(update.device_state)}
        label={deviceOutcomeLabel(update.device_state)}
      />
      {/* WHEN it installed. "Installed" with no date cannot distinguish the
          apply the operator just ran from one three months ago, which is the
          question they opened the page to answer. */}
      {update.device_state === "installed" && update.device_applied_at && (
        <div className="cell-sub mono">{formatDateTime(update.device_applied_at, timeZone)}</div>
      )}
      {/* THE AGENT'S OWN WORDS. Rendered as text, never as markup: this
          string comes off the endpoint and is the one field on the row that
          carries free-form remote content. `break-words` because a Windows
          Update error is a bare hex code and an apt one is a paragraph. The
          crit pill above it is what says "failed"; the words need no second
          colour. */}
      {update.device_state === "failed" && update.device_last_error && (
        <div className="cell-sub mono break-words">{update.device_last_error}</div>
      )}
    </div>
  );
}
