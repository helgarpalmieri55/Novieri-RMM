/**
 * WHAT A PERSON HAD TYPED WHEN THEIR SESSION ENDED, KEPT ONLY SO IT CAN BE
 * COPIED (operator decision 3, 2026-11-05, "Ocultar y vaciar").
 *
 * When the session ends the console stops drawing the page and empties the
 * query cache (`App.tsx`'s `RequireAuth`), so a tenant's data does not stay on
 * an unattended screen. Losing a half-written reply with it is the defect
 * auditor A filed as HIGH ("the session drops mid-task ... total loss of
 * context"), so the text a person typed is read out of the page before it
 * goes and shown, read-only, on the notice.
 *
 * WHAT COUNTS AS TYPED. A text box the person typed, pasted or dropped into:
 * the browser fires `input` for each of those, and never for a value the page
 * set itself. So a field the page filled from the server and nobody touched
 * is not kept, and a box typed into and emptied again keeps nothing. This
 * cannot know whether a value was saved since -- an edit form left open after
 * Save still holds what was typed -- and showing a person their own words
 * again costs nothing.
 *
 * PASSWORD INPUTS ARE NEVER KEPT, and neither is any other secret:
 *
 *   - a box that is `type="password"`, or WAS one when it was typed into or
 *     focused -- a "Show" toggle that turns it into text afterwards does not
 *     make its value keepable;
 *   - a box whose `autocomplete` names a password or a one-time code;
 *   - a box inside `[data-secret]`: a secret typed in clear, which none of
 *     the above can tell from prose -- the step-up's recovery code, a license
 *     key with its mask lifted.
 *
 * NOTHING IS WRITTEN ANYWHERE. The watch holds element references for as long
 * as the signed-in tree is mounted; what it keeps lives in the guard's state
 * until the document is replaced, and goes with it.
 */

export interface KeptText {
  /** The field's label as the page showed it, or `""` when it had none. */
  label: string;
  text: string;
}

export interface TypingWatch {
  /** The typed text still on the page, in document order, with every secret left out. */
  keep(): KeptText[];
  stop(): void;
}

type TextField = HTMLInputElement | HTMLTextAreaElement;

/** The input types whose value is prose a person types. */
const TEXT_TYPES = new Set(["text", "search", "email", "url", "tel", "number"]);
const SECRET_AUTOCOMPLETE = /\b(?:current-password|new-password|one-time-code)\b/i;

function isTextField(target: EventTarget | null): target is TextField {
  if (target instanceof HTMLTextAreaElement) return true;
  return target instanceof HTMLInputElement && TEXT_TYPES.has(target.type);
}

function looksSecret(field: Element): boolean {
  if (field instanceof HTMLInputElement && field.type === "password") return true;
  if (SECRET_AUTOCOMPLETE.test(field.getAttribute("autocomplete") ?? "")) return true;
  return field.closest("[data-secret]") !== null;
}

/** A label's own words: its text without the field (a textarea's text is its value) or any other control in it. */
function wordsOf(label: Element, field: Element): string {
  return Array.from(label.childNodes)
    .filter((node) => !(node instanceof Element && (node.contains(field) || node.matches("input, textarea, select"))))
    .map((node) => node.textContent ?? "")
    .join(" ")
    .replace(/\s+/g, " ")
    .trim();
}

function labelOf(field: TextField, doc: Document): string {
  const labelled = Array.from(field.labels ?? [])
    .map((label) => wordsOf(label, field))
    .filter(Boolean)
    .join(" ");
  if (labelled) return labelled;
  const named = field.getAttribute("aria-label")?.trim();
  if (named) return named;
  const by = field.getAttribute("aria-labelledby");
  if (by) {
    const words = by
      .split(/\s+/)
      .map((id) => doc.getElementById(id)?.textContent?.trim() ?? "")
      .filter(Boolean)
      .join(" ");
    if (words) return words;
  }
  return "";
}

/** Start remembering which boxes the person types into. Stop it when the signed-in tree goes. */
export function watchTyping(doc: Document): TypingWatch {
  const typed = new WeakSet<Element>();
  const secret = new WeakSet<Element>();
  let watching = true;

  // Focus comes before the first keystroke: a password box revealed by a
  // toggle after it was focused is still remembered as one.
  const onFocus = (event: Event) => {
    if (event.target instanceof Element && looksSecret(event.target)) secret.add(event.target);
  };
  const onInput = (event: Event) => {
    const target = event.target;
    if (!(target instanceof Element)) return;
    if (looksSecret(target)) {
      secret.add(target);
      return;
    }
    if (isTextField(target)) typed.add(target);
  };
  doc.addEventListener("focusin", onFocus, true);
  doc.addEventListener("input", onInput, true);

  return {
    keep() {
      if (!watching) return [];
      return Array.from(doc.querySelectorAll<TextField>("input, textarea"))
        .filter((field) => typed.has(field) && !secret.has(field) && !looksSecret(field) && isTextField(field))
        .filter((field) => field.value.trim() !== "")
        .map((field) => ({ label: labelOf(field, doc), text: field.value }));
    },
    stop() {
      watching = false;
      doc.removeEventListener("focusin", onFocus, true);
      doc.removeEventListener("input", onInput, true);
    },
  };
}
