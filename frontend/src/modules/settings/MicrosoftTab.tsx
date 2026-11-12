// Microsoft sign-in -- `GET /settings/microsoft-sign-in` (`orgs.manage`), `PUT`
// (`orgs.manage` on EVERY client): the one application Novieri registered in
// Microsoft Entra ID, and Novieri's own tenant (own-auth design §4; plan
// D33-D34). One setting for the whole organisation, like Email and Sessions.
//
// THE PLATFORM PROVES ITSELF WITH A CERTIFICATE IT MADE (operator decision
// C4; Task 12, decision 6). Nothing secret is typed here: the server makes
// the key pair, keeps the private key in the vault (invariant 3), and answers
// the certificate -- public -- to be uploaded to the application in Entra
// ("Certificates & secrets", "Certificates", "Upload certificate"). This tab
// shows it, its thumbprint and when it expires, and asks for a new one.
// A RENEWAL IS TWO ACTS, WITH NO GAP (Task 5, decision 5): a new certificate
// waits beside the one in use, which keeps signing, while a person uploads
// it to Entra; "I uploaded it" switches to it. Switching before the upload is
// the one way to break sign-in, which the tab says beside the button.
//
// EVERY SAVE NEEDS A RECENT SECOND FACTOR (design §2.5, operator 2026-11-08,
// S1): a save, a new certificate and the switch to the waiting one. The server
// answers 403 `step_up_required` without it, and `useStepUp` asks and sends
// the same save once more.
//
// THE THUMBPRINT is the SHA-256 one each sign-in's assertion names
// (`x5t#S256`), base64url. The tab does not say it is the one the Entra
// portal shows, which may be another (f2-3 M6).
//
// THE REDIRECT ADDRESS is the server's (`ITS_PUBLIC_CONSOLE_URL` +
// `/sign-in/microsoft`), shown here so it can be copied into the
// application's registration, as a Web platform.
//
// A CLIENT'S CONTACTS sign in from the tenants their client lists (Clients ->
// Edit); the tenant here is for technicians only. Each tenant's administrator
// may have to approve the application once (design §4).
//
// THE SERVER DECIDES EVERYTHING (invariant 6): a GUID is checked here only to
// save a round trip, and every refusal -- an application without a
// certificate, a switch with nothing waiting, two first saves at once (409)
// -- is the server's sentence, shown by the save bar.

import { useEffect, useState, type CSSProperties } from "react";

import { useStepUp } from "../../components/stepUp";
import { Field, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { formatDate } from "../../lib/datetime";
import { saveBlob } from "../../lib/download";
import { useOrgZone } from "../../lib/zone";
import { Btn, FieldGroup, KIND_ICON, Panel, Toggle } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { useGlobalWriteReason, useMicrosoftSettings, usePutMicrosoftSettingsMutation } from "./api";
import { FOLDING_FORM_GRID } from "./formGrid";
import { LoadFailure } from "./LoadFailure";
import { SaveBar } from "./SaveBar";
import { isDirty, isForbidden } from "./settingsState";
import type { MicrosoftSettingsOut } from "./types";

/** Read and write are the same code, as on the Email and Sessions tabs. */
export const MICROSOFT_MANAGE = "orgs.manage";

/** A GUID as Microsoft shows one: 8-4-4-4-12 hexadecimal digits. */
const GUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** How long before its expiry the tab starts saying so. */
const EXPIRY_WARNING_DAYS = 30;
const DAY_MS = 24 * 60 * 60 * 1000;

/** A thumbprint has no spaces to break at; at 390px it must still wrap. */
const WRAP_ANYWHERE: CSSProperties = { overflowWrap: "anywhere" };

/** The certificate is a file for Entra's "Upload certificate" (PEM, which it takes as `.cer`). */
const PEM_TYPE = "application/x-pem-file";

interface MicrosoftForm {
  enabled: boolean;
  app_client_id: string;
  staff_tenant_id: string;
}

function seedFrom(data: MicrosoftSettingsOut | undefined): MicrosoftForm {
  return {
    enabled: data?.enabled ?? false,
    app_client_id: data?.app_client_id ?? "",
    staff_tenant_id: data?.staff_tenant_id ?? "",
  };
}

/** "expired", "soon" (within `EXPIRY_WARNING_DAYS`) or null. */
function expiry(at: string | null | undefined, now: number): "expired" | "soon" | null {
  if (!at) return null;
  const left = new Date(at).getTime() - now;
  if (left <= 0) return "expired";
  return left <= EXPIRY_WARNING_DAYS * DAY_MS ? "soon" : null;
}

/** `canRead` reads the tab (`orgs.manage`); `canEdit` writes it: `orgs.manage` on every client. */
export function MicrosoftTab({ canRead, canEdit }: { canRead: boolean; canEdit: boolean }) {
  const t = useT();
  const zone = useOrgZone();
  const reason = useGlobalWriteReason(MICROSOFT_MANAGE);
  const settings = useMicrosoftSettings(canRead);
  const stepUp = useStepUp();
  const update = usePutMicrosoftSettingsMutation(stepUp.run);
  const seed = seedFrom(settings.data);
  const [form, setForm] = useState<MicrosoftForm>(seed);
  // Not part of the seed: a request, not a stored value.
  const [renew, setRenew] = useState(false);
  const seedSignature = JSON.stringify(seed);
  useEffect(() => {
    setForm(JSON.parse(seedSignature) as MicrosoftForm);
  }, [seedSignature]);

  const forbidden = isForbidden(settings.error);
  const failed = settings.error != null && !forbidden;
  const editable = canEdit && settings.data != null;
  const badId = [form.app_client_id, form.staff_tenant_id].some((value) => value.trim() !== "" && !GUID.test(value.trim()));
  const dirty = isDirty(form, seed) || renew;
  const certificate = settings.data?.certificate ?? null;
  const waiting = settings.data?.next_certificate ?? null;
  const ending = expiry(settings.data?.certificate_expires_at, Date.now());
  // Another application ID stops sign-in until its own certificate is uploaded (Task 5, decision 5).
  const typedApp = form.app_client_id.trim().toLowerCase();
  const appChanged = certificate !== null && typedApp !== "" && typedApp !== (settings.data?.app_client_id ?? "").toLowerCase();

  function submit() {
    if (badId) return;
    update.mutate(
      {
        enabled: form.enabled,
        app_client_id: form.app_client_id.trim() || null,
        staff_tenant_id: form.staff_tenant_id.trim() || null,
        new_certificate: renew,
        activate_certificate: false,
      },
      { onSuccess: () => setRenew(false) },
    );
  }

  /** "I uploaded it": the saved values as they are, and the switch. Off while the form has unsaved changes. */
  function activate() {
    update.mutate({
      enabled: seed.enabled,
      app_client_id: seed.app_client_id || null,
      staff_tenant_id: seed.staff_tenant_id || null,
      new_certificate: false,
      activate_certificate: true,
    });
  }

  // The panel, then the confirmation outside it (`components/stepUp.tsx`).
  const panel = (
    <Panel title={t("settings.microsoft.title")} subtitle={t("settings.microsoft.subtitle")}>
      {settings.isLoading ? (
        <p className="note">{t("settings.microsoft.loading")}</p>
      ) : forbidden ? (
        <p className="note">{t("settings.microsoft.forbidden", { code: MICROSOFT_MANAGE })}</p>
      ) : failed ? (
        <LoadFailure error={settings.error} />
      ) : (
        <>
          <Toggle
            checked={form.enabled}
            disabled={!editable}
            label={t("settings.microsoft.enabled")}
            onChange={(next) => setForm((current) => ({ ...current, enabled: next }))}
          />
          <FieldGroup title={t("settings.microsoft.groups.application")} icon="key">
            <div className="form-grid" style={FOLDING_FORM_GRID}>
              <Field label={t("settings.microsoft.appId")}>
                <input
                  className={inputClass}
                  spellCheck={false}
                  value={form.app_client_id}
                  disabled={!editable}
                  onChange={(event) => setForm((current) => ({ ...current, app_client_id: event.target.value }))}
                />
              </Field>
              <Field label={t("settings.microsoft.redirect")} help={t("settings.microsoft.redirectHelp")} full>
                <input
                  className={inputClass}
                  readOnly
                  value={settings.data?.redirect_uri ?? t("settings.microsoft.noConsoleUrl")}
                  onFocus={(event) => event.currentTarget.select()}
                />
              </Field>
            </div>
            {appChanged ? (
              <p className="notice gold" style={{ marginTop: 8 }}>
                <Icon name={KIND_ICON.warn} size={16} />
                <span>{t("settings.microsoft.appChangeWarning")}</span>
              </p>
            ) : null}
          </FieldGroup>
          <FieldGroup title={t("settings.microsoft.groups.certificate")} icon="certificate">
            {certificate ? (
              <>
                <Field label={t("settings.microsoft.certificate")} help={t("settings.microsoft.certificateHelp")} full>
                  <textarea
                    className={inputClass}
                    readOnly
                    rows={4}
                    spellCheck={false}
                    value={certificate}
                    onFocus={(event) => event.currentTarget.select()}
                  />
                </Field>
                <p className="note" style={{ marginTop: 8 }}>
                  {t("settings.microsoft.thumbprint")} <code style={WRAP_ANYWHERE}>{settings.data?.certificate_thumbprint}</code>
                  {" · "}
                  {t("settings.microsoft.expires", { when: formatDate(settings.data?.certificate_expires_at, zone) })}
                </p>
                <div className="form-actions">
                  <Btn
                    small
                    icon="download"
                    label={t("settings.microsoft.download")}
                    onClick={() => saveBlob(new Blob([certificate], { type: PEM_TYPE }), "itms-sign-in.cer")}
                  />
                </div>
              </>
            ) : (
              <p className="note">{t("settings.microsoft.noCertificate")}</p>
            )}
            {waiting ? (
              <div style={{ marginTop: 12 }}>
                <Field label={t("settings.microsoft.waiting")} help={t("settings.microsoft.waitingHelp")} full>
                  <textarea
                    className={inputClass}
                    readOnly
                    rows={4}
                    spellCheck={false}
                    value={waiting}
                    onFocus={(event) => event.currentTarget.select()}
                  />
                </Field>
                <p className="note" style={{ marginTop: 8 }}>
                  {t("settings.microsoft.thumbprint")} <code style={WRAP_ANYWHERE}>{settings.data?.next_certificate_thumbprint}</code>
                  {" · "}
                  {t("settings.microsoft.expires", { when: formatDate(settings.data?.next_certificate_expires_at, zone) })}
                </p>
                <div className="form-actions">
                  <Btn
                    small
                    icon="download"
                    label={t("settings.microsoft.downloadWaiting")}
                    onClick={() => saveBlob(new Blob([waiting], { type: PEM_TYPE }), "itms-sign-in-new.cer")}
                  />
                  <Btn
                    small
                    icon="check"
                    label={t("settings.microsoft.activate")}
                    disabled={!editable || dirty || update.isPending}
                    onClick={activate}
                  />
                </div>
                <p className="notice gold" style={{ marginTop: 8 }}>
                  <Icon name={KIND_ICON.warn} size={16} />
                  <span>{t("settings.microsoft.activateWarning")}</span>
                </p>
              </div>
            ) : null}
            {/* With a new certificate already waiting, the way out is to
                upload it and switch, not to ask for another (fix round 1). */}
            {ending === "expired" ? (
              <p className="notice red" role="alert" style={{ marginTop: 10 }}>
                <Icon name={KIND_ICON.crit} size={16} />
                <span>{t(waiting ? "settings.microsoft.expiredWaiting" : "settings.microsoft.expired")}</span>
              </p>
            ) : ending === "soon" ? (
              <p className="notice gold" style={{ marginTop: 10 }}>
                <Icon name={KIND_ICON.warn} size={16} />
                <span>{t(waiting ? "settings.microsoft.expiresSoonWaiting" : "settings.microsoft.expiresSoon")}</span>
              </p>
            ) : null}
            <label className="flex items-start gap-2" style={{ marginTop: 10 }}>
              <input
                type="checkbox"
                checked={renew}
                disabled={!editable}
                onChange={(event) => setRenew(event.target.checked)}
              />
              <span>{t("settings.microsoft.renew")}</span>
            </label>
            {renew && certificate ? (
              <p className="notice gold" style={{ marginTop: 8 }}>
                <Icon name={KIND_ICON.warn} size={16} />
                <span>{t(waiting ? "settings.microsoft.renewReplaces" : "settings.microsoft.renewWarning")}</span>
              </p>
            ) : null}
          </FieldGroup>
          <FieldGroup title={t("settings.microsoft.groups.staff")} icon="users">
            <div className="form-grid" style={FOLDING_FORM_GRID}>
              <Field label={t("settings.microsoft.staffTenant")} help={t("settings.microsoft.staffTenantHelp")}>
                <input
                  className={inputClass}
                  spellCheck={false}
                  value={form.staff_tenant_id}
                  disabled={!editable}
                  onChange={(event) => setForm((current) => ({ ...current, staff_tenant_id: event.target.value }))}
                />
              </Field>
            </div>
            <p className="note" style={{ marginTop: 8 }}>
              {t("settings.microsoft.contactsNote")}
            </p>
          </FieldGroup>
          {badId ? (
            <p className="form-error" style={{ marginTop: 10 }}>
              <Icon name={KIND_ICON.crit} size={13} /> {t("settings.microsoft.badId")}
            </p>
          ) : null}
          <SaveBar
            canEdit={editable && !badId}
            disabledReason={canEdit ? t("settings.microsoft.correct") : reason}
            dirty={dirty}
            pending={update.isPending}
            error={update.error}
            saved={update.isSuccess && !dirty}
            onSave={submit}
          />
        </>
      )}
    </Panel>
  );
  return (
    <>
      {panel}
      {stepUp.dialog}
    </>
  );
}
