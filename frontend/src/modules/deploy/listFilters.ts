/**
 * What Packages and Scripts filter their loaded rows with (redesign plan,
 * O3 3.4 and 3.5), kept once for both pages (review fix round 1).
 */

/** The platforms of the System chips, in this order. */
export const OS_FILTERS = ["windows", "linux", "macos"] as const;

/** For the search: case and accents do not count, the console's rule. */
export function fold(text: string): string {
  return text.normalize("NFD").replace(/\p{M}/gu, "").toLowerCase().trim();
}
