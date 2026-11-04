import type { FieldRule, Problem } from "../../components/form";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import type { NewRowClientField } from "../../lib/clientSelection";
import { Icon } from "../../shell/icons";

/**
 * What an IPAM create form says about its new row's client, beside the
 * shared hook `useNewRowClientField` (operator decision 2, 2026-11-02).
 *
 * - `clientRule`: with "All my clients" and no parent deciding, the Client
 *   field is required, and Save names it -- "Client is required" -- rather
 *   than sending nothing behind `ClientNotChosen`'s "has not been chosen".
 * - `UnassignedRefused`: with Unassigned chosen at the top, nothing new can be
 *   filed (the server refuses it, and the header confines the request to
 *   Unassigned, so a Client field could not help); the form says so in place
 *   of a create that can only fail.
 */
export function clientRule(client: NewRowClientField): FieldRule[] {
  return client.needsField ? [{ name: "client", label: translate("components.clientField.label") }] : [];
}

/** The problems of a refused attempt, as this module's `ErrorLine` draws a failure. */
export function ProblemLine({ problems }: { problems: readonly Problem[] }) {
  useT();
  if (problems.length === 0) return null;
  return (
    <p role="alert" className="form-error">
      {problems.map((problem) => problem.message).join(". ")}.
    </p>
  );
}

export function UnassignedRefused() {
  const t = useT();
  return (
    <div className="notice" role="note">
      <Icon name="info" size={16} />
      <p>{t("ipam.unassignedRefused")}</p>
    </div>
  );
}
