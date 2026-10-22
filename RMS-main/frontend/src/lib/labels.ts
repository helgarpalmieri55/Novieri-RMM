/**
 * Wire text as human text.
 *
 * WHAT THIS CLOSES. The audits filed the same defect in seven modules under
 * seven headings: `not_applicable` as a badge label on the patch tab, raw
 * snake_case credential types, `refused_loop` in a disposition filter,
 * lowercase priorities shown to end users in the portal, a delivery failure
 * reported as a bare enum code, "enum values shown raw and lowercase
 * throughout the module". Each was written up as a module bug. It is one bug:
 * the API's identifiers reach the screen because nothing sits between them and
 * the screen.
 *
 * SENTENCE CASE, NOT TITLE CASE. "Not applicable", not "Not Applicable" --
 * these render inside badges and cells next to ordinary prose, and title case
 * in that position reads as a proper noun.
 */

import { translate, type MessageKey } from "../i18n/core";

/**
 * Fragments that are initialisms rather than words. Without this,
 * `sla_breach` reads "Sla breach", which is worse than leaving it raw: it
 * looks like a spelling mistake rather than an untranslated identifier.
 */
const INITIALISMS = new Map<string, string>([
  ["ai", "AI"],
  ["api", "API"],
  ["bcp", "BCP"],
  ["cpu", "CPU"],
  ["cve", "CVE"],
  ["dns", "DNS"],
  ["id", "ID"],
  ["ip", "IP"],
  ["ipam", "IPAM"],
  ["json", "JSON"],
  ["mac", "MAC"],
  ["mcp", "MCP"],
  ["mdm", "MDM"],
  ["mfa", "MFA"],
  ["ok", "OK"],
  ["os", "OS"],
  ["ram", "RAM"],
  ["rls", "RLS"],
  ["rmm", "RMM"],
  ["sla", "SLA"],
  ["smtp", "SMTP"],
  ["sso", "SSO"],
  ["ssh", "SSH"],
  ["tls", "TLS"],
  ["url", "URL"],
  ["uuid", "UUID"],
  ["vlan", "VLAN"],
  ["vm", "VM"],
  ["vrf", "VRF"],
]);

/** Split an identifier on every boundary an identifier can have. */
function words(identifier: string): string[] {
  return identifier
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .split(/[\s_\-.]+/)
    .filter(Boolean)
    .map((word) => word.toLowerCase());
}

/**
 * An enum value as a label: `not_applicable` -> "Not applicable",
 * `refused_loop` -> "Refused loop", `sla_breach` -> "SLA breach",
 * `ok` -> "OK", `inProgress` -> "In progress".
 *
 * With a `type` (the name of the union in lib/api.ts) the catalogue is asked
 * first, `enums.<type>.<value>`, so the label follows the language; a value
 * the catalogue does not know falls back to the derivation above. Pass it as
 * a string literal -- `humaniseEnum(s.state, "RemoteSessionState")` -- because
 * enums.coverage.test.ts finds the rendered types by scanning for that form.
 *
 * Returns the input unchanged when there is nothing to split, so a value that
 * is already prose ("Awaiting approval") survives a caller that applies this
 * defensively.
 */
export function humaniseEnum(value: string, type?: string): string {
  if (type) {
    // `translate` returns the key itself when nothing is catalogued, which is
    // how an absent entry (a value the console has not met yet) is told apart.
    const key = `enums.${type}.${value}` as MessageKey;
    const label = translate(key);
    if (label !== key) return label;
  }
  const parts = words(value);
  if (parts.length === 0) return value;
  return parts
    .map((word, at) => {
      const initialism = INITIALISMS.get(word);
      if (initialism) return initialism;
      // Sentence case: only the first word is capitalised.
      return at === 0 ? word.replace(/^./, (c) => c.toUpperCase()) : word;
    })
    .join(" ");
}

/**
 * A status word from the wire as a label, for the places that cannot know the
 * enum it belongs to (`StatusPill`, `Attributes`): the shared vocabulary
 * `enums.StatusWord.<word>` first, so "failed" reads "Fallido" in Spanish, and
 * the plain derivation for any word that is not in it. The word is matched
 * case-insensitively with `-` and spaces read as `_`. A module that knows its
 * enum passes the label itself (`humaniseEnum` with the type as its second
 * argument), which wins.
 */
export function humaniseStatus(value: string): string {
  const key = `enums.StatusWord.${value.trim().toLowerCase().replace(/[\s-]+/g, "_")}` as MessageKey;
  const label = translate(key);
  return label !== key ? label : humaniseEnum(value);
}

/**
 * A wire field or object key as a label: `size_bytes` -> "Size bytes",
 * `default_queue_id` -> "Default queue".
 *
 * The trailing `_id` goes because it names the storage rather than the thing
 * the user picked -- an operator chose a queue, not a queue's identifier.
 */
export function humaniseKey(name: string): string {
  const trimmed = name.replace(/_id$/, "");
  return humaniseEnum(trimmed || name);
}
