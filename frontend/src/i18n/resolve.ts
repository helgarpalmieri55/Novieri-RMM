import type { Language } from "./types";

function primary(tag: string | null | undefined): string {
  return (tag ?? "").toLowerCase().split("-")[0] ?? "";
}

/** Spec §1.1: the account's saved value, then this browser's last choice,
 *  then the browser's language, then English. A value the console does not
 *  support is skipped, not treated as a choice. */
export function resolveLanguage(saved: string | null | undefined, stored: string | null, browser: string | undefined): Language {
  for (const candidate of [saved, stored]) {
    const p = primary(candidate);
    // An unsupported value (a saved "fr", a stored "pt-BR") is not an answer:
    // fall through to the next source rather than stopping at English.
    if (p === "en" || p === "es") return p;
  }
  return primary(browser) === "es" ? "es" : "en";
}
