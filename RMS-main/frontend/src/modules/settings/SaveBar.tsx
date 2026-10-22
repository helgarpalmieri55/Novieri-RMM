// The save control every tab shares, so that "did this save?" reads the same
// on all of them.
//
// **AND THAT NOW INCLUDES ORGANIZATION, WHICH IS WHY THE PROPS BELOW GREW.**
// The Organization tab committed through `FormActions` -- a plain blue "Save"
// with no glyph -- while the eight sections under it committed through this
// bar: "Save changes" with the check glyph, which is also what v3 draws
// (`btn('Save','check',true)`). Moving down the section list changed the shape
// of the commit control, on one page, for no reason a reader could see. Rather
// than restyle a ninth control into looking like this one, that tab now USES
// this one, and the three things it needed that this bar could not do are
// props: submitting the surrounding `<form>` (so Enter still saves), rendering
// the form kernel's client-side `problems`, and staying enabled when the form
// is clean so a save with nothing to save can be REFUSED OUT LOUD instead of
// silently doing nothing.
//
// DRAWN AS THE FORM KERNEL'S ACTIONS ROW (`.form-actions`, `.form-hint`,
// `.form-error`, `.form-saved`), so a tab that saves through `SaveBar` and a
// tab that saves through `FormActions` are the same row to look at. The
// button is the design's primary `.btn` with the check glyph, the way v3 draws
// every Save (`btn('Save','check',true)`), and each outcome line carries its
// kind's glyph as well as its colour: red and green are never the only thing
// telling a refusal from a confirmation.
//
// `Btn` FROM THE SHELL, NOW THAT IT TAKES `disabled`. Rule 1 requires a
// control the operator cannot submit to be DISABLED with the reason shown
// rather than clickable and refused, and `Btn` forwards the native attribute.
//
// THE FAILURE SENTENCE COMES FROM THE KERNEL (cycle 4). `describeSubmitError`
// replaced this module's own `describeError`, whose last two fallbacks were
// `JSON.stringify(detail)` and 400 raw characters of the response body -- the
// exact "raw Pydantic payload rendered into the UI" the audit filed. It also
// fixes the case `describeError` could not see: an `ApiError` built from a
// bodyless 503 carries `message === ""`, and the kernel answers that with a
// sentence rather than a bare status.
//
// THERE IS NO OPTIMISTIC SUCCESS ANYWHERE ON THIS PAGE. `saved` is only ever
// `mutation.isSuccess` -- the server's 2xx -- and it is suppressed the moment
// the form is edited again, so a stale "Saved." can never sit above unsaved
// changes.

import { describeSubmitError } from "../../components/form";
import type { Problem } from "../../components/form";
import { useT } from "../../i18n/context";
import { Btn, KIND_ICON } from "../../shell/components";
import { Icon } from "../../shell/icons";

export function SaveBar({
  label,
  canEdit,
  disabledReason,
  dirty,
  requireDirty = true,
  pending,
  error,
  problems = [],
  saved,
  savedMessage,
  submit = false,
  onSave,
}: {
  label?: string;
  canEdit: boolean;
  /** Shown whenever `canEdit` is false. Rule 1: disabled, with the reason visible. */
  disabledReason?: string;
  dirty: boolean;
  /**
   * Whether a clean form disables the button.
   *
   * `true` everywhere it always was. `false` for a tab that would rather take
   * the click and SAY "there are no unsaved changes" than present a dead
   * control -- the same outcome (nothing is written, no audit entry for a
   * change that is not one), with the reason on screen.
   */
  requireDirty?: boolean;
  pending: boolean;
  error: unknown;
  /** The form kernel's client-side findings, shown when the server has not
   *  already refused: a refusal in hand beats a restatement of the checks it
   *  got past. */
  problems?: readonly Problem[];
  saved: boolean;
  savedMessage?: string;
  /**
   * Submit the surrounding `<form>` instead of calling `onSave`.
   *
   * A tab wrapped in the kernel's `Form` gets Enter-to-save from the browser,
   * and only a `type="submit"` control keeps it.
   */
  submit?: boolean;
  onSave?: () => void;
}) {
  const t = useT();
  const failure = error ? describeSubmitError(error) : null;
  return (
    <div className="form-actions">
      <Btn
        label={pending ? t("settings.saveBar.saving") : (label ?? t("settings.saveBar.save"))}
        icon="check"
        variant="primary"
        type={submit ? "submit" : "button"}
        disabled={!canEdit || (requireDirty && !dirty) || pending}
        onClick={submit ? undefined : onSave}
      />
      {!canEdit && disabledReason ? <span className="form-hint">{disabledReason}</span> : null}
      {failure ? (
        // `role="alert"` so the failure is announced, not merely coloured.
        <span role="alert" className="form-error">
          <Icon name={KIND_ICON.crit} size={13} />
          {failure}
        </span>
      ) : problems.length > 0 ? (
        <span role="alert" className="form-error">
          <Icon name={KIND_ICON.crit} size={13} />
          {problems.map((problem) => problem.message).join(". ")}.
        </span>
      ) : null}
      {saved && !failure ? (
        <span role="status" className="form-saved">
          <Icon name={KIND_ICON.ok} size={13} />
          {savedMessage ?? t("settings.saveBar.saved")}
        </span>
      ) : null}
    </div>
  );
}
