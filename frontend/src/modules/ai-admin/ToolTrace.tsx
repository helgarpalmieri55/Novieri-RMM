// What the assistant actually looked at, in the honest shape §10.2 asks for.
//
// **A TRUNCATED RESULT MUST NOT READ AS THE WHOLE ANSWER.** The executor clamps
// a list before the request is built and reports `truncated` with it; the model
// is told, and so is the administrator. `total` is deliberately absent — the
// routes do not report one, and inventing a number the model would then quote is
// worse than saying "more were not shown".
//
// **A DENIED CALL READS AS DENIED, NEVER AS AN EMPTY RESULT.** §13 test 1: an
// empty result presented as fact produces a confident wrong answer instead of a
// visible failure. `forbidden` is the tool call that reached the route and was
// refused by the caller's own RBAC, and it says so.
//
// The tool name is a `.mono` span and not a bare `<code>`: a user agent gives
// `<code>` its own `monospace` family and, with it, its own 13px, which is a
// size off the ramp. `.mono` is the design's own class for the same thing.

import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import { lazyLabels } from "../../i18n/lazy";
import { humaniseStatus } from "../../lib/labels";
import type { AiToolCall } from "./types";

const STATUS_TEXT = lazyLabels({
  forbidden: "aiAdmin.trace.status.forbidden",
  not_found: "aiAdmin.trace.status.not_found",
  unknown_tool: "aiAdmin.trace.status.unknown_tool",
  invalid_arguments: "aiAdmin.trace.status.invalid_arguments",
  blocked: "aiAdmin.trace.status.blocked",
  timeout: "aiAdmin.trace.status.timeout",
  upstream_error: "aiAdmin.trace.status.upstream_error",
  session_expired: "aiAdmin.trace.status.session_expired",
} as const);

/**
 * What the call came to, in words. `ok` has no failure to report and reads as
 * the record count; a status this table does not name reads in the shared
 * status words (lower case), never as the wire word.
 */
function describe(call: AiToolCall): string {
  if (call.status !== "ok") {
    const known = (STATUS_TEXT as Record<string, string | undefined>)[call.status];
    return known ?? humaniseStatus(call.status).toLocaleLowerCase();
  }
  return translate(call.truncated ? "aiAdmin.trace.recordsTruncated" : "aiAdmin.trace.records", {
    count: call.result_count,
  });
}

export function ToolTrace({ toolCalls }: { toolCalls: AiToolCall[] }) {
  useT();
  if (toolCalls.length === 0) return null;
  return (
    <ul
      className="note"
      style={{ marginTop: 8, listStyle: "none", padding: 0, display: "grid", gap: 4 }}
    >
      {toolCalls.map((call, index) => (
        <li key={`${call.tool}-${index}`} data-testid={`ai-tool-${call.tool}`}>
          <span className="mono" style={{ fontWeight: 500 }}>
            {call.tool}
          </span>
          {" · "}
          {describe(call)}
        </li>
      ))}
    </ul>
  );
}
