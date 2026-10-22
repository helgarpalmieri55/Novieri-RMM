/**
 * A number beside its unit -- minutes, hours, days -- over a value that is
 * always seconds on the wire.
 *
 * `docs/design/settings-language.md`, "Durations stop being seconds": eight
 * Health fields took a raw integer in seconds and made the operator do the
 * arithmetic (`900`, `3600`, `86400`). This is the replacement -- a number
 * and a unit picker that displays the largest whole unit dividing the stored
 * value exactly (`900` -> `15 minutes`, `86400` -> `1 day`) and falls back to
 * seconds when nothing larger divides it evenly.
 *
 * THE WIRE NEVER SEES A UNIT. `onChange` always receives the equivalent
 * SECONDS, string-encoded, exactly like the plain `<input type="number">`
 * this replaces -- the operator picks a unit to read and type in; the server
 * still gets `"900"`, never `"15"` and `"minutes"` in two fields.
 *
 * THE DISPLAYED UNIT IS CHOSEN ONCE, ON MOUNT, FROM THE INCOMING VALUE, NOT
 * RECOMPUTED ON EVERY KEYSTROKE. Typing "90" while the unit reads "minutes"
 * must not suddenly relabel itself to "hours" because 5400 happens to divide
 * evenly -- that would rewrite the unit under the operator's own fingers. The
 * caller re-derives the unit by REMOUNTING this component (give it a `key`
 * that changes only when the authoritative value is reloaded -- Health's own
 * `seedSignature`, not the live edited form) whenever the stored value should
 * be redisplayed from scratch.
 *
 * ONE WRAPPING ELEMENT, NOT A FRAGMENT, BECAUSE THIS LIVES INSIDE `Field`.
 * `components/ui.tsx`'s `Field` clones exactly one child element to add
 * `aria-describedby` -- a fragment, or a bare pair of sibling inputs, leaves
 * that id pointed at nothing (see `Field`'s own comment on the failure). This
 * component receives `aria-describedby` as a prop and puts it on the NUMBER
 * input specifically.
 *
 * THE NUMBER INPUT CARRIES AN EXPLICIT `aria-label`, AND THAT IS NOT
 * DECORATIVE -- IT IS THE FIX FOR A REAL BROKEN NAME. `Field` wraps its
 * control inside the `<label>` (`<label><span>{label}</span>{children}</label>`),
 * and the HTML label-association algorithm does resolve a wrapping label's
 * CONTROL to the first labelable descendant in tree order -- the number
 * input, written before the unit `<select>`, exactly as an earlier version
 * of this comment claimed. What that algorithm decides is only which
 * control the label activates on click; it says nothing about the
 * ACCESSIBLE NAME a screen reader announces. For that, browsers read the
 * wrapping label's full rendered text content -- caption AND every
 * descendant's own text, the select's currently selected option included --
 * so without an explicit name the spinbutton announced as "Mark a device
 * offline after minutes", not "Mark a device offline after", and its name
 * changed every time the operator changed units. `aria-label` on the input
 * outranks the wrapping label in accessible-name precedence
 * (aria-labelledby, then aria-label, then native label association), so it
 * is what actually makes the number input's name the field's own label and
 * nothing else -- the click-to-focus behaviour the label association gives
 * for free is untouched, since that is a separate mechanism `aria-label`
 * does not affect. The unit `<select>`, which the label does NOT resolve to,
 * already carried its own distinct `aria-label` (`unitLabel` below) rather
 * than silently inheriting the field's name; that half was already correct.
 *
 * `className="duration-field"` ON THE WRAPPER IS LOAD-BEARING, NOT A HOOK
 * FOR A TEST. `shell/components.css`'s `.select-field { width: 100%;
 * max-width: 100% }` is UNLAYERED, and Tailwind puts a utility class like
 * `w-32` in `@layer utilities` -- an unlayered rule beats a layered one
 * regardless of specificity or source order, so a Tailwind width utility on
 * the select can never win here no matter how it is spelled. `.duration-field
 * > .select-field` is itself unlayered (plain CSS in the same file) with
 * specificity (0,2,0), which beats `.select-field`'s (0,1,0) HONESTLY, in the
 * same bucket, rather than by weakening the class every other select in the
 * product also wears. See the rule itself in `shell/components.css` for the
 * rest of the sizing (`flex` on the number input, `flex: none; width: auto`
 * on the select).
 */
import { useState } from "react";

import { useT } from "../i18n/context";
import type { MessageKey } from "../i18n/core";
import { inputClass } from "./ui";

export type DurationUnit = "seconds" | "minutes" | "hours" | "days";

const UNIT_SECONDS: Readonly<Record<DurationUnit, number>> = {
  seconds: 1,
  minutes: 60,
  hours: 3600,
  days: 86400,
};

// Coarsest first: this is a search for the LARGEST unit that still divides
// evenly, so it has to try days before it tries seconds.
const UNITS_LARGEST_FIRST: readonly DurationUnit[] = ["days", "hours", "minutes", "seconds"];

/**
 * The largest whole unit that divides `seconds` exactly. `seconds` always
 * divides evenly into itself, so this never falls through with nothing --
 * "stays in seconds" (the design's own words) is the last resort, not a
 * missing case.
 */
export function bestUnitFor(seconds: number): DurationUnit {
  for (const unit of UNITS_LARGEST_FIRST) {
    if (seconds % UNIT_SECONDS[unit] === 0) return unit;
  }
  /* istanbul ignore next -- "seconds" (factor 1) always matches above */
  return "seconds";
}

/**
 * Mirrors `modules/settings/settingsState.ts`'s `parseIntegerField`: `""` is
 * blank, not zero, and a fractional or non-numeric value is not a whole
 * number. Reimplemented locally (rather than imported) so this stays a
 * general-purpose primitive under `components/`, not coupled to one module.
 */
function parseWholeNumber(raw: string): number | null {
  const text = raw.trim();
  if (text === "") return null;
  const value = Number(text);
  return Number.isInteger(value) ? value : null;
}

const UNIT_OPTIONS: ReadonlyArray<{ value: DurationUnit; label: MessageKey }> = [
  { value: "seconds", label: "components.duration.seconds" },
  { value: "minutes", label: "components.duration.minutes" },
  { value: "hours", label: "components.duration.hours" },
  { value: "days", label: "components.duration.days" },
];

export function DurationInput({
  value,
  bounds,
  disabled = false,
  onChange,
  label,
  unitLabel,
  "aria-describedby": describedBy,
}: {
  /** Seconds, string-encoded -- `""` is blank, exactly like the raw input this replaces. */
  value: string;
  /** The bound in seconds (`SETTING_BOUNDS[key]`) -- an affordance only; the server re-checks. */
  bounds?: readonly [number, number];
  disabled?: boolean;
  /** Always seconds, string-encoded -- the wire format this control exists to keep unchanged. */
  onChange: (nextValueSeconds: string) => void;
  /**
   * The field's own accessible name -- the same string passed as `Field`'s
   * own `label` prop, e.g. "Mark a device offline after". Set as an explicit
   * `aria-label` on the number input so its announced name is exactly this
   * and never the wrapping `<label>`'s full text (which also contains the
   * unit select's current option) -- see the header comment.
   */
  label: string;
  /** The unit `<select>`'s OWN accessible name. Give it something distinct from the field's label. */
  unitLabel?: string;
  "aria-describedby"?: string;
}) {
  const t = useT();
  const initialSeconds = parseWholeNumber(value);
  const [unit, setUnit] = useState<DurationUnit>(
    initialSeconds != null ? bestUnitFor(initialSeconds) : "seconds",
  );
  // Seeded from `value` ONCE, at mount -- see the header comment on why this
  // does not track `value` on every render.
  const [text, setText] = useState<string>(
    initialSeconds != null ? String(initialSeconds / UNIT_SECONDS[unit]) : value,
  );

  function handleNumberChange(raw: string) {
    setText(raw);
    if (raw.trim() === "") {
      onChange("");
      return;
    }
    const parsed = parseWholeNumber(raw);
    if (parsed == null) {
      // Not a whole number in the chosen unit. Passed through as-is rather
      // than coerced: the caller's own `parseIntegerField` will judge this
      // exactly as it judged a raw seconds box, so the "Not a whole number"
      // refusal still fires for the same reason it always did.
      onChange(raw);
      return;
    }
    onChange(String(parsed * UNIT_SECONDS[unit]));
  }

  function handleUnitChange(nextUnit: DurationUnit) {
    // A unit switch REDISPLAYS the value; it does not change it. The wire
    // value `onChange` last sent is still correct, so this only recomputes
    // what the box shows, using the CURRENT (about-to-be-replaced) unit as
    // the source of truth for what the text presently means.
    const parsed = parseWholeNumber(text);
    if (parsed != null) {
      const seconds = parsed * UNIT_SECONDS[unit];
      setText(String(seconds / UNIT_SECONDS[nextUnit]));
    }
    setUnit(nextUnit);
  }

  const min = bounds ? bounds[0] / UNIT_SECONDS[unit] : undefined;
  const max = bounds ? bounds[1] / UNIT_SECONDS[unit] : undefined;

  return (
    <div className="duration-field">
      <input
        className={inputClass}
        type="number"
        min={min}
        max={max}
        value={text}
        disabled={disabled}
        aria-label={label}
        aria-describedby={describedBy}
        onChange={(event) => handleNumberChange(event.target.value)}
      />
      <select
        className="select-field"
        aria-label={unitLabel ?? t("components.duration.unit")}
        value={unit}
        disabled={disabled}
        onChange={(event) => handleUnitChange(event.target.value as DurationUnit)}
      >
        {UNIT_OPTIONS.map((option) => (
          <option key={option.value} value={option.value}>
            {t(option.label)}
          </option>
        ))}
      </select>
    </div>
  );
}
