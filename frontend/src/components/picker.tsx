/**
 * Choosing a thing, instead of typing its UUID.
 *
 * WHY THIS EXISTS (UX remediation cycle 6). The audits found five fields whose
 * only input was a raw UUID typed by hand -- "Target ID", two "Device ID"s,
 * "Requester ID", "Assign to" -- plus a "Link action" that required knowing
 * one. Auditor A: "requires the operator to type a raw device UUID by hand --
 * no picker, no search, no validation."
 *
 * WHY IT IS WORSE THAN INCONVENIENT. A UUID typed by hand is a UUID that can
 * be typed wrong, and these fields dispatch signed jobs, start remote sessions
 * and run backups. The failure of a mistyped one is not a validation message;
 * it is the right action against the wrong machine.
 *
 * THE DATA STAYS AT THE CALL SITE. This takes `options` rather than fetching:
 * each module knows its own endpoint, its own permissions and its own idea of
 * what is selectable. A picker that fetched would need to know all three, and
 * would become the place every module's exceptions collected.
 */

import { useEffect, useId, useMemo, useRef, useState } from "react";

import { useT } from "../i18n/context";
import { inputClass } from "./ui";

export interface PickerOption {
  id: string;
  /** What the user calls it. */
  label: string;
  /** What tells two of them apart: "linux · online", "queue: incidents". */
  sublabel?: string;
}

export function EntityPicker({
  label,
  options,
  value,
  onChange,
  loading = false,
  emptyHint,
  placeholder,
  disabled = false,
  describedBy,
  onPendingChange,
}: {
  label: string;
  options: readonly PickerOption[];
  value: string;
  onChange: (id: string) => void;
  loading?: boolean;
  /**
   * What would make this list non-empty. Shown instead of an empty list --
   * but only when the list actually CAN be empty. A caller whose `options`
   * always carries a synthetic "none of these" entry has no empty list to
   * explain and may omit this (review finding 8).
   */
  emptyHint?: string;
  placeholder?: string;
  disabled?: boolean;
  /**
   * An id elsewhere in the form the input's `aria-describedby` should also
   * point at -- an orphan note explaining that a stored id the list cannot
   * resolve is still in the form and still being submitted, say. Additive:
   * omitted, the input carries no `aria-describedby` at all, exactly as it
   * did for every call site before this prop existed (review finding 3).
   */
  describedBy?: string;
  /**
   * Told whether the box holds TYPED TEXT that is not a choice -- something
   * searched for and not (yet) picked. What the box shows is then not the
   * value, and a form that acts on the value while the box says something
   * else acts on what the person was not looking at (final review, T11R-I1:
   * "Ware" typed, the grant sent to every client). A caller that submits
   * disables its submit while this is true. Additive: omitted, nothing is
   * told, as for every call site before it existed.
   */
  onPendingChange?: (pending: boolean) => void;
}) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState<string | null>(null);
  const listId = useId();
  const box = useRef<HTMLDivElement>(null);
  const blurTimeout = useRef<number | null>(null);

  // The blur close is deferred (below) so a click on an option is not
  // unmounted out from under itself. That timer must not outlive the
  // component: a picker closed by unmount -- the dialog around it dismissed,
  // the row it lived in removed -- would otherwise fire `setOpen`/`setQuery`
  // on an unmounted component and, worse, touch `window` after the test
  // environment that owned it has torn down.
  useEffect(
    () => () => {
      if (blurTimeout.current != null) window.clearTimeout(blurTimeout.current);
    },
    [],
  );

  const pending = query !== null;
  useEffect(() => {
    onPendingChange?.(pending);
  }, [pending, onPendingChange]);

  const chosen = options.find((option) => option.id === value);

  // NOT `chosen?.label ?? value`. Falling back to the raw value would print a
  // UUID in a field whose whole purpose is to avoid one -- and worse, an id
  // that is no longer in the list would render as text while still being
  // submitted. A value nobody can see is a value nobody chose.
  const shown = query ?? chosen?.label ?? "";

  const matches = useMemo(() => {
    const needle = (query ?? "").trim().toLowerCase();
    if (!needle) return options;
    return options.filter((option) =>
      `${option.label} ${option.sublabel ?? ""}`.toLowerCase().includes(needle),
    );
  }, [options, query]);

  const choose = (option: PickerOption) => {
    onChange(option.id);
    setQuery(null);
    setOpen(false);
  };

  return (
    <div className="picker" ref={box}>
      <label className="picker-label" htmlFor={`${listId}-input`}>
        {label}
      </label>
      <input
        id={`${listId}-input`}
        className={inputClass}
        role="combobox"
        aria-expanded={open}
        aria-controls={listId}
        aria-autocomplete="list"
        aria-describedby={describedBy}
        autoComplete="off"
        disabled={disabled}
        placeholder={placeholder ?? t("components.picker.search")}
        value={shown}
        onFocus={() => setOpen(true)}
        onClick={() => setOpen(true)}
        onChange={(event) => {
          setQuery(event.target.value);
          setOpen(true);
        }}
        onBlur={() => {
          // Deferred: a click on an option fires blur first, and closing here
          // synchronously would unmount the option before it is chosen.
          if (blurTimeout.current != null) window.clearTimeout(blurTimeout.current);
          blurTimeout.current = window.setTimeout(() => {
            blurTimeout.current = null;
            setOpen(false);
            setQuery(null);
          }, 120);
        }}
      />

      {open ? (
        <ul className="picker-list" id={listId} role="listbox" aria-label={label}>
          {loading ? (
            // A list that has not arrived and a list with nothing in it are
            // different states, and the hint would be a lie during the first.
            <li className="picker-note">{t("components.picker.loading")}</li>
          ) : matches.length === 0 ? (
            <li className="picker-note">{options.length === 0 ? emptyHint : t("components.picker.noMatch")}</li>
          ) : (
            matches.map((option) => (
              <li key={option.id}>
                <button
                  type="button"
                  role="option"
                  aria-selected={option.id === value}
                  className="picker-option"
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() => choose(option)}
                >
                  <span className="picker-option-label">{option.label}</span>
                  {option.sublabel ? (
                    <span className="picker-option-sub">{option.sublabel}</span>
                  ) : null}
                </button>
              </li>
            ))
          )}
        </ul>
      ) : null}
    </div>
  );
}
