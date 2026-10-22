import { useState } from "react";
import { Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import { formatDate } from "../../lib/datetime";
import { FilterBar, PageHead, Panel, QueryState, StatusPill } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { useSubscriptionRenewals } from "./api";
import { actionKind } from "./subscriptionState";

// The look-ahead windows offered by the control below.
// backend/app/modules/subscriptions/api.py::renewals_route bounds
// `within_days` to [1, 365] with a default of 30, so every option here is
// inside the accepted range and no choice can produce a 422. A fixed set of
// windows rather than a free-text field keeps it that way.
const WINDOWS = [30, 60, 90] as const;

export function RenewalCalendar() {
  const t = useT();
  // A day count, not money -- `Number()` on the select value is safe here
  // (the Decimal money fields elsewhere in this module stay strings).
  const [withinDays, setWithinDays] = useState<number>(WINDOWS[0]);
  const renewals = useSubscriptionRenewals(withinDays);
  const items = renewals.data ?? [];
  const dueCount = items.filter((row) => row.action_required).length;

  return (
    <div>
      <PageHead
        title={t("subscriptions.renewals.title")}
        description={t("subscriptions.renewals.description")}
      />

      <Panel bare>
        {/* The kit's filter bar, as the sibling list pages draw it: the filter
            on the left, the count on the right. v3's `.tb` toolbar sits above
            a panel, never inside one; inside a bare panel its zero padding put
            the label and the count flush against the borders and clipped the
            count at 390px. The window is a labelled `<select>` on the kit's
            field class so it matches the search fields on the sibling pages. */}
        <FilterBar>
          <label className="flex items-center gap-2">
            <span className="note">{t("subscriptions.renewals.window")}</span>
            <select
              className="select-field"
              value={withinDays}
              onChange={(e) => setWithinDays(Number(e.target.value))}
            >
              {WINDOWS.map((days) => (
                <option key={days} value={days}>
                  {t("subscriptions.renewals.nextDays", { count: days })}
                </option>
              ))}
            </select>
          </label>
          <span className="filter-spacer" />
          {renewals.data ? (
            <span className="note">
              {t("subscriptions.renewals.summary", {
                window: t("subscriptions.renewals.inWindow", { count: items.length }),
                action: t("subscriptions.renewals.actionRequired", { count: dueCount }),
              })}
            </span>
          ) : null}
        </FilterBar>
        <QueryState
          loading={renewals.isLoading}
          error={renewals.error}
          empty={items.length === 0}
          emptyLabel={t("subscriptions.renewals.empty")}
          emptyIcon="clock"
        >
          <Table>
            <thead>
              <tr>
                <Th>{t("subscriptions.renewals.colService")}</Th>
                <Th>{t("subscriptions.renewals.colNotice")}</Th>
                <Th>{t("subscriptions.renewals.colRenews")}</Th>
                <Th>{t("subscriptions.renewals.colAction")}</Th>
              </tr>
            </thead>
            <tbody>
              {items.map((row) => {
                const kind = actionKind(row.action_required);
                return (
                  <tr key={row.subscription_id}>
                    <Td lead={<Icon name="calendar-event" size={18} />} gutter={kind}>
                      <div className="cell-title">{row.service_name}</div>
                      {row.days_to_renewal !== null ? (
                        <div className="cell-sub">
                          {row.days_to_renewal < 0
                            ? t("subscriptions.renewals.renewedAgo", { count: Math.abs(row.days_to_renewal) })
                            : t("subscriptions.renewals.renewsIn", { count: row.days_to_renewal })}
                        </div>
                      ) : null}
                    </Td>
                    {/* CALENDAR DATES, PINNED TO UTC. schema.py types both as
                        `date`, and reporting.py derives `notice_deadline` from
                        `renews_at` by subtracting `notice_days` -- whole days,
                        no clock. Rendering either through a zone west of UTC
                        moves it a day earlier, which on a cancellation-notice
                        deadline is the difference between "cancel today" and
                        "you were locked in yesterday". NO_DATE via the helper
                        rather than a bare `??`, so an absent date can never
                        become "Invalid Date". */}
                    <Td>{formatDate(row.notice_deadline, "UTC")}</Td>
                    <Td>{formatDate(row.renews_at, "UTC")}</Td>
                    <Td>
                      <StatusPill
                        value={row.action_required ? "action_required" : "on_track"}
                        kind={kind}
                        label={row.action_required ? t("subscriptions.renewals.pillAction") : t("subscriptions.renewals.pillOnTrack")}
                      />
                    </Td>
                  </tr>
                );
              })}
            </tbody>
          </Table>
        </QueryState>
      </Panel>
    </div>
  );
}
