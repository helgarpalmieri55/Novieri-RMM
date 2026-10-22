import type { ReactNode } from "react";

/**
 * A row that SELECTS what the pane beside it shows: an article in the
 * library list, a document in the Documents panel, a hit in the search
 * results.
 *
 * A LIST OPTION, NOT A `Btn`. Giving thirty of these the design's 37px
 * bordered button would turn a list into a wall of controls; what they owe
 * the keyboard is a focus ring. The ring is written on the tag rather than
 * left to the universal `:focus-visible` rule in `base.css` because
 * `src/test/jsx-tokens.test.ts` reads the tag: a `<button>` that wears
 * neither `.btn` nor the ring is reported as invisible to a keyboard user,
 * and the guard cannot see a stylesheet. The reset strips the user agent's
 * button chrome and nothing else, so the words inherit the row's own type.
 *
 * KIT GAP, reported: there is no `.row-option` class carrying this reset, the
 * ring and a hover wash, so the reset lives here until the kit has one.
 */
export function RowButton({
  selected,
  onClick,
  children,
}: {
  selected?: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      className="cell-title focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--accent)]"
      aria-current={selected ? "true" : undefined}
      onClick={onClick}
      style={{
        background: "none",
        border: 0,
        padding: 0,
        font: "inherit",
        color: "inherit",
        cursor: "pointer",
        textAlign: "left",
      }}
    >
      {children}
    </button>
  );
}
