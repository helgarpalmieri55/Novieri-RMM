/**
 * Presentation logic for the push-install control screens.
 *
 * **THE ONE RULE THIS FILE EXISTS FOR: EVERY GRANT STATES ITS REACH IN
 * ADDRESSES.** `ScanAuthorizationsPage.tsx` established it one privilege level
 * down — "10.0.0.0/8 is four characters and 16,777,216 addresses, and an
 * operator scanning a list of prefixes does not convert mask lengths in their
 * head" — and here the stakes are higher: misreading a scan scope puts an ICMP
 * echo somewhere unintended, and misreading one of these puts an
 * administrative credential there.
 *
 * **THE COUNT IS THE SERVER'S, NOT THIS FILE'S.** `address_count` arrives on
 * every row, computed once in `agent_push/net.py`. Deriving it again here
 * would be a second answer to "how big is this grant", and the two would
 * diverge the first time somebody fixed one of them. `parsePrefix` below
 * exists only for the PREVIEW — sizing a prefix the operator is still typing,
 * which no server round trip has seen yet — and the preview is replaced by the
 * server's number the moment the grant exists.
 *
 * **THREE STATUSES, NOT TWO** (mirroring `ipamState.scopeStatus`). "Revoked",
 * "expired" and "active" are three different facts about a grant and an
 * operator acts on each differently: a revoked grant was withdrawn by a
 * person, an expired one lapsed on a clock nobody watched, and collapsing
 * either into "inactive" hides which happened.
 */

import { translate } from "../../i18n/core";
import { lazyLabels } from "../../i18n/lazy";
import type { BadgeVariant } from "../jobs/jobState";
import { WINRM_FINGERPRINT_MARKERS, WINRM_TRANSPORT, type HostKeyOut, type PushAuthorizationOut, type PushCredentialScopeOut } from "./types";
import { intlLocale } from "../../lib/datetime";

export type GrantStatus = "active" | "expired" | "revoked";

export interface Presentation {
  readonly label: string;
  readonly variant: BadgeVariant;
  /** What this status MEANS, for a title attribute. */
  readonly meaning: string;
}

// `label` and `meaning` are accessors, read when drawn: a stored string would
// keep the language this module loaded in.
export const GRANT_STATUS_PRESENTATION: Readonly<Record<GrantStatus, Presentation>> = {
  active: {
    get label() {
      return translate("agentPush.grant.active");
    },
    variant: "success",
    get meaning() {
      return translate("agentPush.grant.activeMeaning");
    },
  },
  expired: {
    get label() {
      return translate("agentPush.grant.expired");
    },
    variant: "warning",
    get meaning() {
      return translate("agentPush.grant.expiredMeaning");
    },
  },
  revoked: {
    get label() {
      return translate("agentPush.grant.revoked");
    },
    variant: "muted",
    get meaning() {
      return translate("agentPush.grant.revokedMeaning");
    },
  },
};

export function grantStatus(
  grant: Pick<PushAuthorizationOut, "revoked_at" | "expires_at">,
  now: number = Date.now(),
): GrantStatus {
  if (grant.revoked_at !== null) return "revoked";
  if (grant.expires_at !== null && Date.parse(grant.expires_at) <= now) return "expired";
  return "active";
}

export function scopeIsBound(
  scope: Pick<PushCredentialScopeOut, "unbound_at">,
): boolean {
  return scope.unbound_at === null;
}

export interface ParsedPrefix {
  readonly bits: number;
  readonly version: 4 | 6;
}

/**
 * A CIDR the operator is still typing, for the preview only.
 *
 * A BARE ADDRESS IS ACCEPTED and means a single host — the narrowest useful
 * grant, and a common one here, because binding a credential to exactly one
 * server is a legitimate scope. The server's `parse_prefix` does the same.
 * Returns `null` rather than throwing: a half-typed prefix is the ordinary
 * state of this field, not an error to report.
 */
export function parsePrefix(literal: string): ParsedPrefix | null {
  const text = literal.trim();
  if (text === "") return null;
  const parts = text.split("/");
  // MORE THAN ONE "/" IS NOT A PREFIX, and it must fail here rather than be
  // truncated: `10.0.0.0/24/8` silently read as `10.0.0.0/24` is a grant that
  // does not match what the operator typed.
  if (parts.length > 2) return null;
  const address = parts[0] ?? "";
  const mask = parts[1];
  const version = address.includes(":") ? 6 : 4;
  if (version === 4) {
    const octets = address.split(".");
    if (octets.length !== 4) return null;
    for (const octet of octets) {
      if (!/^\d{1,3}$/.test(octet)) return null;
      if (Number(octet) > 255) return null;
    }
  } else if (!/^[0-9a-fA-F:]+$/.test(address) || address.split(":").length < 3) {
    return null;
  }
  const width = version === 4 ? 32 : 128;
  if (mask === undefined) return { bits: width, version };
  if (!/^\d{1,3}$/.test(mask)) return null;
  const bits = Number(mask);
  if (bits > width) return null;
  return { bits, version };
}

/** How many addresses a prefix reaches. `bigint`, because a /8 of IPv6 is not a number. */
export function prefixSize(prefix: ParsedPrefix): bigint {
  const width = prefix.version === 4 ? 32 : 128;
  return 2n ** BigInt(width - prefix.bits);
}

/** `16777216` -> `"16,777,216"`. Grouped, because that is the whole point. */
export function formatCount(value: bigint | number): string {
  return value.toLocaleString(intlLocale());
}

/**
 * The sentence a grant form shows before the grant exists.
 *
 * WRITTEN AS ONE FUNCTION so the preview and the confirmation cannot drift
 * into saying different things about the same prefix, and so this claim is
 * testable without rendering anything.
 */
export function describeReach(prefix: ParsedPrefix): string {
  const size = prefixSize(prefix);
  if (size === 1n) {
    return translate("agentPush.shared.reachOne");
  }
  return translate("agentPush.shared.reachMany", { size: formatCount(size) });
}

/**
 * Whether the enabled site servers serve every address of a push range.
 *
 * THE SAME RULE THE PREFLIGHT APPLIES, WIDENED FROM ONE ADDRESS TO A RANGE.
 * `agent_push/preflight.py` refuses a target (`no_site_server`) unless an
 * ENABLED site server declares a segment containing its address -- per
 * address, any server. So a range is `served` when every address in it is
 * inside some enabled server's segment (which can take more than one server
 * between them), `partial` when only some are, `none` when none are. An IPv6
 * range is never served: `parse_segment` refuses IPv6, so no declared segment
 * can contain one (`ipv6`).
 *
 * What this does NOT know, and the step says so: whether that server is
 * connected right now. The preflight does not check that either.
 *
 * Integer arithmetic on the four octets, never string prefixes: "10.200.7.0"
 * starts with "10.20" and is not inside 10.20.0.0/16.
 */
export type SiteServerCoverage = "served" | "partial" | "none" | "ipv6";

/** An IPv4 prefix (or bare address, one host) as its first and last address. */
function ipv4Range(literal: string): readonly [number, number] | null {
  const [address = "", mask] = literal.trim().split("/");
  const octets = address.split(".");
  if (octets.length !== 4 || octets.some((octet) => !/^\d{1,3}$/.test(octet) || Number(octet) > 255)) {
    return null;
  }
  const bits = mask === undefined ? 32 : Number(mask);
  if (!Number.isInteger(bits) || bits < 0 || bits > 32) return null;
  const value = octets.reduce((acc, octet) => acc * 256 + Number(octet), 0);
  const size = 2 ** (32 - bits);
  const start = Math.floor(value / size) * size;
  return [start, start + size - 1];
}

export function siteServerCoverage(
  prefix: string,
  servers: readonly { readonly segments: readonly string[]; readonly enabled: boolean }[],
): SiteServerCoverage {
  if (prefix.includes(":")) return "ipv6";
  const range = ipv4Range(prefix);
  if (range === null) return "none";
  const [start, end] = range;
  const overlapping = servers
    .filter((server) => server.enabled)
    .flatMap((server) => server.segments.map(ipv4Range))
    .filter((segment): segment is readonly [number, number] => segment !== null)
    .filter(([from, to]) => to >= start && from <= end)
    .sort((a, b) => a[0] - b[0]);
  if (overlapping.length === 0) return "none";
  // Walk the overlapping segments in order; a gap before `end` means some
  // address in the range has no server.
  let reached = start - 1;
  for (const [from, to] of overlapping) {
    if (from > reached + 1) return "partial";
    reached = Math.max(reached, to);
    if (reached >= end) return "served";
  }
  return "partial";
}

/**
 * What the credential-free WinRM probes found, counted per Windows machine
 * (design §2.4). Read from the host-key list, where a Windows probe's row
 * carries `winrm_https`/`winrm_http`/`kdc_reachable` instead of a host key.
 *
 * ONLY `winrm` ROWS COUNT. An SSH row carries the same three fields at their
 * default (false), and counting it would report a Linux server as a Windows
 * machine WinRM did not answer on -- a claim about the estate that is false.
 *
 * A field that is absent (a console newer than its API) reads as false:
 * "not observed" is never "answered".
 */
export interface WindowsProbeReadiness {
  /** Windows machines probed. */
  readonly probed: number;
  /** ...on which WinRM answered on either port. */
  readonly winrm: number;
  /** ...on which 5986 (HTTPS) answered. */
  readonly https: number;
  /** ...on which 5985 (HTTP, Kerberos-encrypted) answered. */
  readonly http: number;
  /** ...for which a domain controller answered from the site server. */
  readonly kdc: number;
}

/**
 * Is this row a Windows (WinRM) probe's rather than an SSH host key? By
 * transport, and -- belt and braces, because offering a marker for
 * confirmation as if it were a key is the one mistake this must never make --
 * by the marker a WinRM row carries where a fingerprint would be.
 */
export function isWinrmRow(row: Pick<HostKeyOut, "transport" | "observed_fingerprint">): boolean {
  return row.transport === WINRM_TRANSPORT || WINRM_FINGERPRINT_MARKERS.includes(row.observed_fingerprint);
}

/** Where a Windows target stands for a push, from its WinRM probe row. */
export type WinrmTargetState = "not_probed" | "no_winrm" | "kdc_not_found" | "ready";

/**
 * KDC REACHABILITY IS ADVISORY (review I2). The credential-free probe looks a
 * domain controller up by DNS for the target's domain; a Windows domain
 * account that names its domain controller never uses that lookup, so "not
 * found via DNS" is a warning, not a block. Only WinRM answering -- on either
 * port -- decides whether an install is offered. The server's behaviour is
 * unchanged: it never gated on either.
 */
export function winrmTargetState(row: HostKeyOut | undefined): WinrmTargetState {
  if (!row || !isWinrmRow(row)) return "not_probed";
  if (row.winrm_https !== true && row.winrm_http !== true) return "no_winrm";
  if (row.kdc_reachable !== true) return "kdc_not_found";
  return "ready";
}

/** Does this state let the console offer an install? WinRM answered. */
export function winrmInstallable(state: WinrmTargetState): boolean {
  return state === "ready" || state === "kdc_not_found";
}

/** How the domain-controller finding reads, beside the WinRM one. */
export function kdcNotFound(): string {
  return translate("agentPush.preflight.kdcNotFound");
}

/**
 * A server refusal reason (`preflight.REFUSAL_REASONS`, the host-key bound,
 * the WinRM site-server gate) as a sentence an operator acts on. The server's
 * own `reason_help` is shown beneath it as the longer remedy; a code this
 * console has not been taught falls back to that text rather than to the code.
 */
export const REFUSAL_REASON_LABELS: Readonly<Record<string, string>> = lazyLabels({
  no_asset: "agentPush.refusal.no_asset",
  ambiguous_asset: "agentPush.refusal.ambiguous_asset",
  credential_missing: "agentPush.refusal.credential_missing",
  no_authorization: "agentPush.refusal.no_authorization",
  credential_not_scoped: "agentPush.refusal.credential_not_scoped",
  no_site_server: "agentPush.refusal.no_site_server",
  no_winrm_site_server: "agentPush.refusal.no_winrm_site_server",
  site_server_too_old: "agentPush.refusal.site_server_too_old",
  no_dns_name: "agentPush.refusal.no_dns_name",
  invalid_dns_name: "agentPush.refusal.invalid_dns_name",
  wrong_credential_kind: "agentPush.refusal.wrong_credential_kind",
  no_console_url: "agentPush.refusal.no_console_url",
  outside_realm: "agentPush.refusal.outside_realm",
  realm_not_recorded: "agentPush.refusal.realm_not_recorded",
  realm_changed: "agentPush.refusal.realm_changed",
  host_key_unconfirmed: "agentPush.refusal.host_key_unconfirmed",
  host_key_changed: "agentPush.refusal.host_key_changed",
});

export function refusalReasonLabel(code: string, help?: string): string {
  const known = REFUSAL_REASON_LABELS[code];
  if (known) return known;
  if (help) return help.charAt(0).toUpperCase() + help.slice(1) + (help.endsWith(".") ? "" : ".");
  return translate("agentPush.refusal.fallback");
}

export function windowsProbeReadiness(rows: readonly HostKeyOut[]): WindowsProbeReadiness {
  const windows = rows.filter(isWinrmRow);
  const count = (predicate: (row: HostKeyOut) => boolean) => windows.filter(predicate).length;
  return {
    probed: windows.length,
    winrm: count((row) => row.winrm_https === true || row.winrm_http === true),
    https: count((row) => row.winrm_https === true),
    http: count((row) => row.winrm_http === true),
    kdc: count((row) => row.kdc_reachable === true),
  };
}

/**
 * THE HOST KEYS BY (CLIENT, ADDRESS), NOT BY ADDRESS ALONE. Two clients'
 * machines can share one address (MSP clients phase 1), and each has its own
 * key. A push is made in its credential's client, so the row that speaks for
 * an address is that client's, else the global one -- recorded before its
 * machine was known -- exactly as the server picks the key an install is
 * verified against (`host_key.confirmed_key_for`). Another client's row is
 * never this push's.
 */
export function hostKeysForClient(
  rows: readonly HostKeyOut[],
  client: string | null,
): Map<string, HostKeyOut> {
  const keyed = new Map<string, HostKeyOut>();
  for (const row of rows) keyed.set(`${row.client_id ?? ""}|${row.address}`, row);
  const byAddress = new Map<string, HostKeyOut>();
  for (const row of rows) {
    const own = client === null ? undefined : keyed.get(`${client}|${row.address}`);
    const global = keyed.get(`|${row.address}`);
    const chosen = own ?? global;
    if (chosen) byAddress.set(row.address, chosen);
  }
  return byAddress;
}
