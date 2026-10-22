// Mirrors backend/app/modules/licenses/schema.py request/response models
// exactly (snake_case keys, matching the JSON). Per security invariant #3 and
// spec §5: `License`/`LicenseOut` never expose the license key or its sealed
// form — only `has_key: boolean`. The plaintext key only ever appears in
// `LicenseKeyOut`, the response shape of the dedicated, audited
// `GET /licenses/{id}/key` reveal endpoint.
//
// The type/compliance/renewal vocabularies are sourced from the backend, not
// invented — see licenseState.ts for the exact citations backing each
// vocabulary.

export interface MatchRule {
  type: "exact" | "prefix" | "regex";
  pattern: string;
  publisher: string | null;
}

export interface VendorOut {
  id: string;
  name: string;
  website: string | null;
  contacts: Record<string, unknown>[];
  created_at: string;
}

export interface VendorCreate {
  name: string;
  website?: string | null;
  contacts?: Record<string, unknown>[];
}

export interface VendorUpdate {
  name?: string | null;
  website?: string | null;
  contacts?: Record<string, unknown>[] | null;
}

export interface ProductOut {
  id: string;
  name: string;
  publisher: string;
  match_rules: MatchRule[];
  created_at: string;
}

export interface ProductCreate {
  name: string;
  publisher?: string;
  match_rules?: MatchRule[];
}

export interface ProductUpdate {
  name?: string | null;
  publisher?: string | null;
  match_rules?: MatchRule[] | null;
}

// License lifecycle type -- schema.py's `LicenseType` Literal, shared by
// LicenseCreate/LicenseUpdate so the accepted domain can never drift between
// the two. The only values the backend ever accepts on write.
export type LicenseType = "perpetual" | "volume" | "oem" | "cal" | "per_device" | "per_user";

export interface LicenseCreate {
  product_id: string;
  vendor_id?: string | null;
  type: LicenseType;
  entitlements: number;
  key?: string | null;
  cost?: string | null;
  currency?: string | null;
  po_ref?: string | null;
  purchased_at?: string | null;
  maintenance_expires_at?: string | null;
  renews_at?: string | null;
  agreement_ref?: string | null;
  notes?: string;
}

export interface LicenseUpdate {
  product_id?: string | null;
  vendor_id?: string | null;
  type?: LicenseType | null;
  entitlements?: number | null;
  key?: string | null;
  cost?: string | null;
  currency?: string | null;
  po_ref?: string | null;
  purchased_at?: string | null;
  maintenance_expires_at?: string | null;
  renews_at?: string | null;
  agreement_ref?: string | null;
  notes?: string | null;
}

export interface LicenseOut {
  // No `key` / `key_sealed` field here -- spec §5: list/detail responses
  // carry `has_key` only. Reveal is the separate, audited
  // `GET /licenses/{id}/key` endpoint, returning `LicenseKeyOut`.
  id: string;
  product_id: string;
  vendor_id: string | null;
  // `str`, not `LicenseType`, in schema.py's LicenseOut -- the backend does
  // not re-narrow the type on the way out.
  type: string;
  entitlements: number;
  has_key: boolean;
  // Decimal serializes as a string over the wire.
  cost: string | null;
  currency: string | null;
  po_ref: string | null;
  purchased_at: string | null;
  maintenance_expires_at: string | null;
  renews_at: string | null;
  agreement_ref: string | null;
  notes: string;
  created_at: string;
}

export interface LicenseKeyOut {
  key: string;
}

export interface AllocationCreate {
  target_type: "device" | "user" | "person";
  target_id: string;
}

export interface AllocationOut {
  id: string;
  license_id: string;
  target_type: string;
  target_id: string;
  /** The person holding it; null for a device. */
  person_id?: string | null;
  allocated_at: string;
  released_at: string | null;
}

export interface ReviewQueueRow {
  name: string;
  publisher: string;
  device_count: number;
}

export interface ReviewMapRequest {
  name: string;
  product_id: string;
}

export interface ReviewIgnoreRequest {
  name: string;
}

export interface ComplianceRow {
  product_id: string;
  name: string;
  entitled: number;
  installed: number;
  delta: number;
  breach: boolean;
}

export interface RenewalRow {
  license_id: string;
  product_id: string;
  trigger: "renewal" | "maintenance";
  due_date: string;
  due_in_days: number;
}

export interface CostRow {
  product_id: string;
  currency: string;
  // Decimal serializes as a string over the wire.
  total_cost: string;
}
