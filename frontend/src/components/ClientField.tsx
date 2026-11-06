/**
 * THE CLIENT FIELD (phase 3 design §3.1, Q1): what a form shows when a new
 * row's client is decided neither by its parent nor by the top bar -- "All
 * my clients" is chosen. Required, and it offers only the person's clients
 * whose `/me` permissions hold the write's code; the Unassigned client never
 * (C10). Nothing is chosen for the person, not even when one client is the
 * only choice: the row goes where they say.
 *
 * TWO WAYS TO DRAW IT, while the forms move to the shared hook:
 *
 * - `<ClientField field={…}/>`: the form's `useNewRowClientField` answer,
 *   drawn as it is -- its options, its pick, its `setPicked`, its `ready`.
 *   The field reads no scope of its own, so there is one source of truth for
 *   what is offered.
 * - `<ClientField code value onChange/>`: the older shape, where the form
 *   keeps the value. Shown where the decision's `needsField` says so (a form reads it from
 *   `useNewRowClientField`; `useNewRowClient` is that decision alone).
 *
 * Either way the form sends the client inside its mutation (`requireKnown`).
 *
 * INVARIANT 6: THIS GATES NOTHING. The server refuses a client where the
 * person lacks the code, whatever this offered.
 */

import { useMemo } from "react";

import { useT } from "../i18n/context";
import type { MeClient } from "../lib/api";
import { useClientScope } from "../lib/clientScope";
import { clientsHolding, type NewRowClientField } from "../lib/clientSelection";
import { Field, inputClass } from "./ui";

export type ClientFieldProps =
  | {
      /** The form's `useNewRowClientField(code, …)`. */
      field: NewRowClientField;
      code?: never;
      value?: never;
      onChange?: never;
    }
  | {
      field?: never;
      /** The permission the create route checks: `ticket.write`, `docs.manage`… */
      code: string;
      /** The client chosen, or `""`. */
      value: string;
      onChange: (clientId: string) => void;
    };

export function ClientField(props: ClientFieldProps) {
  if (props.field !== undefined) {
    const { field } = props;
    return (
      <ClientSelect
        options={field.options}
        ready={field.ready}
        value={field.picked}
        onChange={field.setPicked}
      />
    );
  }
  return <ScopedClientField code={props.code} value={props.value} onChange={props.onChange} />;
}

/** The older shape: the options read here, from the person's scope. */
function ScopedClientField({
  code,
  value,
  onChange,
}: {
  code: string;
  value: string;
  onChange: (clientId: string) => void;
}) {
  const { ready, clients } = useClientScope();
  const options = useMemo(() => clientsHolding(clients, code), [clients, code]);
  return <ClientSelect options={options} ready={ready} value={value} onChange={onChange} />;
}

function ClientSelect({
  options,
  ready,
  value,
  onChange,
}: {
  options: readonly MeClient[];
  ready: boolean;
  value: string;
  onChange: (clientId: string) => void;
}) {
  const t = useT();
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
