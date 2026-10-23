// Your profile — `GET/PATCH /api/v1/me`, plus the three credential steps.
//
// A PAGE AT `/profile`, NOT A TAB IN SETTINGS, since 2026-08-20, and the move
// fixes a defect rather than only tidying the navigation. The credential steps
// below leave ITMS for the sign-in pages and come back; `startAccountAction`
// records where to return in the OIDC `state`, and that value is a URL. The
// Settings tab was NOT in the URL -- `SettingsPage` keeps it in React state --
// so every credential step returned the user to `/settings` and its DEFAULT
// tab, Organization. From the operator's chair that is indistinguishable from
// the action having done nothing at all, which is exactly how it was reported:
// "when I click it just refreshes and goes back to organization".
//
// With its own route the return is exact, and the operator's other request is
// satisfied at the same time: a profile reached from the account menu behind
// your own avatar, which is where every other product puts it.
//
// THE SPLIT IS THE WHOLE DESIGN (spec §7.1):
//
//   Keycloak owns  password, MFA device, passkeys, and the EMAIL ADDRESS
//   ITMS owns      display name, phone, timezone, locale
//
// So this page has no password field, no MFA control and no email box, and it
// is not going to grow one. `console-auth` §1 exists precisely so that the
// password field is not in this application, and invariant 3 forbids a
// credential in an API response — a "change password" form here would need
// both.
//
// WHAT THE CREDENTIAL BUTTONS DO NOW, AND WHAT THEY USED TO DO. They used to
// be ANCHORS to Keycloak's account console, in a new tab, labelled "(opens the
// account console)". That is a second application: different chrome, different
// navigation, the vendor's name on it. The operator hit exactly this in
// production, and it is the seam
// `docs/superpowers/specs/2026-08-19-invisible-identity-layer-design.md` §1.1
// exists to remove: a buyer does not know what Keycloak is and must never need
// to.
//
// They are now BUTTONS calling `startAccountAction`, which is an
// APPLICATION-INITIATED ACTION: a normal OIDC authorization redirect carrying
// `kc_action`, so the identity provider runs exactly one step — set a
// password, enrol a second factor, register a passkey — on the same themed
// pages the user signed in on, then redirects back into ITMS. One tab, one
// look, no second product.
//
// THE SECURITY PROPERTY IS UNCHANGED, WHICH IS THE POINT. The credential is
// still handed to the identity provider by the user's own re-authenticated
// session; ITMS neither sees it nor proxies it. Rejected — iframing the
// account console: Keycloak denies framing by default, so it would mean
// loosening `frame-ancestors` on exactly the pages where a hijacked click
// drops an MFA device. Rejected — proxying the actions through the
// provisioning credential: it keeps the buttons in ITMS but routes a
// SELF-SERVICE action through `manage-users` rather than the user's own
// session, which makes ITMS the thing that can reset anybody's MFA.
// Self-service should need the user's own session; that is the point of
// self-service.
//
// NOT VERIFIABLE IN CI, AND SAID SO IN THE SPEC (§1.1, falsifier 7): that each
// of the three actions completes and round-trips under the committed theme
// needs a live realm. The tests here pin which action each button asks for and
// that no link out survives; they cannot pin what the identity provider does
// with it.
//
// THE PAGE IS v3's `profile` SCREEN: the head, then one untitled panel of
// fields with the primary Save under them. v3 draws the sign-in state as two
// pills in a field; this console has three real controls there instead, and
// they get a panel of their own so the Save above them is unambiguously the
// profile's and not theirs.

import { useEffect, useState } from "react";

import { describeSubmitError } from "../../components/form";
import type { Problem } from "../../components/form";
import { Field, inputClass } from "../../components/ui";
import { useLanguage, useT } from "../../i18n/context";
import type { MessageKey } from "../../i18n/core";
import { resolveLanguage } from "../../i18n/resolve";
import { startAccountAction } from "../../lib/auth";
import type { AccountAction } from "../../lib/auth";
import { Btn, KIND_ICON, PageHead, Panel } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { useMe, useUpdateMeMutation } from "../settings/api";
import { SaveBar } from "../settings/SaveBar";
import { isDirty } from "../settings/settingsState";
import type { MeProfileUpdate } from "../settings/types";

const CREDENTIAL_ACTIONS: readonly { label: MessageKey; hint: MessageKey; action: AccountAction; icon: string }[] = [
  {
    label: "profile.security.changePassword.label",
    action: "UPDATE_PASSWORD",
    icon: "lock",
    hint: "profile.security.changePassword.hint",
  },
  {
    label: "profile.security.manageMfa.label",
    action: "CONFIGURE_TOTP",
    icon: "shield",
    hint: "profile.security.manageMfa.hint",
  },
  {
    label: "profile.security.addPasskey.label",
    action: "webauthn-register-passwordless",
    icon: "certificate",
    hint: "profile.security.addPasskey.hint",
  },
] as const;

type ProfileForm = {
  display_name: string;
  phone: string;
  timezone: string;
  locale: string;
};

/**
 * THE FOUR PROFILE FIELDS FOLD TO ONE COLUMN ON A HANDSET.
 *
 * `.form-grid` WAS `repeat(2, minmax(0, 1fr))` at every width: the rule meant
 * to fold it lived in `shell.css` and lost on import order to the
 * unconditional rule in `components.css`. So at 390px "Display name" / "Phone"
 * and "Timezone" / "Locale" stayed two-up at about 148px each — the only thing
 * on this page still in two columns, with both panels, the sign-in buttons and
 * their help lines folding around them. Reflow at that width is SC 1.4.10.
 * The kit rule has since landed and folds every `.form-grid` at 640px; this
 * one stays because it does more than fold.
 *
 * `auto-fit` counts the columns from the space available rather than from a
 * breakpoint: two while each can hold 260px (the width `shell.css` itself
 * names as where a tracked caption stops fitting above its control), one
 * below. `min(100%, 260px)` keeps the single column from overflowing a
 * container narrower than the minimum. The 560px ceiling is `.field`'s own
 * `max-width`, so a pair of fields is no wider than one field standing alone,
 * and `auto-fit` cannot draw a third and fourth column in a wide panel.
 *
 * Duplicated rather than imported from `modules/settings`, deliberately: a
 * module does not reach into another module for a style, and the kit is where
 * the one copy belongs once it lands there.
 */
const FOLDING_FORM_GRID = {
  gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 260px), 1fr))",
  maxWidth: 560,
} as const;

export function ProfilePage() {
  const me = useMe();
  const update = useUpdateMeMutation();
  const t = useT();
  const { language, setLanguage } = useLanguage();
  const [languagePicked, setLanguagePicked] = useState(false);

  /**
   * A credential step that could not even be STARTED.
   *
   * `void startAccountAction(...)` discarded the rejection: `signinRedirect`
   * fetches the provider's discovery document before it navigates, so an
   * identity provider that is down produced a button that did nothing at all
   * — which is precisely the report this page was rewritten to fix ("when I
   * click it just refreshes"), arriving through a second door.
   */
  const [actionError, setActionError] = useState<string | null>(null);

  // An ABSENT field is an empty box, never the string "undefined": a server
  // that predates migration 0066 serves no `phone`, and typing over the word
  // "undefined" is how a placeholder becomes a stored value.
  const seed: ProfileForm = {
    display_name: me.data?.display_name ?? "",
    phone: me.data?.phone ?? "",
    timezone: me.data?.timezone ?? "",
    locale: me.data?.locale ?? "",
  };
  const [form, setForm] = useState<ProfileForm>(seed);

  // Re-seed only when the SERVER'S values change — after a save, after a
  // refetch. Typing is never overwritten by a background refetch of the same
  // values. Same shape as the Organization tab, for the same reason.
  const seedSignature = JSON.stringify(seed);
  useEffect(() => {
    setForm(JSON.parse(seedSignature) as ProfileForm);
    setLanguagePicked(false);
    setRefusal([]);
  }, [seedSignature]);

  // What the select shows: the account's saved language if it is one the
  // console supports, otherwise the language the console is showing now.
  const shownLanguage = resolveLanguage(form.locale, null, language);

  const dirty = isDirty(form, seed);
  const editable = me.data != null;

  /**
   * A save this page refused, and the reason.
   *
   * **ONE CONTROL, ONE BEHAVIOUR, ACROSS THE GROUP.** This page and /settings
   * draw the same `SaveBar`, and they behaved differently: /settings passes
   * `requireDirty={false}`, so its "Save changes" is a live accent button that
   * refuses out loud; here the default left a clean form showing a pale
   * disabled chip, the same size, radius, icon and shape as the enabled
   * "Change password" 170px below it, carrying no on-screen reason it could
   * not be pressed. Two routes, one control, two weights and two answers to
   * the same click. This page now takes the click and says why, which is rule
   * 1's own preference where the refusal is the console's rather than the
   * server's.
   */
  const [refusal, setRefusal] = useState<Problem[]>([]);

  function submit() {
    // A save with nothing to save writes an audit entry for a change that is
    // not one, so it is still refused -- out loud, in the same words the
    // Organization tab uses.
    if (!dirty) {
      setRefusal([{ name: "display_name", message: t("profile.page.noChanges") }]);
      return;
    }
    setRefusal([]);
    // `""` is a CLEARED field, not an empty string to store: the columns are
    // nullable and "no phone number" is null.
    const blankToNull = (value: string): string | null =>
      value.trim() === "" ? null : value.trim();
    const body: MeProfileUpdate = {
      display_name: form.display_name.trim(),
      phone: blankToNull(form.phone),
      timezone: blankToNull(form.timezone),
      locale: blankToNull(form.locale),
    };
    // Only a language the PERSON picked is sent as a new value and applied.
    // An untouched field is sent exactly as it was read (a stored "en-GB"
    // stays "en-GB", a null stays null), so saving a phone number can never
    // change the language. The picked one is applied to the console once the
    // server has accepted it, without a second save: this PATCH is the save.
    if (languagePicked) body.locale = shownLanguage;
    update.mutate(body, {
      onSuccess: () => {
        if (languagePicked) setLanguage(shownLanguage, { persist: false });
      },
    });
  }

  return (
    <>
      <PageHead
        title={t("profile.page.title")}
        description={t("profile.page.description")}
      />

      <Panel>
        {me.isLoading ? (
          <p className="note">{t("profile.page.loading")}</p>
        ) : me.error ? (
          // The server's own reason (cycle 4), announced: "That did not load"
          // is the same sentence for a 403, a 404 on an API that predates
          // `GET /me`, and a proxy 503 — three different things to do next.
          <p role="alert" className="notice red">
            <Icon name={KIND_ICON.crit} size={16} />
            <span>{describeSubmitError(me.error)}</span>
          </p>
        ) : (
          <>
            <p className="note" style={{ marginBottom: 14 }}>
              {t("profile.page.signedInAs", { email: me.data?.email ?? "" })}
            </p>

            <div className="form-grid" style={FOLDING_FORM_GRID}>
              <Field label={t("profile.page.displayName")}>
                <input
                  className={inputClass}
                  value={form.display_name}
                  disabled={!editable}
                  onChange={(event) =>
                    setForm((current) => ({ ...current, display_name: event.target.value }))
                  }
                />
              </Field>
              <Field label={t("profile.page.phone")}>
                <input
                  className={inputClass}
                  value={form.phone}
                  disabled={!editable}
                  onChange={(event) =>
                    setForm((current) => ({ ...current, phone: event.target.value }))
                  }
                />
              </Field>
              <Field label={t("profile.page.timezone")}>
                <input
                  className={inputClass}
                  value={form.timezone}
                  disabled={!editable}
                  placeholder={t("profile.page.timezonePlaceholder")}
                  onChange={(event) =>
                    setForm((current) => ({ ...current, timezone: event.target.value }))
                  }
                />
              </Field>
              <Field label={t("common.language.label")}>
                <select
                  className={inputClass}
                  value={shownLanguage}
                  disabled={!editable}
                  onChange={(event) => {
                    setLanguagePicked(true);
                    setForm((current) => ({ ...current, locale: event.target.value }));
                  }}
                >
                  <option value="en">{t("common.language.english")}</option>
                  <option value="es">{t("common.language.spanish")}</option>
                </select>
              </Field>
            </div>

            <p className="note" style={{ marginTop: 10 }}>
              {t("profile.page.smsNote")}
            </p>

            <SaveBar
              canEdit={editable}
              dirty={dirty}
              // The same bar, the same way round, as the sibling route in this
              // group -- see `refusal` above.
              requireDirty={false}
              // Stale the moment anything is typed: the refusal says there is
              // nothing to save, and there now is.
              problems={dirty ? [] : refusal}
              pending={update.isPending}
              error={update.error}
              saved={update.isSuccess && !dirty}
              onSave={submit}
            />
            {update.isSuccess && !dirty && update.data.identity_locale_synced === false ? (
              // NON-BLOCKING: the save succeeded and the console already
              // switched; this only says the sign-in pages did not follow.
              <p role="status" className="notice gold" style={{ marginTop: 12 }}>
                <Icon name={KIND_ICON.warn} size={16} />
                <span>{t("profile.page.languageNotMirrored")}</span>
              </p>
            ) : null}
          </>
        )}
      </Panel>

      <Panel title={t("profile.security.title")} subtitle={t("profile.security.subtitle")}>
        <p className="note">
          {t("profile.security.note")}
        </p>
        <ul style={{ listStyle: "none", margin: "12px 0 0", padding: 0, display: "grid", gap: 12 }}>
          {CREDENTIAL_ACTIONS.map((item) => (
            <li key={item.action}>
              {/* THESE ARE ACTIONS, so they are buttons. Each one leaves the
                  console for a sign-in flow that changes a credential; drawn as
                  underlined text they read as prose, and they got none of the
                  design's height, weight, hover or focus ring. Secondary rather
                  than primary: the accent is held for the one control on a
                  screen that commits, and this panel has three peers. */}
              <Btn
                label={t(item.label)}
                icon={item.icon}
                small
                onClick={() => {
                  setActionError(null);
                  startAccountAction(item.action).catch((cause: unknown) =>
                    setActionError(describeSubmitError(cause)),
                  );
                }}
              />
              <div className="note" style={{ marginTop: 4 }}>{t(item.hint)}</div>
            </li>
          ))}
        </ul>
        {actionError ? (
          <p role="alert" className="notice red" style={{ marginTop: 12 }}>
            <Icon name={KIND_ICON.crit} size={16} />
            <span>{t("profile.security.startFailed", { reason: actionError })}</span>
          </p>
        ) : null}
      </Panel>
    </>
  );
}
