// Confirm or decline one previewed action (§6.3, §6.4, §8.2).
//
// **THE CONFIRM SENDS THE DIGEST IT WAS SHOWN, AND NOTHING ELSE.** There is no
// job type, no parameter and no target in this component's state, so there is
// nothing for a well-meaning future edit to add to the request body. The server
// recomputes the digest from the stored row and a mismatch is a 409.
//
// THE THREE FAILURES ARE DISTINGUISHED, BECAUSE THEY ARE DIFFERENT SITUATIONS:
//   409 — the world moved. Offer "show me the current preview", never a retry:
//         the digest in hand is stale by definition.
//   410 — the preview's clock ran out. Ask again; the action is gone.
//   403 — this person may not confirm THIS job type. §9.2's second part, and the
//         message names the permission their own module's route requires.
//
// **THE DECISION PAIR IS IN THE PAGE HEAD, WHICH IS WHERE v3 DRAWS IT.** The
// previous version of this comment claimed the actions row under the two
// regions was "the way v3 draws the pair on this screen", and the design file
// says otherwise:
//
//   P['assistant/actions/:actionId'] = () => head(..., [btn('Reject','x'),
//                                                       btn('Approve','check',true)], ...)
//
// It is a `head()` actions slot, and the difference is not decoration: under
// the regions the pair landed after the rationale panel, roughly 1,100px down
// at 390px, so the two controls the whole screen exists for were below the
// fold on a handset. This component therefore owns the head for the loaded
// action -- title, sentence and actions are one decision surface, and splitting
// them across two files is how they drifted apart.
//
// THE DECLINE REASON IS A SECOND STEP, NOT A THIRD CONTROL IN THE HEAD. It
// was a labelled `<select>` sitting between the two buttons, and putting it
// there cost the fix above most of what it bought: the head block measures
// ~559px and label-plus-select-plus-two-buttons ~561px, so `.ph-row`'s
// flex-wrap dropped the whole cluster onto a row of its own beneath the
// description — a bare caption and a dropdown on the page background, in no
// container, attached to neither the head above nor the panel below, while
// every sibling detail route in this console (/devices/dev-1, /changes/chg-1)
// keeps its head actions on the h1 line. The design file draws exactly two:
//
//   P['assistant/actions/:actionId'] = () => head(..., [btn('Reject','x'),
//                                                       btn('Approve','check',true)])
//
// So the head carries two, and "why" is asked once Decline is pressed, in a
// panel of its own directly under the head. The reason is still required, still
// visible, still labelled, and now unambiguously belongs to the decline it
// precedes rather than floating between two buttons only one of which reads it.
// It is `Chips` rather than a `<select>` for the same reason the autonomy dial
// is: six values a person picks from, all of them worth seeing at once, and no
// browser widget painting through the kit's box.

import { useState, type ReactElement } from "react";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import { ApiError } from "../../lib/api";
import { describeSubmitError } from "../../components/form";
import { Btn, KIND_ICON, PageHead, Panel } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { humaniseEnum, humaniseStatus } from "../../lib/labels";
import { Chips } from "../../shell/viz";
import { useConfirmAiAction, useRejectAiAction } from "./api";
import { ActionPreview } from "./ActionPreview";
import {
  AI_REJECTION_REASONS,
  rejectionReasonLabel,
  type AiActionPreviewOut,
  type AiActionState,
  type AiRejectionReason,
} from "./types";

/**
 * THE THREE SITUATIONS ABOVE, ON TOP OF THE SHARED SENTENCE.
 *
 * This function used to be a local `describeError` copy built out of
 * `error.message`, and that is the exact shape cycle 4 exists to remove.
 * `ApiError` is constructed as `new ApiError(status, await response.text())`,
 * so a bodyless refusal — which a 409 from behind a proxy, or any 503, very
 * often is — carried `message === ""`. The 409 branch then rendered
 * " — nothing was run. Reload the preview and look again.", a sentence
 * beginning with a dash and naming no cause, and the fall-through branch
 * returned `""`, which is falsy: `failure !== ""` was false, the paragraph
 * never rendered, and A CONFIRM THAT DID NOT RUN LOOKED LIKE ONE THAT DID.
 *
 * So the base sentence now comes from `describeSubmitError`, which is driven
 * off `status` first and the body second and therefore always exists. What
 * stays local is only what is TRUE HERE AND NOWHERE ELSE: that nothing ran,
 * that a stale digest must not be retried, and that 410 means this particular
 * preview's clock ran out — a status the shared function has no opinion about.
 */
export function explainFailure(error: unknown): string {
  if (error == null) return "";
  const status = error instanceof ApiError ? error.status : null;
  // 410 FIRST AND WITHOUT THE SHARED SENTENCE: `describeSubmitError` has no
  // 410 case, so deferring to it here would print the honest-but-useless
  // fallback instead of the one fact that matters — the preview is gone and
  // the assistant has to be asked again.
  if (status === 410) {
    return translate("aiAutomation.confirm.expired");
  }
  const sentence = describeSubmitError(error);
  if (status === 409) {
    return translate("aiAutomation.confirm.stale", { sentence });
  }
  if (status === 403) return translate("aiAutomation.confirm.refused", { sentence });
  return sentence;
}

const ACTION_STATES: ReadonlySet<string> = new Set<AiActionState>([
  "previewed",
  "confirmed",
  "dispatched",
  "rejected",
  "expired",
]);

/** A proposal's state in lower case, for the middle of a sentence. */
function actionStateWord(state: string): string {
  const label = ACTION_STATES.has(state) ? humaniseEnum(state, "AiActionState") : humaniseStatus(state);
  return label.toLocaleLowerCase();
}

export function ConfirmDialog({
  preview,
  canConfirm,
  onReload,
}: {
  preview: AiActionPreviewOut;
  canConfirm: boolean;
  onReload: () => void;
}): ReactElement {
  const t = useT();
  const confirm = useConfirmAiAction();
  const reject = useRejectAiAction();
  const [reason, setReason] = useState<AiRejectionReason>("not_needed");
  /** Whether the operator has pressed Decline and is being asked why. */
  const [declining, setDeclining] = useState(false);
  const failure = explainFailure(confirm.error ?? reject.error ?? null);

  const pending = preview.state === "previewed";

  const decision = pending ? (
    <>
      <Btn
        label={t("aiAutomation.confirm.decline")}
        icon="close"
        disabled={reject.isPending}
        onClick={() => setDeclining(true)}
      />
      <Btn
        label={t("aiAutomation.confirm.confirmAndRun")}
        icon="check"
        variant="primary"
        disabled={!canConfirm || confirm.isPending}
        onClick={() =>
          confirm.mutate({
            id: preview.ai_action_id,
            // THE DIGEST THE HUMAN WAS SHOWN, unmodified.
            digest: preview.preview_digest,
          })
        }
      />
    </>
  ) : undefined;

  return (
    <>
      <PageHead
        title={preview.name}
        description={t("aiAutomation.confirm.prepared", { host: preview.target_hostname })}
        actions={decision}
      />

      {/* Rendered but not hidden when the caller lacks `ai.act.confirm`: a
          missing explanation reads as "there is nothing to do here", and the
          person looking at the screen is often exactly the person who must
          fetch somebody who can. */}
      {/* ASKED IMMEDIATELY UNDER THE HEAD, so the answer to "why" is the next
          thing on the page after the button that asked it — and inside a panel,
          which is what a group of controls on this canvas lives in. */}
      {pending && declining && (
        <Panel
          title={t("aiAutomation.confirm.declineTitle")}
          subtitle={t("aiAutomation.confirm.declineSubtitle")}
        >
          <div className="field" style={{ marginBottom: 12 }}>
            <span>{t("aiAutomation.confirm.declineReason")}</span>
            <Chips
              items={AI_REJECTION_REASONS.map(rejectionReasonLabel)}
              active={AI_REJECTION_REASONS.indexOf(reason)}
              onSelect={(at) => {
                const next = AI_REJECTION_REASONS[at];
                if (next !== undefined) setReason(next);
              }}
              label={t("aiAutomation.confirm.declineReason")}
            />
          </div>
          <div className="form-actions" style={{ margin: 0 }}>
            <Btn
              label={reject.isPending ? t("aiAutomation.confirm.declining") : t("aiAutomation.confirm.sendDecline")}
              icon="close"
              disabled={reject.isPending}
              onClick={() => reject.mutate({ id: preview.ai_action_id, reason })}
            />
            <Btn label={t("aiAutomation.confirm.keepWaiting")} onClick={() => setDeclining(false)} />
          </div>
        </Panel>
      )}

      {/* SHOWN TO WHOEVER CAN CONFIRM, NOT ONLY TO WHOEVER CANNOT. The
          actions page used to carry "nothing here has run by itself: a
          proposal dispatches through the signed-job engine only once a person
          holding the job type's own permission confirms it" in its page
          description, where it was read by someone browsing a list. It is the
          same fact, and this is the moment it governs. */}
      {pending && (
        <p className="form-hint" style={{ marginBottom: 16 }}>
          {t("aiAutomation.confirm.hint")}
        </p>
      )}

      {!pending && (
        <p className="note" style={{ marginBottom: 16 }}>
          {t("aiAutomation.confirm.settled", { state: actionStateWord(preview.state) })}
        </p>
      )}

      {failure !== "" && (
        <div role="alert" data-testid="ai-action-error" className="notice red" style={{ marginBottom: 16 }}>
          <Icon name={KIND_ICON.crit} size={16} />
          <span className="flex flex-wrap items-center gap-2">
            <span>{failure}</span>
            {confirm.error?.status === 409 && (
              <Btn label={t("aiAutomation.confirm.showCurrent")} small onClick={onReload} />
            )}
          </span>
        </div>
      )}

      <ActionPreview preview={preview} />
    </>
  );
}
