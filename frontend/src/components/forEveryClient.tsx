import { useT } from "../i18n/context";
import { useEverywherePermissions } from "../lib/mePermissions";

/**
 * "FOR EVERY CLIENT": FILE A NEW ROW AS GLOBAL CONFIGURATION.
 *
 * MSP clients plan 2026-10-26 Task 14. An article, record, document or patch
 * approval made in this console -- and, since phase 3's final review, an SLA
 * policy -- belongs to one client: the one chosen in the top bar, or the one
 * its form's Client field names (`lib/clientSelection.ts`); a patch approval
 * made from a machine's drill-down, to that machine's client.
 * One made for every client is global, and the server lets only someone with
 * access to every client create it. So the switch renders only for a person
 * holding `code` on EVERY client -- `/me`'s `permissions_everywhere` -- and for
 * anyone else it is absent rather than offered and then refused.
 *
 * WHAT IT FILES CARRIES NO `X-Client-Id`, WHATEVER THE TOP BAR SAYS (phase 3
 * final review B, Critical 1). Narrowed to the client chosen above, the
 * server refuses a row with no client (403, `core/global_rows.py`) -- to the
 * very person this switch is drawn for. So the `ApiClient` method that sends
 * the row says it is global by its body (`RequestRoute.global`; pinned per
 * method by `docs/docsGlobalWrites.test.ts` and its siblings), and a client
 * chosen at the top neither hides the switch nor narrows what ticking it
 * files. The later writes to that row (edit, archive, revoke) go the same way.
 *
 * INVARIANT 6: THIS GATES NOTHING. The server refuses a global row from a
 * narrower scope whatever the console shows.
 */
export function ForEveryClientToggle({
  code,
  hint,
  checked,
  onChange,
}: {
  /** The permission the create route is gated on (`docs.manage`, `patch.approve`, `sla.admin`). */
  code: string;
  /**
   * What ticking does, where the generic sentence -- who will SEE the row --
   * is not the point: a patch approval's effect is its reach (final review E).
   */
  hint?: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
}) {
  const t = useT();
  const permissions = useEverywherePermissions();
  if (!permissions.has(code)) return null;
  return (
    <label className="flex items-start gap-2">
      <input
        type="checkbox"
        checked={checked}
        onChange={(event) => onChange(event.target.checked)}
      />
      <span>
        {t("components.forEveryClient.label")}
        <span className="note block">{hint ?? t("components.forEveryClient.hint")}</span>
      </span>
    </label>
  );
}
