import { useQuery } from "@tanstack/react-query";
import { Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import type { Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { formatDate } from "../../lib/datetime";
import {
  ClientFilter,
  FilterChips,
  ListEmpty,
  ListToolbar,
  PageHead,
  Panel,
  QueryState,
  ResultCount,
  StatusPill,
  useUrlFilters,
} from "../../shell/components";
import { Icon } from "../../shell/icons";
import { useSubscriptionRenewals } from "./api";
import { actionKind } from "./subscriptionState";
import { SubscriptionPageNav } from "./subscriptionsNav";
import { useRecordTitle } from "../../shell/title";

// The look-ahead windows offered by the control below.
// backend/app/modules/subscriptions/api.py::renewals_route bounds
// `within_days` to [1, 365] with a default of 30, so every option here is
// inside the accepted range and no choice can produce a 422. A fixed set of
// windows rather than a free-text field keeps it that way.
const WINDOWS = [30, 60, 90] as const;

export function RenewalCalendar() {
  const t = useT();
  // A sub-page without a rail entry names itself in the tab, as its h1 does
  // (the coordinator's ruling 1b): "<this page> · <rail entry> · Novieri RMM".
  useRecordTitle(t("subscriptions.renewals.title"));
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const canRead = (me.data?.permissions ?? []).includes("subscriptions.read");
  // THE WINDOW IS IN THE URL (Task 10, 5.4): `?within=60` is a link and a
  // reload keeps it; the default 30 days writes nothing. A value that is not
  // one of the windows falls back to the default, so no request can leave the
  // server's [1, 365] range. The Client chip (decision D6) is here because
  // the registry's renewal tiles link to this page with their client.
  const filters = useUrlFilters(["within", "client"] as const);
  // A day count, not money -- `Number()` on it is safe here (the Decimal
  // money fields elsewhere in this module stay strings).
  const asked = Number(filters.values.within);
  const withinDays: number = (WINDOWS as readonly number[]).includes(asked) ? asked : WINDOWS[0];
  const renewals = useSubscriptionRenewals(withinDays, filters.values.client);
  const items = renewals.data ?? [];
  const dueCount = items.filter((row) => row.action_required).length;

  return (
    <div>
      <PageHead
        title={t("subscriptions.renewals.title")}
        description={t("subscriptions.renewals.description")}
      />

      <SubscriptionPageNav canRead={canRead} />

      <Panel bare>
        {/* The kit's list toolbar: the window as chips (one of three, so no
            count: a count per window would be three more requests), the
            Client chip, and the window's own summary at the end. */}
        <ListToolbar
          chips={
            <>
              <FilterChips
                label={t("subscriptions.renewals.window")}
                options={WINDOWS.map((days) => ({
                  value: days === WINDOWS[0] ? "" : String(days),
                  label: t("subscriptions.renewals.nextDays", { count: days }),
                }))}
                value={withinDays === WINDOWS[0] ? "" : String(withinDays)}
                onChange={(value) => filters.set("within", value)}
              />
              <ClientFilter filters={filters} />
            </>
          }
          end={
            renewals.data ? (
              <ResultCount>
                {t("subscriptions.renewals.summary", {
                  window: t("subscriptions.renewals.inWindow", { count: items.length }),
                  action: t("subscriptions.renewals.actionRequired", { count: dueCount }),
                })}
              </ResultCount>
            ) : null
          }
        />
        <QueryState loading={renewals.isPending} error={renewals.error} empty={false} emptyLabel="">
          {items.length === 0 ? (
            /* The cause: nothing renews in this window. Under the Client chip,
               the kit's sentence and the way out. */
            <ListEmpty
              filtered={filters.values.client !== ""}
              onClear={filters.clear}
              nothingYet={t("subscriptions.renewals.emptyWindow", { count: withinDays })}
              icon="clock"
            />
          ) : (
          <Table label={t("subscriptions.renewals.title")}>
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
          )}
        </QueryState>
      </Panel>
    </div>
  );
}
