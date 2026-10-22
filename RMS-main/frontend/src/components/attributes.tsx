/**
 * Structured values, rendered as something other than the JSON they arrived as.
 *
 * WHAT THIS IS FOR. "35 of the 105 table cells on the page contain unparsed
 * JSON printed verbatim" — including a NIC row 225 characters long, braces,
 * quotes, escaped `%` interface suffixes and all. The audit's own summary of
 * the defect is the design brief: "This is the API response leaking into the
 * presentation layer. The user must mentally parse JSON to learn a disk is
 * 256 GB or a NIC has IP 172.22.104.28/29."
 *
 * "VIEW RAW" IS KEPT ON PURPOSE. The same finding asks for it: engineers do
 * need the underlying object, and hiding it would trade one group's problem
 * for another's. It is a closed `<details>`, so it costs nothing until asked
 * for.
 *
 * BYTE-LOOKING KEYS ARE FORMATTED, and that is a naming convention this file
 * depends on rather than a type it can check: a key ending `_bytes` holds a
 * byte count. The convention is the wire's, not this component's invention —
 * `size_bytes`, `total_bytes`, `free_bytes` all appear in the inventory
 * payloads. A key that ends in `_bytes` and holds something else would be
 * mis-rendered, and that is worth saying out loud rather than discovering.
 */

import { useT } from "../i18n/context";
import { translate } from "../i18n/core";
import { formatBytes } from "../lib/format";
import { humaniseKey, humaniseStatus } from "../lib/labels";
import { NoValue } from "./emptystate";

/** One scalar, as a person reads it. */
export function describeValue(key: string, value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "number") {
    return /_bytes$/.test(key) ? formatBytes(value) : String(value);
  }
  if (typeof value === "boolean") return translate(value ? "components.attributes.yes" : "components.attributes.no");
  if (typeof value === "string") {
    // An identifier-shaped string is an enum; anything with a space, a slash
    // or a colon is already prose, an address or a path, and must survive
    // untouched -- "172.22.104.28/29" is not an enum.
    return /^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$/.test(value) ? humaniseStatus(value) : value;
  }
  if (Array.isArray(value)) return value.map((item) => describeItem(item)).join(", ");
  return describeItem(value);
}

/** A nested object, flattened to its most useful single line. */
function describeItem(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value !== "object") return String(value);
  if (Array.isArray(value)) return value.map(describeItem).join(", ");

  const entries = Object.entries(value as Record<string, unknown>);
  // An address record is the case this exists for: `{family, origin, address}`
  // is worth exactly one field, and printing the other two is the noise the
  // audit measured.
  const address = entries.find(([key]) => key === "address" || key === "value");
  if (address) return describeValue(address[0], address[1]);
  return entries.map(([key, item]) => `${humaniseKey(key)} ${describeItem(item)}`).join(" · ");
}

export interface AttributesProps {
  /** The object as it came off the wire. */
  value: unknown;
  /** What to say when there is nothing in it. Required: `{}` rendered as `{}`
   *  "reads as 'something is broken' rather than 'this hasn't produced a
   *  result yet'", and only the caller knows which of those it is. */
  empty: string;
  /** Show the underlying JSON behind a disclosure. On by default. */
  raw?: boolean;
}

export function Attributes({ value, empty, raw = true }: AttributesProps) {
  const t = useT();
  const entries =
    value && typeof value === "object" && !Array.isArray(value)
      ? Object.entries(value as Record<string, unknown>)
      : [];

  if (entries.length === 0) return <NoValue label={empty} />;

  return (
    <div>
      <div className="attributes">
        {entries.map(([key, item], at) => (
          <span key={key}>
            {at > 0 ? <span className="attribute-sep"> · </span> : null}
            <span className="attribute-key">{humaniseKey(key)} </span>
            <span>{describeValue(key, item)}</span>
          </span>
        ))}
      </div>
      {raw ? (
        <details className="attribute-raw">
          <summary>{t("components.attributes.viewRaw")}</summary>
          <pre className="code">{JSON.stringify(value, null, 2)}</pre>
        </details>
      ) : null}
    </div>
  );
}
