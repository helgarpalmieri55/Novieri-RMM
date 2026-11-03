// frontend/src/modules/health/__tests__/assignments.test.tsx
/**
 * Where a profile applies, and the console's one way to apply it: as a
 * client's default or as the organisation's (MSP clients phase 3, design
 * §5.1). `useNewRowClient` and `ClientField` are Task 9's and are replaced
 * here: what is pinned is the body this panel sends for each answer they give.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setActiveLanguage } from "../../../i18n/core";
import { ApiError } from "../../../lib/api";
import type { ApiClient, Me, MeClient } from "../../../lib/api";
import { appliedByLabel } from "../healthState";
import { ProfileAssignments, describeAssignmentTarget } from "../ProfileAssignments";
import { describeAssignments } from "../ProfileList";
import type { AssignmentOut } from "../types";

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const state = vi.hoisted(() => ({
  selected: null as string | null,
  clients: [] as MeClient[],
  newRow: { clientId: null as string | null, needsField: true, options: [] as MeClient[] },
}));
vi.mock("../../../lib/clientScope", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../../lib/clientScope")>()),
  useClientScope: () => ({ selected: state.selected, select: vi.fn(), clients: state.clients, ready: true }),
}));
vi.mock("../../../lib/clientSelection", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../../lib/clientSelection")>()),
  useNewRowClient: () => state.newRow,
}));
vi.mock("../../../components/ClientField", () => ({
  ClientField: ({ onChange }: { onChange: (id: string | null) => void }) => (
    <select aria-label="Client" defaultValue="" onChange={(event) => onChange(event.target.value || null)}>
      <option value="" />
      <option value="c-andes">Andes Logistics</option>
    </select>
  ),
}));

const ANDES: MeClient = {
  id: "c-andes",
  name: "Andes Logistics",
  kind: "customer",
  enabled_modules: [],
  permissions: ["health.read", "health.admin"],
};
const ORG_DEFAULT: AssignmentOut = {
  id: "ha-org",
  profile_id: "hp-1",
  profile_name: "Baseline",
  device_id: null,
  device_group_id: null,
  client_id: null,
  level: "org",
  created_by: null,
  created_at: "2026-10-01T00:00:00Z",
};
const ANDES_DEFAULT: AssignmentOut = { ...ORG_DEFAULT, id: "ha-andes", client_id: "c-andes", level: "client" };

function me(everywhere: string[]): Me {
  return {
    user_id: "u1",
    org_id: "org-1",
    email: "op@example.com",
    display_name: "Operator",
    permissions: ["health.read", "health.admin"],
    permissions_everywhere: everywhere,
    clients: [ANDES],
  };
}

function renderPanel(api: Partial<ApiClient>, everywhere: string[] = []) {
  mockUseApi.mockReturnValue({
    me: vi.fn().mockResolvedValue(me(everywhere)),
    listHealthAssignments: vi.fn().mockResolvedValue([]),
    ...api,
  } as unknown as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <ProfileAssignments profileId="hp-1" canAdmin />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mockUseApi.mockReset();
  state.selected = null;
  state.clients = [ANDES];
  state.newRow = { clientId: null, needsField: true, options: [ANDES] };
});

afterEach(() => setActiveLanguage("en"));

describe("where a profile applies", () => {
  it("lists every assignment of the profile, a client default by its client's name", async () => {
    const listHealthAssignments = vi.fn().mockResolvedValue([ORG_DEFAULT, ANDES_DEFAULT]);
    renderPanel({ listHealthAssignments });
    expect(await screen.findByText("Organisation default")).toBeInTheDocument();
    expect(await screen.findByText("Default of Andes Logistics")).toBeInTheDocument();
    expect(listHealthAssignments).toHaveBeenCalledWith("hp-1");
  });

  it("removes a client default for a person who administers health on that client", async () => {
    const deleteHealthAssignment = vi.fn().mockResolvedValue(undefined);
    renderPanel({
      listHealthAssignments: vi.fn().mockResolvedValue([ORG_DEFAULT, ANDES_DEFAULT]),
      deleteHealthAssignment,
    });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Remove: Default of Andes Logistics" }));
    await user.click(
      within(await screen.findByRole("dialog", { name: "Remove this assignment" })).getByRole("button", {
        name: "Remove",
      }),
    );
    expect(deleteHealthAssignment).toHaveBeenCalledWith("ha-andes");
    // The organisation default is global: not removable without every client.
    expect(screen.queryByRole("button", { name: "Remove: Organisation default" })).toBeNull();
  });
});

describe("assigning a profile as a client's default", () => {
  it("asks for the client with every client shown, and sends it", async () => {
    const createHealthAssignment = vi.fn().mockResolvedValue(ANDES_DEFAULT);
    renderPanel({ createHealthAssignment });
    const user = userEvent.setup();
    await user.selectOptions(await screen.findByRole("combobox", { name: "Client" }), "c-andes");
    await user.click(screen.getByRole("button", { name: "Assign" }));
    await waitFor(() =>
      expect(createHealthAssignment).toHaveBeenCalledWith({ profile_id: "hp-1", client_id: "c-andes" }),
    );
  });

  it("names the missing client instead of sending anything", async () => {
    const createHealthAssignment = vi.fn();
    renderPanel({ createHealthAssignment });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Assign" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Client is required");
    expect(createHealthAssignment).not.toHaveBeenCalled();
  });

  it("uses the client chosen above without asking, and offers no organisation default", async () => {
    state.selected = "c-andes";
    state.newRow = { clientId: "c-andes", needsField: false, options: [] };
    const createHealthAssignment = vi.fn().mockResolvedValue(ANDES_DEFAULT);
    renderPanel({ createHealthAssignment }, ["health.admin"]);
    const user = userEvent.setup();
    expect(await screen.findByText("For Andes Logistics, the client the console is working on.")).toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Client" })).toBeNull();
    expect(screen.queryByRole("combobox", { name: "Assign as" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Assign" }));
    await waitFor(() =>
      expect(createHealthAssignment).toHaveBeenCalledWith({ profile_id: "hp-1", client_id: "c-andes" }),
    );
  });

  it("explains a refused assignment in the server's words", async () => {
    const createHealthAssignment = vi.fn().mockRejectedValue(
      new ApiError(409, JSON.stringify({ detail: "this client already has a default health profile" })),
    );
    renderPanel({ createHealthAssignment });
    const user = userEvent.setup();
    await user.selectOptions(await screen.findByRole("combobox", { name: "Client" }), "c-andes");
    await user.click(screen.getByRole("button", { name: "Assign" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The profile was not assigned. this client already has a default health profile",
    );
  });
});

describe("assigning a profile as the organisation default", () => {
  it("is offered only with health.admin on every client, and sends no client", async () => {
    const createHealthAssignment = vi.fn().mockResolvedValue(ORG_DEFAULT);
    renderPanel({ createHealthAssignment }, ["health.admin"]);
    const user = userEvent.setup();
    await user.selectOptions(await screen.findByRole("combobox", { name: "Assign as" }), "org");
    expect(screen.queryByRole("combobox", { name: "Client" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Assign" }));
    await waitFor(() => expect(createHealthAssignment).toHaveBeenCalledWith({ profile_id: "hp-1" }));
  });

  it("is not offered without every client", async () => {
    renderPanel({});
    await screen.findByRole("combobox", { name: "Client" });
    expect(screen.queryByRole("combobox", { name: "Assign as" })).toBeNull();
  });
});

describe("the words for a client default", () => {
  const name = (id: string) => (id === "c-andes" ? "Andes Logistics" : id);

  it("counts client defaults among a profile's assignments", () => {
    expect(
      describeAssignments({ id: "p" }, [
        { profile_id: "p", level: "org" },
        { profile_id: "p", level: "client" },
        { profile_id: "p", level: "client" },
        { profile_id: "p", level: "group" },
      ]),
    ).toBe("org default, default of 2 clients, 1 group");
  });

  it("says a device's profile came from its client's default", () => {
    expect(appliedByLabel("client", { client_id: "c-andes" }, name)).toBe("its client's default (Andes Logistics)");
    expect(appliedByLabel("group", { device_group_id: "g1" }, name)).toBe("a group assignment (g1)");
    expect(appliedByLabel("org", null, name)).toBe("the organisation default assignment");
    expect(appliedByLabel("device", null, name)).toBe("an assignment on this device");
  });

  it("says it in Spanish", () => {
    setActiveLanguage("es");
    expect(describeAssignmentTarget(ANDES_DEFAULT, name)).toBe("Por defecto de Andes Logistics");
    expect(appliedByLabel("client", { client_id: "c-andes" }, name)).toBe(
      "el perfil por defecto de su cliente (Andes Logistics)",
    );
    expect(describeAssignments({ id: "hp-1" }, [ANDES_DEFAULT])).toBe("por defecto de 1 cliente");
  });
});
