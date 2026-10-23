/**
 * The two ways this console says "there is nothing here".
 *
 * WHY ONE COMPONENT. On a single page -- /devices/:id -- the audit counted SIX
 * different treatments across six tabs: three plain sentences with the table
 * removed, one message stuffed into a table row leaving five trailing empty
 * cells, and two cards of labels reading "—" mixed with the literal word
 * "none" and badges saying "not enrolled". Its verdict is the requirement:
 * "The user must re-learn 'what does nothing look like?' on every tab."
 *
 * THE SHAPE IS v3's `empty(icon, title, copy, action)`: a muted glyph, a bold
 * title, a sentence, and the way out. `body` is required and says WHY it is
 * empty; `action` is where the way out goes. A state with neither is still
 * expressible, but you have to leave `action` off deliberately -- both
 * auditors filed empty states "phrased as a dead end" as a defect in its own
 * right.
 */

import type { ReactNode } from "react";

import { Icon, type IconProps } from "../shell/icons";

export interface EmptyStateProps {
  /** The glyph. Distinct per surface -- two panels sharing one icon and one
   *  sentence was itself a finding. */
  icon: IconProps["name"];
  /** What is missing, as a noun phrase: "No deployments recorded". */
  title: string;
  /** WHY it is missing, and what would fill it. One or two sentences. */
  body: string;
  /** The way out: usually a Btn or a Link. */
  action?: ReactNode;
  /**
   * The rung this title sits on. `3` by default, because the usual home for an
   * empty state is inside a titled `Panel`, whose title is the `h2`.
   *
   * PASS `2` WHEN THE PANEL HAS NO TITLE. An untitled `Panel` renders no `h2`,
   * so an `h3` inside it follows the page's `h1` directly and skips a level --
   * axe `heading-order`, and a screen-reader user is told there is a
   * subsection of a section that does not exist. `/ipam/search` is exactly
   * that shape and reported it at both widths the moment this component
   * arrived there. The harmony gate did NOT stop it: `heading-order` is
   * `moderate` and the gate fails on `serious`, which is why this prop exists
   * rather than a rule someone has to remember.
   */
  headingLevel?: 2 | 3;
}

export function EmptyState({ icon, title, body, action, headingLevel = 3 }: EmptyStateProps) {
  const Heading = headingLevel === 2 ? "h2" : "h3";
  return (
    <div className="empty">
      {/* A heading where the design renders `b`: a heading is what a screen
          reader lands on, and it is the rung under the panel's `h2`. Styled
          identically (`.empty b, .empty h3, .empty h2`). */}
      <span className="empty-icon" aria-hidden="true">
        <Icon name={icon} size={26} />
      </span>
      <Heading>{title}</Heading>
      <p>{body}</p>
      {action}
    </div>
  );
}

/**
 * A single missing value, inside a cell or a field.
 *
 * THE DASH ON ITS OWN IS THE BUG. "Screen readers announce this as 'em dash'
 * or skip it; sighted users cannot tell which field is empty" -- the audit, on
 * a placeholder that appeared eight times on one page. The dash stays as the
 * visual token, because a column of prose where the data should be is
 * unreadable, but it is `aria-hidden` and carries the real sentence beside it
 * for anything that is not looking at pixels.
 */
export function NoValue({ label }: { label: string }) {
  return (
    <span className="no-value">
      <span aria-hidden="true">—</span>
      <span className="sr-only">{label}</span>
    </span>
  );
}
