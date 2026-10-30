// Mirrors backend/app/modules/people/schema.py.

export type PersonStatus = "starting" | "active" | "leaving" | "left";
export type ChecklistKind = "starter" | "leaver";
export type StepState = "pending" | "done" | "skipped";

export interface PersonOut {
  id: string;
  /** The one client the person works for (MSP clients phase 2). Never changes. */
  client_id: string;
  display_name: string;
  email: string | null;
  user_id: string | null;
  status: PersonStatus;
  department: string | null;
  job_title: string | null;
  start_date: string | null;
  end_date: string | null;
  source: string;
  created_at: string;
}

export interface PersonListRow extends PersonOut {
  device_count: number;
}

export interface PersonCreate {
  /** Required: a person belongs to exactly one client. */
  client_id: string;
  display_name: string;
  email?: string | null;
  department?: string | null;
  job_title?: string | null;
  start_date?: string | null;
}

/** A person's client never changes, so an edit cannot carry one. */
export type PersonUpdate = Partial<Omit<PersonCreate, "client_id">> & { end_date?: string | null };

export interface LoginOut {
  id: string;
  login: string;
  created_at: string;
}

export interface AccountOut {
  user_id: string;
  /** `staff` signs in to the console; a `contact` is a client's, switched off through Clients. */
  kind: "staff" | "contact";
  email: string;
  status: string;
  last_login_at: string | null;
  console: boolean;
  roles: string[] | null;
}

export interface PersonDeviceOut {
  device_id: string;
  hostname: string | null;
  os_family: string | null;
  lifecycle: string;
  device_group_id: string | null;
  group_name: string | null;
  last_seen_at: string | null;
  source: string;
  observed_login: string | null;
  observed_at: string | null;
}

export interface PersonLicenceOut {
  allocation_id: string;
  license_id: string;
  product_name: string;
  through_device_id: string | null;
  allocated_at: string;
}

export interface PersonSeatOut {
  seat_id: string;
  subscription_id: string;
  service_name: string;
  plan: string;
  assigned_at: string;
}

export interface PersonTicketOut {
  ticket_id: string;
  number: string;
  subject: string;
  status: string;
  priority: string;
  created_at: string;
}

export interface StepOut {
  key: string;
  position: number;
  title: string;
  help: string;
  link: string;
  destructive: boolean;
  /** true/false from live data; null when the step cannot tell by itself. */
  checked: boolean | null;
  evidence: string;
  marked: StepState;
  state: StepState;
  note: string;
  updated_at: string | null;
}

export interface ChecklistOut {
  id: string;
  kind: ChecklistKind;
  status: "open" | "completed" | "cancelled";
  started_at: string;
  closed_at: string | null;
  steps: StepOut[];
  can_complete: boolean;
}

export interface PastChecklistOut {
  id: string;
  kind: string;
  status: string;
  closed_at: string | null;
}

export interface PersonDetailOut {
  person: PersonOut;
  logins: LoginOut[];
  account: AccountOut | null;
  devices: PersonDeviceOut[];
  /** null: the viewer cannot read licences. */
  licences: PersonLicenceOut[] | null;
  seats: PersonSeatOut[] | null;
  tickets: PersonTicketOut[] | null;
  checklist: ChecklistOut | null;
  past_checklists: PastChecklistOut[];
}

export interface DevicePersonOut {
  device_id: string;
  person: PersonOut | null;
  source: "inventory" | "operator" | null;
  observed_login: string | null;
  observed_at: string | null;
}

export interface UnmatchedOut {
  seats: {
    seat_id: string;
    subscription_id: string;
    service_name: string;
    assignee_name: string;
    assignee_email: string;
  }[];
  devices: {
    device_id: string;
    hostname: string | null;
    owner: string | null;
    observed_login: string | null;
  }[];
  allocations: {
    allocation_id: string;
    license_id: string;
    product_name: string;
    user_id: string;
  }[];
}
