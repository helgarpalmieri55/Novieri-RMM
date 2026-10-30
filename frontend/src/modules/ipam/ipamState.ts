// Pure presentation logic for the IPAM module. Everything here is a DISPLAY
// decision over values the server already decided. Nothing here decides whether
// an address is in use, whether a finding is drift, or whether a prefix may be
// probed — those are `allocation.derive_status`, `reconcile` and
// `authorization.authorized_targets`, server-side, and this module never
// re-derives them.
//
// THE SEVEN RULES THIS FILE EXISTS TO KEEP. Each is a distinction a plausible
// renderer collapses, and each collapse turns this module into the thing it was
// explicitly designed not to be:
//
//   1. AGE, NEVER A TIMESTAMP (§0.10, §7). "Last seen" is documentation
//      currency — the age of the most recent evidence-producing event — and a
//      precise timestamp in a cell invites reading it as liveness. There is no
//      up/down field, no transition event, no latency and no history anywhere
//      in this contract; a UI that prints "09:42:07" is the one place the
//      monitoring boundary could still be crossed. `formatAge` is the only
//      renderer of every instant this module shows.
//   2. `dormant` AND ORPHANS SIT OUTSIDE THE DRIFT COUNT (§0.11, §4.2, §4.3).
//      A dormant address is a switched-off laptop; an orphan is an absent plan,
//      not a wrong one. Counting either as drift is exactly how this module
//      becomes the noise nobody reads. `driftCount` is the one place the
//      headline number is computed.
//   3. `state` IS DOCUMENTED INTENT; `status` IS DERIVED (§0.6). Different
//      columns, different meanings, two separate maps below — a UI that showed
//      one under the other's heading makes an operator think the plan changed
//      when only reality did.
//   4. ABOVE THE ENUMERATION CEILING, UTILISATION IS UNAVAILABLE — NOT 0%
//      (§0.4, §0.5). `percentage: null` with real counts is the honest answer;
//      a coerced zero reads as "empty".
//   5. A CONFLICT IS NOT A MISMATCH (§4.3). Distinct classes, distinct
//      evidence, distinct remedies. `DRIFT_CLASS_PRESENTATION` states each
//      remedy so the two never share a button.
//   6. SCAN AUTHORIZATION IS DENY-BY-DEFAULT AND SECURITY-CRITICAL (§0.12).
//      `describeScope` says what a row actually permits, in addresses, and
//      `effectiveSweepScope` shows the intersection-with-the-subnet-FIRST rule
//      that stops an over-broad grant from widening anything. Both are
//      previews of a decision the server makes again; neither is authority.
//   7. IMPORT IS TWO-STEP, BOUND BY A DIGEST (§8.1). `importApplyBlockReason`
//      is the one predicate deciding whether apply may be offered, and it
//      refuses a file that was never previewed.
//
// `__tests__/ipamState.test.ts` asserts the distinctions rather than the
// wording: every case there is two inputs that must produce different output.

import type { BadgeVariant } from "../jobs/jobState";
import type {
  AddressState,
  AddressStatus,
  DriftClass,
  DriftOut,
  GridEntryOut,
  HistoryEntryOut,
  ImportRowOut,
  RangeOut,
  ScopeOut,
  SubnetOut,
  SweepRunOut,
  UtilizationOut,
} from "./types";
import { DORMANT_CLASS, DRIFT_CLASSES } from "./types";
import { getLanguage, translate, type MessageKey } from "../../i18n/core";
import { lazyFields } from "../../i18n/lazy";
import { intlLocale } from "../../lib/datetime";

// --------------------------------------------------------------------------
// Rule 1 — age, never a timestamp
// --------------------------------------------------------------------------

const MINUTE_MS = 60_000;
const HOUR_MS = 60 * MINUTE_MS;
const DAY_MS = 24 * HOUR_MS;

/**
 * The ONLY renderer of an instant in this module.
 *
 * Returns a coarse age — "3 days ago", "5 hours ago" — and never a clock time
 * or a date. §0.10 is a schema-level argument that "last seen" answers *how
 * stale is this documentation*, not *is this host up*; the moment the grid
 * prints `2026-03-01 09:42:07` an operator reads the second question out of the
 * first, and every structural property that keeps this module out of monitoring
 * (no probe endpoint, no up/down column, no transition event, no history, no
 * RTT, a ≥1 h sweep clamp) is defeated by one cell.
 *
 * Deliberately coarse for the same reason: a resolution of minutes below an
 * hour and of hours below a day is as much precision as documentation currency
 * can carry, and more precision would only look like telemetry.
 *
 * `null` in, `"never"` out — an address nothing has ever reported is not
 * "0 minutes ago".
 */
export function formatAge(instant: string | null | undefined, now: number = Date.now()): string {
  if (instant == null || instant === "") return translate("ipam.age.never");
  const then = Date.parse(instant);
  if (!Number.isFinite(then)) return translate("ipam.age.unknown");
  const elapsed = now - then;
  if (elapsed < 0) return translate("ipam.age.justNow");
  if (elapsed < MINUTE_MS) return translate("ipam.age.justNow");
  if (elapsed < HOUR_MS) return translate("ipam.age.minutes", { count: Math.floor(elapsed / MINUTE_MS) });
  if (elapsed < DAY_MS) return translate("ipam.age.hours", { count: Math.floor(elapsed / HOUR_MS) });
  return translate("ipam.age.days", { count: Math.floor(elapsed / DAY_MS) });
}

/**
 * The OTHER direction of the same clock: how long until an instant that has
 * not happened yet.
 *
 * `formatAge` answers "how stale is this documentation" and clamps anything in
 * the future to "just now", which is the right answer for a last-seen stamp
 * (a clock skew of a few seconds is not news) and the WRONG one for a
 * deadline: an authorization that lapses in thirty days was drawn as though it
 * had just lapsed, on the one page in this module that decides where the
 * platform emits packets. Deadlines get their own renderer rather than a
 * second meaning bolted onto the first.
 *
 * Same coarse resolution as `formatAge`, and the same refusal to print a clock
 * time: "in 30 days", "in 5 hours", "in under a minute".
 *
 * ROUNDED, WHERE `formatAge` FLOORS, and the asymmetry is deliberate: an age
 * counts up from an instant that has passed, so flooring names the elapsed
 * unit the observer has actually seen. A deadline counts down towards one that
 * has not, and flooring it renames a grant written for thirty days "in 29
 * days" one millisecond after it is signed. Each unit is clamped so rounding
 * cannot spill into the next one ("in 60 minutes").
 */
export function formatUntil(instant: string, now: number = Date.now()): string {
  const then = Date.parse(instant);
  if (!Number.isFinite(then)) return translate("ipam.age.unknown");
  const remaining = then - now;
  if (remaining <= 0) return translate("ipam.until.now");
  if (remaining < MINUTE_MS) return translate("ipam.until.underMinute");
  if (remaining < HOUR_MS) {
    return translate("ipam.until.minutes", { count: Math.min(59, Math.round(remaining / MINUTE_MS)) });
  }
  if (remaining < DAY_MS) {
    return translate("ipam.until.hours", { count: Math.min(23, Math.round(remaining / HOUR_MS)) });
  }
  return translate("ipam.until.days", { count: Math.round(remaining / DAY_MS) });
}

/**
 * The full "last seen" cell, age plus the sentence that says what it is not.
 *
 * Used as a `title` so the meaning travels with the number: an operator hovering
 * a stale-looking address must not conclude the machine is down.
 */
export function lastSeenMeaning(): string {
  return translate("ipam.lastSeenMeaning");
}

/**
 * A presentation whose worded fields are read from the catalogue when they are
 * read, so an exported table of them follows a language switch. Never copy the
 * label or the meaning into state or a memo.
 */
function present(variant: BadgeVariant, label: MessageKey, meaning: MessageKey): Presentation {
  return lazyFields({ variant }, { label, meaning });
}

// --------------------------------------------------------------------------
// Rule 3 — `state` (documented intent) versus `status` (derived)
// --------------------------------------------------------------------------

export interface Presentation {
  label: string;
  variant: BadgeVariant;
  meaning: string;
}

/**
 * `ipam_address.state` — the four values a HUMAN can assert, and the only
 * column an operator with `ipam.assign` ever writes. No ingestion path writes
 * it: an agent's report can create an address row and can never change its
 * `state` (§0.6), which is also an untrusted-input control.
 */
export const ADDRESS_STATE_PRESENTATION: Readonly<Record<AddressState, Presentation>> = {
  free: present("muted", "ipam.addressState.free.label", "ipam.addressState.free.meaning"),
  reserved: present("info", "ipam.addressState.reserved.label", "ipam.addressState.reserved.meaning"),
  assigned: present("success", "ipam.addressState.assigned.label", "ipam.addressState.assigned.meaning"),
  deprecated: present("muted", "ipam.addressState.deprecated.label", "ipam.addressState.deprecated.meaning"),
};

/**
 * The DERIVED status (§3.1's nine-row table). A pure function of (state,
 * covering ranges, current observations, claimant liveness and the three
 * evidence gates), computed server-side at read time and stored nowhere.
 *
 * WHY THIS IS NOT A SECOND IMPLEMENTATION OF THAT TABLE. Spec §0.16 imagines a
 * `status.ts` mirroring `derive_status` and table-tested against the same
 * cases. The shipped API forecloses it: `GET /subnets/{id}/addresses` returns
 * `status` already derived, and Gates E, L and H — the three inputs that
 * separate `assigned` from `dormant` and `stale` from both — appear on no route
 * in `api.py`. A browser-side mirror would therefore have to GUESS its inputs,
 * and the two derivations could not be kept from drifting; the honest move is
 * to render the server's verdict and explain it. This map is the explanation.
 */
export const ADDRESS_STATUS_PRESENTATION: Readonly<Record<AddressStatus, Presentation>> = {
  dhcp: present("info", "ipam.addressStatus.dhcp.label", "ipam.addressStatus.dhcp.meaning"),
  conflicting: present("destructive", "ipam.addressStatus.conflicting.label", "ipam.addressStatus.conflicting.meaning"),
  in_use: present("success", "ipam.addressStatus.in_use.label", "ipam.addressStatus.in_use.meaning"),
  stale: present("warning", "ipam.addressStatus.stale.label", "ipam.addressStatus.stale.meaning"),
  assigned: present("muted", "ipam.addressStatus.assigned.label", "ipam.addressStatus.assigned.meaning"),
  dormant: present("muted", "ipam.addressStatus.dormant.label", "ipam.addressStatus.dormant.meaning"),
  reserved: present("info", "ipam.addressStatus.reserved.label", "ipam.addressStatus.reserved.meaning"),
  undocumented: present("warning", "ipam.addressStatus.undocumented.label", "ipam.addressStatus.undocumented.meaning"),
  free: present("muted", "ipam.addressStatus.free.label", "ipam.addressStatus.free.meaning"),
};

const ADDRESS_STATUS_SET = new Set<string>(Object.keys(ADDRESS_STATUS_PRESENTATION));
const ADDRESS_STATE_SET = new Set<string>(Object.keys(ADDRESS_STATE_PRESENTATION));

/** The derived `status` of a grid row. Falls back rather than throwing: a
 * server that grows a tenth status must not blank the grid. */
export function statusPresentation(status: string): Presentation {
  if (ADDRESS_STATUS_SET.has(status)) {
    return ADDRESS_STATUS_PRESENTATION[status as AddressStatus];
  }
  return { label: status, variant: "muted", meaning: translate("ipam.unrecognized.status") };
}

/**
 * The DOCUMENTED `state` of a grid row, which is `null` for the ordinary case
 * of no address row at all (§0.4's sparseness).
 *
 * Returns "not documented" — NOT the derived status, and not blank. The two
 * columns answer different questions and a row whose `status` reads "In use"
 * can legitimately have no documented state at all.
 */
export function statePresentation(state: string | null): Presentation {
  if (state === null || state === "") {
    return {
      label: translate("ipam.addressState.none.label"),
      variant: "muted",
      meaning: translate("ipam.addressState.none.meaning"),
    };
  }
  if (ADDRESS_STATE_SET.has(state)) return ADDRESS_STATE_PRESENTATION[state as AddressState];
  return { label: state, variant: "muted", meaning: translate("ipam.unrecognized.state") };
}

/**
 * §3.2's allocation method, and WHO SAID IT.
 *
 * The grid renders both halves in one cell because they are one answer, and
 * because rendering the method alone is §0.6's mistake in a second column: a
 * technician who reads `DHCP` next to an address an operator documented as
 * static, with nothing saying which of the two spoke, concludes the IPAM changed
 * their record.
 *
 * `unknown` is DELIBERATELY NOT rendered as a value. "Nobody has said" is an
 * absence, and an absence in this grid is an em dash everywhere else.
 *
 * Falls back rather than throwing, exactly as `statusPresentation` does: a
 * server that grows a sixth method or a third source must not blank the grid.
 */
/**
 * The wire value of an allocation method, written the way it is written.
 *
 * `method.toUpperCase()` was doing this, and it shouted: `STATIC` is ALL-CAPS
 * body text outside the `.k`/`.grp` caption exception, and any value the API
 * grows later would have been shouted verbatim. `DHCP` and `SLAAC` stay in
 * capitals because they are acronyms, not because the renderer capitalises
 * everything. An unmapped value is sentence-cased rather than passed through,
 * so a sixth method arrives readable.
 */
const ALLOCATION_METHOD_LABELS: Readonly<Record<string, MessageKey>> = {
  static: "ipam.allocation.label.static",
  dhcp: "ipam.allocation.label.dhcp",
  dhcp_reservation: "ipam.allocation.label.dhcp_reservation",
  slaac: "ipam.allocation.label.slaac",
  manual: "ipam.allocation.label.manual",
  pool: "ipam.allocation.label.pool",
  discovered: "ipam.allocation.label.discovered",
};

/** The wire values of an allocation method that have a word of their own, as the address dialog's picker prints them. */
const ALLOCATION_METHOD_WORDS: Readonly<Record<string, MessageKey>> = {
  static: "ipam.allocation.option.static",
  dhcp: "ipam.allocation.option.dhcp",
  slaac: "ipam.allocation.option.slaac",
  discovered: "ipam.allocation.option.discovered",
  unknown: "ipam.allocation.option.unknown",
};

/** A wire allocation method as a lowercase word; one this build does not know stays as the server wrote it. */
export function allocationMethodWord(method: string): string {
  return Object.hasOwn(ALLOCATION_METHOD_WORDS, method) ? translate(ALLOCATION_METHOD_WORDS[method] as MessageKey) : method;
}

export function allocationMethodLabel(method: string): string {
  const known = Object.hasOwn(ALLOCATION_METHOD_LABELS, method.toLowerCase())
    ? ALLOCATION_METHOD_LABELS[method.toLowerCase()]
    : undefined;
  if (known !== undefined) return translate(known);
  const spaced = method.replace(/_/g, " ");
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

export function allocationMethodPresentation(
  entry: Pick<GridEntryOut, "allocation_method" | "allocation_method_source">,
): { label: string; meaning: string } {
  const method = entry.allocation_method;
  if (method === "unknown" || method === "") {
    return { label: "—", meaning: translate("ipam.allocation.noneMeaning") };
  }
  const label = allocationMethodLabel(method);
  if (entry.allocation_method_source === "documented") {
    return { label, meaning: translate("ipam.allocation.documented", { method: allocationMethodWord(method) }) };
  }
  if (entry.allocation_method_source === "observed") {
    return { label, meaning: translate("ipam.allocation.observed", { method: allocationMethodWord(method) }) };
  }
  return { label, meaning: translate("ipam.allocation.unrecognizedSource", { method: allocationMethodWord(method) }) };
}

/**
 * Does the derived status DISAGREE with the documented state?
 *
 * The pair the grid exists to surface: `undocumented` (reality has something
 * the plan does not), `stale` (the plan has something reality does not, and we
 * could have seen it), `conflicting` (two claimants). Deliberately FALSE for
 * `dormant` and for `assigned`-with-no-evidence — in neither case does anything
 * disagree; we simply cannot currently see.
 */
export function planDisagreesWithReality(entry: Pick<GridEntryOut, "status">): boolean {
  return (
    entry.status === "undocumented" || entry.status === "stale" || entry.status === "conflicting"
  );
}

// --------------------------------------------------------------------------
// The free-form `detail` payloads — ONE renderer for both of them
// --------------------------------------------------------------------------
//
// Two responses carry a `Record<string, unknown>` written by the server and read
// by the same operator: the address-history projection (§0.13, §11.3) and a
// drift finding's evidence (§4.3). Both GROW — a new audit writer or a new drift
// class adds keys without asking the browser — and neither is ever rendered as
// JSON: `{"macs":["aa:bb:…"],"vrf_count":2}` in a table cell is a finding
// nobody acts on, which is the failure §4.3 spends its length avoiding.
//
// THEY SHARE ONE ENGINE, one value formatter and one unknown-key fallback,
// for the reason `scope_digest` has one implementation and the grid reads the
// shared conflict predicate instead of computing its own: two renderers of one
// shape drift apart, and the one that drifts is always the one nobody is
// looking at.
//
// KEYED OFF WHICH KEYS ARE PRESENT — never off `action`, never off
// `drift_class`. `detail` is free-form, the set of actions that target an
// address grows, and §4.3's class list has already grown once; a payload whose
// producer this build has never heard of still renders everything it carries.

/** One labelled fact out of a `detail` payload. */
export interface DetailFact {
  /** The `detail` key it came from — a stable React key, and the name a reader
   *  can correlate with the audit event or the reconcile pass itself. */
  key: string;
  label: string;
  value: string;
}

/**
 * Keys that say WHICH address, in WHICH VRF, in WHICH subnet. Every one of
 * §11.3's `ipam_address` events carries all three (`_audit_state_change` and
 * `allocation.claim_address` both write them), so they are constant down the
 * whole list — and this panel is already scoped to exactly one address, whose
 * literal is the dialog's title and whose prefix is the line under it.
 * Repeating three identifiers on every row is what makes the two that matter
 * unreadable.
 *
 * THIS IS DE-DUPLICATION, NOT REDACTION, and nothing may be built on it as
 * though it were a control. The server decides what is in this response —
 * §0.13 pins `org_id`, `target_type` and `target_id`, and the projection is
 * reachable with `ipam.read` alone precisely because that pinning is the
 * control. Any OTHER key, including one this build has never seen, renders.
 */
const HISTORY_CONTEXT_KEYS: readonly string[] = ["address", "vrf_id", "subnet_id"];

const CLAIMANT_TYPE_WORD: Readonly<Record<string, MessageKey>> = {
  device: "ipam.facts.claimantType.device",
  asset_label: "ipam.facts.claimantType.asset_label",
};

/** Wire keys this module's own writers and importer produce, with a worded label. */
const KNOWN_KEY_LABEL: Readonly<Record<string, MessageKey>> = {
  address: "ipam.facts.key.address",
  hostname: "ipam.facts.key.hostname",
  description: "ipam.facts.key.description",
  asset_label: "ipam.facts.key.asset_label",
  mac: "ipam.facts.key.mac",
  allocation_method: "ipam.facts.key.allocation_method",
  reason: "ipam.facts.key.reason",
  source: "ipam.facts.key.source",
};

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** `previous_claimant` -> "Previous claimant". Used for any key this build did
 *  not anticipate, so a new one arrives readable instead of raw. */
function humanizeKey(key: string): string {
  if (Object.hasOwn(KNOWN_KEY_LABEL, key)) return translate(KNOWN_KEY_LABEL[key] as MessageKey);
  const words = key.replace(/_/g, " ").trim();
  if (words === "") return key;
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function formatScalar(value: unknown): string {
  if (value === null || value === undefined) return translate("ipam.facts.none");
  if (typeof value === "string") return value === "" ? translate("ipam.facts.empty") : value;
  if (typeof value === "number" || typeof value === "boolean" || typeof value === "bigint") {
    return String(value);
  }
  return JSON.stringify(value) ?? String(value);
}

/**
 * One `detail` value as text.
 *
 * Rule 1 travels in here too: a key ending `_at` is an INSTANT, and instants in
 * this module are ages. No writer of an `ipam_address` audit event puts a
 * timestamp in its metadata today; this is what stops the first one that does
 * from printing a clock time inside a module that has no liveness signal at all.
 *
 * Lists and nested objects are flattened to `label: value` text rather than
 * dumped as JSON — an operator reading "who took this address and how" should
 * not have to parse braces — and neither case can throw, so an unanticipated
 * shape degrades to something readable instead of blanking the panel.
 */
function formatDetailValue(key: string, value: unknown): string {
  if (key.endsWith("_at") && typeof value === "string" && value !== "") {
    // `_at` IS A NAMING CONVENTION, NOT A TYPE, and reconcile proves it: a
    // `device_moved` finding's detail is
    // `{documented_at: "10.4.7.9", observed_at: "10.4.7.57", …}` — the two
    // ADDRESSES the laptop is documented at and seen at, which are the whole
    // content of that finding. `formatAge` on those returns "unknown"
    // (`Date.parse("10.4.7.57")` is `NaN`) and the operator loses the evidence.
    //
    // So the age rule applies to values that ARE instants, and anything else
    // keeps its literal text. Rule 1 is untouched: a string that does not parse
    // as a date cannot print a clock time, and every real timestamp still comes
    // out as an age.
    if (Number.isFinite(Date.parse(value))) return formatAge(value);
    return value;
  }
  if (Array.isArray(value)) {
    return value.length === 0 ? translate("ipam.facts.noneList") : value.map((item) => formatScalar(item)).join(", ");
  }
  if (isPlainObject(value)) {
    const entries = Object.entries(value);
    return entries.length === 0
      ? translate("ipam.facts.noneList")
      : entries.map(([k, v]) => `${humanizeKey(k)}: ${formatScalar(v)}`).join(", ");
  }
  return formatScalar(value);
}

/**
 * `ipam.address_updated` writes `from` and `to` — the same field names in each,
 * the old and new values (`addresses.update_address`). Rendered as one line per
 * field, "old → new", because two objects side by side make the reader diff
 * them by eye, which is the same failure as printing the blob.
 */
function fieldChanges(detail: Record<string, unknown>): DetailFact[] {
  const from = detail.from;
  const to = detail.to;
  if (!isPlainObject(from) || !isPlainObject(to)) return [];
  const fields = [...new Set([...Object.keys(from), ...Object.keys(to)])].sort();
  return fields.map((field) => ({
    key: `changed:${field}`,
    label: humanizeKey(field),
    value: `${formatDetailValue(field, from[field])} → ${formatDetailValue(field, to[field])}`,
  }));
}

/**
 * One rule: "when this key is here, say this about it".
 *
 * A rule that returns no facts DECLINES — it consumes nothing, and the generic
 * pass below renders its keys as humanised text instead. That is what stops a
 * payload of an unexpected shape (a `vrf_count` that arrives as a string, a
 * `from` with no `to`) from swallowing its own evidence.
 */
interface FactRule {
  /** The key whose PRESENCE fires the rule. */
  key: string;
  /** Further keys this rule speaks for, consumed with it so they do not also
   *  appear as raw rows. */
  also?: readonly string[];
  render(detail: Record<string, unknown>, names: EvidenceNames): DetailFact[];
}

/**
 * RECORD IDS ARE NOT NAMES, and a payload full of them is a payload nobody
 * reads.
 *
 * `reconcile._evaluate` writes ids, because ids are what it holds: `dev-3`,
 * `subnet-1`. Every other column of the drift table names the thing it is
 * about — the address, the class, the subnet — so the evidence beside them
 * read as the one place the machine's vocabulary leaked through. The page
 * already fetches both registries this needs (the subnet list for the row's
 * own link, the device list from the shared `["devices"]` cache), so naming
 * costs nothing it was not already paying.
 *
 * A LOOKUP THAT MISSES FALLS BACK TO THE ID. An id the operator cannot resolve
 * is still better than a blank, and a device that has left the fleet keeps its
 * finding readable.
 */
export interface EvidenceNames {
  /** Device id → the hostname an operator recognises. */
  devices?: ReadonlyMap<string, string>;
  /** Subnet id → its prefix (and name). */
  subnets?: ReadonlyMap<string, string>;
}

const NO_NAMES: EvidenceNames = {};

/**
 * The engine both payloads run through.
 *
 * Named rules first, in the order an operator asks their questions; then EVERY
 * REMAINING KEY, humanised and formatted, sorted for stability. Nothing is
 * dropped and nothing here can throw: an unanticipated key is the expected case,
 * not the error case.
 */
function describeDetail(
  raw: unknown,
  rules: readonly FactRule[],
  contextKeys: readonly string[] = [],
  names: EvidenceNames = NO_NAMES,
): DetailFact[] {
  if (!isPlainObject(raw)) return [];
  const facts: DetailFact[] = [];
  const consumed = new Set<string>(contextKeys);
  for (const rule of rules) {
    if (!(rule.key in raw)) continue;
    const produced = rule.render(raw, names);
    if (produced.length === 0) continue;
    consumed.add(rule.key);
    for (const extra of rule.also ?? []) consumed.add(extra);
    facts.push(...produced);
  }
  for (const key of Object.keys(raw).sort()) {
    if (consumed.has(key)) continue;
    facts.push({ key, label: humanizeKey(key), value: formatDetailValue(key, raw[key]) });
  }
  return facts;
}

/** A key that only needs a better name than `humanizeKey` gives it. The VALUE
 *  still goes through the shared formatter, so lists flatten and instants age. */
function labelled(key: string, label: MessageKey): FactRule {
  return {
    key,
    render: (detail) => [{ key, label: translate(label), value: formatDetailValue(key, detail[key]) }],
  };
}

/**
 * A key whose VALUE is a record id, rendered as the name it stands for.
 *
 * Falls back to `labelled`'s behaviour in both directions that can miss: a
 * value that is not a non-empty string goes through the shared formatter (so a
 * `device_id: null` still reads "none", not a blank), and an id no lookup
 * knows renders as itself.
 */
function named(key: string, label: MessageKey, book: "devices" | "subnets"): FactRule {
  return {
    key,
    render: (detail, names) => {
      const raw = detail[key];
      const value =
        typeof raw === "string" && raw !== ""
          ? (names[book]?.get(raw) ?? raw)
          : formatDetailValue(key, raw);
      return [{ key, label: translate(label), value }];
    },
  };
}

/**
 * A boolean whose MEANING is the fact. `Gate e false` is not a sentence an
 * operator can act on; "nothing looked at this segment inside the window" is the
 * reason this row is dormant rather than drift (§4.3).
 *
 * Declines on a non-boolean rather than coercing one — `Boolean("false")` is
 * `true`, and a wrong gate reading here is the difference between "we cannot
 * see it" and "it is wrong".
 */
function flag(key: string, label: MessageKey, whenTrue: MessageKey, whenFalse: MessageKey): FactRule {
  return {
    key,
    render: (detail) => {
      const value = detail[key];
      if (typeof value !== "boolean") return [];
      return [{ key, label: translate(label), value: translate(value ? whenTrue : whenFalse) }];
    },
  };
}

const HISTORY_FACT_RULES: readonly FactRule[] = [
  // §11.3's assign payload: WHO took it and HOW. `claimant_type` is folded into
  // the claimant rather than given its own row — "dev-7 (device)" answers the
  // question, whereas two rows make the reader join them.
  {
    key: "claimant",
    also: ["claimant_type"],
    render: (detail) => {
      const who = formatScalar(detail.claimant);
      const rawKind = typeof detail.claimant_type === "string" ? detail.claimant_type : "";
      const kind = Object.hasOwn(CLAIMANT_TYPE_WORD, rawKind)
        ? translate(CLAIMANT_TYPE_WORD[rawKind] as MessageKey)
        : rawKind;
      return [
        { key: "claimant", label: translate("ipam.facts.claimedBy"), value: kind === "" ? who : `${who} (${kind})` },
      ];
    },
  },
  {
    key: "previous_claimant",
    render: (detail) => {
      const who = detail.previous_claimant;
      return [
        {
          key: "previous_claimant",
          label: translate("ipam.facts.releasedFrom"),
          // Releasing a RESERVED address is the real empty case: a reservation
          // is held by nobody, so `addresses.release` records "". "(empty)"
          // there reads as a value that went missing, and it did not.
          value:
            who === null || who === undefined || who === ""
              ? translate("ipam.facts.releasedNobody")
              : formatScalar(who),
        },
      ];
    },
  },
  {
    key: "allocation_method",
    render: (detail) => {
      const method = detail.allocation_method;
      return [
        {
          key: "allocation_method",
          label: translate("ipam.facts.method"),
          // The wire method as a word of its own; one this build does not know stays as written.
          value: typeof method === "string" && method !== "" ? allocationMethodWord(method) : formatDetailValue("allocation_method", method),
        },
      ];
    },
  },
  { key: "from", also: ["to"], render: (detail) => fieldChanges(detail) },
];

/**
 * §4.3's evidence payloads, as facts.
 *
 * DERIVED FROM WHAT `reconcile._evaluate` ACTUALLY WRITES, key by key, not from
 * the class list — the classes below share keys and every one of these keys can
 * turn up under a class that does not write it today:
 *
 *   `conflict`             → `macs`, `claimant_mismatch`, `vrf_count`
 *   `mac_mismatch`         → `documented_mac`, `observed_macs`
 *   `claimant_mismatch`    → `documented_device_id`, `observed_device_id`
 *   `device_moved`         → `documented_at`, `observed_at` (BOTH ADDRESSES),
 *                            `device_id`, `observed_subnet_id`
 *   `present_undocumented` → `macs`, `sources`, `hostnames`
 *   `documented_absent`    → `state`, `device_id`, `asset_label`, `gate_e`,
 *                            `gate_l`
 *   `dormant`              → `state`, `device_id`, `gate_e`, `gate_l`
 *
 * This renders `dormant` too. It changes NOTHING about what is counted:
 * `driftCounts` is still the only place the headline is computed and `dormant`
 * and orphans are still outside it — a bucket the operator can read is not a
 * bucket the operator is charged for.
 */
/*
 * A LABEL IS A QUESTION, NOT A PARAGRAPH, and this list sits in a table cell.
 *
 * The labels below were once whole clauses ("VRFs claiming it across a shared
 * segment", "Segment looked at in the window") and the values below them were
 * whole sentences restating those clauses. Inside a `.kv` grid roughly 24ch
 * wide that is four wrapped lines per fact: one `documented_absent` row stood
 * 230px tall beside a 60px neighbour and the list lost its rhythm. Each label
 * is now short enough not to wrap in the cell's label track, and each value
 * answers rather than repeats. The class's full meaning and remedy are stated
 * once in the notice above the table, which is the place a sentence that is
 * true of every row of a class belongs.
 */
const DRIFT_FACT_RULES: readonly FactRule[] = [
  labelled("state", "ipam.facts.documentedState"),
  named("device_id", "ipam.facts.documentedDevice", "devices"),
  labelled("asset_label", "ipam.facts.assetLabel"),
  named("documented_device_id", "ipam.facts.documentedClaimant", "devices"),
  // `reconcile._evaluate` writes `documented_claimant` for the claimant
  // mismatch class and `documented_device_id` elsewhere. Both are the same
  // question with the same answer, so both get the same label rather than one
  // of them falling through to the generic pass under a raw key name.
  named("documented_claimant", "ipam.facts.documentedClaimant", "devices"),
  // §4.3: only `source='inventory'` evidence can name a device, so this is the
  // machine's own account of itself, not a sweep's guess.
  named("observed_device_id", "ipam.facts.observedClaimant", "devices"),
  named("observed_claimant", "ipam.facts.observedClaimant", "devices"),
  named("observed_by", "ipam.facts.observedBy", "devices"),
  labelled("documented_mac", "ipam.facts.documentedMac"),
  labelled("observed_macs", "ipam.facts.observedMacs"),
  labelled("macs", "ipam.facts.macsSeen"),
  labelled("hostnames", "ipam.facts.hostnames"),
  labelled("sources", "ipam.facts.sources"),
  // Addresses, not instants — see `formatDetailValue`. Named here rather than
  // left to the generic pass so a `device_moved` finding reads
  // "where it should be, where it is, which subnet" in that order.
  labelled("documented_at", "ipam.facts.documentedAt"),
  labelled("observed_at", "ipam.facts.observedAt"),
  named("observed_subnet_id", "ipam.facts.observedIn", "subnets"),
  flag("claimant_mismatch", "ipam.facts.claimantMismatch", "ipam.facts.mismatchYes", "ipam.facts.mismatchNo"),
  {
    // §4.3's cross-VRF disjunct, as a number: how many VRFs claim this address
    // across a segment something has actually swept. Zero is not "no VRFs" — it
    // is "this conflict is not the cross-VRF one", and printing a bare 0 beside
    // the label reads as the opposite.
    key: "vrf_count",
    render: (detail) => {
      const count = detail.vrf_count;
      if (typeof count !== "number") return [];
      return [
        {
          key: "vrf_count",
          label: translate("ipam.facts.vrfCount"),
          value: count === 0 ? translate("ipam.facts.vrfNone") : formatCount(count),
        },
      ];
    },
  },
  // THE LABEL ASKS, THE VALUE ANSWERS, and neither says the other's words
  // again. "Segment looked at in the window: yes, a sweep or an inventory
  // ingest covered this segment" is one fact spelled twice, and in a 24ch cell
  // it was six wrapped lines of a row that had four such facts in it.
  flag("gate_e", "ipam.facts.segmentLooked", "ipam.facts.insideWindow", "ipam.facts.notInsideWindow"),
  flag("gate_l", "ipam.facts.claimantReachable", "ipam.facts.agentCheckedIn", "ipam.facts.offlinePastWindow"),
];

/** §11.3's payload, as labelled facts in the order an operator asks for them. */
export function describeHistoryDetail(entry: Pick<HistoryEntryOut, "detail">): DetailFact[] {
  return describeDetail(entry.detail, HISTORY_FACT_RULES, HISTORY_CONTEXT_KEYS);
}

/**
 * §4.3's evidence, as labelled facts — the same engine, the same formatter and
 * the same unknown-key fallback as the history panel.
 *
 * No context keys: a drift row's own address, class and severity are its
 * neighbouring columns, and `detail` repeats none of them except
 * `device_moved`'s `documented_at`, which is there to be read against
 * `observed_at`.
 */
export function describeDriftDetail(
  finding: Pick<DriftOut, "detail">,
  names: EvidenceNames = NO_NAMES,
): DetailFact[] {
  return describeDetail(finding.detail, DRIFT_FACT_RULES, [], names);
}

/**
 * §8.1's per-row payload, as labelled facts — the THIRD caller of one engine.
 *
 * The import verdict table rendered `row.error || JSON.stringify(row.detail)`,
 * so a row that imported cleanly showed `{"asset_label":"Laptop 4"}` in its
 * Detail cell and a row that carried nothing showed the literal `{}`. That is
 * the raw-payload defect both auditors filed elsewhere in this product, in the
 * one table an operator reads to decide whether to write a CSV into the plan of
 * record.
 *
 * NO RULE SET. Unlike drift and history, nothing here needs renaming: the
 * importer's keys are the mapped column names the operator chose themselves, so
 * `humanizeKey` gives them back the header they typed.
 *
 * EMPTY VALUES ARE DROPPED, not rendered "(empty)". `preview_import` sends
 * `asset_label` for every accepted row whether the column was mapped or not, so
 * keeping them would put "Asset label (empty)" on every line of a 200-row
 * preview — noise that hides the rows that DO carry something.
 */
export function describeImportDetail(row: Pick<ImportRowOut, "detail">): DetailFact[] {
  if (!isPlainObject(row.detail)) return [];
  const carried = Object.fromEntries(
    Object.entries(row.detail).filter(([, value]) => value !== "" && value !== null && value !== undefined),
  );
  return describeDetail(carried, []);
}

// --------------------------------------------------------------------------
// Rule 4 — the enumeration ceiling
// --------------------------------------------------------------------------

/**
 * A discriminated union, so a read site that has checked `available` holds a
 * real percentage and never reaches for `?? 0`: unmeasured is never 0, and
 * the type is where that rule is enforced rather than remembered.
 */
export type UtilizationDisplay =
  | {
      available: false;
      /** No percentage to show. */
      percentLabel: null;
      /** No bar may be drawn at all. */
      barPercent: null;
      /** The word to render where the percentage would go. Never "0%". */
      headline: string;
      detail: string;
    }
  | {
      available: true;
      /** "42.0%" */
      percentLabel: string;
      /** Bar width 0..100. */
      barPercent: number;
      headline: string;
      detail: string;
      /** The counts, as the server sent them: `null` is rendered as `n/a`. */
      used: number | null;
      capacity: number | null;
    };

/** A count the server may not have sent: `n/a`, never a coerced 0. */
export function formatCountOrNa(value: number | null): string {
  return value === null ? translate("ipam.utilization.na") : String(value);
}

/** A percentage to one decimal place, with the reader's decimal separator. */
function formatPercentNumber(percent: number): string {
  const fixed = percent.toFixed(1);
  return getLanguage() === "es" ? fixed.replace(".", ",") : fixed;
}

/**
 * Utilisation, with the ceiling's null percentage rendered as UNAVAILABLE.
 *
 * §0.4 and §0.5 are one rule with two applications: above the org's
 * enumeration ceiling, and for every IPv6 prefix, the server returns
 * `enumerable: false` with `capacity`/`free`/`used`/`percentage` all null and
 * the four COUNTS still populated. `percentage ?? 0` would print "0%" and a bar
 * at zero, which reads as "this /12 is empty" — the opposite of "we did not
 * enumerate it". So the percentage is omitted entirely and the counts, which
 * are real, are shown instead.
 */
export function utilizationDisplay(util: UtilizationOut): UtilizationDisplay {
  if (util.percentage === null) {
    return {
      percentLabel: null,
      barPercent: null,
      headline: translate("ipam.utilization.unavailable"),
      detail: translate("ipam.utilization.tooLarge", {
        prefix: util.prefix,
        ceiling: util.enumerable ? "" : translate("ipam.utilization.ceiling"),
        assigned: util.assigned,
        reserved: util.reserved,
        deprecated: util.deprecated,
        observed: util.observed,
      }),
      available: false,
    };
  }
  const percent = Math.max(0, Math.min(100, util.percentage));
  return {
    percentLabel: translate("ipam.utilization.percent", { percent: formatPercentNumber(percent) }),
    barPercent: percent,
    headline: translate("ipam.utilization.used", { percent: formatPercentNumber(percent) }),
    detail: translate("ipam.utilization.detail", {
      count: util.capacity ?? 0,
      used: formatCountOrNa(util.used),
      capacity: formatCountOrNa(util.capacity),
      prefix: util.prefix,
      free: formatCountOrNa(util.free),
    }),
    available: true,
    used: util.used,
    capacity: util.capacity,
  };
}

/** Container prefixes hold no addresses at all (§0.2) — the tree renders them
 * without a utilisation bar rather than with an empty one. */
export function hasUtilization(subnet: Pick<SubnetOut, "kind">): boolean {
  return subnet.kind === "assignable";
}

// --------------------------------------------------------------------------
// Rules 2 and 5 — drift, and what is not drift
// --------------------------------------------------------------------------

/** A drift class's presentation, its words read from the catalogue when read. */
function drift(
  variant: BadgeVariant,
  label: MessageKey,
  meaning: MessageKey,
  remedy: MessageKey,
): DriftClassPresentation {
  return lazyFields({ variant }, { label, meaning, remedy });
}

export interface DriftClassPresentation {
  label: string;
  variant: BadgeVariant;
  meaning: string;
  /** THE REMEDY. A conflict and a mismatch are not the same finding and must
   * never share a button (§4.3). */
  remedy: string;
}

export const DRIFT_CLASS_PRESENTATION: Readonly<Record<DriftClass, DriftClassPresentation>> = {
  documented_absent: drift(
    "warning",
    "ipam.driftClass.documented_absent.label",
    "ipam.driftClass.documented_absent.meaning",
    "ipam.driftClass.documented_absent.remedy",
  ),
  present_undocumented: drift(
    "info",
    "ipam.driftClass.present_undocumented.label",
    "ipam.driftClass.present_undocumented.meaning",
    "ipam.driftClass.present_undocumented.remedy",
  ),
  mac_mismatch: drift(
    "warning",
    "ipam.driftClass.mac_mismatch.label",
    "ipam.driftClass.mac_mismatch.meaning",
    "ipam.driftClass.mac_mismatch.remedy",
  ),
  claimant_mismatch: drift(
    "destructive",
    "ipam.driftClass.claimant_mismatch.label",
    "ipam.driftClass.claimant_mismatch.meaning",
    "ipam.driftClass.claimant_mismatch.remedy",
  ),
  device_moved: drift(
    "warning",
    "ipam.driftClass.device_moved.label",
    "ipam.driftClass.device_moved.meaning",
    "ipam.driftClass.device_moved.remedy",
  ),
  conflict: drift(
    "destructive",
    "ipam.driftClass.conflict.label",
    "ipam.driftClass.conflict.meaning",
    "ipam.driftClass.conflict.remedy",
  ),
};

/** `dormant` is NOT in `DRIFT_CLASSES` and this map is deliberately separate:
 * it is a bucket, not a finding. */
export const DORMANT_PRESENTATION: DriftClassPresentation = drift(
    "muted",
    "ipam.driftClass.dormant.label",
    "ipam.driftClass.dormant.meaning",
    "ipam.driftClass.dormant.remedy",
  );

export function driftClassPresentation(driftClass: string): DriftClassPresentation {
  if (driftClass === DORMANT_CLASS) return DORMANT_PRESENTATION;
  if ((DRIFT_CLASSES as readonly string[]).includes(driftClass)) {
    return DRIFT_CLASS_PRESENTATION[driftClass as DriftClass];
  }
  return {
    label: driftClass,
    variant: "muted",
    meaning: translate("ipam.unrecognized.driftClass"),
    remedy: "",
  };
}

/** Is this row a DRIFT FINDING at all? False for `dormant`, which the server
 * also excludes from `/drift`'s default list and from `export/drift.csv`. */
export function isDriftFinding(item: Pick<DriftOut, "drift_class">): boolean {
  return (DRIFT_CLASSES as readonly string[]).includes(item.drift_class);
}

/** Has this finding earned the right to speak? One detection is a switch that
 * was rebooting; the server publishes an event only at two (§0.8). */
export function isCorroborated(item: Pick<DriftOut, "consecutive_detections">): boolean {
  return item.consecutive_detections >= 2;
}

export function isSuppressed(
  item: Pick<DriftOut, "suppressed_until">,
  now: number = Date.now(),
): boolean {
  if (item.suppressed_until === null) return false;
  const until = Date.parse(item.suppressed_until);
  return Number.isFinite(until) && until > now;
}

export function isResolved(item: Pick<DriftOut, "resolved_at">): boolean {
  return item.resolved_at !== null;
}

export interface DriftCounts {
  /** THE HEADLINE. Unresolved findings only, `dormant` excluded, orphans not
   * in it at all. */
  drift: number;
  /** Shown BESIDE the headline, never inside it. */
  dormant: number;
  /** Also shown beside it: an absent plan is not a wrong one. */
  orphans: number;
  acknowledged: number;
  suppressed: number;
  byClass: Record<string, number>;
}

/**
 * The drift headline, with the two exclusions that decide whether anybody opens
 * this page a second time.
 *
 * `dormant` is a bucket, not a finding: every IPAM that got ignored got ignored
 * because its drift list was mostly laptops that were asleep. Orphans are not
 * counted either — nothing is wrong with the plan there, the plan is ABSENT
 * (§4.2), and the action is "register a subnet", not "fix a record". Both are
 * returned as their own numbers rather than dropped, because "37 addresses we
 * cannot verify" and "24 addresses in an unregistered range" are each worth
 * knowing and neither is "37 addresses are wrong".
 *
 * Resolved items are excluded too: a stamped, closed finding is history.
 */
export function driftCounts(
  items: readonly DriftOut[],
  orphanCount = 0,
  now: number = Date.now(),
): DriftCounts {
  const counts: DriftCounts = {
    drift: 0,
    dormant: 0,
    orphans: orphanCount,
    acknowledged: 0,
    suppressed: 0,
    byClass: {},
  };
  for (const item of items) {
    counts.byClass[item.drift_class] = (counts.byClass[item.drift_class] ?? 0) + 1;
    if (isResolved(item)) continue;
    if (!isDriftFinding(item)) {
      // `dormant`, and anything else the server does not call drift.
      counts.dormant += 1;
      continue;
    }
    counts.drift += 1;
    if (item.acknowledged_at !== null) counts.acknowledged += 1;
    if (isSuppressed(item, now)) counts.suppressed += 1;
  }
  return counts;
}

export function severityVariant(severity: string): BadgeVariant {
  if (severity === "high") return "destructive";
  if (severity === "medium") return "warning";
  return "info";
}

// --------------------------------------------------------------------------
// Prefix arithmetic — BigInt, both families, no second code path
// --------------------------------------------------------------------------
//
// Used only to PREVIEW what an authorization covers and to build the derived
// tree. `authorization.authorized_targets` makes the real decision server-side
// on every dispatch; nothing below is relied on for a security outcome.

export interface ParsedPrefix {
  version: 4 | 6;
  /** Network address as an integer. */
  base: bigint;
  bits: number;
  prefixLen: number;
  /** Inclusive last address. */
  last: bigint;
  text: string;
}

function parseIpv4(literal: string): bigint | null {
  const parts = literal.split(".");
  if (parts.length !== 4) return null;
  let value = 0n;
  for (const part of parts) {
    if (!/^\d{1,3}$/.test(part)) return null;
    const octet = Number(part);
    if (octet > 255) return null;
    value = (value << 8n) | BigInt(octet);
  }
  return value;
}

function parseIpv6(literal: string): bigint | null {
  const halves = literal.split("::");
  if (halves.length > 2) return null;
  const expand = (chunk: string): string[] => (chunk === "" ? [] : chunk.split(":"));
  const head = expand(halves[0] ?? "");
  const tail = halves.length === 2 ? expand(halves[1] ?? "") : [];
  const groups: string[] =
    halves.length === 2
      ? [...head, ...Array<string>(8 - head.length - tail.length).fill("0"), ...tail]
      : head;
  if (groups.length !== 8) return null;
  let value = 0n;
  for (const group of groups) {
    if (!/^[0-9a-fA-F]{1,4}$/.test(group)) return null;
    value = (value << 16n) | BigInt(Number.parseInt(group, 16));
  }
  return value;
}

/** Parse `10.0.0.0/24` or `2001:db8::/64`. Host bits are masked off, matching
 * the server's `strict=False`: an operator typing `10.70.0.5/24` means the /24. */
export function parsePrefix(literal: string): ParsedPrefix | null {
  const trimmed = literal.trim();
  const slash = trimmed.lastIndexOf("/");
  if (slash < 0) return null;
  const addressPart = trimmed.slice(0, slash);
  const lengthPart = trimmed.slice(slash + 1);
  if (!/^\d{1,3}$/.test(lengthPart)) return null;
  const prefixLen = Number(lengthPart);
  const version: 4 | 6 = addressPart.includes(":") ? 6 : 4;
  const bits = version === 4 ? 32 : 128;
  if (prefixLen > bits) return null;
  const raw = version === 4 ? parseIpv4(addressPart) : parseIpv6(addressPart);
  if (raw === null) return null;
  const hostBits = BigInt(bits - prefixLen);
  const mask = hostBits === 0n ? (1n << BigInt(bits)) - 1n : ~((1n << hostBits) - 1n);
  const base = raw & mask & ((1n << BigInt(bits)) - 1n);
  return {
    version,
    base,
    bits,
    prefixLen,
    last: base + (1n << hostBits) - 1n,
    text: trimmed,
  };
}

/** Total addresses in a prefix — including network and broadcast. What an
 * authorization actually puts in reach. */
export function prefixSize(prefix: ParsedPrefix): bigint {
  return 1n << BigInt(prefix.bits - prefix.prefixLen);
}

/** Does `outer` contain `inner` (or equal it)? */
export function prefixContains(outer: ParsedPrefix, inner: ParsedPrefix): boolean {
  if (outer.version !== inner.version) return false;
  return inner.base >= outer.base && inner.last <= outer.last;
}

export function prefixesOverlap(a: ParsedPrefix, b: ParsedPrefix): boolean {
  if (a.version !== b.version) return false;
  return a.base <= b.last && b.base <= a.last;
}

/** Group digits so 16777216 reads as 16,777,216. Nobody counts zeroes
 * correctly at a glance, and this number is the point of the sentence. */
export function formatCount(value: bigint | number): string {
  return value.toLocaleString(intlLocale());
}

// --------------------------------------------------------------------------
// Rule 6 — scan authorization
// --------------------------------------------------------------------------

export type ScopeStatus = "active" | "expired" | "revoked";

/** Revoked outranks expired: a row that was revoked and then passed its expiry
 * was stopped by a person, and saying "expired" would misattribute that. */
export function scopeStatus(scope: ScopeOut, now: number = Date.now()): ScopeStatus {
  if (scope.revoked_at !== null) return "revoked";
  if (scope.expires_at !== null) {
    const expiry = Date.parse(scope.expires_at);
    if (Number.isFinite(expiry) && expiry <= now) return "expired";
  }
  return "active";
}

export const SCOPE_STATUS_PRESENTATION: Readonly<Record<ScopeStatus, Presentation>> = {
  active: present("warning", "ipam.scopeStatus.active.label", "ipam.scopeStatus.active.meaning"),
  expired: present("muted", "ipam.scopeStatus.expired.label", "ipam.scopeStatus.expired.meaning"),
  revoked: present("muted", "ipam.scopeStatus.revoked.label", "ipam.scopeStatus.revoked.meaning"),
};

export interface ScopeDescription {
  status: ScopeStatus;
  /** The plain sentence: what this row permits, in addresses. */
  summary: string;
  /**
   * The safeguard that applies to EVERY grant, identically.
   *
   * Carried on the description and rendered ONCE on the page rather than once
   * per row: it is the same forty words under every prefix, and repeating it
   * in a table cell made each row about 500px tall on a phone, pushing the
   * row's state, its note and its only action out of the viewport. A sentence
   * that is true of all rows is not a property of any one row.
   */
  safeguard: string;
  addressCount: string | null;
  expiry: string;
}

/**
 * Why an over-broad grant is survivable, and why it is still not a good idea.
 *
 * True of every grant identically, so it is stated once above the table rather
 * than repeated in every row (see `ScopeDescription.safeguard`).
 */
export function sweepScopeSafeguard(): string {
  return translate("ipam.scope.safeguard");
}

/**
 * The safeguard as it read when this module loaded. Kept only because the unit
 * test imports the name; every screen reads `sweepScopeSafeguard()`, which
 * follows a language switch, and this snapshot does not.
 */
export const SWEEP_SCOPE_SAFEGUARD = sweepScopeSafeguard();

/**
 * What an ACTIVE grant permits, in one sentence — true of every active row
 * identically, so it is stated once above the table.
 *
 * Per row it was `describeScope`'s own `summary`, and per row it said nothing
 * the row had not already said: "Sweeps may put ICMP/ARP packets on any address
 * inside 10.20.4.0/24, 256 addresses, in this VRF" beside a Prefix cell already
 * reading "10.20.4.0/24 · 256 addresses". Four wrapped lines of it in every
 * row, and on a phone those four lines sat outside the card's horizontal
 * scroll setting a row height with nothing visible in it. What varies per
 * active row — which registered subnets fall inside the prefix — stays in the
 * cell. `summary` itself is unchanged and is still what an INACTIVE row shows,
 * because "it covered this while it was active" is a fact about that row.
 */
export function sweepScopeGrantMeaning(): string {
  return translate("ipam.scope.grantMeaning");
}

/**
 * What a given authorization ACTUALLY PERMITS, in the operator's terms.
 *
 * This is the module's control surface (§0.12): deny by default, per prefix,
 * admin tier. An operator who misreads the scope authorises packets against a
 * segment they did not intend, so the sentence names the address count rather
 * than leaving `/8` to be read as "a network". The safeguard that a prefix can
 * never WIDEN a sweep is stated too, because it is the reason a /8 grant is
 * survivable — and it is not a reason to write one.
 */
export function describeScope(scope: ScopeOut, now: number = Date.now()): ScopeDescription {
  const status = scopeStatus(scope, now);
  const parsed = parsePrefix(scope.prefix);
  const size = parsed === null ? null : prefixSize(parsed);
  const addressCount = size === null ? null : formatCount(size);
  const reach =
    size === null || addressCount === null
      ? `${scope.prefix}`
      : translate("ipam.scope.reach", { prefix: scope.prefix, total: addressCount, count: size === 1n ? 1 : 2 });
  // AN EXPIRY IS A DEADLINE, NOT AN AGE. `formatAge` returns "just now" for
  // any instant that has not arrived, so a grant lapsing in thirty days read
  // "Expires just now" — the exact opposite of the fact, on the page that
  // decides where this platform puts packets. Future and past are two
  // sentences because they are two states.
  const expiry =
    scope.expires_at === null
      ? translate("ipam.scope.noExpiry")
      : Date.parse(scope.expires_at) > now
        ? translate("ipam.scope.expires", { when: formatUntil(scope.expires_at, now) })
        : translate("ipam.scope.expired", { age: formatAge(scope.expires_at, now) });
  const summary =
    status === "active"
      ? translate("ipam.scope.summaryActive", { reach })
      : translate("ipam.scope.summaryInactive", { reach });
  return { status, summary, safeguard: sweepScopeSafeguard(), addressCount, expiry };
}

export interface EffectiveScope {
  /** Active authorizations in the subnet's VRF that overlap its prefix. */
  covering: ScopeOut[];
  /** True when at least one active authorization overlaps. Deny by default:
   * an empty list means this subnet is NOT sweepable. */
  authorized: boolean;
  explanation: string;
}

/**
 * A PREVIEW of whether this subnet may be swept, and by what.
 *
 * Deliberately a preview and nothing more. `authorized_targets` recomputes the
 * set server-side on every dispatch and the answer there is the only one that
 * matters — this exists so the subnet page can say "nobody has authorised
 * probing this segment" before an operator clicks a button that 403s, and so
 * the operator can see WHICH grant makes it sweepable.
 *
 * The intersection is taken with the SUBNET FIRST, which is the safeguard
 * §6.2 turns on: an authorization for `10.0.0.0/8` covering a `/24` yields the
 * `/24`, never the `/8`.
 */
export function effectiveSweepScope(
  subnet: Pick<SubnetOut, "prefix" | "vrf_id">,
  scopes: readonly ScopeOut[],
  now: number = Date.now(),
): EffectiveScope {
  const parsedSubnet = parsePrefix(subnet.prefix);
  const covering = scopes.filter((scope) => {
    if (scope.vrf_id !== subnet.vrf_id) return false;
    if (scopeStatus(scope, now) !== "active") return false;
    const parsedScope = parsePrefix(scope.prefix);
    if (parsedScope === null || parsedSubnet === null) return false;
    return prefixesOverlap(parsedScope, parsedSubnet);
  });
  if (covering.length === 0) {
    return {
      covering,
      authorized: false,
      explanation: translate("ipam.scope.notAuthorized"),
    };
  }
  return {
    covering,
    authorized: true,
    explanation: translate("ipam.scope.sweepable", {
      count: covering.length,
      prefix: subnet.prefix,
      list: covering.map((scope) => scope.prefix).join(", "),
    }),
  };
}

// --------------------------------------------------------------------------
// Sweep runs
// --------------------------------------------------------------------------

export const SWEEP_STATE_PRESENTATION: Readonly<Record<string, Presentation>> = {
  queued: present("info", "ipam.sweepState.queued.label", "ipam.sweepState.queued.meaning"),
  dispatched: present("info", "ipam.sweepState.dispatched.label", "ipam.sweepState.dispatched.meaning"),
  succeeded: present("success", "ipam.sweepState.succeeded.label", "ipam.sweepState.succeeded.meaning"),
  failed: present("destructive", "ipam.sweepState.failed.label", "ipam.sweepState.failed.meaning"),
  refused: present("destructive", "ipam.sweepState.refused.label", "ipam.sweepState.refused.meaning"),
  no_agent: present("warning", "ipam.sweepState.no_agent.label", "ipam.sweepState.no_agent.meaning"),
};

export function sweepStatePresentation(state: string): Presentation {
  return (
    SWEEP_STATE_PRESENTATION[state] ?? {
      label: state,
      variant: "muted",
      meaning: translate("ipam.unrecognized.sweepState"),
    }
  );
}

/** The one-line outcome of the most recent run, including `no_agent`. */
export function describeSweepRun(run: SweepRunOut | undefined, now: number = Date.now()): string {
  if (run === undefined) return translate("ipam.sweepRun.never");
  const state = sweepStatePresentation(run.state);
  const when = formatAge(run.finished_at ?? run.started_at, now);
  return [
    translate("ipam.sweepRun.head", { state: state.label, when }),
    run.state === "succeeded"
      ? translate("ipam.sweepRun.found", { count: run.hosts_found, found: run.hosts_found, total: run.target_count })
      : "",
    run.truncated ? translate("ipam.sweepRun.truncated") : "",
    run.error ?? "",
  ]
    .filter((part) => part !== "")
    .join(" ");
}

// --------------------------------------------------------------------------
// Ranges — the banded rows of the grid
// --------------------------------------------------------------------------

export const RANGE_KIND_PRESENTATION: Readonly<Record<string, Presentation>> = {
  dhcp_pool: present("info", "ipam.rangeKind.dhcp_pool.label", "ipam.rangeKind.dhcp_pool.meaning"),
  reserved: present("warning", "ipam.rangeKind.reserved.label", "ipam.rangeKind.reserved.meaning"),
  excluded: present("muted", "ipam.rangeKind.excluded.label", "ipam.rangeKind.excluded.meaning"),
};

export function rangeKindPresentation(kind: string): Presentation {
  return (
    RANGE_KIND_PRESENTATION[kind] ?? {
      label: kind,
      variant: "muted",
      meaning: translate("ipam.unrecognized.rangeKind"),
    }
  );
}

/** The band an address falls in, for the grid's banded rows. `range_kinds`
 * arrives sorted from the server and an address can be in more than one. */
export function bandLabel(entry: Pick<GridEntryOut, "range_kinds">): string | null {
  if (entry.range_kinds.length === 0) return null;
  return entry.range_kinds.map((kind) => rangeKindPresentation(kind).label).join(" + ");
}

export function rangeSpan(range: Pick<RangeOut, "start_address" | "end_address">): string {
  return `${range.start_address} – ${range.end_address}`;
}

// --------------------------------------------------------------------------
// The derived hierarchy (§0.2)
// --------------------------------------------------------------------------

export interface SubnetNode {
  subnet: SubnetOut;
  depth: number;
  children: SubnetNode[];
}

/**
 * The tree, built from `parent_id` — which is a server-maintained CACHE of "the
 * smallest other prefix in the same VRF that contains this one", recomputed on
 * every write. Nobody sets it, and this function does not re-derive it from the
 * prefixes: doing so would give the tree a second opinion about containment.
 *
 * A row whose `parent_id` names a subnet not in the current (filtered) result
 * is rendered at the ROOT rather than dropped — a filter that hid a child
 * because its parent did not match would silently shrink the estate.
 */
export function buildSubnetTree(subnets: readonly SubnetOut[]): SubnetNode[] {
  const nodes = new Map<string, SubnetNode>();
  for (const subnet of subnets) nodes.set(subnet.id, { subnet, depth: 0, children: [] });
  const roots: SubnetNode[] = [];
  for (const subnet of subnets) {
    const node = nodes.get(subnet.id);
    if (node === undefined) continue;
    const parent = subnet.parent_id === null ? undefined : nodes.get(subnet.parent_id);
    if (parent === undefined) roots.push(node);
    else parent.children.push(node);
  }
  const assignDepth = (node: SubnetNode, depth: number): void => {
    node.depth = depth;
    for (const child of node.children) assignDepth(child, depth + 1);
  };
  for (const root of roots) assignDepth(root, 0);
  return roots;
}

/** Depth-first flattening, so the tree renders as table rows with an indent. */
export function flattenSubnetTree(roots: readonly SubnetNode[]): SubnetNode[] {
  const out: SubnetNode[] = [];
  const walk = (node: SubnetNode): void => {
    out.push(node);
    for (const child of node.children) walk(child);
  };
  for (const root of roots) walk(root);
  return out;
}

// --------------------------------------------------------------------------
// Rule 7 — import is two-step, bound by a digest
// --------------------------------------------------------------------------

export interface PreviewedFile {
  /** Identity of the file the digest belongs to. */
  name: string;
  size: number;
  lastModified: number;
  digest: string;
  /**
   * The VRF the preview read -- `null` when it named the internal client's
   * `global` VRF instead. Apply goes THERE, never to whatever the picker shows
   * by then: the server's digest covers the VRF too and refuses any other.
   * Required, not optional: an absent value would silently mean "the internal
   * client".
   */
  vrfId: string | null;
}

/**
 * WHY APPLY MAY NOT BE OFFERED, or `null` when it may.
 *
 * §8.1 makes import two steps bound by a sha256: preview writes nothing and
 * returns the digest, and `apply` must echo it. The guard only works if the
 * file being applied is the file that was previewed — so this refuses when
 * nothing has been previewed, and refuses again when the chosen file is not the
 * previewed one. A UI that let someone apply a file they never previewed (or
 * swapped after previewing) defeats the guard entirely; the server would still
 * reject a stale digest, but the operator would have learned nothing from the
 * preview they read.
 */
export function importApplyBlockReason(
  selected: { name: string; size: number; lastModified: number } | null,
  previewed: PreviewedFile | null,
): string | null {
  if (selected === null) return translate("ipam.importBlock.choose");
  if (previewed === null) return translate("ipam.importBlock.neverPreviewed");
  if (
    previewed.name !== selected.name ||
    previewed.size !== selected.size ||
    previewed.lastModified !== selected.lastModified
  ) {
    return translate("ipam.importBlock.swapped", { previewed: previewed.name, selected: selected.name });
  }
  if (selected.size > 10 * 1024 * 1024) return translate("ipam.importBlock.tooLarge");
  return null;
}

export const IMPORT_ACTION_PRESENTATION: Readonly<Record<string, Presentation>> = {
  create: present("success", "ipam.importAction.create.label", "ipam.importAction.create.meaning"),
  update: present("info", "ipam.importAction.update.label", "ipam.importAction.update.meaning"),
  conflict: present("warning", "ipam.importAction.conflict.label", "ipam.importAction.conflict.meaning"),
  error: present("destructive", "ipam.importAction.error.label", "ipam.importAction.error.meaning"),
};

export function importActionPresentation(action: string): Presentation {
  return (
    IMPORT_ACTION_PRESENTATION[action] ?? {
      label: action,
      variant: "muted",
      meaning: translate("ipam.unrecognized.verdict"),
    }
  );
}

// --------------------------------------------------------------------------
// Misc
// --------------------------------------------------------------------------

const SEARCH_KIND_MEANING: Readonly<Record<string, MessageKey>> = {
  address: "ipam.searchKind.address",
  cidr: "ipam.searchKind.cidr",
  mac: "ipam.searchKind.mac",
  text: "ipam.searchKind.text",
};

/** How the search read the query, as a sentence; "" for a kind this build does not know. */
export function searchKindMeaning(kind: string): string {
  return Object.hasOwn(SEARCH_KIND_MEANING, kind) ? translate(SEARCH_KIND_MEANING[kind] as MessageKey) : "";
}

// `describeError` USED TO LIVE HERE and has been deleted rather than rewritten
// (UX remediation cycle 4). It was `error.message`, and `ApiError` is built as
// `new ApiError(status, await response.text())` -- so a bodyless response, which
// a 503 from a proxy generally is, carries `message === ""`. Every call site in
// this module rendered an EMPTY paragraph for it: revoking a scan authorization
// or releasing an address could fail and say nothing at all. The one renderer of
// a failure is now `components/form.tsx#describeSubmitError`, which is driven off
// the status first and the body second, so the sentence exists whether or not the
// server wrote one. A second copy here is how the product ended up with four
// error styles; there is no local wrapper on purpose.
