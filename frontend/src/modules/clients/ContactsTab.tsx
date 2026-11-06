/**
 * A client's contacts (`GET/POST /clients/{id}/contacts`, `PATCH
 * /contacts/{id}`): list, add, edit, and switch off and on, with
 * `clients.manage` on this client. A contact uses the portal, never the
 * console; `/users` is staff-only, so this is where a contact's sign-in is
 * turned off and back on.
 */
import { useState } from "react";

import { ConfirmDialog } from "../../components/confirm";
import { NoValue } from "../../components/emptystate";
import { Form, FormActions, attemptProblems, describeSubmitError } from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import { Button, Dialog, Field, Table, Td, Th, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import type { ClientOut, ContactOut } from "../../lib/api";
import { useClientScope } from "../../lib/clientScope";
import { Btn, Panel, QueryState, StatusPill } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { useClientContacts, useCreateContactMutation, useUpdateContactMutation } from "./api";

const contactRules = (): readonly FieldRule[] => [{ name: "email", label: translate("clients.contacts.email") }];

export function ContactsTab({ client }: { client: ClientOut }) {
  const t = useT();
  const { clients } = useClientScope();
  const mine = clients.find((item) => item.id === client.id);
  const canManage =
    client.status === "active" &&
    client.kind !== "unassigned" &&
    (mine?.permissions.includes("clients.manage") ?? false);
  const contacts = useClientContacts(client.id);
  const change = useUpdateContactMutation(client.id);
  const [editing, setEditing] = useState<ContactOut | "new" | null>(null);
  const [turningOff, setTurningOff] = useState<ContactOut | null>(null);
  const rows = contacts.data ?? [];

  if (client.kind === "unassigned") {
    return (
      <Panel title={t("clients.contacts.title")}>
        <p className="note">{t("clients.contacts.unassigned")}</p>
      </Panel>
    );
  }

  return (
    <>
      <Panel
        title={t("clients.contacts.title")}
        subtitle={t("clients.contacts.subtitle")}
        actions={canManage ? <Btn label={t("clients.contacts.add")} icon="plus" onClick={() => setEditing("new")} /> : undefined}
        bare
      >
        <QueryState
          loading={contacts.isLoading}
          error={contacts.error}
          empty={rows.length === 0}
          emptyLabel={t("clients.contacts.empty")}
          emptyIcon="users"
        >
          <Table label={t("clients.contacts.tableLabel")}>
            <thead>
              <tr>
                <Th>{t("clients.contacts.colContact")}</Th>
                <Th>{t("clients.contacts.colPhone")}</Th>
                <Th>{t("clients.contacts.colStatus")}</Th>
                <Th />
              </tr>
            </thead>
            <tbody>
              {rows.map((contact) => {
                const name = contact.display_name || contact.email;
                const active = contact.status === "active";
                return (
                  <tr key={contact.id}>
                    <Td lead={<Icon name="user" size={18} />}>
                      <div className="cell-title">{name}</div>
                      <div className="cell-sub">{contact.email}</div>
                    </Td>
                    <Td>{contact.phone || <NoValue label={t("clients.summary.notSet")} />}</Td>
                    <Td>
                      <StatusPill
                        value={contact.status}
                        kind={active ? "ok" : "mut"}
                        label={active ? t("clients.contacts.active") : t("clients.contacts.disabled")}
                      />
                    </Td>
                    <Td>
                      {canManage ? (
                        <>
                          <Btn
                            small
                            label={t("clients.contacts.edit")}
                            ariaLabel={t("clients.contacts.editAria", { name })}
                            onClick={() => setEditing(contact)}
                          />{" "}
                          {active ? (
                            <Btn
                              small
                              variant="danger"
                              label={t("clients.contacts.turnOff")}
                              ariaLabel={t("clients.contacts.turnOffAria", { name })}
                              onClick={() => {
                                change.reset();
                                setTurningOff(contact);
                              }}
                            />
                          ) : (
                            <Btn
                              small
                              label={t("clients.contacts.turnOn")}
                              ariaLabel={t("clients.contacts.turnOnAria", { name })}
                              onClick={() => {
                                change.reset();
                                change.mutate({ contactId: contact.id, body: { status: "active" } });
                              }}
                            />
                          )}
                        </>
                      ) : null}
                    </Td>
                  </tr>
                );
              })}
            </tbody>
          </Table>
        </QueryState>
        {change.isError ? (
          <p role="alert" className="form-error">
            {t("clients.contacts.changeFailed")} {describeSubmitError(change.error)}
          </p>
        ) : null}
      </Panel>

      {editing !== null ? (
        <ContactFormDialog
          clientId={client.id}
          contact={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
        />
      ) : null}
      <ConfirmDialog
        open={turningOff !== null}
        title={t("clients.contacts.turnOffTitle")}
        subject={turningOff ? turningOff.display_name || turningOff.email : ""}
        body={t("clients.contacts.turnOffBody")}
        confirmLabel={t("clients.contacts.turnOffConfirm")}
        destructive
        onCancel={() => setTurningOff(null)}
        onConfirm={() => {
          const contact = turningOff;
          setTurningOff(null);
          if (contact) change.mutate({ contactId: contact.id, body: { status: "disabled" } });
        }}
      />
    </>
  );
}

function ContactFormDialog({
  clientId,
  contact,
  onClose,
}: {
  clientId: string;
  contact: ContactOut | null;
  onClose: () => void;
}) {
  const t = useT();
  const create = useCreateContactMutation(clientId);
  const update = useUpdateContactMutation(clientId);
  const mutation = contact ? update : create;
  const [email, setEmail] = useState(contact?.email ?? "");
  const [displayName, setDisplayName] = useState(contact?.display_name ?? "");
  const [phone, setPhone] = useState(contact?.phone ?? "");
  const [attempt, setAttempt] = useState<Attempt | null>(null);

  function submit() {
    mutation.reset();
    const next: Attempt = { rules: contactRules, values: { email } };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    const body = { email: email.trim(), display_name: displayName.trim(), phone: phone.trim() || null };
    if (contact) update.mutate({ contactId: contact.id, body }, { onSuccess: onClose });
    else create.mutate(body, { onSuccess: onClose });
  }

  return (
    <Dialog
      open
      onClose={onClose}
      title={contact ? t("clients.contacts.formEditTitle") : t("clients.contacts.formAddTitle")}
    >
      <Form onSubmit={submit}>
        <div className="form-grid">
          <Field label={t("clients.contacts.email")}>
            <input
              className={inputClass}
              type="email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
            />
          </Field>
          <Field label={t("clients.contacts.displayName")}>
            <input
              className={inputClass}
              value={displayName}
              onChange={(event) => setDisplayName(event.target.value)}
            />
          </Field>
          <Field label={t("clients.contacts.phone")}>
            <input className={inputClass} value={phone} onChange={(event) => setPhone(event.target.value)} />
          </Field>
        </div>
        <div className="flex flex-wrap items-center justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>
            {t("common.actions.cancel")}
          </Button>
          <FormActions
            label={t("clients.contacts.save")}
            pendingLabel={t("clients.contacts.saving")}
            pending={mutation.isPending}
            error={mutation.error}
            failedMessage={t("clients.contacts.saveFailed")}
            problems={attemptProblems(attempt)}
          />
        </div>
      </Form>
    </Dialog>
  );
}
