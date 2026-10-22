/**
 * First boot — the only screen an installation shows before it has an owner.
 *
 * WHAT THIS REPLACES. Steps 8–10 of GO-LIVE: a `docker compose exec` into the
 * api container to run a Python one-liner, an `INSERT` for the organization
 * row, and a second one for the role grant. That is a runbook, and a runbook is
 * a product defect the moment somebody who is not the vendor installs this
 * (`docs/superpowers/specs/2026-08-19-invisible-identity-layer-design.md` §3).
 * Install, open the URL, type the code the installer printed, enter your name
 * and your organization — and you are the administrator.
 *
 * WHY A CODE AND NOT JUST "NOBODY HAS CLAIMED IT". An appliance answers on the
 * network long before its owner sits down at it. Zero users alone would mean
 * whoever reaches the URL first owns the fleet. The server checks both facts
 * (`backend/app/api/v1/setup.py`); this screen's job is to collect the code and
 * to say something useful when it is wrong.
 *
 * THIS PAGE IS NOT INSIDE THE APP SHELL, AND CANNOT BE. The shell asks
 * `/api/v1/me` for a session that does not exist yet, and every route inside it
 * is behind `RequireAuth`. So this is a standalone full-page form, styled from
 * the same tokens, reached from `App.tsx` above the auth gate. It is the
 * design's `.panel` on the design's page ground, with the page-title rung for
 * its headline and the form kernel's field and actions row: v3 draws no
 * screen for first boot, so it is composed from the kit alone.
 *
 * NO CREDENTIAL IS COLLECTED HERE. The form takes an address; the identity
 * provider mails an enrolment link and the password is set there, exactly as
 * for every account the Users tab creates. A password field on this screen
 * would be the first place a password ever entered this process, and there is
 * no such place.
 */

import { useRef, useState, type CSSProperties } from "react";

import { describeSubmitError } from "../../components/form";
import { useT } from "../../i18n/context";
import { Trans } from "../../i18n/Trans";
import { Field, inputClass } from "../../components/ui";
import { ApiError, requestHeaders } from "../../lib/api";
import { signIn } from "../../lib/auth";
import { Btn, KIND_ICON } from "../../shell/components";
import { Icon } from "../../shell/icons";

/** `GET /api/v1/setup/status` — one bit, deliberately (see the route's docstring). */
export type SetupStatus = { needed: boolean };

/** `POST /api/v1/setup/claim`, 201. Carries no credential and no install code. */
export type Claimed = {
  organization_id: string;
  organization_slug: string;
  user_id: string;
  email: string;
  invitation_sent: boolean;
  invitation_error: string | null;
};

/**
 * Ask whether this installation still needs setting up.
 *
 * UNGATED AND DELIBERATELY NOT ON `ApiClient`. That client attaches a bearer
 * token and, on a 401, kicks the browser into a sign-in redirect. This question
 * is asked before any of that exists, and the answer decides whether a sign-in
 * redirect would be a dead end.
 */
export async function fetchSetupStatus(): Promise<SetupStatus> {
  const response = await fetch("/api/v1/setup/status", {
    headers: requestHeaders(null, { Accept: "application/json" }),
  });
  if (!response.ok) throw new Error(`setup status: HTTP ${response.status}`);
  return (await response.json()) as SetupStatus;
}

/**
 * What went wrong, in the server's own words where they are usable.
 *
 * THIS USED TO BE A LOCAL COPY of the error-describing logic (cycle 4 found
 * several across the product, each with its own idea of what a failure sounds
 * like). It now defers to `describeSubmitError`, and the difference is not
 * cosmetic: the local version handled `detail` as a STRING and dropped a 422's
 * `detail` LIST on the floor, so the one refusal that names the offending
 * field — "Email address: value is not a valid email address" — arrived as
 * "the server answered 422". Wrapping the response in `ApiError` is what the
 * rest of the product's error paths already look like, so this screen gets the
 * same sentences as every other form despite not going through `ApiClient`.
 *
 * WHY NOT `ApiClient` ITSELF: it attaches a bearer token and, on a 401, kicks
 * the browser into a sign-in redirect. This request is made before any of that
 * exists. Only the error TYPE is borrowed.
 */
async function refusal(response: Response): Promise<string> {
  // `.text()` rather than `.json()`, matching how `ApiClient` builds an
  // `ApiError`: a proxy's HTML error page must not throw on the way to being
  // reported.
  let body = "";
  try {
    body = await response.text();
  } catch {
    // A body that cannot even be read leaves the status to speak.
  }
  return describeSubmitError(new ApiError(response.status, body));
}

type Form = { code: string; name: string; email: string; organization: string };

const EMPTY: Form = { code: "", name: "", email: "", organization: "" };

/** The page ground and the one card on it: the pre-auth surface with room to breathe. */
const GROUND: CSSProperties = { minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center", background: "var(--bg)", padding: 32 };
const CARD: CSSProperties = { width: "100%", maxWidth: 520, padding: 32, margin: 0 };
/** The page-title rung (`.ph h1`), on a screen that has no `.ph`. */
const HEADLINE: CSSProperties = { fontSize: "var(--t-h1)", fontWeight: 600, letterSpacing: "-.015em", margin: 0 };

export function SetupPage() {
  const t = useT();
  const [form, setForm] = useState<Form>(EMPTY);
  const [error, setError] = useState<string | null>(null);
  const [claimed, setClaimed] = useState<Claimed | null>(null);
  const [submitting, setSubmitting] = useState(false);

  /**
   * The double-click guard, and it is a ref rather than the `submitting` state
   * above because state is applied on the NEXT render. Two clicks inside one
   * frame — which is what a double-click is — both read `submitting === false`
   * and both POST. The server serializes claims too (an advisory lock), so the
   * second would be refused with a 409; that is a correct outcome presented as
   * an error on a screen where nothing went wrong. Both halves are wanted: the
   * server's, because a browser cannot be trusted; this one, because the buyer
   * should never see the collision.
   */
  const inFlight = useRef(false);

  const complete =
    form.code.trim() !== "" &&
    form.name.trim() !== "" &&
    form.email.trim() !== "" &&
    form.organization.trim() !== "";

  function set(field: keyof Form, value: string) {
    setForm((current) => ({ ...current, [field]: value }));
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (inFlight.current || !complete) return;
    inFlight.current = true;
    setSubmitting(true);
    setError(null);
    try {
      const response = await fetch("/api/v1/setup/claim", {
        method: "POST",
        headers: requestHeaders(null, { "Content-Type": "application/json", Accept: "application/json" }),
        body: JSON.stringify({
          code: form.code.trim(),
          email: form.email.trim(),
          display_name: form.name.trim(),
          organization_name: form.organization.trim(),
        }),
      });
      if (!response.ok) {
        // The form keeps everything the operator typed: a wrong code is a typo
        // in one field, and clearing the other three would make correcting it a
        // punishment.
        setError(await refusal(response));
        return;
      }
      setClaimed((await response.json()) as Claimed);
    } catch (cause: unknown) {
      // `String(cause)` rendered "TypeError: Failed to fetch" at the operator.
      // The lead sentence is the load-bearing half — nothing was created — and
      // the describe call supplies the rest without the constructor name.
      setError(t("setup.page.unreachable", { reason: describeSubmitError(cause) }));
    } finally {
      inFlight.current = false;
      setSubmitting(false);
    }
  }

  return (
    <div style={GROUND}>
      <div className="panel" style={CARD}>
        {claimed ? <Claim claimed={claimed} /> : null}
        {claimed ? null : (
          <>
            <h1 style={HEADLINE}>{t("setup.page.title")}</h1>
            <p className="note" style={{ marginTop: 6 }}>
              {t("setup.page.intro")}
            </p>

            <form style={{ marginTop: 20 }} onSubmit={(event) => void submit(event)}>
              <Field label={t("setup.page.installCode")}>
                <input
                  className={inputClass}
                  value={form.code}
                  autoComplete="off"
                  spellCheck={false}
                  onChange={(event) => set("code", event.target.value)}
                />
              </Field>
              <Field label={t("setup.page.yourName")}>
                <input
                  className={inputClass}
                  value={form.name}
                  onChange={(event) => set("name", event.target.value)}
                />
              </Field>
              <Field label={t("setup.page.yourEmail")}>
                <input
                  className={inputClass}
                  type="email"
                  value={form.email}
                  onChange={(event) => set("email", event.target.value)}
                />
              </Field>
              <Field label={t("setup.page.organizationName")}>
                <input
                  className={inputClass}
                  value={form.organization}
                  onChange={(event) => set("organization", event.target.value)}
                />
              </Field>

              {error ? (
                <p role="alert" className="notice red">
                  <Icon name={KIND_ICON.crit} size={16} />
                  <span>{error}</span>
                </p>
              ) : null}

              <div className="form-actions">
                <Btn
                  type="submit"
                  label={submitting ? t("setup.page.settingUp") : t("setup.page.finish")}
                  icon="check"
                  variant="primary"
                  disabled={!complete || submitting}
                />
                <span className="form-hint">
                  {t("setup.page.passwordHint")}
                </span>
              </div>
            </form>
          </>
        )}
      </div>
    </div>
  );
}

/**
 * What happened, said honestly.
 *
 * A fresh installation usually has no email configured, so the invitation is
 * the step most likely to fail — and it fails AFTER the organization and the
 * administrator exist, which is the one outcome a green checkmark would
 * misrepresent. When the mail did not go, this says so and says what to do,
 * rather than sending the operator to a sign-in page that will never let them
 * in.
 */
function Claim({ claimed }: { claimed: Claimed }) {
  const t = useT();
  const [signInError, setSignInError] = useState<string | null>(null);
  return (
    <>
      <h1 style={HEADLINE}>{t("setup.claim.title")}</h1>
      <p className="note" style={{ marginTop: 6 }}>
        <Trans
          k="setup.claim.belongsTo"
          vars={{ email: claimed.email }}
          components={{ b: <b style={{ color: "var(--ink)" }} /> }}
        />
      </p>
      {claimed.invitation_sent ? (
        <p className="note" style={{ marginTop: 14 }}>
          {t("setup.claim.checkInbox")}
        </p>
      ) : (
        <p role="alert" className="notice red" style={{ marginTop: 14 }}>
          <Icon name={KIND_ICON.crit} size={16} />
          <span>
            {claimed.invitation_error ?? t("setup.claim.invitationFailed")}
          </span>
        </p>
      )}
      <div className="form-actions" style={{ marginTop: 20 }}>
        {/* `void signIn()` SWALLOWED THE REJECTION ENTIRELY. `signinRedirect`
            fetches the provider's discovery document first, so an identity
            provider that is not up yet — the likeliest state of a freshly
            installed appliance — rejected here and the button did visibly
            nothing, on the last screen of an installation, with no other way
            forward. */}
        <Btn
          label={t("setup.claim.goToSignIn")}
          icon="lock"
          variant="primary"
          onClick={() => {
            setSignInError(null);
            signIn().catch((cause: unknown) => setSignInError(describeSubmitError(cause)));
          }}
        />
      </div>
      {signInError ? (
        <p role="alert" className="notice red" style={{ marginTop: 12 }}>
          <Icon name={KIND_ICON.crit} size={16} />
          <span>{t("setup.claim.signInFailed", { reason: signInError })}</span>
        </p>
      ) : null}
    </>
  );
}
