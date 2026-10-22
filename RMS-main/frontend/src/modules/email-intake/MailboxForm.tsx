import { useState } from "react";
import { attemptProblems, Form, FormActions } from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import { EntityPicker } from "../../components/picker";
import { Button, Dialog, Field, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate, type MessageKey } from "../../i18n/core";
import { ChipsField } from "../../shell/viz";
import {
  useCreateEmailMailboxMutation,
  useUpdateEmailMailboxMutation,
} from "./api";
import type { AuthKind, MailboxCreate, MailboxOut, MailboxUpdate } from "./types";

// Keys, not words: the chip labels are looked up when the form renders.
const SIGN_IN_CHOICES: readonly { value: AuthKind; label: MessageKey }[] = [
  { value: "password", label: "emailIntake.form.password" },
  { value: "microsoft_oauth", label: "emailIntake.form.microsoft365" },
];

/** Microsoft 365's IMAP server, filled in when the operator picks Microsoft. */
const MICROSOFT_IMAP_HOST = "outlook.office365.com";

// THIS FORM NEVER RENDERS A STORED PASSWORD BACK, and cannot: `MailboxOut`
// has no field that could carry one (invariant #3), and the password input
// below is initialised to `""` unconditionally rather than from any prop.
// The operator is told WHETHER a password is configured -- `has_credential`,
// the one boolean the API surfaces -- and given a write-only input for
// setting a new one.
//
// IT IS MOUNTED ONLY WHILE OPEN (see MailboxList), so every open starts from
// fresh state. That is the reason a password typed into a dialog that was
// then cancelled cannot reappear the next time the form is opened: there is
// no component instance left holding it.
//
// ON EDIT, AN UNTOUCHED PASSWORD FIELD SENDS NO `password` KEY AT ALL.
// `MailboxUpdate.password = None` means "leave the stored password alone"
// (schema.py) and there is no way to clear it -- `credential_id` is NOT
// NULL, there is no unauthenticated IMAP. Sending `""` would be a 422 and
// sending `null` would read as an attempt to clear.

// Auditor (MEDIUM): Address is `type="text"`, not `type="email"` -- and the
// dialog validated nothing, so "helpdesk" (no domain) saved as a mailbox
// address. That address is not decoration: `intake.py` matches an inbound
// sender against it and `poller.py` logs in as it, so a malformed one produces
// a mailbox that polls, fails, and reports `last_error` on a row an operator
// has to go looking for.
const EMAIL_SHAPE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

function invalidAddress(value: string): string | null {
  if (!EMAIL_SHAPE.test(value)) {
    return translate("emailIntake.form.badAddress", {
      example: translate("emailIntake.form.addressPlaceholder"),
      value,
    });
  }
  return null;
}

// `Number(port)` turns anything non-numeric into `NaN`, which `JSON.stringify`
// writes as `null` -- so a typo arrived at the API as a missing field and came
// back as a raw 422 nobody could act on. The bounds are `schema.py`'s
// (`ge=1, le=65535`), stated here in the words the user typed.
function invalidPort(value: string): string | null {
  if (!/^\d+$/.test(value)) return translate("emailIntake.form.badPort", { value });
  const port = Number(value);
  if (port < 1 || port > 65535) return translate("emailIntake.form.portRange", { port });
  return null;
}

// `ge=1, le=3650` (schema.py). Zero is the interesting one: it reads as
// "never reopen", but the server refuses it, so the honest answer is the
// bound rather than a silent 422.
function invalidReopenWindow(value: string): string | null {
  if (!/^\d+$/.test(value)) {
    return translate("emailIntake.form.badReopen", { value });
  }
  const days = Number(value);
  if (days < 1 || days > 3650) {
    return translate("emailIntake.form.reopenRange", { days });
  }
  return null;
}

// Auditor (MEDIUM): "Add mailbox is the same shared modal -- third
// confirmation of the four shared gaps (not a <form>, no required)". The
// replaced expression was
// `disabled={pending || !address || !host || !username || !categoryId || (!editing && !password)}`
// -- six reasons to refuse the click, none of them ever said out loud.
//
// Ordered as the fields are on screen, so the joined sentence reads top to
// bottom. `label` is the words ON SCREEN ("IMAP host", never "host").
//
// A factory of rule lists, so the words are looked up when a refusal is
// rendered; the boolean it closes over is a value, not live state.
function mailboxRules(needsPassword: boolean): () => readonly FieldRule[] {
  return () => [
    { name: "address", label: translate("emailIntake.form.address"), validate: invalidAddress },
    { name: "host", label: translate("emailIntake.form.host") },
    { name: "port", label: translate("emailIntake.form.port"), validate: invalidPort },
    { name: "folder", label: translate("emailIntake.form.folder") },
    { name: "username", label: translate("emailIntake.form.username") },
    // On edit an untouched password means "keep the stored one" (module
    // comment above), so it is required only when there is none to keep: a
    // new password mailbox, or one being switched back from Microsoft. A
    // Microsoft mailbox has no password at all.
    ...(needsPassword ? [{ name: "password", label: translate("emailIntake.form.password") }] : []),
    { name: "categoryId", label: translate("emailIntake.form.defaultCategory") },
    { name: "reopenWindowDays", label: translate("emailIntake.form.reopenWindow"), validate: invalidReopenWindow },
  ];
}

export interface MailboxFormProps {
  // `null` is create, a row is edit. The component is remounted per open, so
  // this is read once into initial state and never watched.
  mailbox: MailboxOut | null;
  categories: ReadonlyArray<{ id: string; name: string }>;
  categoriesForbidden: boolean;
  onClose: () => void;
}

export function MailboxForm({
  mailbox,
  categories,
  categoriesForbidden,
  onClose,
}: MailboxFormProps) {
  const t = useT();
  const create = useCreateEmailMailboxMutation();
  const update = useUpdateEmailMailboxMutation();
  const editing = mailbox !== null;

  const [address, setAddress] = useState(mailbox?.address ?? "");
  const [host, setHost] = useState(mailbox?.host ?? "");
  const [port, setPort] = useState(String(mailbox?.port ?? 993));
  const [useSsl, setUseSsl] = useState(mailbox?.use_ssl ?? true);
  const [folder, setFolder] = useState(mailbox?.folder ?? "INBOX");
  const [username, setUsername] = useState(mailbox?.username ?? "");
  const [categoryId, setCategoryId] = useState(
    mailbox?.default_category_id ?? categories[0]?.id ?? "",
  );
  const [reopenWindowDays, setReopenWindowDays] = useState(
    String(mailbox?.reopen_window_days ?? 14),
  );
  // NEVER seeded from `mailbox`. There is nothing to seed it from.
  const [password, setPassword] = useState("");
  const savedKind: AuthKind =
    mailbox?.auth_kind === "microsoft_oauth" ? "microsoft_oauth" : "password";
  const [authKind, setAuthKind] = useState<AuthKind>(savedKind);
  const microsoft = authKind === "microsoft_oauth";
  const switching = editing && authKind !== savedKind;
  const needsPassword = !microsoft && (!editing || switching);
  // Filled ON SUBMIT, never while typing: an "Address is required" that
  // appears before the first keystroke lands is its own kind of unhelpful. Held
  // as the rules and the values they were checked against (the password only as
  // "set" or nothing), so the sentences are worded at render.
  const [attempt, setAttempt] = useState<Attempt | null>(null);

  const pending = create.isPending || update.isPending;
  const error = create.error ?? update.error;

  // Cycle 6/7. `default_category_id` is NOT NULL and load-bearing -- an email
  // carries no category, so this field decides where every inbound ticket
  // lands -- and the control for it was a <select> that silently became a
  // "Category ID" free-text box on a fresh install, i.e. a UUID typed by hand
  // into the one field that cannot be left wrong. The picker states what would
  // fill it instead of presenting an empty list as a normal one.
  //
  // Not memoised: the sublabel is worded, and a memo would keep the language
  // the form opened in.
  const categoryOptions = categories.map((c) => ({
    id: c.id,
    label: c.name,
    sublabel: t("emailIntake.form.categorySublabel"),
  }));

  function submit() {
    const next: Attempt = {
      rules: mailboxRules(needsPassword),
      values: {
        address,
        host,
        port,
        folder,
        username,
        // Never the password itself: the rule only asks whether one was typed,
        // and it trims before asking.
        password: password.trim() ? "set" : "",
        categoryId,
        reopenWindowDays,
      },
    };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;

    // Trimmed to match what was validated -- `requiredProblems` trims before
    // testing, so an untrimmed send could carry an address the checks never
    // saw. The PASSWORD is deliberately not trimmed: leading or trailing
    // space is a legitimate part of a secret, and silently altering one
    // produces a mailbox that authenticates here and nowhere else.
    if (editing && mailbox) {
      const body: MailboxUpdate = {
        address: address.trim(),
        host: host.trim(),
        port: Number(port.trim()),
        use_ssl: useSsl,
        folder: folder.trim(),
        username: username.trim(),
        default_category_id: categoryId.trim(),
        reopen_window_days: Number(reopenWindowDays.trim()),
      };
      if (switching) body.auth_kind = authKind;
      // The key is OMITTED, not set to "" or null (module comment).
      if (password && !microsoft) body.password = password;
      update.mutate({ id: mailbox.id, body }, { onSuccess: onClose });
      return;
    }
    const body: MailboxCreate = {
      address: address.trim(),
      host: host.trim(),
      port: Number(port.trim()),
      use_ssl: useSsl,
      folder: folder.trim(),
      username: username.trim(),
      auth_kind: authKind,
      default_category_id: categoryId.trim(),
      reopen_window_days: Number(reopenWindowDays.trim()),
    };
    // A Microsoft mailbox has no password; it signs in with "Connect with Microsoft".
    if (!microsoft) body.password = password;
    create.mutate(body, { onSuccess: onClose });
  }

  return (
    <Dialog open onClose={onClose} title={editing ? t("emailIntake.form.editTitle") : t("emailIntake.form.addTitle")}>
      {/* A real <form>, so Enter in the Address field saves instead of doing
          nothing -- the other half of the same finding. */}
      <Form onSubmit={submit}>
        <div className="form-grid">
          <ChipsField
            caption={t("emailIntake.form.signIn")}
            full
            items={SIGN_IN_CHOICES.map((choice) => t(choice.label))}
            active={SIGN_IN_CHOICES.findIndex((choice) => choice.value === authKind)}
            onSelect={(at) => {
              const choice = SIGN_IN_CHOICES[at];
              if (!choice) return;
              setAuthKind(choice.value);
              if (choice.value === "microsoft_oauth" && host.trim() === "") {
                setHost(MICROSOFT_IMAP_HOST);
                setPort("993");
                setUseSsl(true);
              }
            }}
          />
          {microsoft ? (
            <p className="form-hint">
              {switching
                ? t("emailIntake.form.switchingHint")
                : t("emailIntake.form.microsoftHint")}
            </p>
          ) : null}
          <Field label={t("emailIntake.form.address")} full>
            {/* `type="email"` is the half of the finding the browser can answer
                on its own: the right keyboard on mobile, and the value marked
                invalid for assistive tech. It does NOT block submission --
                `<Form>` is `noValidate`, because a native bubble that vanishes
                on the next click is the silent no-op wearing a hat.
                `invalidAddress` is what the user actually reads. */}
            <input
              className={inputClass}
              type="email"
              inputMode="email"
              autoComplete="off"
              placeholder={t("emailIntake.form.addressPlaceholder")}
              value={address}
              onChange={(e) => setAddress(e.target.value)}
            />
          </Field>
          <Field label={t("emailIntake.form.host")}>
            <input className={inputClass} value={host} onChange={(e) => setHost(e.target.value)} />
          </Field>
          <Field label={t("emailIntake.form.port")}>
            <input
              className={inputClass}
              type="number"
              value={port}
              onChange={(e) => setPort(e.target.value)}
            />
          </Field>
          <Field label={t("emailIntake.form.transport")} full>
            <select
              className={inputClass}
              value={useSsl ? "implicit" : "starttls"}
              onChange={(e) => setUseSsl(e.target.value === "implicit")}
            >
              <option value="implicit">{t("emailIntake.form.implicitOption")}</option>
              <option value="starttls">{t("emailIntake.form.starttlsOption")}</option>
            </select>
          </Field>
          {/* Neither option is "unencrypted". Both verify the server certificate
              and both fail closed before the LOGIN line is written; a server that
              does not advertise STARTTLS gets `tls failed: starttls-unsupported`
              rather than a password over cleartext (spec §2.1). */}
          <p className="form-hint">
            {t("emailIntake.form.transportHint")}
          </p>
          <Field label={t("emailIntake.form.folder")}>
            <input className={inputClass} value={folder} onChange={(e) => setFolder(e.target.value)} />
          </Field>
          <Field label={t("emailIntake.form.username")}>
            {/* Auditor A (HIGH), on this dialog: the password beside it sets
                `autocomplete="new-password"` correctly while "the Username field
                beside this password also sets `autocomplete: null`". A bare
                username field next to a password field is what a password
                manager reads as a login form for the operator's OWN account. */}
            <input
              className={inputClass}
              autoComplete="off"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
            />
          </Field>

          {editing && !microsoft && !switching && (
            <p className="form-hint">
              {mailbox?.has_credential
                ? t("emailIntake.form.passwordConfigured")
                : t("emailIntake.form.passwordNotConfigured")}
            </p>
          )}
          {!microsoft && (
            <Field label={editing && !switching ? t("emailIntake.form.newPassword") : t("emailIntake.form.password")} full>
              <input
                className={inputClass}
                type="password"
                autoComplete="new-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </Field>
          )}

          {/* The 403 fallback stays: `ticket.read` is a DIFFERENT permission
              from `email_intake.admin`, so an admin who cannot list categories
              must still be able to configure a mailbox with an id they were
              given. What changes is the OTHER empty case -- no categories exist
              yet -- which used to be indistinguishable from the 403 and is now
              the picker's hint naming the console surface that creates them. */}
          {categoriesForbidden ? (
            <Field label={t("emailIntake.form.defaultCategory")} full>
              <input
                className={inputClass}
                placeholder={t("emailIntake.form.categoryId")}
                value={categoryId}
                onChange={(e) => setCategoryId(e.target.value)}
              />
            </Field>
          ) : (
            <EntityPicker
              label={t("emailIntake.form.defaultCategory")}
              options={categoryOptions}
              value={categoryId}
              onChange={setCategoryId}
              emptyHint={t("emailIntake.form.categoryEmpty")}
              placeholder={t("emailIntake.form.categoryPlaceholder")}
            />
          )}
          {/* `default_category_id` is NOT NULL and load-bearing: an email carries
              no category, and a category with no default queue makes every
              inbound message quarantine (intake.py). */}
          <p className="form-hint">
            {t("emailIntake.form.categoryHint")}
          </p>

          <Field label={t("emailIntake.form.reopenWindow")}>
            <input
              className={inputClass}
              type="number"
              value={reopenWindowDays}
              onChange={(e) => setReopenWindowDays(e.target.value)}
            />
          </Field>
        </div>

        <div className="flex items-center justify-end gap-2">
          {/* `type="button"` is load-bearing now that there is a form around
              it: a button with no type inside a <form> IS a submit button, so
              Cancel would save. */}
          <Button type="button" variant="ghost" onClick={onClose}>
            {t("common.actions.cancel")}
          </Button>
          {/* Enabled with the fields empty ON PURPOSE. The refusal is what the
              user gets to read; a control that will not move is not. The
              failure also comes from here rather than the old
              `String(error)` line, which rendered "Error: " for a bodyless 503
              and nothing at all for the guard around it. */}
          <FormActions
            label={t("common.actions.save")}
            pending={pending}
            error={error}
            problems={attemptProblems(attempt)}
          />
        </div>
      </Form>
    </Dialog>
  );
}