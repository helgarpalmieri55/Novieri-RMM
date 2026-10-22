import { useQuery } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Form, FormActions, attemptProblems } from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import { Field, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import { Icon } from "../../shell/icons";
import { ApiError } from "../../lib/api";
import type { Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { useCreatePortalTicketMutation, usePortalCategories, useUploadPortalAttachmentMutation } from "./api";
import { HelpArticleDialog, HelpSuggestions } from "./HelpSuggestions";
import { PortalNav } from "./PortalNav";
import {
  PORTAL_PRIORITY_OPTIONS,
  attachmentLimitHint,
  groupPortalCategories,
  oversizedAttachmentMessage,
} from "./portalState";
import type { PortalTicketPriority } from "./types";
import { PageHead, Panel } from "../../shell/components";
import { Chips } from "../../shell/viz";

// "New ticket" (spec §7): category, subject, description, optional
// attachment. Category is required (schema.py's PortalTicketCreate) --
// there is no `type` field to pick separately; the server resolves it from
// the chosen category.

// Auditor A (HIGH), /portal/new -> Submit: "Submit with an empty form is a
// silent no-op -- the seventh occurrence." The cause was
// `disabled={... || !categoryId || !subject || ...}`: the two fields the
// server insists on were exactly the two the dead button refused to name.
// Submit stays enabled now and answers through this list. `description` is
// NOT here -- PortalTicketCreate defaults it to "" (schema.py), and demanding
// a field the API accepts empty is the same lie pointing the other way.
//
// A function, and fed the files as plain data: the sentences are worded when
// the refusal is shown, so they follow a language switch, and a file too large
// is a rule of its own (named by the file, refused by `oversizedAttachmentMessage`).
const ticketRules = (files: ReadonlyArray<{ name: string; size: number }> = []): readonly FieldRule[] => [
  { name: "categoryId", label: translate("portal.newTicket.category") },
  { name: "subject", label: translate("portal.newTicket.subject") },
  ...files.map((file, at) => ({
    name: `attachment-${at}`,
    label: file.name,
    validate: () => oversizedAttachmentMessage(file),
  })),
];

export function NewTicketPage() {
  const t = useT();
  const api = useApi();
  const navigate = useNavigate();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const permissions = me.data?.permissions ?? [];
  const canRead = permissions.includes("portal.ticket.read");

  // Categories are a secondary, enriching call for this page (spec §5's
  // "degrade quietly" requirement, `DeviceSummaryPanel`'s precedent in
  // service-desk/TicketDetailPage.tsx): a portal caller could in principle
  // hold `portal.ticket.write` without `portal.ticket.read`, and this must
  // not render an error banner or a broken page if that call 403s -- the
  // form still renders, with a quiet note in place of the category picker
  // and submission disabled (there is nothing valid to submit without a
  // category id).
  const categories = usePortalCategories();
  const categoriesForbidden = categories.error instanceof ApiError && categories.error.status === 403;

  const create = useCreatePortalTicketMutation();
  const upload = useUploadPortalAttachmentMutation();
  const fileInput = useRef<HTMLInputElement>(null);

  const [categoryId, setCategoryId] = useState("");
  const [subject, setSubject] = useState("");
  const [helpArticle, setHelpArticle] = useState<string | null>(null);
  const [description, setDescription] = useState("");
  const [priority, setPriority] = useState<PortalTicketPriority>("normal");
  // The chip row is index-addressed; the wire value stays the state. A value
  // the list does not carry falls back to the first chip rather than to -1,
  // which would draw a row with nothing selected on a form that always has a
  // priority.
  const priorityIndex = Math.max(
    0,
    PORTAL_PRIORITY_OPTIONS.findIndex((option) => option.value === priority),
  );
  // `PORTAL_PRIORITY_OPTIONS` is a non-empty literal and `priorityIndex` is
  // clamped to 0, so index 0 always exists; `noUncheckedIndexedAccess` cannot
  // see that, and a fallback that invented a grade would be worse than one
  // that names the first.
  const chosenPriority = PORTAL_PRIORITY_OPTIONS[priorityIndex] ?? PORTAL_PRIORITY_OPTIONS[0];
  // Auditor A (LOW): "Attachment input accepts any file type, one at a
  // time, with no stated limits ... Users troubleshooting an issue typically
  // have several screenshots; being able to attach only one, with no
  // explanation, forces them to file multiple tickets or omit evidence."
  // A LIST now, uploaded one call after another because the route takes one
  // file per request (`POST /portal/tickets/{id}/attachments`).
  const [files, setFiles] = useState<File[]>([]);
  // How many of `files` the server has already accepted for
  // `filedTicketId`. A ref, not state: nothing renders from it, and it must
  // survive the re-render a failed upload causes so that pressing Submit
  // again resumes at the file that failed instead of re-sending the ones
  // that succeeded.
  const uploadedCount = useRef(0);
  // Set once the POST succeeds, so a retry after a failed attachment upload
  // resumes rather than filing a duplicate ticket.
  const [filedTicketId, setFiledTicketId] = useState<string | null>(null);
  // Set ON SUBMIT, never while typing: "Subject is required" appearing
  // before the first keystroke lands is its own kind of unhelpful. Held as the
  // rules and the values they were checked against, so the sentences are
  // worded at render and follow a language switch.
  const [attempt, setAttempt] = useState<Attempt | null>(null);

  async function fileTicket() {
    // RESUME, DO NOT RE-FILE. The ticket is created first and the attachment
    // second, so an upload that fails leaves a real ticket behind -- and
    // before this, pressing Submit again created a SECOND one. Remembering
    // the id means the retry retries the half that failed.
    const ticketId =
      filedTicketId ??
      (
        await create.mutateAsync({
          category_id: categoryId,
          // Trimmed to match what was validated -- `requiredProblems` trims
          // before testing emptiness, so an untrimmed send could carry a
          // subject the checks never actually saw.
          subject: subject.trim(),
          description: description.trim(),
          priority,
        })
      ).id;
    setFiledTicketId(ticketId);
    // Sequential, and resumable: `uploadedCount` only advances past a file
    // the server actually took, so a failure part-way through a batch leaves
    // the retry pointing at the first file that did not land.
    for (let at = uploadedCount.current; at < files.length; at += 1) {
      const next = files[at];
      if (!next) break;
      await upload.mutateAsync({ id: ticketId, file: next });
      uploadedCount.current = at + 1;
    }
    navigate(`/portal/tickets/${ticketId}`);
  }

  function submit() {
    // A file the server is going to refuse, refused here instead -- with the
    // filename in the sentence, in the same announced summary every other
    // problem on this form uses. The files are copied to plain name/size pairs
    // so the rules read nothing that can change after the click, and each file
    // is present in the values (its size) so only the size rule can refuse it.
    const submitted = files.map((file) => ({ name: file.name, size: file.size }));
    const values: Record<string, string> = { categoryId, subject };
    submitted.forEach((file, at) => {
      values[`attachment-${at}`] = String(file.size);
    });
    const next: Attempt = { rules: () => ticketRules(submitted), values };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    // Both mutations record their own failure and `FormActions` renders it,
    // so the rejection is swallowed here rather than left to surface as an
    // unhandled promise rejection that tells the requester nothing.
    void fileTicket().catch(() => {});
  }

  return (
    <div>
      <PageHead
        title={t("portal.newTicket.title")}
        description={t("portal.newTicket.description")}
      />
      <PortalNav canRead={canRead} />

      <Panel>
        {/* A real <form>, so Enter in Subject files the ticket instead of
            doing nothing -- the other half of the same finding. */}
        <Form onSubmit={submit}>
          {/* `required` states the same fact for assistive tech; it blocks
              nothing, because <Form> is noValidate -- a native bubble that
              vanishes on the next click is the silent no-op wearing a hat.
              No "*" markers: this form already declares optionality the other
              way round ("Attachment (optional)"), and two conventions for one
              fact in one form is worse than either. */}
          <Field label={t("portal.newTicket.category")}>
            <select
              className={inputClass}
              required
              value={categoryId}
              disabled={categoriesForbidden}
              onChange={(e) => setCategoryId(e.target.value)}
            >
              <option value="">{t("portal.newTicket.selectCategory")}</option>
              {/* Grouped, not flat: see portalState.groupPortalCategories --
                  the flat alphabetical list put "Software" directly beside
                  "Software request" with nothing to tell them apart. */}
              {groupPortalCategories(categories.data ?? []).map((group) => (
                <optgroup key={group.label} label={group.label}>
                  {group.categories.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.name}
                    </option>
                  ))}
                </optgroup>
              ))}
            </select>
          </Field>
          {categoriesForbidden && (
            <p className="form-hint" style={{ marginBottom: 12 }}>
              {t("portal.newTicket.categoriesForbidden")}
            </p>
          )}

          <Field label={t("portal.newTicket.subject")}>
            <input
              className={inputClass}
              required
              value={subject}
              onChange={(e) => setSubject(e.target.value)}
            />
          </Field>
          <HelpSuggestions subject={subject} enabled={canRead} onOpen={setHelpArticle} />

          {/* Auditor A (HIGH): "Ticket Description is a single-line <input>
              here too -- the defect is systemic to ticket creation ... This
              is the *end-user* reporting form -- the page where a
              non-technical employee describes what is broken, pastes an
              error message, or lists what they tried. A single-line input
              cannot hold or display that, accepts no newlines, and signals
              'a few words will do'." */}
          <Field label={t("portal.newTicket.descriptionLabel")}>
            <textarea
              className={inputClass}
              rows={6}
              placeholder={t("portal.newTicket.descriptionPlaceholder")}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </Field>

          {/* v3 draws urgency as a CHIP ROW of plain sentences
              (`P['portal/new']`: "I can work around it" / "It is slowing me
              down" / "I cannot work"), not as a dropdown. Four chips rather
              than the mock's three, because the wire has four priorities and
              this port loses no control; `Chips` renders each as a real
              button carrying `aria-pressed`, inside a `role="group"` named by
              the field.

              The grade the chosen sentence maps to is named under the row.
              Without it the requester could not tell that "I cannot work at
              all" files an urgent ticket, which is the one thing about this
              control they might reasonably want to know. */}
          {/* A `div.field`, not `Field`: `Field` renders a real `<label>`,
              and a label wrapping a group of buttons labels nothing. The
              caption is the same `.field > span` the other fields draw, and
              the group carries its own accessible name through `Chips`. */}
          <div className="field">
            <span>{t("portal.newTicket.urgent")}</span>
            <Chips
              label={t("portal.newTicket.urgent")}
              items={PORTAL_PRIORITY_OPTIONS.map((option) => option.label)}
              active={priorityIndex}
              onSelect={(index) => {
                const option = PORTAL_PRIORITY_OPTIONS[index];
                if (option) setPriority(option.value as PortalTicketPriority);
              }}
            />
            <p className="form-hint" style={{ marginTop: 6 }}>
              {t("portal.newTicket.filedAs", { name: chosenPriority?.name ?? "" })}
            </p>
          </div>

          <Field label={t("portal.newTicket.attachments")}>
            {/* The native input stays (it is what the browser's picker and
                the tests drive) but is hidden; the drop target below is what
                the requester sees. */}
            <input
              ref={fileInput}
              type="file"
              multiple
              className="hidden"
              aria-label={t("portal.newTicket.attachments")}
              onChange={(e) => {
                // A NEW selection is a new batch: whatever was uploaded for
                // the previous one says nothing about this one, so the
                // resume counter goes back to the start.
                uploadedCount.current = 0;
                setFiles(Array.from(e.target.files ?? []));
              }}
            />
            {/* v3's dashed drop target (`P['portal/new']`: a `.inp` with
                `border-style:dashed`, centred, muted, "Drop a file or click"),
                in place of a plain secondary button. IT REALLY TAKES A DROP.
                Saying "drop files here" over a control that only opens a
                picker is a promise the page does not keep, so `onDrop` reads
                `dataTransfer.files` through the same setter the picker uses --
                one batch, one reset of the resume counter, one list below.

                A real `<button>`, so it is a tab stop, takes the global focus
                ring and is operable from the keyboard; the dashed edge is the
                only thing it borrows from `.inp`. */}
            <button
              type="button"
              className="inp focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--accent)]"
              style={{
                borderStyle: "dashed",
                justifyContent: "center",
                color: "var(--muted)",
                font: "inherit",
                cursor: "pointer",
              }}
              onClick={() => fileInput.current?.click()}
              onDragOver={(event) => event.preventDefault()}
              onDrop={(event) => {
                event.preventDefault();
                const dropped = Array.from(event.dataTransfer.files);
                if (dropped.length === 0) return;
                uploadedCount.current = 0;
                setFiles(dropped);
              }}
            >
              <Icon name="paperclip" size={14} />
              <span>{t("portal.newTicket.drop")}</span>
            </button>
            {files.length > 0 && (
              <ul style={{ margin: "8px 0 0", padding: 0, listStyle: "none" }}>
                {files.map((file) => (
                  <li key={`${file.name}-${file.size}`} className="cell-sub">
                    {file.name}
                  </li>
                ))}
              </ul>
            )}
          </Field>
          <p className="form-hint" style={{ marginBottom: 12 }}>{attachmentLimitHint()}</p>

          {/* Enabled with the fields empty ON PURPOSE -- the refusal is what
              the requester gets to read, and a control that will not move is
              not. The only disable left is the one case where no input could
              help: without the category list there is no valid category id to
              send, and now that reason is rendered beside the button instead
              of being left for the requester to infer. The server failure
              comes from here too, so the bodyless 503 that
              `{String(create.error)}` rendered as nothing now produces a
              sentence under `role="alert"`. */}
          <FormActions
            label={t("portal.newTicket.submit")}
            pending={create.isPending || upload.isPending}
            error={create.error ?? upload.error}
            problems={attemptProblems(attempt)}
            disabledReason={categoriesForbidden ? t("portal.newTicket.noCategory") : undefined}
          />
        </Form>
      </Panel>
      <HelpArticleDialog articleId={helpArticle} onClose={() => setHelpArticle(null)} />
    </div>
  );
}
