/**
 * What a change request can do next, and what it costs to do it.
 *
 * WHY THIS FILE EXISTS (UX remediation cycle 7). Auditor B, [BLOCKER],
 * /changes/:id: "A change request can be created but can never leave `draft` --
 * there is no approval, submit, schedule or close control anywhere in the UI...
 * An exhaustive enumeration of every interactive element on the page returns
 * exactly four controls: Export evidence, a select of action types, a text
 * input for an action id, and Link." The module whose stated purpose is
 * "change requests, APPROVALS, and scheduled windows" could not record a single
 * approval, so "Awaiting approval" was structurally always 0 and every record
 * accumulated in `draft` for ever.
 *
 * THE SERVER ALREADY HAD ALL OF IT. `POST /changes/{id}/submit`,
 * `/approve`, `/reject` and `/transition` have shipped since the module landed
 * (backend/app/modules/change_mgmt/api.py), and `lib/api.ts` has had the client
 * methods and `modules/change/api.ts` the mutation hooks the whole time. What
 * was missing was any control that called them from the record they act on.
 *
 * A TABLE, NOT A JSX TREE, because this vocabulary is the backend's and drift
 * from it is the Phase-5 regression: every row below mirrors exactly one branch
 * of `backend/app/modules/change_mgmt/state.py#next_state`, cited per row.
 * Offering a transition the server will refuse is a button that 400s, which is
 * the finding again wearing a nicer hat.
 *
 * WHICH ACTIONS ASK FIRST (cycle 8). Every transition here is written to the
 * immutable hash-chained audit log and none of them can be walked back, but
 * confirming all seven would train people to click through the dialog -- worse
 * than no confirmation. So the dialog is on the decisions somebody else has to
 * live with (`approve`, `reject` -- recorded against another person's request),
 * on the declaration that production work has started (`begin`), and on the two
 * that end the record's life (`complete`, `cancel`). `submit` and `revise` move
 * a request the caller already owns between states it can come back from, and
 * they are the two pressed most often.
 */

import { translate, type MessageKey } from "../../i18n/core";
import { lazyFields } from "../../i18n/lazy";
import { joinWords } from "../../lib/joinWords";
import { changeStateLabel } from "./changeState";
import type { TransitionAction } from "./types";

/**
 * `approve`/`reject` post a decision, `submit` has a route of its own, and the
 * remaining four are `TransitionRequest.action` values -- which is why `submit`
 * is named separately here rather than folded into `TransitionAction`: sending
 * `{"action": "submit"}` to `/transition` is a 422, even though `next_state`
 * itself accepts the word.
 */
export type ChangeActionKind = "approve" | "reject" | "submit" | TransitionAction;

export interface ChangeConfirmCopy {
  /** The action as a sentence, for `ConfirmDialog`'s title. */
  title: string;
  /** What follows from it. Consequences, not reassurance. */
  body: string;
  confirmLabel: string;
  /** Ends the record's life or destroys the request. Styling only. */
  destructive?: boolean;
}

export interface ChangeActionSpec {
  action: ChangeActionKind;
  /** The words on the button. */
  label: string;
  /** The permission the server checks (change_mgmt/api.py's dependencies). */
  permission: "change.request" | "change.approve";
  /** null = no dialog; see the header for why these five and not all seven. */
  confirm: ChangeConfirmCopy | null;
}

// The words are read from the catalogue WHEN A FIELD IS READ (lazyFields), so
// these module-level specs follow a language switch instead of freezing the
// language the page started in.
function confirmCopy(
  keys: { title: MessageKey; body: MessageKey; confirmLabel: MessageKey },
  destructive?: boolean,
): ChangeConfirmCopy {
  return lazyFields(destructive ? { destructive: true } : {}, keys);
}

function spec(
  action: ChangeActionKind,
  label: MessageKey,
  permission: ChangeActionSpec["permission"],
  confirm: ChangeConfirmCopy | null,
): ChangeActionSpec {
  return lazyFields({ action, permission, confirm }, { label });
}

const SUBMIT = spec("submit", "change.actions.submit", "change.request", null);

const APPROVE = spec(
  "approve",
  "change.actions.approve",
  "change.approve",
  confirmCopy({
    title: "change.actions.approveTitle",
    body: "change.actions.approveBody",
    confirmLabel: "change.actions.approveConfirm",
  }),
);

const REJECT = spec(
  "reject",
  "change.actions.reject",
  "change.approve",
  confirmCopy({
    title: "change.actions.rejectTitle",
    body: "change.actions.rejectBody",
    confirmLabel: "change.actions.rejectConfirm",
  }),
);

// The retrospective pair. Same routes, same permission, different meaning: an
// emergency change is decided AFTER it has run, and state.py deliberately
// leaves `state` at "implementing" for these -- so the label must not promise
// a state change that will not happen.
const APPROVE_RETROSPECTIVE = spec(
  "approve",
  "change.actions.approveRetro",
  "change.approve",
  confirmCopy({
    title: "change.actions.approveRetroTitle",
    body: "change.actions.approveRetroBody",
    confirmLabel: "change.actions.approveRetroConfirm",
  }),
);

const REJECT_RETROSPECTIVE = spec(
  "reject",
  "change.actions.rejectRetro",
  "change.approve",
  confirmCopy({
    title: "change.actions.rejectRetroTitle",
    body: "change.actions.rejectRetroBody",
    confirmLabel: "change.actions.rejectRetroConfirm",
  }),
);

const BEGIN = spec(
  "begin",
  "change.actions.begin",
  "change.request",
  confirmCopy({
    title: "change.actions.beginTitle",
    body: "change.actions.beginBody",
    confirmLabel: "change.actions.beginConfirm",
  }),
);

const COMPLETE = spec(
  "complete",
  "change.actions.complete",
  "change.request",
  confirmCopy({
    title: "change.actions.completeTitle",
    body: "change.actions.completeBody",
    confirmLabel: "change.actions.completeConfirm",
  }),
);

const CANCEL = spec(
  "cancel",
  "change.actions.cancel",
  "change.request",
  confirmCopy(
    {
      title: "change.actions.cancelTitle",
      body: "change.actions.cancelBody",
      confirmLabel: "change.actions.cancelConfirm",
    },
    true,
  ),
);

const REVISE = spec("revise", "change.actions.revise", "change.request", null);

const EMERGENCY = "emergency";

/**
 * The transitions `state.py#next_state` will accept for this change, in the
 * order an operator meets them.
 *
 * Every branch is cited. `type_` values outside {standard, normal, emergency}
 * reach `next_state`'s final `raise`, so they legitimately offer nothing but
 * `cancel`/`revise` -- the two entries in `_SIMPLE_TRANSITIONS` that do not
 * consult the type at all.
 */
export function availableChangeActions(state: string, type: string): ChangeActionSpec[] {
  const emergency = type === EMERGENCY;

  switch (state) {
    // _SIMPLE_TRANSITIONS: ("draft","submit") and ("draft","cancel").
    case "draft":
      return [SUBMIT, CANCEL];

    // ("submitted","cancel") always; approve/reject only for standard/normal
    // (`type_ in _STANDARD_TYPES and current == "submitted"`); `begin` from
    // "submitted" only for emergency (the fast path with no prior approval).
    case "submitted":
      return emergency ? [BEGIN, CANCEL] : [APPROVE, REJECT, CANCEL];

    // `begin` from "approved" for standard/normal; ("approved","cancel").
    case "approved":
      return [BEGIN, CANCEL];

    // `complete` requires current == "implementing". For an emergency change
    // the retrospective decision is legal here and only here, and completion
    // is refused until one is recorded.
    case "implementing":
      return emergency
        ? [APPROVE_RETROSPECTIVE, REJECT_RETROSPECTIVE, COMPLETE]
        : [COMPLETE];

    // ("rejected","revise") -> draft.
    case "rejected":
      return [REVISE];

    // Terminal. next_state has no branch that leaves either.
    case "completed":
    case "cancelled":
    default:
      return [];
  }
}

/**
 * Why the header is empty, in the user's words.
 *
 * A page that offers no transition and does not say why is the finding this
 * cycle closes, one state further along: the auditor's complaint was not only
 * that the buttons were missing but that nothing on the page accounted for
 * their absence.
 */
export function noActionsReason(state: string): string {
  if (state === "completed") return translate("change.actions.noneCompleted");
  if (state === "cancelled") return translate("change.actions.noneCancelled");
  return translate("change.actions.noneOther", { state: changeStateLabel(state) });
}

const PERMISSION_WORDS: Readonly<Record<string, MessageKey>> = {
  "change.approve": "change.actions.permissionApprove",
  "change.request": "change.actions.permissionRequest",
};

/**
 * Why a control the state allows is not on screen.
 *
 * The permissions are separate on purpose (`change.request` raises and drives,
 * `change.approve` decides), so the common case is a real operator seeing a
 * submitted change with nothing to press. "There is nothing to do here" and
 * "you may not do it" must not look the same.
 */
export function hiddenActionsReason(hidden: readonly ChangeActionSpec[]): string | null {
  const permissions = [...new Set(hidden.map((spec) => spec.permission))].sort();
  if (permissions.length === 0) return null;
  return translate("change.actions.hidden", {
    count: hidden.length,
    labels: hidden.map((spec) => spec.label).join(", "),
    permissions: joinWords(
      permissions.map((code) => translate(PERMISSION_WORDS[code] as MessageKey)),
      "or",
    ),
  });
}
