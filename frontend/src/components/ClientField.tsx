/**
 * THE CLIENT FIELD (phase 3 design §3.1, Q1): what a form shows when a new
 * row's client is decided neither by its parent nor by the top bar -- "All
 * my clients" is chosen. Required, and it offers only the person's clients
 * whose `/me` permissions hold `code`, the code of that write; the Unassigned
 * client never (C10). Nothing is chosen for the person, not even when one
 * client is the only choice: the row goes where they say.
 *
 * Shown where `useNewRowClient(code).needsField` says so; the form keeps the
 * value and sends it inside its mutation (`requireKnown`).
 *
 * INVARIANT 6: THIS GATES NOTHING. The server refuses a client where the
 * person lacks the code, whatever this offered.
 */

import { useT } from "../i18n/context";
import { useClientScope } from "../lib/clientScope";
import { useNewRowClient } from "../lib/clientSelection";
import { Field, inputClass } from "./ui";

export function ClientField({
  code,
  value,
  onChange,
}: {
  /** The permission the create route checks: `ticket.write`, `docs.manage`… */
  code: string;
  /** The client chosen, or `""`. */
  value: string;
  onChange: (clientId: string) => void;
}) {
  const t = useT();
  const { ready } = useClientScope();
  const { options } = useNewRowClient(code);
  return (
    <Field
      label={t("components.clientField.label")}
      // "None" only once `/me` has answered: before it every person has no
      // clients. And `Field` wraps its control only while it has help, so a
      // help shown during that wait and dropped after it would remount the
      // <select> the moment the clients arrive.
      help={ready && options.length === 0 ? t("components.clientField.none") : undefined}
    >
      <select
        className={inputClass}
        required
        value={value}
        onChange={(event) => onChange(event.target.value)}
      >
        <option value="">{t("components.clientField.choose")}</option>
        {options.map((client) => (
          <option key={client.id} value={client.id}>
            {client.name}
          </option>
        ))}
      </select>
    </Field>
  );
}
