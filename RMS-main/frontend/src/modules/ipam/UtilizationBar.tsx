import { useT } from "../../i18n/context";
import { Progress, StatusPill } from "../../shell/components";
import { formatCountOrNa, utilizationDisplay } from "./ipamState";
import type { UtilizationOut } from "./types";

/**
 * The utilisation bar, and the pill that REPLACES it above the enumeration
 * ceiling. v3's "Used" cell: the count in mono over the bar.
 *
 * `percentage: null` is not zero. Above the org's ceiling, and for every IPv6
 * prefix, which is the same rule with a second application, the server can
 * still count the documented rows and the observations, but it will not
 * enumerate 16 million addresses to work out what fraction of them are free.
 * Rendering that as a bar at 0% would tell an operator their /12 is empty. So
 * this renders the word "unavailable" and the counts that ARE real, and draws
 * no bar at all.
 */
export function UtilizationBar({
  utilization,
  compact = false,
}: {
  utilization: UtilizationOut;
  compact?: boolean;
}) {
  const t = useT();
  const display = utilizationDisplay(utilization);

  if (!display.available) {
    return (
      <div title={display.detail}>
        <StatusPill value="unavailable" kind="mut" label={t("ipam.utilization.unavailable")} />
        <p className="note" style={{ marginTop: 4 }}>
          {compact
            ? t("ipam.utilization.compact", { assigned: utilization.assigned, observed: utilization.observed })
            : display.detail}
        </p>
      </div>
    );
  }

  // Narrowed by the `available` check above: a real percentage, never `?? 0`.
  const pct = display.barPercent;
  const tone = pct >= 90 ? "red" : pct >= 75 ? "gold" : "green";
  return (
    <div title={display.detail} style={{ minWidth: 150 }}>
      <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
        <span className="mono">
          {formatCountOrNa(display.used)} / {formatCountOrNa(display.capacity)}
        </span>
        <span className="note">{display.percentLabel}</span>
      </div>
      <Progress value={pct} tone={tone} label={t("ipam.utilization.aria", { prefix: utilization.prefix })} />
      {!compact && (
        <p className="note" style={{ marginTop: 4 }}>
          {display.detail}
        </p>
      )}
    </div>
  );
}
