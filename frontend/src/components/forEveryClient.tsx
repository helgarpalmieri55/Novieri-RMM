import { useT } from "../i18n/context";
import { useMePermissions } from "../lib/mePermissions";

/**
 * "FOR EVERY CLIENT": FILE A NEW ROW AS GLOBAL CONFIGURATION.
 *
 * MSP clients plan 2026-10-26 Task 14. An article, record, document or patch
 * approval made in this console belongs to the organisation's internal client
 * (`lib/internalClient.ts`, until phase 3's selector); one made for every
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
  checked,
  onChange,
}: {
  /** The permission the create route is gated on (`docs.manage`, `patch.approve`). */
  code: string;
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
        <span className="note block">{t("components.forEveryClient.hint")}</span>
      </span>
    </label>
  );
}
