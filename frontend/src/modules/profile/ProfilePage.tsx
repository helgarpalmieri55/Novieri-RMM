// Your profile — `GET/PATCH /api/v1/me`.
//
// A PAGE AT `/profile`, NOT A TAB IN SETTINGS, since 2026-08-20: a profile
// reached from the account menu behind your own avatar, which is where every
// other product puts it, and a URL that a link can land on.
//
// THE SPLIT CHANGED WITH THE PLATFORM'S OWN SIGN-IN (own-auth design §1.1,
// §2). The identity provider used to own the password, the second factor and
// the passkeys, and the three buttons that stood here sent the person to its
// pages for one step each. The platform verifies all of them itself now, so
// they are managed on a page of their own -- Sign-in and security,
// `/profile/security` -- where every change asks for a recent second factor
// first (design §2.5). This page links there and does nothing else with a
// credential:
//
//   this page        display name, phone, timezone, locale
//   /profile/security password, passkeys, authenticator app, recovery codes,
//                     trusted computers, sessions
//
// So this page still has no password field and no email box. The address a
// person signs in with is not changed here: that needs the re-authentication
// and verification this page does not perform.
//
// THE PAGE IS v3's `profile` SCREEN: the head, then one untitled panel of
// fields with the primary Save under them, then the sign-in panel, kept apart
// so the Save is unambiguously the profile's.

import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { describeSubmitError } from "../../components/form";
import type { Problem } from "../../components/form";
import { Field, inputClass } from "../../components/ui";
import { useLanguage, useT } from "../../i18n/context";
import { resolveLanguage } from "../../i18n/resolve";
import { KIND_ICON, PageHead, Panel } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { useMe, useUpdateMeMutation } from "../settings/api";
import { SaveBar } from "../settings/SaveBar";
import { isDirty } from "../settings/settingsState";
import type { MeProfileUpdate } from "../settings/types";

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
        {/* A LINK, BECAUSE IT GOES SOMEWHERE: a page of its own, not an
            action that runs here. Drawn as the kit's small button so it has
            the design's height, focus ring and hover, like the three buttons
            it replaces. */}
        <div style={{ marginTop: 12 }}>
          <Link className="btn small" to="/profile/security">
            <Icon name="lock" size={14} />
            <span>{t("profile.security.open")}</span>
          </Link>
        </div>
      </Panel>
    </>
  );
}
