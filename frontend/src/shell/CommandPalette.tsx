/**
 * "Find or ask" -- the top bar's search field and the results that drop below
 * it.
 *
 * ---------------------------------------------------------------------------
 * IT WAS A MODAL, AND THE OPERATOR ASKED FOR IT NOT TO BE.
 *
 * "The search bar is a pop-up, it shouldn't be like that." What was objected
 * to is the SURFACE, not the capability: a button in the top bar opened a
 * centred dialog over a 38%-black scrim, with its own second search input
 * inside it, and the console behind it went out of play. Two inputs for one
 * query is what `TopSearch.tsx` existed to reconcile, and reconciling them was
 * never the point -- it was the cost of the dialog.
 *
 * So the dialog is gone and everything it carried is kept. The field lives in
 * the top bar and is a real `<input>`; the results, the assistant hand-off and
 * the empty state drop below it as an anchored panel. `TopSearch.tsx` is
 * deleted rather than rewritten, because a separate affordance component only
 * made sense while the thing it opened was somewhere else.
 *
 * WHAT THAT CHANGES ABOUT THE CONTRACT, stated because a test asserted each of
 * these and each assertion had to be rewritten rather than deleted:
 *
 * | Was (dialog)                              | Is (inline combobox) |
 * |-------------------------------------------|----------------------|
 * | `role="dialog" aria-modal="true"`         | `role="combobox"` on the field, owning a `listbox` popup — the APG pattern this always was underneath |
 * | Tab is TRAPPED inside the surface         | Tab LEAVES, and leaving closes the panel. A non-modal popup that held the keyboard would be the defect, not the feature |
 * | A scrim, and the page behind inert        | No scrim. The page behind stays live and scrollable, which is the whole of what "not a pop-up" means |
 * | Escape returns focus to the trigger       | Escape returns focus to whatever was focused when `/` was pressed, and otherwise leaves it in the field, which IS the trigger |
 * | The keystroke that opened it was SEEDED in| The field is a real input, so the keystroke lands in it natively — the seeding existed only to paper over the handover |
 *
 * The browser harness measures the two that jsdom cannot: that Tab now leaves
 * (and closes), and that `/`-then-Escape puts focus back where it started.
 *
 * ---------------------------------------------------------------------------
 * WHAT IS UNCHANGED, and was transcribed from the prototype's `#palette`
 * markup and its `paletteItems` / `renderPalette()` helpers in
 * `docs/design/itms-prototype.html`.
 *
 * The prototype builds `paletteItems` itself, flattening `navGroups`,
 * `devices`, `tickets`, `alerts` and `docs` module-level constants into one
 * searchable array. That is exactly the seam the UI port spec (§1) says has
 * no backend behind it in a mock -- so this component does not reproduce it.
 * `items` is a prop. Nothing in this file hardcodes a device, a ticket, or a
 * module list; the caller (wired to the real typed API client) decides what is
 * searchable.
 *
 * FILTERING matches the prototype's `renderPalette()`: a query is matched
 * case-insensitively against `label`, `sub` and `type` joined with spaces,
 * and results are capped at 18. Selection defaults to the first result
 * (`i===0 ? 'active' : ''`), same as the prototype.
 *
 * KEYBOARD. The prototype has **no ArrowUp/ArrowDown handling at all**; the
 * active item never moves except by re-filtering. That is a gap, not a design
 * choice to preserve: a `role="listbox"` a keyboard user cannot arrow through
 * is not navigable, so this component adds Up/Down selection movement the
 * prototype's static demo has no need of.
 *
 * ACCESSIBILITY THE PROTOTYPE DOES NOT HAVE. The source markup is a
 * `<section class="palette">` of `<button class="palette-item">` elements --
 * no listbox semantics at all. This transcribes the design's APPEARANCE
 * (class names, structure, the `.meta` type label, the shortcut hint), not
 * its accessibility gaps: `role="listbox"` on the results container and
 * `role="option"` + `aria-selected` on each item.
 */

import {
  useEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type RefObject,
} from "react";

import { useT } from "../i18n/context";
import { Icon } from "./icons";
import "./overlays.css";

export interface CommandPaletteItem {
  id: string;
  label: string;
  /** `item.sub` in the prototype -- the secondary line under the label. */
  sub?: string;
  /** `item.type` in the prototype -- rendered in `.meta` and included in the
   * search match, same as the source (`${label} ${sub} ${type}`). */
  type?: string;
  /** `item.icon` in the prototype's `mini-icon`. Defaults to `search`. */
  icon?: string;
  /** Already matched by the server; shown as-is rather than re-filtered by
   *  label, since a device found by its address need not contain the query
   *  in its name. */
  prefiltered?: boolean;
  onSelect: () => void;
}

export interface AskAffordance {
  /** Where the question goes, given what the operator typed. */
  onAsk: (question: string) => void;
  /** What the destination is called, in the operator's words. */
  label: string;
}

/**
 * WHY CLOSING CARRIES A REASON, and it is not bookkeeping.
 *
 * The three ways this panel closes want three different things done with the
 * keyboard, and a single `onClose()` cannot tell them apart:
 *
 *   - `escape`  — the operator backed out. Focus goes back to whatever they
 *                 were on when they pressed `/`.
 *   - `select`  — a destination was chosen. The app navigates; moving focus
 *                 first would fight the page that is about to render.
 *   - `dismiss` — focus left the field (Tab, or a click elsewhere). Restoring
 *                 it here would drag the operator back into the control they
 *                 just deliberately left, which is a focus trap built out of
 *                 good intentions.
 */
export type PaletteCloseReason = "escape" | "select" | "dismiss";

export interface CommandPaletteProps {
  /** Whether the results panel is showing. The FIELD always renders. */
  open: boolean;
  /** The field wants the panel open: it was focused, or typed into. */
  onOpen?: () => void;
  onClose: (reason: PaletteCloseReason) => void;
  /** Never hardcoded here -- see the file header. */
  items: readonly CommandPaletteItem[];
  /**
   * FIND OR ASK. Typing surfaces the option to hand the same words to the
   * assistant instead of matching them against records — the pattern HubSpot
   * uses, and the reason is the same: a search box is where a person arrives
   * already holding a question, and half of those questions are not lookups.
   * "Which machines missed last night's patch window and why" matches nothing
   * and is answerable.
   *
   * Omitted when the operator lacks `ai.use`, so the affordance never offers
   * a destination they cannot reach.
   */
  ask?: AskAffordance;
  /**
   * The field itself, handed back to the shell.
   *
   * The `/` shortcut lives up there because it listens on the whole document
   * and has to know what it interrupted; the FIELD lives down here. A
   * shortcut that opened a panel anchored to a control nobody can see would
   * leave the console holding open state with nothing on screen to close it,
   * so the shell asks THIS element whether it is visible rather than
   * restating a breakpoint in JS. Below 1024px `shell.css` collapses the
   * control to its magnifier rather than removing it, so the answer there is
   * yes and the palette is reachable on a handset.
   */
  fieldRef?: RefObject<HTMLInputElement | null>;
  /** Told every query, so the caller can look records up for it. */
  onQueryChange?: (query: string) => void;
  /** True while the caller is still looking records up for the query. */
  pending?: boolean;
  /** What the field promises to find. Should match what the caller searches. */
  placeholder?: string;
  /** The empty state's explanation of what search covers. */
  emptyHint?: string;
}

const RESULT_LIMIT = 18;

function filterItems(items: readonly CommandPaletteItem[], query: string): CommandPaletteItem[] {
  const q = query.trim().toLowerCase();
  const matches = q
    ? items.filter(
        (item) =>
          item.prefiltered ||
          `${item.label} ${item.sub ?? ""} ${item.type ?? ""}`.toLowerCase().includes(q),
      )
    : items.filter((item) => !item.prefiltered);
  return matches.slice(0, RESULT_LIMIT);
}

export function CommandPalette({
  open,
  onOpen,
  onClose,
  items,
  ask,
  fieldRef,
  onQueryChange,
  pending = false,
  placeholder,
  emptyHint,
}: CommandPaletteProps) {
  const t = useT();
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const own = useRef<HTMLInputElement>(null);
  const search = fieldRef ?? own;

  useEffect(() => {
    onQueryChange?.(query);
  }, [query]);

  // A fresh query every time, matching the prototype's `openPalette()`, which
  // clears `paletteInput.value` and calls `renderPalette('')` before focusing
  // -- but done on CLOSE rather than on open, and the difference is the whole
  // of what makes an inline field work. The field is always on screen, so
  // leaving a dismissed search sitting in the top bar would leave it looking
  // live; and clearing on OPEN would wipe the character that opened the panel,
  // because typing into the field is now what opens it.
  //
  // FOCUS ON OPEN IS THIS COMPONENT'S JOB, because the panel can be opened by
  // something that is not the field: the shell's `/` shortcut sets `open` from
  // the far side of the page and expects the caret to arrive here.
  useEffect(() => {
    if (open) {
      search.current?.focus();
      return;
    }
    setQuery("");
    setActiveIndex(0);
  }, [open]);

  const results = filterItems(items, query);

  // Re-filtering can shrink the result list out from under the current
  // selection; clamp rather than let the index point past the end.
  useEffect(() => {
    setActiveIndex((index) => Math.min(index, Math.max(results.length - 1, 0)));
  }, [results.length]);

  function activate(item: CommandPaletteItem | undefined) {
    if (!item) return;
    onClose("select");
    item.onSelect();
  }

  function handleKeyDown(event: ReactKeyboardEvent<HTMLInputElement>) {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (!open) {
        onOpen?.();
        return;
      }
      setActiveIndex((index) =>
        event.key === "ArrowDown"
          ? Math.min(index + 1, results.length - 1)
          : Math.max(index - 1, 0),
      );
      return;
    }
    if (event.key === "Enter") {
      if (!open) return;
      event.preventDefault();
      const hit = results[activeIndex] ?? results[0];
      // NOTHING MATCHED IS THE ASK CASE, not a dead end. A question phrased as
      // a question matches no record by construction, so Enter on an empty
      // result set hands it to the assistant rather than doing nothing.
      if (!hit && ask && query.trim()) {
        onClose("select");
        ask.onAsk(query.trim());
        return;
      }
      activate(hit);
    }
  }

  return (
    <div
      className="topbar-find"
      /* ESCAPE IS BOUND HERE, NOT ON THE FIELD AND NOT ON THE DOCUMENT.
         On the field it stopped working the moment focus reached a result --
         the browser pass tabbed once and left a surface no keypress could
         dismiss. On the document it would be a non-modal popup answering a
         key pressed anywhere on a page it does not own. The wrapper is the
         exact scope: the field and its results, and nothing else. */
      onKeyDown={(event) => {
        if (event.key !== "Escape" || !open) return;
        event.stopPropagation();
        onClose("escape");
      }}
      /* FOCUS LEAVING THE CONTROL IS HOW A NON-MODAL POPUP CLOSES. A scrim
         would be the modal answer and it is the one that was rejected.
         `relatedTarget` inside the wrapper -- a result being tabbed to -- is
         not leaving, so it does not close. */
      onBlur={(event) => {
        if (!open) return;
        const next = event.relatedTarget as Node | null;
        if (next && event.currentTarget.contains(next)) return;
        onClose("dismiss");
      }}
    >
      <span className="topbar-find-icon" aria-hidden="true">
        <Icon name="search" size={14} />
      </span>
      <input
        ref={search}
        className="topbar-search"
        type="text"
        /* THE APG COMBOBOX PATTERN, which is what a text field owning a
           listbox has always been. It carried `aria-controls` and
           `aria-activedescendant` while calling itself a plain textbox, which
           is half the pattern with none of the semantics.

           BOTH REFERENCES ARE CONDITIONAL ON `open`, and that is not
           tidiness: `aria-controls` pointing at an id that is not in the
           document is an `aria-valid-attr-value` violation, and the listbox
           only exists while the panel is rendered. */
        role="combobox"
        aria-expanded={open}
        aria-controls={open ? "palette-results" : undefined}
        aria-activedescendant={
          open && results[activeIndex] ? `palette-item-${results[activeIndex].id}` : undefined
        }
        aria-autocomplete="list"
        /* "Find or ask", not "Search": the second verb is the whole point, and
           a placeholder that only promises search is why nobody would try the
           other thing. The accessible name says the same, because a
           placeholder is not a label. */
        aria-label={t("shell.palette.fieldLabel")}
        /* The caller names what it actually searches for this account, so
           the field never promises a record kind it will not look up. The
           accessible name stays the short form. */
        placeholder={placeholder ?? t("shell.palette.placeholder")}
        autoComplete="off"
        value={query}
        onFocus={() => {
          if (!open) onOpen?.();
        }}
        onChange={(event) => {
          if (!open) onOpen?.();
          setQuery(event.target.value);
        }}
        onKeyDown={handleKeyDown}
      />
      {/* THE SHORTCUT HINT, AND IT SWAPS. `/` is how you get here and `Esc` is
          how you leave, and only one of the two is ever true at a time. Both
          used to be rendered, in two different components, because the dialog
          had a top bar to show one on and a header of its own to show the
          other. */}
      <kbd className="kbd">{open ? "Esc" : "/"}</kbd>
      {open ? (
        <div
          className="palette open"
          /* A MOUSEDOWN ANYWHERE IN THE PANEL WOULD BLUR THE FIELD, and the
             blur handler above would close the panel out from under the click
             that was about to activate a result. Suppressing the default on
             mousedown keeps the caret in the field; the click still fires. */
          onMouseDown={(event) => event.preventDefault()}
        >
          {ask && query.trim() ? (
            <button type="button" className="palette-ask" onClick={() => { onClose("select"); ask.onAsk(query.trim()); }}>
              <span className="palette-ask-icon">
                <Icon name="sparkles" size={15} />
              </span>
              <span className="palette-ask-copy">
                <strong>{t("shell.palette.ask", { label: ask.label })}</strong>
                <span className="palette-ask-query">&ldquo;{query.trim()}&rdquo;</span>
              </span>
              <span className="palette-ask-go">
                <Icon name="chevronRight" size={15} />
              </span>
            </button>
          ) : null}
          <div className="palette-results">
            {/* THE LISTBOX CONTAINS OPTIONS AND NOTHING ELSE, and axe rated the
                alternative `critical`: the no-results empty state -- a `<div
                class="empty">` around an `<h3>` -- used to render INSIDE this
                container. `role="listbox"` may own only `option` and `group`,
                so a heading in it is undefined to a screen reader, and "nothing
                matched" is the state a searching user hits most often. The
                empty state is now a sibling, and the listbox stays mounted and
                empty so the field's `aria-controls="palette-results"` keeps
                pointing at something that exists. It is also NAMED: axe reads a
                listbox as an ARIA input field and wants an accessible name, and
                this one had none. */}
            <div id="palette-results" role="listbox" aria-label={t("shell.palette.results")}>
              {results.map((item, index) => (
                <button
                  className={`palette-item${index === activeIndex ? " active" : ""}`}
                  key={item.id}
                  id={`palette-item-${item.id}`}
                  role="option"
                  aria-selected={index === activeIndex}
                  onMouseEnter={() => setActiveIndex(index)}
                  onClick={() => activate(item)}
                  type="button"
                >
                  <span className={`mini-icon${item.type === "alert" ? " gold" : ""}`}>
                    <Icon name={item.icon ?? "search"} size={14} />
                  </span>
                  <span>
                    <strong className="palette-label">{item.label}</strong>
                    {item.sub ? <span className="palette-sub">{item.sub}</span> : null}
                  </span>
                  {item.type ? <span className="meta">{item.type}</span> : null}
                </button>
              ))}
            </div>
            {results.length ? null : pending ? (
              <p className="palette-status" role="status">
                {t("shell.palette.searching")}
              </p>
            ) : (
              <div className="empty">
                <div>
                  <div className="empty-icon">
                    <Icon name="search" size={22} />
                  </div>
                  <h3>{t("shell.palette.noRecords")}</h3>
                  <p>
                    {emptyHint ?? t("shell.palette.pageNamesOnly")}
                    {ask
                      ? t("shell.palette.askInstead", { label: ask.label })
                      : ""}
                  </p>
                </div>
              </div>
            )}
          </div>
        </div>
      ) : null}
    </div>
  );
}
