// How an email channel signs in to its mail server: a password, or a
// Microsoft 365 application (client credentials). Shared by Notifications →
// Channels and Settings → Notifications so the two cannot drift.
//
// WRITE-ONLY (invariant 3). Nothing here is ever seeded from the server:
// `ChannelOut` has no field that could carry a sign-in. Blank means "keep what
// is stored".

import { Field, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate, type MessageKey } from "../../i18n/core";
import { ChipsField } from "../../shell/viz";
import type { EmailAuth, EmailSignInFields as SignInRequest } from "./types";

export interface EmailSignIn {
  auth: EmailAuth;
  username: string;
  password: string;
  tenantId: string;
  clientId: string;
  clientSecret: string;
}

export const EMPTY_SIGN_IN: EmailSignIn = {
  auth: "password",
  username: "",
  password: "",
  tenantId: "",
  clientId: "",
  clientSecret: "",
};

/** Microsoft 365 always sends here; the server refuses anything else. */
export const MICROSOFT_SMTP = { host: "smtp.office365.com", port: 587 } as const;

// Keys, not words: the chip labels are looked up when the fields render.
const AUTH_CHOICES: readonly { value: EmailAuth; label: MessageKey }[] = [
  { value: "password", label: "notification.signIn.password" },
  { value: "microsoft", label: "notification.signIn.microsoft" },
];

/** `config.auth` of a stored channel; absent means "password". */
export function emailAuthOf(config: Record<string, unknown>): EmailAuth {
  return config.auth === "microsoft" ? "microsoft" : "password";
}

/** The request fields for what was typed. Nothing typed sends nothing. */
export function signInRequest(value: EmailSignIn): SignInRequest {
  if (value.auth === "microsoft") {
    if (value.tenantId.trim() === "" && value.clientId.trim() === "" && value.clientSecret === "") {
      return {};
    }
    return {
      microsoft_tenant_id: value.tenantId.trim(),
      microsoft_client_id: value.clientId.trim(),
      microsoft_client_secret: value.clientSecret,
    };
  }
  if (value.username.trim() === "" && value.password === "") return {};
  return { smtp_username: value.username.trim(), smtp_password: value.password };
}

/** What is missing from a sign-in: a code, so it can be held as data and worded later. */
export type SignInProblemKind = "microsoftIncomplete" | "passwordIncomplete";

/**
 * What is missing, as a code, or null. `stored` is true when a sign-in of this
 * same kind is already saved, so leaving everything blank keeps it. The code
 * holds no secret; `signInProblemText` words it.
 */
export function signInProblemKind(value: EmailSignIn, stored: boolean): SignInProblemKind | null {
  if (value.auth === "microsoft") {
    const filled = [value.tenantId.trim(), value.clientId.trim(), value.clientSecret].filter(
      (v) => v !== "",
    ).length;
    if (filled === 0 && stored) return null;
    return filled < 3 ? "microsoftIncomplete" : null;
  }
  return (value.username.trim() === "") !== (value.password === "") ? "passwordIncomplete" : null;
}

/** A missing-sign-in code as a sentence, in the language active now. */
export function signInProblemText(kind: SignInProblemKind): string {
  return translate(
    kind === "microsoftIncomplete"
      ? "notification.signIn.microsoftIncomplete"
      : "notification.signIn.passwordIncomplete",
  );
}

/** What is missing, as a sentence, or null (see `signInProblemKind`). */
export function signInProblem(value: EmailSignIn, stored: boolean): string | null {
  const kind = signInProblemKind(value, stored);
  return kind === null ? null : signInProblemText(kind);
}

export function EmailSignInFields({
  value,
  onChange,
  stored,
  disabled = false,
}: {
  value: EmailSignIn;
  onChange: (next: EmailSignIn) => void;
  /** A sign-in of the chosen kind is already saved for this channel. */
  stored: boolean;
  disabled?: boolean;
}) {
  const t = useT();
  const set = (patch: Partial<EmailSignIn>) => onChange({ ...value, ...patch });
  return (
    <>
      <ChipsField
        caption={t("notification.signIn.caption")}
        full
        items={AUTH_CHOICES.map((choice) => t(choice.label))}
        active={AUTH_CHOICES.findIndex((choice) => choice.value === value.auth)}
        disabled={disabled}
        onSelect={(at) => {
          const choice = AUTH_CHOICES[at];
          if (choice) set({ auth: choice.value });
        }}
      />
      {value.auth === "password" ? (
        <>
          <Field label={t("notification.signIn.username")} help={t("notification.signIn.optional")}>
            <input
              className={inputClass}
              value={value.username}
              disabled={disabled}
              autoComplete="off"
              onChange={(e) => set({ username: e.target.value })}
            />
          </Field>
          <Field
            label={t("notification.signIn.password")}
            help={stored ? t("notification.signIn.passwordSaved") : t("notification.signIn.optional")}
          >
            <input
              className={inputClass}
              type="password"
              autoComplete="new-password"
              value={value.password}
              disabled={disabled}
              onChange={(e) => set({ password: e.target.value })}
            />
          </Field>
        </>
      ) : (
        <>
          <Field label={t("notification.signIn.tenant")} help={t("notification.signIn.tenantHelp")}>
            <input
              className={inputClass}
              value={value.tenantId}
              disabled={disabled}
              placeholder={t("notification.signIn.tenantPlaceholder")}
              onChange={(e) => set({ tenantId: e.target.value })}
            />
          </Field>
          <Field label={t("notification.signIn.client")} help={t("notification.signIn.clientHelp")}>
            <input
              className={inputClass}
              value={value.clientId}
              disabled={disabled}
              onChange={(e) => set({ clientId: e.target.value })}
            />
          </Field>
          <Field
            label={t("notification.signIn.secret")}
            help={stored ? t("notification.signIn.secretSaved") : undefined}
          >
            <input
              className={inputClass}
              type="password"
              autoComplete="new-password"
              value={value.clientSecret}
              disabled={disabled}
              onChange={(e) => set({ clientSecret: e.target.value })}
            />
          </Field>
          <p className="form-hint full">
            {t("notification.signIn.microsoftNote", { host: MICROSOFT_SMTP.host, port: MICROSOFT_SMTP.port })}
          </p>
        </>
      )}
    </>
  );
}
