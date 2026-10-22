// The organisation's Azure app registration, which Microsoft 365 mailboxes
// sign in through. The client secret is stored encrypted by this platform and
// is never shown: the box always starts empty and is sent only when typed.
// Design: docs/superpowers/specs/2026-09-11-mail-oauth-design.md §4.4.

import { useEffect, useState } from "react";
import { attemptProblems, Form, FormActions } from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import { Field, inputClass } from "../../components/ui";
import { Trans } from "../../i18n/Trans";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import { ApiError } from "../../lib/api";
import { KIND_ICON, Panel } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { useEmailOAuthApp, useSaveEmailOAuthAppMutation } from "./api";
import type { OAuthAppUpdate } from "./types";

// A function, so the labels are worded when a refusal is rendered. The client
// secret is checked for PRESENCE only: the snapshot a refusal holds carries
// "set" or nothing, never the secret.
const connectionRules = (): readonly FieldRule[] => [
  { name: "tenantId", label: translate("emailIntake.microsoft.tenant") },
  { name: "clientId", label: translate("emailIntake.microsoft.client") },
  { name: "clientSecret", label: translate("emailIntake.microsoft.secret") },
];

export function MicrosoftConnection({ canAdmin }: { canAdmin: boolean }) {
  const t = useT();
  const app = useEmailOAuthApp();
  const save = useSaveEmailOAuthAppMutation();
  const [tenantId, setTenantId] = useState("");
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [attempt, setAttempt] = useState<Attempt | null>(null);

  const loadedTenant = app.data?.tenant_id ?? "";
  const loadedClient = app.data?.client_id ?? "";
  useEffect(() => {
    setTenantId(loadedTenant);
    setClientId(loadedClient);
  }, [loadedTenant, loadedClient]);

  // A reader who may not see this, or a server older than this panel, is
  // not an error worth a banner on a page about mailboxes.
  if (app.error instanceof ApiError && (app.error.status === 403 || app.error.status === 404)) {
    return null;
  }

  const hasSecret = app.data?.has_secret === true;

  function submit() {
    // Present when one is stored already or one was typed; the value itself is
    // never kept in the snapshot.
    const secretPresent = hasSecret || clientSecret !== "";
    const next: Attempt = {
      rules: connectionRules,
      values: { tenantId, clientId, clientSecret: secretPresent ? "set" : "" },
    };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    const body: OAuthAppUpdate = { tenant_id: tenantId.trim(), client_id: clientId.trim() };
    // The omission is the feature: an untouched box keeps the stored secret.
    if (clientSecret !== "") body.client_secret = clientSecret;
    save.mutate(body, { onSuccess: () => setClientSecret("") });
  }

  return (
    <div id="microsoft">
      <Panel
        title={t("emailIntake.microsoft.title")}
        subtitle={t("emailIntake.microsoft.subtitle")}
      >
        {app.isLoading ? (
          <p className="note">{t("emailIntake.common.loading")}</p>
        ) : (
          <Form onSubmit={submit}>
            {app.data && app.data.redirect_uri === null ? (
              <p role="status" className="notice gold" style={{ marginBottom: 12 }}>
                <Icon name={KIND_ICON.warn} size={16} />
                <span>
                  {t("emailIntake.microsoft.switchedOff")}
                </span>
              </p>
            ) : null}
            {app.data && app.data.redirect_uri === null ? (
              <details className="form-hint" style={{ marginBottom: 12 }}>
                <summary>{t("emailIntake.microsoft.adminNeeds")}</summary>
                <Trans k="emailIntake.microsoft.adminSteps" components={{ code: <code className="mono" /> }} />
              </details>
            ) : null}
            <div className="form-grid">
              <Field label={t("emailIntake.microsoft.tenant")} help={t("emailIntake.microsoft.tenantHelp")}>
                <input
                  className={inputClass}
                  value={tenantId}
                  disabled={!canAdmin}
                  autoComplete="off"
                  placeholder={t("emailIntake.microsoft.tenantPlaceholder")}
                  onChange={(e) => setTenantId(e.target.value)}
                />
              </Field>
              <Field label={t("emailIntake.microsoft.client")} help={t("emailIntake.microsoft.clientHelp")}>
                <input
                  className={inputClass}
                  value={clientId}
                  disabled={!canAdmin}
                  autoComplete="off"
                  onChange={(e) => setClientId(e.target.value)}
                />
              </Field>
              <Field
                label={t("emailIntake.microsoft.secret")}
                full
                help={hasSecret ? t("emailIntake.microsoft.secretHelpSet") : t("emailIntake.microsoft.secretHelpNotSet")}
              >
                <input
                  className={inputClass}
                  type="password"
                  autoComplete="new-password"
                  value={clientSecret}
                  disabled={!canAdmin}
                  onChange={(e) => setClientSecret(e.target.value)}
                />
              </Field>
            </div>
            {app.data?.redirect_uri ? (
              <div className="field" style={{ marginTop: 12 }}>
                <span>{t("emailIntake.microsoft.returnAddress")}</span>
                <code className="mono" style={{ userSelect: "all", overflowWrap: "anywhere" }}>
                  {app.data.redirect_uri}
                </code>
                <span className="help">
                  {t("emailIntake.microsoft.returnHelp")}
                </span>
              </div>
            ) : null}
            {canAdmin ? (
              <div className="flex items-center justify-end gap-2" style={{ marginTop: 12 }}>
                <FormActions
                  label={t("emailIntake.microsoft.save")}
                  pending={save.isPending}
                  error={save.error}
                  saved={save.isSuccess}
                  problems={attemptProblems(attempt)}
                />
              </div>
            ) : null}
          </Form>
        )}
      </Panel>
    </div>
  );
}
