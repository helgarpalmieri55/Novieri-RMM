/**
 * What the disposition vocabulary means, in words.
 *
 * Auditor A (LOW), "Disposition filter exposes `refused_loop` and other raw
 * enum values": "`refused_loop` is meaningless to a user; it presumably
 * denotes a mail loop the system declined to process, which is exactly the
 * kind of event an admin needs to understand. Sixth module exhibiting the
 * missing enum display-label layer."
 *
 * One map, used by BOTH render sites this module has -- the filter's options
 * and the badge on every row -- because the finding elsewhere in the product
 * is precisely a value spelled one way in a dropdown and another in the table
 * beside it.
 *
 * THE TWO CLAUSES ARE JOINED WITH A COLON, not an em dash. Every one of
 * these labels states a verdict and then the ground for it, and the port's
 * global constraints forbid an em dash in new UI copy; a colon carries
 * "verdict, then why" exactly as well and is what the rest of the console
 * uses.
 *
 * An unrecognised value falls back to its humanised self. The backend
 * deliberately passes `disposition` through unvalidated so one off-vocabulary
 * row cannot 500 the page (types.ts); swallowing such a value here would hide
 * the same row a different way.
 */

import { humaniseEnum, humaniseStatus } from "../../lib/labels";
import { DISPOSITIONS } from "./types";

// The words are `enums.Disposition`, looked up when read. A value the console
// has not met is humanised, never printed raw.
export function dispositionLabel(disposition: string): string {
  return (DISPOSITIONS as readonly string[]).includes(disposition)
    ? humaniseEnum(disposition, "Disposition")
    : humaniseStatus(disposition);
}
