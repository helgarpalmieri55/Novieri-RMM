/**
 * "Sign in with Microsoft" (own-auth phase 2, Task 12): asks the server to
 * start, remembers where to come back to, and leaves -- for Microsoft only.
 * A refusal is one sentence under the button. `labelKey`, the catalogue key
 * of the button's words (a key, not text: `i18n/coverage.test.ts` reads a
 * `label` literal as English). `run` is `useStepUp().run` where the server
 * may ask for a recent confirmation (linking). `check`, a sentence that stops
 * it before asking: a technician's new password that breaks the rule
 * (decision 5).
 *
 * NOT A TANSTACK MUTATION, on purpose: a technician's invitation sends their
 * new password in this request (operator decision C3), and a direct call
 * keeps it in the page's state only -- nothing of the body or the answer is
 * left in a cache (`acceptInvite`'s pattern on `/invite`).
 *
 * ONE TRANSACTION PER PAGE (fix round 1). Once the document is leaving for
 * Microsoft the button stays off: a second click before the page unloads
 * would start a second transaction, whose cookie replaces the first's, and
 * Microsoft's answer to the first would then fail. Only a page the browser
 * restores from its back/forward cache -- the person pressed Back at
 * Microsoft -- turns it on again (`useBackFromMicrosoft`).
 *
 * `guard`: a page with other ways in (`/invite`) runs this through ITS OWN
 * guard, so one action is in flight at a time and a dead invitation is that
 * page's own state. The button then says nothing itself: the page does.
 */
import { useCallback, useEffect, useRef, useState } from "react";

import type { StepUpRun } from "../../components/stepUp";
import { useT } from "../../i18n/context";
import type { MessageKey } from "../../i18n/core";
import { translate } from "../../i18n/core";
import type { MicrosoftStartIn } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { navigation } from "../../lib/session";
import { Btn } from "../../shell/components";
import { AuthAlert } from "./AuthCard";
import { describeAuthError } from "./errors";
import { checkedAuthorizeUrl, describeMicrosoftError, rememberMicrosoftReturn } from "./microsoft";

/** What a step answers once the document is leaving for Microsoft: nothing turns back on. */
export const LEFT = "left";

/** A page's own guard: it runs the step, says its refusal, and stays busy once the step has `LEFT`. */
export interface MicrosoftGuard {
  busy: boolean;
  run: (step: () => Promise<typeof LEFT>) => void;
}

/**
 * Turn a page back on when the browser restores it from its back/forward
 * cache (`pageshow` with `persisted`): the person left for Microsoft and
 * pressed Back there. A fresh load is a new page and needs nothing.
 */
export function useBackFromMicrosoft(reset: () => void): void {
  useEffect(() => {
    const restored = (event: PageTransitionEvent) => {
      if (event.persisted) reset();
    };
    window.addEventListener("pageshow", restored);
    return () => window.removeEventListener("pageshow", restored);
  }, [reset]);
}

export function MicrosoftButton({
  start,
  back,
  labelKey,
  run,
  check,
  guard,
  variant = "secondary",
}: {
  start: MicrosoftStartIn;
  back: string;
  labelKey: MessageKey;
  run?: StepUpRun;
  check?: () => string | null;
  guard?: MicrosoftGuard;
  variant?: "primary" | "secondary";
}) {
  const t = useT();
  const api = useApi();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // A ref, not `busy`: two clicks in one frame both read `busy` as false.
  const inFlight = useRef(false);
  const reset = useCallback(() => {
    inFlight.current = false;
    setBusy(false);
  }, []);
  useBackFromMicrosoft(reset);

  /** Check, ask, leave. Throws the refusal, in words `describeAuthError` passes on. */
  async function leave(): Promise<typeof LEFT> {
    const problem = check?.() ?? null;
    if (problem !== null) throw new Error(problem);
    const ask = () => api.microsoftStart(start, run ? { signedIn: true } : {});
    const { authorize_url } = await (run ? run(ask) : ask());
    const url = checkedAuthorizeUrl(authorize_url);
    if (url === null) throw new Error(translate("auth.microsoft.failed"));
    rememberMicrosoftReturn({ intent: start.intent, back });
    navigation.assign(url);
    return LEFT;
  }

  async function go() {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    try {
      await leave();
      // Left: the button stays off until the page unloads (or comes back from the cache).
    } catch (cause: unknown) {
      setError(describeMicrosoftError(cause) ?? describeAuthError(cause, { canTimeOut: false }));
      reset();
    }
  }

  const off = guard ? guard.busy : busy;
  return (
    <>
      <div className="form-actions">
        {/* `key`, not a vendor's mark: the console's icon set has none, and
            the label says whose sign-in it is. */}
        <Btn
          variant={variant}
          icon="key"
          label={!guard && busy ? t("auth.common.working") : t(labelKey)}
          disabled={off}
          onClick={() => (guard ? guard.run(leave) : void go())}
        />
      </div>
      {guard ? null : <AuthAlert text={error} />}
    </>
  );
}
