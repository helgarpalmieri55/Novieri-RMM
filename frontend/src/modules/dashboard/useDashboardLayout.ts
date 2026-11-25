import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo } from "react";

import { useApi } from "../../lib/ApiContext";
import { useDraft } from "../../shell/components";
import {
  resolveLayout,
  type WidgetDefault,
  type WidgetPlacement,
} from "./layout";

export const DASHBOARD_LAYOUT_QUERY_KEY = "dashboard-layout";

/**
 * The operator's arrangement, resolved against the widgets this build has.
 *
 * A DRAFT WHILE EDITING, SAVED WITH ONE SAVE (redesign plan, Task 7, 2.1;
 * Receta FO). Rearranging used to write the whole arrangement after every
 * move, so removing a tile could not be undone (Atera's flaw too: "Quitar un
 * widget es un clic sin confirmación ni deshacer"). Now every move changes a
 * draft (`useDraft`) and nothing reaches the server until the save bar's
 * Save: the same last-write-wins PUT of the WHOLE arrangement as before.
 * Discard puts the arrangement back without a call.
 *
 * WHAT HAPPENS WHEN THE SAVE FAILS is that the screen keeps the arrangement,
 * still unsaved (`markSent(null)`), and the page says the save failed.
 * Reverting would throw away work the operator can see themselves doing.
 *
 * THE QUERY IS NOT REFETCHED AFTER A SAVE. Its answer is what we just sent;
 * the cache is set from the response instead, and the draft adopts it.
 */
export function useDashboardLayout(available: WidgetDefault[]) {
  const api = useApi();
  const queryClient = useQueryClient();
  const stored = useQuery({
    queryKey: [DASHBOARD_LAYOUT_QUERY_KEY],
    queryFn: () => api.getDashboardLayout(),
    staleTime: 60_000,
  });

  const availableKey = available.map((widget) => widget.id).join(",");
  const saved = useMemo(
    () => (stored.isSuccess ? resolveLayout(stored.data.widgets, available) : undefined),
    // `available` is keyed on its ids, as the page builds it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [stored.isSuccess, stored.data, availableKey],
  );
  const { draft, setDraft, dirty, discard, markSent } = useDraft<WidgetPlacement[]>(saved);

  const save = useMutation({
    mutationFn: (widgets: WidgetPlacement[]) => api.saveDashboardLayout(widgets),
    onSuccess: (response) => {
      queryClient.setQueryData([DASHBOARD_LAYOUT_QUERY_KEY], response);
    },
    // Not saved: the arrangement on screen is still a change.
    onError: () => markSent(null),
  });

  const reset = useMutation({
    mutationFn: () => api.resetDashboardLayout(),
    onSuccess: () => {
      queryClient.setQueryData([DASHBOARD_LAYOUT_QUERY_KEY], { widgets: null });
      setDraft(resolveLayout(null, available));
    },
  });

  /** Apply a rearrangement to the draft. A transform that changes nothing
   *  returns the same array (see `moveWidget`), and nothing changes. */
  function apply(transform: (current: WidgetPlacement[]) => WidgetPlacement[]) {
    if (draft === undefined) return;
    const next = transform(draft);
    if (next !== draft) setDraft(next);
  }

  /** The save bar's Save: the whole draft, as one PUT; `then` once the server took it. */
  function commit(then?: () => void) {
    if (draft === undefined) return;
    markSent(draft);
    save.mutate(draft, { onSuccess: () => then?.() });
  }

  return {
    layout: draft ?? null,
    loading: stored.isLoading,
    apply,
    reset: () => reset.mutate(),
    saveError: save.error,
    resetting: reset.isPending,
    dirty,
    discard: () => {
      save.reset();
      discard();
    },
    commit,
    saving: save.isPending,
  };
}
