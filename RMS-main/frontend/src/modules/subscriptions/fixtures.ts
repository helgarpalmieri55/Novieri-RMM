/**
 * Fixtures for the subscriptions module: the SaaS registry, each
 * subscription's seats and history, and the three reports computed from it.
 *
 * ONE REGISTRY, EVERY REPORT DERIVED FROM IT. `spend` normalises each active
 * subscription's cost to a month and rolls it up per currency, vendor,
 * category and owner -- never across currencies, which is the module's own
 * rule -- and `idleSeats` subtracts the held seat assignments from each
 * subscription's seat count. `renewals` reads the same renewal dates and
 * notice periods, so the "notice deadlines due" tile on `/subscriptions` and
 * the "action required" pill on `/subscriptions/renewals` are one fact.
 *
 * Owners are people from the shell's worked directory (the service desk's
 * `listUsers` fixture), so the same person carries the same avatar tone on
 * every page. Vendors come from the licenses fixture, the way the real API
 * shares the vendor table between the two modules.
 *
 * DEV-ONLY BY CONSTRUCTION: reached through `fixtures/client.ts` only, which
 * nothing imports outside `FIXTURE_MODE`.
 */

import type { FixtureMap } from "../../fixtures/client";
import { ApiError } from "../../lib/api";
import type {
  EventOut,
  IdleSeatRow,
  RenewalRow,
  SeatAssignmentOut,
  SpendRow,
  SubscriptionOut,
} from "./types";

const MINUTE = 60_000;
const DAY = 24 * 60 * MINUTE;

/** An ISO timestamp `days` before the call. */
const ago = (days: number): string => new Date(Date.now() - days * DAY).toISOString();
/** A calendar date (no time, no zone) `days` from today. */
const dateIn = (days: number): string => new Date(Date.now() + days * DAY).toISOString().slice(0, 10);

const notFound = (what: string) => new ApiError(404, JSON.stringify({ detail: `${what} not found` }));

/* ----------------------------------------------------------- subscriptions */

interface SubscriptionSeed {
  id: string;
  vendor: string | null;
  name: string;
  plan: string;
  category: string;
  cycle: "monthly" | "quarterly" | "annual";
  cost: string;
  currency: string;
  seats: number;
  owner: string | null;
  startedDays: number;
  /** Days from today; null when the renewal date is not known. */
  renewsDays: number | null;
  autoRenew: boolean;
  noticeDays: number;
  paymentRef: string | null;
  licenseRef: string | null;
  status: "active" | "trial" | "cancelled";
}

const SEEDS: SubscriptionSeed[] = [
  { id: "sub-1", vendor: null, name: "Figma Organization", plan: "Organization", category: "Design", cycle: "monthly", cost: "540.00", currency: "EUR", seats: 12, owner: "usr-lf", startedDays: 410, renewsDays: 14, autoRenew: true, noticeDays: 30, paymentRef: "Card ending 4417", licenseRef: null, status: "active" },
  { id: "sub-2", vendor: null, name: "GitHub Enterprise", plan: "Enterprise Cloud", category: "Engineering", cycle: "monthly", cost: "714.00", currency: "EUR", seats: 34, owner: "usr-mr", startedDays: 700, renewsDays: 116, autoRenew: true, noticeDays: 30, paymentRef: "Invoice", licenseRef: null, status: "active" },
  { id: "sub-3", vendor: null, name: "Slack Business+", plan: "Business+", category: "Collaboration", cycle: "quarterly", cost: "4320.00", currency: "EUR", seats: 120, owner: null, startedDays: 900, renewsDays: 40, autoRenew: true, noticeDays: 60, paymentRef: "Invoice", licenseRef: null, status: "active" },
  { id: "sub-4", vendor: "ven-atlassian", name: "Atlassian Jira Cloud", plan: "Premium", category: "Engineering", cycle: "annual", cost: "8100.00", currency: "EUR", seats: 60, owner: null, startedDays: 160, renewsDays: 205, autoRenew: false, noticeDays: 0, paymentRef: "Invoice", licenseRef: null, status: "active" },
  { id: "sub-5", vendor: "ven-jetbrains", name: "JetBrains All Products Pack", plan: "Organization", category: "Engineering", cycle: "annual", cost: "9240.00", currency: "USD", seats: 12, owner: "usr-tk", startedDays: 130, renewsDays: 235, autoRenew: true, noticeDays: 30, paymentRef: "Card ending 4417", licenseRef: "lic-4", status: "active" },
  { id: "sub-6", vendor: null, name: "Notion Plus", plan: "Plus", category: "Collaboration", cycle: "monthly", cost: "96.00", currency: "USD", seats: 10, owner: "usr-ao", startedDays: 22, renewsDays: 8, autoRenew: false, noticeDays: 0, paymentRef: null, licenseRef: null, status: "trial" },
  { id: "sub-7", vendor: null, name: "Zoom Workplace", plan: "Business", category: "Collaboration", cycle: "monthly", cost: "279.00", currency: "EUR", seats: 20, owner: "usr-ic", startedDays: 800, renewsDays: null, autoRenew: false, noticeDays: 30, paymentRef: "Card ending 4417", licenseRef: null, status: "cancelled" },
];

function subscription(seed: SubscriptionSeed): SubscriptionOut {
  return {
    id: seed.id,
    vendor_id: seed.vendor,
    service_name: seed.name,
    plan: seed.plan,
    category: seed.category,
    billing_cycle: seed.cycle,
    cost_per_cycle: seed.cost,
    currency: seed.currency,
    seats: seed.seats,
    owner_user_id: seed.owner,
    started_at: dateIn(-seed.startedDays),
    renews_at: seed.renewsDays === null ? null : dateIn(seed.renewsDays),
    auto_renew: seed.autoRenew,
    notice_days: seed.noticeDays,
    payment_ref: seed.paymentRef,
    license_ref: seed.licenseRef,
    status: seed.status,
    created_at: ago(seed.startedDays),
  };
}

const subscriptions = (): SubscriptionOut[] => SEEDS.map(subscription);

/* ------------------------------------------------------------------- seats */

interface SeatSeed {
  id: string;
  subscription: string;
  name: string;
  email: string;
  assignedDays: number;
  releasedDays: number | null;
}

const SEAT_SEEDS: SeatSeed[] = [
  { id: "seat-1", subscription: "sub-1", name: "Leila Farah", email: "leila.farah@toeshee.example", assignedDays: 400, releasedDays: null },
  { id: "seat-2", subscription: "sub-1", name: "Marta Coelho", email: "marta.coelho@toeshee.example", assignedDays: 380, releasedDays: null },
  { id: "seat-3", subscription: "sub-1", name: "Adaeze Okonkwo", email: "adaeze.okonkwo@toeshee.example", assignedDays: 300, releasedDays: null },
  { id: "seat-4", subscription: "sub-1", name: "Tomas Kalu", email: "tomas.kalu@toeshee.example", assignedDays: 250, releasedDays: null },
  { id: "seat-5", subscription: "sub-1", name: "Ana Ribeiro", email: "ana.ribeiro@toeshee.example", assignedDays: 200, releasedDays: null },
  { id: "seat-6", subscription: "sub-1", name: "Sofia Martins", email: "sofia.martins@toeshee.example", assignedDays: 120, releasedDays: null },
  { id: "seat-7", subscription: "sub-1", name: "Rui Santos", email: "rui.santos@toeshee.example", assignedDays: 390, releasedDays: 31 },
  { id: "seat-8", subscription: "sub-2", name: "Mateus Ribeiro", email: "mateus.ribeiro@toeshee.example", assignedDays: 690, releasedDays: null },
  { id: "seat-9", subscription: "sub-2", name: "Tomas Kalu", email: "tomas.kalu@toeshee.example", assignedDays: 600, releasedDays: null },
  { id: "seat-10", subscription: "sub-2", name: "Sofia Martins", email: "sofia.martins@toeshee.example", assignedDays: 400, releasedDays: null },
  { id: "seat-11", subscription: "sub-5", name: "Tomas Kalu", email: "tomas.kalu@toeshee.example", assignedDays: 120, releasedDays: null },
  { id: "seat-12", subscription: "sub-5", name: "Sofia Martins", email: "sofia.martins@toeshee.example", assignedDays: 120, releasedDays: null },
];

function seat(seed: SeatSeed): SeatAssignmentOut {
  return {
    id: seed.id,
    subscription_id: seed.subscription,
    assignee_name: seed.name,
    assignee_email: seed.email,
    assigned_at: ago(seed.assignedDays),
    released_at: seed.releasedDays === null ? null : ago(seed.releasedDays),
  };
}

/* ------------------------------------------------------------------ events */

interface EventSeed {
  id: string;
  subscription: string;
  type: string;
  details: Record<string, unknown>;
  days: number;
}

const EVENT_SEEDS: EventSeed[] = [
  { id: "evt-1", subscription: "sub-1", type: "created", details: { seats: 8 }, days: 410 },
  { id: "evt-2", subscription: "sub-1", type: "renewed", details: { renews_at: dateIn(14 - 365) }, days: 351 },
  { id: "evt-3", subscription: "sub-1", type: "seat_assigned", details: { assignee: "Sofia Martins" }, days: 120 },
  { id: "evt-4", subscription: "sub-1", type: "seat_released", details: { assignee: "Rui Santos" }, days: 31 },
  { id: "evt-5", subscription: "sub-1", type: "updated", details: { seats: [10, 12] }, days: 30 },
  { id: "evt-6", subscription: "sub-2", type: "created", details: {}, days: 700 },
  { id: "evt-7", subscription: "sub-7", type: "created", details: {}, days: 800 },
  { id: "evt-8", subscription: "sub-7", type: "cancelled", details: { reason: "Consolidated on Teams" }, days: 60 },
];

function event(seed: EventSeed): EventOut {
  return { id: seed.id, subscription_id: seed.subscription, type: seed.type, details: seed.details, created_at: ago(seed.days) };
}

/* ------------------------------------------------------------------- money */

/** Whole cents of one billing cycle, normalised to a month. */
function monthlyCents(seed: SubscriptionSeed): number {
  const cents = Math.round(Number(seed.cost) * 100);
  const divisor = seed.cycle === "annual" ? 12 : seed.cycle === "quarterly" ? 3 : 1;
  return Math.round(cents / divisor);
}

const money = (cents: number): string => (cents / 100).toFixed(2);

/**
 * One roll-up: sum monthly cents per (group, currency). `null` group keys are
 * the by-currency top line; a vendor or owner the org cannot resolve is
 * grouped with neither a key nor an id, as reporting.py does.
 */
function rollup(
  groupType: string | null,
  keyOf: (seed: SubscriptionSeed) => { key: string | null; id: string | null },
): SpendRow[] {
  const buckets = new Map<string, SpendRow & { cents: number }>();
  for (const seed of SEEDS) {
    if (seed.status !== "active") continue;
    const { key, id } = keyOf(seed);
    const bucket = `${key ?? ""}|${id ?? ""}|${seed.currency}`;
    const prior = buckets.get(bucket);
    const cents = (prior?.cents ?? 0) + monthlyCents(seed);
    buckets.set(bucket, {
      currency: seed.currency,
      monthly_total: money(cents),
      annual_total: money(cents * 12),
      group_key: key,
      group_type: groupType,
      group_id: id,
      cents,
    });
  }
  return [...buckets.values()].map(({ cents: _cents, ...row }) => row);
}

const OWNER_NAMES: Record<string, string> = {
  "usr-lf": "Leila Farah",
  "usr-mr": "Mateus Ribeiro",
  "usr-tk": "Tomas Kalu",
  "usr-ao": "Adaeze Okonkwo",
  "usr-ic": "Ines Caetano",
};

const VENDOR_NAMES: Record<string, string> = {
  "ven-atlassian": "Atlassian",
  "ven-jetbrains": "JetBrains",
};

/* ------------------------------------------------------------------ reads */

const listSubscriptions: FixtureMap["listSubscriptions"] = () => subscriptions();

const findSubscription = (id: string): SubscriptionOut => {
  const found = subscriptions().find((s) => s.id === id);
  if (!found) throw notFound("subscription");
  return found;
};

const getSubscription: FixtureMap["getSubscription"] = findSubscription;

const listSeats: FixtureMap["listSeats"] = (subscriptionId) => {
  findSubscription(subscriptionId);
  return SEAT_SEEDS.filter((s) => s.subscription === subscriptionId).map(seat);
};

const listEvents: FixtureMap["listEvents"] = (subscriptionId) => {
  findSubscription(subscriptionId);
  return EVENT_SEEDS.filter((e) => e.subscription === subscriptionId)
    .map(event)
    .sort((a, b) => (a.created_at < b.created_at ? 1 : -1));
};

const spend: FixtureMap["spend"] = () => ({
  by_currency: rollup(null, () => ({ key: null, id: null })),
  by_vendor: rollup("vendor", (seed) =>
    seed.vendor && VENDOR_NAMES[seed.vendor] ? { key: VENDOR_NAMES[seed.vendor] ?? null, id: seed.vendor } : { key: null, id: null },
  ),
  by_category: rollup("category", (seed) => ({ key: seed.category, id: null })),
  by_owner: rollup("owner", (seed) =>
    seed.owner && OWNER_NAMES[seed.owner] ? { key: OWNER_NAMES[seed.owner] ?? null, id: seed.owner } : { key: null, id: null },
  ),
});

const renewals: FixtureMap["renewals"] = (withinDays = 30) => {
  const rows: RenewalRow[] = [];
  for (const seed of SEEDS) {
    if (seed.status === "cancelled" || seed.renewsDays === null) continue;
    const daysToNotice = seed.renewsDays - seed.noticeDays;
    // In the window when either the renewal or its notice deadline is.
    if (seed.renewsDays > withinDays && daysToNotice > withinDays) continue;
    rows.push({
      subscription_id: seed.id,
      service_name: seed.name,
      renews_at: dateIn(seed.renewsDays),
      notice_deadline: dateIn(daysToNotice),
      days_to_notice: daysToNotice,
      days_to_renewal: seed.renewsDays,
      action_required: seed.autoRenew && daysToNotice <= 0,
    });
  }
  return rows.sort((a, b) => (a.days_to_renewal ?? 0) - (b.days_to_renewal ?? 0));
};

const idleSeats: FixtureMap["idleSeats"] = () =>
  SEEDS.filter((seed) => seed.status === "active").map((seed): IdleSeatRow => {
    const assigned = SEAT_SEEDS.filter((s) => s.subscription === seed.id && s.releasedDays === null).length;
    const idle = Math.max(0, seed.seats - assigned);
    const perSeat = seed.seats > 0 ? monthlyCents(seed) / seed.seats : 0;
    return {
      subscription_id: seed.id,
      service_name: seed.name,
      seats: seed.seats,
      assigned,
      idle,
      currency: seed.currency,
      idle_monthly_cost: money(Math.round(perSeat * idle)),
    };
  });

/* ----------------------------------------------------------------- writes */

const createSubscription: FixtureMap["createSubscription"] = (body) => ({
  id: `sub-${Date.now()}`,
  vendor_id: body.vendor_id ?? null,
  service_name: body.service_name,
  plan: body.plan ?? "",
  category: body.category ?? "",
  billing_cycle: body.billing_cycle,
  cost_per_cycle: body.cost_per_cycle,
  currency: body.currency,
  seats: body.seats ?? 0,
  owner_user_id: body.owner_user_id ?? null,
  started_at: body.started_at ?? null,
  renews_at: body.renews_at ?? null,
  auto_renew: body.auto_renew ?? false,
  notice_days: body.notice_days ?? 0,
  payment_ref: body.payment_ref ?? null,
  license_ref: body.license_ref ?? null,
  status: body.status ?? "active",
  created_at: new Date().toISOString(),
});

const updateSubscription: FixtureMap["updateSubscription"] = (id, body) => {
  const found = findSubscription(id);
  const patch: Partial<SubscriptionOut> = {};
  for (const [key, value] of Object.entries(body)) {
    if (value !== undefined) (patch as Record<string, unknown>)[key] = value;
  }
  return { ...found, ...patch };
};

const cancelSubscription: FixtureMap["cancelSubscription"] = (id) => ({ ...findSubscription(id), status: "cancelled" });

const renewSubscription: FixtureMap["renewSubscription"] = (id) => {
  const found = findSubscription(id);
  const seed = SEEDS.find((s) => s.id === id);
  const step = seed?.cycle === "annual" ? 365 : seed?.cycle === "quarterly" ? 91 : 30;
  return { ...found, renews_at: dateIn((seed?.renewsDays ?? 0) + step) };
};

const assignSeat: FixtureMap["assignSeat"] = (subscriptionId, body) => {
  findSubscription(subscriptionId);
  return {
    id: `seat-${Date.now()}`,
    subscription_id: subscriptionId,
    assignee_name: body.assignee_name,
    assignee_email: body.assignee_email,
    assigned_at: new Date().toISOString(),
    released_at: null,
  };
};

const releaseSeat: FixtureMap["releaseSeat"] = (seatId) => {
  const found = SEAT_SEEDS.find((s) => s.id === seatId);
  if (!found) throw notFound("seat");
  return { ...seat(found), released_at: new Date().toISOString() };
};

const fixtures: FixtureMap = {
  listSubscriptions,
  getSubscription,
  createSubscription,
  updateSubscription,
  cancelSubscription,
  renewSubscription,
  listSeats,
  listEvents,
  assignSeat,
  releaseSeat,
  spend,
  renewals,
  idleSeats,
};

export default fixtures;
