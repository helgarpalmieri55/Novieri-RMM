/**
 * Subscriptions — the SaaS/cloud registry. Migrated onto the design per the
 * UI port spec §6, following DevicesPage.tsx as the template.
 *
 * WHAT THE DESIGN ASKS FOR THAT THE API DOES NOT SERVE. The prototype's
 * Subscriptions stats show five cards: Active subscriptions, Monthly run
 * rate, Renewals in 90d, Notice deadlines, Savings identified. Two have no
 * honest number behind them:
 *
 * - "Monthly run rate" -- `GET /subscriptions/spend` (useSpend) returns
 *   `by_currency` as ONE ROW PER (CLIENT, CURRENCY) (invariant #6: money is
 *   Decimal, never summed across currencies; and since MSP clients phase 2
 *   never summed across clients either). A registry with more than one such
 *   row has no single "run rate" number to show, so the card appears only
 *   when there is exactly one row: one client, paying in one currency. Two
 *   clients sharing a currency are two rows, and hide it too.
 * - "Savings identified" -- has no definition in the backend. Idle-seat cost
 *   (`useIdleSeats`) is the closest data, but it is per-subscription and
 *   per-currency too, and "identified savings" implies a tracked decision
 *   this module does not record. Omitted; needs both a definition and an
 *   aggregate endpoint (mirrors the Devices Risk gap, UI port spec §7).
 */

import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { attemptProblems, describeSubmitError, Form, FormActions } from "../../components/form";
import { NoValue } from "../../components/emptystate";
import type { Attempt, FieldRule } from "../../components/form";
import { EntityPicker } from "../../components/picker";
import { Button, Dialog, Field, Table, Td, Th, inputClass } from "../../components/ui";
import { useLanguage, useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import type { Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { formatDate } from "../../lib/datetime";
import { internalClientId, requireKnown } from "../../lib/internalClient";
import { humaniseEnum } from "../../lib/labels";
import {
  useCreateSubscriptionMutation,
  useOrgUsers,
  useSpend,
  useSubscriptionRenewals,
  useSubscriptions,
  useVendorDirectory,
} from "./api";
import {
  ownerName,
  ownerPickerEmptyHint,
  ownerPickerOptions,
  vendorPickerEmptyHint,
  vendorPickerOptions,
} from "./pickerOptions";
import { initialsOf, personIndex } from "../../lib/people";
import { settingsHref } from "../settings/tabs";
import { SubscriptionReadiness } from "./SubscriptionReadiness";
import { billingCycleLabel, statusKind } from "./subscriptionState";
import type { BillingCycle, SubscriptionOut } from "./types";
import { FilterBar, PageHead, Panel, QueryState, SearchField, StatCard, StatsGrid, StatusPill } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { Person } from "../../shell/viz";

const BILLING_CYCLES: BillingCycle[] = ["monthly", "quarterly", "annual"];
const RENEWAL_WINDOW_DAYS = 90;

/**
 * What a subscription may not be registered without, and what each field may
 * not contain.
 *
 * Closes two audit findings on this form. "Empty submit is a silent no-op":
 * the control read `disabled={create.isPending || !serviceName}`, so a Save
 * with the name blank moved nothing and named no field -- and the three
 * fields that ship with a default (`0.00`, `USD`, `0`) were never checked at
 * all, so clearing one submitted it empty. "Cost and renewal fields accept
 * nonsense values": `cost_per_cycle` is a Decimal server-side carried as a
 * string (module docstring / invariant #6), so nothing between the keyboard
 * and pydantic parses it -- "1,200" and "twelve hundred" were accepted here
 * and 422'd there, and the 422 was rendered as `String(create.error)`.
 *
 * `seats` went through `Number()` with no guard, so a cleared field became
 * NaN (serialising to null) and a typed "-4" became a negative seat count
 * that the idle-seat report then subtracts assignments from.
 */
// Built when asked, never held: the labels and sentences follow the language.
const subscriptionRules = (): readonly FieldRule[] => [
  { name: "service_name", label: translate("subscriptions.rules.serviceName") },
  { name: "cost_per_cycle", label: translate("subscriptions.rules.costPerCycle"), validate: decimalAmount },
  { name: "currency", label: translate("subscriptions.rules.currency"), validate: currencyCode },
  { name: "seats", label: translate("subscriptions.rules.seats"), validate: seatCount },
  // Cycle 7. The notice period is the input to the "Notice deadlines due" KPI
  // and to reporting.py's `notice_deadline = renews_at - notice_days`, and the
  // form had no control for it at all -- so the tile the page leads with could
  // only ever read 0. Zero is a real answer (a subscription cancellable up to
  // the renewal date), which is why it is a bounds check and not a required
  // non-empty field.
  { name: "notice_days", label: translate("subscriptions.rules.noticePeriod"), validate: noticeDays },
];

function decimalAmount(value: string): string | null {
  // No symbol and no thousands separator: the string is forwarded verbatim as
  // the Decimal, so whatever is typed here is what the backend parses.
  if (!/^\d+(\.\d{1,2})?$/.test(value)) {
    return translate("subscriptions.rules.costAmount");
  }
  return null;
}

function currencyCode(value: string): string | null {
  // ISO 4217 is three letters; the field is `maxLength={3}` but a paste of
  // "US" or "$" satisfies that and still means nothing.
  if (!/^[A-Za-z]{3}$/.test(value)) return translate("subscriptions.rules.currencyCode");
  return null;
}

function noticeDays(value: string): string | null {
  // A notice period longer than a year is a typo for every real contract, and
  // the number it is usually a typo FOR (30, 60, 90) is small. The server's own
  // bound is `ge=0, le=2147483647` (schema.py's `_MAX_COUNT`, there to keep an
  // int32 overflow from becoming a 500); this is the narrower business rule,
  // said before the round trip.
  if (!/^\d+$/.test(value)) return translate("subscriptions.rules.noticeWhole");
  if (Number(value) > 365) return translate("subscriptions.rules.noticeMax");
  return null;
}

function seatCount(value: string): string | null {
  // Zero is a real answer (a subscription billed per usage, not per seat);
  // a negative or fractional one is not.
  if (!/^\d+$/.test(value)) return translate("subscriptions.rules.seatsWhole");
  return null;
}

function matchesSearch(sub: SubscriptionOut, needle: string): boolean {
  if (needle === "") return true;
  // The cycle as it is worded on screen too, so "mensual" finds a row.
  return [sub.service_name, sub.plan, sub.category, sub.billing_cycle, humaniseEnum(sub.billing_cycle, "BillingCycle")]
    .join(" ")
    .toLowerCase()
    .includes(needle);
}

/**
 * The second line under a tile's figure: WHICH ones, not how many.
 *
 * v3's tile carries a breakdown under the count (`{k:'Renewing 30d',v:'4',
 * z:'€3,120'}`), and every count on this row was standing on its own. The
 * money in the design's example is not available here -- `cost_per_cycle` is
 * a Decimal per subscription in its own currency and invariant #6 forbids
 * adding those in the browser -- so each foot names the records behind the
 * number instead, which is the half of the fact an operator would open the
 * list to find anyway.
 *
 * Two names, then a count: a foot longer than its tile stops being readable,
 * and the page it links to has the rest.
 */
function nameList(names: readonly string[]): string {
  if (names.length <= 2) return names.join(", ");
  return translate("subscriptions.list.andMore", { names: names.slice(0, 2).join(", "), count: names.length - 2 });
}

export function SubscriptionsPage() {
  const t = useT();
  // The filter matches on worded labels, so it is rebuilt when the language changes.
  const { language } = useLanguage();
  const api = useApi();
  const [creating, setCreating] = useState(false);
  const [search, setSearch] = useState("");

  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const permissions = me.data?.permissions ?? [];
  const canRead = permissions.includes("subscriptions.read");
  const canManage = permissions.includes("subscriptions.manage");

  const subscriptions = useSubscriptions();
  const renewals = useSubscriptionRenewals(RENEWAL_WINDOW_DAYS);
  const spend = useSpend();
  // Read only to put a name and a face on `owner_user_id`. `users.read` is
  // not implied by `subscriptions.read`, so this may 403 -- `ownerName` falls
  // back to the id rather than to a blank.
  const users = useOrgUsers();
  // Not memoised: the sublabels are worded, and a held list would keep the
  // language it was built in.
  const ownerOptions = ownerPickerOptions(users.data?.users ?? []);

  const items = subscriptions.data ?? [];
  const filtered = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return items.filter((sub) => matchesSearch(sub, needle));
  }, [items, search, language]);

  const activeCount = items.filter((sub) => sub.status === "active").length;
  const noticeDueCount = (renewals.data ?? []).filter((row) => row.action_required).length;
  // v3's "No owner" tile: a live subscription nobody is accountable for is
  // the one that renews unread. Cancelled ones are nobody's problem.
  const noOwner = items.filter((sub) => sub.status !== "cancelled" && sub.owner_user_id === null);
  const noOwnerCount = noOwner.length;

  // The feet, each computed from the same query its figure is, and each
  // suppressed when that query failed or its count is zero -- a breakdown
  // beside "nothing renews in this window" would contradict the label above
  // it. The renewal window is ordered by how soon it lands, so the first row
  // is the next thing to happen; a row with no recorded renewal date has no
  // day to name and is not the soonest anything.
  const renewalRows = renewals.data ?? [];
  const nextRenewal = renewalRows
    .filter((row) => row.renews_at != null)
    .slice()
    .sort((a, b) => (a.days_to_renewal ?? 0) - (b.days_to_renewal ?? 0))[0];
  const noticeDue = renewalRows.filter((row) => row.action_required);

  // Exactly one (client, currency) row only -- see the module docstring above.
  const byCurrency = spend.data?.by_currency ?? [];
  const monthlyRunRate = byCurrency.length === 1 ? byCurrency[0] : null;

  // A STAT CARD MUST NOT REPORT A FAILED QUERY AS A NUMBER. `renewals.data ??
  // []` made a refused or unreachable `/subscriptions/renewals` render
  // "Renewals in 90 days: 0" and "Notice deadlines due: 0" -- two reassuring
  // figures no data supports, indistinguishable from a genuinely clear
  // quarter, on the one number in this module that is a deadline. Auditor A's
  // swallowed-503 class, in the place on this route where the UI still looked
  // successful after a failure. Each card falls back for its own query, and
  // the reason is said once, announced. `spend` needs no value fallback: its
  // card renders only when a single-currency row actually arrived -- but its
  // failure still belongs in the sentence, or "no run rate shown" reads as
  // "you have vendors in more than one currency". `subscriptions.error` is
  // deliberately NOT here: QueryState already reports it over the table, and
  // saying it twice is how a page teaches people to skip its alerts.
  const statsError = renewals.error ?? spend.error;

  return (
    <div>
      <PageHead
        title={t("subscriptions.list.title")}
        description={t("subscriptions.list.description")}
        actions={
          <>
            {/* This module contributes one top-level nav entry (App.tsx), so
                these are the only way into its two report pages. ONE element
                each, a link drawn as v3's secondary button, not a `<button>`
                inside an `<a>`: two overlapping controls for one target broke
                middle-click and "copy link address". The `subscriptions.read`
                condition is the same one App.tsx's nav entry uses and is
                presentational only: api.py's require_permission gates both
                routes server-side. */}
            {canRead && (
              <Link className="btn" to="/subscriptions/spend">
                <Icon name="chart-bar" size={14} />
                <span>{t("subscriptions.list.spend")}</span>
              </Link>
            )}
            {canRead && (
              <Link className="btn" to="/subscriptions/renewals">
                <Icon name="calendar-event" size={14} />
                <span>{t("subscriptions.list.renewals")}</span>
              </Link>
            )}
            {canManage && (
              <Button onClick={() => setCreating(true)}>
                <Icon name="plus" size={14} />
                {t("subscriptions.list.newSubscription")}
              </Button>
            )}
          </>
        }
      />

      <SubscriptionReadiness />

      {/* EVERY TILE NAMES THE LIST BEHIND IT. Two of these are the renewal
          calendar read two ways -- everything in the window, and the subset
          whose cancellation notice is closing -- so they land on the same
          page, which is the page that distinguishes them ("action required"
          vs "on track"). The run rate is the by-currency roll-up on the spend
          report, which is exactly this figure broken out.

          "Active subscriptions" is the one tile that points at the page it
          sits on, and that is deliberate rather than lazy: the table directly
          below IS the list of subscriptions, there is no second one anywhere
          in the product, and no page in this app expresses a status filter in
          its URL to link to. It stays a link so it behaves like the rest of
          the row -- focusable, middle-clickable, no dead corner.

          Weight goes to the notice deadlines alone: a renewal 90 days out is
          a thing to plan, a notice period closing is a thing to act on this
          week or pay for another year. */}
      <StatsGrid columns={monthlyRunRate ? 5 : 4}>
        {/* v3's order: the run rate leads the row when there is one to show
            (single currency), then the counts. */}
        {monthlyRunRate && (
          <StatCard
            label={t("subscriptions.list.tileRunRate")}
            value={`${monthlyRunRate.monthly_total} ${monthlyRunRate.currency}`}
            icon="budget"
            to="/subscriptions/spend"
            tone="primary"
            /* The server's own annual figure for the same row -- not this
               one multiplied by twelve, which would be the browser doing
               arithmetic on a Decimal. */
            foot={t("subscriptions.list.perYear", { amount: monthlyRunRate.annual_total, currency: monthlyRunRate.currency })}
          />
        )}
        <StatCard
          label={t("subscriptions.list.tileActive")}
          value={subscriptions.error ? t("subscriptions.common.na") : String(activeCount)}
          icon="subscription"
          to="/subscriptions"
          tone="blue"
          zeroLabel={t("subscriptions.list.tileActiveZero")}
          foot={
            !subscriptions.error && activeCount > 0 && items.length > activeCount
              ? t("subscriptions.list.ofRecorded", { count: items.length })
              : undefined
          }
        />
        <StatCard
          label={t("subscriptions.list.tileRenewals", { count: RENEWAL_WINDOW_DAYS })}
          value={renewals.error ? t("subscriptions.common.na") : String(renewalRows.length)}
          icon="clock"
          to="/subscriptions/renewals"
          tone="gold"
          zeroLabel={t("subscriptions.list.tileRenewalsZero")}
          foot={
            !renewals.error && nextRenewal?.renews_at
              ? t("subscriptions.list.nextRenewal", {
                  service: nextRenewal.service_name,
                  date: formatDate(nextRenewal.renews_at, "UTC"),
                })
              : undefined
          }
        />
        <StatCard
          label={t("subscriptions.list.tileNotice")}
          value={renewals.error ? t("subscriptions.common.na") : String(noticeDueCount)}
          icon="warning"
          to="/subscriptions/renewals"
          tone="orange"
          urgent={!renewals.error && noticeDueCount > 0}
          zeroLabel={t("subscriptions.list.tileNoticeZero")}
          foot={
            !renewals.error && noticeDueCount > 0
              ? nameList(noticeDue.map((row) => row.service_name))
              : undefined
          }
        />
        <StatCard
          label={t("subscriptions.list.tileNoOwner")}
          value={subscriptions.error ? t("subscriptions.common.na") : String(noOwnerCount)}
          icon="user"
          to="/subscriptions"
          tone="gold"
          urgent={!subscriptions.error && noOwnerCount > 0}
          zeroLabel={t("subscriptions.list.tileNoOwnerZero")}
          foot={
            !subscriptions.error && noOwnerCount > 0
              ? nameList(noOwner.map((sub) => sub.service_name))
              : undefined
          }
        />
      </StatsGrid>

      {statsError ? (
        <p role="alert" className="form-error">
          {describeSubmitError(statsError)}
        </p>
      ) : null}

      <Panel bare>
        <FilterBar>
          <SearchField
            placeholder={t("subscriptions.list.searchPlaceholder")}
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            aria-label={t("subscriptions.list.searchAria")}
          />
          <span className="filter-spacer" />
          <span className="note">{t("subscriptions.list.range", { shown: filtered.length, total: items.length })}</span>
        </FilterBar>

        <QueryState
          loading={subscriptions.isLoading}
          error={subscriptions.error}
          empty={filtered.length === 0}
          emptyLabel={items.length === 0 ? t("subscriptions.list.emptyNone") : t("subscriptions.list.emptyFiltered")}
          emptyIcon="subscription"
        >
          <Table>
            <thead>
              <tr>
                <Th>{t("subscriptions.list.colService")}</Th>
                <Th>{t("subscriptions.list.colCycle")}</Th>
                <Th>{t("subscriptions.list.colCost")}</Th>
                <Th>{t("subscriptions.list.colSeats")}</Th>
                <Th>{t("subscriptions.list.colOwner")}</Th>
                <Th>{t("subscriptions.list.colStatus")}</Th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((sub) => {
                const owner = ownerName(ownerOptions, sub.owner_user_id);
                return (
                  <tr key={sub.id}>
                    {/* v3's row: the glyph, the title, and one line under it
                        with the seat count and the renewal day. `renews_at` is
                        a calendar date, pinned to UTC for the reason the
                        detail page gives. */}
                    <Td lead={<Icon name="credit-card" size={18} />}>
                      <div className="cell-title">
                        <Link to={`/subscriptions/${sub.id}`}>{sub.service_name}</Link>
                      </div>
                      <div className="cell-sub">
                        {sub.renews_at
                          ? t("subscriptions.list.seatsRenews", { count: sub.seats, date: formatDate(sub.renews_at, "UTC") })
                          : t("subscriptions.list.seats", { count: sub.seats })}
                      </div>
                    </Td>
                    {/* TEXT, NOT A PILL. The cadence went through the
                        severity vocabulary -- annual `info`, everything else
                        `mut` -- so every monthly and quarterly row drew the
                        muted minus glyph, the console's mark for "not
                        applicable", one column from the row's real Status.
                        None of the three cadences is a state anybody acts
                        on. */}
                    <Td>{billingCycleLabel(sub.billing_cycle)}</Td>
                    <Td>
                      <span className="mono">{`${sub.cost_per_cycle} ${sub.currency}`}</span>
                    </Td>
                    <Td>
                      <span className="mono">{sub.seats}</span>
                    </Td>
                    <Td>
                      {owner && sub.owner_user_id ? (
                        <Person initials={initialsOf(owner)} index={personIndex(sub.owner_user_id)} name={owner} />
                      ) : (
                        <NoValue label={t("subscriptions.common.noOwner")} />
                      )}
                    </Td>
                    <Td>
                      <StatusPill
                        value={sub.status}
                        label={humaniseEnum(sub.status, "SubStatus")}
                        kind={statusKind(sub.status)}
                      />
                    </Td>
                  </tr>
                );
              })}
            </tbody>
          </Table>
        </QueryState>
      </Panel>

      <NewSubscriptionDialog open={creating} onClose={() => setCreating(false)} />
    </div>
  );
}

function NewSubscriptionDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const t = useT();
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const create = useCreateSubscriptionMutation();
  const vendors = useVendorDirectory();
  const users = useOrgUsers();

  // Not memoised: the sublabels are worded, and a held list would keep the
  // language it was built in.
  const vendorOptions = vendorPickerOptions(vendors.data ?? []);
  const ownerOptions = ownerPickerOptions(users.data?.users ?? []);

  const [serviceName, setServiceName] = useState("");
  const [billingCycle, setBillingCycle] = useState<BillingCycle>("monthly");
  const [costPerCycle, setCostPerCycle] = useState("0.00");
  const [currency, setCurrency] = useState("USD");
  const [seats, setSeats] = useState("0");
  const [renewsAt, setRenewsAt] = useState("");
  const [vendorId, setVendorId] = useState("");
  const [ownerUserId, setOwnerUserId] = useState("");
  const [noticeDaysValue, setNoticeDaysValue] = useState("0");
  // FALSE, the server's own default (schema.py: `auto_renew: bool = False`).
  // Defaulting it true here would have the form assert a contract term nobody
  // read -- and it is the term the "Notice deadlines due" tile is computed
  // from (`action_required = auto_renew and today >= notice_deadline`), so a
  // guessed `true` manufactures deadlines rather than reporting them.
  const [autoRenew, setAutoRenew] = useState(false);
  // Populated ON SUBMIT, never while typing -- complaining about a field the
  // operator has not finished filling in is its own kind of unhelpful. Held as
  // the rules and the values they were checked against, worded at render.
  const [attempt, setAttempt] = useState<Attempt | null>(null);

  function reset() {
    setServiceName("");
    setBillingCycle("monthly");
    setCostPerCycle("0.00");
    setCurrency("USD");
    setSeats("0");
    setRenewsAt("");
    setVendorId("");
    setOwnerUserId("");
    setNoticeDaysValue("0");
    setAutoRenew(false);
    setAttempt(null);
  }

  // SubscriptionsPage keeps this dialog mounted and toggles `open`, so a
  // failure from one attempt would otherwise still be on screen when the
  // form is next opened, describing a request the operator never made.
  function close() {
    create.reset();
    setAttempt(null);
    onClose();
  }

  function submit() {
    const next: Attempt = {
      rules: subscriptionRules,
      values: {
        service_name: serviceName,
        cost_per_cycle: costPerCycle,
        currency,
        seats,
        renews_at: renewsAt,
        notice_days: noticeDaysValue,
      },
    };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;

    // A subscription is one client's (MSP clients phase 2): its seats are held
    // by that client's people.
    // PHASE 3: the client/site selector. Until it exists that is the
    // organisation's internal client, whose id is the organisation's --
    // `/me`'s `org_id` (`lib/internalClient.ts`).
    const internalClient = internalClientId(me.data);
    create.mutate(
      () => ({
        client_id: requireKnown(internalClient),
        service_name: serviceName,
        billing_cycle: billingCycle,
        // Money stays a STRING to the wire (module docstring / invariant #6).
        cost_per_cycle: costPerCycle,
        currency,
        // `seats` is a count, so it is the one legitimate Number() here -- and
        // the rule above is what guarantees it is not NaN or negative first.
        seats: Number(seats),
        // Empty means "renewal date not known", which is a different fact
        // from a date and must reach the API as null, not as "".
        renews_at: renewsAt || null,
        // Both optional lookups: an unchosen one is `null`, never "" -- the
        // API types them as `UUID | None` and an empty string is a 422.
        vendor_id: vendorId || null,
        owner_user_id: ownerUserId || null,
        // A count, guarded by `noticeDays` above before this Number() runs.
        notice_days: Number(noticeDaysValue),
        auto_renew: autoRenew,
      }),
      {
        onSuccess: () => {
          reset();
          close();
        },
      },
    );
  }

  return (
    <Dialog open={open} onClose={close} title={t("subscriptions.new.title")}>
      <Form onSubmit={submit}>
        {/* The contract terms, paired. `full` on the service name because it
            is the record's identity and the only free-text line here; the
            rest read naturally two-up -- cycle beside cost, currency beside
            seats, the renewal date beside the notice that counts back from
            it. */}
        <div className="form-grid">
          <Field label={t("subscriptions.new.serviceName")} full>
            <input
              className={inputClass}
              required
              value={serviceName}
              onChange={(e) => setServiceName(e.target.value)}
            />
          </Field>
          <Field label={t("subscriptions.new.billingCycle")}>
            <select
              className={inputClass}
              value={billingCycle}
              onChange={(e) => setBillingCycle(e.target.value as BillingCycle)}
            >
              {BILLING_CYCLES.map((cycle) => (
                <option key={cycle} value={cycle}>
                  {humaniseEnum(cycle, "BillingCycle")}
                </option>
              ))}
            </select>
          </Field>
          <Field label={t("subscriptions.new.costPerCycle")}>
            {/* Text with a decimal keypad, not type="number": the value is a
                Decimal string on the wire, and a number input would hand us a
                locale-formatted or exponent-notation value to re-serialise. */}
            <input
              className={inputClass}
              type="text"
              inputMode="decimal"
              required
              value={costPerCycle}
              onChange={(e) => setCostPerCycle(e.target.value)}
            />
          </Field>
          <Field label={t("subscriptions.new.currency")}>
            <input
              className={inputClass}
              value={currency}
              maxLength={3}
              required
              onChange={(e) => setCurrency(e.target.value.toUpperCase())}
            />
          </Field>
          <Field label={t("subscriptions.new.seats")}>
            <input
              type="number"
              min={0}
              step={1}
              required
              className={inputClass}
              value={seats}
              onChange={(e) => setSeats(e.target.value)}
            />
          </Field>
          <Field label={t("subscriptions.new.renewsOn")}>
            {/* A real date input, so the value this form can produce is either
                empty or an ISO date -- the shape reporting.py's renewal
                calendar and notice-deadline arithmetic assume. */}
            <input
              type="date"
              className={inputClass}
              value={renewsAt}
              onChange={(e) => setRenewsAt(e.target.value)}
            />
            {/* The date this holds is spelled out under the grid, below
                (design review M1): a native date input draws the USER AGENT'S
                mask and no CSS reorders it, so this reads `09/22/2026` to a
                US-locale browser on a console that renders every other date
                `22 Sept 2026`. It is not inside this `<Field>` because `Field`
                wraps its control in the `<label>` and text placed here becomes
                part of the field's NAME; the accounting is at
                `agent-push/PushAuthorizationsPage`. "UTC" because a renewal
                date is a calendar DATE, not an instant -- through a zone it
                moves a day west of Greenwich, and the list above reads it the
                same way. */}
          </Field>
          {/* THE REST OF AUDITOR B's [HIGH]: "no notice period and no owner
              field ... Two of three headline metrics — and the module's stated
              differentiator, cancellation-notice deadlines — depend on data the
              UI provides no way to enter." All three controls below feed
              reporting.py's renewal_calendar, which computes
              `notice_deadline = renews_at - notice_days` and flags
              `action_required = auto_renew and today >= notice_deadline`. */}
          <Field label={t("subscriptions.new.noticePeriod")}>
            <input
              type="number"
              min={0}
              max={365}
              step={1}
              className={inputClass}
              value={noticeDaysValue}
              onChange={(e) => setNoticeDaysValue(e.target.value)}
            />
          </Field>
        </div>
        {renewsAt !== "" ? (
          <p className="note" style={{ marginBottom: 12 }}>
            {t("subscriptions.common.renewsOn", { date: formatDate(`${renewsAt}T00:00:00Z`, "UTC") })}
          </p>
        ) : null}
        <p className="note" style={{ marginBottom: 12 }}>
          {t("subscriptions.new.noticeNote", { tile: t("subscriptions.list.tileNotice") })}
        </p>
        <label className="flex items-center gap-2" style={{ marginBottom: 12 }}>
          <input
            type="checkbox"
            checked={autoRenew}
            onChange={(e) => setAutoRenew(e.target.checked)}
          />
          {t("subscriptions.common.renewsAutomatically")}
        </label>
        {/* Owner and vendor: two searches of the same shape, so they pair. */}
        <div className="form-grid">
          <EntityPicker
            label={t("subscriptions.new.owner")}
            options={ownerOptions}
            loading={users.isLoading}
            emptyHint={ownerPickerEmptyHint()}
            placeholder={t("subscriptions.common.searchPeople")}
            value={ownerUserId}
            onChange={setOwnerUserId}
          />
          <EntityPicker
            label={t("subscriptions.new.vendor")}
            options={vendorOptions}
            loading={vendors.isLoading}
            emptyHint={vendorPickerEmptyHint()}
            placeholder={t("subscriptions.common.searchVendors")}
            value={vendorId}
            onChange={setVendorId}
          />
        </div>
        {users.data != null && ownerOptions.length === 0 ? (
          <p className="note">
            <Link className="panel-link" to={settingsHref("users")}>
              {t("subscriptions.new.addUser")}
            </Link>
          </p>
        ) : null}
        {!vendors.isLoading && vendorOptions.length === 0 ? (
          <p className="note">
            <Link className="panel-link" to="/licenses/vendors">
              {t("subscriptions.new.addVendor")}
            </Link>
          </p>
        ) : null}
        <div className="flex justify-end gap-2">
          {/* `type="button"` is load-bearing inside a real `<form>`:
              ui.tsx's Button sets no type, and an untyped button submits --
              Cancel would create the subscription. */}
          <Button type="button" variant="ghost" onClick={close}>
            {t("subscriptions.common.cancel")}
          </Button>
          {/* Enabled with an empty Service name ON PURPOSE. `disabled={... ||
              !serviceName}` was the silent no-op the audit found: the click
              moved nothing and named no field. Now the click says which. */}
          <FormActions
            label={t("subscriptions.common.save")}
            pending={create.isPending}
            error={create.error}
            problems={attemptProblems(attempt)}
          />
        </div>
      </Form>
    </Dialog>
  );
}
