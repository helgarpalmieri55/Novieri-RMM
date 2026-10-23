// Report copy, as a person reads it.
//
// TWO AUDIT FINDINGS, ONE CAUSE. The catalogue and the report page print the
// server's `key`, `summary` and `description` straight through, and those
// strings are written in a Python source file:
//
//   [HIGH]  "Report cards in the catalogue have no titles -- the link text is a
//           200-word paragraph ... The actual report names exist only in the
//           URLs."
//   [LOW]   "Unrendered markdown backticks leak into report descriptions" --
//           `meta.generated_at` renders WITH the backticks in one description
//           and without them in the parallel sentence of another.
//   [LOW]   "Descriptions use `--` where an em dash is intended", six times on
//           the catalogue alone.
//
// WHY IT IS FIXED HERE AND NOT IN THE DEFINITIONS. The strings are also the
// OpenAPI `summary`/`description` of every report route and the header of every
// CSV export; a backtick is correct markup in the first and wrong glyph in the
// browser. Normalising at the point of render leaves the wire text alone --
// which is the same reason this module never rewrites the server's numbers.
//
// NOT A MARKDOWN RENDERER. The audit's fix line offers "render these through
// the markdown pipeline (or strip the backticks)"; a renderer here would take
// arbitrary server prose and turn it into markup, which is a much larger door
// than the one that needs closing. These two substitutions are the whole of it.

import { translate } from "../../i18n/core";
import type { MessageKey } from "../../i18n/core";
import { humaniseEnum, humaniseKey } from "../../lib/labels";
import { permissionsSentence } from "../../lib/permissions";
import { ticketPriorityLabel } from "../service-desk/serviceDeskState";
import type {
  ReportAgentStatus,
  ReportConsistency,
  ReportCoverage,
  ReportOffsite,
  ReportPointState,
  ReportScanState,
  ReportSource,
} from "./types";

/**
 * A report's human name, from the key the URL already carries:
 * `sla-attainment` -> "SLA attainment", `backup-coverage` -> "Backup
 * coverage".
 *
 * DERIVED, NOT A SECOND TABLE. A map from key to display name written here
 * would be a list the server does not have, and a report added to the registry
 * would render under its raw key until somebody remembered this file.
 * `humaniseEnum` already knows `sla` is an initialism.
 */
export function reportName(key: string): string {
  // A key the console has not met reads as `humaniseEnum` always derived it.
  return humaniseEnum(key, "ReportKey");
}

/**
 * A wire field (a row key or a totals key) as a column header or a tile label:
 * the catalogue first (`reporting.fields.<name>`), so the header follows the
 * language, and the derivation (`humaniseKey`) for a field this console has not
 * met. The CSV export keeps the server's own header row; this is the screen's.
 */
export function fieldLabel(name: string): string {
  const key = `reporting.fields.${name}` as MessageKey;
  const label = translate(key);
  return label !== key ? label : humaniseKey(name);
}

const SCAN_STATES: readonly ReportScanState[] = ["fresh", "stale", "never"];
const AGENT_STATUSES: readonly ReportAgentStatus[] = ["online", "offline", "never_enrolled"];
const POINT_STATES: readonly ReportPointState[] = ["complete", "failed", "pruned"];
const CONSISTENCIES: readonly ReportConsistency[] = ["snapshot", "live", "application", "partial"];
const OFFSITES: readonly ReportOffsite[] = ["off", "current", "behind"];
const COVERAGES: readonly ReportCoverage[] = ["covered", "stale", "failing", "unprotected"];
const PRIORITIES: readonly string[] = ["low", "normal", "high", "urgent"];

/**
 * The cell of a column whose values this console has a vocabulary for, or
 * null when the column is not one of them or the value is one it has not met
 * (the caller's own rule then reads it, never the raw wire word in Spanish).
 */
export function vocabularyCell(column: string, value: string): string | null {
  switch (column) {
    case "scan_state":
      return (SCAN_STATES as readonly string[]).includes(value) ? humaniseEnum(value, "ReportScanState") : null;
    case "agent_status":
      return (AGENT_STATUSES as readonly string[]).includes(value) ? humaniseEnum(value, "ReportAgentStatus") : null;
    case "last_point_state":
      return (POINT_STATES as readonly string[]).includes(value) ? humaniseEnum(value, "ReportPointState") : null;
    case "consistency_level":
      return (CONSISTENCIES as readonly string[]).includes(value) ? humaniseEnum(value, "ReportConsistency") : null;
    case "offsite":
      return (OFFSITES as readonly string[]).includes(value) ? humaniseEnum(value, "ReportOffsite") : null;
    case "coverage":
      return (COVERAGES as readonly string[]).includes(value) ? humaniseEnum(value, "ReportCoverage") : null;
    case "priority":
      return PRIORITIES.includes(value) ? ticketPriorityLabel(value) : null;
    default:
      return null;
  }
}

/**
 * Server prose as typography: backticks stripped, ` -- ` promoted to an em
 * dash. Anything else is left exactly as written.
 */
export function reportProse(text: string): string {
  return text.replace(/`([^`\n]+)`/g, "$1").replace(/ -- /g, " — ");
}

/**
 * A provenance line, as a person reads it.
 *
 * WHAT IT REPLACES: `${module} (${permission}): ${recency}` — "patch
 * (patch.read): as of each device's last scan". The module was the wire enum,
 * the permission was an RBAC code in brackets, and the only part written for a
 * reader was the clause after the colon. The sentence now leads with the
 * source as a label a person recognises ("Patch", "Devices", "SLA" — the
 * initialism table already knows the last one), keeps the server's recency
 * clause verbatim because it is the whole point of the note, and names the
 * permission in a sentence of its own.
 *
 * THE CODE STAYS. It is not decoration: it is what an operator seeing a
 * refusal, or an auditor asking why a number is visible to this account, has
 * to quote. The catalogue already names codes for the same reason ("Missing
 * permissions: sla.read"). What changes is that it is introduced rather than
 * dropped in brackets between two enum strings.
 */
export function sourceLine(source: ReportSource): string {
  return translate("reporting.source.line", {
    module: humaniseEnum(source.module, "ReportSourceModule"),
    // The server's own clause, as sent.
    recency: source.recency,
    permissions: permissionsSentence([source.permission]),
  });
}
