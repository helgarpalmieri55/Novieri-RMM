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
 */
import { useRef, useState } from "react";

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

export function MicrosoftButton({
  start,
  back,
  labelKey,
  run,
  check,
  variant = "secondary",
}: {
  start: MicrosoftStartIn;
  back: string;
  labelKey: MessageKey;
  run?: StepUpRun;
  check?: () => string | null;
  variant?: "primary" | "secondary";
}) {
  const t = useT();
  const api = useApi();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // A ref, not `busy`: two clicks in one frame both read `busy` as false.
  const inFlight = useRef(false);

  async function go() {
    if (inFlight.current) return;
    const problem = check?.() ?? null;
    if (problem !== null) {
      setError(problem);
      return;
    }
    inFlight.current = true;
    setBusy(true);
    setError(null);
    try {
      const ask = () => api.microsoftStart(start, run ? { signedIn: true } : {});
      const { authorize_url } = await (run ? run(ask) : ask());
      const url = checkedAuthorizeUrl(authorize_url);
      if (url === null) {
        setError(translate("auth.microsoft.failed"));
        return;
      }
      rememberMicrosoftReturn({ intent: start.intent, back });
      navigation.assign(url);
    } catch (cause: unknown) {
      setError(describeMicrosoftError(cause) ?? describeAuthError(cause, { canTimeOut: false }));
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  return (
    <>
      <div className="form-actions">
        {/* `key`, not a vendor's mark: the console's icon set has none, and
            the label says whose sign-in it is. */}
        <Btn
          variant={variant}
          icon="key"
          label={busy ? t("auth.common.working") : t(labelKey)}
          disabled={busy}
          onClick={() => void go()}
        />
      </div>
      <AuthAlert text={error} />
    </>
  );
}
