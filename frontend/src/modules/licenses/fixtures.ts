/**
 * Fixtures for the licenses module: the vendor record, the product catalog
 * with its match rules, the licenses bought against it, the compliance
 * reconciliation, the review queue and the renewal calendar.
 *
 * ONE ESTATE, EVERY NUMBER DERIVED FROM IT. `licenseCompliance` sums the
 * entitlements of every license per product and sets each product's
 * installed count from one table, so the "over-deployed" tile on `/licenses`,
 * the rows on `/licenses/compliance` and the per-license reading on
 * `/licenses/:id` cannot disagree. `licenseRenewals` reads the same licenses'
 * dates, and `licenseCosts` their costs, per currency, never summed across
 * currencies (the module docstring's own rule).
 *
 * Every license is the internal client's (`ORG_ID`, the id convention): the
 * console files each one there until phase 3's client selector, so every
 * per-client report here has that one client.
 *
 * The device allocations name machines from the shell's worked fleet
 * (`fixtures/core.ts`), so the seat on `lis-ws-041` here is the same
 * workstation `/devices` lists.
 *
 * DEV-ONLY BY CONSTRUCTION: reached through `fixtures/client.ts` only, which
 * nothing imports outside `FIXTURE_MODE`.
 */

import type { FixtureMap } from "../../fixtures/client";
import { ORG_ID } from "../../fixtures/core";
import { ApiError } from "../../lib/api";
import type {
  AllocationOut,
  ComplianceRow,
  CostRow,
  LicenseOut,
  ProductOut,
  RenewalRow,
  ReviewQueueRow,
  VendorOut,
} from "./types";

const MINUTE = 60_000;
const DAY = 24 * 60 * MINUTE;

/** An ISO timestamp `days` before the call. */
const ago = (days: number): string => new Date(Date.now() - days * DAY).toISOString();
/** A calendar date (no time, no zone) `days` from today. */
const dateIn = (days: number): string => new Date(Date.now() + days * DAY).toISOString().slice(0, 10);

const notFound = (what: string) => new ApiError(404, JSON.stringify({ detail: `${what} not found` }));

/* ----------------------------------------------------------------- vendors */

const VENDORS: VendorOut[] = [
  {
    id: "ven-microsoft",
    name: "Microsoft",
    website: "https://www.microsoft.com",
    contacts: [{ name: "Rui Pinto", role: "account manager", email: "rui.pinto@microsoft.example" }],
    created_at: ago(410),
  },
  {
    id: "ven-adobe",
    name: "Adobe",
    website: "https://www.adobe.com",
    contacts: [],
    created_at: ago(380),
  },
  {
    id: "ven-jetbrains",
    name: "JetBrains",
    website: "https://www.jetbrains.com",
    contacts: [{ name: "Sales desk", email: "sales@jetbrains.example" }],
    created_at: ago(200),
  },
  {
    id: "ven-atlassian",
    name: "Atlassian",
    website: null,
    contacts: [],
    created_at: ago(95),
  },
];

/* ---------------------------------------------------------------- products */

const PRODUCTS: ProductOut[] = [
  {
    id: "prod-acrobat",
    name: "Adobe Acrobat Pro",
    publisher: "Adobe",
    match_rules: [
      { type: "prefix", pattern: "Adobe Acrobat", publisher: "Adobe" },
      { type: "regex", pattern: "^Acrobat (DC|Pro) 20\\d\\d", publisher: null },
    ],
    created_at: ago(380),
  },
  {
    id: "prod-m365",
    name: "Microsoft 365 E3",
    publisher: "Microsoft",
    match_rules: [{ type: "prefix", pattern: "Microsoft 365 Apps", publisher: "Microsoft Corporation" }],
    created_at: ago(410),
  },
  {
    id: "prod-visio",
    name: "Microsoft Visio",
    publisher: "Microsoft",
    match_rules: [{ type: "prefix", pattern: "Microsoft Visio", publisher: "Microsoft Corporation" }],
    created_at: ago(300),
  },
  {
    id: "prod-idea",
    name: "JetBrains IntelliJ IDEA",
    publisher: "JetBrains",
    match_rules: [{ type: "prefix", pattern: "IntelliJ IDEA", publisher: "JetBrains s.r.o." }],
    created_at: ago(200),
  },
  {
    id: "prod-server-cal",
    name: "Windows Server 2022 user CAL",
    publisher: "Microsoft",
    match_rules: [],
    created_at: ago(410),
  },
];

/* ---------------------------------------------------------------- licenses */

interface LicenseSeed {
  id: string;
  product: string;
  vendor: string | null;
  type: LicenseOut["type"];
  entitlements: number;
  hasKey: boolean;
  cost: string | null;
  currency: string | null;
  po: string | null;
  purchasedDays: number;
  /** Days from today; null when the license carries no maintenance term. */
  maintenanceDays: number | null;
  /** Days from today; null for a license that never renews. */
  renewsDays: number | null;
  agreement: string | null;
  notes: string;
}

const LICENSE_SEEDS: LicenseSeed[] = [
  { id: "lic-1", product: "prod-acrobat", vendor: "ven-adobe", type: "per_user", entitlements: 50, hasKey: true, cost: "7200.00", currency: "EUR", po: "PO-2188", purchasedDays: 310, maintenanceDays: null, renewsDays: 55, agreement: "VIP-88213", notes: "Adobe VIP agreement; seats are named users, not machines." },
  { id: "lic-2", product: "prod-m365", vendor: "ven-microsoft", type: "per_user", entitlements: 280, hasKey: false, cost: "60480.00", currency: "EUR", po: "PO-2107", purchasedDays: 249, maintenanceDays: null, renewsDays: 116, agreement: "EA-71-402118", notes: "" },
  { id: "lic-3", product: "prod-visio", vendor: "ven-microsoft", type: "per_device", entitlements: 15, hasKey: true, cost: "8100.00", currency: "EUR", po: "PO-1960", purchasedDays: 400, maintenanceDays: 20, renewsDays: null, agreement: "EA-71-402118", notes: "Software Assurance covers the installed base; maintenance is what expires." },
  { id: "lic-4", product: "prod-idea", vendor: "ven-jetbrains", type: "perpetual", entitlements: 12, hasKey: true, cost: "5988.00", currency: "USD", po: "PO-2231", purchasedDays: 130, maintenanceDays: 235, renewsDays: null, agreement: null, notes: "Perpetual fallback licence after the first year of the subscription." },
  { id: "lic-5", product: "prod-server-cal", vendor: "ven-microsoft", type: "cal", entitlements: 300, hasKey: false, cost: "9600.00", currency: "EUR", po: "PO-1811", purchasedDays: 620, maintenanceDays: null, renewsDays: null, agreement: "EA-71-402118", notes: "" },
];

function license(seed: LicenseSeed): LicenseOut {
  return {
    id: seed.id,
    client_id: ORG_ID,
    product_id: seed.product,
    vendor_id: seed.vendor,
    type: seed.type,
    entitlements: seed.entitlements,
    has_key: seed.hasKey,
    cost: seed.cost,
    currency: seed.currency,
    po_ref: seed.po,
    purchased_at: dateIn(-seed.purchasedDays),
    maintenance_expires_at: seed.maintenanceDays === null ? null : dateIn(seed.maintenanceDays),
    renews_at: seed.renewsDays === null ? null : dateIn(seed.renewsDays),
    agreement_ref: seed.agreement,
    notes: seed.notes,
    created_at: ago(seed.purchasedDays),
  };
}

const licenses = (): LicenseOut[] => LICENSE_SEEDS.map(license);

/* ------------------------------------------------------------- allocations */

interface AllocationSeed {
  id: string;
  license: string;
  targetType: "device" | "user";
  target: string;
  allocatedDays: number;
  releasedDays: number | null;
}

const ALLOCATION_SEEDS: AllocationSeed[] = [
  { id: "alloc-1", license: "lic-1", targetType: "user", target: "usr-ao", allocatedDays: 300, releasedDays: null },
  { id: "alloc-2", license: "lic-1", targetType: "user", target: "usr-lf", allocatedDays: 280, releasedDays: null },
  { id: "alloc-3", license: "lic-1", targetType: "user", target: "usr-mr", allocatedDays: 260, releasedDays: 40 },
  { id: "alloc-4", license: "lic-3", targetType: "device", target: "dev-1", allocatedDays: 390, releasedDays: null },
  { id: "alloc-5", license: "lic-3", targetType: "device", target: "dev-2", allocatedDays: 390, releasedDays: null },
  { id: "alloc-6", license: "lic-3", targetType: "device", target: "dev-4", allocatedDays: 200, releasedDays: 12 },
  { id: "alloc-7", license: "lic-4", targetType: "user", target: "usr-tk", allocatedDays: 120, releasedDays: null },
];

function allocation(seed: AllocationSeed): AllocationOut {
  return {
    id: seed.id,
    license_id: seed.license,
    target_type: seed.targetType,
    target_id: seed.target,
    allocated_at: ago(seed.allocatedDays),
    released_at: seed.releasedDays === null ? null : ago(seed.releasedDays),
  };
}

/* -------------------------------------------------------------- inventory */

/**
 * What inventory found installed, per product: the one number this module
 * does not own. Acrobat and Visio are over their entitlement, the way the
 * design's own mock has them.
 */
const INSTALLED: Record<string, number> = {
  "prod-acrobat": 58,
  "prod-m365": 241,
  "prod-visio": 19,
  "prod-idea": 9,
  "prod-server-cal": 0,
};

const REVIEW_QUEUE: ReviewQueueRow[] = [
  { name: "7-Zip 24.08 (x64 edition)", publisher: "Igor Pavlov", device_count: 118 },
  { name: "Notepad++ (64-bit x64)", publisher: "Notepad++ Team", device_count: 63 },
  { name: "Zoom Workplace", publisher: "Zoom Communications, Inc.", device_count: 41 },
  { name: "Foxit PDF Editor", publisher: "Foxit Software Inc.", device_count: 4 },
  { name: "WinRAR 7.01", publisher: "", device_count: 2 },
];

/* ----------------------------------------------------------------- reads */

const listVendors: FixtureMap["listVendors"] = () => VENDORS;

const findVendor = (id: string): VendorOut => {
  const found = VENDORS.find((v) => v.id === id);
  if (!found) throw notFound("vendor");
  return found;
};

const findProduct = (id: string): ProductOut => {
  const found = PRODUCTS.find((p) => p.id === id);
  if (!found) throw notFound("product");
  return found;
};

const findLicense = (id: string): LicenseOut => {
  const found = licenses().find((l) => l.id === id);
  if (!found) throw notFound("license");
  return found;
};

const getVendor: FixtureMap["getVendor"] = findVendor;

const listProducts: FixtureMap["listProducts"] = () => PRODUCTS;

const getProduct: FixtureMap["getProduct"] = findProduct;

const listLicenses: FixtureMap["listLicenses"] = () => licenses();

const getLicense: FixtureMap["getLicense"] = findLicense;

const listAllocations: FixtureMap["listAllocations"] = (licenseId) => {
  findLicense(licenseId);
  return ALLOCATION_SEEDS.filter((a) => a.license === licenseId).map(allocation);
};

const licenseCompliance: FixtureMap["licenseCompliance"] = () =>
  PRODUCTS.map((product): ComplianceRow => {
    const entitled = LICENSE_SEEDS.filter((l) => l.product === product.id).reduce(
      (sum, l) => sum + l.entitlements,
      0,
    );
    const installed = INSTALLED[product.id] ?? 0;
    return {
      client_id: ORG_ID,
      product_id: product.id,
      name: product.name,
      entitled,
      installed,
      delta: installed - entitled,
      breach: installed > entitled,
    };
  });

const reviewQueue: FixtureMap["reviewQueue"] = () => REVIEW_QUEUE;

const licenseRenewals: FixtureMap["licenseRenewals"] = (withinDays = 90) => {
  const rows: RenewalRow[] = [];
  for (const seed of LICENSE_SEEDS) {
    if (seed.renewsDays !== null && seed.renewsDays <= withinDays) {
      rows.push({ license_id: seed.id, client_id: ORG_ID, product_id: seed.product, trigger: "renewal", due_date: dateIn(seed.renewsDays), due_in_days: seed.renewsDays });
    }
    if (seed.maintenanceDays !== null && seed.maintenanceDays <= withinDays) {
      rows.push({ license_id: seed.id, client_id: ORG_ID, product_id: seed.product, trigger: "maintenance", due_date: dateIn(seed.maintenanceDays), due_in_days: seed.maintenanceDays });
    }
  }
  return rows.sort((a, b) => a.due_in_days - b.due_in_days);
};

const licenseCosts: FixtureMap["licenseCosts"] = () => {
  const rows = new Map<string, CostRow>();
  for (const seed of LICENSE_SEEDS) {
    if (seed.cost === null || seed.currency === null) continue;
    const key = `${seed.product}:${seed.currency}`;
    const prior = rows.get(key);
    // Whole cents, so the string never grows a float tail.
    const cents = Math.round(Number(seed.cost) * 100) + (prior ? Math.round(Number(prior.total_cost) * 100) : 0);
    rows.set(key, { client_id: ORG_ID, product_id: seed.product, currency: seed.currency, total_cost: (cents / 100).toFixed(2) });
  }
  return [...rows.values()];
};

/* ---------------------------------------------------------------- writes */

const revealLicenseKey: FixtureMap["revealLicenseKey"] = (id) => {
  const found = findLicense(id);
  if (!found.has_key) throw notFound("license key");
  return { key: `${id.toUpperCase()}-7Q2K-M8NV-3XZR-PL0D` };
};

const createVendor: FixtureMap["createVendor"] = (body) => ({
  id: `ven-${Date.now()}`,
  name: body.name,
  website: body.website ?? null,
  contacts: body.contacts ?? [],
  created_at: new Date().toISOString(),
});

const createProduct: FixtureMap["createProduct"] = (body) => ({
  id: `prod-${Date.now()}`,
  name: body.name,
  publisher: body.publisher ?? "",
  match_rules: body.match_rules ?? [],
  created_at: new Date().toISOString(),
});

const updateProduct: FixtureMap["updateProduct"] = (id, body) => {
  const found = findProduct(id);
  return {
    ...found,
    name: body.name ?? found.name,
    publisher: body.publisher ?? found.publisher,
    match_rules: body.match_rules ?? found.match_rules,
  };
};

const createLicense: FixtureMap["createLicense"] = (body) => ({
  id: `lic-${Date.now()}`,
  client_id: body.client_id,
  product_id: body.product_id,
  vendor_id: body.vendor_id ?? null,
  type: body.type,
  entitlements: body.entitlements,
  has_key: Boolean(body.key),
  cost: body.cost ?? null,
  currency: body.currency ?? null,
  po_ref: body.po_ref ?? null,
  purchased_at: body.purchased_at ?? null,
  maintenance_expires_at: body.maintenance_expires_at ?? null,
  renews_at: body.renews_at ?? null,
  agreement_ref: body.agreement_ref ?? null,
  notes: body.notes ?? "",
  created_at: new Date().toISOString(),
});

const deleteLicense: FixtureMap["deleteLicense"] = (id) => {
  findLicense(id);
  return undefined;
};

const mapSoftware: FixtureMap["mapSoftware"] = (body) => findProduct(body.product_id);

const ignoreSoftware: FixtureMap["ignoreSoftware"] = (body) => ({
  id: `ign-${Date.now()}`,
  name: body.name,
  client_id: body.client_id ?? null,
});

const fixtures: FixtureMap = {
  listVendors,
  getVendor,
  createVendor,
  listProducts,
  getProduct,
  createProduct,
  updateProduct,
  listLicenses,
  getLicense,
  createLicense,
  deleteLicense,
  revealLicenseKey,
  listAllocations,
  licenseCompliance,
  reviewQueue,
  mapSoftware,
  ignoreSoftware,
  licenseRenewals,
  licenseCosts,
};

export default fixtures;
