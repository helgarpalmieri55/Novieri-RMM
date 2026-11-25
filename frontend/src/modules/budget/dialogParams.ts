/**
 * The budget pages' dialogs in the URL, and how a dialog tells its page that
 * it holds changes (redesign Task 11; Receta FO; coordinator rulings 3 and 4).
 *
 * `?edit=<id>` is the kit's `useEditParam` (adoption pass: this file's own
 * copy is gone). What stays here is what the kit has no piece for:
 *
 * - `useDropParam`: removes a key in place, for an `?edit=` that names
 *   nothing the page holds (fix round 1, M10). The kit's `close()` goes Back
 *   from the entry its own `start()` added; dropping never does, so a record
 *   that leaves the list while its editor is open takes no one back a step.
 * - `useReportDirty`: A PAGE HAS ONE GUARD (`useUnsavedGuard`; two dirty
 *   guards at once are a defect the kit reports). Its dialogs report whether
 *   they hold changes with it, and the page gives its one guard the combined
 *   answer.
 */
import { useCallback, useEffect } from "react";
import { useSearchParams } from "react-router-dom";

import { writeQuery } from "../../shell/components";

/** Removes `?<key>=` in place, whoever added it: for a value that names nothing the page holds. */
export function useDropParam(key: string): () => void {
  const [params, setParams] = useSearchParams();
  return useCallback(() => {
    if (!params.has(key)) return;
    const query = new URLSearchParams(params);
    query.delete(key);
    writeQuery(params, query, setParams, { replace: true });
  }, [params, setParams, key]);
}

/** Tells the page whether a form holds changes, for its one guard; forgets them when the form goes. */
export function useReportDirty(dirty: boolean, onDirty: (dirty: boolean) => void): void {
  useEffect(() => onDirty(dirty), [dirty, onDirty]);
  useEffect(() => () => onDirty(false), [onDirty]);
}
