// Email — `GET /settings/email`, `PUT /settings/email`, `POST /settings/email/test`.
// Read and write are both `orgs.manage`.
//
// WHAT THIS TAB REPLACES. Configuring the relay used to mean opening the
// identity provider's admin console — a second product, a second password, and
// an afternoon of the operator's life spent looking for a setting they had no
// reason to know lived somewhere else. Everything that matters about this form
// follows from the one thing that makes it awkward: THE SERVER NEVER RETURNS
// THE PASSWORD, and it is right not to.
//
// So the box is always empty, and "Password: set" is the honest way to report a
// stored credential — a masked value would imply this console received
// something, and it did not. An untouched box sends NOTHING: the server reads an
// absent field as "keep what is stored". A form that sent `password: ""` would
// blank a working credential the first time somebody corrected the port, and the
// only symptom would be invitations that quietly stop arriving.
//
// THE TEST BUTTON REPORTS WHAT HAPPENED AND NOT MORE. The identity provider
// accepting a message is not the relay delivering it — a relay that takes mail
// from an unknown sender and drops it silently is the common case, and this
// platform's own runbook already warns about it. So: "Sent to <mailbox> — check
// that mailbox", never a green tick.
//
// THE TEST GOES WHERE THE OPERATOR SAYS, WITHIN THEIR OWN DOMAINS. The box is
// pre-filled with the operator's own address tagged `+itms-test`: the identity
// provider will not send a test to a sign-in account's own address, and a
// tagged one lands in the same inbox on Microsoft 365 and Gmail. Until
// 2026-09-23 the test could only go to the sending mailbox itself, which an
// operator had to go and open separately. The server refuses any address
// other than the sending address, the operator's own mailbox, or the
// organization's sign-in domain (backend `realm_config`), so this box is not a
// way to mail strangers.
//
// TLS IS ONE CHOICE OF THREE, not two independent switches. A configuration
// carrying both StartTLS and SSL leaves the relay to decide what was meant.
//
// AUTHENTICATION FOLLOWS THE USERNAME. There is no separate "authenticate"
// toggle, because "authenticate, as nobody" is not a state worth being able to
// save.
//
// SIGN-IN IS A PASSWORD OR A MICROSOFT 365 APPLICATION. For Microsoft the
// realm's sign-in service fetches the token itself; the client secret follows
// the password's rule exactly (never shown, sent only when typed). Switching
// modes replaces the other credential server-side, so the console says so
// before the save. Design: docs/superpowers/specs/2026-09-11-mail-oauth-design.md §3.

import { useEffect, useState } from "react";

import { describeSubmitError } from "../../components/form";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import { lazyFields } from "../../i18n/lazy";
import { Field, inputClass } from "../../components/ui";
import { Btn, FieldGroup, KIND_ICON, Panel } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { ChipsField } from "../../shell/viz";
import {
  useEmailSettings,
  useTestEmailMutation,
  useUpdateEmailSettingsMutation,
  useGlobalWriteReason,
} from "./api";
import { FOLDING_FORM_GRID } from "./formGrid";
import { LoadFailure } from "./LoadFailure";
import { SaveBar } from "./SaveBar";
import { isDirty, isForbidden, parseIntegerField } from "./settingsState";
import type { EmailEncryption, EmailSettingsOut, EmailSettingsUpdate } from "./types";

/**
 * Read AND write are the same code here. The named constant is `EMAIL_MANAGE`
 * rather than `ORGS_MANAGE` only because `OrganizationTab` already exports that
 * name for the same string; two exports of one code would be a rename waiting
 * to disagree with itself.
 */
export const EMAIL_MANAGE = "orgs.manage";

/**
 * A failed test send, said as a send rather than as a save.
 *
 * THIS USED TO BE A STRING SURGERY ON ANOTHER FUNCTION'S OUTPUT. The old
 * module-local `describeError` hard-coded the prefix "Save failed", so this
 * sliced that prefix off and glued "Not sent" on — a rewrite that silently did
 * nothing the moment the sentence started with any other word, which is every
 * sentence `describeSubmitError` produces. The kernel's sentences stand alone,
 * so the context is now a LEAD-IN and the reason is quoted, not edited.
 */
function describeSendFailure(error: unknown): string {
  return translate("settings.email.notSent", { reason: describeSubmitError(error) });
}

/** The honest report of a test send, in one place so a test can pin the words. */
export function testSentMessage(recipient: string | null | undefined): string {
  return recipient
    ? translate("settings.email.sentTo", { recipient })
    : translate("settings.email.sentSending");
}

// THE HEDGE IS GONE, NOT THE PORT (language pass, 2026-09-11,
// `docs/design/settings-language.md` "Email"). "Usually port 587" told an
// operator the number might be wrong for them specifically; it never was --
// this is the well-known port for the choice beside it, stated as a fact.
// The labels are accessors over the catalogue, read when the chips are drawn.
const ENCRYPTION_CHOICES: readonly { value: EmailEncryption; label: string }[] = [
  lazyFields({ value: "starttls" as EmailEncryption }, { label: "settings.email.encryptionChoices.starttls" }),
  lazyFields({ value: "ssl" as EmailEncryption }, { label: "settings.email.encryptionChoices.ssl" }),
  lazyFields({ value: "none" as EmailEncryption }, { label: "settings.email.encryptionChoices.none" }),
];

type EmailAuthType = "password" | "token";

const AUTH_CHOICES: readonly { value: EmailAuthType; label: string }[] = [
  lazyFields({ value: "password" as EmailAuthType }, { label: "settings.email.authChoices.password" }),
  lazyFields({ value: "token" as EmailAuthType }, { label: "settings.email.authChoices.token" }),
];

/** Microsoft's default for a mail-sending application; editable under "Advanced". */
const MICROSOFT_SCOPE = "https://outlook.office365.com/.default";
const MICROSOFT_TOKEN = /^https:\/\/login\.microsoftonline\.com\/([^/?#]+)\/oauth2\/v2\.0\/token$/;

function tokenUrlFor(tenant: string): string {
  const trimmed = tenant.trim();
  return trimmed === ""
    ? ""
    : `https://login.microsoftonline.com/${encodeURIComponent(trimmed)}/oauth2/v2.0/token`;
}

/** The directory ID inside a standard token address, or "" for any other address. */
function tenantOf(tokenUrl: string): string {
  const match = MICROSOFT_TOKEN.exec(tokenUrl);
  return match?.[1] ? decodeURIComponent(match[1]) : "";
}

interface EmailForm {
  host: string;
  port: string;
  fromAddress: string;
  fromDisplayName: string;
  replyTo: string;
  encryption: EmailEncryption;
  username: string;
  /** Always starts empty. What the operator types here, and only that, is sent. */
  password: string;
  authType: EmailAuthType;
  tokenUrl: string;
  tokenScope: string;
  tokenClientId: string;
  /** Always starts empty, for the same reason `password` does. */
  tokenClientSecret: string;
}

function encryptionOf(data: EmailSettingsOut | undefined): EmailEncryption {
  return data?.encryption === "ssl" ? "ssl" : data?.encryption === "none" ? "none" : "starttls";
}

function seedFrom(data: EmailSettingsOut | undefined): EmailForm {
  return {
    host: data?.host ?? "",
    port: data?.port ?? "",
    fromAddress: data?.from_address ?? "",
    fromDisplayName: data?.from_display_name ?? "",
    replyTo: data?.reply_to ?? "",
    encryption: encryptionOf(data),
    username: data?.username ?? "",
    password: "",
    authType: data?.auth_type === "token" ? "token" : "password",
    tokenUrl: data?.microsoft_sign_in_url ?? "",
    tokenScope: data?.microsoft_scope || MICROSOFT_SCOPE,
    tokenClientId: data?.microsoft_client_id ?? "",
    tokenClientSecret: "",
  };
}

/** `canManage` reads the tab (its read is `orgs.manage` too); `canEdit` writes it: `orgs.manage` on every client (phase 3 design §2). */
export function EmailTab({ canManage, canEdit = canManage }: { canManage: boolean; canEdit?: boolean }) {
  const t = useT();
  const reason = useGlobalWriteReason(EMAIL_MANAGE);
  const settings = useEmailSettings(canManage);
  const update = useUpdateEmailSettingsMutation();
  const test = useTestEmailMutation();
  const suggestion = settings.data?.suggested_test_recipient || settings.data?.from_address || "";
  const [recipient, setRecipient] = useState(suggestion);
  useEffect(() => {
    setRecipient(suggestion);
  }, [suggestion]);

  const seed = seedFrom(settings.data);
  const [form, setForm] = useState<EmailForm>(seed);

  const seedSignature = JSON.stringify(seed);
  useEffect(() => {
    setForm(JSON.parse(seedSignature) as EmailForm);
  }, [seedSignature]);

  const forbidden = isForbidden(settings.error);
  const failed = settings.error != null && !forbidden;
  const dirty = isDirty(form, seed);
  const port = parseIntegerField(form.port);
  const portValid = port != null && port >= 1 && port <= 65535;
  // A password with no username is REFUSED by the server rather than quietly
  // dropped — saving with no username CLEARS the stored credential, so a typed
  // password would vanish into a 200 and the operator would believe they had
  // rotated it. The console says so before the round trip instead of relaying
  // the refusal afterwards.
  const passwordWithoutUsername =
    form.authType === "password" && form.password !== "" && form.username.trim() === "";
  // The server keeps a stored client secret only while the saved mode is
  // already Microsoft; on the first switch there is nothing to keep.
  const savedAsMicrosoft = settings.data?.auth_type === "token";
  const microsoftIncomplete =
    form.authType === "token" &&
    (form.username.trim() === "" ||
      form.tokenClientId.trim() === "" ||
      form.tokenUrl.trim() === "" ||
      (!savedAsMicrosoft && form.tokenClientSecret === ""));
  const complete =
    form.host.trim() !== "" &&
    form.fromAddress.trim() !== "" &&
    portValid &&
    !passwordWithoutUsername &&
    !microsoftIncomplete;
  const passwordSaved = settings.data?.authenticated === true && !savedAsMicrosoft;
  const customTokenUrl = form.tokenUrl !== "" && tenantOf(form.tokenUrl) === "";
  const editable = canEdit && settings.data != null;

  function set<K extends keyof EmailForm>(key: K, value: EmailForm[K]) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  function submit() {
    const body: EmailSettingsUpdate = {
      host: form.host.trim(),
      port: port as number,
      from_address: form.fromAddress.trim(),
      from_display_name: form.fromDisplayName.trim(),
      reply_to: form.replyTo.trim(),
      encryption: form.encryption,
      username: form.username.trim(),
      auth_type: form.authType,
    };
    // THE OMISSION IS THE FEATURE. Only a secret the operator actually typed is
    // sent; anything else would overwrite the stored one.
    if (form.authType === "token") {
      body.token_url = form.tokenUrl.trim();
      body.token_scope = form.tokenScope.trim();
      body.token_client_id = form.tokenClientId.trim();
      if (form.tokenClientSecret !== "") body.token_client_secret = form.tokenClientSecret;
    } else if (form.password !== "") {
      body.password = form.password;
    }
    update.mutate(body, {
      onSuccess: () =>
        setForm((current) => ({ ...current, password: "", tokenClientSecret: "" })),
    });
  }

  return (
    <Panel
      title={t("settings.email.title")}
      subtitle={t("settings.email.subtitle")}
    >
      {settings.isLoading ? (
        <p className="note">{t("settings.email.loading")}</p>
      ) : forbidden ? (
        <p className="note">
          {t("settings.email.forbidden", { code: EMAIL_MANAGE })}
        </p>
      ) : failed ? (
        <LoadFailure error={settings.error} />
      ) : (
        <>
          {settings.data && !settings.data.configured ? (
            <p role="status" className="notice gold" style={{ marginBottom: 14 }}>
              <Icon name={KIND_ICON.warn} size={16} />
              <span>
                {t("settings.email.notSetUp")}
              </span>
            </p>
          ) : null}

          <FieldGroup title={t("settings.email.groups.connection")} icon="network">
            <div className="form-grid" style={FOLDING_FORM_GRID}>
              <Field label={t("settings.email.mailServer")}>
                <input
                  className={inputClass}
                  value={form.host}
                  disabled={!editable}
                  placeholder={t("settings.email.hostPlaceholder")}
                  onChange={(event) => set("host", event.target.value)}
                />
              </Field>
              <Field label={t("settings.email.port")}>
                <input
                  className={inputClass}
                  value={form.port}
                  disabled={!editable}
                  inputMode="numeric"
                  placeholder={t("settings.email.portPlaceholder")}
                  onChange={(event) => set("port", event.target.value)}
                />
              </Field>
              {/* A CHOICE OF THREE, NOT A MENU. `<select>` is the browser's
                  own widget painted through the kit's box; the design draws a
                  short, fixed enumeration as `Chips`. `ChipsField` closes
                  this and the two other near-identical call sites
                  (`BudgetTab`'s "Fiscal year starts in",
                  `OrganizationTab`'s per-key choice fields) into one: a
                  plain `.field` `div` rather than `Field`'s `<label>` (a
                  chip button nested inside a `<label>` inherits that
                  label's text as ITS OWN accessible name -- the same
                  conflict `Toggle`'s own comment documents: "a button
                  nested in a <label> is two things claiming the same
                  click"), its caption named once through
                  `aria-labelledby` rather than doubled by `Chips`' own
                  `aria-label`, and `disabled={!editable}` standing in for
                  the `<select disabled>` twin this used to need before
                  `Chips` carried a disabled state of its own. */}
              <ChipsField
                caption={t("settings.email.encryption")}
                items={ENCRYPTION_CHOICES.map((choice) => choice.label)}
                active={ENCRYPTION_CHOICES.findIndex((choice) => choice.value === form.encryption)}
                disabled={!editable}
                onSelect={(at) => {
                  const choice = ENCRYPTION_CHOICES[at];
                  if (choice) set("encryption", choice.value);
                }}
              />
            </div>
          </FieldGroup>
          <FieldGroup title={t("settings.email.groups.sender")} icon="mail">
            <div className="form-grid" style={FOLDING_FORM_GRID}>
              <Field label={t("settings.email.fromAddress")}>
                <input
                  className={inputClass}
                  value={form.fromAddress}
                  disabled={!editable}
                  placeholder={t("settings.email.addressPlaceholder")}
                  onChange={(event) => set("fromAddress", event.target.value)}
                />
              </Field>
              <Field label={t("settings.email.senderName")}>
                <input
                  className={inputClass}
                  value={form.fromDisplayName}
                  disabled={!editable}
                  onChange={(event) => set("fromDisplayName", event.target.value)}
                />
              </Field>
              <Field label={t("settings.email.replyTo")}>
                <input
                  className={inputClass}
                  value={form.replyTo}
                  disabled={!editable}
                  onChange={(event) => set("replyTo", event.target.value)}
                />
              </Field>
            </div>
          </FieldGroup>
          <FieldGroup title={t("settings.email.groups.authentication")} icon="key">
            <div className="form-grid" style={FOLDING_FORM_GRID}>
              <ChipsField
                caption={t("settings.email.signIn")}
                items={AUTH_CHOICES.map((choice) => choice.label)}
                active={AUTH_CHOICES.findIndex((choice) => choice.value === form.authType)}
                disabled={!editable}
                onSelect={(at) => {
                  const choice = AUTH_CHOICES[at];
                  if (choice) set("authType", choice.value);
                }}
              />
              <Field
                label={t("settings.email.username")}
                help={form.authType === "token" ? t("settings.email.usernameHelp") : undefined}
              >
                <input
                  className={inputClass}
                  value={form.username}
                  disabled={!editable}
                  onChange={(event) => set("username", event.target.value)}
                  placeholder={
                    form.authType === "token"
                      ? t("settings.email.addressPlaceholder")
                      : t("settings.email.usernamePlaceholder")
                  }
                />
              </Field>
              {form.authType === "password" ? (
                <Field
                  label={t("settings.email.password")}
                  // THE PASSWORD LIVES AT THE IDENTITY PROVIDER, IN BOTH STATES.
                  // Confirmed against `backend/app/modules/user_admin/realm_config.py`:
                  // `set_smtp` writes it into the Keycloak realm's own
                  // `smtpServer` block, `get_smtp` strips it on every read, and
                  // it is Keycloak itself — via `kc_client.py`'s
                  // `execute-actions-email` — that connects to the mail server
                  // and sends with it. This platform never stores it; that is
                  // true whether or not one is currently set, so both branches
                  // below say the same thing about custody, not opposite ones.
                  help={
                    passwordSaved
                      ? t("settings.email.passwordHelpSet")
                      : t("settings.email.passwordHelpNotSet")
                  }
                >
                  <input
                    className={inputClass}
                    type="password"
                    value={form.password}
                    disabled={!editable}
                    autoComplete="new-password"
                    onChange={(event) => set("password", event.target.value)}
                  />
                </Field>
              ) : (
                <>
                  <Field
                    label={t("settings.email.tenant")}
                    help={t("settings.email.tenantHelp")}
                  >
                    <input
                      className={inputClass}
                      value={tenantOf(form.tokenUrl)}
                      disabled={!editable}
                      placeholder={t("settings.email.tenantPlaceholder")}
                      onChange={(event) => set("tokenUrl", tokenUrlFor(event.target.value))}
                    />
                  </Field>
                  <Field label={t("settings.email.client")} help={t("settings.email.clientHelp")}>
                    <input
                      className={inputClass}
                      value={form.tokenClientId}
                      disabled={!editable}
                      onChange={(event) => set("tokenClientId", event.target.value)}
                    />
                  </Field>
                  <Field
                    label={t("settings.email.secret")}
                    help={
                      savedAsMicrosoft
                        ? t("settings.email.secretHelpSet")
                        : t("settings.email.secretHelpNotSet")
                    }
                  >
                    <input
                      className={inputClass}
                      type="password"
                      value={form.tokenClientSecret}
                      disabled={!editable}
                      autoComplete="new-password"
                      onChange={(event) => set("tokenClientSecret", event.target.value)}
                    />
                  </Field>
                </>
              )}
            </div>
            {form.authType === "token" ? (
              <details className="form-hint" open={customTokenUrl} style={{ marginTop: 10 }}>
                <summary>{t("settings.email.advanced")}</summary>
                <div className="form-grid" style={FOLDING_FORM_GRID}>
                  <Field label={t("settings.email.tokenAddress")}>
                    <input
                      className={inputClass}
                      value={form.tokenUrl}
                      disabled={!editable}
                      onChange={(event) => set("tokenUrl", event.target.value)}
                    />
                  </Field>
                  <Field
                    label={t("settings.email.scope")}
                    help={t("settings.email.scopeHelp")}
                  >
                    <input
                      className={inputClass}
                      value={form.tokenScope}
                      disabled={!editable}
                      onChange={(event) => set("tokenScope", event.target.value)}
                    />
                  </Field>
                </div>
              </details>
            ) : null}
            {form.authType === "token" && !savedAsMicrosoft && settings.data?.authenticated ? (
              // Said before the save: the realm holds one credential for the
              // mail server, and choosing Microsoft replaces it.
              <p role="status" className="notice gold" style={{ marginTop: 10 }}>
                <Icon name={KIND_ICON.warn} size={16} />
                <span>
                  {t("settings.email.replaceNotice")}
                </span>
              </p>
            ) : null}
            {form.authType === "token" ? (
              <p className="form-hint" style={{ marginTop: 10 }}>
                {t("settings.email.microsoftRelay")}
              </p>
            ) : null}
          </FieldGroup>

          {!portValid && form.port !== "" ? (
            <p className="form-error" style={{ marginTop: 10 }}>
              <Icon name={KIND_ICON.crit} size={13} /> {t("settings.email.portInvalid")}
            </p>
          ) : null}

          {passwordWithoutUsername ? (
            <p className="form-error" style={{ marginTop: 10 }}>
              <Icon name={KIND_ICON.crit} size={13} /> {t("settings.email.passwordNeedsUser")}
            </p>
          ) : null}

          <SaveBar
            label={t("common.actions.save")}
            canEdit={editable && complete}
            disabledReason={
              canEdit
                ? form.authType === "token"
                  ? t("settings.email.incompleteToken")
                  : t("settings.email.incompletePassword")
                : reason
            }
            dirty={dirty}
            pending={update.isPending}
            error={update.error}
            saved={update.isSuccess && !dirty}
            onSave={submit}
          />

          <FieldGroup title={t("settings.email.groups.test")} icon="mail">
            <div className="form-grid" style={FOLDING_FORM_GRID}>
              <Field label={t("settings.email.testTo")}>
                <input
                  className={inputClass}
                  type="email"
                  value={recipient}
                  disabled={!canEdit || !settings.data?.configured}
                  onChange={(e) => setRecipient(e.target.value)}
                />
              </Field>
            </div>
          </FieldGroup>
          <div className="form-actions">
            <Btn
              label={test.isPending ? t("settings.email.sending") : t("settings.email.sendTest")}
              icon="mail"
              // Offered only once something is saved: a button that can only
              // fail teaches an operator to ignore its result.
              disabled={!canEdit || !settings.data?.configured || test.isPending}
              onClick={() => test.mutate(recipient.trim() ? { recipient: recipient.trim() } : {})}
            />
            <span className="form-hint">
              {t("settings.email.testHint")}
            </span>
            {test.error ? (
              <span role="alert" className="form-error">
                <Icon name={KIND_ICON.crit} size={13} />
                {describeSendFailure(test.error)}
              </span>
            ) : null}
            {test.isSuccess && !test.error ? (
              // NOT a tick. Accepted is not delivered, and saying so is the
              // whole difference between this button and a decorative one.
              <span role="status" className="form-saved">
                <Icon name={KIND_ICON.ok} size={13} />
                {testSentMessage(test.data?.recipient)}
              </span>
            ) : null}
          </div>
        </>
      )}
    </Panel>
  );
}
