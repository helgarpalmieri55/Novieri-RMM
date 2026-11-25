/**
 * The change-management pages as one row of links (redesign Task 11, 6.2):
 * the list, the approval queue and the calendar. Before, the list's head
 * carried two buttons to the other two and they carried "All changes" back;
 * now the three are the kit's `SectionNav`, with the current page marked.
 *
 * Every page here is `change.read`, which the route itself requires, so the
 * strip has no permission of its own to check.
 */
import { useT } from "../../i18n/context";
import { SectionNav } from "../../shell/components";

export function ChangeSectionNav() {
  const t = useT();
  return (
    <SectionNav
      label={t("change.nav.label")}
      items={[
        { to: "/changes", label: t("change.nav.changes"), end: true },
        { to: "/changes/approvals", label: t("change.list.approvalQueue") },
        { to: "/changes/calendar", label: t("change.list.calendar") },
      ]}
    />
  );
}
