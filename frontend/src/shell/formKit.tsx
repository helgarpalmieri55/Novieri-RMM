/**
 * THE FORM PATTERN (design §2, "Formulario"): a full page with back and title,
 * collapsible sections, and ONE sticky bar -- "Unsaved changes · Discard ·
 * Save" -- for the whole console. Atera's walkthrough counts six ways to save;
 * this console has one. A page with changes asks before it is left.
 */
import { useCallback, useContext, useEffect, useId, useRef, useState, type ReactElement, type ReactNode } from "react";
import { Link, UNSAFE_DataRouterContext, useBeforeUnload, useBlocker, type Location } from "react-router-dom";

import { ConfirmDialog } from "../components/confirm";
import { describeSubmitError, type Problem } from "../components/form";
import { useT } from "../i18n/context";
import { Btn } from "./components";
import { Icon } from "./icons";
import { KIND_ICON } from "./viz";
import "./kit.css";

export function FormPage({
  back,
  title,
  description,
  children,
}: {
  back: { to: string; label: string };
  title: string;
  description?: string;
  children: ReactNode;
}) {
  return (
    <div className="form-page">
      <section className="ph">
        <Link className="back-link" to={back.to}>
          <Icon name="chevron-left" size={14} />
          {back.label}
        </Link>
        <div className="ph-row">
          <div>
            <h1>{title}</h1>
            {description ? <p className="sub">{description}</p> : null}
          </div>
        </div>
      </section>
      {children}
    </div>
  );
}

export function FormSection({
  title,
  icon,
  defaultOpen = true,
  children,
}: {
  title: string;
  icon?: string;
  defaultOpen?: boolean;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const bodyId = useId();
  return (
    <section className="panel form-section">
      <h2 className="form-section-h">
        <button
          type="button"
          className="form-section-toggle"
          aria-expanded={open}
          aria-controls={bodyId}
          onClick={() => setOpen((value) => !value)}
        >
          {icon ? <Icon name={icon} size={16} /> : null}
          <span>{title}</span>
          <Icon name="chevron-down" size={14} className={open ? "chev open" : "chev"} />
        </button>
      </h2>
      <div id={bodyId} className="panel-b" hidden={!open}>
        {children}
      </div>
    </section>
  );
}

/**
 * The one save bar. Drawn only while there is something to say: a change, a
 * save in flight, a failure, or "Saved." once the server answered.
 *
 * IT DOES WHAT THE BUTTONS IT REPLACES DID (settings' `SaveBar`, the ticket's
 * "Save changes"), so no page loses a behaviour by moving here: the reason it
 * cannot save (`blockedReason`: Rule 1, disabled with the reason shown), the
 * form's own `problems`, the confirmation after a 2xx (`saved`, never an
 * optimistic one), and Enter in a field (`submit`: the bar sits inside the
 * page's `<Form>` and its Save is that form's submit button).
 *
 * `blockedReason` IS "YOU MAY NOT" OR "NOT RIGHT NOW", NEVER MISSING INPUT
 * (`components/form.tsx`, Rule 1): a permission the person lacks ("an
 * org-admin can change these"), or a state of the record. A required field
 * left empty keeps Save enabled and is explained on submit, through
 * `problems`.
 *
 * WIRING A SAVE: call `useDraft`'s `markSent(draft)` when the request goes
 * out, and `markSent(null)` if it fails, so the server's answer becomes the
 * form's new state (see `useDraft`).
 */
export function SaveBar({
  dirty,
  saving,
  error,
  onDiscard,
  onSave,
  submit = false,
  saved = false,
  savedMessage,
  problems = [],
  blockedReason,
}: {
  dirty: boolean;
  saving: boolean;
  error?: unknown;
  onDiscard: () => void;
  onSave?: () => void;
  submit?: boolean;
  saved?: boolean;
  savedMessage?: string;
  problems?: readonly Problem[];
  blockedReason?: string;
}) {
  const t = useT();
  if (!dirty && !saving && !error) {
    return saved ? (
      <div className="savebar saved" role="status">
        <Icon name={KIND_ICON.ok} size={15} />
        <span className="savebar-what">{savedMessage ?? t("shell.kit.saved")}</span>
      </div>
    ) : null;
  }
  const failure = error
    ? describeSubmitError(error)
    : problems.length > 0
      ? `${problems.map((problem) => problem.message).join(". ")}.`
      : null;
  return (
    <div className="savebar" role="region" aria-label={t("shell.kit.unsaved")}>
      <span className="savebar-what">{saving ? t("shell.kit.saving") : t("shell.kit.unsaved")}</span>
      {failure ? (
        <span className="savebar-error" role="alert">
          {failure}
        </span>
      ) : null}
      {blockedReason ? <span className="savebar-blocked">{blockedReason}</span> : null}
      <Btn label={t("shell.kit.discard")} onClick={onDiscard} disabled={saving} />
      <Btn
        label={saving ? t("shell.kit.saving") : t("shell.kit.save")}
        variant="primary"
        type={submit ? "submit" : "button"}
        onClick={submit ? undefined : onSave}
        disabled={saving || !dirty || blockedReason !== undefined}
      />
    </div>
  );
}

/**
 * What the form shows, against what the server has. A new answer from the
 * server replaces the draft only while nothing was being edited: a refetch
 * must not wipe what someone is typing. Compared by value, so a refetch that
 * returns the same record is not a change.
 *
 * AND WHEN THE ANSWER IS TO THIS FORM'S OWN SAVE (review fix round 1, item
 * 3). The server answers a save with the record it stored, which is rarely
 * the bytes the form sent: an `updated_at`, a normalised value, a field the
 * form never edits. Judged as "a refetch while someone was editing", that
 * answer left the form dirty after a successful save, and leaving asked
 * "Leave without saving?" about changes the server already had. So the page
 * calls `markSent(draft)` when it sends, and the next answer is adopted when
 * the draft is still exactly what was sent. Anything typed after the send
 * makes the draft differ from it, and wins. A failed save calls
 * `markSent(null)`, so a later refetch cannot replace the unsaved change.
 *
 * Wiring: take `markSent` from `useDraft(query.data)`; in the save bar's
 * `onSave`, call `markSent(draft)` and then send the draft; in the save's
 * error handler, call `markSent(null)`. (Prose, not code, on purpose:
 * `test/secretMutations.test.ts` reads source text and would take a sample
 * mutation here for a real one.)
 */
export function useDraft<T>(saved: T | undefined): {
  draft: T | undefined;
  setDraft: (next: T) => void;
  dirty: boolean;
  discard: () => void;
  /** The value sent to the server (`null`: forget it, the save failed). */
  markSent: (sent: T | null) => void;
} {
  const savedKey = JSON.stringify(saved ?? null);
  const [draft, setDraftState] = useState<T | undefined>(saved);
  const [baseKey, setBaseKey] = useState(savedKey);
  const [sentKey, setSentKey] = useState<string | null>(null);
  const draftKey = JSON.stringify(draft ?? null);
  if (savedKey !== baseKey) {
    setBaseKey(savedKey);
    if (draftKey === baseKey || draftKey === sentKey) setDraftState(saved);
    if (sentKey !== null) setSentKey(null);
  }
  const setDraft = useCallback((next: T) => setDraftState(next), []);
  const discard = useCallback(() => setDraftState(saved), [saved]);
  const markSent = useCallback(
    (sent: T | null) => setSentKey(sent === null ? null : JSON.stringify(sent)),
    [],
  );
  return { draft, setDraft, dirty: saved !== undefined && draftKey !== savedKey, discard, markSent };
}

/**
 * A PAGE WITH CHANGES ASKS BEFORE IT IS LEFT (design §5). Inside the console's
 * data router (decision D7) a navigation is blocked and the person chooses;
 * a reload or a closed tab gets the browser's own question. Under a router
 * that cannot block -- a test mounted with `MemoryRouter` -- only the browser
 * half applies.
 *
 * LEAVING IS A CHANGE OF PATH (decision D16). A change of query alone -- a
 * tab, a filter, `?new=`, the record a save just selected -- keeps the page,
 * so it does not ask, unless the page says its query IS what it edits:
 * `search: true` (Settings, where a tab is another form), or the keys whose
 * change unmounts the form, `search: ["new"]` for a form in a `?new=` dialog
 * or `["tab"]` for one in a `?tab=` panel (review fix round 1, item 5). A
 * form that lives in such a panel and names no key is dropped, change and
 * all, by Back or by closing it.
 *
 * ONE GUARD HOLDING CHANGES PER PAGE (review fix round 1, item 2). A router
 * honours one blocker, the last registered (react-router 7.18.1,
 * "A router only supports one blocker at a time"), so the question is mounted
 * only while `dirty`: a clean guard elsewhere on the page cannot disarm a
 * dirty one. Two guards dirty at once still cannot both ask, so that logs a
 * `console.error`, which fails any test that does it; a page with two forms
 * gives one guard their combined `dirty`.
 *
 * A SAVE THAT LEAVES, LEAVES. `useBlocker` reads its condition from an
 * effect, so a handler that saves and navigates in one step is judged by the
 * render before the save, with the change still in it (pre-flight I-1,
 * reproduced with react-router 7.18.1). `release()` is read at once and lets
 * ONE navigation through, the next one the guard would have stopped, if it
 * starts in the same step: call it right before the `navigate()` it is for.
 * Unused, it expires at the end of that step, so a save that did not leave
 * (it failed, or the code took another branch) leaves the guard armed
 * (review fix round 1, item 7).
 */
export function useUnsavedGuard(
  dirty: boolean,
  options: { search?: GuardedQuery } = {},
): { guard: ReactElement | null; release: () => void } {
  const released = useRef(false);
  useEffect(() => {
    if (!dirty) released.current = false;
  }, [dirty]);
  const release = useCallback(() => {
    released.current = true;
    setTimeout(() => {
      released.current = false;
    }, 0);
  }, []);
  useBeforeUnload(
    useCallback(
      (event: BeforeUnloadEvent) => {
        if (!dirty || released.current) return;
        event.preventDefault();
        event.returnValue = "";
      },
      [dirty],
    ),
  );
  const dataRouter = useContext(UNSAFE_DataRouterContext);
  const guard = dataRouter && dirty ? <LeaveQuestion search={options.search ?? false} released={released} /> : null;
  return { guard, release };
}

/** The guard for a page that does not leave when it saves. */
export function UnsavedGuard({ dirty, search = false }: { dirty: boolean; search?: GuardedQuery }) {
  return useUnsavedGuard(dirty, { search }).guard;
}

/** What of the query the form lives in: `true`, all of it; a list, those keys; `false`, none. */
export type GuardedQuery = boolean | readonly string[];

/** Whether a navigation leaves the form: its path, or the part of its query that holds it. */
function leavesForm(current: Location, next: Location, search: GuardedQuery): boolean {
  if (current.pathname !== next.pathname) return true;
  if (search === false) return false;
  if (search === true) return current.search !== next.search;
  const before = new URLSearchParams(current.search);
  const after = new URLSearchParams(next.search);
  return search.some((key) => before.get(key) !== after.get(key));
}

/** How many questions are mounted, that is how many guards hold changes right now. */
let guardsHoldingChanges = 0;

function LeaveQuestion({ search, released }: { search: GuardedQuery; released: { current: boolean } }) {
  const t = useT();
  useEffect(() => {
    guardsHoldingChanges += 1;
    if (guardsHoldingChanges > 1) {
      console.error(
        "UnsavedGuard: two UnsavedGuards hold changes at once. A router honours one blocker, so only the last " +
          "would ask; give the page one guard fed the combined `dirty` (shell/formKit.tsx).",
      );
    }
    return () => {
      guardsHoldingChanges -= 1;
    };
  }, []);
  const blocker = useBlocker(({ currentLocation, nextLocation }) => {
    if (!leavesForm(currentLocation, nextLocation, search)) return false;
    // `release()` lets this one through, and only this one.
    if (released.current) {
      released.current = false;
      return false;
    }
    return true;
  });
  return (
    <ConfirmDialog
      open={blocker.state === "blocked"}
      title={t("shell.kit.leaveTitle")}
      subject=""
      body={t("shell.kit.leaveBody")}
      confirmLabel={t("shell.kit.leave")}
      destructive
      onCancel={() => blocker.reset?.()}
      onConfirm={() => blocker.proceed?.()}
    />
  );
}
