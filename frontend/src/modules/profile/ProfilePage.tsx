// Your profile -- `GET/PATCH /api/v1/me`, and the way to your security.
//
// A PAGE AT `/profile`, NOT A TAB IN SETTINGS, since 2026-08-20: it is your
// own record rather than administration, reached from the account menu
// behind your own avatar, which is where every other product puts it.
//
// THE SPLIT, NOW THAT THE PLATFORM IS ITS OWN IDENTITY PROVIDER (own
// authentication design §1.1, §2). This page edits how the console shows you
// -- display name, phone, timezone, language. How you SIGN IN -- your
// password, passkeys, authenticator app, recovery codes, remembered devices
// and sessions -- is one page further, at `/profile/security`
// (`SecurityPage.tsx`), behind the recent confirmation of the second factor
// where the design asks for it (§2.5). It used to be three buttons that left
// the console for the identity provider's own pages, one step each; there is
// no second application any more, so the panel below is a link inside this
// one.
//
// THE EMAIL ADDRESS IS NOT CHANGED HERE: a sign-in address changes through a
// re-authenticated, verified flow this console does not offer, and the note
// under the title says to ask an administrator.
//
// THE PAGE IS v3's `profile` SCREEN: the head, then one untitled panel of
// fields with the primary Save under them, then the sign-in panel with its
// one link -- a panel of its own, so the Save above it is unambiguously the
// profile's.

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
import { FOLDING_FORM_GRID } from "../settings/formGrid";
import type { MeProfileUpdate } from "../settings/types";

type ProfileForm = {
  display_name: string;
  phone: string;
  timezone: string;
  locale: string;
};

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
   * disabled chip carrying no on-screen reason it could not be pressed. This
   * page now takes the click and says why, which is rule 1's own preference
   * where the refusal is the console's rather than the server's.
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
          </>
        )}
      </Panel>

      <Panel title={t("profile.security.title")} subtitle={t("profile.security.subtitle")}>
        <p className="note">{t("profile.security.note")}</p>
        <div className="form-actions">
          {/* A LINK, BECAUSE IT GOES SOMEWHERE: it opens a page of this
              console and changes nothing by itself. Drawn as the kit's button
              (`btn`), the way Reports and Jobs draw theirs. */}
          <Link className="btn" to="/profile/security">
            <Icon name="shield-lock" size={14} />
            <span>{t("profile.security.open")}</span>
          </Link>
        </div>
      </Panel>
    </>
  );
}
