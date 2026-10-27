import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../../../lib/api";
import type { ApiClient, Me } from "../../../lib/api";
import { DeviceAssignedTo, PersonLinkForUser } from "../links";
import { PersonPage } from "../PersonPage";
import { UnmatchedPage } from "../UnmatchedPage";
import type {
  ChecklistOut,
  DevicePersonOut,
  PersonDetailOut,
  PersonListRow,
  UnmatchedOut,
} from "../types";

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

function renderAt(ui: ReactElement, path: string, route: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[route]}>
        <Routes>
          <Route path={path} element={ui} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function me(...permissions: string[]): Me {
  return {
    user_id: "u1",
    org_id: "o1",
    email: "it@corp.example",
    display_name: "IT",
    permissions,
    timezone: "UTC",
  };
}

const MARIA: PersonListRow = {
  id: "p1",
  client_id: "o1",
  display_name: "Maria Garcia",
  email: "maria@corp.example",
  user_id: "u9",
  status: "active",
  department: "Finance",
  job_title: "Accountant",
  start_date: "2024-01-08",
  end_date: null,
  source: "account",
  created_at: "2026-09-01T09:00:00Z",
  device_count: 1,
};

const NOTHING_UNMATCHED: UnmatchedOut = { seats: [], devices: [], allocations: [] };

function leaverChecklist(): ChecklistOut {
  return {
    id: "c1",
    kind: "leaver",
    status: "open",
    started_at: "2026-09-20T09:00:00Z",
    closed_at: null,
    can_complete: false,
    steps: [
      {
        key: "access",
        position: 0,
        title: "Turn off their sign-in",
        help: "They can no longer use the console or the portal.",
        link: "/settings",
        destructive: true,
        checked: false,
        evidence: "Their sign-in still works",
        marked: "pending",
        state: "pending",
        note: "",
        updated_at: null,
      },
      {
        key: "tickets",
        position: 1,
        title: "Close their open tickets",
        help: "Resolve or close anything they raised.",
        link: "/service-desk",
        destructive: false,
        checked: true,
        evidence: "No open tickets",
        marked: "pending",
        state: "done",
        note: "",
        updated_at: null,
      },
    ],
  };
}

function detail(overrides: Partial<PersonDetailOut> = {}): PersonDetailOut {
  const { device_count: _count, ...person } = MARIA;
  return {
    person,
    logins: [{ id: "l1", login: "corp\\maria", created_at: "2026-09-01T09:00:00Z" }],
    account: {
      user_id: "u9",
      kind: "staff",
      email: "maria@corp.example",
      status: "active",
      last_login_at: "2026-09-22T08:00:00Z",
      console: false,
      roles: ["requester"],
    },
    devices: [
      {
        device_id: "d1",
        hostname: "FIN-LT-042",
        os_family: "windows",
        lifecycle: "in_service",
        device_group_id: null,
        group_name: null,
        last_seen_at: null,
        source: "inventory",
        observed_login: "CORP\\maria",
        observed_at: null,
      },
    ],
    licences: [],
    seats: [
      {
        seat_id: "s1",
        subscription_id: "sub1",
        service_name: "Slack",
        plan: "Pro",
        assigned_at: "2026-02-01T09:00:00Z",
      },
    ],
    tickets: null,
    checklist: null,
    past_checklists: [],
    ...overrides,
  };
}

function api(overrides: Partial<Record<keyof ApiClient, unknown>>, perms: string[]): ApiClient {
  return {
    me: vi.fn().mockResolvedValue(me(...perms)),
    listUnmatched: vi.fn().mockResolvedValue(NOTHING_UNMATCHED),
    ...overrides,
  } as unknown as ApiClient;
}

beforeEach(() => {
  mockUseApi.mockReset();
});

describe("People is not a page of its own any more (operator decision 2026-09-23)", () => {
  // Read from App.tsx itself, as `ipam.test.tsx` reads its route table: a
  // list of routes in this file would rot beside the real one.
  const app = readFileSync(join(import.meta.dirname, "..", "..", "..", "App.tsx"), "utf8");

  it("sends a bookmark of the old list to Devices, replacing the history entry", () => {
    expect(app).toMatch(/path="\/people"\s+element=\{<Navigate to="\/devices" replace \/>\}/);
    expect(app).not.toMatch(/<PeoplePage/);
  });

  it("keeps the person page, which licences, seats and devices still link to", () => {
    expect(app).toMatch(/path="\/people\/:personId"/);
  });
});

describe("PersonPage", () => {
  it("shows what they hold and says a hidden section is hidden, not empty", async () => {
    const client = api({ getPerson: vi.fn().mockResolvedValue(detail()) }, ["people.read"]);
    mockUseApi.mockReturnValue(client);
    renderAt(<PersonPage />, "/people/:personId", "/people/p1");

    expect(await screen.findByRole("heading", { name: "Maria Garcia" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "FIN-LT-042" })).toHaveAttribute("href", "/devices/d1");
    expect(screen.getByRole("link", { name: "Slack" })).toHaveAttribute("href", "/subscriptions/sub1");
    expect(screen.getByText("From who signs in")).toBeInTheDocument();
    expect(screen.getByText("Hidden: you can't view tickets.")).toBeInTheDocument();
    expect(screen.getByText("The portal only")).toBeInTheDocument();
    // Read-only: no checklist or release controls.
    expect(screen.queryByRole("button", { name: /Start/ })).toBeNull();
    expect(screen.queryByRole("button", { name: "Release" })).toBeNull();
  });

  it("offers the leaver checklist only with people.offboard", async () => {
    const client = api({ getPerson: vi.fn().mockResolvedValue(detail()) }, [
      "people.read",
      "people.manage",
    ]);
    mockUseApi.mockReturnValue(client);
    renderAt(<PersonPage />, "/people/:personId", "/people/p1");
    expect(await screen.findByRole("button", { name: "Start new starter" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Start leaver" })).toBeNull();
  });

  it("releases a seat only after confirming, through the subscriptions endpoint", async () => {
    const user = userEvent.setup();
    const releaseSeat = vi.fn().mockResolvedValue({});
    const client = api({ getPerson: vi.fn().mockResolvedValue(detail()), releaseSeat }, [
      "people.read",
      "subscriptions.manage",
    ]);
    mockUseApi.mockReturnValue(client);
    renderAt(<PersonPage />, "/people/:personId", "/people/p1");

    await user.click(await screen.findByRole("button", { name: "Release" }));
    expect(releaseSeat).not.toHaveBeenCalled();
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Release" }));
    expect(releaseSeat).toHaveBeenCalledWith("s1");
  });

  it("turns a staff sign-in off through Settings' user endpoint", async () => {
    const user = userEvent.setup();
    const setUserEnabled = vi.fn().mockResolvedValue({});
    const updateContact = vi.fn();
    const client = api(
      { getPerson: vi.fn().mockResolvedValue(detail()), setUserEnabled, updateContact },
      ["people.read", "users.manage"],
    );
    mockUseApi.mockReturnValue(client);
    renderAt(<PersonPage />, "/people/:personId", "/people/p1");

    await user.click(await screen.findByRole("button", { name: "Turn off sign-in" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Turn off sign-in" }));
    await vi.waitFor(() => expect(setUserEnabled).toHaveBeenCalledWith("u9", false));
    expect(updateContact).not.toHaveBeenCalled();
  });

  it("turns a client contact's sign-in off as a contact, with clients.manage", async () => {
    const user = userEvent.setup();
    const setUserEnabled = vi.fn();
    const updateContact = vi.fn().mockResolvedValue({});
    const account = detail().account;
    if (account === null) throw new Error("the fixture has an account");
    const contact = detail({ account: { ...account, kind: "contact" } });
    // `users.manage` alone: `/users` is staff-only, so it offers nothing here.
    const staffAdmin = api({ getPerson: vi.fn().mockResolvedValue(contact) }, [
      "people.read",
      "users.manage",
    ]);
    mockUseApi.mockReturnValue(staffAdmin);
    const { unmount } = renderAt(<PersonPage />, "/people/:personId", "/people/p1");
    expect(await screen.findByRole("heading", { name: "Maria Garcia" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Turn off sign-in" })).toBeNull();
    unmount();

    const client = api(
      { getPerson: vi.fn().mockResolvedValue(contact), setUserEnabled, updateContact },
      ["people.read", "clients.manage"],
    );
    mockUseApi.mockReturnValue(client);
    renderAt(<PersonPage />, "/people/:personId", "/people/p1");
    await user.click(await screen.findByRole("button", { name: "Turn off sign-in" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Turn off sign-in" }));
    await vi.waitFor(() =>
      expect(updateContact).toHaveBeenCalledWith("u9", { status: "disabled" }),
    );
    expect(setUserEnabled).not.toHaveBeenCalled();
  });

  it("asks why before marking a step done that the data disagrees with", async () => {
    const user = userEvent.setup();
    const updateChecklistStep = vi.fn().mockResolvedValue(leaverChecklist());
    const client = api(
      {
        getPerson: vi.fn().mockResolvedValue(
          detail({ checklist: leaverChecklist(), person: { ...detail().person, status: "leaving" } }),
        ),
        updateChecklistStep,
      },
      ["people.read", "people.manage", "people.offboard"],
    );
    mockUseApi.mockReturnValue(client);
    renderAt(<PersonPage />, "/people/:personId", "/people/p1");

    expect(await screen.findByText("Leaver checklist")).toBeInTheDocument();
    expect(screen.getByText("1 of 2 steps left.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Finish" })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "Mark done" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/Their sign-in still works/)).toBeInTheDocument();
    const confirm = within(dialog).getByRole("button", { name: "Mark done" });
    expect(confirm).toBeDisabled();
    await user.type(within(dialog).getByRole("textbox"), "Disabled in Microsoft 365");
    await user.click(confirm);
    expect(updateChecklistStep).toHaveBeenCalledWith("p1", "c1", "access", {
      state: "done",
      note: "Disabled in Microsoft 365",
    });
  });
});

describe("UnmatchedPage", () => {
  it("adds a seat's holder as a new person", async () => {
    const user = userEvent.setup();
    const linkSeatToPerson = vi.fn().mockResolvedValue({ ...MARIA, id: "p2" });
    const client = api(
      {
        listUnmatched: vi.fn().mockResolvedValue({
          ...NOTHING_UNMATCHED,
          seats: [
            {
              seat_id: "s9",
              subscription_id: "sub1",
              service_name: "Zoom",
              assignee_name: "Pat Doe",
              assignee_email: "pat@corp.example",
            },
          ],
        }),
        linkSeatToPerson,
      },
      ["people.read", "people.manage"],
    );
    mockUseApi.mockReturnValue(client);
    renderAt(<UnmatchedPage />, "/people/unmatched", "/people/unmatched");
    await user.click(await screen.findByRole("button", { name: "Add as a new person" }));
    expect(linkSeatToPerson).toHaveBeenCalledWith("s9", null);
  });
});

function devicePerson(overrides: Partial<DevicePersonOut> = {}): DevicePersonOut {
  return {
    device_id: "d1",
    person: MARIA,
    source: "operator",
    observed_login: "CORP\\maria",
    observed_at: null,
    ...overrides,
  };
}

/** The device page's control, and the picker it opens with "Change". */
async function openPicker(overrides: Partial<Record<keyof ApiClient, unknown>>) {
  const user = userEvent.setup();
  const client = api(
    {
      getDevicePerson: vi.fn().mockResolvedValue(devicePerson({ person: null, source: null })),
      listPeople: vi.fn().mockResolvedValue([MARIA]),
      ...overrides,
    },
    ["people.read", "people.manage"],
  );
  mockUseApi.mockReturnValue(client);
  renderAt(<DeviceAssignedTo deviceId="d1" />, "/devices/:id", "/devices/d1");
  await user.click(await screen.findByRole("button", { name: "Change" }));
  const dialog = await screen.findByRole("dialog");
  return { user, client, dialog };
}

describe("DeviceAssignedTo", () => {
  it("reads as a field of the device, and renders nothing without people.read", async () => {
    const getDevicePerson = vi.fn().mockResolvedValue(devicePerson());
    mockUseApi.mockReturnValue(api({ getDevicePerson }, ["people.read"]));
    const { unmount } = renderAt(
      <DeviceAssignedTo deviceId="d1" />,
      "/devices/:id",
      "/devices/d1",
    );
    const holder = await screen.findByRole("link", { name: "Maria Garcia" });
    expect(holder).toHaveAttribute("href", "/people/p1");
    const field = screen.getByTestId("device-assigned-to");
    expect(field).toHaveTextContent("Assigned to: Maria Garcia · set by hand");
    expect(field).not.toHaveTextContent(/Used by/);
    // Read-only: no way to change it.
    expect(screen.queryByRole("button", { name: "Change" })).toBeNull();
    unmount();

    const hidden = vi.fn();
    const client = api({ getDevicePerson: hidden }, ["devices.read"]);
    mockUseApi.mockReturnValue(client);
    const { container } = renderAt(
      <DeviceAssignedTo deviceId="d1" />,
      "/devices/:id",
      "/devices/d1",
    );
    await vi.waitFor(() => expect(client.me).toHaveBeenCalled());
    expect(hidden).not.toHaveBeenCalled();
    expect(container).toBeEmptyDOMElement();
  });

  it("says nobody is assigned yet, and who last signed in", async () => {
    const getDevicePerson = vi.fn().mockResolvedValue(
      devicePerson({ person: null, source: "inventory", observed_login: "TOESHEE\\aruiz" }),
    );
    mockUseApi.mockReturnValue(api({ getDevicePerson }, ["people.read"]));
    renderAt(<DeviceAssignedTo deviceId="d1" />, "/devices/:id", "/devices/d1");
    expect(await screen.findByTestId("device-assigned-to")).toHaveTextContent(
      "Assigned to: nobody yet · last signed in as TOESHEE\\aruiz",
    );
  });

  it("says where an automatic holder came from, and offers the way back only from a hand-set one", async () => {
    const getDevicePerson = vi.fn().mockResolvedValue(devicePerson({ source: "inventory" }));
    mockUseApi.mockReturnValue(api({ getDevicePerson }, ["people.read", "people.manage"]));
    renderAt(<DeviceAssignedTo deviceId="d1" />, "/devices/:id", "/devices/d1");
    expect(await screen.findByTestId("device-assigned-to")).toHaveTextContent(
      "Assigned to: Maria Garcia · from who signs in",
    );
    expect(screen.getByRole("button", { name: "Change" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Go back to automatic" })).toBeNull();
  });

  it("assigns someone already here", async () => {
    const setDevicePerson = vi.fn().mockResolvedValue(devicePerson());
    const { user, dialog } = await openPicker({ setDevicePerson });
    await user.click(within(dialog).getByRole("combobox", { name: "Person" }));
    await user.click(await within(dialog).findByRole("option", { name: /Maria Garcia/ }));
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    expect(setDevicePerson).toHaveBeenCalledWith("d1", "p1");
  });

  it("adds someone new and assigns them in one go", async () => {
    const createPerson = vi
      .fn()
      .mockResolvedValue({ ...MARIA, id: "p7", display_name: "Ana Ruiz", email: "ana@corp.example" });
    const setDevicePerson = vi.fn().mockResolvedValue(devicePerson());
    const { user, dialog } = await openPicker({ createPerson, setDevicePerson });
    // The dead end this replaces: there is no People page to add them on.
    expect(within(dialog).queryByText(/People page/)).toBeNull();

    await user.click(within(dialog).getByRole("button", { name: "Someone new…" }));
    await user.type(within(dialog).getByLabelText("Name"), "Ana Ruiz");
    await user.type(within(dialog).getByLabelText("Email"), "ana@corp.example");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));

    // In the internal client, whose id is `/me`'s org_id (PHASE 3: the selector's).
    expect(createPerson).toHaveBeenCalledWith({
      display_name: "Ana Ruiz",
      email: "ana@corp.example",
      client_id: "o1",
    });
    await vi.waitFor(() => expect(setDevicePerson).toHaveBeenCalledWith("d1", "p7"));
    // Created first, assigned second.
    expect(createPerson.mock.invocationCallOrder[0]).toBeLessThan(
      setDevicePerson.mock.invocationCallOrder[0] ?? 0,
    );
    await vi.waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("needs a name, and nothing else", async () => {
    const createPerson = vi.fn().mockResolvedValue({ ...MARIA, id: "p8", display_name: "Pat", email: null });
    const setDevicePerson = vi.fn().mockResolvedValue(devicePerson());
    const { user, dialog } = await openPicker({ createPerson, setDevicePerson });
    await user.click(within(dialog).getByRole("button", { name: "Someone new…" }));

    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    expect(await within(dialog).findByText(/Name is required/)).toBeInTheDocument();
    expect(createPerson).not.toHaveBeenCalled();

    await user.type(within(dialog).getByLabelText("Name"), "Pat");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    expect(createPerson).toHaveBeenCalledWith({ display_name: "Pat", email: null, client_id: "o1" });
    await vi.waitFor(() => expect(setDevicePerson).toHaveBeenCalledWith("d1", "p8"));
  });

  it("assigns nobody when adding them fails, and says why", async () => {
    const createPerson = vi
      .fn()
      .mockRejectedValue(new ApiError(409, "someone here already has that email address"));
    const setDevicePerson = vi.fn();
    const { user, dialog } = await openPicker({ createPerson, setDevicePerson });
    await user.click(within(dialog).getByRole("button", { name: "Someone new…" }));
    await user.type(within(dialog).getByLabelText("Name"), "Maria G");
    await user.type(within(dialog).getByLabelText("Email"), "maria@corp.example");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));

    expect(await within(dialog).findByText(/already has that email address/)).toBeInTheDocument();
    expect(createPerson).toHaveBeenCalledTimes(1);
    expect(setDevicePerson).not.toHaveBeenCalled();
  });

  it("does not add the same person twice when only the assignment failed", async () => {
    const createPerson = vi
      .fn()
      .mockResolvedValue({ ...MARIA, id: "p9", display_name: "Ana Ruiz", email: null });
    const setDevicePerson = vi
      .fn()
      .mockRejectedValueOnce(new ApiError(503, "try again"))
      .mockResolvedValueOnce(devicePerson());
    const { user, dialog } = await openPicker({ createPerson, setDevicePerson });
    await user.click(within(dialog).getByRole("button", { name: "Someone new…" }));
    await user.type(within(dialog).getByLabelText("Name"), "Ana Ruiz");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    await vi.waitFor(() => expect(setDevicePerson).toHaveBeenCalledTimes(1));
    expect(await within(dialog).findByRole("alert")).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    await vi.waitFor(() => expect(setDevicePerson).toHaveBeenCalledTimes(2));
    expect(setDevicePerson).toHaveBeenLastCalledWith("d1", "p9");
    expect(createPerson).toHaveBeenCalledTimes(1);
  });
});

describe("PersonLinkForUser", () => {
  it("links a ticket requester's account to their person", async () => {
    const personForUser = vi.fn().mockResolvedValue(MARIA);
    mockUseApi.mockReturnValue(api({ personForUser }, ["people.read"]));
    renderAt(<PersonLinkForUser userId="u9" />, "/t", "/t");
    expect(await screen.findByRole("link", { name: "View person" })).toHaveAttribute(
      "href",
      "/people/p1",
    );
    expect(personForUser).toHaveBeenCalledWith("u9");
  });
});
