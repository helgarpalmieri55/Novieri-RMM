import { useSearchParams } from "react-router-dom";
import { describeSubmitError } from "../../components/form";
import { RelTime } from "../../components/reltime";
import { Badge, Table, Td, Th } from "../../components/ui";
import { NoValue } from "../../components/emptystate";
import { useT } from "../../i18n/context";
import { ApiError } from "../../lib/api";
import { Icon } from "../../shell/icons";
import { FilterBar, Panel } from "../../shell/components";
import { useNotificationDeadCount, useNotificationDeliveries } from "./api";
import {
  deliveryLastErrorMessage,
  deliveryStatusLabel,
  deliveryStatusVariant,
} from "./notificationState";
import type { DeliveryStatus } from "./types";

const STATUS_OPTIONS: readonly DeliveryStatus[] = ["pending", "sent", "failed", "dead"];

// Spec §7/§11: "Deliveries -- recent deliveries with status, attempt count,
// and the dead count surfaced prominently." Requirement #4: a system that
// silently stops delivering is indistinguishable from one with nothing to
// send, so the dead-letter count is rendered as its own callout at the top
// of this panel -- never buried inside the status filter a reader has to
// think to apply.
// `zone` is the SIGNED-IN USER'S PROFILE ZONE (`/me`.timezone), passed down
// from NotificationAdminPage which already holds `me` -- not read from the
// browser here. A retry schedule read in the wrong zone is a five-hour lie
// about when this system will next try to deliver (lib/datetime.ts's header).
// `null`/absent falls back to the browser zone inside the helper.
export function DeliveriesPanel({ zone }: { zone?: string | null }) {
  const t = useT();
  // THE FILTER LIVES IN THE QUERY STRING, not in component state, and that is
  // what makes the "Dead-lettered deliveries" stat tile above a real
  // destination rather than a self-link that changes nothing. The tile points
  // at `/notifications?status=dead#deliveries`; with the selection in local
  // state there was nowhere for it to point, because the only surface that
  // lists dead letters is this panel and the only way to narrow it was a
  // dropdown a reader had to think to apply. It also makes a filtered view
  // pasteable into an incident channel, for the same reason docs' `?article=`
  // is (DocsPage.tsx).
  const [searchParams, setSearchParams] = useSearchParams();
  const raw = searchParams.get("status") ?? "";
  // An unknown `?status=` is treated as no filter rather than sent to the
  // server: a typo in a pasted URL should show everything, not an empty table
  // that reads as "nothing has ever been delivered".
  const status: DeliveryStatus | "" = (STATUS_OPTIONS as readonly string[]).includes(raw)
    ? (raw as DeliveryStatus)
    : "";
  const setStatus = (next: DeliveryStatus | "") => {
    setSearchParams(
      (current) => {
        const params = new URLSearchParams(current);
        if (next) params.set("status", next);
        else params.delete("status");
        return params;
      },
      // Replace, not push: narrowing a log is refining one view, not visiting
      // a new place, and Back should leave the page rather than walk back
      // through four dropdown selections.
      { replace: true },
    );
  };

  const deadCount = useNotificationDeadCount();
  const deliveries = useNotificationDeliveries(status ? { status, limit: 50 } : { limit: 50 });

  const deadCountForbidden = deadCount.error instanceof ApiError && deadCount.error.status === 403;
  const deliveriesForbidden = deliveries.error instanceof ApiError && deliveries.error.status === 403;
  const deliveriesFailed = deliveries.error != null && !deliveriesForbidden;

  return (
    // The dead-letter stat tile links to `/notifications?status=dead#deliveries`.
    <div id="deliveries">
      <Panel
        title={t("notification.deliveries.title")}
        subtitle={t("notification.deliveries.subtitle")}
        bare
        actions={
          !deadCountForbidden && deadCount.isSuccess ? (
            <Badge
              variant={deadCount.data.total > 0 ? "destructive" : "muted"}
              aria-label={t("notification.deliveries.gaveUpAria")}
            >
              {t("notification.deliveries.gaveUp", { count: deadCount.data.total })}
            </Badge>
          ) : undefined
        }
      >
        {/* Auditor A (MEDIUM), "A failed delivery test is not recorded in the
            Deliveries log": after a Test that visibly failed, this panel still
            read "0 dead" and "No deliveries yet", so "a user who sees a failure
            in one panel and 'No deliveries yet' in the panel directly below
            cannot tell whether failures are being recorded at all". The
            behaviour is deliberate -- a test send writes no delivery row and no
            outbox event (delivery.py's `send_test`) -- and the audit's own
            alternative fix was to "explain the difference". Saying it is the
            honest half; claiming a test appears here would not be. */}
        <p className="note" style={{ padding: "12px 18px 0" }}>
          {t("notification.deliveries.testNote")}
        </p>

        <FilterBar>
          {/* Auditor A (LOW): "the delivery-status filter likewise uses raw
              lowercase pending / sent / failed / dead". The filter and the
              badge now read the same words. */}
          <select
            className="select-field"
            aria-label={t("notification.deliveries.filterAria")}
            value={status}
            onChange={(e) => setStatus(e.target.value as DeliveryStatus | "")}
          >
            <option value="">{t("notification.deliveries.allStatuses")}</option>
            {STATUS_OPTIONS.map((s) => (
              <option key={s} value={s}>
                {deliveryStatusLabel(s)}
              </option>
            ))}
          </select>
          <span className="filter-spacer" />
          {deliveries.data ? (
            <span className="mono cell-sub">
              {t("notification.deliveries.shown", { shown: deliveries.data.items.length, count: deliveries.data.total })}
            </span>
          ) : null}
        </FilterBar>

        {deliveries.isLoading ? (
          <p className="note" style={{ padding: "16px 18px" }}>{t("notification.common.loading")}</p>
        ) : deliveriesForbidden ? (
          <p className="note" style={{ padding: "16px 18px" }}>{t("notification.deliveries.forbidden")}</p>
        ) : deliveriesFailed ? (
          // `role="alert"` and the REASON, not just the fact: an unannounced
          // line of red text is invisible to a screen reader and says nothing a
          // reader can act on. `describeSubmitError` is the single translation
          // (components/form.tsx) -- it names a 503 as a 503 rather than
          // printing a Pydantic payload or an empty string.
          <p role="alert" className="notice red" style={{ margin: 14 }}>
            {t("notification.deliveries.failed", { reason: describeSubmitError(deliveries.error) })}
          </p>
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>{t("notification.deliveries.colRecipient")}</Th>
                <Th>{t("notification.deliveries.colStatus")}</Th>
                <Th>{t("notification.deliveries.colAttempts")}</Th>
                <Th>{t("notification.deliveries.colNext")}</Th>
              </tr>
            </thead>
            <tbody>
              {deliveries.data?.items.map((d) => (
                <tr key={d.id}>
                  {/* THE REASON BELONGS TO THE RECIPIENT, not to a sixth
                      column past the fold. As its own column the failure
                      excerpt was the narrowest cell in the table and the
                      longest text in it, so a dead-lettered row wrapped to
                      roughly 350px tall -- and at 390px that column sits
                      outside the viewport inside the table's horizontal
                      scroll, so the phone rendered a screen and a half of
                      near-empty rows carrying one recipient line each. Under
                      the recipient it has the widest cell in the row, it is
                      on screen at every width, and it reads as what it is:
                      this address, and what stopped the send.

                      `NoValue` still appears where a row that FAILED records
                      no reason -- an absent explanation for a failure is a
                      fact about the delivery. A sent or pending row needs no
                      such line, and drawing one would put an em dash on every
                      healthy row.

                      `deliveryLastErrorMessage` stays the one translation:
                      Auditor A (HIGH) filed the raw enum on the Test button,
                      and this is the same code in the same module, so without
                      it the log would go on printing `scheme_not_allowed` at
                      the operator after the button had stopped. */}
                  <Td lead={<Icon name="send" size={18} />} gutter={d.status === "dead" ? "crit" : d.status === "failed" ? "warn" : undefined}>
                    <div className="mono cell-title">{d.recipient}</div>
                    {d.last_error ? (
                      <div className="cell-sub">{deliveryLastErrorMessage(d.last_error)}</div>
                    ) : d.status === "dead" || d.status === "failed" ? (
                      <div className="cell-sub">
                        <NoValue label={t("notification.common.noErrorRecorded")} />
                      </div>
                    ) : null}
                  </Td>
                  <Td>
                    <Badge variant={deliveryStatusVariant(d.status)}>
                      {deliveryStatusLabel(d.status)}
                    </Badge>
                  </Td>
                  <Td>
                    <span className="mono">{d.attempts}</span>
                  </Td>
                  {/* Was the raw ISO string straight from the API -- a UTC
                      instant printed as "2026-08-20T19:00:00+00:00" beside
                      times rendered every other way in the product. */}
                  <Td>
                    <RelTime iso={d.next_attempt_at} zone={zone} />
                  </Td>
                </tr>
              ))}
              {deliveries.data && deliveries.data.items.length === 0 && (
                <tr>
                  <Td colSpan={4}>
                    <span className="note">{t("notification.deliveries.empty")}</span>
                  </Td>
                </tr>
              )}
            </tbody>
          </Table>
        )}
      </Panel>
    </div>
  );
}
