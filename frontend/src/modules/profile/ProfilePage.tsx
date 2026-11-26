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
// THE PAGE IS THE KIT'S FORM PATTERN (redesign plan, Task 12, 7.2; Receta
// FO; Atera's "My profile"): the head with no back link (it is reached from
// the account menu, and the crumb says "Your account"), one section of
// fields, the console's one save bar (decision D22: "Unsaved changes ·
// Discard · Save", drawn only with a change), then the sign-in panel with its
// one link. Leaving with a change asks first.

import { useState } from "react";
import { Link } from "react-router-dom";

import { describeSubmitError, Form } from "../../components/form";
import { Field, inputClass } from "../../components/ui";
import { useLanguage, useT } from "../../i18n/context";
import { resolveLanguage } from "../../i18n/resolve";
import {
  FormPage,
  FormSection,
  KIND_ICON,
  SaveBar,
  UnsavedGuard,
  useDraft,
} from "../../shell/components";
import { Icon } from "../../shell/icons";
import { useMe, useUpdateMeMutation } from "../settings/api";
import { FOLDING_FORM_GRID } from "../settings/formGrid";
import type { MeProfileUpdate } from "../settings/types";
import { useRecordTitle } from "../../shell/title";

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
  // Reached from the account menu, under no rail entry: the shell's half of
  // the tab is the product alone, so the page names itself, as its h1 does
  // (general ruling 1b): "<this page> · Novieri RMM".
  useRecordTitle(t("profile.page.title"));
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
  // THE KIT'S DRAFT: what the server holds is the saved state; a background
  // refetch never overwrites typing, and a save's answer becomes the new
  // saved state (`markSent`).
  const { draft, setDraft, dirty, discard, markSent } = useDraft<ProfileForm>(me.data ? seed : undefined);
  const form = draft ?? seed;
  const setForm = (change: (current: ProfileForm) => ProfileForm) => setDraft(change(form));

  // What the select shows: the account's saved language if it is one the
  // console supports, otherwise the language the console is showing now.
  const shownLanguage = resolveLanguage(form.locale, null, language);

  const editable = me.data != null;

  function submit() {
    // Enter in an untouched form writes nothing (no change, no audit entry):
    // with the kit's bar there is no Save to press until something changed.
    if (!dirty) return;
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
    const picked = languagePicked;
    markSent(form);
    update.mutate(body, {
      onSuccess: () => {
        if (picked) setLanguage(shownLanguage, { persist: false });
        setLanguagePicked(false);
      },
      onError: () => markSent(null),
    });
  }

  return (
    // The kit's form page with NO back link (batch 3, B3-2: `back` optional):
    // this page is reached from the account menu, not from a list.
    <FormPage title={t("profile.page.title")} description={t("profile.page.description")}>
      <UnsavedGuard dirty={dirty} />

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
        // A REAL `<form>`: Enter in a field saves, through the bar's Save.
        <Form onSubmit={submit}>
          <FormSection title={t("profile.page.details")} icon="user">
            {/* Notes at the notices' measure (76ch), never the section's full width. */}
            <p className="note" style={{ marginBottom: 14, maxWidth: "76ch" }}>
              {t("profile.page.signedInAs", { email: me.data?.email ?? "" })}
            </p>

            <div className="form-grid" style={FOLDING_FORM_GRID}>
              <Field label={t("profile.page.displayName")}>
                <input
                  className={inputClass}
                  value={form.display_name}
                  disabled={!editable}
                  onChange={(event) => setForm((current) => ({ ...current, display_name: event.target.value }))}
                />
              </Field>
              <Field label={t("profile.page.phone")}>
                <input
                  className={inputClass}
                  value={form.phone}
                  disabled={!editable}
                  onChange={(event) => setForm((current) => ({ ...current, phone: event.target.value }))}
                />
              </Field>
              <Field label={t("profile.page.timezone")}>
                <input
                  className={inputClass}
                  value={form.timezone}
                  disabled={!editable}
                  placeholder={t("profile.page.timezonePlaceholder")}
                  onChange={(event) => setForm((current) => ({ ...current, timezone: event.target.value }))}
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

            <p className="note" style={{ marginTop: 10, maxWidth: "76ch" }}>
              {t("profile.page.smsNote")}
            </p>
          </FormSection>

          {/* THE ONE BAR: "Saved." only after the server's 2xx, never
              optimistic; the language is applied with the save, not before. */}
          <SaveBar
            submit
            dirty={dirty}
            saving={update.isPending}
            error={update.error}
            saved={update.isSuccess && !dirty}
            onDiscard={() => {
              discard();
              setLanguagePicked(false);
              update.reset();
            }}
          />
        </Form>
      )}

      {/* The same frame as the details above (critique, P2): a section, not a
          second card style on one page. */}
      <FormSection title={t("profile.security.title")} icon="shield-lock">
        <p className="note" style={{ marginBottom: 6, maxWidth: "76ch" }}>
          {t("profile.security.subtitle")}
        </p>
        <p className="note" style={{ maxWidth: "76ch" }}>
          {t("profile.security.note")}
        </p>
        <div className="form-actions">
          {/* A LINK, BECAUSE IT GOES SOMEWHERE: it opens a page of this
              console and changes nothing by itself. Drawn as the kit's button
              (`btn`), the way Reports and Jobs draw theirs. */}
          <Link className="btn" to="/profile/security">
            <Icon name="shield-lock" size={14} />
            <span>{t("profile.security.open")}</span>
          </Link>
        </div>
      </FormSection>
    </FormPage>
  );
}
