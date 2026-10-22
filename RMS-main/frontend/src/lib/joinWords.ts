/**
 * "a, b and c" -- a list as a phrase, in the active language.
 *
 * Lives apart from `detectors.ts` so the shell can use it without importing
 * the prerequisite machinery (which imports the shell).
 */

import { getLanguage, translate } from "../i18n/core";

export type Conjunction = "and" | "or";

/**
 * The word that joins the last two items. Spanish bends it for the sound that
 * follows: "y" becomes "e" before an i-sound ("IPv6", "Intel") and "o" becomes
 * "u" before an o-sound -- "dispositivos e IPs", "uno u otro". English never
 * changes.
 */
export function conjunctionBefore(next: string, kind: Conjunction = "and"): string {
  const word = translate(kind === "and" ? "lib.list.and" : "lib.list.or");
  if (getLanguage() !== "es") return word;
  const head = next.trim().toLowerCase();
  if (kind === "and" && /^h?i(?![aeou])/.test(head)) return "e";
  if (kind === "or" && /^h?o/.test(head)) return "u";
  return word;
}

export function joinWords(words: readonly string[], conjunction: Conjunction = "and"): string {
  if (words.length <= 1) return words[0] ?? "";
  const last = words[words.length - 1] as string;
  return `${words.slice(0, -1).join(", ")} ${conjunctionBefore(last, conjunction)} ${last}`;
}
