// "Before a built machine can join": a machine built here enrolls when its
// answer file runs the agent with an enrollment token (`provisioning/render.py`:
// Windows' first-logon command is `agent.exe enroll --token ...`), so the agent
// has to be in the image, and it comes from a published agent package
// (`docs/design/cross-module-guidance.md` §2, Imaging).
//
// ADVISORY AND A PROXY, SAID PLAINLY. The platform cannot see inside an image:
// a published package is what makes it POSSIBLE to put the agent there, not
// proof that anyone did. The row says so rather than claiming more.
//
// Credentials are NOT here: the domain-join and local-admin passwords are
// sealed on the provisioning profile itself (doc §0.5).
//
// NOTHING HERE GATES ANYTHING (invariant 6).

import { Prerequisites } from "../../components/prerequisites";
import { useT } from "../../i18n/context";
import { useInstallerPrerequisite } from "../../lib/detectors";

export function ProvisioningPrerequisites() {
  const t = useT();
  const installer = useInstallerPrerequisite({
    label: t("provisioning.prerequisites.label"),
    missingDetail: t("provisioning.prerequisites.missing"),
    doneDetail: t("provisioning.prerequisites.done"),
    ctaLabel: t("provisioning.prerequisites.cta"),
  });
  return <Prerequisites title={t("provisioning.prerequisites.title")} items={[installer]} />;
}
