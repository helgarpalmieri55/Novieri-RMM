import { useCallback } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";

import { writeQuery } from "../../shell/components";

/**
 * ONE RECORD OPEN BESIDE A LIST, IN THE URL (`?finding=<id>` on the drift
 * page): a link opens it, a reload keeps it, Back closes it. Starting adds a
 * history entry; closing goes Back from the entry this page's `start` added
 * and otherwise (it arrived in a link) drops the key in place -- `useNewParam`'s
 * rule for `?new=`, so two copies of the list are never left behind.
 *
 * LOCAL, NOT THE KIT'S `useEditParam`: that one drives `?edit=` only, and this
 * key is `?finding=`. Its verbs are the kit's (`{ id, start(id), close() }`,
 * adoption pass), so the two read alike.
 */
export function useOpenParam(key: string): { id: string; start: (id: string) => void; close: () => void } {
  const [params, setParams] = useSearchParams();
  const location = useLocation();
  const navigate = useNavigate();
  const id = params.get(key) ?? "";
  const start = useCallback(
    (next: string) => {
      const query = new URLSearchParams(params);
      query.set(key, next);
      writeQuery(params, query, setParams, { state: { ipamOpened: { key, id: next } } });
    },
    [params, setParams, key],
  );
  const close = useCallback(() => {
    if (!params.has(key)) return;
    const mark = (location.state as { ipamOpened?: { key: string; id: string } } | null)?.ipamOpened;
    if (mark?.key === key && mark.id === params.get(key)) {
      void navigate(-1);
      return;
    }
    const query = new URLSearchParams(params);
    query.delete(key);
    writeQuery(params, query, setParams, { replace: true });
  }, [params, setParams, location.state, navigate, key]);
  return { id, start, close };
}
