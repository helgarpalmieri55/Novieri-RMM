import { Fragment } from "react";
import type { DetailFact } from "./ipamState";

/**
 * A `detail` payload, rendered as labelled facts.
 *
 * ONE presentation for both places a free-form `Record<string, unknown>`
 * reaches an operator, the address-history panel (§11.3) and a drift finding's
 * evidence (§4.3), for the same reason `describeHistoryDetail` and
 * `describeDriftDetail` share one engine: the copy that is not being looked at
 * is the copy that quietly goes back to printing JSON.
 *
 * v3's `.kv`, not a `<pre>`: the label is the question and the value is the
 * answer, and a value that wraps stays attached to the label it belongs to.
 */
export function DetailFacts({
  facts,
  empty,
  narrow = false,
}: {
  facts: readonly DetailFact[];
  /** What to say when the payload carried nothing. Omit to render nothing. */
  empty?: string;
  /** Inside a table cell: the label track is `fit-content(20ch)` rather than
   * the kit's `max-content`, so a long question cannot hold the whole column
   * open and push the table's last column past the panel edge at 1440.
   *
   * 20ch, not 18ch: the ceiling only bites when a label exceeds it, and every
   * label it bit WRAPPED TO TWO LINES, which is the cost this cap exists to
   * avoid paying somewhere else. The drift labels were shortened to fit under
   * it (`ipamState.ts`, `DRIFT_FACT_RULES`) rather than the cap raised to fit
   * them, and the longest of them is 19 characters. */
  narrow?: boolean;
}) {
  if (facts.length === 0) {
    return empty === undefined ? null : <p className="note">{empty}</p>;
  }
  return (
    <dl
      className="kv"
      style={{
        gap: "4px 12px",
        fontSize: "var(--t-sm)",
        ...(narrow ? { gridTemplateColumns: "fit-content(20ch) 1fr" } : {}),
      }}
    >
      {facts.map((fact) => (
        <Fragment key={fact.key}>
          <dt>{fact.label}</dt>
          {/* `overflow-wrap: break-word`, NOT `word-break: break-word`. The two
              wrap the same, but the latter also zeroes the value column's
              min-content, and inside a table cell that let `.kv`'s 1fr track
              collapse to a few pixels: a MAC broke at every character and one
              drift row stood 700px tall. This one keeps the longest word. */}
          <dd style={{ overflowWrap: "break-word" }}>{fact.value}</dd>
        </Fragment>
      ))}
    </dl>
  );
}
