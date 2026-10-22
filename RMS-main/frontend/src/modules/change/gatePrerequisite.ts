/**
 * "Where a change gate applies, an approved change exists": the prerequisite
 * Patching and Software share, owned here because gates and changes are this
 * module's.
 *
 * WHY (`docs/design/cross-module-guidance.md` §2 Patching #3, Software #4,
 * §6 #6). A gate on patches or deployments makes the server refuse an
 * approval or a deployment reaching the gated devices with 409 "… need an
 * approved change" (`change_mgmt/gates.py`). Nothing on those pages said a
 * gate existed, so the first an operator heard of it was the refusal.
 *
 *   no gate for this kind          done: nothing needs a change first;
 *   gated, and a change is         done: it names where, and that one can
 *   approved or implementing       be linked;
 *   gated, and none is             missing (crit): raise one first.
 *
 * Both reads are `change.read`; an account without it is told the check
 * cannot be made from here, never that nothing is gated.
 *
 * NOTHING HERE GATES ANYTHING (invariant 6). The server checks the gate on
 * every request and again at dispatch, whatever this says.
 */

import { useQuery } from "@tanstack/react-query";

import { combineQueries, prerequisiteFromQuery, type Prerequisite } from "../../components/prerequisites";
import { translate } from "../../i18n/core";
import { useT } from "../../i18n/context";
import type { ApiError } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { joinWords, mayCheck } from "../../lib/detectors";
import { useMePermissions } from "../../lib/mePermissions";
import { CHANGE_GATES_KEY } from "./api";
import type { ChangeGate, ChangeRequest } from "./types";

export const CHANGE_READ = "change.read";

/** The states in which a change authorizes gated work (`gates.AUTHORIZING_STATES`). */
export const AUTHORIZING_STATES: readonly string[] = ["approved", "implementing"];

export type GateKind = "patches" | "deployments";

function gatesFor(gates: readonly ChangeGate[], kind: GateKind): ChangeGate[] {
  return gates.filter((gate) => (kind === "patches" ? gate.applies_to_patches : gate.applies_to_deployments));
}

/** "every device", or the gated groups by name. */
export function gatedWhere(gates: readonly ChangeGate[]): string {
  if (gates.some((gate) => gate.device_group_id == null)) return translate("change.gate.everyDevice");
  const names = gates.map((gate) => gate.device_group_name ?? translate("change.gate.aDeviceGroup"));
  return translate("change.gate.devicesIn", { names: joinWords([...new Set(names)].sort()) });
}

export interface ChangeGateOptions {
  label: string;
  ctaLabel: string;
}

export function useChangeGatePrerequisite(kind: GateKind, options: ChangeGateOptions): Prerequisite {
  // Subscribes the caller to a language switch: the sentences below are worded
  // by `translate()` when the prerequisite is built, which happens every render.
  useT();
  const api = useApi();
  const me = useMePermissions();
  const allowed = mayCheck(me, CHANGE_READ);
  // The keys the Changes pages use, so a gate set or a change approved there
  // refreshes this row.
  const gates = useQuery<ChangeGate[], ApiError>({
    queryKey: [CHANGE_GATES_KEY],
    queryFn: () => api.listChangeGates(),
    enabled: allowed,
    retry: false,
  });
  const changes = useQuery<ChangeRequest[], ApiError>({
    queryKey: ["changes"],
    queryFn: () => api.listChanges(),
    enabled: allowed,
    retry: false,
  });

  return prerequisiteFromQuery<readonly [ChangeGate[], ChangeRequest[]]>({
    label: options.label,
    query: combineQueries(gates, changes),
    permission: { code: CHANGE_READ, me },
    isDone: ([all, list]) =>
      gatesFor(all, kind).length === 0 || list.some((change) => AUTHORIZING_STATES.includes(change.state)),
    done: ([all, list]) => {
      const gated = gatesFor(all, kind);
      if (gated.length === 0) {
        return translate(kind === "patches" ? "change.gate.noGatePatches" : "change.gate.noGateDeployments");
      }
      const ready = list.filter((change) => AUTHORIZING_STATES.includes(change.state)).length;
      return translate(kind === "patches" ? "change.gate.donePatches" : "change.gate.doneDeployments", {
        where: gatedWhere(gated),
        count: ready,
      });
    },
    missing: ([all]) => ({
      severity: "crit",
      detail: translate(kind === "patches" ? "change.gate.missingPatches" : "change.gate.missingDeployments", {
        where: gatedWhere(gatesFor(all, kind)),
      }),
      cta: { to: "/changes", label: options.ctaLabel },
    }),
  });
}
