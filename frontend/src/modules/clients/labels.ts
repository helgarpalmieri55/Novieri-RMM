/**
 * The Clients page's vocabulary (MSP clients phase 3, design §4.1), worded
 * when read so a language switch shows on the next render.
 */
import { translate } from "../../i18n/core";
import type { Kind } from "../../shell/components";
import type { AssignmentOut } from "../health/types";

/** Held equal to the server's `OptionalModule` (`backend/app/api/v1/clients.py`). */
export const OPTIONAL_MODULES = ["budget", "bcp", "change"] as const;
export type OptionalModule = (typeof OPTIONAL_MODULES)[number];

export function clientKindLabel(kind: string): string {
  switch (kind) {
    case "customer":
      return translate("clients.kind.customer");
    case "internal":
      return translate("clients.kind.internal");
    case "unassigned":
      return translate("clients.kind.unassigned");
    default:
      return kind;
  }
}

export function clientStatusLabel(status: string): string {
  switch (status) {
    case "active":
      return translate("clients.status.active");
    case "archived":
      return translate("clients.status.archived");
    default:
      return status;
  }
}

/** An archived client is history, not a fault: muted, never red. */
export function clientStatusKind(status: string): Kind {
  return status === "active" ? "ok" : "mut";
}

export function moduleLabel(module: OptionalModule): string {
  switch (module) {
    case "budget":
      return translate("clients.module.budget");
    case "bcp":
      return translate("clients.module.bcp");
    case "change":
      return translate("clients.module.change");
  }
}

export function moduleHint(module: OptionalModule): string {
  switch (module) {
    case "budget":
      return translate("clients.module.budgetHint");
    case "bcp":
      return translate("clients.module.bcpHint");
    case "change":
      return translate("clients.module.changeHint");
  }
}

/** One email domain per line (or comma), trimmed, blanks dropped. The server validates. */
export function parseDomains(text: string): string[] {
  return text
    .split(/\r?\n|,/)
    .map((item) => item.trim())
    .filter((item) => item !== "");
}

/**
 * The health profile a client's devices fall back to (design §5.1): its own
 * client default, and the organisation default that applies when there is
 * none. The resolution itself is the server's (`health/resolution.py`).
 */
export function clientHealthDefault(
  assignments: readonly AssignmentOut[],
  clientId: string,
): { own: AssignmentOut | null; org: AssignmentOut | null } {
  return {
    own: assignments.find((a) => a.level === "client" && a.client_id === clientId) ?? null,
    org: assignments.find((a) => a.level === "org") ?? null,
  };
}
