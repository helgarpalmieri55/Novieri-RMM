// Wire types for the site-server console (docs/modules/site-server/spec.md §11).
//
// A SITE SERVER IS A PROMOTED `Device`. There is no separate identity here and
// no second kind of thing to render: `device_id` is the machine, `agent_status`
// and `agent_last_seen_at` are that machine's ordinary agent state, and the
// registry row is the declaration on top.
//
// `evidence` on a segment check is NOT decoration. "No mismatches" and "we could
// not look" are opposite answers, and with IPAM absent all three lists are empty
// for a reason that has nothing to do with the declaration being correct. The UI
// must render them differently, and `SegmentCheck.tsx` does.

import { lazyLabels } from "../../i18n/lazy";

export const SITE_READ = "site.read";
export const SITE_MANAGE = "site.manage";
export const SITE_WAKE = "site.wake";

/**
 * The roles a site server may declare.
 *
 * `cache` and `pxe` are admitted by the DATABASE CHECK — that vocabulary is the
 * forward seam for the deferred cache and PXE slices, so landing them will not
 * need a migration to widen a shipped constraint — and are REFUSED BY THE
 * SERVICE with a 400 naming spec §4.4, whose invariant amendment has not been
 * accepted. So the promote dialog offers `wake` and nothing else: an option the
 * server rejects is a bug report waiting to be filed.
 */
export const OFFERABLE_ROLES = ["wake"] as const;
export type SiteServerRole = (typeof OFFERABLE_ROLES)[number];

/**
 * The role tokens, written the way an operator says them.
 *
 * `roles` is a wire vocabulary: the list and the detail page printed
 * `server.roles.join(", ")` straight into a cell, so the console read "wake"
 * in mono — the API's word for the capability, not the product's. An unmapped
 * token is sentence-cased rather than passed through, so `cache` and `pxe`
 * arrive readable on the day the service stops refusing them.
 */
const ROLE_LABELS: Readonly<Record<string, string>> = lazyLabels({
  wake: "siteServers.roles.wake",
  cache: "siteServers.roles.cache",
  pxe: "siteServers.roles.pxe",
});

export function roleLabel(role: string): string {
  const known = ROLE_LABELS[role.toLowerCase()];
  if (known !== undefined) return known;
  return role.charAt(0).toUpperCase() + role.slice(1);
}

/** The roles of one server, in words, in the order the server declared them. */
export function roleLabels(roles: readonly string[]): string {
  return roles.map(roleLabel).join(", ");
}

export interface SiteServerOut {
  id: string;
  /** The device's client: a site server serves its own client's segments. */
  client_id: string;
  device_id: string;
  name: string;
  segments: string[];
  roles: string[];
  enabled: boolean;
  notes: string;
  /** One of the device's client's sites, grouping only (was `ipam_site_id`). */
  site_id: string | null;
  agent_status: string | null;
  agent_last_seen_at: string | null;
}

export interface SiteServerCreate {
  device_id: string;
  segments: string[];
  roles?: string[];
  name?: string | null;
  site_id?: string | null;
  notes?: string;
}

export interface SiteServerUpdate {
  name?: string | null;
  segments?: string[] | null;
  roles?: string[] | null;
  notes?: string | null;
  enabled?: boolean | null;
  site_id?: string | null;
}

export interface SiteServerFilters {
  enabled?: boolean;
  site_id?: string;
  /** Containment, not equality: `10.20.7.0/24` finds a server declared for `10.20.0.0/16`. */
  segment?: string;
}

export interface SegmentCheckOut {
  site_server_id: string;
  device_id: string;
  /**
   * `"ipam"` — the three lists below are real.
   * `"unavailable"` — IPAM is not installed, nothing could be looked up, and
   * the empty lists mean NOTHING. Never render this as "no mismatches".
   */
  evidence: "ipam" | "unavailable";
  matched: string[];
  /**
   * Declared, with no current inventory observation inside.
   *
   * NOT an error and not a mismatch: promotion deliberately accepts an
   * operator's claim without verifying it (§3.3), and a freshly promoted server
   * has no observation until inventory next runs. What it does mean is that the
   * server has NO SWEEP PREFERENCE on that segment yet — preference reorders the
   * candidate set and never adds to it (§0.8).
   */
  declared_unobserved: string[];
  /** Observed on this device, inside no declared segment. Usually a second NIC. */
  observed_undeclared: string[];
}

export interface SiteWakeRequest {
  target_mac: string;
  /** Optional only when the site server declares exactly one segment. */
  segment?: string | null;
}

export interface SiteWakeOut {
  job_id: string;
  device_id: string;
  /** SERVER-COMPUTED. The client never supplies this. */
  broadcast: string;
  target_mac: string;
  expires_at: string;
}
