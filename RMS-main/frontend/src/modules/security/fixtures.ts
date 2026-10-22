/**
 * Fixtures for the security-log module: the events `/security` searches, the
 * stored fields behind one row, and the evidence CSV.
 *
 * Hostnames are the core fleet's so a row here names a machine `/devices`
 * shows. The window is the last seven days with most of the traffic in the
 * last day, two failed sign-ins for one account in the last hour and a
 * lockout behind them, so the "failed logins" tile has something to count
 * and the filters have something to narrow.
 *
 * DEV-ONLY BY CONSTRUCTION: reached through `fixtures/client.ts` only, which
 * nothing imports outside `FIXTURE_MODE`.
 */

import type { FixtureMap } from "../../fixtures/client";
import type { SecurityEvent, SecurityEventDetail } from "../../lib/api";
import { ApiError } from "../../lib/api";

const MINUTE = 60_000;
const ago = (minutes: number): string => new Date(Date.now() - minutes * MINUTE).toISOString();

interface Seed {
  id: string;
  device: string;
  host: string;
  /** Minutes ago. */
  at: number;
  type: string;
  user: string;
  source: string;
  outcome: string;
  raw: Record<string, unknown>;
}

const SEEDS: Seed[] = [
  { id: "sec-1", device: "dev-7", host: "lis-dc-01", at: 12, type: "logon_failure", user: "helgar.palmieri", source: "Security/4625", outcome: "failure", raw: { EventID: 4625, LogonType: 10, Status: "0xC000006D", SubStatus: "0xC000006A", IpAddress: "185.12.4.9", WorkstationName: "UNKNOWN", FailureReason: "Unknown user name or bad password" } },
  { id: "sec-2", device: "dev-7", host: "lis-dc-01", at: 14, type: "logon_failure", user: "helgar.palmieri", source: "Security/4625", outcome: "failure", raw: { EventID: 4625, LogonType: 10, Status: "0xC000006D", SubStatus: "0xC000006A", IpAddress: "185.12.4.9", WorkstationName: "UNKNOWN", FailureReason: "Unknown user name or bad password" } },
  { id: "sec-3", device: "dev-1", host: "lis-ws-041", at: 27, type: "privilege_elevation", user: "ana.ribeiro", source: "Security/4672", outcome: "success", raw: { EventID: 4672, PrivilegeList: "SeDebugPrivilege, SeBackupPrivilege", ProcessName: "C:\\Windows\\System32\\consent.exe" } },
  { id: "sec-4", device: "dev-1", host: "lis-ws-041", at: 41, type: "logon_success", user: "ana.ribeiro", source: "Security/4624", outcome: "success", raw: { EventID: 4624, LogonType: 2, AuthenticationPackageName: "Kerberos", WorkstationName: "LIS-WS-041" } },
  { id: "sec-5", device: "dev-2", host: "lis-ws-042", at: 63, type: "logon_success", user: "tiago.sousa", source: "Security/4624", outcome: "success", raw: { EventID: 4624, LogonType: 2, AuthenticationPackageName: "Kerberos", WorkstationName: "LIS-WS-042" } },
  { id: "sec-6", device: "dev-5", host: "lis-app-02", at: 95, type: "logon_success", user: "deploy", source: "auth.log/sshd", outcome: "success", raw: { pid: 41822, method: "publickey", fingerprint: "SHA256:qJ2r…4mZo", from: "10.20.10.40", port: 51422 } },
  { id: "sec-7", device: "dev-7", host: "lis-dc-01", at: 2 * 60 + 18, type: "admin_group_change", user: "helgar.palmieri", source: "Security/4728", outcome: "success", raw: { EventID: 4728, GroupName: "Domain Admins", MemberName: "CN=Sofia Martins,OU=Engineering,DC=corp,DC=toeshee,DC=example", SubjectUserName: "helgar.palmieri" } },
  { id: "sec-8", device: "dev-14", host: "por-ws-011", at: 3 * 60 + 5, type: "logon_success", user: "sofia.martins", source: "auth.log/gdm", outcome: "success", raw: { pid: 1204, session: 3, seat: "seat0" } },
  { id: "sec-9", device: "dev-7", host: "lis-dc-01", at: 7 * 60 + 30, type: "account_lockout", user: "rui.almeida", source: "Security/4740", outcome: "failure", raw: { EventID: 4740, TargetUserName: "rui.almeida", CallerComputerName: "POR-WS-008" } },
  { id: "sec-10", device: "dev-4", host: "por-ws-008", at: 7 * 60 + 33, type: "logon_failure", user: "rui.almeida", source: "Security/4625", outcome: "failure", raw: { EventID: 4625, LogonType: 2, Status: "0xC000006D", SubStatus: "0xC000006A", WorkstationName: "POR-WS-008", FailureReason: "Unknown user name or bad password" } },
  { id: "sec-11", device: "dev-3", host: "lis-mbp-017", at: 9 * 60, type: "logoff", user: "marta.coelho", source: "unified/loginwindow", outcome: "success", raw: { subsystem: "com.apple.loginwindow", reason: "user" } },
  { id: "sec-12", device: "dev-7", host: "lis-dc-01", at: 26 * 60, type: "policy_change", user: "SYSTEM", source: "Security/4719", outcome: "success", raw: { EventID: 4719, CategoryId: "%%8274", SubcategoryGuid: "{0CCE9215-69AE-11D9-BED3-505054503030}", AuditPolicyChanges: "%%8449" } },
  { id: "sec-13", device: "dev-2", host: "lis-ws-042", at: 2 * 24 * 60 + 15, type: "logon_success", user: "tiago.sousa", source: "Security/4624", outcome: "success", raw: { EventID: 4624, LogonType: 2, AuthenticationPackageName: "Kerberos", WorkstationName: "LIS-WS-042" } },
  { id: "sec-14", device: "dev-6", host: "lis-db-01", at: 4 * 24 * 60 + 50, type: "logon_success", user: "postgres", source: "auth.log/sshd", outcome: "success", raw: { pid: 2231, method: "publickey", from: "10.20.10.12", port: 40118 } },
  { id: "sec-15", device: "dev-1", host: "lis-ws-041", at: 6 * 24 * 60 + 5, type: "logon_failure", user: "ana.ribeiro", source: "Security/4625", outcome: "failure", raw: { EventID: 4625, LogonType: 2, Status: "0xC000006D", SubStatus: "0xC000006A", WorkstationName: "LIS-WS-041", FailureReason: "Unknown user name or bad password" } },
];

function event(seed: Seed): SecurityEvent {
  return {
    id: seed.id,
    device_id: seed.device,
    hostname: seed.host,
    ts: ago(seed.at),
    event_type: seed.type,
    username: seed.user,
    source: seed.source,
    outcome: seed.outcome,
  };
}

const events = (): SecurityEvent[] => SEEDS.map(event);

function inWindow(e: SecurityEvent, from?: string, to?: string): boolean {
  const at = Date.parse(e.ts);
  if (from && at < Date.parse(from)) return false;
  if (to && at > Date.parse(to)) return false;
  return true;
}

const fixtures: FixtureMap = {
  listSecurityEvents: (params) => {
    const needle = (params.username ?? "").trim().toLowerCase();
    const matched = events().filter(
      (e) =>
        inWindow(e, params.from_ts, params.to_ts) &&
        (!params.device_id || e.device_id === params.device_id) &&
        (!params.event_type || e.event_type === params.event_type) &&
        (!needle || e.username.toLowerCase().includes(needle)),
    );
    const offset = params.offset ?? 0;
    const limit = params.limit ?? 250;
    return { items: matched.slice(offset, offset + limit), total: matched.length };
  },

  getSecurityEvent: (id): SecurityEventDetail => {
    const seed = SEEDS.find((s) => s.id === id);
    if (!seed) throw new ApiError(404, JSON.stringify({ detail: "security event not found" }));
    return { ...event(seed), raw: seed.raw };
  },

  /** The CSV the page saves: the same rows the search would show. */
  exportAccessEvidence: (params) => {
    const rows = events().filter(
      (e) =>
        inWindow(e, params.from_ts) &&
        (!params.event_type || e.event_type === params.event_type) &&
        (!params.username || e.username.toLowerCase().includes(params.username.toLowerCase())),
    );
    const lines = ["ts,hostname,username,event_type,source,outcome"].concat(
      rows.map((e) => [e.ts, e.hostname, e.username, e.event_type, e.source, e.outcome].join(",")),
    );
    return new Blob([lines.join("\n")], { type: "text/csv" });
  },
};

export default fixtures;
