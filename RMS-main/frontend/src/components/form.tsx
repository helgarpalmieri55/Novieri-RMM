/**
 * The form kernel — UX remediation cycle 3.
 *
 * WHAT IT IS FOR. Two independent auditors walked this product and found the
 * same defect on route after route, counting it out loud as they went: "the
 * seventh occurrence", "the eighth occurrence", "same silent-no-op failure
 * repeats on every form in the module". Forty-six findings, two BLOCKERs.
 *
 * THE CAUSE IS ONE EXPRESSION. Every create and save in the product reads
 * `<Button disabled={mutation.isPending || !someField}>` — measured at more
 * than twenty sites. The product contains six `<form>` elements in total, and
 * `components/ui.tsx`'s `Button` sets no `type`, so a "submit" button is an
 * orphan control and Enter does nothing anywhere.
 *
 * **A disabled button that never says why IS the silent no-op.** The user
 * clicks, nothing moves, and the interface offers no theory about which field
 * is at fault. So the rule here is not a nicer disabled state — it is refusing
 * to disable at all for missing input, and explaining on submit instead.
 * Disabled is reserved for "you may not do this" (no permission) and "not
 * right now" (a request in flight), both of which say so beside the control.
 *
 * NOT INVENTED. Two implementations already in this tree do it correctly, and
 * this generalises them rather than adding a third opinion:
 * `modules/settings/SaveBar.tsx` — disabled with a visible reason,
 * `role="alert"` on failure, `role="status"` on success, and no optimistic
 * success anywhere — and `modules/reporting/paramsForm.ts#missingRequired`,
 * the one route in the product that renders its validation.
 */

import type { FormEvent, ReactNode } from "react";

import { useT } from "../i18n/context";
import { getLanguage, translate } from "../i18n/core";
import { ApiError } from "../lib/api";
import { Icon } from "../shell/icons";
import { KIND_ICON } from "../shell/viz";
import { Button } from "./ui";

export interface FieldRule {
  /** Key in the values object. */
  name: string;
  /** THE NAME ON THE SCREEN, not the key. "Requester", never "requester_id". */
  label: string;
  /** Optional shape check. Return a message, or null when the value is fine. */
  validate?: (value: string) => string | null;
}

export interface Problem {
  name: string;
  message: string;
}

/**
 * What stops this submission going through, in the user's vocabulary.
 *
 * ABSENCE AND SHAPE SHARE ONE CHANNEL. Auditor B found an impossible CIDR
 * (`999.999.999.999/99`) accepted by the field with Register a silent no-op —
 * a different reason for the same silence. A second mechanism for "wrong"
 * beside the mechanism for "missing" is how a product ends up with the four
 * distinct error styles the audit also found.
 */
export function requiredProblems(
  rules: readonly FieldRule[],
  values: Readonly<Record<string, string | null | undefined>>,
): Problem[] {
  const problems: Problem[] = [];
  for (const rule of rules) {
    // Trimmed: a space is not an answer, and `" "` is truthy, which is how
    // whitespace-only input reached servers that then 422'd.
    const value = (values[rule.name] ?? "").trim();
    if (!value) {
      problems.push({ name: rule.name, message: translate("validation.required", { label: rule.label }) });
      continue;
    }
    const invalid = rule.validate?.(value);
    if (invalid) problems.push({ name: rule.name, message: invalid });
  }
  return problems;
}

/**
 * A submission that was refused, held as DATA: the rules and the values they
 * were checked against. Holding the finished `Problem[]` instead would keep the
 * sentences in the language active at submit time, and a language switch would
 * leave them behind. The sentences are rebuilt by `attemptProblems` at render.
 */
export interface Attempt {
  rules: () => readonly FieldRule[];
  values: Readonly<Record<string, string | null | undefined>>;
}

/** The problems of a refused submission, worded in the current language. */
export function attemptProblems(attempt: Attempt | null): readonly Problem[] {
  return attempt ? requiredProblems(attempt.rules(), attempt.values) : [];
}

/** The last resort, so that no failure can present as an empty string. */

/**
 * A failure, as a sentence.
 *
 * THIS FUNCTION IS THE BLOCKER FIX. `ApiError` is constructed as
 * `new ApiError(response.status, await response.text())`, so a bodyless
 * response — which a 503 from a proxy generally is — carries `message === ""`.
 * Every call site rendering `{error ? <span>{error.message}</span> : null}`
 * therefore rendered NOTHING: an empty string is falsy, so even the guard
 * passed. Auditor A found the consequence and filed it as a BLOCKER: "A failed
 * destructive API call (HTTP 503) is silently swallowed — the UI shows success
 * and the user is never told."
 *
 * Driven off `status` first and the body second, so the sentence exists
 * whether or not the server wrote one.
 *
 * CYCLE 4 OWNS WHAT THESE SENTENCES SAY. This is the seam and an honest
 * fallback; the full translation from API error shapes to human text — the
 * raw Pydantic JSON the audit found rendered in three modules — is that
 * cycle's subject, and it extends this function rather than adding a rival.
 */
/**
 * A wire field name as a person would read it: `default_queue_id` -> "Default
 * queue". The trailing `_id` goes because it names the storage, not the thing
 * the user picked.
 */
function humaniseField(name: string): string {
  const words = name.replace(/_id$/, "").split("_").filter(Boolean);
  if (words.length === 0) return name;
  return words.join(" ").replace(/^./, (c) => c.toUpperCase());
}

/**
 * FastAPI's validation errors, as sentences.
 *
 * THE SHAPE. This backend registers no custom handler, so a 422 arrives as
 * `{"detail":[{"type","loc","msg","input","ctx"}, …]}`. Every part of that is
 * for a machine except `msg`, and `loc` -- which names the field -- is an
 * array whose first element is the request part ("body").
 *
 * `input` IS DELIBERATELY NEVER READ. It echoes the value the user submitted,
 * so a secret field that fails a length rule puts the secret in the error
 * payload; rendering it writes a credential to the screen and to any
 * screenshot of it. Invariant 3 is about responses and logs, and a validation
 * echo is the same disclosure through a third door.
 *
 * Returns null when the body is not a shape this understands, so the caller
 * falls through to the status-based text rather than printing a payload.
 */
/**
 * ONE PYDANTIC ERROR AS A SENTENCE.
 *
 * `msg` is the server's own wording, always English (Pydantic's built-ins) or
 * the server's translation of a custom validator's text. While the console is
 * in English that is what is shown, unchanged -- it is what every screen and
 * test has always pinned, and a sentence of ours would replace a specific one
 * ("String should have at least 12 characters") with a paraphrase. In any other
 * language a built-in message is English that no one can translate for us, so
 * the error `type` is mapped to a catalogue sentence, filled from `ctx`. A type
 * the table does not know, or a `ctx` that lacks the number the sentence needs,
 * falls back to `msg` rather than guessing.
 *
 * `value_error` is a custom validator: the server wrote (and translated) the
 * message, and Pydantic prefixes it with "Value error, ", which is noise in
 * every language.
 */
function sentenceFor(type: unknown, msg: string, ctx: unknown): string {
  if (type === "value_error") return msg.replace(/^Value error, /, "");
  if (getLanguage() === "en") return msg;
  const given = (ctx && typeof ctx === "object" ? ctx : {}) as Record<string, unknown>;
  const figure = (key: string): string | null => {
    const value = given[key];
    return typeof value === "number" || (typeof value === "string" && value.trim() !== "") ? String(value) : null;
  };
  switch (type) {
    case "missing":
      return translate("validation.type.missing");
    case "string_too_short": {
      const min = figure("min_length");
      return min === null ? msg : translate("validation.type.stringTooShort", { min_length: min, count: Number(min) });
    }
    case "string_too_long": {
      const max = figure("max_length");
      return max === null ? msg : translate("validation.type.stringTooLong", { max_length: max, count: Number(max) });
    }
    case "int_parsing":
    case "float_parsing":
      return translate("validation.type.notANumber");
    case "greater_than_equal": {
      const ge = figure("ge");
      return ge === null ? msg : translate("validation.type.atLeast", { ge });
    }
    case "less_than_equal": {
      const le = figure("le");
      return le === null ? msg : translate("validation.type.atMost", { le });
    }
    case "enum":
    case "literal_error":
      return translate("validation.type.notAllowed");
    default:
      return msg;
  }
}

function describeValidation(body: string): string | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(body);
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null) return null;
  const detail = (parsed as { detail?: unknown }).detail;

  // `HTTPException(400, "sentence")` -- the server explained itself.
  if (typeof detail === "string" && detail.trim()) return detail.trim();

  if (!Array.isArray(detail) || detail.length === 0) return null;

  const problems = detail
    .map((item) => {
      if (typeof item !== "object" || item === null) return null;
      const { loc, msg, type, ctx } = item as { loc?: unknown; msg?: unknown; type?: unknown; ctx?: unknown };
      if (typeof msg !== "string" || !msg.trim()) return null;
      const field = Array.isArray(loc)
        ? loc.filter((part) => typeof part === "string" && part !== "body").at(-1)
        : undefined;
      const sentence = sentenceFor(type, msg, ctx);
      return typeof field === "string" ? `${humaniseField(field)}: ${sentence}` : sentence;
    })
    .filter((problem): problem is string => problem != null);

  // EVERY problem, not the first. Fixing one and resubmitting to discover the
  // next is how a four-field form takes four round trips.
  return problems.length > 0 ? problems.join(". ") : null;
}

export function describeSubmitError(error: unknown): string {
  if (error instanceof ApiError) {
    const body = error.message.trim();
    // The client's own 401 sentinel (`lib/api.ts`), not a server message.
    if (error.status === 401 && body === "unauthorized") return translate("validation.unauthorized");
    // A server that explained itself is believed...
    // A server that explained itself is believed -- but only if what it sent
    // is prose. `{` and `[` alone were not enough: a proxy answering for a
    // down API sends an HTML error page, and that was rendered into the UI as
    // the explanation, tags and all. Found by an agent adopting this kernel.
    const looksLikePayload = /^[{[<]/.test(body);
    if (body && !looksLikePayload) return body;
    // ...and one that explained itself in JSON is translated rather than
    // printed. This is the fix for five BLOCKERs across three modules: raw
    // Pydantic payloads rendered into the UI as the error message.
    const validation = body ? describeValidation(body) : null;
    if (validation) return validation;

    if (error.status === 503) return translate("validation.serviceUnavailable");
    if (error.status === 403) return translate("validation.forbidden");
    if (error.status === 404) return translate("validation.notFound");
    if (error.status === 409) return translate("validation.conflict");
    if (error.status >= 500) return translate("validation.serverFailed");
    // NOT `body ||`: reaching here means the body is JSON nothing could read,
    // and printing it is the defect this cycle exists to close.
    return translate("validation.unexplained");
  }
  if (error instanceof Error && error.message.trim()) return error.message;
  return translate("validation.unexplained");
}

/**
 * A real `<form>`, which is most of the fix for "Enter does not submit".
 *
 * `preventDefault` here rather than at every call site: a missed one is a full
 * page navigation that discards the user's work, and it would be missed,
 * because it is invisible until somebody presses Enter.
 */
export function Form({
  onSubmit,
  children,
  className,
}: {
  onSubmit: () => void;
  children: ReactNode;
  className?: string;
}) {
  return (
    <form
      className={className}
      noValidate
      onSubmit={(event: FormEvent) => {
        event.preventDefault();
        onSubmit();
      }}
    >
      {children}
    </form>
  );
}

/**
 * The submit control and everything the user must be told.
 *
 * `problems` is rendered as ONE announced summary rather than a message per
 * field, because a person who pressed submit needs the whole list to plan
 * their next action, and because `role="alert"` firing five times reads as
 * noise and gets ignored.
 */
export function FormActions({
  label,
  pendingLabel,
  pending,
  error,
  saved,
  problems = [],
  savedMessage,
  failedMessage,
  disabledReason,
  variant = "default",
  className,
}: {
  label: string;
  /** Overrides the default "<label>…" while the request is in flight. */
  pendingLabel?: string;
  pending: boolean;
  error?: unknown;
  saved?: boolean;
  problems?: readonly Problem[];
  savedMessage?: string;
  /**
   * A lead-in naming what did NOT happen: "Settings were not saved."
   *
   * The reason on its own can be read as advice rather than as a refusal --
   * "Auto close after days: Input should be less than or equal to 365" does
   * not say whether the save went through. Where a form sits among other
   * controls, or its effect is not obvious from where the message appears,
   * this sentence goes first, in the SAME alert so it is announced as one
   * thing.
   */
  failedMessage?: string;
  /** Shown beside a control disabled because the user may not use it at all. */
  disabledReason?: string;
  /**
   * THE WEIGHT OF THE SUBMIT CONTROL, AND WHY THIS IS NOT COSMETIC.
   *
   * `Button`'s `default` IS the commit magenta, and this component hard-coded
   * it, so every form in the product had to be the loudest thing on its screen.
   * DESIGN.md's Two Voices Rule is that magenta commits and there is normally
   * exactly one on a page — and `/patch` was already breaking it before this
   * parameter existed: the page head's Scan button is a `Form` + `FormActions`
   * pair, so a fleet SCAN — a read, which changes nothing — was painted in the
   * same hue as the approval that authorises code to run on 227 machines.
   *
   * Fixed HERE rather than around it (CLAUDE.md: a module registers into the
   * kernel, it does not fork it) because the alternative every call site
   * reaches for is dropping out of the kernel to get a plain `<Button>` back,
   * which takes the announced error and the no-optimistic-success rule with it.
   *
   * The default is unchanged, so no existing caller moves.
   */
  variant?: "default" | "outline" | "ghost";
  className?: string;
}) {
  const t = useT();
  const failure = error ? describeSubmitError(error) : null;

  return (
    <div className={`form-actions ${className ?? ""}`.trim()}>
      <Button type="submit" variant={variant} disabled={pending || Boolean(disabledReason)}>
        {/* `${label}…` RATHER THAN A CONJUGATION. The first version built this
            as `${label.replace(/e$/, "")}ing…`, which reads as English for
            exactly one shape of verb: "Save settings" became "Save
            settingsing…" and "Create profile" became "Create profileing…".
            Two agents adopting the kernel worked around it by SHORTENING their
            button labels -- a UI copy change to dodge a bug in here. English
            verb morphology is not something a template string gets right;
            `pendingLabel` is there for the cases where a nicer word exists. */}
        {pending ? (pendingLabel ?? `${label}…`) : label}
      </Button>

      {disabledReason ? <span className="form-hint">{disabledReason}</span> : null}

      {/* ONE alert, and failure wins over problems: if the server has already
          refused the request, what the user needs is that refusal, not a
          restatement of the client-side checks it got past. */}
      {/* EACH LINE CARRIES ITS KIND'S GLYPH (crit circle-x, ok circle-check),
          the same pair every pill in the console draws: the red and the green
          are never the only thing telling a refusal from a confirmation. */}
      {failure ? (
        <span role="alert" className="form-error">
          <Icon name={KIND_ICON.crit} size={13} />
          {failedMessage ? `${failedMessage} ` : ""}
          {failure}
        </span>
      ) : problems.length > 0 ? (
        <span role="alert" className="form-error">
          <Icon name={KIND_ICON.crit} size={13} />
          {problems.map((p) => p.message).join(". ")}.
        </span>
      ) : null}

      {/* NO OPTIMISTIC SUCCESS, the rule SaveBar already holds: `saved` is the
          server's 2xx and nothing else, and a failure suppresses it. */}
      {saved && !failure ? (
        <span role="status" className="form-saved">
          <Icon name={KIND_ICON.ok} size={13} />
          {savedMessage ?? t("validation.saved")}
        </span>
      ) : null}
    </div>
  );
}
