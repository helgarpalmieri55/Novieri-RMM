import type { FieldRule, Problem } from "../../components/form";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import type { NewRowClientField } from "../../lib/clientSelection";

/**
 * What an IPAM create form says about its new row's client, beside the
 * shared hook `useNewRowClientField` (operator decision 2, 2026-11-02).
 *
 * - `clientRule`: with "All my clients" and no parent deciding, the Client
 *   field is required, and Save names it -- "Client is required" -- rather
 *   than sending nothing behind `ClientNotChosen`'s "has not been chosen".
 * With Unassigned chosen at the top a form shows `components/UnassignedRefusal`
 * in place of a create that can only fail.
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
