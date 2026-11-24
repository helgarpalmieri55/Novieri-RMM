/**
 * THE FORM PATTERN (design §2, "Formulario"): a full page with back and title,
 * collapsible sections, and ONE sticky bar -- "Unsaved changes · Discard ·
 * Save" -- for the whole console. Atera's walkthrough counts six ways to save;
 * this console has one. A page with changes asks before it is left.
 */
import { useCallback, useContext, useEffect, useId, useRef, useState, type ReactElement, type ReactNode } from "react";
import { Link, UNSAFE_DataRouterContext, useBeforeUnload, useBlocker } from "react-router-dom";

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
 */
export function useDraft<T>(saved: T | undefined): {
  draft: T | undefined;
  setDraft: (next: T) => void;
  dirty: boolean;
  discard: () => void;
} {
  const savedKey = JSON.stringify(saved ?? null);
  const [draft, setDraftState] = useState<T | undefined>(saved);
  const [baseKey, setBaseKey] = useState(savedKey);
  const draftKey = JSON.stringify(draft ?? null);
  if (savedKey !== baseKey) {
    setBaseKey(savedKey);
    if (draftKey === baseKey) setDraftState(saved);
  }
  const setDraft = useCallback((next: T) => setDraftState(next), []);
  const discard = useCallback(() => setDraftState(saved), [saved]);
  return { draft, setDraft, dirty: saved !== undefined && draftKey !== savedKey, discard };
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
 * so it does not ask, unless the page says its query IS what it edits
 * (`search`: Settings, where a tab is another form).
 *
 * A SAVE THAT LEAVES, LEAVES. `useBlocker` reads its condition from an
 * effect, so a handler that saves and navigates in one step is judged by the
 * render before the save, with the change still in it (pre-flight I-1,
 * reproduced with react-router 7.18.1). `release()` is read at once and lets
 * the next navigation through; a new change arms the guard again.
 */
export function useUnsavedGuard(
  dirty: boolean,
  options: { search?: boolean } = {},
): { guard: ReactElement | null; release: () => void } {
  const released = useRef(false);
  useEffect(() => {
    if (!dirty) released.current = false;
  }, [dirty]);
  const release = useCallback(() => {
    released.current = true;
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
  const guard = dataRouter ? <LeaveQuestion dirty={dirty} search={options.search ?? false} released={released} /> : null;
  return { guard, release };
}

/** The guard for a page that does not leave when it saves. */
export function UnsavedGuard({ dirty, search = false }: { dirty: boolean; search?: boolean }) {
  return useUnsavedGuard(dirty, { search }).guard;
}

function LeaveQuestion({
  dirty,
  search,
  released,
}: {
  dirty: boolean;
  search: boolean;
  released: { readonly current: boolean };
}) {
  const t = useT();
  const blocker = useBlocker(
    ({ currentLocation, nextLocation }) =>
      dirty &&
      !released.current &&
      (currentLocation.pathname !== nextLocation.pathname || (search && currentLocation.search !== nextLocation.search)),
  );
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
