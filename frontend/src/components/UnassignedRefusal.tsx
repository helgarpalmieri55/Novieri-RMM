/**
 * WHAT A FORM SAYS IN PLACE OF ITS SAVE, while Unassigned is chosen in the top
 * bar (phase 3 final review B, Important 4; operator decision 2).
 *
 * The server files nothing new under the Unassigned client (a 422), and the
 * top bar's choice confines every request to it, so no Client field could
 * help either: a Save offered there could only fail. `useNewRowClientField`
 * reports this as `refused: "unassigned"`, and a form that would create a row
 * shows this sentence instead of its Save. A row with a parent (a device, a
 * group) is the parent's client's and is never refused; a row for every
 * client ("For every client") is not filed under Unassigned either, so a form
 * drops this once that is ticked.
 *
 * INVARIANT 6: THIS GATES NOTHING. The server refuses the row whatever the
 * console offers.
 */

import { useT } from "../i18n/context";
import { Icon } from "../shell/icons";
import { KIND_ICON } from "../shell/viz";

export function UnassignedRefusal({ className = "" }: { className?: string }) {
  const t = useT();
  return (
    <div role="status" className={`notice ${className}`.trim()}>
      <Icon name={KIND_ICON.info} size={17} />
      <div>{t("components.unassignedRefusal")}</div>
    </div>
  );
}
