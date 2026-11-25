/**
 * WHAT EACH MAILBOX DOES WITH ITS MAIL, edited in place (redesign plan, Task
 * 7, 2.7; Receta FO): one section per mailbox with its two settings -- the
 * category its tickets are filed under and how long a reply reopens a closed
 * ticket -- and ONE draft, ONE save bar and ONE guard for every mailbox on the
 * page, so a change to two of them is saved, discarded and asked about as one.
 *
 * The connection (address, server, TLS, account, the password or the
 * Microsoft sign-in) stays in the mailbox's own dialog: a secret is written
 * there and never read back (invariant 3), and nothing here touches it. The
 * save is the same `PATCH /email-intake/mailboxes/{id}` the dialog makes
 * (`api.updateEmailMailbox`), with only the settings that changed.
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import type { Problem } from "../../components/form";
import { Field, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import { ApiError } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { FormSection, SaveBar, UnsavedGuard, useDraft } from "../../shell/components";
import { useEmailMailboxes } from "./api";
import { invalidReopenWindow } from "./MailboxForm";
import type { MailboxOut } from "./types";

/** One mailbox's two settings, as the fields hold them (the window as typed). */
interface MailboxSetting {
  readonly category: string;
  readonly reopen: string;
}

type Settings = Readonly<Record<string, MailboxSetting>>;

function settingsOf(mailboxes: readonly MailboxOut[]): Settings {
  return Object.fromEntries(
    mailboxes.map((m) => [m.id, { category: m.default_category_id, reopen: String(m.reopen_window_days) }]),
  );
}

/** What is wrong with the draft, named by the mailbox and the field (worded when read). */
function problemsOf(draft: Settings, mailboxes: readonly MailboxOut[]): Problem[] {
  const problems: Problem[] = [];
  for (const mailbox of mailboxes) {
    const setting = draft[mailbox.id];
    if (!setting) continue;
    if (setting.category.trim() === "") {
      problems.push({
        name: `${mailbox.id}-category`,
        message: translate("emailIntake.settings.categoryMissing", { address: mailbox.address }),
      });
    }
    const window = invalidReopenWindow(setting.reopen.trim());
    if (window) problems.push({ name: `${mailbox.id}-reopen`, message: `${mailbox.address}: ${window.replace(/\.$/, "")}` });
  }
  return problems;
}

export function MailboxSettings({ canAdmin }: { canAdmin: boolean }) {
  const t = useT();
  const api = useApi();
  const mailboxes = useEmailMailboxes();
  const queryClient = useQueryClient();
  // The same read `MailboxList` makes (one cache entry): the category picker's choices.
  const categories = useQuery({
    queryKey: ["email-intake-ticket-categories"],
    queryFn: () => api.listTicketCategories(),
    retry: false,
  });
  const categoriesForbidden = categories.error instanceof ApiError && categories.error.status === 403;

  const rows = mailboxes.data;
  const saved = useMemo(() => (rows ? settingsOf(rows) : undefined), [rows]);
  const { draft, setDraft, dirty, discard, markSent } = useDraft(saved);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<unknown>(null);
  // Set on Save, so a field being typed is not called wrong before it is done.
  const [asked, setAsked] = useState(false);

  if (!rows || rows.length === 0 || !draft) return null;
  const problems = asked ? problemsOf(draft, rows) : [];

  function change(id: string, patch: Partial<MailboxSetting>) {
    if (!draft) return;
    const current = draft[id];
    if (!current) return;
    setDraft({ ...draft, [id]: { ...current, ...patch } });
  }

  async function save() {
    if (!draft || !rows || !saved) return;
    setAsked(true);
    if (problemsOf(draft, rows).length > 0) return;
    setSaving(true);
    setError(null);
    markSent(draft);
    // EVERY ROW, THEN ONE REFETCH (fix round 1). Through the mutation, each
    // PATCH refetched the mailboxes on its own, and the draft adopted the
    // first answer mid-loop, with the second mailbox not saved yet. So the
    // PATCHes go out one after another, and the list is read again once, at
    // the end -- after `markSent(null)` when one of them failed, so that
    // answer cannot replace what is still unsaved.
    try {
      for (const mailbox of rows) {
        const next = draft[mailbox.id];
        const before = saved[mailbox.id];
        if (!next || !before) continue;
        const body: { default_category_id?: string; reopen_window_days?: number } = {};
        if (next.category !== before.category) body.default_category_id = next.category.trim();
        if (next.reopen !== before.reopen) body.reopen_window_days = Number(next.reopen.trim());
        if (Object.keys(body).length > 0) await api.updateEmailMailbox(mailbox.id, body);
      }
      setAsked(false);
    } catch (caught) {
      // Not saved: what is on screen is still a change, and the refetch must not wipe it.
      markSent(null);
      setError(caught);
    } finally {
      setSaving(false);
      void queryClient.invalidateQueries({ queryKey: ["email-intake-mailboxes"] });
    }
  }

  return (
    <>
      {rows.map((mailbox) => {
        const setting = draft[mailbox.id];
        if (!setting) return null;
        const choices = categories.data ?? [];
        const known = choices.some((c) => c.id === setting.category);
        return (
          <FormSection
            key={mailbox.id}
            title={t("emailIntake.settings.title", { address: mailbox.address })}
            icon="mail"
          >
            <div className="form-grid">
              <Field label={t("emailIntake.form.defaultCategory")} help={t("emailIntake.form.categoryHint")}>
                {categoriesForbidden ? (
                  // `ticket.read` is not `email_intake.admin`: an admin who cannot
                  // list categories still types the id they were given.
                  <input
                    className={inputClass}
                    placeholder={t("emailIntake.form.categoryId")}
                    value={setting.category}
                    disabled={!canAdmin}
                    onChange={(e) => change(mailbox.id, { category: e.target.value })}
                  />
                ) : (
                  <select
                    className={inputClass}
                    value={setting.category}
                    disabled={!canAdmin}
                    onChange={(e) => change(mailbox.id, { category: e.target.value })}
                  >
                    {known || setting.category === "" ? null : (
                      // A category the list does not carry (inactive, or not yet
                      // listed): named as such, never as a bare id.
                      <option value={setting.category}>{t("emailIntake.settings.unlisted", { id: setting.category })}</option>
                    )}
                    {choices.map((c) => (
                      <option key={c.id} value={c.id}>
                        {c.name}
                      </option>
                    ))}
                  </select>
                )}
              </Field>
              <Field label={t("emailIntake.form.reopenWindow")} help={t("emailIntake.settings.reopenHint")}>
                <input
                  className={inputClass}
                  type="text"
                  inputMode="numeric"
                  value={setting.reopen}
                  disabled={!canAdmin}
                  onChange={(e) => change(mailbox.id, { reopen: e.target.value })}
                />
              </Field>
            </div>
          </FormSection>
        );
      })}
      <UnsavedGuard dirty={dirty} />
      <SaveBar
        dirty={dirty}
        saving={saving}
        error={error ?? undefined}
        problems={problems}
        onDiscard={() => {
          discard();
          setAsked(false);
          setError(null);
        }}
        onSave={() => void save()}
      />
    </>
  );
}
