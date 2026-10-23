/**
 * The operating system a person reads, from the two fields the wire carries.
 *
 * WHY THIS EXISTS. `os_family` is an enum -- `windows` / `linux` / `macos` --
 * and both fleet screens joined it to `os_version` and printed the result, so
 * every row of the estate read "macos 15.6", "windows Server 2022",
 * "linux Ubuntu 24.04". That is the API's vocabulary on the operator's screen:
 * the same defect `lib/labels.ts` was written to close, in the one column of
 * the fleet list nobody can avoid looking at, and on the sub-line of every
 * device page under it.
 *
 * SENTENCE CASE EXCEPT WHERE THE VENDOR OWNS THE SPELLING. "Windows" and
 * "Linux" are proper nouns; "macOS" is a trademark with a lowercase first
 * letter, which is why this is a table rather than a call to `humaniseEnum`
 * (that would produce "Macos"). `deploy/pickerOptions.ts` carries the same
 * three-row table for package platforms; the two are deliberately separate,
 * because that one labels an artefact's target and this one an observation
 * about a machine, and neither module owns the other's vocabulary.
 *
 * AN UNKNOWN FAMILY RENDERS AS ITSELF. A value this table does not have is a
 * fact worth seeing, not one worth hiding behind "Unknown" -- and a family the
 * agent starts reporting tomorrow must not disappear from the column until
 * somebody edits this file.
 */

const OS_FAMILY_LABELS: Record<string, string> = {
  windows: "Windows",
  linux: "Linux",
  macos: "macOS",
};

/** `macos` -> "macOS". An unrecognised family is returned unchanged. */
export function osFamilyLabel(family: string): string {
  return OS_FAMILY_LABELS[family.toLowerCase()] ?? family;
}

/**
 * The family and the version as one line: "Windows 11 24H2", "macOS 15.6",
 * "Linux Ubuntu 24.04".
 *
 * `null` when there is no family at all -- a switch is none of windows, linux
 * or macos (asset-inventory spec §1), and the callers render `NoValue` for it
 * rather than "undefined 11 23H2".
 */
export function osLabel(
  family: string | null | undefined,
  version: string | null | undefined,
): string | null {
  if (family == null || family === "") return null;
  return [osFamilyLabel(family), version ?? ""].join(" ").trim();
}
