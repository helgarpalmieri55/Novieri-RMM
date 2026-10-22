/**
 * Fixtures for People: a small directory, one person mid-way through a
 * leaver checklist, and one seat nobody could be matched to.
 *
 * Dev-only: reached through `fixtures/client.ts` behind `FIXTURE_MODE`.
 */

import type { FixtureMap } from "../../fixtures/client";
import { ApiError } from "../../lib/api";
import type { PersonDetailOut, PersonListRow, UnmatchedOut } from "./types";

const DAY = 24 * 60 * 60_000;
const ago = (days: number): string => new Date(Date.now() - days * DAY).toISOString();

const PEOPLE: PersonListRow[] = [
  {
    id: "person-maria",
    display_name: "Maria Garcia",
    email: "maria.garcia@toeshee.example",
    user_id: null,
    status: "leaving",
    department: "Finance",
    job_title: "Accountant",
    start_date: "2022-03-01",
    end_date: null,
    source: "operator",
    created_at: ago(400),
    device_count: 1,
  },
  {
    id: "person-sam",
    display_name: "Sam Okafor",
    email: "sam.okafor@toeshee.example",
    user_id: null,
    status: "active",
    department: "Operations",
    job_title: "Coordinator",
    start_date: "2024-06-10",
    end_date: null,
    source: "operator",
    created_at: ago(200),
    device_count: 0,
  },
];

const UNMATCHED: UnmatchedOut = {
  seats: [
    {
      seat_id: "seat-pat",
      subscription_id: "sub-zoom",
      service_name: "Zoom",
      assignee_name: "Pat Doe",
      assignee_email: "pat.doe@partner.example",
    },
  ],
  devices: [],
  allocations: [],
};

function detail(personId: string): PersonDetailOut {
  const row = PEOPLE.find((p) => p.id === personId);
  if (!row) throw new ApiError(404, "person not found");
  const { device_count: _count, ...person } = row;
  const leaving = person.status === "leaving";
  return {
    person,
    logins: leaving ? [{ id: "login-1", login: "azuread\\mariagarcia", created_at: ago(30) }] : [],
    account: null,
    devices: leaving
      ? [
          {
            device_id: "dev-fin-042",
            hostname: "FIN-LT-042",
            os_family: "windows",
            lifecycle: "in_service",
            device_group_id: null,
            group_name: "Finance laptops",
            last_seen_at: ago(0.1),
            source: "inventory",
            observed_login: "AzureAD\\MariaGarcia",
            observed_at: ago(0.1),
          },
        ]
      : [],
    licences: [],
    seats: leaving
      ? [
          {
            seat_id: "seat-maria-slack",
            subscription_id: "sub-slack",
            service_name: "Slack",
            plan: "Pro",
            assigned_at: ago(300),
          },
        ]
      : [],
    tickets: [],
    checklist: leaving
      ? {
          id: "checklist-maria",
          kind: "leaver",
          status: "open",
          started_at: ago(1),
          closed_at: null,
          can_complete: false,
          steps: [
            ["access", "Turn off their sign-in", true, "They have no sign-in", "done"],
            ["seats", "Take back their subscriptions", false, "Still holds 1 subscription", "pending"],
            ["licences", "Take back their licences", true, "Holds no licences", "done"],
            ["devices", "Collect or reassign their computers", false, "1 computer is still theirs and in use", "pending"],
            ["credentials", "Change passwords they have seen", true, "Nothing they revealed is still unchanged", "done"],
            ["tickets", "Close their open tickets", true, "No open tickets", "done"],
          ].map(([key, title, checked, evidence, state], position) => ({
            key: key as string,
            position,
            title: title as string,
            help: "",
            link: "/devices",
            destructive: key !== "tickets",
            checked: checked as boolean,
            evidence: evidence as string,
            marked: "pending" as const,
            state: state as "done" | "pending",
            note: "",
            updated_at: null,
          })),
        }
      : null,
    past_checklists: [],
  };
}

const fixtures: FixtureMap = {
  listPeople: () => PEOPLE,
  getPerson: (personId) => detail(personId),
  listUnmatched: () => UNMATCHED,
  getDevicePerson: (deviceId) => ({
    device_id: deviceId,
    person: null,
    source: null,
    observed_login: null,
    observed_at: null,
  }),
  personForUser: () => {
    throw new ApiError(404, "person not found");
  },
};

export default fixtures;
