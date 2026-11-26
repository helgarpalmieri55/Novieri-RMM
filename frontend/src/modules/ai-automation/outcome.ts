/**
 * What became of a proposal, in the operator's words. The wire's `state` is
 * the server's vocabulary (`app/models/ai.py`); anything this table does not
 * name renders as itself through `StatusPill`'s own classification, so an
 * unknown state is visible rather than mislabelled.
 *
 * In its own file because the list and the action's own head both draw it
 * (redesign plan, Task 12, 7.6: the head carries the action's state).
 */
import { lazyFields } from "../../i18n/lazy";
import type { Kind } from "../../shell/components";

const OUTCOME = {
  previewed: lazyFields({ kind: "warn" as Kind }, { label: "aiAutomation.outcome.previewed" }),
  confirmed: lazyFields({ kind: "ok" as Kind }, { label: "aiAutomation.outcome.confirmed" }),
  dispatched: lazyFields({ kind: "ok" as Kind }, { label: "aiAutomation.outcome.dispatched" }),
  rejected: lazyFields({ kind: "mut" as Kind }, { label: "aiAutomation.outcome.rejected" }),
  expired: lazyFields({ kind: "mut" as Kind }, { label: "aiAutomation.outcome.expired" }),
} as const;

export function outcomeOf(state: string): { label: string; kind: Kind } | undefined {
  return (OUTCOME as Record<string, { label: string; kind: Kind } | undefined>)[state];
}
