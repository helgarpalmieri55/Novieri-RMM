/**
 * Fixtures for the change-management module: the change requests, their
 * approval decisions, the actions they are linked to and the calendar view
 * over their windows.
 *
 * ONE LIST, EVERY NUMBER DERIVED FROM IT. The three tiles on `/changes` count
 * the same rows the table under them shows, the approval queue is the
 * `submitted` subset of the same list, and the calendar is the same rows
 * cut to a window, with conflicts computed from the overlaps that actually
 * exist. The two changes the v3 design names (`CR-118 Upgrade core switch
 * firmware`, `CR-117 Rotate VPN certificates`) are here with the states the
 * design draws them in; `chg-1` is the one the gate opens.
 *
 * `chg-1` IS RAISED BY SOMEONE ELSE ON PURPOSE. The fixture session is
 * `usr-helgar` (`fixtures/core.ts`), and the segregation-of-duties guard bars
 * a requester from deciding their own change, so a queue whose only row was
 * Helgar's would render both decision controls disabled.
 *
 * DEV-ONLY BY CONSTRUCTION: reached through `fixtures/client.ts` only, which
 * nothing imports outside `FIXTURE_MODE`.
 */

import type { FixtureMap } from "../../fixtures/client";
import { ApiError } from "../../lib/api";
import type { Approval, CalendarConflict, CalendarEntry, ChangeRequest, Link } from "./types";

const MINUTE = 60_000;
const HOUR = 60;
const DAY = 24 * HOUR;
/** An ISO timestamp `minutes` before the call. */
const ago = (minutes: number): string => new Date(Date.now() - minutes * MINUTE).toISOString();
/** An ISO timestamp `minutes` after the call. */
const ahead = (minutes: number): string => new Date(Date.now() + minutes * MINUTE).toISOString();

const notFound = (what: string) => new ApiError(404, JSON.stringify({ detail: `${what} not found` }));

/* ----------------------------------------------------------------- changes */

interface ChangeSeed {
  id: string;
  type: string;
  title: string;
  description: string;
  risk: string;
  state: string;
  /** Minutes from now to the window's start (negative is the past), and its length. */
  startsIn: number | null;
  lengthMinutes: number;
  rollback: string | null;
  createdBy: string;
  createdDaysAgo: number;
  affected: Record<string, unknown>[];
}

const SEEDS: ChangeSeed[] = [
  {
    id: "chg-1",
    type: "standard",
    title: "CR-118 Upgrade core switch firmware",
    description:
      "Upgrade lis-sw-core-1 from 9.3.4 to 9.3.8 to close the CVE the vendor published in August. Both uplinks stay up during the reload; the rack loses connectivity for the reboot only.",
    risk: "high",
    state: "submitted",
    startsIn: 7 * DAY + 2 * HOUR,
    lengthMinutes: 40,
    rollback: "Boot the previous image from the second partition and reapply the saved running config.",
    createdBy: "usr-mateus",
    createdDaysAgo: 3,
    affected: [{ kind: "device", id: "dev-10", hostname: "lis-sw-core-1" }],
  },
  {
    id: "chg-2",
    type: "normal",
    title: "CR-117 Rotate VPN certificates",
    description: "Replace the VPN gateway certificate before it expires on the 21st and push the new CA bundle to every managed workstation.",
    risk: "low",
    state: "completed",
    startsIn: -(7 * DAY + 2 * HOUR),
    lengthMinutes: 60,
    rollback: "Reinstate the previous certificate from the vault; it stays valid for another twelve days.",
    createdBy: "usr-ines",
    createdDaysAgo: 14,
    affected: [{ kind: "device", id: "dev-7", hostname: "lis-dc-01" }],
  },
  {
    id: "chg-3",
    type: "standard",
    title: "CR-119 Apply the Ubuntu security pocket to lis-app-02",
    description: "Install the outstanding security updates on the application server inside the approved patch window and restart the affected services.",
    risk: "medium",
    state: "approved",
    startsIn: 3 * DAY + 4 * HOUR,
    lengthMinutes: 90,
    rollback: "Hold the previous kernel in GRUB; downgrade the packages from the local mirror if a service fails its health check.",
    createdBy: "usr-helgar",
    createdDaysAgo: 5,
    affected: [{ kind: "device", id: "dev-5", hostname: "lis-app-02" }],
  },
  {
    id: "chg-4",
    type: "emergency",
    title: "CR-116 Replace the failed power supply in por-fs-01",
    description: "The second PSU failed overnight and the file server is on one feed. Swap the unit from spares and confirm both feeds report healthy.",
    risk: "high",
    state: "implementing",
    startsIn: -(2 * HOUR),
    lengthMinutes: 180,
    rollback: null,
    createdBy: "usr-rui",
    createdDaysAgo: 0,
    affected: [{ kind: "device", id: "dev-8", hostname: "por-fs-01" }],
  },
  {
    id: "chg-5",
    type: "normal",
    title: "CR-120 Move the finance shares to the new NAS",
    description: "Migrate the finance department's shares to the new storage and repoint the drive mappings through group policy.",
    risk: "medium",
    state: "draft",
    startsIn: 12 * DAY + 20 * HOUR,
    lengthMinutes: 240,
    rollback: "Re-enable the old share paths; the source stays read-only for thirty days.",
    createdBy: "usr-helgar",
    createdDaysAgo: 1,
    affected: [{ kind: "device_group", id: "grp-workstations" }],
  },
  {
    id: "chg-6",
    type: "standard",
    title: "CR-115 Decommission lis-ws-019",
    description: "Wipe and retire the workstation whose certificate was revoked in July.",
    risk: "low",
    state: "cancelled",
    startsIn: -(20 * DAY),
    lengthMinutes: 30,
    rollback: null,
    createdBy: "usr-mateus",
    createdDaysAgo: 24,
    affected: [{ kind: "device", id: "dev-9", hostname: "lis-ws-019" }],
  },
  {
    id: "chg-7",
    type: "standard",
    title: "CR-121 Extend the guest wireless VLAN to the Porto office",
    description: "Trunk VLAN 60 to the Porto access switches and publish the guest SSID there.",
    risk: "medium",
    state: "submitted",
    startsIn: 3 * DAY + 5 * HOUR,
    lengthMinutes: 60,
    rollback: "Remove VLAN 60 from the trunk; the SSID is withdrawn with it.",
    createdBy: "usr-rui",
    createdDaysAgo: 2,
    affected: [{ kind: "device", id: "dev-10", hostname: "lis-sw-core-1" }],
  },
];

function change(seed: ChangeSeed): ChangeRequest {
  const start = seed.startsIn === null ? null : ahead(seed.startsIn);
  const end = seed.startsIn === null ? null : ahead(seed.startsIn + seed.lengthMinutes);
  return {
    id: seed.id,
    type: seed.type,
    title: seed.title,
    description: seed.description,
    risk: seed.risk,
    affected: seed.affected,
    window_start: start,
    window_end: end,
    rollback_plan: seed.rollback,
    state: seed.state,
    created_by: seed.createdBy,
    created_at: ago(seed.createdDaysAgo * DAY + 3 * HOUR),
  };
}

const changes = (): ChangeRequest[] => SEEDS.map(change);

function findChange(id: string): ChangeRequest {
  const found = changes().find((row) => row.id === id);
  if (!found) throw notFound("change");
  return found;
}

const listChanges: FixtureMap["listChanges"] = () => changes();
const getChange: FixtureMap["getChange"] = (id) => findChange(id);

const createChange: FixtureMap["createChange"] = (body) => ({
  id: `chg-${Date.now()}`,
  type: body.type,
  title: body.title,
  description: body.description,
  risk: body.risk,
  affected: body.affected ?? [],
  window_start: body.window_start ?? null,
  window_end: body.window_end ?? null,
  rollback_plan: body.rollback_plan ?? null,
  state: "draft",
  created_by: "usr-helgar",
  created_at: ago(0),
});

const updateChange: FixtureMap["updateChange"] = (id, body) => ({ ...findChange(id), ...body });
const submitChange: FixtureMap["submitChange"] = (id) => ({ ...findChange(id), state: "submitted" });
const decision =
  (verdict: "approved" | "rejected"): FixtureMap["approveChange"] =>
  (id, body) => {
    findChange(id);
    return {
      id: `apr-${Date.now()}`,
      approver_id: "usr-helgar",
      decision: verdict,
      comment: body.comment ?? null,
      is_retrospective: false,
      decided_at: ago(0),
    };
  };
const approveChange = decision("approved");
const rejectChange = decision("rejected");

const NEXT_STATE: Record<string, string> = {
  begin: "implementing",
  complete: "completed",
  cancel: "cancelled",
  revise: "draft",
};

const transitionChange: FixtureMap["transitionChange"] = (id, body) => ({
  ...findChange(id),
  state: NEXT_STATE[body.action] ?? findChange(id).state,
});

/* --------------------------------------------------------------- approvals */

const APPROVALS: Record<string, Approval[]> = {
  "chg-2": [
    {
      id: "apr-1",
      approver_id: "usr-helgar",
      decision: "approved",
      comment: "Window agreed with the network team.",
      is_retrospective: false,
      decided_at: ago(10 * DAY),
    },
  ],
  "chg-3": [
    {
      id: "apr-2",
      approver_id: "usr-ines",
      decision: "approved",
      comment: null,
      is_retrospective: false,
      decided_at: ago(2 * DAY),
    },
  ],
  "chg-6": [
    {
      id: "apr-3",
      approver_id: "usr-ines",
      decision: "rejected",
      comment: "The device is still holding finance data; wipe it through the retirement process first.",
      is_retrospective: false,
      decided_at: ago(22 * DAY),
    },
  ],
};

const listChangeApprovals: FixtureMap["listChangeApprovals"] = (id) => {
  findChange(id);
  return APPROVALS[id] ?? [];
};

/* ------------------------------------------------------------------- links */

const LINKS: Record<string, Link[]> = {
  "chg-2": [
    {
      id: "lnk-1",
      action_type: "deployment",
      action_id: "06a85c19-55e2-7f3c-8000-831c7bdba357",
      linked_at: ago(7 * DAY + HOUR),
      linked_by: "usr-ines",
    },
  ],
  "chg-3": [
    {
      id: "lnk-2",
      action_type: "patch_approval",
      action_id: "1c3d5e7f-9a1b-4c3d-8e5f-2a4b6c8d0e1f",
      linked_at: ago(2 * DAY),
      linked_by: "usr-helgar",
    },
  ],
};

const listChangeLinks: FixtureMap["listChangeLinks"] = (id) => {
  findChange(id);
  return LINKS[id] ?? [];
};

const addChangeLink: FixtureMap["addChangeLink"] = (id, body) => {
  findChange(id);
  return {
    id: `lnk-${Date.now()}`,
    action_type: body.action_type,
    action_id: body.action_id,
    linked_at: ago(0),
    linked_by: "usr-helgar",
  };
};

const removeChangeLink: FixtureMap["removeChangeLink"] = () => undefined;

/* ---------------------------------------------------------------- calendar */

const overlaps = (a: CalendarEntry, b: CalendarEntry): boolean =>
  a.window_start !== null &&
  a.window_end !== null &&
  b.window_start !== null &&
  b.window_end !== null &&
  a.window_start < b.window_end &&
  b.window_start < a.window_end;

const getChangeCalendar: FixtureMap["getChangeCalendar"] = (from, to) => {
  const entries: CalendarEntry[] = changes()
    .filter((row) => row.window_start !== null && row.window_end !== null)
    .filter((row) => (row.window_end as string) >= from && (row.window_start as string) <= to)
    .filter((row) => row.state !== "cancelled" && row.state !== "rejected")
    .map((row) => ({
      change_id: row.id,
      title: row.title,
      type: row.type,
      risk: row.risk,
      state: row.state,
      window_start: row.window_start,
      window_end: row.window_end,
    }))
    .sort((a, b) => (a.window_start as string).localeCompare(b.window_start as string));
  const conflicts: CalendarConflict[] = [];
  for (let i = 0; i < entries.length; i += 1) {
    for (let j = i + 1; j < entries.length; j += 1) {
      const a = entries[i];
      const b = entries[j];
      if (a && b && overlaps(a, b)) conflicts.push({ change_a: a.change_id, change_b: b.change_id });
    }
  }
  return { entries, conflicts };
};

/* ---------------------------------------------------------------- evidence */

const getChangeEvidence: FixtureMap["getChangeEvidence"] = (id) => ({
  change: findChange(id),
  approvals: APPROVALS[id] ?? [],
  links: LINKS[id] ?? [],
  audit_refs: [`audit-${id}-created`, `audit-${id}-submitted`],
});

const listChangeGates: FixtureMap["listChangeGates"] = () => [];

export const fixtures: FixtureMap = {
  listChanges,
  listChangeGates,
  getChange,
  createChange,
  updateChange,
  submitChange,
  approveChange,
  rejectChange,
  transitionChange,
  listChangeApprovals,
  listChangeLinks,
  addChangeLink,
  removeChangeLink,
  getChangeCalendar,
  getChangeEvidence,
};

export default fixtures;
