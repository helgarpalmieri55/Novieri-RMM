import { useT } from "../i18n/context";
import { useMePermissions } from "../lib/mePermissions";

/**
 * "FOR EVERY CLIENT": FILE A NEW ROW AS GLOBAL CONFIGURATION.
 *
 * MSP clients plan 2026-10-26 Task 14. An article, record, document or patch
 * approval made in this console belongs to the organisation's internal client
 * (`lib/internalClient.ts`, until phase 3's selector) -- a patch approval made
 * from a machine's drill-down to that machine's client; one made for every
 * client is global, and the server lets only someone with access to every
 * client create it. So the switch renders only for a person holding `code` on
 * EVERY client -- what `/me`'s permissions list -- and for anyone else it is
 * absent rather than offered and then refused.
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
  /** The permission the create route is gated on (`docs.manage`, `patch.approve`). */
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
  const permissions = useMePermissions();
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
