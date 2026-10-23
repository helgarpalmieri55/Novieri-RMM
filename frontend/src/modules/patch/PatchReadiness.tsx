// "Before updates can be approved": what another module decides about this
// page's approvals, shown before an approval is refused rather than after.
//
// THE OPERATOR ASKED FOR IT (2026-09-24, `docs/design/cross-module-guidance.md`
// §2 Patching, §6 #6). One row today: where a change gate covers patches,
// approving updates for the gated devices is refused with 409 until a change
// is approved (`patch/api.py`, `change_mgmt/gates.py`).
//
// Not here, on purpose: "computers run the agent" is already this page's own
// empty state (`PatchTabPanel`'s "Add devices"), and "device groups exist" is
// said where rings are chosen, on Patch policies. Maintenance windows and
// policies are not prerequisites: without one the safe defaults apply
// (`patch/policy.py`).
//
// NOTHING HERE GATES ANYTHING (invariant 6).

import { Prerequisites } from "../../components/prerequisites";
import { useT } from "../../i18n/context";
import { useChangeGatePrerequisite } from "../change/gatePrerequisite";

export function PatchReadiness() {
  const t = useT();
  const gateItem = useChangeGatePrerequisite("patches", {
    label: t("patch.readiness.gate"),
    ctaLabel: t("patch.readiness.gateCta"),
  });
  return (
    <Prerequisites
      title={t("patch.readiness.title")}
      subtitle={t("patch.readiness.subtitle")}
      items={[gateItem]}
    />
  );
}
