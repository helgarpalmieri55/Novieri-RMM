import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { useApi } from "../../lib/ApiContext";
import {
  resolveLayout,
  type WidgetDefault,
  type WidgetPlacement,
} from "./layout";

export const DASHBOARD_LAYOUT_QUERY_KEY = "dashboard-layout";

/**
 * The operator's arrangement, resolved against the widgets this build has.
 *
 * OPTIMISTIC LOCAL STATE, WRITTEN THROUGH. Every rearrangement is applied to
 * local state first and saved in the background, because the alternative —
 * awaiting a round trip per move — makes dragging a tile feel broken and makes
 * four consecutive moves take four visible pauses. The save is a plain
 * last-write-wins PUT of the WHOLE arrangement, which is the right shape for
 * this data: the document is small, the writer is one person, and a merge of
 * two orderings has no meaning anyone could define.
 *
 * WHAT HAPPENS WHEN THE SAVE FAILS is that the screen keeps the arrangement
 * and the page says the save failed. Reverting would throw away work the
 * operator can see themselves doing, to reach a state they did not ask for;
 * an operator whose network blipped wants their layout back on the next save,
 * not their five moves undone.
 *
 * THE QUERY IS NOT REFETCHED AFTER A SAVE. Its answer is what we just sent,
 * and a refetch mid-drag would replace the arrangement under the operator's
 * hands with a stale copy. The cache is set from the response instead.
 */
export function useDashboardLayout(available: WidgetDefault[]) {
  const api = useApi();
  const queryClient = useQueryClient();
  const stored = useQuery({
    queryKey: [DASHBOARD_LAYOUT_QUERY_KEY],
    queryFn: () => api.getDashboardLayout(),
    staleTime: 60_000,
  });

  const [local, setLocal] = useState<WidgetPlacement[] | null>(null);

  // Seeded ONCE, when the stored answer arrives, and re-seeded when the set of
  // available widgets changes — which is what happens as permissions load: the
  // page cannot know which widgets exist until `GET /me` answers, so a layout
  // resolved before that would be resolved against an empty list.
  const availableKey = available.map((widget) => widget.id).join(",");
  useEffect(() => {
    if (!stored.isSuccess) return;
    setLocal(resolveLayout(stored.data.widgets, available));
    // `availableKey` rather than `available`, which is a fresh array each
    // render and would re-seed the layout on every one of them — wiping an
    // in-progress rearrangement continuously.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stored.isSuccess, stored.data, availableKey]);

  const save = useMutation({
    mutationFn: (widgets: WidgetPlacement[]) => api.saveDashboardLayout(widgets),
    onSuccess: (response) => {
      queryClient.setQueryData([DASHBOARD_LAYOUT_QUERY_KEY], response);
    },
  });

  const reset = useMutation({
    mutationFn: () => api.resetDashboardLayout(),
    onSuccess: () => {
      queryClient.setQueryData([DASHBOARD_LAYOUT_QUERY_KEY], { widgets: null });
      setLocal(resolveLayout(null, available));
    },
  });

  /** Apply a rearrangement and persist it. A transform that changes nothing
   *  returns the same array (see `moveWidget`), and that save is skipped. */
  function apply(transform: (current: WidgetPlacement[]) => WidgetPlacement[]) {
    setLocal((current) => {
      if (current == null) return current;
      const next = transform(current);
      if (next !== current) save.mutate(next);
      return next;
    });
  }

  return {
    layout: local,
    loading: stored.isLoading,
    apply,
    reset: () => reset.mutate(),
    saveError: save.error,
    resetting: reset.isPending,
  };
}
