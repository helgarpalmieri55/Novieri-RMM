/**
 * `/sign-in/microsoft` — Microsoft's return (own-auth phase 2, Task 12,
 * decision 2; design §4).
 *
 * THE FRAGMENT IS READ ONCE AND LEAVES THE ADDRESS, like an emailed link's
 * token (`./flow.ts::useLinkToken`). The code and the state go to the server
 * in a body, ONCE: a `ref` holds the request across React's double mount, and
 * a second one would only be refused (the state is single-use).
 *
 * WHAT THE SERVER ANSWERS DECIDES THE NEXT PAGE, as on `/sign-in`:
 * `signed_in` -> where the person was going; `factor_required` and
 * `enroll_required` -> the steps of the first sign-in, carrying it; `linked`
 * -> back to Sign-in and security. Microsoft's own refusal (`#error=`) is
 * said in this console's words -- its `error_description` is never shown --
 * and only an empty code goes, to spend the transaction (decision 2).
 *
 * A PAGE FOR PEOPLE WITHOUT A SESSION, like the other sign-in pages: no `/me`
 * here (a client's contact has none, and the harmony gate's fixture session
 * would always answer one).
 */
import { useEffect, useRef, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";

import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import { useApi } from "../../lib/ApiContext";
import { enterConsole, signInPath } from "../../lib/session";
import { AuthAlert, AuthCard } from "./AuthCard";
import { describeAuthError } from "./errors";
import { carryNext } from "./flow";
import { describeMicrosoftError, forgetMicrosoftReturn, microsoftReturnOf, readMicrosoftReturn } from "./microsoft";

export function MicrosoftCallbackPage() {
  const t = useT();
  const api = useApi();
  const location = useLocation();
  const navigate = useNavigate();
  const [answer] = useState(() => microsoftReturnOf(location.hash));
  const [target] = useState(() => readMicrosoftReturn());
  const [error, setError] = useState<string | null>(() =>
    answer === null
      ? translate("auth.microsoft.failed")
      : "error" in answer
        ? translate("auth.microsoft.refusedByMicrosoft")
        : null,
  );
  const sent = useRef(false);

  useEffect(() => {
    if (location.hash) navigate({ pathname: location.pathname, search: location.search }, { replace: true });
  }, [location.hash, location.pathname, location.search, navigate]);

  useEffect(() => {
    if (sent.current) return;
    sent.current = true;
    forgetMicrosoftReturn();
    if (answer !== null && "error" in answer) {
      // Decision 2 (A2-70): an empty code spends the transaction and clears
      // its cookie now; the 400 it answers changes nothing on this page.
      if (answer.state) api.microsoftComplete({ code: "", state: answer.state }).catch(() => undefined);
      return;
    }
    if (answer === null) return;
    api
      .microsoftComplete({ code: answer.code, state: answer.state })
      .then((step) => {
        if (step.state === "signed_in") enterConsole(target.back);
        else if (step.state === "linked") navigate(target.back, { replace: true });
        else if (step.state === "enroll_required") navigate(carryNext("/sign-in/enroll", target.back), { replace: true });
        else
          navigate(carryNext("/sign-in/factor", target.back), {
            replace: true,
            state: { methods: step.methods, rememberDays: step.remember_device_days ?? null },
          });
      })
      .catch((cause: unknown) => {
        setError(describeMicrosoftError(cause) ?? describeAuthError(cause, { canTimeOut: false }));
      });
  }, [answer, api, navigate, target]);

  // For a link, `target.back` is the security page -- the console's or the
  // portal's; for a sign-in, `signInPath` picks the portal's sign-in page when
  // `back` is a portal page.
  const signedIn = target.intent === "link";
  return (
    <AuthCard title={t("auth.microsoft.returnTitle")}>
      {error === null ? (
        <p className="note" style={{ margin: 0 }}>
          {t("auth.common.working")}
        </p>
      ) : (
        <>
          <AuthAlert text={error} />
          <p className="note" style={{ margin: 0 }}>
            <Link to={signedIn ? target.back : signInPath(target.back)}>
              {t(signedIn ? "auth.microsoft.backToSecurity" : "auth.microsoft.backToSignIn")}
            </Link>
          </p>
        </>
      )}
    </AuthCard>
  );
}
