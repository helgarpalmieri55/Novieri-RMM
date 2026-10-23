// Turning a report's PUBLISHED params schema into a form (spec §17.11, §11).
//
// **THE SERVER OWNS THIS CONTRACT AND THIS FILE READS IT.** Before §17.11 the
// catalogue published no params schema, so `ReportView` hardcoded the two
// parameters every definition happens to have (`limit`, `offset`) plus an
// optional window it sent only when filled. That was a second copy of a contract
// the server already held -- and the two copies were already disagreeing. EIGHT
// real parameters were unreachable from the browser entirely -- `os_family`,
// `device_group_id` and `stale_after_days` (three reports between them),
// `severity_at_least`, `window_days`, `scan_stale_after_intervals`, `queue_id`
// and `priority` -- while `from`/`to` were offered for the three reports that
// answer 422 for them.
//
// **NOTHING HERE IS A SECURITY DECISION.** The server validates every parameter
// against the same model this schema was derived from; `extra="forbid"` still
// makes an unknown name a 422 and every `ge`/`le` still binds. `min`/`max` on an
// input and `required` on a field are a courtesy that saves a round trip, not a
// gate -- invariant #6: the frontend gates nothing.
//
// **AN UNRECOGNISED SHAPE FALLS BACK TO A TEXT BOX, IT IS NEVER DROPPED.** A
// parameter this file cannot classify is still a parameter the report accepts,
// and a control the browser silently omits is a filter an operator cannot reach.
// Typing it as text and letting the server judge the value is the honest answer.

import { toInstant } from "../../lib/datetime";
import type { ParamProperty, ParamsSchema, ReportParams } from "./types";

export type ParamControlKind = "select" | "datetime" | "number" | "text";

export interface ParamControl {
  // The WIRE name -- `from`, not `window_from`. The server publishes aliases.
  name: string;
  kind: ParamControlKind;
  label: string;
  description: string;
  required: boolean;
  options: string[];
  min?: number;
  max?: number;
}

// The non-null branch of an optional parameter. Pydantic emits an optional
// field as `anyOf: [<the real type>, {type: "null"}]`, so a reader that only
// looked at the top level would classify every optional parameter as untyped.
function effective(property: ParamProperty): ParamProperty {
  const branches = property.anyOf;
  if (branches == null) return property;
  const real = branches.find((branch) => branch.type !== "null");
  if (real == null) return property;
  // The outer object still carries `description`/`title`/`default`; the branch
  // carries `type`/`format`/`enum`. Both matter, so they are merged rather than
  // one of them chosen.
  return { ...property, ...real };
}

function kindOf(property: ParamProperty): ParamControlKind {
  if (property.enum != null && property.enum.length > 0) return "select";
  if (property.format === "date-time") return "datetime";
  if (property.type === "integer" || property.type === "number") return "number";
  return "text";
}

export function paramControls(schema: ParamsSchema): ParamControl[] {
  const properties = schema.properties ?? {};
  const required = schema.required ?? [];
  return Object.keys(properties).map((name) => {
    const property = effective(properties[name] as ParamProperty);
    return {
      name,
      kind: kindOf(property),
      label: property.title ?? name,
      description: property.description ?? "",
      required: required.includes(name),
      options: property.enum ?? [],
      min: property.minimum,
      max: property.maximum,
    };
  });
}

// The form's starting values: THE SERVER'S OWN DEFAULTS, never numbers written
// here. A default written in the browser is the same second copy this file
// exists to delete -- and a `limit` the page defaults to 50 while the server
// defaults to 25 is a page that reports a different query than it ran.
//
// `default: null` is Pydantic's way of writing "optional, unset". It becomes an
// empty control, which `toQuery` then does not send at all -- the server may not
// be handed an empty string for a parameter typed as a UUID.
export function initialForm(schema: ParamsSchema): Record<string, string> {
  const properties = schema.properties ?? {};
  return Object.fromEntries(
    Object.keys(properties).map((name) => {
      const value = (properties[name] as ParamProperty).default;
      return [name, value == null ? "" : String(value)];
    }),
  );
}

function wireValue(control: ParamControl, raw: string, zone?: string | null): string {
  if (control.kind !== "datetime") return raw;
  // `<input type="datetime-local">` yields local wall-clock time with no zone.
  // The server wants an instant, and `format: "date-time"` is what says so.
  //
  // THE ZONE IS THE OPERATOR'S PROFILE ZONE, NOT THE BROWSER'S (cycle 5).
  // `new Date("2026-03-01T00:00")` used to do this, which reads the wall clock
  // in whatever zone the machine happens to be set to -- so the same window,
  // typed by the same operator, produced two different report periods from a
  // laptop and from a jump box, and neither was the period they meant. A
  // report window is the one parameter where that error is invisible: the
  // numbers come back plausible, just for the wrong hours.
  const instant = toInstant(raw, zone);
  // Unreadable input is still SENT AS TYPED rather than dropped: the server
  // owns validation (invariant #6), and a parameter the browser silently
  // deletes is a filter the operator believes is applied.
  return instant === "" ? raw : instant;
}

// Only the names the schema publishes, and only the ones that carry a value.
//
// A form field this page does not know about cannot reach the query string,
// because the loop is over `properties` rather than over the form -- so a stale
// key left in component state after a report change is dropped here instead of
// becoming a 422 for a parameter the operator never typed.
export function toQuery(
  schema: ParamsSchema,
  form: Record<string, string>,
  zone?: string | null,
): ReportParams {
  return Object.fromEntries(
    paramControls(schema)
      .map((control): [string, string] => [
        control.name,
        wireValue(control, form[control.name] ?? "", zone),
      ])
      .filter(([, value]) => value !== ""),
  );
}

// The required parameters with nothing in them. `sla-attainment` is the report
// with a required window and no default for it (§5.4: "a default would make the
// period invisible"), so opening its page runs nothing until an operator picks
// one -- which is better than the 422 the old hardcoded bar produced on load.
export function missingRequired(
  schema: ParamsSchema,
  form: Record<string, string>,
): string[] {
  return (schema.required ?? []).filter((name) => (form[name] ?? "") === "");
}
