import { useEffect } from "react";

/**
 * THE SLA PAGE'S ONE GUARD (general ruling 3; final review M5). Three parts
 * of `/sla` hold changes: the Add policy and Add calendar forms in their
 * `?new=` dialogs, and a calendar's week editor, which keeps its own Save week
 * and Revert because a save replaces the whole week (coordinator, O2 Q1). A
 * router honours one blocker, so the page owns the guard and each part says
 * whether it holds changes (the way the budget pages do it). Closing a dialog
 * is leaving it (`?new=`); the week lives on the page, so only leaving the
 * page leaves it.
 */
export interface SlaGuardWiring {
  /** Whether this part holds changes. */
  readonly onDirty: (dirty: boolean) => void;
  /** Lets through the navigation a Cancel or a successful save makes (`useUnsavedGuard`). */
  readonly release: () => void;
}

/** Tells the page whether a part holds changes, for its one guard; forgets them when the part goes. */
export function useReportDirty(dirty: boolean, onDirty: (dirty: boolean) => void): void {
  useEffect(() => onDirty(dirty), [dirty, onDirty]);
  useEffect(() => () => onDirty(false), [onDirty]);
}
