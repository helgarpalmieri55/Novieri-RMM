/**
 * THE RECENT CONFIRMATION OF THE SECOND FACTOR (own authentication design §2.5).
 *
 * Changing your own password or second factors, resetting another person's
 * factors and checking a credential out of the vault need the second factor
 * confirmed in the last 10 minutes. The SERVER decides that, on every such
 * request: one it refuses answers 403 `{"code": "step_up_required"}`, in the
 * body or in its `detail`. Nothing here is a check -- the console keeps no
 * clock of its own and never skips a request because it believes the
 * confirmation is fresh (invariant 6: the console gates nothing).
 *
 * WHAT THIS DOES WITH THAT ANSWER. `run(action)` sends the request. On that
 * one refusal it opens a dialog asking for a passkey, a code from the
 * authenticator app or a recovery code -- passkey first, the design's order --
 * sends it to `/auth/step-up/*`, and only after the server ACCEPTS it sends
 * the same request once more. A second refusal comes back as it came: no loop,
 * and no retry the server did not make possible. Closing the dialog rejects
 * with `StepUpCancelled`, so the caller's mutation ends saying nothing changed.
 * Several requests waiting at once share one confirmation.
 *
 * NOTHING TYPED HERE IS KEPT. The boxes are emptied each time the dialog opens
 * or closes and once the server has accepted a code; the passkey assertion
 * goes from the browser to the server and nowhere else.
 *
 * The person's own methods come from `GET /me/security` (`MY_SECURITY_KEY`,
 * the cache key the security page shares), read only while the dialog is open.
 *
 * RENDER `dialog` OUTSIDE ANY `<form>`: it holds forms of its own, and nested
 * forms are invalid HTML. A caller with another dialog open under it passes
 * `dismissible={!stepUp.open}` to that one, so Escape answers this one only.
 */

import { useQuery } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";

import { useT } from "../i18n/context";
import { translate } from "../i18n/core";
import { ApiError } from "../lib/api";
import { useApi } from "../lib/ApiContext";
import { describePasskeyError, signWithPasskey } from "../modules/auth/passkey";
import type { MySecurity } from "../modules/profile/types";
import { Btn } from "../shell/components";
import { Icon } from "../shell/icons";
import { KIND_ICON } from "../shell/viz";
import { describeSubmitError, Form } from "./form";
import { Button, Dialog, Field, inputClass } from "./ui";

/** `GET /me/security`'s cache key, shared with the security page. */
export const MY_SECURITY_KEY = "my-security";

/** The person closed the confirmation: the request was not sent again, and nothing changed. */
export class StepUpCancelled extends Error {
  constructor() {
    super(translate("components.stepUp.cancelled"));
    this.name = "StepUpCancelled";
  }
}

/** The server's 403 `step_up_required` (Task 6), with the code at the top of the body or in `detail`. */
export function isStepUpRequired(error: unknown): boolean {
  if (!(error instanceof ApiError) || error.status !== 403) return false;
  let body: unknown;
  try {
    body = JSON.parse(error.message);
  } catch {
    return false;
  }
  if (typeof body !== "object" || body === null) return false;
  const { code, detail } = body as { code?: unknown; detail?: unknown };
  if (code === "step_up_required") return true;
  return typeof detail === "object" && detail !== null && (detail as { code?: unknown }).code === "step_up_required";
}

/**
 * What a passkey ceremony that failed means to the person: the browser's
 * refusal in Task 11's words (`describePasskeyError`, the sentence the
 * sign-in pages say), and anything else -- the server's refusal, a closed
 * confirmation -- as `describeSubmitError` words it.
 */
function describePasskeyFailure(error: unknown): string {
  return describePasskeyError(error) ?? describeSubmitError(error);
}

/** Send `action`; on 403 `step_up_required`, confirm the second factor and send it once more. */
export type StepUpRun = <T>(action: () => Promise<T>) => Promise<T>;

export interface StepUp {
  run: StepUpRun;
  /** The dialog is open: a dialog under it is not dismissible meanwhile. */
  open: boolean;
  /** Rendered by the caller, outside any form. */
  dialog: ReactNode;
}

interface Waiter {
  resolve: () => void;
  reject: (reason: unknown) => void;
}

export function useStepUp(): StepUp {
  const waiters = useRef<Waiter[]>([]);
  const [open, setOpen] = useState(false);

  const settle = useCallback((confirmed: boolean) => {
    const pending = waiters.current;
    waiters.current = [];
    setOpen(false);
    for (const waiter of pending) {
      if (confirmed) waiter.resolve();
      else waiter.reject(new StepUpCancelled());
    }
  }, []);

  // A page left while the dialog waits: whatever was waiting ends, cancelled,
  // rather than holding a mutation pending for ever.
  useEffect(
    () => () => {
      const pending = waiters.current;
      waiters.current = [];
      for (const waiter of pending) waiter.reject(new StepUpCancelled());
    },
    [],
  );

  const run: StepUpRun = useCallback(async <T,>(action: () => Promise<T>): Promise<T> => {
    try {
      return await action();
    } catch (error) {
      if (!isStepUpRequired(error)) throw error;
      await new Promise<void>((resolve, reject) => {
        waiters.current.push({ resolve, reject });
        setOpen(true);
      });
      // ONCE. A second refusal is the server's answer, and it is returned.
      return action();
    }
  }, []);

  return {
    run,
    open,
    dialog: <StepUpDialog open={open} onConfirmed={() => settle(true)} onCancel={() => settle(false)} />,
  };
}

function StepUpDialog({
  open,
  onConfirmed,
  onCancel,
}: {
  open: boolean;
  onConfirmed: () => void;
  onCancel: () => void;
}) {
  const t = useT();
  const api = useApi();
  const security = useQuery<MySecurity, ApiError>({
    queryKey: [MY_SECURITY_KEY],
    queryFn: () => api.getMySecurity(),
    enabled: open,
    retry: false,
  });
  const [code, setCode] = useState("");
  const [recovery, setRecovery] = useState("");
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);

  useEffect(() => {
    setCode("");
    setRecovery("");
    setFailure(null);
    setBusy(false);
  }, [open]);

  async function attempt(confirm: () => Promise<void>, describe: (error: unknown) => string = describeSubmitError) {
    setBusy(true);
    setFailure(null);
    try {
      await confirm();
      setCode("");
      setRecovery("");
      onConfirmed();
    } catch (error) {
      setFailure(describe(error));
    } finally {
      setBusy(false);
    }
  }

  const data = security.data;
  const hasPasskey = (data?.passkeys.length ?? 0) > 0;
  const hasTotp = data?.has_totp === true;
  const codesLeft = data?.recovery_codes_left ?? 0;

  return (
    <Dialog open={open} onClose={onCancel} title={t("components.stepUp.title")}>
      <p style={{ marginBottom: 12 }}>{t("components.stepUp.body")}</p>
      {security.isLoading ? <p className="note">{t("components.stepUp.loading")}</p> : null}
      {security.error ? (
        <p role="alert" className="notice red">
          <Icon name={KIND_ICON.crit} size={16} />
          <span>{describeSubmitError(security.error)}</span>
        </p>
      ) : null}
      {hasPasskey ? (
        <div className="form-actions">
          <Btn
            label={t("components.stepUp.usePasskey")}
            icon="key"
            variant="primary"
            disabled={busy}
            onClick={() =>
              void attempt(async () => {
                const credential = await signWithPasskey(await api.stepUpPasskeyOptions());
                await api.stepUpPasskey(credential);
              }, describePasskeyFailure)
            }
          />
        </div>
      ) : null}
      {hasTotp ? (
        <Form onSubmit={() => void attempt(() => api.stepUpTotp(code.replace(/\s+/g, "")))}>
          <div className="form-grid">
            <Field label={t("components.stepUp.code")}>
              <input
                className={inputClass}
                value={code}
                inputMode="numeric"
                autoComplete="one-time-code"
                maxLength={12}
                disabled={busy}
                onChange={(event) => setCode(event.target.value)}
              />
            </Field>
          </div>
          <div className="form-actions">
            <Btn
              type="submit"
              icon="check"
              variant={hasPasskey ? "secondary" : "primary"}
              label={t("components.stepUp.confirm")}
              disabled={busy || code.trim() === ""}
            />
          </div>
        </Form>
      ) : null}
      {codesLeft > 0 ? (
        <details style={{ marginTop: 10 }}>
          <summary className="note cursor-pointer">{t("components.stepUp.useRecovery")}</summary>
          <Form onSubmit={() => void attempt(() => api.stepUpRecovery(recovery.trim()))}>
            <div className="form-grid">
              <Field label={t("components.stepUp.recoveryCode")} help={t("components.stepUp.recoveryHelp", { count: codesLeft })}>
                <input
                  className={inputClass}
                  value={recovery}
                  autoComplete="off"
                  spellCheck={false}
                  disabled={busy}
                  onChange={(event) => setRecovery(event.target.value)}
                />
              </Field>
            </div>
            <div className="form-actions">
              <Btn
                type="submit"
                label={t("components.stepUp.confirmRecovery")}
                disabled={busy || recovery.trim() === ""}
              />
            </div>
          </Form>
        </details>
      ) : null}
      {data && !hasPasskey && !hasTotp && codesLeft === 0 ? (
        <p className="note">{t("components.stepUp.noMethod")}</p>
      ) : null}
      {failure ? (
        <p role="alert" className="notice red" style={{ marginTop: 12 }}>
          <Icon name={KIND_ICON.crit} size={16} />
          <span>{failure}</span>
        </p>
      ) : null}
      <div className="form-actions">
        <Button type="button" variant="outline" onClick={onCancel}>
          {t("common.actions.cancel")}
        </Button>
      </div>
    </Dialog>
  );
}
