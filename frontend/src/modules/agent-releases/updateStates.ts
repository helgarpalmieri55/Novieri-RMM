/**
 * The states the agent self-update API reports, for the typed enum labels
 * (`enums.UpdateRolloutState` and friends). `UpdateRollout.state` and
 * `UpdateRing.state` in `lib/api.ts` carry the first two inline; the target's
 * is a string there, and these are the values the server's reconciler writes
 * (`agent_update/models.py`, `reconcile.py`, `service.py`).
 */
export type UpdateRolloutState = "draft" | "running" | "paused" | "completed" | "cancelled";
export type UpdateRingState = "pending" | "active" | "done";
export type UpdateTargetState =
  | "pending"
  | "dispatched"
  | "verifying"
  | "succeeded"
  | "failed"
  | "rolled_back"
  | "refused"
  | "lost"
  | "skipped"
  | "needs_manual_update";
