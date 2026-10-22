/**
 * The model provider, as the console STATES it — not as it picks it.
 *
 * WHAT THIS FILE REPLACED, AND WHY (spec §0.14, the operator's decision of
 * 2026-08-25: "dont allow anyone to choose an AI model, we will use the same
 * for every tenant and api key will be set in the .env per tenant").
 *
 * It was `providerOptions.ts`, and it built `PickerOption[]` for a dropdown on
 * two screens. There is no dropdown any more: `ITS_AI_PROVIDERS` names exactly
 * one provider for the whole deployment, `PUT /ai/settings` has no
 * `provider_key` field, and `ai_settings` has no column to put one in.
 *
 * THE ROUTE THAT FED THE PICKER IS STILL READ, and that is deliberate rather
 * than leftover. `GET /ai/providers` answers key, kind and model — never a base
 * URL, never key material — and an administrator looking at the egress
 * disclosure is entitled to know WHERE the data listed there goes. Displaying
 * it is disclosure; offering it would be a choice, and the choice is the thing
 * that was removed.
 *
 * THE OLD UX FINDING STILL APPLIES TO THE EMPTY CASE. Auditor A, [HIGH],
 * /assistant/settings → "Model provider": a control that looks answerable and
 * is not, with "no link to add a provider, no instructions, and no
 * explanation". The fix then and now is the same and is still unusual: there is
 * no console screen to link to and there must never be one, so the honest hint
 * NAMES THE ENVIRONMENT VARIABLES and says the console cannot do it.
 */

import { translate } from "../../i18n/core";
import type { AiProviderOut } from "./types";

/**
 * WHAT WOULD FILL THE GAP, for a deployment that has configured nothing.
 *
 * LANGUAGE PASS, 2026-09-11 (`docs/design/settings-language.md`, "Second
 * surfaces carrying the old words"): this used to be one sentence,
 * `PROVIDER_UNCONFIGURED_HINT`, addressed to the wrong reader -- an org admin
 * on `/assistant/settings` cannot set an environment variable, and the old
 * copy told them to. The fix splits that fact in two: what changed for THIS
 * reader (one sentence, always visible), and what the person who actually can
 * act needs (the variable names, verbatim, behind a disclosure).
 *
 * THESE THREE FUNCTIONS ARE THE ONLY COPY. `Settings → Assistant`'s
 * `AssistantTab.tsx` and `/assistant/settings`'s `AiSettingsPage.tsx` both
 * import them from here rather than each defining their own — the two pages
 * shipped in separate commits (`557d54a`, `262cd76`) with text-identical
 * local copies of exactly this split, which is two copies waiting to
 * disagree the first time one is edited, not two. `provider.ts` already held
 * `describeProvider`, the other thing both pages need for this same panel, so
 * this is where a shared string for these two modules belongs; nothing
 * re-exports these three so a third copy cannot quietly reappear.
 *
 * `configured_providers()` reads the process environment once, so an operator
 * who edits `.env` and waits sees nothing change until the restart
 * `SERVER_ADMIN_DETAIL` names.
 */
export const providerOffSummary = (): string => translate("aiAdmin.provider.offSummary");
/** The disclosure's own summary text -- verbatim, so it reads identically wherever it opens. */
export const serverAdminHeading = (): string => translate("aiAdmin.provider.adminHeading");
/** The variable names, kept EXACTLY as `deploy/.env.example` spells them -- nothing renamed. */
export const serverAdminDetail = (): string => translate("aiAdmin.provider.adminDetail");

// The wording at load time, kept because the module's unit tests import the
// constants. Components call the functions above, which follow the language.
export const PROVIDER_OFF_SUMMARY = providerOffSummary();
export const SERVER_ADMIN_HEADING = serverAdminHeading();
export const SERVER_ADMIN_DETAIL = serverAdminDetail();

/**
 * The configured provider as one line, or `null` when there is none.
 *
 * `kind · model` beside the key, the same shape the picker's rows used: the key
 * alone is an operator's private naming ("local", "hosted", "primary"), and
 * what tells an administrator where their data goes is the kind and the model.
 * Key material is not a field of `AiProviderOut` and is not read here.
 *
 * **THE KIND IS WRITTEN THE WAY THE DESIGN WRITES IT, NOT THE WAY THE WIRE
 * SPELLS IT.** The line read "local openai_compatible · gpt-oss-120b" — a raw
 * enum with an underscore, and two facts run together with no separator —
 * where v3's own `P['assistant/autonomy']` writes "Local · openai-compatible ·
 * gpt-oss-120b". So the key is sentence-cased, the kind's underscores become
 * the hyphens the vendor's own name uses, and the three parts are separated.
 * NOTHING IS TRANSLATED: `openai_compatible` and `openai-compatible` are the
 * same string re-punctuated, and the model id is untouched, so what is on
 * screen is still what the deployment configured.
 *
 * TAKES THE LIST AND RETURNS THE FIRST, rather than assuming a scalar: the
 * route is a published contract and still answers a list. The server refuses a
 * second provider at startup, so a list of two cannot reach a running console —
 * and if one ever did, showing the one the server would resolve is better than
 * a crash.
 */
export function describeProvider(
  providers: readonly AiProviderOut[] | undefined,
): { key: string; detail: string } | null {
  const provider = (providers ?? [])[0];
  if (provider === undefined) return null;
  const key = provider.key === "" ? provider.key : provider.key.charAt(0).toUpperCase() + provider.key.slice(1);
  return { key, detail: `${provider.kind.replace(/_/g, "-")} · ${provider.model}` };
}
