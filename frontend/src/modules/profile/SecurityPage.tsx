// Your security -- `/profile/security` (own authentication design §2.1-§2.6).
//
// WHAT THIS PAGE REPLACES. `/profile` carried three buttons that left the
// console for the identity provider's own pages, one sign-in step each. The
// platform is its own identity provider now (design §1.1), so the steps are
// here, as forms over the platform's routes:
//
//   GET    /me/security                                        what this page shows
//   PUT    /me/password                                        change the password
//   POST   /me/password                                        the first password (own-auth phase 2)
//   POST   /auth/enroll/passkey/options, /auth/enroll/passkey  add a passkey
//   DELETE /me/passkeys/{id}                                   remove one
//   POST   /auth/enroll/totp/start, /auth/enroll/totp/confirm  set up the app, or move it
//   DELETE /me/totp                                            remove the app
//   POST   /me/recovery-codes                                  ten new codes, shown once
//   DELETE /me/trusted-devices/{id}                            forget a remembered device
//   DELETE /me/sessions/{id}                                   sign out another session
//   POST   /auth/microsoft/start, DELETE /me/microsoft         link or unlink a Microsoft account (own-auth phase 2)
//
// A CLIENT'S CONTACT HAS THE SAME PAGE IN THE PORTAL (`PortalSecurityPage`,
// `/portal/security`; own-auth phase 2, Task 11). `GET /me/security` says
// whose rules apply (`kind`): a contact's password is 15 characters or more,
// their authenticator app is optional, their last way in -- a password, a
// passkey or Microsoft -- is never offered for removal, and recovery codes
// are shown only to one whose authenticator app is on: a contact's passkey is
// another way in, not a second factor (operator decision C5, B2-06). The
// time zone comes from `/portal/me`: a contact cannot read the
// organisation's, nor `/me`. The server refuses all of it anyway.
//
// THE SERVER DECIDES EVERYTHING (invariant 6). The form checks a new password
// with the sign-in pages' own rule (Task 11's `checkNewPassword`: 12 -- 15
// for a contact -- to 128 characters, each code point one), which only saves
// a round trip; the
// blocklist, the context words and the bounds are the server's (§2.1), and its
// refusal is shown in its own words. Removing the last second factor is
// refused by the server; the console only says so before the click. A change
// that needs the second factor confirmed in the last 10 minutes (§2.5) is the
// server's 403 `step_up_required`, answered through `useStepUp`.
//
// A PASSWORD CHANGE KEEPS THIS SESSION (own-auth Task 7): the server ends every
// OTHER session and forgets every remembered device, and this one stays
// signed in. So a 204 re-reads the page and says so; it is not a sign-out.
//
// NOTHING SECRET IS KEPT (invariant 3). The password boxes are emptied once the
// server has answered, and the mutation that sent both passwords is reset
// when it settles: the page keeps only what came of it, a success or the
// server's refusal. The authenticator key and new recovery codes are the
// values a person must see once: each is held in this component's state only,
// the mutation that brought it is reset at once, and it is gone when the
// person says they have it. `reset()` alone only detaches a mutation --
// TanStack keeps a detached one, its answer and its body, in the mutation
// cache for its `gcTime`, five minutes by default -- so every mutation here
// that carries a secret sets `gcTime: 0` (`./api.ts`) and is dropped as soon
// as it is reset. The page renders named fields of `GET /me/security`, never
// the response, so a field a server should not have sent is not drawn.
//
// A BROWSER CREATES A PASSKEY ONLY FROM A CLICK (WebKit: "Face ID and Touch ID
// for the web require user gestures to function", carried across only XHR and
// fetch). When "Add a passkey" first needs the second factor confirmed, that
// click is spent on the confirmation, so the page does not create the passkey
// from the confirmation's continuation: it asks for one more click, "Create
// the passkey", which fetches the options and creates the passkey itself
// (`useAddPasskeyMutation`). A refusal there is said as "not created, try
// again", not as a prompt the person closed.

import { useState, type CSSProperties } from "react";

import { ConfirmDialog } from "../../components/confirm";
import { describeSubmitError, Form } from "../../components/form";
import { useStepUp, type StepUpRun } from "../../components/stepUp";
import { Badge, Field, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { describeWhen, formatDate } from "../../lib/datetime";
import { useOrgZone } from "../../lib/zone";
import { Btn, KIND_ICON, PageHead, Panel } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { checkNewPassword } from "../auth/errors";
import { setupKeyOf } from "../auth/flow";
import { describeMicrosoftError } from "../auth/microsoft";
import { MicrosoftButton } from "../auth/MicrosoftButton";
import { describePasskeyError } from "../auth/passkey";
import { usePortalMe } from "../portal/usePortalMe";
import { FOLDING_FORM_GRID } from "../settings/formGrid";
import {
  useAddPasskeyMutation,
  useAddPasswordMutation,
  useChangePasswordMutation,
  useConfirmTotpMutation,
  useEndMySessionMutation,
  useMySecurity,
  useRegenerateRecoveryCodesMutation,
  useRemovePasskeyMutation,
  useRemoveTotpMutation,
  useRevokeTrustedDeviceMutation,
  useStartTotpMutation,
  useUnlinkMicrosoftMutation,
} from "./api";
import {
  RECOVERY_CODE_COUNT,
  type MyPasskey,
  type MySecurity,
  type MySession,
  type MyTrustedDevice,
} from "./types";

const LIST: CSSProperties = { listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 10 };
const ROW: CSSProperties = { display: "flex", flexWrap: "wrap", alignItems: "center", gap: "6px 12px" };
const ROW_TEXT: CSSProperties = { flex: "1 1 240px", minWidth: 0 };
/** A browser's own string or a key has no spaces to break at; at 390px it must still wrap. */
const WRAP_ANYWHERE: CSSProperties = { overflowWrap: "anywhere" };

/**
 * Copying without swallowing the failure (the pattern of
 * `enrollment/EnrollmentPage.tsx:254-270`): a copy that fails silently would
 * let a person dismiss the only copy of their codes believing they had it.
 */
async function copyToClipboard(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

function Failure({ message }: { message: string }) {
  return (
    <p role="alert" className="notice red" style={{ marginTop: 12 }}>
      <Icon name={KIND_ICON.crit} size={16} />
      <span>{message}</span>
    </p>
  );
}

/** `/profile/security`, in the console's frame. */
export function SecurityPage() {
  const zone = useOrgZone();
  return <SecurityBody zone={zone} back="/profile/security" />;
}

/** `/portal/security`, in the portal's: the zone of `/portal/me` (decision 3). */
export function PortalSecurityPage() {
  const me = usePortalMe();
  return <SecurityBody zone={me.data?.timezone ?? null} back="/portal/security" />;
}

/** `back`: this page's own address, where linking a Microsoft account comes back to. */
function SecurityBody({ zone, back }: { zone: string | null; back: string }) {
  const t = useT();
  const security = useMySecurity();
  const stepUp = useStepUp();
  const data = security.data;
  const contact = data?.kind === "contact";
  // A contact's second factor is the authenticator app alone (C5).
  const secondFactor = data !== undefined && data.has_totp;
  // "Your password was added." outlives the form that added it (fix round 1):
  // the re-read page offers to CHANGE the password instead, and says so there
  // until a change is asked for.
  const [passwordAdded, setPasswordAdded] = useState(false);

  return (
    <>
      <PageHead title={t("profile.securityPage.title")} description={t("profile.securityPage.description")} />
      {security.isLoading ? (
        <Panel>
          <p className="note">{t("profile.securityPage.loading")}</p>
        </Panel>
      ) : security.error ? (
        <Panel>
          <p role="alert" className="notice red">
            <Icon name={KIND_ICON.crit} size={16} />
            <span>{describeSubmitError(security.error)}</span>
          </p>
        </Panel>
      ) : data ? (
        <>
          {data.has_password ? (
            <PasswordPanel
              run={stepUp.run}
              minimum={contact ? 15 : 12}
              contact={contact}
              added={passwordAdded}
              onChanging={() => setPasswordAdded(false)}
            />
          ) : (
            <AddPasswordPanel run={stepUp.run} minimum={contact ? 15 : 12} onAdded={() => setPasswordAdded(true)} />
          )}
          <PasskeysPanel security={data} run={stepUp.run} zone={zone} />
          <MicrosoftPanel security={data} run={stepUp.run} zone={zone} back={back} />
          <TotpPanel security={data} run={stepUp.run} />
          {!contact || secondFactor ? <RecoveryPanel security={data} run={stepUp.run} /> : null}
          <DevicesPanel security={data} run={stepUp.run} zone={zone} />
          <SessionsPanel security={data} run={stepUp.run} zone={zone} />
        </>
      ) : null}
      {stepUp.dialog}
    </>
  );
}

function PasswordPanel({
  run,
  minimum,
  contact,
  added,
  onChanging,
}: {
  run: StepUpRun;
  minimum: number;
  contact: boolean;
  /** The password was just added (`AddPasswordPanel`): said here until a change is asked for. */
  added: boolean;
  onChanging: () => void;
}) {
  const t = useT();
  const change = useChangePasswordMutation(run);
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [repeat, setRepeat] = useState("");
  // Whether "Change password" was pressed: from then on the console's own
  // check is shown, worked out again on every render so a change of language
  // re-words it (`checkNewPassword` speaks the active language).
  const [checked, setChecked] = useState(false);
  // What came of the last change, held here rather than in the mutation, which
  // is reset when it settles because its body is both passwords.
  const [outcome, setOutcome] = useState<{ kind: "changed" } | { kind: "failed"; error: unknown } | null>(null);
  // Each code point is one character, as the server counts them (design §2.1).
  const length = Array.from(next).length;
  const refusal = !checked
    ? null
    : current === ""
      ? t("profile.securityPage.password.missingCurrent")
      : // Task 11's rule and sentences, the ones the sign-in pages say.
        checkNewPassword(next, repeat, minimum);

  function submit() {
    setOutcome(null);
    onChanging();
    setChecked(true);
    if (current === "" || checkNewPassword(next, repeat, minimum) !== null) return;
    change.mutate(
      { current, new: next },
      {
        // Whatever the server answered, the boxes are emptied: a password is
        // typed again rather than kept on the page. And so is the mutation:
        // the page keeps only what came of it (`gcTime: 0` drops it from the
        // cache once it is reset).
        onSettled: (_data, error) => {
          setCurrent("");
          setNext("");
          setRepeat("");
          setChecked(false);
          setOutcome(error ? { kind: "failed", error } : { kind: "changed" });
          change.reset();
        },
      },
    );
  }

  return (
    <Panel
      title={t("profile.securityPage.password.title")}
      subtitle={t(contact ? "profile.securityPage.password.subtitleContact" : "profile.securityPage.password.subtitle")}
    >
      <Form onSubmit={submit}>
        <div className="form-grid" style={FOLDING_FORM_GRID}>
          <Field label={t("profile.securityPage.password.current")}>
            <input
              className={inputClass}
              type="password"
              autoComplete="current-password"
              value={current}
              onChange={(event) => setCurrent(event.target.value)}
            />
          </Field>
          <Field
            label={t("profile.securityPage.password.next")}
            help={t("profile.securityPage.password.length", { count: length })}
          >
            <input
              className={inputClass}
              type="password"
              autoComplete="new-password"
              value={next}
              onChange={(event) => setNext(event.target.value)}
            />
          </Field>
          <Field label={t("profile.securityPage.password.repeat")}>
            <input
              className={inputClass}
              type="password"
              autoComplete="new-password"
              value={repeat}
              onChange={(event) => setRepeat(event.target.value)}
            />
          </Field>
        </div>
        <div className="form-actions">
          <Btn
            type="submit"
            variant="primary"
            icon="lock"
            label={change.isPending ? t("profile.securityPage.password.changing") : t("profile.securityPage.password.submit")}
            disabled={change.isPending}
          />
          {refusal ? (
            <span role="alert" className="form-error">
              <Icon name={KIND_ICON.crit} size={13} />
              {refusal}
            </span>
          ) : null}
          {outcome?.kind === "failed" ? (
            <span role="alert" className="form-error">
              <Icon name={KIND_ICON.crit} size={13} />
              {describeSubmitError(outcome.error)}
            </span>
          ) : null}
          {outcome?.kind === "changed" ? (
            <span role="status" className="form-saved">
              <Icon name={KIND_ICON.ok} size={13} />
              {t("profile.securityPage.password.changed")}
            </span>
          ) : null}
          {added && outcome === null ? (
            <span role="status" className="form-saved">
              <Icon name={KIND_ICON.ok} size={13} />
              {t("profile.securityPage.password.added")}
            </span>
          ) : null}
        </div>
      </Form>
    </Panel>
  );
}

/**
 * The FIRST password (own-auth phase 2): for someone who signed up with a
 * passkey or Microsoft. No current password to give; the recent confirmation
 * is the server's to ask (§2.5), and the boxes empty whatever it answers.
 */
function AddPasswordPanel({ run, minimum, onAdded }: { run: StepUpRun; minimum: number; onAdded: () => void }) {
  const t = useT();
  const add = useAddPasswordMutation(run);
  const [next, setNext] = useState("");
  const [repeat, setRepeat] = useState("");
  const [checked, setChecked] = useState(false);
  const [outcome, setOutcome] = useState<{ kind: "added" } | { kind: "failed"; error: unknown } | null>(null);
  const refusal = checked ? checkNewPassword(next, repeat, minimum) : null;

  function submit() {
    setOutcome(null);
    setChecked(true);
    if (checkNewPassword(next, repeat, minimum) !== null) return;
    // `useSecretMutation` forgets the body once this has run.
    add.mutate(next, {
      onSettled: (_data, error) => {
        setNext("");
        setRepeat("");
        setChecked(false);
        setOutcome(error ? { kind: "failed", error } : { kind: "added" });
        if (!error) onAdded();
      },
    });
  }

  return (
    <Panel
      title={t("profile.securityPage.password.title")}
      subtitle={t("profile.securityPage.password.addSubtitle", { min: minimum })}
    >
      <Form onSubmit={submit}>
        <div className="form-grid" style={FOLDING_FORM_GRID}>
          <Field label={t("profile.securityPage.password.next")}>
            <input
              className={inputClass}
              type="password"
              autoComplete="new-password"
              value={next}
              onChange={(event) => setNext(event.target.value)}
            />
          </Field>
          <Field label={t("profile.securityPage.password.repeat")}>
            <input
              className={inputClass}
              type="password"
              autoComplete="new-password"
              value={repeat}
              onChange={(event) => setRepeat(event.target.value)}
            />
          </Field>
        </div>
        <div className="form-actions">
          <Btn
            type="submit"
            variant="primary"
            icon="lock"
            label={add.isPending ? t("profile.securityPage.password.adding") : t("profile.securityPage.password.add")}
            disabled={add.isPending}
          />
          {refusal ? (
            <span role="alert" className="form-error">
              <Icon name={KIND_ICON.crit} size={13} />
              {refusal}
            </span>
          ) : null}
          {outcome?.kind === "failed" ? (
            <span role="alert" className="form-error">
              <Icon name={KIND_ICON.crit} size={13} />
              {describeSubmitError(outcome.error)}
            </span>
          ) : null}
          {outcome?.kind === "added" ? (
            <span role="status" className="form-saved">
              <Icon name={KIND_ICON.ok} size={13} />
              {t("profile.securityPage.password.added")}
            </span>
          ) : null}
        </div>
      </Form>
    </Panel>
  );
}

function PasskeysPanel({ security, run, zone }: { security: MySecurity; run: StepUpRun; zone: string | null }) {
  const t = useT();
  const add = useAddPasskeyMutation(run);
  const remove = useRemovePasskeyMutation(run);
  const [name, setName] = useState("");
  const [removing, setRemoving] = useState<MyPasskey | null>(null);
  // The second factor was confirmed on the way to "Add a passkey": that click
  // was spent on the confirmation, and a browser creates a passkey only from a
  // click, so the page asks for one more ("Create the passkey").
  const [confirmed, setConfirmed] = useState(false);
  // A technician never goes without a second factor; a contact, without a
  // way in (own-auth phase 2: a password, a passkey or Microsoft). The
  // server refuses both (`last_factor`, `last_method`); this only says so first.
  const contact = security.kind === "contact";
  const onlyFactor = contact
    ? security.passkeys.length <= 1 && !security.has_password && security.microsoft === null
    : security.passkeys.length + (security.has_totp ? 1 : 0) <= 1;
  const onlyFactorText = t(
    contact ? "profile.securityPage.passkeys.lastMethod" : "profile.securityPage.passkeys.onlyFactor",
  );
  // The browser's refusal in Task 11's words (a passkey already on the
  // account, a closed prompt); anything else -- the server's refusal, a
  // closed confirmation -- as `describeSubmitError` words it. After the
  // confirmation, a refusal from the browser is said as what it is to the
  // person: not created, try again -- they may have closed nothing.
  const refusedAfterConfirmation =
    confirmed && add.error instanceof Error && add.error.name === "NotAllowedError";
  const addFailure = add.error
    ? refusedAfterConfirmation
      ? t("profile.securityPage.passkeys.retry")
      : (describePasskeyError(add.error) ?? describeSubmitError(add.error))
    : null;

  function addPasskey() {
    add.mutate(
      { name: name.trim() },
      {
        onSuccess: (outcome) => {
          if (outcome === "confirmed") {
            setConfirmed(true);
            return;
          }
          setConfirmed(false);
          setName("");
        },
      },
    );
  }

  return (
    <Panel
      title={t("profile.securityPage.passkeys.title")}
      subtitle={t(contact ? "profile.securityPage.passkeys.subtitleContact" : "profile.securityPage.passkeys.subtitle")}
    >
      {security.passkeys.length === 0 ? (
        <p className="note">{t("profile.securityPage.passkeys.empty")}</p>
      ) : (
        <ul style={LIST}>
          {security.passkeys.map((passkey) => (
            <li key={passkey.id} style={ROW}>
              <div style={ROW_TEXT}>
                <div className="cell-title">
                  {passkey.name}
                  {passkey.backup_state ? (
                    <>
                      {" "}
                      <Badge variant="info">{t("profile.securityPage.passkeys.synced")}</Badge>
                    </>
                  ) : null}
                </div>
                <div className="cell-sub">
                  {`${t("profile.securityPage.passkeys.added", { when: describeWhen(passkey.created_at, undefined, zone) })} · ${
                    passkey.last_used_at
                      ? t("profile.securityPage.passkeys.lastUsed", { when: describeWhen(passkey.last_used_at, undefined, zone) })
                      : t("profile.securityPage.passkeys.neverUsed")
                  }`}
                </div>
              </div>
              <Btn
                small
                icon="trash"
                label={t("profile.securityPage.passkeys.remove")}
                ariaLabel={t("profile.securityPage.passkeys.removeAria", { name: passkey.name })}
                disabled={onlyFactor || remove.isPending}
                title={onlyFactor ? onlyFactorText : undefined}
                onClick={() => setRemoving(passkey)}
              />
            </li>
          ))}
        </ul>
      )}
      {onlyFactor && security.passkeys.length === 1 ? (
        <p className="note" style={{ marginTop: 10 }}>
          {onlyFactorText}
        </p>
      ) : null}
      {remove.error ? <Failure message={describeSubmitError(remove.error)} /> : null}

      {/* ONE FORM, TWO LABELS. "Create the passkey" is the same submit, from a
          new click: the options are fetched by that click and nothing else
          stands before the browser's prompt (`useAddPasskeyMutation`). */}
      <Form onSubmit={addPasskey}>
        <div className="form-grid" style={{ ...FOLDING_FORM_GRID, marginTop: 14 }}>
          <Field label={t("profile.securityPage.passkeys.name")}>
            <input
              className={inputClass}
              value={name}
              placeholder={t("profile.securityPage.passkeys.namePlaceholder")}
              onChange={(event) => setName(event.target.value)}
            />
          </Field>
        </div>
        {confirmed ? (
          <p className="note" role="status" style={{ marginTop: 10 }}>
            {t("profile.securityPage.passkeys.confirmed")}
          </p>
        ) : null}
        <div className="form-actions">
          <Btn
            type="submit"
            icon="key"
            variant={confirmed ? "primary" : "secondary"}
            label={
              add.isPending
                ? t("profile.securityPage.passkeys.adding")
                : confirmed
                  ? t("profile.securityPage.passkeys.createNow")
                  : t("profile.securityPage.passkeys.add")
            }
            disabled={add.isPending || name.trim() === ""}
          />
        </div>
      </Form>
      {addFailure ? <Failure message={addFailure} /> : null}

      <ConfirmDialog
        open={removing !== null}
        title={t("profile.securityPage.confirm.passkey.title")}
        body={t("profile.securityPage.confirm.passkey.body")}
        subject={removing?.name ?? ""}
        confirmLabel={t("profile.securityPage.confirm.passkey.action")}
        destructive
        onConfirm={() => {
          const target = removing;
          setRemoving(null);
          if (target) remove.mutate(target.id);
        }}
        onCancel={() => setRemoving(null)}
      />
    </Panel>
  );
}

function TotpPanel({ security, run }: { security: MySecurity; run: StepUpRun }) {
  const t = useT();
  const start = useStartTotpMutation(run);
  const confirm = useConfirmTotpMutation(run);
  const remove = useRemoveTotpMutation(run);
  // The `otpauth://` address while a set-up is open: the one place the key is.
  const [setupUri, setSetupUri] = useState<string | null>(null);
  const [code, setCode] = useState("");
  // Why the code was not accepted, held here: the mutation's body is the code,
  // so it is reset once it settles.
  const [confirmFailure, setConfirmFailure] = useState<unknown>(null);
  const [asking, setAsking] = useState(false);
  // A contact's app is optional (own-auth phase 2, B2-28): never their only factor.
  const onlyFactor = security.kind !== "contact" && security.has_totp && security.passkeys.length === 0;

  function begin() {
    confirm.reset();
    setConfirmFailure(null);
    start.mutate(undefined, {
      onSuccess: (out) => {
        setSetupUri(out.otpauth_uri);
        setCode("");
        // The key is in this component's state now, and nowhere else.
        start.reset();
      },
    });
  }

  function finish() {
    setConfirmFailure(null);
    confirm.mutate(code.replace(/\s+/g, ""), {
      onSuccess: () => {
        setSetupUri(null);
        setCode("");
      },
      onSettled: (_data, error) => {
        setConfirmFailure(error ?? null);
        confirm.reset();
      },
    });
  }

  function cancel() {
    setSetupUri(null);
    setCode("");
    setConfirmFailure(null);
    confirm.reset();
  }

  return (
    <Panel
      title={t("profile.securityPage.totp.title")}
      subtitle={t(security.kind === "contact" ? "profile.securityPage.totp.subtitleContact" : "profile.securityPage.totp.subtitle")}
      actions={
        <Badge variant={security.has_totp ? "success" : "muted"}>
          {security.has_totp ? t("profile.securityPage.totp.on") : t("profile.securityPage.totp.off")}
        </Badge>
      }
    >
      {setupUri ? (
        <>
          <p className="note">{t("profile.securityPage.totp.instructions")}</p>
          {security.has_totp ? (
            <p className="note" style={{ marginTop: 6 }}>
              {t("profile.securityPage.totp.replaceNote")}
            </p>
          ) : null}
          {/* Only an authenticator address is a link: a server that answered
              anything else gets the key alone. */}
          {setupUri.startsWith("otpauth://") ? (
            <div className="form-actions">
              <a className="btn small" href={setupUri}>
                <Icon name="link" size={14} />
                <span>{t("profile.securityPage.totp.open")}</span>
              </a>
            </div>
          ) : null}
          <p style={{ marginTop: 10 }}>
            <span className="k">{t("profile.securityPage.totp.key")}</span>{" "}
            <code className="code" style={WRAP_ANYWHERE}>
              {setupKeyOf(setupUri) ?? ""}
            </code>
          </p>
          <Form onSubmit={finish}>
            <div className="form-grid" style={{ ...FOLDING_FORM_GRID, marginTop: 10 }}>
              <Field label={t("profile.securityPage.totp.code")}>
                <input
                  className={inputClass}
                  value={code}
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  maxLength={12}
                  onChange={(event) => setCode(event.target.value)}
                />
              </Field>
            </div>
            <div className="form-actions">
              <Btn
                type="submit"
                variant="primary"
                icon="check"
                label={confirm.isPending ? t("profile.securityPage.totp.confirming") : t("profile.securityPage.totp.confirm")}
                disabled={confirm.isPending || code.trim() === ""}
              />
              <Btn label={t("profile.securityPage.totp.cancel")} onClick={cancel} />
            </div>
          </Form>
          {confirmFailure ? <Failure message={describeSubmitError(confirmFailure)} /> : null}
        </>
      ) : (
        <div className="form-actions">
          <Btn
            icon="shield-lock"
            variant={security.has_totp ? "secondary" : "primary"}
            label={
              start.isPending
                ? t("profile.securityPage.totp.starting")
                : security.has_totp
                  ? t("profile.securityPage.totp.replace")
                  : t("profile.securityPage.totp.setUp")
            }
            disabled={start.isPending}
            onClick={begin}
          />
          {security.has_totp ? (
            <Btn
              icon="trash"
              label={t("profile.securityPage.totp.remove")}
              ariaLabel={t("profile.securityPage.confirm.totp.action")}
              disabled={onlyFactor || remove.isPending}
              title={onlyFactor ? t("profile.securityPage.totp.onlyFactor") : undefined}
              onClick={() => setAsking(true)}
            />
          ) : null}
        </div>
      )}
      {onlyFactor && setupUri === null ? (
        <p className="note" style={{ marginTop: 10 }}>
          {t("profile.securityPage.totp.onlyFactor")}
        </p>
      ) : null}
      {start.error ? <Failure message={describeSubmitError(start.error)} /> : null}
      {remove.error ? <Failure message={describeSubmitError(remove.error)} /> : null}

      <ConfirmDialog
        open={asking}
        title={t("profile.securityPage.confirm.totp.title")}
        body={t("profile.securityPage.confirm.totp.body")}
        subject={t("profile.securityPage.confirm.totpSubject")}
        confirmLabel={t("profile.securityPage.confirm.totp.action")}
        destructive
        onConfirm={() => {
          setAsking(false);
          remove.mutate();
        }}
        onCancel={() => setAsking(false)}
      />
    </Panel>
  );
}

/**
 * The Microsoft account (design §4): linked the first time from here or from
 * the invitation, by its `tid` + `oid`, never by address. Linking leaves for
 * Microsoft after the server's recent confirmation and comes back to `back`,
 * this page; unlinking is refused when it is the last way in (`last_method`),
 * and this only says so first. A technician always keeps a password (operator
 * decision C3); a contact keeps a password or a passkey.
 */
function MicrosoftPanel({
  security,
  run,
  zone,
  back,
}: {
  security: MySecurity;
  run: StepUpRun;
  zone: string | null;
  back: string;
}) {
  const t = useT();
  const unlink = useUnlinkMicrosoftMutation(run);
  const [confirming, setConfirming] = useState(false);
  const link = security.microsoft;
  const lastWayIn =
    security.kind === "contact" ? !security.has_password && security.passkeys.length === 0 : !security.has_password;

  return (
    <Panel title={t("profile.securityPage.microsoft.title")} subtitle={t("profile.securityPage.microsoft.subtitle")}>
      {link ? (
        <>
          <div style={ROW}>
            <div style={ROW_TEXT}>
              <div className="cell-title" style={WRAP_ANYWHERE}>
                {t("profile.securityPage.microsoft.linked", { tenant: link.tenant_id })}
              </div>
              <div className="cell-sub">
                {`${t("profile.securityPage.microsoft.added", { when: describeWhen(link.created_at, undefined, zone) })} · ${
                  link.last_used_at
                    ? t("profile.securityPage.microsoft.lastUsed", { when: describeWhen(link.last_used_at, undefined, zone) })
                    : t("profile.securityPage.microsoft.neverUsed")
                }`}
              </div>
            </div>
            <Btn
              small
              icon="trash"
              label={t("profile.securityPage.microsoft.unlink")}
              disabled={lastWayIn || unlink.isPending}
              title={lastWayIn ? t("profile.securityPage.microsoft.lastWayIn") : undefined}
              onClick={() => setConfirming(true)}
            />
          </div>
          {lastWayIn ? (
            <p className="note" style={{ marginTop: 10 }}>
              {t("profile.securityPage.microsoft.lastWayIn")}
            </p>
          ) : null}
          {unlink.error ? (
            <Failure message={describeMicrosoftError(unlink.error) ?? describeSubmitError(unlink.error)} />
          ) : null}
        </>
      ) : (
        <MicrosoftButton start={{ intent: "link" }} back={back} labelKey="profile.securityPage.microsoft.link" run={run} />
      )}
      <ConfirmDialog
        open={confirming}
        title={t("profile.securityPage.microsoft.confirmTitle")}
        body={t("profile.securityPage.microsoft.confirmBody")}
        subject={link?.tenant_id ?? ""}
        confirmLabel={t("profile.securityPage.microsoft.unlink")}
        destructive
        onConfirm={() => {
          setConfirming(false);
          unlink.mutate();
        }}
        onCancel={() => setConfirming(false)}
      />
    </Panel>
  );
}

function RecoveryPanel({ security, run }: { security: MySecurity; run: StepUpRun }) {
  const t = useT();
  const regenerate = useRegenerateRecoveryCodesMutation(run);
  // The new codes while they are shown: the one place they exist in the clear.
  const [codes, setCodes] = useState<readonly string[] | null>(null);
  const [asking, setAsking] = useState(false);
  const [copied, setCopied] = useState<"copied" | "failed" | null>(null);
  const left = security.recovery_codes_left;

  return (
    <Panel
      title={t("profile.securityPage.recovery.title")}
      subtitle={t(security.kind === "contact" ? "profile.securityPage.recovery.subtitleContact" : "profile.securityPage.recovery.subtitle")}
    >
      {codes ? (
        <>
          <div className="notice gold" role="status">
            <Icon name={KIND_ICON.warn} size={16} />
            <span>{t("profile.securityPage.recovery.shownOnce")}</span>
          </div>
          <ol className="mono" style={{ margin: "12px 0 0", paddingLeft: 22, display: "grid", gap: 4 }}>
            {codes.map((code) => (
              <li key={code}>{code}</li>
            ))}
          </ol>
          <div className="form-actions">
            <Btn
              icon="copy"
              label={t("profile.securityPage.recovery.copy")}
              onClick={() => void copyToClipboard(codes.join("\n")).then((ok) => setCopied(ok ? "copied" : "failed"))}
            />
            <Btn
              variant="primary"
              icon="check"
              label={t("profile.securityPage.recovery.done")}
              onClick={() => {
                setCodes(null);
                setCopied(null);
              }}
            />
            {copied === "copied" ? (
              <span className="form-saved">
                <Icon name={KIND_ICON.ok} size={13} />
                {t("profile.securityPage.recovery.copied")}
              </span>
            ) : null}
            {copied === "failed" ? (
              <span role="alert" className="form-error">
                <Icon name={KIND_ICON.crit} size={13} />
                {t("profile.securityPage.recovery.copyFailed")}
              </span>
            ) : null}
          </div>
        </>
      ) : (
        <>
          <p>{t("profile.securityPage.recovery.left", { count: left, total: RECOVERY_CODE_COUNT })}</p>
          {left === 0 ? (
            <p className="notice gold" style={{ marginTop: 10 }}>
              <Icon name={KIND_ICON.warn} size={16} />
              <span>{t("profile.securityPage.recovery.none")}</span>
            </p>
          ) : left <= 3 ? (
            <p className="notice gold" style={{ marginTop: 10 }}>
              <Icon name={KIND_ICON.warn} size={16} />
              <span>{t("profile.securityPage.recovery.low")}</span>
            </p>
          ) : null}
          <div className="form-actions">
            <Btn
              icon="refresh"
              label={regenerate.isPending ? t("profile.securityPage.recovery.regenerating") : t("profile.securityPage.recovery.regenerate")}
              disabled={regenerate.isPending}
              onClick={() => setAsking(true)}
            />
          </div>
        </>
      )}
      {regenerate.error ? <Failure message={describeSubmitError(regenerate.error)} /> : null}

      <ConfirmDialog
        open={asking}
        title={t("profile.securityPage.confirm.codes.title")}
        body={t("profile.securityPage.confirm.codes.body")}
        subject={t("profile.securityPage.confirm.codesSubject", { count: left })}
        confirmLabel={t("profile.securityPage.confirm.codes.action")}
        destructive
        onConfirm={() => {
          setAsking(false);
          regenerate.mutate(undefined, {
            onSuccess: (out) => {
              setCodes(out.recovery_codes);
              setCopied(null);
              // The codes are in this component's state now, and nowhere else.
              regenerate.reset();
            },
          });
        }}
        onCancel={() => setAsking(false)}
      />
    </Panel>
  );
}

function DevicesPanel({ security, run, zone }: { security: MySecurity; run: StepUpRun; zone: string | null }) {
  const t = useT();
  const revoke = useRevokeTrustedDeviceMutation(run);
  const [forgetting, setForgetting] = useState<MyTrustedDevice | null>(null);
  const label = (device: MyTrustedDevice): string => device.label || t("profile.securityPage.devices.unnamed");

  return (
    <Panel title={t("profile.securityPage.devices.title")} subtitle={t("profile.securityPage.devices.subtitle")}>
      {security.trusted_devices.length === 0 ? (
        <p className="note">{t("profile.securityPage.devices.empty")}</p>
      ) : (
        <ul style={LIST}>
          {security.trusted_devices.map((device) => (
            <li key={device.id} style={ROW}>
              <div style={ROW_TEXT}>
                <div className="cell-title">
                  {label(device)}
                  {device.current ? (
                    <>
                      {" "}
                      <Badge variant="info">{t("profile.securityPage.devices.thisDevice")}</Badge>
                    </>
                  ) : null}
                </div>
                <div className="cell-sub">
                  {`${t("profile.securityPage.devices.remembered", { when: describeWhen(device.created_at, undefined, zone) })} · ${t(
                    "profile.securityPage.devices.until",
                    { date: formatDate(device.expires_at, zone) },
                  )}`}
                </div>
              </div>
              <Btn
                small
                icon="unlink"
                label={t("profile.securityPage.devices.forget")}
                ariaLabel={t("profile.securityPage.devices.forgetAria", { label: label(device) })}
                disabled={revoke.isPending}
                onClick={() => setForgetting(device)}
              />
            </li>
          ))}
        </ul>
      )}
      {revoke.error ? <Failure message={describeSubmitError(revoke.error)} /> : null}

      <ConfirmDialog
        open={forgetting !== null}
        title={t("profile.securityPage.confirm.device.title")}
        body={t(forgetting?.current ? "profile.securityPage.confirm.device.bodyCurrent" : "profile.securityPage.confirm.device.body")}
        subject={forgetting ? label(forgetting) : ""}
        confirmLabel={t("profile.securityPage.confirm.device.action")}
        destructive
        onConfirm={() => {
          const target = forgetting;
          setForgetting(null);
          if (target) revoke.mutate(target.id);
        }}
        onCancel={() => setForgetting(null)}
      />
    </Panel>
  );
}

function SessionsPanel({ security, run, zone }: { security: MySecurity; run: StepUpRun; zone: string | null }) {
  const t = useT();
  const end = useEndMySessionMutation(run);
  const [ending, setEnding] = useState<MySession | null>(null);
  const browser = (session: MySession): string => session.user_agent || t("profile.securityPage.sessions.unknownBrowser");
  const where = (session: MySession): string => session.ip || browser(session);

  return (
    <Panel title={t("profile.securityPage.sessions.title")} subtitle={t("profile.securityPage.sessions.subtitle")}>
      <ul style={LIST}>
        {security.sessions.map((session) => (
          <li key={session.id} style={ROW}>
            <div style={ROW_TEXT}>
              <div className="cell-title mono" style={WRAP_ANYWHERE}>
                {browser(session)}
              </div>
              <div className="cell-sub">
                {[
                  session.ip ? t("profile.securityPage.sessions.from", { ip: session.ip }) : null,
                  t("profile.securityPage.sessions.started", { when: describeWhen(session.created_at, undefined, zone) }),
                  t("profile.securityPage.sessions.lastActive", { when: describeWhen(session.last_seen_at, undefined, zone) }),
                ]
                  .filter((part): part is string => part !== null)
                  .join(" · ")}
              </div>
            </div>
            {session.current ? (
              <Badge variant="info">{t("profile.securityPage.sessions.thisSession")}</Badge>
            ) : (
              <Btn
                small
                icon="logout"
                label={t("profile.securityPage.sessions.end")}
                ariaLabel={t("profile.securityPage.sessions.endAria", { where: where(session) })}
                disabled={end.isPending}
                onClick={() => setEnding(session)}
              />
            )}
          </li>
        ))}
      </ul>
      {end.error ? <Failure message={describeSubmitError(end.error)} /> : null}

      <ConfirmDialog
        open={ending !== null}
        title={t("profile.securityPage.confirm.session.title")}
        body={t("profile.securityPage.confirm.session.body")}
        subject={ending ? browser(ending) : ""}
        confirmLabel={t("profile.securityPage.confirm.session.action")}
        destructive
        onConfirm={() => {
          const target = ending;
          setEnding(null);
          if (target) end.mutate(target.id);
        }}
        onCancel={() => setEnding(null)}
      />
    </Panel>
  );
}
