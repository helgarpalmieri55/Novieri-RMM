// "Before a token can enroll a computer": a token only lets a machine JOIN; it
// installs nothing. The agent that redeems it comes from a published agent
// package (Agent packages), and with none published a token has nothing to be
// redeemed by (`docs/design/cross-module-guidance.md` §2, Enrollment).
//
// Rendered for holders of `enrollment.manage` only -- the people who issue
// tokens -- by the page. NOTHING HERE GATES ANYTHING (invariant 6).

import { Prerequisites } from "../../components/prerequisites";
import { useT } from "../../i18n/context";
import { useInstallerPrerequisite } from "../../lib/detectors";

export function EnrollmentPrerequisites() {
  const t = useT();
  const installer = useInstallerPrerequisite({
    severity: "crit",
    missingDetail: t("enrollment.prerequisites.missing"),
  });
  return (
    <Prerequisites
      title={t("enrollment.prerequisites.title")}
      subtitle={t("enrollment.prerequisites.subtitle")}
      items={[installer]}
    />
  );
}
