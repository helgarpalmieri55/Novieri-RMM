/**
 * The budget pages' dialogs in the URL, and how a dialog tells its page that
 * it holds changes (redesign Task 11; Receta FO; coordinator rulings 3 and 4).
 *
 * `?edit=<id>` opens a row's edit dialog the way the kit's `useNewParam` opens
 * an add dialog with `?new=<kind>`: `start` adds a history entry, so Back
 * closes it; `close` goes Back from the entry this document's `start` added,
 * and otherwise removes the key in place (a link that arrived with it).
 *
 * A PAGE HAS ONE GUARD (`useUnsavedGuard`; two dirty guards at once are a
 * defect the kit reports). Its dialogs report whether they hold changes with
 * `useReportDirty`, and the page gives its one guard the combined answer.
 */
import { useCallback, useEffect } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";

import { writeQuery } from "../../shell/components";

const OPENED_IN = Math.random().toString(36).slice(2);
interface EditMark {
  readonly budgetEditOpened?: string;
}

export function useEditParam(): { id: string; start: (id: string) => void; close: () => void } {
  const [params, setParams] = useSearchParams();
  const location = useLocation();
  const navigate = useNavigate();
  const id = params.get("edit") ?? "";
  const start = useCallback(
    (next: string) => {
      const query = new URLSearchParams(params);
      query.set("edit", next);
      writeQuery(params, query, setParams, { state: { budgetEditOpened: OPENED_IN } satisfies EditMark });
    },
    [params, setParams],
  );
  const close = useCallback(() => {
    if (!params.has("edit")) return;
    if ((location.state as EditMark | null)?.budgetEditOpened === OPENED_IN) {
      void navigate(-1);
      return;
    }
    const query = new URLSearchParams(params);
    query.delete("edit");
    writeQuery(params, query, setParams, { replace: true });
  }, [params, setParams, location.state, navigate]);
  return { id, start, close };
}

/** Tells the page whether a form holds changes, for its one guard; forgets them when the form goes. */
export function useReportDirty(dirty: boolean, onDirty: (dirty: boolean) => void): void {
  useEffect(() => onDirty(dirty), [dirty, onDirty]);
  useEffect(() => () => onDirty(false), [onDirty]);
}
