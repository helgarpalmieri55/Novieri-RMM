/**
 * A client's details as a form: "New client" (with the optional modules, the
 * one moment they are chosen here) and "Edit" (the modules have their own
 * tab). Only the name is required; the server validates the domains and
 * refuses one another client holds (409), and that sentence is shown as is.
 */
import { useState } from "react";

import { Form, FormActions, attemptProblems } from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import { useStepUp } from "../../components/stepUp";
import { Button, Dialog, Field, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import type { ClientOut } from "../../lib/api";
import { useCreateClientMutation, useUpdateClientMutation } from "./api";
import { OPTIONAL_MODULES, type OptionalModule, moduleLabel, parseDomains, parseTenants } from "./labels";

const clientRules = (): readonly FieldRule[] => [{ name: "name", label: translate("clients.form.name") }];

export function ClientFormDialog({
  open,
  client,
  onClose,
  onSaved,
}: {
  open: boolean;
  /** `null` creates a client; a client edits it. */
  client: ClientOut | null;
  onClose: () => void;
  onSaved: (saved: ClientOut) => void;
}) {
  const t = useT();
  const create = useCreateClientMutation();
  // A change to the client's Microsoft tenants needs the second factor
  // confirmed in the last 10 minutes (design §2.5, S1); the rest of the
  // edit never does, and the server says which.
  const stepUp = useStepUp();
  const update = useUpdateClientMutation(client?.id ?? "", stepUp.run);
  const mutation = client ? update : create;
  const [name, setName] = useState(client?.name ?? "");
  const [domains, setDomains] = useState((client?.email_domains ?? []).join("\n"));
  const [tenants, setTenants] = useState((client?.microsoft_tenants ?? []).join("\n"));
  const [phone, setPhone] = useState(client?.phone ?? "");
  const [address, setAddress] = useState(client?.address ?? "");
  const [website, setWebsite] = useState(client?.website ?? "");
  const [notes, setNotes] = useState(client?.notes ?? "");
  const [modules, setModules] = useState<ReadonlySet<OptionalModule>>(new Set());
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  // THE UNASSIGNED CLIENT TAKES NO EMAIL DOMAINS (phase 3 final review C, T12
  // minor): a typed one is the server's 422. The field is not offered and the
  // edit sends none. Nor Microsoft tenants (own-auth phase 2): it has no
  // contacts to sign in.
  const takesDomains = client?.kind !== "unassigned";

  function toggle(module: OptionalModule) {
    setModules((current) => {
      const next = new Set(current);
      if (next.has(module)) next.delete(module);
      else next.add(module);
      return next;
    });
  }

  function submit() {
    mutation.reset();
    const next: Attempt = { rules: clientRules, values: { name } };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    const fields = {
      name: name.trim(),
      phone: phone.trim(),
      address: address.trim(),
      website: website.trim(),
      notes,
    };
    const emailDomains = parseDomains(domains);
    if (client) {
      update.mutate(
        takesDomains
          ? { ...fields, email_domains: emailDomains, microsoft_tenants: parseTenants(tenants) }
          : fields,
        { onSuccess: onSaved },
      );
    } else {
      create.mutate(
        {
          ...fields,
          email_domains: emailDomains,
          enabled_modules: OPTIONAL_MODULES.filter((module) => modules.has(module)),
        },
        { onSuccess: onSaved },
      );
    }
  }

  // The form's dialog, then the confirmation outside it: it holds forms of
  // its own, and Escape answers it alone while it is open.
  const editor = (
    <Dialog
      open={open}
      onClose={onClose}
      title={client ? t("clients.form.editTitle") : t("clients.form.createTitle")}
      dismissible={!stepUp.open}
    >
      <Form onSubmit={submit}>
        <div className="form-grid">
          <Field label={t("clients.form.name")}>
            <input className={inputClass} value={name} onChange={(event) => setName(event.target.value)} />
          </Field>
          {takesDomains ? (
            <Field label={t("clients.form.emailDomains")} help={t("clients.form.emailDomainsHelp")} full>
              <textarea
                className={inputClass}
                rows={3}
                value={domains}
                onChange={(event) => setDomains(event.target.value)}
              />
            </Field>
          ) : null}
          {/* Editing only: `ClientCreate` takes no tenants (own-auth phase 2, Task 5). */}
          {client !== null && takesDomains ? (
            <Field label={t("clients.form.microsoftTenants")} help={t("clients.form.microsoftTenantsHelp")} full>
              <textarea
                className={inputClass}
                rows={2}
                spellCheck={false}
                value={tenants}
                onChange={(event) => setTenants(event.target.value)}
              />
            </Field>
          ) : null}
          <Field label={t("clients.form.phone")}>
            <input className={inputClass} value={phone} onChange={(event) => setPhone(event.target.value)} />
          </Field>
          <Field label={t("clients.form.website")}>
            <input className={inputClass} value={website} onChange={(event) => setWebsite(event.target.value)} />
          </Field>
          <Field label={t("clients.form.address")} full>
            <input className={inputClass} value={address} onChange={(event) => setAddress(event.target.value)} />
          </Field>
          <Field label={t("clients.form.notes")} full>
            <textarea
              className={inputClass}
              rows={3}
              value={notes}
              onChange={(event) => setNotes(event.target.value)}
            />
          </Field>
          {client === null ? (
            <fieldset className="field full">
              <legend>{t("clients.form.modules")}</legend>
              {OPTIONAL_MODULES.map((module) => (
                <label key={module} className="flex items-center gap-2">
                  <input type="checkbox" checked={modules.has(module)} onChange={() => toggle(module)} />
                  <span>{moduleLabel(module)}</span>
                </label>
              ))}
            </fieldset>
          ) : null}
        </div>
        <div className="flex flex-wrap items-center justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>
            {t("common.actions.cancel")}
          </Button>
          <FormActions
            label={client ? t("clients.form.save") : t("clients.form.create")}
            pendingLabel={client ? t("clients.form.saving") : t("clients.form.creating")}
            pending={mutation.isPending}
            error={mutation.error}
            failedMessage={client ? t("clients.form.saveFailed") : t("clients.form.createFailed")}
            problems={attemptProblems(attempt)}
          />
        </div>
      </Form>
    </Dialog>
  );
  return (
    <>
      {editor}
      {stepUp.dialog}
    </>
  );
}
