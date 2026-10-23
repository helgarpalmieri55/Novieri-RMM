// "Before a session can start": a screen session needs a connected agent on
// the other end, and the page used to say so only as a zero on a tile.
//
// THE OPERATOR ASKED FOR IT (2026-09-24, `docs/design/cross-module-guidance.md`
// §2 Remote sessions). The fleet-wide half lives here -- is ANY agent
// connected -- and sends an operator to add a computer when none runs the
// agent, or to the offline list when some do and none is connected. The
// per-device half (this device's agent is too old to share its screen) is
// said in the Start session dialog, beside the device it is about, from
// `Device.screen_capable`.
//
// Not here: "a remote policy allows unattended access". Unattended silently
// runs attended without one, but no console screen edits remote policies yet,
// so there is nowhere to send anyone.
//
// NOTHING HERE GATES ANYTHING (invariant 6).

import { Prerequisites } from "../../components/prerequisites";
import { useT } from "../../i18n/context";
import { useFleetPrerequisite } from "../../lib/detectors";

export function RemoteReadiness() {
  const t = useT();
  const onlineItem = useFleetPrerequisite("online", {
    severity: "crit",
    label: t("remote.readiness.online"),
  });
  return (
    <Prerequisites
      title={t("remote.readiness.title")}
      subtitle={t("remote.readiness.subtitle")}
      items={[onlineItem]}
    />
  );
}
