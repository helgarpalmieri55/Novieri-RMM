// Presentation logic for the Settings forms, kept out of the components so it
// can be tested as a function and reused identically by all six tabs.
//
// THERE IS NO `describeError` HERE ANY MORE (UX remediation cycle 4).
// This file used to carry the module's own translation of an API failure into
// a sentence — `Save failed (HTTP 422): …`, with an `extractDetail` that fell
// back to `JSON.stringify(detail)` and, failing that, to 400 characters of the
// raw response body. Those two fallbacks are the defect cycle 4 exists to
// close: a Pydantic payload or a proxy's HTML error page rendered verbatim
// into the UI. `components/form.tsx#describeSubmitError` is now the ONE
// translation in the product, and every call site in this module calls it
// directly rather than through a module-local alias, so a second opinion
// cannot quietly grow back here.
//
// WHAT WAS LOST AND WHY THAT IS RIGHT. The old sentence led with the HTTP
// status. `describeSubmitError` leads with what the operator can do about it
// ("You do not have permission to do that.", "That service is unavailable
// right now. Nothing was changed — try again shortly.") and names the FIELD on
// a validation failure, which the old `msg`-only join did not. The status code
// is diagnostic, not actionable, and one product speaking one way about
// failure was the point of the cycle.

import type { Attempt } from "../../components/form";
import { translate, type MessageKey } from "../../i18n/core";
import { ApiError } from "../../lib/api";

/** A 403 is "you may not", which is not the same screen as "it broke". */
export function isForbidden(error: unknown): boolean {
  return error instanceof ApiError && error.status === 403;
}

/**
 * A 404 on `GET /orgs/current` means the route is not deployed — the state the
 * task brief calls out explicitly, and the one an operator also sees if the
 * route is ever removed. It is neither a permission problem nor an outage, and
 * it must never be rendered as an empty-but-editable form.
 */
export function isMissingRoute(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404;
}

/** The sentence shown next to a control the operator cannot submit. */
export function permissionReason(code: string): string {
  return translate("settings.readOnly", { code });
}

/**
 * The read-only sentence for a write to configuration EVERY client shares
 * (phase 3 design §2), for a person who holds the code on some clients but
 * not on all: "you lack the permission" would be false for them.
 */
export function everyClientReason(code: string): string {
  return translate("settings.readOnlyEveryClient", { code });
}

/** Field-level dirtiness, so "Saved." disappears the moment an edit resumes. */
export function isDirty<T>(form: T, seed: T): boolean {
  return JSON.stringify(form) !== JSON.stringify(seed);
}

/**
 * `""` is a cleared field, not zero.
 *
 * `Number("")` is 0, so a blank box submitted through the obvious path sets a
 * retention window to zero rather than leaving it alone. Every numeric field
 * on this page goes through here.
 */
export function parseIntegerField(raw: string): number | null {
  const text = raw.trim();
  if (text === "") return null;
  const value = Number(text);
  return Number.isInteger(value) ? value : null;
}

/**
 * A refusal that is not a missing field ("there are no unsaved changes"), held
 * as DATA the way `Attempt` holds the rest: the sentence is the catalogue's,
 * looked up when the form is drawn, so a language switch re-words it. The
 * field name is only the carrier; it is never shown.
 */
export function refusal(name: string, key: MessageKey): Attempt {
  return { rules: () => [{ name, label: name, validate: () => translate(key) }], values: { [name]: "x" } };
}
