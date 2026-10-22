/**
 * One timestamp convention for the console: relative time in the page's own
 * face, the absolute time (in the operator's zone) on hover and in the
 * `<time>` element's machine-readable value. Not monospace: a date is prose.
 */

import { useT } from "../i18n/context";
import { describeWhen, formatDateTime, NO_DATE } from "../lib/datetime";

export function RelTime({
  iso,
  zone,
  now,
}: {
  iso: string | null | undefined;
  /** The operator's zone, for the hover text. */
  zone?: string | null;
  /** One instant per render, so two rows cannot disagree about "now". */
  now?: number;
}) {
  useT();
  const relative = describeWhen(iso, now, zone);
  if (relative === NO_DATE || !iso) return <span>{NO_DATE}</span>;
  const absolute = formatDateTime(iso, zone);
  return (
    <time dateTime={iso} title={absolute}>
      {relative}
    </time>
  );
}
