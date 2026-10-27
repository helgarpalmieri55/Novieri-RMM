// Mirrors backend/app/modules/subscriptions/schema.py request/response models
// exactly (snake_case keys, matching the JSON). Per invariant #6 (money is
// Decimal server-side): `cost_per_cycle`, `monthly_total`, `annual_total`,
// and `idle_monthly_cost` are `Decimal` in schema.py and serialize as JSON
// strings over the wire -- typed `string` here, never `number`, and never
// used for arithmetic in the UI (format for display only).
//
// The status/billing-cycle vocabularies are sourced from the backend, not
// invented -- see subscriptionState.ts for the exact citations backing each
// vocabulary.

// schema.py: BillingCycle = Literal["monthly", "quarterly", "annual"] --
// shared by SubscriptionCreate/SubscriptionUpdate so the accepted domain can
// never drift between the two.
export type BillingCycle = "monthly" | "quarterly" | "annual";

// schema.py: SubStatus = Literal["active", "trial", "cancelled"].
export type SubStatus = "active" | "trial" | "cancelled";

export interface SubscriptionCreate {
  /** The client the service is paid for; it never changes (MSP clients phase 2). */
  client_id: string;
  vendor_id?: string | null;
  service_name: string;
  plan?: string;
  category?: string;
  billing_cycle: BillingCycle;
  // Decimal on the wire; the request body sends a numeric-looking string,
  // matching how the backend accepts pydantic Decimal fields as JSON.
  cost_per_cycle: string;
  currency: string;
  seats?: number;
  owner_user_id?: string | null;
  started_at?: string | null;
  renews_at?: string | null;
  auto_renew?: boolean;
  notice_days?: number;
  payment_ref?: string | null;
  license_ref?: string | null;
  status?: SubStatus;
}

export interface SubscriptionUpdate {
  vendor_id?: string | null;
  service_name?: string | null;
  plan?: string | null;
  category?: string | null;
  billing_cycle?: BillingCycle | null;
  cost_per_cycle?: string | null;
  currency?: string | null;
  seats?: number | null;
  owner_user_id?: string | null;
  started_at?: string | null;
  renews_at?: string | null;
  auto_renew?: boolean | null;
  notice_days?: number | null;
  payment_ref?: string | null;
  license_ref?: string | null;
  status?: SubStatus | null;
}

export interface SubscriptionOut {
  // No `org_id` field here -- security invariant #5: response models never
  // leak the tenant id, mirroring schema.py's SubscriptionOut.
  id: string;
  client_id: string;
  vendor_id: string | null;
  service_name: string;
  plan: string;
  category: string;
  // `str`, not `BillingCycle`/`SubStatus`, in schema.py's SubscriptionOut --
  // the backend does not re-narrow the type on the way out.
  billing_cycle: string;
  // Decimal serializes as a string over the wire.
  cost_per_cycle: string;
  currency: string;
  seats: number;
  owner_user_id: string | null;
  started_at: string | null;
  renews_at: string | null;
  auto_renew: boolean;
  notice_days: number;
  payment_ref: string | null;
  license_ref: string | null;
  status: string;
  created_at: string;
}

export interface SeatAssignCreate {
  assignee_name: string;
  assignee_email: string;
  /** Optional: without it the server links whoever holds this address. */
  person_id?: string | null;
}

export interface SeatAssignmentOut {
  id: string;
  subscription_id: string;
  assignee_name: string;
  assignee_email: string;
  /** The person this seat belongs to, when one is linked. */
  person_id?: string | null;
  assigned_at: string;
  released_at: string | null;
}

// The types an entry in a subscription's event history takes: the ones the
// service records (service.py's `_record_event`) and the ones the history
// list draws a glyph for. `EventOut.type` stays a string -- the server may add
// more -- and an entry of a type not listed here reads through the shared
// derivation.
export type SubscriptionEventType =
  | "created"
  | "renewal"
  | "renewed"
  | "cancel"
  | "cancelled"
  | "price_change"
  | "seat_change"
  | "seat_assigned"
  | "seat_released"
  | "plan_change"
  | "updated";

export interface EventOut {
  id: string;
  subscription_id: string;
  type: string;
  details: Record<string, unknown>;
  created_at: string;
}

// One flexible shape covers both the top-level per-currency totals and the
// vendor/category/owner roll-ups -- see reporting.py's spend_summary
// docstring: `group_key`/`group_type`/`group_id` are all `null` on the
// top-level by_currency rows. On a roll-up row `group_key` and `group_id`
// are still both nullable: `by_category` never has an id, and `by_vendor` /
// `by_owner` suppress BOTH fields together when the referenced entity is
// not resolvable in the org (an ungrouped row, or a deleted/cross-org id --
// emitting the raw id alone would leak it). Render neither unguarded.
//
// Every row is ONE client's (MSP clients phase 2): spend is never summed
// across clients any more than across currencies.
export interface SpendRow {
  client_id: string;
  currency: string;
  // Decimal serializes as a string over the wire.
  monthly_total: string;
  annual_total: string;
  group_key: string | null;
  group_type: string | null;
  group_id: string | null;
}

// GET /subscriptions/spend's response shape (api.py: `dict[str,
// list[SpendRow]]`) -- the four roll-ups reporting.py's spend_summary
// returns.
export interface SpendSummary {
  by_currency: SpendRow[];
  by_vendor: SpendRow[];
  by_category: SpendRow[];
  by_owner: SpendRow[];
}

export interface RenewalRow {
  subscription_id: string;
  service_name: string;
  renews_at: string | null;
  notice_deadline: string | null;
  days_to_notice: number | null;
  days_to_renewal: number | null;
  action_required: boolean;
}

export interface IdleSeatRow {
  subscription_id: string;
  service_name: string;
  seats: number;
  assigned: number;
  idle: number;
  currency: string;
  // Decimal serializes as a string over the wire.
  idle_monthly_cost: string;
}
