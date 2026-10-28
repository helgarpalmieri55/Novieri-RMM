import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../../../lib/api";
import type { ApiClient, Device, DeviceGroup, Me } from "../../../lib/api";
import { NO_DATE } from "../../../lib/datetime";
import { cronProblem, windowLabel } from "../cron";
import { devicePickerOptions, packagePickerOptions } from "../pickerOptions";
import {
  deploymentResultStateVariant,
  deploymentStateVariant,
  scriptRunStateVariant,
} from "../deployState";
import { DeployPage } from "../DeployPage";
import { DeviceDeployTab } from "../DeviceDeployTab";
import { PackagesPage } from "../PackagesPage";
import { ScriptsPage } from "../ScriptsPage";
import type {
  Deployment,
  DeviceDeploymentHistory,
  Package,
  Script,
  ScriptListItem,
} from "../types";

// The auth-backed API client is mocked so this is a pure component test: no
// real fetch. Deploy data is plain TanStack Query state (no live-event
// stream), so freshness comes from invalidating the deploy query keys after
// mutations succeed (mirrors modules/patch/__tests__/patch.test.tsx).
const mockUseApi = vi.fn();
// The Scripts page also lists schedules and reads the org's zone; tests about
// scripts get an empty schedule list unless they say otherwise.
vi.mock("../../../lib/ApiContext", () => ({
  useApi: () => ({
    listScriptPolicies: () => Promise.resolve([]),
    getCurrentOrg: () =>
      Promise.resolve({ id: "o1", name: "Org", slug: "org", status: "active", settings: {} }),
    ...mockUseApi(),
  }),
}));

function renderWithProviders(ui: ReactElement, path = "/deploy") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

const ME: Me = {
  user_id: "u1",
  org_id: "o1",
  email: "operator@example.com",
  display_name: "Operator",
  permissions: [
    "deploy.read",
    "deploy.create",
    "deploy.execute",
    "deploy.package.manage",
    "deploy.script.manage",
    "deploy.script.run",
  ],
};

const PACKAGES: Package[] = [
  {
    id: "pkg1",
    name: "Notepad++",
    version: "8.6",
    os_family: "windows",
    installer_type: "msi",
    artifact_ref: "sha256:abc",
    artifact_sha256: "abc",
    detection_rule: {},
    uninstall_rule: {},
    install_args: null,
    created_at: "2026-01-01T00:00:00.000Z",
  },
];

/**
 * An endpoint, with only the fields the picker's sublabel reads set per case.
 * Copied field-for-field from `lib/api.ts`'s `Device`, so a new required
 * column on that interface fails this file rather than the deployment dialog.
 */
function device(overrides: Partial<Device> & Pick<Device, "id">): Device {
  return {
    hostname: null,
    fqdn: null,
    os_family: null,
    os_version: "",
    architecture: "",
    device_group_id: null,
    status: "active",
    agent_status: null,
    last_seen_at: null,
    cert_not_after: null,
    cert_state: null,
    cert_revoked: false,
    lifecycle: "in_service",
    asset_type: "computer",
    source: "agent",
    // An enrolled machine proved its own identity at enrolment; nobody had
    // to vouch for the row (asset-inventory spec §3).
    provenance: "agent",
    description: null,
    primary_ip: null,
    owner: null,
    location: null,
    ...overrides,
  };
}

const DEVICES: Device[] = [
  device({ id: "dev1", hostname: "WKS-042", os_family: "windows", agent_status: "online" }),
  device({ id: "dev2", hostname: "BUILD-01", os_family: "linux", agent_status: "offline" }),
];

const DEVICE_PAGE = { items: DEVICES, total: DEVICES.length };

const DEVICE_GROUPS: DeviceGroup[] = [
  {
    id: "grp-build",
    name: "Build runners",
    parent_group_id: null,
    description: "",
    dynamic: false,
    device_count: 3,
    precedence: 0,
    archived_at: null,
  },
];

const DEVICE_GROUP_PAGE = {
  items: DEVICE_GROUPS,
  total: DEVICE_GROUPS.length,
  ungrouped_device_count: 0,
  evaluation: null,
};

/**
 * Choose an option from an `EntityPicker`, the way a person does: focus the
 * combobox, then click the row. Deliberately NOT `user.type(...)` into the
 * box -- typing filters, it does not select, and a test that typed a UUID and
 * asserted it was submitted would pass against the very free-text field this
 * cycle removed.
 */
async function pickEntity(
  user: ReturnType<typeof userEvent.setup>,
  pickerLabel: string,
  optionName: RegExp,
) {
  // `getByRole("combobox", ...)`, not `getByLabelText`: the picker's own
  // listbox carries `aria-label={label}` too, so the plain label query is
  // ambiguous whenever the list happens to be open.
  await user.click(screen.getByRole("combobox", { name: pickerLabel }));
  await user.click(await screen.findByRole("option", { name: optionName }));
}

const DEPLOYMENTS: Deployment[] = [
  {
    id: "dep1",
    client_id: "o1",
    package_id: "pkg1",
    action: "install",
    target_type: "device",
    target_id: "dev1",
    window_cron: null,
    requested_by: "u1",
    state: "pending",
    created_at: "2026-01-02T00:00:00.000Z",
  },
];

/**
 * The org directory `/deploy`'s BY column reads. Its own permission, so a
 * caller may hold every deploy permission above and still not see it -- the
 * "names the id it cannot resolve" case below is that caller.
 */
const USERS = {
  users: [
    {
      id: "u1",
      email: "operator@example.com",
      display_name: "Adaeze Okonkwo",
      status: "active",
      roles: [],
    },
  ],
};

const SCRIPT_LIST_ITEM: ScriptListItem = {
  id: "scr1",
  name: "cleanup-temp",
  os_family: "windows",
  interpreter: "powershell",
  version: 1,
  created_by: "u1",
  created_at: "2026-01-01T00:00:00.000Z",
};

const SCRIPTS: ScriptListItem[] = [SCRIPT_LIST_ITEM];

const SCRIPT_DETAIL: Script = {
  ...SCRIPT_LIST_ITEM,
  body: "Remove-Item -Recurse C:\\Temp\\*",
};

describe("DeployPage", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      listDeployments: vi.fn().mockResolvedValue(DEPLOYMENTS),
      listPackages: vi.fn().mockResolvedValue(PACKAGES),
      listDevices: vi.fn().mockResolvedValue(DEVICE_PAGE),
      listDeviceGroups: vi.fn().mockResolvedValue(DEVICE_GROUP_PAGE),
      executeDeployment: vi.fn().mockResolvedValue({ job_ids: ["j1"] }),
      createDeployment: vi.fn().mockResolvedValue(DEPLOYMENTS[0]),
      listUsers: vi.fn().mockResolvedValue(USERS),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  it("lists deployments from the mocked api", async () => {
    renderWithProviders(<DeployPage />);

    expect(await screen.findByText("dep1")).toBeInTheDocument();
    // The verb the row shows is the verb a person writes. The wire value is
    // still `install`; the column is not where the wire is read.
    expect(screen.getByText("Install")).toBeInTheDocument();
  });

  // REVIEW WAVE, round 1, /deploy (copy): three columns printed machine
  // values -- the lowercase action enum, the raw 5-field cron expression in
  // mono, and "group grp-build", the bare group id. The window is painted on
  // a grid in the create dialog, so the list was the only place in the
  // console a reader met crontab syntax.
  it("writes the action and the window as prose, and the group by its name", async () => {
    api.listDeployments = vi.fn().mockResolvedValue([
      {
        ...(DEPLOYMENTS[0] as Deployment),
        id: "dep2",
        action: "uninstall",
        target_type: "group",
        target_id: "grp-build",
        window_cron: "0 22-23 * * 1-5",
      },
    ]);
    renderWithProviders(<DeployPage />);

    await screen.findByText("dep2");
    expect(screen.getByText("Uninstall")).toBeInTheDocument();
    expect(screen.getByText("Weekdays, 22:00 to 24:00")).toBeInTheDocument();
    // The group's NAME, resolved from the same list the picker offers --
    // "group grp-build" as the whole cell was a database value wearing a
    // lowercase noun.
    expect(screen.getByText("Build runners")).toBeInTheDocument();
    expect(screen.queryByText("grp-build")).not.toBeInTheDocument();
    expect(screen.queryByText("0 22-23 * * 1-5")).not.toBeInTheDocument();
  });

  it("falls back to the raw group id when the group cannot be resolved", async () => {
    api.listDeployments = vi.fn().mockResolvedValue([
      {
        ...(DEPLOYMENTS[0] as Deployment),
        id: "dep2",
        target_type: "group",
        target_id: "grp-unknown",
      },
    ]);
    // No group named "grp-unknown" in the fetched list -- an id whose row
    // the caller cannot read is still more than nothing.
    api.listDeviceGroups = vi.fn().mockResolvedValue(DEVICE_GROUP_PAGE);
    renderWithProviders(<DeployPage />);

    await screen.findByText("dep2");
    expect(screen.getByText("grp-unknown")).toBeInTheDocument();
  });

  // An expression the painter cannot draw has no sentence to print, and
  // inventing one would describe a window the server does not keep.
  it("prints an unpaintable expression as the expression it is", async () => {
    api.listDeployments = vi.fn().mockResolvedValue([
      { ...(DEPLOYMENTS[0] as Deployment), id: "dep3", window_cron: "30 2 1 * *" },
    ]);
    renderWithProviders(<DeployPage />);

    await screen.findByText("dep3");
    expect(screen.getByText("30 2 1 * *")).toBeInTheDocument();
  });

  it("calls the execute mutation once the operator confirms", async () => {
    const user = userEvent.setup();
    renderWithProviders(<DeployPage />);

    await screen.findByText("dep1");
    const row = screen.getByText("dep1").closest("tr") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: "Execute" }));

    // CYCLE 8: the row click no longer dispatches. Nothing has been signed
    // and sent at this point -- the confirmation is the dispatch.
    expect(api.executeDeployment).not.toHaveBeenCalled();
    const confirm = await screen.findByRole("dialog", { name: "Execute deployment" });
    await user.click(within(confirm).getByRole("button", { name: "Execute" }));

    expect(api.executeDeployment).toHaveBeenCalledWith("dep1");
  });

  it("hides the Execute control when the caller lacks deploy.execute", async () => {
    api.me = vi.fn().mockResolvedValue({
      ...ME,
      permissions: ME.permissions.filter((p) => p !== "deploy.execute"),
    });
    renderWithProviders(<DeployPage />);

    await screen.findByText("dep1");
    expect(screen.queryByRole("button", { name: "Execute" })).not.toBeInTheDocument();
  });

  // A deployment that has already reached a terminal state has nothing left
  // for another Execute click to do -- offering it invites a "run it again"
  // that is really a second deployment wearing the first one's row.
  it("does not offer Execute on a deployment that has already finished", async () => {
    api.listDeployments = vi.fn().mockResolvedValue([
      { ...(DEPLOYMENTS[0] as Deployment), id: "dep-done", state: "complete" },
      { ...(DEPLOYMENTS[0] as Deployment), id: "dep-failed", state: "failed" },
      { ...(DEPLOYMENTS[0] as Deployment), id: "dep-pending", state: "pending" },
    ]);
    renderWithProviders(<DeployPage />);

    await screen.findByText("dep-done");
    expect(
      within(screen.getByText("dep-done").closest("tr") as HTMLElement).queryByRole("button", {
        name: "Execute",
      }),
    ).not.toBeInTheDocument();
    expect(
      within(screen.getByText("dep-failed").closest("tr") as HTMLElement).queryByRole("button", {
        name: "Execute",
      }),
    ).not.toBeInTheDocument();
    expect(
      within(screen.getByText("dep-pending").closest("tr") as HTMLElement).getByRole("button", {
        name: "Execute",
      }),
    ).toBeInTheDocument();
  });

  it("hides the New deployment control when the caller lacks deploy.create", async () => {
    api.me = vi.fn().mockResolvedValue({
      ...ME,
      permissions: ME.permissions.filter((p) => p !== "deploy.create"),
    });
    renderWithProviders(<DeployPage />);

    await screen.findByText("dep1");
    expect(screen.queryByRole("button", { name: "New deployment" })).not.toBeInTheDocument();
  });

  it("counts the stat cards from the fetched deployments, not a fixed number", async () => {
    renderWithProviders(<DeployPage />);

    await screen.findByText("dep1");
    const stats = document.querySelectorAll(".stat-card");
    const values = [...stats].map((card) => card.querySelector(".stat-value")?.textContent);
    // total=1; DEPLOYMENTS[0].created_at is far in the past relative to
    // `now`, so "this week" is 0; state is "pending" (not terminal), so
    // success rate has no terminal deployments yet ("—"); failed=0.
    expect(values).toEqual(["1", "0", "—", "0"]);
    // The design's own stat cards read 64 / 31 / 96% / 5 -- mock figures
    // that would be wrong on every deployment if they survived the port.
    expect(values).not.toContain("64");
  });

  // REVIEW WAVE, round 2, /deploy (composition): the design's rows carry a BY
  // column and these did not, though `requested_by` is on every row.
  it("names the person who asked for each run", async () => {
    renderWithProviders(<DeployPage />);

    await screen.findByText("dep1");
    const row = screen.getByText("dep1").closest("tr") as HTMLElement;
    expect(within(row).getByText("Adaeze Okonkwo")).toBeInTheDocument();
    // The avatar is decoration beside the name, never the name itself.
    expect(within(row).getByText("AO")).toHaveAttribute("aria-hidden", "true");
  });

  it("keeps the requester id when the directory cannot be read", async () => {
    // 403 on /users: the caller may deploy and may not list accounts.
    api.listUsers = vi.fn().mockRejectedValue(new ApiError(403, ""));
    renderWithProviders(<DeployPage />);

    await screen.findByText("dep1");
    const row = screen.getByText("dep1").closest("tr") as HTMLElement;
    // The id, not a blank cell and not an invented name.
    expect(within(row).getByText("u1")).toBeInTheDocument();
  });

  it("says n/a for a deployment with no requester recorded", async () => {
    api.listDeployments = vi
      .fn()
      .mockResolvedValue([{ ...(DEPLOYMENTS[0] as Deployment), requested_by: null }]);
    renderWithProviders(<DeployPage />);

    await screen.findByText("dep1");
    const row = screen.getByText("dep1").closest("tr") as HTMLElement;
    expect(within(row).getByText("n/a")).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------
// REVIEW WAVE, round 2, /deploy/scripts (function): the module's three pages
// are routed but the rail carries one entry, so before this strip existed
// nothing in the console linked to /deploy/packages or /deploy/scripts.
// --------------------------------------------------------------------------

describe("every deploy page links to the other two", () => {
  function api(): Partial<ApiClient> {
    return {
      me: vi.fn().mockResolvedValue(ME),
      listDeployments: vi.fn().mockResolvedValue(DEPLOYMENTS),
      listPackages: vi.fn().mockResolvedValue(PACKAGES),
      listScripts: vi.fn().mockResolvedValue(SCRIPTS),
      listDevices: vi.fn().mockResolvedValue(DEVICE_PAGE),
      listUsers: vi.fn().mockResolvedValue(USERS),
    };
  }

  const EXPECTED = [
    ["Deployments", "/deploy"],
    ["Packages", "/deploy/packages"],
    ["Scripts", "/deploy/scripts"],
  ] as const;

  function expectStrip(activeLabel: string) {
    // `role="tablist"` overrides the <nav> element's implicit role, so the
    // strip is found as the tablist it announces itself to be.
    const nav = screen.getByRole("tablist", { name: "Software deployment" });
    for (const [label, href] of EXPECTED) {
      expect(within(nav).getByRole("tab", { name: label })).toHaveAttribute("href", href);
    }
    expect(within(nav).getByRole("tab", { name: activeLabel }).className).toContain("active");
  }

  it("draws the strip on the deployments page", async () => {
    mockUseApi.mockReturnValue(api() as ApiClient);
    renderWithProviders(<DeployPage />, "/deploy");

    await screen.findByText("dep1");
    expectStrip("Deployments");
  });

  it("draws the strip on the packages page", async () => {
    mockUseApi.mockReturnValue(api() as ApiClient);
    renderWithProviders(<PackagesPage />, "/deploy/packages");

    await screen.findByText("Notepad++");
    expectStrip("Packages");
  });

  it("draws the strip on the scripts page", async () => {
    mockUseApi.mockReturnValue(api() as ApiClient);
    renderWithProviders(<ScriptsPage />, "/deploy/scripts");

    await screen.findByText("cleanup-temp");
    expectStrip("Scripts");
  });

  it("draws no strip for a caller without deploy.read", async () => {
    const withoutRead = api();
    withoutRead.me = vi
      .fn()
      .mockResolvedValue({ ...ME, permissions: ME.permissions.filter((p) => p !== "deploy.read") });
    mockUseApi.mockReturnValue(withoutRead as ApiClient);
    renderWithProviders(<ScriptsPage />, "/deploy/scripts");

    await screen.findByText("cleanup-temp");
    expect(screen.queryByRole("tablist", { name: "Software deployment" })).not.toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------
// DeployPage: the three states a mock never has (UI port spec §6/§8.5).
// --------------------------------------------------------------------------

describe("DeployPage query states", () => {
  function baseApi(listDeployments: ApiClient["listDeployments"]): Partial<ApiClient> {
    return {
      me: vi.fn().mockResolvedValue(ME),
      listDeployments,
      listPackages: vi.fn().mockResolvedValue(PACKAGES),
    };
  }

  it("says it is loading rather than showing an empty deployment list", async () => {
    mockUseApi.mockReturnValue(baseApi(vi.fn(() => new Promise<never>(() => {}))) as ApiClient);
    renderWithProviders(<DeployPage />);

    expect(await screen.findByText("Loading…")).toBeInTheDocument();
  });

  it("says the request FAILED rather than showing an empty deployment list", async () => {
    mockUseApi.mockReturnValue(baseApi(vi.fn(() => Promise.reject(new Error("boom")))) as ApiClient);
    renderWithProviders(<DeployPage />);

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText("No deployments yet.")).not.toBeInTheDocument();
  });

  it("says there are no deployments when there are genuinely none", async () => {
    mockUseApi.mockReturnValue(baseApi(vi.fn(() => Promise.resolve([]))) as ApiClient);
    renderWithProviders(<DeployPage />);

    expect(await screen.findByText("No deployments yet.")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------
// PackagesPage: had NO test coverage before this migration. Covers the
// three states, mirroring DevicesPage.test.tsx.
// --------------------------------------------------------------------------

describe("PackagesPage", () => {
  function baseApi(listPackages: ApiClient["listPackages"]): Partial<ApiClient> {
    return { me: vi.fn().mockResolvedValue(ME), listPackages };
  }

  it("says it is loading rather than showing an empty catalog", async () => {
    mockUseApi.mockReturnValue(baseApi(vi.fn(() => new Promise<never>(() => {}))) as ApiClient);
    renderWithProviders(<PackagesPage />);

    expect(await screen.findByText("Loading…")).toBeInTheDocument();
  });

  it("says the request FAILED rather than showing an empty catalog", async () => {
    mockUseApi.mockReturnValue(baseApi(vi.fn(() => Promise.reject(new Error("boom")))) as ApiClient);
    renderWithProviders(<PackagesPage />);

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText("No packages registered.")).not.toBeInTheDocument();
  });

  it("says the catalog is genuinely empty when it is", async () => {
    mockUseApi.mockReturnValue(baseApi(vi.fn(() => Promise.resolve([]))) as ApiClient);
    renderWithProviders(<PackagesPage />);

    expect(await screen.findByText("No packages registered.")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("lists packages from the mocked api", async () => {
    mockUseApi.mockReturnValue(baseApi(vi.fn(() => Promise.resolve(PACKAGES))) as ApiClient);
    renderWithProviders(<PackagesPage />);

    expect(await screen.findByText("Notepad++")).toBeInTheDocument();
  });

  // REVIEW WAVE, round 1 (harmony): Platform was drawn as a severity pill in
  // the `info` kind, so "Windows" arrived wearing an info-circle in the
  // accent wash. A platform is a property of the row; the kit draws it with
  // the brand glyph, as /deploy's Target column does.
  it("draws the platform as a platform, not as a severity", async () => {
    mockUseApi.mockReturnValue(baseApi(vi.fn(() => Promise.resolve(PACKAGES))) as ApiClient);
    renderWithProviders(<PackagesPage />);

    await screen.findByText("Notepad++");
    const row = screen.getByText("Notepad++").closest("tr") as HTMLElement;
    // The name is still text on the row -- it was never colour alone, and it
    // still is not.
    expect(within(row).getByText("Windows")).toBeInTheDocument();
    expect(within(row).getByRole("img", { name: "Windows" })).toBeInTheDocument();
    expect(within(row).queryByText("windows")).not.toBeInTheDocument();
    expect(row.querySelector(".pill")).toBeNull();
  });

  // REVIEW WAVE, round 2 (harmony): Delete was `ghost` -- accent blue, no
  // edge, indistinguishable from a link and from the "Upload artifact"
  // button beside it -- while the same verb on /credentials wears the
  // critical edge. One console, one treatment for delete.
  it("keeps Delete in the row's menu, dressed as the destructive control it is", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(baseApi(vi.fn(() => Promise.resolve(PACKAGES))) as ApiClient);
    renderWithProviders(<PackagesPage />);

    await screen.findByText("Notepad++");
    const row = screen.getByText("Notepad++").closest("tr") as HTMLElement;
    // Not a red button on every row: red is kept for the one control that destroys.
    expect(within(row).queryByRole("button", { name: "Delete" })).toBeNull();
    await user.click(within(row).getByRole("button", { name: /^More actions for Notepad\+\+/ }));
    const remove = screen.getByRole("menuitem", { name: "Delete" });
    expect(remove.className).toContain("menu-item-destructive");
  });
});

describe("ScriptsPage", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      listScripts: vi.fn().mockResolvedValue(SCRIPTS),
      listDevices: vi.fn().mockResolvedValue(DEVICE_PAGE),
      listDeviceGroups: vi.fn().mockResolvedValue(DEVICE_GROUP_PAGE),
      getScript: vi.fn().mockResolvedValue(SCRIPT_DETAIL),
      authorScript: vi.fn(),
      editScript: vi.fn(),
      runScript: vi.fn().mockResolvedValue({ job_ids: ["j1"] }),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  it("offers device groups from a picker rather than a pasted id", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ScriptsPage />);

    await screen.findByText("cleanup-temp");
    const row = screen.getByText("cleanup-temp").closest("tr") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: "Run" }));
    const dialog = await screen.findByRole("dialog", { name: "Run cleanup-temp" });
    await user.selectOptions(within(dialog).getByLabelText("Target type"), "group");
    await pickEntity(user, "Target", /Build runners/);
    await user.click(within(dialog).getByRole("button", { name: "Run script" }));
    const confirm = await screen.findByRole("dialog", { name: "Run script as SYSTEM" });
    await user.click(within(confirm).getByRole("button", { name: "Run script" }));

    expect(api.runScript).toHaveBeenCalledWith(
      "scr1",
      expect.objectContaining({ target_type: "group", target_id: "grp-build" }),
    );
  });

  it("says the request FAILED rather than showing an empty script list", async () => {
    api.listScripts = vi.fn().mockRejectedValue(new Error("boom"));
    mockUseApi.mockReturnValue(api as ApiClient);
    renderWithProviders(<ScriptsPage />);

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText("No scripts authored yet.")).not.toBeInTheDocument();
  });

  it("lists scripts from the mocked api", async () => {
    renderWithProviders(<ScriptsPage />);

    expect(await screen.findByText("cleanup-temp")).toBeInTheDocument();
  });

  // REVIEW WAVE, round 1 (copy and harmony): the Interpreter column printed
  // the stored token, and Platform wore the same info-circle severity pill
  // the package catalog did.
  it("spells the interpreter the way its vendor does, and draws the platform as one", async () => {
    renderWithProviders(<ScriptsPage />);

    await screen.findByText("cleanup-temp");
    const row = screen.getByText("cleanup-temp").closest("tr") as HTMLElement;
    expect(within(row).getByText("PowerShell")).toBeInTheDocument();
    expect(within(row).queryByText("powershell")).not.toBeInTheDocument();
    expect(within(row).getByText("Windows")).toBeInTheDocument();
    expect(within(row).getByRole("img", { name: "Windows" })).toBeInTheDocument();
  });

  // REVIEW WAVE, round 3 (composition): the name cell carried the same
  // sub-line on every row, "runs as SYSTEM or root", which the page
  // description one line above already says. Structure only -- the row still
  // names the script and still carries every column and both controls.
  it("puts no constant sub-line under the script name, and keeps every column", async () => {
    renderWithProviders(<ScriptsPage />);

    await screen.findByText("cleanup-temp");
    const row = screen.getByText("cleanup-temp").closest("tr") as HTMLElement;
    expect(row.querySelector(".cell-sub")).toBeNull();
    expect(within(row).queryByText(/runs as SYSTEM/i)).not.toBeInTheDocument();
    expect(within(row).getByText("PowerShell")).toBeInTheDocument();
    expect(within(row).getByText("Windows")).toBeInTheDocument();
    expect(within(row).getByText(String(SCRIPT_LIST_ITEM.version))).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: "Edit" })).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: "Run" })).toBeInTheDocument();
  });

  it("hides the Run control when the caller lacks deploy.script.run", async () => {
    api.me = vi.fn().mockResolvedValue({
      ...ME,
      permissions: ME.permissions.filter((p) => p !== "deploy.script.run"),
    });
    renderWithProviders(<ScriptsPage />);

    await screen.findByText("cleanup-temp");
    expect(screen.queryByRole("button", { name: "Run" })).not.toBeInTheDocument();
  });

  it("hides the New script control when the caller lacks deploy.script.manage", async () => {
    api.me = vi.fn().mockResolvedValue({
      ...ME,
      permissions: ME.permissions.filter((p) => p !== "deploy.script.manage"),
    });
    renderWithProviders(<ScriptsPage />);

    await screen.findByText("cleanup-temp");
    expect(screen.queryByRole("button", { name: "New script" })).not.toBeInTheDocument();
  });

  it("prefills the Edit dialog body from the fetched script instead of leaving it blank", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ScriptsPage />);

    await screen.findByText("cleanup-temp");
    const row = screen.getByText("cleanup-temp").closest("tr") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: "Edit" }));

    expect(api.getScript).toHaveBeenCalledWith("scr1");
    const textarea = await screen.findByDisplayValue(SCRIPT_DETAIL.body);
    expect(textarea).toBeInTheDocument();
  });

  it("calls the run mutation with the target the operator picked and confirmed", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ScriptsPage />);

    await screen.findByText("cleanup-temp");
    const row = screen.getByText("cleanup-temp").closest("tr") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: "Run" }));

    await pickEntity(user, "Target", /WKS-042/);
    await user.click(screen.getByRole("button", { name: "Run script" }));

    // Still nothing dispatched: this script runs as SYSTEM/root and the
    // confirmation is the last place anybody can stop it.
    expect(api.runScript).not.toHaveBeenCalled();
    const confirm = await screen.findByRole("dialog", { name: "Run script as SYSTEM" });
    await user.click(within(confirm).getByRole("button", { name: "Run script" }));

    expect(api.runScript).toHaveBeenCalledWith("scr1", {
      target_type: "device",
      target_id: "dev1",
    });
  });
});

describe("deploy state vocabulary", () => {
  it("maps real backend deployment_results states to non-default badge variants", () => {
    expect(deploymentResultStateVariant("installed")).toBe("success");
    expect(deploymentResultStateVariant("failed")).toBe("destructive");
    expect(deploymentResultStateVariant("running")).toBe("info");
    expect(deploymentResultStateVariant("pending")).toBe("muted");
    expect(deploymentResultStateVariant("not_applicable")).toBe("muted");
    // Invented vocabulary must not resolve specially.
    expect(deploymentResultStateVariant("applied")).toBe("muted");
  });

  it("maps real backend deployments states to non-default badge variants", () => {
    expect(deploymentStateVariant("complete")).toBe("success");
    expect(deploymentStateVariant("failed")).toBe("destructive");
    expect(deploymentStateVariant("dispatching")).toBe("info");
    expect(deploymentStateVariant("pending")).toBe("muted");
    // "installed"/"not_applicable" are deployment_results states, not
    // deployments states — must not resolve specially here.
    expect(deploymentStateVariant("installed")).toBe("muted");
  });

  it("maps real backend script_runs states to non-default badge variants", () => {
    expect(scriptRunStateVariant("succeeded")).toBe("success");
    expect(scriptRunStateVariant("failed")).toBe("destructive");
    expect(scriptRunStateVariant("running")).toBe("info");
    expect(scriptRunStateVariant("pending")).toBe("muted");
    // "complete" is a deployments state, not a script_runs state.
    expect(scriptRunStateVariant("complete")).toBe("muted");
  });
});

// --------------------------------------------------------------------------
// CreateDeploymentDialog: the module's copy of the product-wide silent no-op.
//
// The audit walked /deploy → New deployment and filed [MEDIUM] "the modal is
// not a `<form>`, continuing the product-wide pattern": the panel's controls
// were loose in a `<div>`, so Enter did nothing, and the Create button was
// `disabled={create.isPending || !packageId || !targetId}` — pressing it with
// a field missing moved nothing and named no field. Adopting the form kernel
// closes both: the button stays enabled and the click produces the reason.
// --------------------------------------------------------------------------

describe("CreateDeploymentDialog", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      listDeployments: vi.fn().mockResolvedValue(DEPLOYMENTS),
      listPackages: vi.fn().mockResolvedValue(PACKAGES),
      listDevices: vi.fn().mockResolvedValue(DEVICE_PAGE),
      createDeployment: vi.fn().mockResolvedValue(DEPLOYMENTS[0]),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  async function openDialog() {
    const user = userEvent.setup();
    renderWithProviders(<DeployPage />);
    await screen.findByText("dep1");
    await user.click(screen.getByRole("button", { name: "New deployment" }));
    const dialog = await screen.findByRole("dialog", { name: "New deployment" });
    // Both lists are fetched, so neither picker has anything on first paint;
    // `pickEntity` waits for the row it is about to click, so there is
    // nothing to pre-await here.
    return { user, dialog };
  }

  it("leaves Create enabled with nothing filled in", async () => {
    // THE RULE: a disabled button that never says why IS the silent no-op.
    // Disabled is for a request in flight, not for input not yet supplied.
    const { dialog } = await openDialog();
    expect(within(dialog).getByRole("button", { name: "Create" })).toBeEnabled();
  });

  it("refuses an empty form and names both missing fields on screen", async () => {
    const { user, dialog } = await openDialog();
    await user.click(within(dialog).getByRole("button", { name: "Create" }));

    expect(api.createDeployment).not.toHaveBeenCalled();
    // The labels as printed beside the controls, never `package_id`.
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("Package is required");
    expect(alert).toHaveTextContent("Target is required");
  });

  it("names only the field that is actually missing", async () => {
    const { user, dialog } = await openDialog();
    await pickEntity(user, "Package", /Notepad\+\+ 8\.6/);
    await user.click(within(dialog).getByRole("button", { name: "Create" }));

    expect(api.createDeployment).not.toHaveBeenCalled();
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("Target is required");
    expect(alert).not.toHaveTextContent("Package is required");
  });

  it("refuses a target the operator only typed into the picker without choosing", async () => {
    // The picker's box FILTERS, it does not accept. Typing a hostname (or a
    // UUID) and submitting must not send anything -- otherwise the free-text
    // field this cycle removed is still there, wearing a listbox.
    const { user, dialog } = await openDialog();
    await pickEntity(user, "Package", /Notepad\+\+ 8\.6/);
    await user.type(within(dialog).getByRole("combobox", { name: "Target" }), "WKS-042");
    await user.click(within(dialog).getByRole("button", { name: "Create" }));

    expect(api.createDeployment).not.toHaveBeenCalled();
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Target is required");
  });

  it("submits on Enter from the window field", async () => {
    // The finding itself: the controls were not in a `<form>`, so the submit
    // button was an orphan and Enter was a no-op on this route.
    const { user, dialog } = await openDialog();
    await pickEntity(user, "Package", /Notepad\+\+ 8\.6/);
    await pickEntity(user, "Target", /WKS-042/);
    // The window is painted on a grid now; the expression itself lives in the
    // painter's Advanced field, which is the text control Enter is typed into.
    await user.type(within(dialog).getByLabelText("Cron expression"), "{Enter}");

    expect(api.createDeployment).toHaveBeenCalledWith({
      package_id: "pkg1",
      action: "install",
      target_type: "device",
      target_id: "dev1",
      window_cron: null,
    });
  });

  it("tells the operator when the server refuses, instead of showing nothing", async () => {
    // The bodyless 503 the form kernel exists for: `String(create.error)`
    // rendered "Error: " at best and an empty string at worst, so a failed
    // create was indistinguishable from one that never left.
    api.createDeployment = vi.fn().mockRejectedValue(new ApiError(503, ""));
    const { user, dialog } = await openDialog();
    await pickEntity(user, "Package", /Notepad\+\+ 8\.6/);
    await pickEntity(user, "Target", /WKS-042/);
    await user.click(within(dialog).getByRole("button", { name: "Create" }));

    const alert = await within(dialog).findByRole("alert");
    expect(alert.textContent?.trim()).not.toBe("");
    expect(alert).toHaveTextContent(/unavailable/i);
    // Still open: a dialog that closed here would read as success.
    expect(screen.getByRole("dialog", { name: "New deployment" })).toBeInTheDocument();
  });

  it("does not create when Cancel is pressed", async () => {
    // `ui.tsx`'s Button sets no `type`, and an untyped button inside a form
    // submits it — wrapping the panel in a `<form>` would have turned Cancel
    // into a second Create.
    const { user, dialog } = await openDialog();
    await pickEntity(user, "Package", /Notepad\+\+ 8\.6/);
    await pickEntity(user, "Target", /WKS-042/);
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));

    expect(api.createDeployment).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog", { name: "New deployment" })).not.toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------
// Cycle 5 (lib/datetime) and cycle 4 (describeSubmitError) in this module
// --------------------------------------------------------------------------

// The operator's profile zone, deliberately NOT the zone this suite runs in
// (vite.config.ts pins no TZ; the runner resolves to UTC). Each assertion
// below is written so the browser rendering and the profile rendering are
// DIFFERENT STRINGS — a test that passes either way proves nothing about
// whether `me.timezone` is read at all.
const ME_IN_NEW_YORK: Me = { ...ME, timezone: "America/New_York" };

const DEVICE_HISTORY: DeviceDeploymentHistory[] = [
  {
    deployment_id: "dep1",
    package_id: "pkg1",
    action: "install",
    state: "installed",
    exit_code: 0,
    log_ref: null,
    finished_at: "2026-01-02T00:00:00.000Z",
    created_at: "2026-01-01T00:00:00.000Z",
  },
  {
    deployment_id: "dep2",
    package_id: "pkg1",
    action: "install",
    state: "running",
    exit_code: null,
    log_ref: null,
    finished_at: null,
    created_at: "2026-01-02T00:00:00.000Z",
  },
];

describe("DeviceDeployTab renders dates in the profile's zone", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME_IN_NEW_YORK),
      getDeviceDeployments: vi.fn().mockResolvedValue(DEVICE_HISTORY),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  it("renders finished_at in the profile zone, not the browser's", async () => {
    // The tab is mounted by the devices module with only `deviceId`, so this
    // also asserts the zone reaches it without a prop chain.
    renderWithProviders(<DeviceDeployTab deviceId="dev1" />);

    expect(await screen.findByText("01 Jan 2026, 19:00")).toBeInTheDocument();
    expect(screen.queryByText("02 Jan 2026, 00:00")).not.toBeInTheDocument();
  });

  it("renders NO_DATE, never Invalid Date, for a deployment still running", async () => {
    renderWithProviders(<DeviceDeployTab deviceId="dev1" />);

    const row = (await screen.findByText("dep2")).closest("tr") as HTMLElement;
    // The LAST cell specifically — the Finished column. A row-wide query
    // would also match the empty Exit code beside it and pass for the wrong
    // reason.
    const cells = within(row).getAllByRole("cell");
    expect(cells[cells.length - 1]).toHaveTextContent(NO_DATE);
    expect(screen.queryByText(/Invalid Date/)).not.toBeInTheDocument();
  });
});

describe("deploy failure paths say what went wrong", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      listDeployments: vi.fn().mockResolvedValue(DEPLOYMENTS),
      listPackages: vi.fn().mockResolvedValue(PACKAGES),
      listDevices: vi.fn().mockResolvedValue(DEVICE_PAGE),
      executeDeployment: vi.fn(),
      deletePackage: vi.fn(),
      uploadPackageArtifact: vi.fn(),
      listScripts: vi.fn().mockResolvedValue(SCRIPTS),
      getScript: vi.fn().mockResolvedValue(SCRIPT_DETAIL),
      editScript: vi.fn(),
      runScript: vi.fn(),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  it("announces a bodyless 503 from Execute instead of an empty red line", async () => {
    // Execute dispatches signed jobs to a fleet. `ApiError` is built as
    // `new ApiError(status, await response.text())`, so the bodyless 503 a
    // proxy returns carries `message === ""` and `String(execute.error)`
    // rendered the literal "ApiError: " beside a row that had not changed.
    api.executeDeployment = vi.fn().mockRejectedValue(new ApiError(503, ""));
    const user = userEvent.setup();
    renderWithProviders(<DeployPage />);

    await screen.findByText("dep1");
    await user.click(screen.getByRole("button", { name: "Execute" }));
    const confirm = await screen.findByRole("dialog", { name: "Execute deployment" });
    await user.click(within(confirm).getByRole("button", { name: "Execute" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/unavailable right now/);
    expect(alert).not.toHaveTextContent(/ApiError/);
  });

  it("tells the operator when a package Delete is refused, instead of swallowing it", async () => {
    // THE SWALLOWED DESTRUCTIVE FAILURE. `PackagesPage` rendered nothing at
    // all for `remove.error`: the mutation rejected, the row stayed, and a
    // row that stays reads as a list that has not refreshed yet — not as a
    // refusal. The operator walks away believing the package is gone.
    api.deletePackage = vi.fn().mockRejectedValue(new ApiError(403, ""));
    const user = userEvent.setup();
    renderWithProviders(<PackagesPage />);

    await screen.findByText("Notepad++");
    await user.click(screen.getByRole("button", { name: /^More actions for / }));
    await user.click(screen.getByRole("menuitem", { name: "Delete" }));
    const confirm = await screen.findByRole("dialog", { name: "Delete package" });
    await user.click(within(confirm).getByRole("button", { name: "Delete package" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/do not have permission/);
    // Named, because this alert sits above a whole catalog.
    expect(alert).toHaveTextContent("Notepad++");
  });

  it("announces a failed artifact upload with a sentence", async () => {
    api.uploadPackageArtifact = vi.fn().mockRejectedValue(new ApiError(503, ""));
    const user = userEvent.setup();
    renderWithProviders(<PackagesPage />);

    await screen.findByText("Notepad++");
    await user.upload(
      screen.getByLabelText("Installer file for Notepad++"),
      new File(["bytes"], "app.msi"),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(/unavailable right now/);
  });

  it("translates a 422 from Run script rather than printing the raw payload", async () => {
    // Run dispatches a signed job to a device or a whole group. The old
    // `String(run.error)` printed FastAPI's `{"detail":[…]}` verbatim.
    api.runScript = vi.fn().mockRejectedValue(
      new ApiError(
        422,
        JSON.stringify({
          detail: [
            {
              type: "uuid_parsing",
              loc: ["body", "target_id"],
              msg: "Input should be a valid UUID",
              input: "not-a-uuid",
            },
          ],
        }),
      ),
    );
    const user = userEvent.setup();
    renderWithProviders(<ScriptsPage />);

    await screen.findByText("cleanup-temp");
    await user.click(screen.getByRole("button", { name: "Run" }));
    await pickEntity(user, "Target", /WKS-042/);
    await user.click(screen.getByRole("button", { name: "Run script" }));
    const confirm = await screen.findByRole("dialog", { name: "Run script as SYSTEM" });
    await user.click(within(confirm).getByRole("button", { name: "Run script" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Target: Input should be a valid UUID");
    expect(alert).not.toHaveTextContent("uuid_parsing");
    expect(alert).not.toHaveTextContent("detail");
  });

  it("announces a failed script save, whose body runs as SYSTEM when it succeeds", async () => {
    api.editScript = vi.fn().mockRejectedValue(new ApiError(503, ""));
    const user = userEvent.setup();
    renderWithProviders(<ScriptsPage />);

    await screen.findByText("cleanup-temp");
    await user.click(screen.getByRole("button", { name: "Edit" }));
    await screen.findByDisplayValue(SCRIPT_DETAIL.body);
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/unavailable right now/);
  });

  it("says WHY the current script body could not be loaded, not just that it failed", async () => {
    // "Failed to load the current script body." read the same whether the
    // caller lacked the permission, the script was deleted under them, or the
    // service was down — and only one of those is worth retrying.
    api.getScript = vi.fn().mockRejectedValue(new ApiError(404, ""));
    const user = userEvent.setup();
    renderWithProviders(<ScriptsPage />);

    await screen.findByText("cleanup-temp");
    await user.click(screen.getByRole("button", { name: "Edit" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Failed to load the current script body.");
    expect(alert).toHaveTextContent(/no longer exists/);
  });
});

// ==========================================================================
// CYCLE 6/7/8 in this module.
//
// Auditor A, [BLOCKER], /deploy: "Deployments cannot be created — the Package
// dropdown is empty and Create silently does nothing." Auditor A, [HIGH], the
// same dialog: "'Target ID' requires the operator to type a raw device UUID by
// hand — no picker, no search, no validation."
//
// Every test below fails against the shipped dialog: it had a `<select>` with
// one dead option, a free-text UUID box, an unvalidated cron field whose own
// placeholder the server cannot parse, and a one-click Execute.
// ==========================================================================

describe("the package catalog is chosen, not typed — and says so when it is empty", () => {
  function api(packages: Package[]): Partial<ApiClient> {
    return {
      me: vi.fn().mockResolvedValue(ME),
      listDeployments: vi.fn().mockResolvedValue(DEPLOYMENTS),
      listPackages: vi.fn().mockResolvedValue(packages),
      listDevices: vi.fn().mockResolvedValue(DEVICE_PAGE),
      createDeployment: vi.fn(),
    };
  }

  async function open(packages: Package[]) {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(api(packages) as ApiClient);
    renderWithProviders(<DeployPage />);
    await screen.findByText("dep1");
    await user.click(screen.getByRole("button", { name: "New deployment" }));
    const dialog = await screen.findByRole("dialog", { name: "New deployment" });
    return { user, dialog };
  }

  it("offers the catalog as a searchable list, not a dropdown of ids", async () => {
    const { user, dialog } = await open(PACKAGES);
    const field = within(dialog).getByRole("combobox", { name: "Package" });
    // A `<select>` reports role combobox too, so the tag is the assertion.
    expect(field.tagName).toBe("INPUT");

    await user.click(field);
    // Name AND the two facts that decide whether it can install: a Windows
    // MSI aimed at a Linux endpoint is the mistake this list prevents.
    expect(
      await screen.findByRole("option", { name: /Notepad\+\+ 8\.6\s*windows · msi/ }),
    ).toBeInTheDocument();
  });

  it("says what would fill an empty catalog instead of dead-ending", async () => {
    // THE BLOCKER ITSELF. The shipped control offered "Select a package…" and
    // nothing else, with no explanation anywhere on screen.
    const { user, dialog } = await open([]);
    await user.click(within(dialog).getByRole("combobox", { name: "Package" }));

    expect(await screen.findByText(/No packages in the catalog/)).toBeInTheDocument();
    // ...and not asserted as the only possible cause: this read carries its
    // own permission.
    expect(screen.getByText(/permission to list them/)).toBeInTheDocument();
  });

  it("links to the screen that fills the catalog when it is empty", async () => {
    const { dialog } = await open([]);
    const link = within(dialog).getByRole("link", { name: /Add a package to the catalog/ });
    expect(link).toHaveAttribute("href", "/deploy/packages");
  });

  it("offers no such link once the catalog has something in it", async () => {
    const { dialog } = await open(PACKAGES);
    expect(
      within(dialog).queryByRole("link", { name: /Add a package to the catalog/ }),
    ).not.toBeInTheDocument();
  });

  it("still names the missing field rather than doing nothing, with an empty catalog", async () => {
    // "Create silently does nothing" was half the BLOCKER, and an empty
    // catalog is exactly when it happened.
    const { user, dialog } = await open([]);
    await user.click(within(dialog).getByRole("button", { name: "Create" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Package is required");
  });
});

describe("the deployment target is picked, and switching type does not carry it over", () => {
  async function openDialog() {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue({
      me: vi.fn().mockResolvedValue(ME),
      listDeployments: vi.fn().mockResolvedValue(DEPLOYMENTS),
      listPackages: vi.fn().mockResolvedValue(PACKAGES),
      listDevices: vi.fn().mockResolvedValue(DEVICE_PAGE),
      listDeviceGroups: vi.fn().mockResolvedValue(DEVICE_GROUP_PAGE),
      createDeployment: vi.fn().mockResolvedValue(DEPLOYMENTS[0]),
    } as unknown as ApiClient);
    renderWithProviders(<DeployPage />);
    await screen.findByText("dep1");
    await user.click(screen.getByRole("button", { name: "New deployment" }));
    const dialog = await screen.findByRole("dialog", { name: "New deployment" });
    return { user, dialog };
  }

  it("shows the agent state beside every device, so an offline one is visible first", async () => {
    // The picker header's own rule: the sublabel "is also what stops somebody
    // dispatching to a machine that cannot answer". The dashboard's two
    // 24-hour-old `patch.scan` jobs are what happens without it.
    const { user, dialog } = await openDialog();
    await user.click(within(dialog).getByRole("combobox", { name: "Target" }));

    expect(await screen.findByRole("option", { name: /WKS-042\s*windows · online/ })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /BUILD-01\s*linux · offline/ })).toBeInTheDocument();
  });

  it("submits the id behind the hostname the operator chose", async () => {
    const { user, dialog } = await openDialog();
    await pickEntity(user, "Package", /Notepad\+\+ 8\.6/);
    await pickEntity(user, "Target", /BUILD-01/);
    await user.click(within(dialog).getByRole("button", { name: "Create" }));

    expect(mockUseApi().createDeployment).toHaveBeenCalledWith(
      expect.objectContaining({ target_type: "device", target_id: "dev2" }),
    );
  });

  it("clears the chosen device when the target type changes to group", async () => {
    // A device id under `target_type: "group"` is a request the server has no
    // reason to refuse and every reason to misread.
    const { user, dialog } = await openDialog();
    await pickEntity(user, "Package", /Notepad\+\+ 8\.6/);
    await pickEntity(user, "Target", /WKS-042/);
    await user.selectOptions(within(dialog).getByLabelText("Target type"), "group");

    expect(within(dialog).getByLabelText("Target")).toHaveValue("");
    await user.click(within(dialog).getByRole("button", { name: "Create" }));
    expect(mockUseApi().createDeployment).not.toHaveBeenCalled();
  });

  it("offers device groups from a picker rather than a pasted id", async () => {
    const { user, dialog } = await openDialog();
    await user.selectOptions(within(dialog).getByLabelText("Target type"), "group");
    await pickEntity(user, "Target", /Build runners/);

    await pickEntity(user, "Package", /Notepad\+\+ 8\.6/);
    await user.click(within(dialog).getByRole("button", { name: "Create" }));

    expect(mockUseApi().createDeployment).toHaveBeenCalledWith(
      expect.objectContaining({ target_type: "group", target_id: "grp-build" }),
    );
  });

  it("says why the group picker is empty, rather than offering a dead end", async () => {
    // NOT the shared `openDialog()`: `CreateDeploymentDialog` is mounted
    // unconditionally (open or not), so the group list has already been
    // fetched by the time that helper's own mock would be in place.
    const user = userEvent.setup();
    mockUseApi.mockReturnValue({
      me: vi.fn().mockResolvedValue(ME),
      listDeployments: vi.fn().mockResolvedValue(DEPLOYMENTS),
      listPackages: vi.fn().mockResolvedValue(PACKAGES),
      listDevices: vi.fn().mockResolvedValue(DEVICE_PAGE),
      listDeviceGroups: vi.fn().mockResolvedValue({
        items: [],
        total: 0,
        ungrouped_device_count: 0,
        evaluation: null,
      }),
      createDeployment: vi.fn().mockResolvedValue(DEPLOYMENTS[0]),
    } as unknown as ApiClient);
    renderWithProviders(<DeployPage />);
    await screen.findByText("dep1");
    await user.click(screen.getByRole("button", { name: "New deployment" }));
    const dialog = await screen.findByRole("dialog", { name: "New deployment" });
    await user.selectOptions(within(dialog).getByLabelText("Target type"), "group");
    await user.click(within(dialog).getByRole("combobox", { name: "Target" }));

    expect(await within(dialog).findByText(/No device groups to choose from/)).toBeInTheDocument();
  });
});

describe("the maintenance window is printed the way it is painted", () => {
  it("says the days and the hours a window is open", () => {
    expect(windowLabel("0 2-4 * * 6")).toBe("Sat, 02:00 to 05:00");
    expect(windowLabel("0 22-23 * * 1-5")).toBe("Weekdays, 22:00 to 24:00");
    expect(windowLabel("0 1-4 * * *")).toBe("Every day, 01:00 to 05:00");
    expect(windowLabel("0 3 * * 0,6")).toBe("Weekends, 03:00 to 04:00");
  });

  // The hour a window closes is the hour AFTER the last one it holds: the
  // matcher lets a job start at any minute of hour 4, so `2-4` runs until
  // 05:00. `WeekPainter` writes the same sentence under its own grid, and
  // the two must not disagree about the same value.
  it("ends a window at the hour after its last open hour", () => {
    expect(windowLabel("0 0 * * 1")).toBe("Mon, 00:00 to 01:00");
    expect(windowLabel("0 0-23 * * 1")).toBe("Mon, all day");
  });

  it("names two separate stretches of hours rather than merging them", () => {
    expect(windowLabel("0 2-3,22-23 * * 1")).toBe("Mon, 02:00 to 04:00 and 22:00 to 24:00");
  });

  // An expression the grid shows as "custom expression" gets no sentence:
  // the caller prints the expression, which is honest, rather than a
  // description that would have to guess at a step or a day of the month.
  it("has no words for an expression the painter cannot draw", () => {
    expect(windowLabel("30 2 * * 6")).toBeNull();
    expect(windowLabel("0 2 1 * *")).toBeNull();
    expect(windowLabel("0 */4 * * *")).toBeNull();
    expect(windowLabel("0 2 * * SAT")).toBeNull();
    expect(windowLabel("not a cron")).toBeNull();
  });
});

describe("the maintenance window is validated against the matcher that reads it", () => {
  // `backend/app/core/schedule.py` calls `int()` on every cron term, so a
  // NAME raises `ValueError` inside `execute_deployment`. The field shipped
  // with `e.g. 0 2 * * SAT` as its placeholder — its own example was a value
  // that breaks the dispatch it schedules.
  it("rejects the placeholder this field used to carry", () => {
    expect(cronProblem("0 2 * * SAT")).toMatch(/names like "SAT" are not supported/);
  });

  it("accepts the numeric form the matcher can parse", () => {
    expect(cronProblem("0 2 * * 6")).toBeNull();
    expect(cronProblem("*/15 0-4 * * 1,3,5")).toBeNull();
  });

  it("treats an empty window as no window at all, which is what the server means", () => {
    expect(cronProblem("")).toBeNull();
    expect(cronProblem("   ")).toBeNull();
  });

  it("rejects a field count the matcher would throw on", () => {
    expect(cronProblem("0 2 * *")).toMatch(/5-field/);
    expect(cronProblem("0 2 * * 6 7")).toMatch(/5-field/);
  });

  it("rejects a value outside the range the matcher enforces", () => {
    // `_parse_cron_field` raises on out-of-range rather than never matching,
    // because a window that silently never opens disables patching forever.
    expect(cronProblem("0 24 * * 6")).toMatch(/hour of the window must be between 0 and 23/);
    expect(cronProblem("60 2 * * 6")).toMatch(/minute of the window must be between 0 and 59/);
  });

  it("refuses to create a deployment carrying a window the server cannot read", async () => {
    const user = userEvent.setup();
    const api = {
      me: vi.fn().mockResolvedValue(ME),
      listDeployments: vi.fn().mockResolvedValue(DEPLOYMENTS),
      listPackages: vi.fn().mockResolvedValue(PACKAGES),
      listDevices: vi.fn().mockResolvedValue(DEVICE_PAGE),
      createDeployment: vi.fn(),
    };
    mockUseApi.mockReturnValue(api as unknown as ApiClient);
    renderWithProviders(<DeployPage />);
    await screen.findByText("dep1");
    await user.click(screen.getByRole("button", { name: "New deployment" }));
    const dialog = await screen.findByRole("dialog", { name: "New deployment" });

    await pickEntity(user, "Package", /Notepad\+\+ 8\.6/);
    await pickEntity(user, "Target", /WKS-042/);
    // Typed into the painter's Advanced field: the grid cannot draw a name,
    // and the raw expression reaches `cronProblem` the same way either way.
    await user.type(within(dialog).getByLabelText("Cron expression"), "0 2 * * SAT");
    await user.click(within(dialog).getByRole("button", { name: "Create" }));

    expect(api.createDeployment).not.toHaveBeenCalled();
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(/SAT/);
  });

  it("states the zone the window is matched in", async () => {
    // `window_open` is called with `datetime.now(UTC)`. "2 AM where?" had no
    // answer anywhere in the dialog.
    const user = userEvent.setup();
    mockUseApi.mockReturnValue({
      me: vi.fn().mockResolvedValue(ME),
      listDeployments: vi.fn().mockResolvedValue(DEPLOYMENTS),
      listPackages: vi.fn().mockResolvedValue(PACKAGES),
      listDevices: vi.fn().mockResolvedValue(DEVICE_PAGE),
      createDeployment: vi.fn(),
    } as unknown as ApiClient);
    renderWithProviders(<DeployPage />);
    await screen.findByText("dep1");
    await user.click(screen.getByRole("button", { name: "New deployment" }));
    const dialog = await screen.findByRole("dialog", { name: "New deployment" });

    expect(within(dialog).getByText(/Maintenance window \(UTC, optional\)/)).toBeInTheDocument();
  });
});

describe("irreversible deploy actions name their subject before they happen", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      listDeployments: vi.fn().mockResolvedValue(DEPLOYMENTS),
      listPackages: vi.fn().mockResolvedValue(PACKAGES),
      listDevices: vi.fn().mockResolvedValue(DEVICE_PAGE),
      listScripts: vi.fn().mockResolvedValue(SCRIPTS),
      getScript: vi.fn().mockResolvedValue(SCRIPT_DETAIL),
      executeDeployment: vi.fn().mockResolvedValue({ job_ids: ["j1"] }),
      deletePackage: vi.fn().mockResolvedValue(undefined),
      runScript: vi.fn().mockResolvedValue({ job_ids: ["j1"] }),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  it("names the package and the machine before executing, not the two UUIDs", async () => {
    const user = userEvent.setup();
    renderWithProviders(<DeployPage />);

    await screen.findByText("dep1");
    await user.click(screen.getByRole("button", { name: "Execute" }));

    const confirm = await screen.findByRole("dialog", { name: "Execute deployment" });
    expect(confirm).toHaveTextContent("Install Notepad++ 8.6 on WKS-042");
    // The ids are what the row used to show and what nobody can check.
    expect(confirm).not.toHaveTextContent("pkg1");
  });

  it("dispatches nothing when the execute confirmation is cancelled", async () => {
    const user = userEvent.setup();
    renderWithProviders(<DeployPage />);

    await screen.findByText("dep1");
    await user.click(screen.getByRole("button", { name: "Execute" }));
    const confirm = await screen.findByRole("dialog", { name: "Execute deployment" });
    await user.click(within(confirm).getByRole("button", { name: "Cancel" }));

    expect(api.executeDeployment).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog", { name: "Execute deployment" })).not.toBeInTheDocument();
  });

  it("shows the package name in the deployments table instead of its id", async () => {
    renderWithProviders(<DeployPage />);

    expect(await screen.findByText("Notepad++ 8.6")).toBeInTheDocument();
    expect(screen.queryByText("pkg1")).not.toBeInTheDocument();
  });

  it("names the package version before deleting it, since the catalog carries several", async () => {
    const user = userEvent.setup();
    renderWithProviders(<PackagesPage />);

    await screen.findByText("Notepad++");
    await user.click(screen.getByRole("button", { name: /^More actions for / }));
    await user.click(screen.getByRole("menuitem", { name: "Delete" }));

    const confirm = await screen.findByRole("dialog", { name: "Delete package" });
    expect(confirm).toHaveTextContent("Notepad++ 8.6");
    expect(confirm).toHaveTextContent(/cannot be undone/);
    expect(api.deletePackage).not.toHaveBeenCalled();
  });

  it("deletes nothing when the delete confirmation is cancelled", async () => {
    const user = userEvent.setup();
    renderWithProviders(<PackagesPage />);

    await screen.findByText("Notepad++");
    await user.click(screen.getByRole("button", { name: /^More actions for / }));
    await user.click(screen.getByRole("menuitem", { name: "Delete" }));
    const confirm = await screen.findByRole("dialog", { name: "Delete package" });
    await user.click(within(confirm).getByRole("button", { name: "Cancel" }));

    expect(api.deletePackage).not.toHaveBeenCalled();
  });

  it("names the script and the machine before running it as SYSTEM", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ScriptsPage />);

    await screen.findByText("cleanup-temp");
    await user.click(screen.getByRole("button", { name: "Run" }));
    await pickEntity(user, "Target", /BUILD-01/);
    await user.click(screen.getByRole("button", { name: "Run script" }));

    const confirm = await screen.findByRole("dialog", { name: "Run script as SYSTEM" });
    expect(confirm).toHaveTextContent("cleanup-temp on BUILD-01");
    // No cancel path exists for a dispatched job, and the dialog says so.
    expect(confirm).toHaveTextContent(/the console cannot take it back/);
    expect(api.runScript).not.toHaveBeenCalled();
  });

  it("runs nothing when the script confirmation is cancelled", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ScriptsPage />);

    await screen.findByText("cleanup-temp");
    await user.click(screen.getByRole("button", { name: "Run" }));
    await pickEntity(user, "Target", /WKS-042/);
    await user.click(screen.getByRole("button", { name: "Run script" }));
    const confirm = await screen.findByRole("dialog", { name: "Run script as SYSTEM" });
    await user.click(within(confirm).getByRole("button", { name: "Cancel" }));

    expect(api.runScript).not.toHaveBeenCalled();
    // Back on the run form, with the target still chosen — cancelling the
    // confirmation is not cancelling the task.
    expect(screen.getByRole("dialog", { name: "Run cleanup-temp" })).toBeInTheDocument();
  });

  it("refuses to run a script with no target, naming the field", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ScriptsPage />);

    await screen.findByText("cleanup-temp");
    await user.click(screen.getByRole("button", { name: "Run" }));
    await user.click(screen.getByRole("button", { name: "Run script" }));

    expect(api.runScript).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog", { name: "Run script as SYSTEM" })).not.toBeInTheDocument();
    expect(await screen.findByRole("alert")).toHaveTextContent("Target is required");
  });
});

describe("the create forms name what is missing instead of a dead button", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      listPackages: vi.fn().mockResolvedValue(PACKAGES),
      listScripts: vi.fn().mockResolvedValue(SCRIPTS),
      listDevices: vi.fn().mockResolvedValue(DEVICE_PAGE),
      createPackage: vi.fn().mockResolvedValue(PACKAGES[0]),
      authorScript: vi.fn().mockResolvedValue(SCRIPT_DETAIL),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  it("names the empty package fields rather than disabling Create", async () => {
    // WAS `disabled={create.isPending || !name || !version}` — the click moved
    // nothing and named no field, and the guard did not even cover the other
    // two fields the server requires.
    const user = userEvent.setup();
    renderWithProviders(<PackagesPage />);

    await screen.findByText("Notepad++");
    await user.click(screen.getByRole("button", { name: "New package" }));
    const dialog = await screen.findByRole("dialog", { name: "New package" });
    await user.clear(within(dialog).getByLabelText("OS family"));
    await user.click(within(dialog).getByRole("button", { name: "Create" }));

    expect(api.createPackage).not.toHaveBeenCalled();
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("Name is required");
    expect(alert).toHaveTextContent("Version is required");
    expect(alert).toHaveTextContent("OS family is required");
  });

  it("creates a package on Enter, with its values trimmed", async () => {
    const user = userEvent.setup();
    renderWithProviders(<PackagesPage />);

    await screen.findByText("Notepad++");
    await user.click(screen.getByRole("button", { name: "New package" }));
    const dialog = await screen.findByRole("dialog", { name: "New package" });
    await user.type(within(dialog).getByLabelText("Name"), " 7-Zip ");
    await user.type(within(dialog).getByLabelText("Version"), "24.09{Enter}");

    expect(api.createPackage).toHaveBeenCalledWith({
      name: "7-Zip",
      version: "24.09",
      os_family: "windows",
      installer_type: "msi",
      install_args: null,
    });
  });

  it("names the empty script fields rather than disabling Save", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ScriptsPage />);

    await screen.findByText("cleanup-temp");
    await user.click(screen.getByRole("button", { name: "New script" }));
    const dialog = await screen.findByRole("dialog", { name: "New script" });
    await user.click(within(dialog).getByRole("button", { name: "Save" }));

    expect(api.authorScript).not.toHaveBeenCalled();
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("Name is required");
    expect(alert).toHaveTextContent("Body is required");
  });

  it("does not trim the script body, which is the text an interpreter will run", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ScriptsPage />);

    await screen.findByText("cleanup-temp");
    await user.click(screen.getByRole("button", { name: "New script" }));
    const dialog = await screen.findByRole("dialog", { name: "New script" });
    await user.type(within(dialog).getByLabelText("Name"), " reboot ");
    await user.type(within(dialog).getByLabelText("Body"), "  Restart-Computer  ");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));

    expect(api.authorScript).toHaveBeenCalledWith({
      name: "reboot",
      os_family: "windows",
      interpreter: "powershell",
      body: "  Restart-Computer  ",
    });
  });
});

describe("picker options carry the fact that decides the choice", () => {
  it("marks a device with no agent row, which cannot run anything", () => {
    const [withAgent, noAgent] = devicePickerOptions([
      device({ id: "d1", hostname: "WKS-042", os_family: "windows", agent_status: "online" }),
      device({ id: "d2", hostname: "SWITCH-1" }),
    ]);
    expect(withAgent?.sublabel).toBe("windows · online");
    expect(noAgent?.sublabel).toBe("no agent");
  });

  it("marks a package whose artifact was never uploaded", () => {
    // A catalog row with no bytes dispatches a job that cannot succeed.
    const [ready, empty] = packagePickerOptions([
      PACKAGES[0] as Package,
      { ...(PACKAGES[0] as Package), id: "pkg2", artifact_sha256: null },
    ]);
    expect(ready?.sublabel).toBe("windows · msi");
    expect(empty?.sublabel).toBe("windows · msi · no artifact");
  });
});

// --------------------------------------------------------------------------
// CYCLE 12. What was left on this dialog once the BLOCKER above it closed:
//
//   [HIGH] A72 "The deployment modal has no Escape handler — Escape does not
//   close it ... confirming the application registers NO Escape-to-close
//   handler rather than the keypress simply not being delivered."
//
//   [MEDIUM] A75 "Modal does not lock background scrolling ... with
//   `aria-modal="true"` set, `getComputedStyle(document.body).overflow` is
//   `auto` — the underlying page remains scrollable behind the dialog."
//
//   [LOW] A78 "'Success rate' shows an em-dash while sibling cards show 0 ...
//   mixing 0 and — for one state makes the reader hunt for a distinction that
//   does not exist."
// --------------------------------------------------------------------------

describe("the New deployment dialog can be left with the keyboard", () => {
  function api(): Partial<ApiClient> {
    return {
      me: vi.fn().mockResolvedValue(ME),
      listDeployments: vi.fn().mockResolvedValue(DEPLOYMENTS),
      listPackages: vi.fn().mockResolvedValue(PACKAGES),
      listDevices: vi.fn().mockResolvedValue(DEVICE_PAGE),
      createDeployment: vi.fn().mockRejectedValue(new ApiError(503, "")),
    };
  }

  async function openDialog() {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(api() as ApiClient);
    renderWithProviders(<DeployPage />);
    await screen.findByText("dep1");
    await user.click(screen.getByRole("button", { name: "New deployment" }));
    const dialog = await screen.findByRole("dialog", { name: "New deployment" });
    return { user, dialog };
  }

  it("closes on Escape", async () => {
    const { user } = await openDialog();
    await user.keyboard("{Escape}");

    expect(screen.queryByRole("dialog", { name: "New deployment" })).not.toBeInTheDocument();
  });

  it("clears a refusal on the way out, exactly as Cancel does", async () => {
    // This dialog is never unmounted — the page toggles `open` — so a 503 from
    // the last attempt would otherwise still be announced on reopening.
    const { user, dialog } = await openDialog();
    await pickEntity(user, "Package", /Notepad\+\+ 8\.6/);
    await pickEntity(user, "Target", /WKS-042/);
    await user.click(within(dialog).getByRole("button", { name: "Create" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(/unavailable/i);

    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("button", { name: "New deployment" }));

    const reopened = await screen.findByRole("dialog", { name: "New deployment" });
    expect(within(reopened).queryByRole("alert")).not.toBeInTheDocument();
  });

  it("locks the page behind it, and gives the scrollbar back on close", async () => {
    const { user } = await openDialog();
    expect(document.body.style.overflow).toBe("hidden");

    await user.keyboard("{Escape}");
    expect(document.body.style.overflow).toBe("");
  });

  it("does not lock the page when no dialog is open", async () => {
    mockUseApi.mockReturnValue(api() as ApiClient);
    renderWithProviders(<DeployPage />);
    await screen.findByText("dep1");

    expect(document.body.style.overflow).toBe("");
  });
});

describe("the stat cards use one convention for 'nothing to divide by'", () => {
  function withDeployments(deployments: Deployment[]): Partial<ApiClient> {
    return {
      me: vi.fn().mockResolvedValue(ME),
      listDeployments: vi.fn().mockResolvedValue(deployments),
      listPackages: vi.fn().mockResolvedValue(PACKAGES),
      listDevices: vi.fn().mockResolvedValue(DEVICE_PAGE),
    };
  }

  function successCard(): HTMLElement {
    const label = screen.getByText("Success rate");
    return label.closest(".stat-card") as HTMLElement;
  }

  it("says WHY the rate is a dash instead of leaving it beside three zeros", async () => {
    mockUseApi.mockReturnValue(withDeployments([]) as ApiClient);
    renderWithProviders(<DeployPage />);

    await screen.findByText("No deployments yet.");
    const card = successCard();
    expect(card.querySelector(".stat-value")?.textContent).toBe("—");
    expect(card).toHaveTextContent("No completed runs yet");
  });

  it("says what the rate is over once runs have finished", async () => {
    mockUseApi.mockReturnValue(
      withDeployments([
        { ...(DEPLOYMENTS[0] as Deployment), id: "d-ok", state: "complete" },
        { ...(DEPLOYMENTS[0] as Deployment), id: "d-bad", state: "failed" },
        // Still running: not part of a rate, and saying so is the point.
        { ...(DEPLOYMENTS[0] as Deployment), id: "d-run", state: "running" },
      ]) as ApiClient,
    );
    renderWithProviders(<DeployPage />);

    await screen.findByText("d-ok");
    const card = successCard();
    expect(card.querySelector(".stat-value")?.textContent).toBe("50%");
    expect(card).toHaveTextContent("1 of 2 completed");
  });

  it("keeps a genuine zero as a zero — nothing deployed is knowledge, not absence", async () => {
    mockUseApi.mockReturnValue(withDeployments([]) as ApiClient);
    renderWithProviders(<DeployPage />);

    await screen.findByText("No deployments yet.");
    const values = [...document.querySelectorAll(".stat-card")].map(
      (card) => card.querySelector(".stat-value")?.textContent,
    );
    expect(values).toEqual(["0", "0", "—", "0"]);
  });
});

describe("every dialog in this module answers Escape, not only the audited one", () => {
  it("closes the New package dialog", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue({
      me: vi.fn().mockResolvedValue(ME),
      listPackages: vi.fn().mockResolvedValue(PACKAGES),
    } as unknown as ApiClient);
    renderWithProviders(<PackagesPage />);
    await user.click(await screen.findByRole("button", { name: "New package" }));
    await screen.findByRole("dialog", { name: "New package" });

    expect(document.body.style.overflow).toBe("hidden");
    await user.keyboard("{Escape}");

    expect(screen.queryByRole("dialog", { name: "New package" })).not.toBeInTheDocument();
    expect(document.body.style.overflow).toBe("");
  });

  it("closes the Run script dialog — but lets its confirmation answer first", async () => {
    // Stacked surfaces: `ConfirmDialog` runs its own document-level Escape
    // handler, so without the guard one keypress cancels the confirmation AND
    // discards the target chosen behind it.
    const user = userEvent.setup();
    mockUseApi.mockReturnValue({
      me: vi.fn().mockResolvedValue(ME),
      listScripts: vi.fn().mockResolvedValue(SCRIPTS),
      listDevices: vi.fn().mockResolvedValue(DEVICE_PAGE),
      runScript: vi.fn(),
    } as unknown as ApiClient);
    renderWithProviders(<ScriptsPage />);
    await user.click(await screen.findByRole("button", { name: "Run" }));
    const dialog = await screen.findByRole("dialog", { name: "Run cleanup-temp" });

    await pickEntity(user, "Target", /WKS-042/);
    await user.click(within(dialog).getByRole("button", { name: "Run script" }));
    await screen.findByRole("dialog", { name: "Run script as SYSTEM" });

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Run script as SYSTEM" })).not.toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "Run cleanup-temp" })).toBeInTheDocument();

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Run cleanup-temp" })).not.toBeInTheDocument();
  });
});


/**
 * NO DEAD TILES. The operator's instruction, verbatim: "all tiles and all
 * numbers or kpis need to be clickable and direct the user to the specific
 * menus, i dont want dead tiles everywhere".
 *
 * The hrefs are asserted exactly AND each destination is opened, because a
 * tile linking to a filter this page no longer reads is indistinguishable
 * from a working one until somebody clicks it.
 */
describe("every deployment KPI tile is a link to the runs it counts", () => {
  const RECENT = new Date(Date.now() - 2 * 24 * 60 * 60 * 1000).toISOString();
  const OLD = new Date(Date.now() - 60 * 24 * 60 * 60 * 1000).toISOString();

  const RUNS: Deployment[] = [
    { ...(DEPLOYMENTS[0] as Deployment), id: "dep-old", state: "pending", created_at: OLD },
    { ...(DEPLOYMENTS[0] as Deployment), id: "dep-ok", state: "complete", created_at: RECENT },
    { ...(DEPLOYMENTS[0] as Deployment), id: "dep-bad", state: "failed", created_at: RECENT },
  ];

  function api(deployments: Deployment[] = RUNS): Partial<ApiClient> {
    return {
      me: vi.fn().mockResolvedValue(ME),
      listDeployments: vi.fn().mockResolvedValue(deployments),
      listPackages: vi.fn().mockResolvedValue(PACKAGES),
      listDevices: vi.fn().mockResolvedValue(DEVICE_PAGE),
    };
  }

  function tile(label: string): HTMLElement {
    // `.stat-card`, not `article`: a tile is an anchor now.
    const card = screen
      .getAllByText(label)
      .map((node) => node.closest(".stat-card"))
      .find(Boolean);
    if (!card) throw new Error(`no stat tile labelled ${label}`);
    return card as HTMLElement;
  }

  it("renders every tile as an anchor with an href the router serves", async () => {
    mockUseApi.mockReturnValue(api() as ApiClient);
    renderWithProviders(<DeployPage />);
    await screen.findByText("dep-old");

    expect(
      [...document.querySelectorAll(".stat-card")].map((card) => ({
        tag: card.tagName,
        href: card.getAttribute("href"),
      })),
    ).toEqual([
      { tag: "A", href: "/deploy" },
      { tag: "A", href: "/deploy?state=week" },
      { tag: "A", href: "/deploy?state=complete" },
      { tag: "A", href: "/deploy?state=failed" },
    ]);
  });

  it("lands the Failed tile on the failed run, not on the whole list", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(api() as ApiClient);
    renderWithProviders(<DeployPage />);
    await screen.findByText("dep-old");

    await user.click(within(tile("Failed")).getByText("Failed"));

    expect(screen.getByText("dep-bad")).toBeInTheDocument();
    expect(screen.queryByText("dep-ok")).not.toBeInTheDocument();
    expect(screen.queryByText("dep-old")).not.toBeInTheDocument();
  });

  it("lands the week tile on the runs it counted, and agrees with its own number", async () => {
    mockUseApi.mockReturnValue(api() as ApiClient);
    renderWithProviders(<DeployPage />, "/deploy?state=week");
    await screen.findByText("dep-ok");

    expect(tile("Deployed this week")).toHaveTextContent("2");
    expect(screen.getByText("dep-bad")).toBeInTheDocument();
    // Sixty days old: counted in the total, not in the week.
    expect(screen.queryByText("dep-old")).not.toBeInTheDocument();
  });

  it("lands the success-rate tile on the completed runs", async () => {
    mockUseApi.mockReturnValue(api() as ApiClient);
    renderWithProviders(<DeployPage />, "/deploy?state=complete");
    await screen.findByText("dep-ok");

    expect(screen.queryByText("dep-bad")).not.toBeInTheDocument();
    expect(screen.queryByText("dep-old")).not.toBeInTheDocument();
  });

  it("gives the reader a control that clears what a tile set", async () => {
    // A filter a tile can set and nothing on screen can undo is a trap door.
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(api() as ApiClient);
    renderWithProviders(<DeployPage />, "/deploy?state=failed");
    await screen.findByText("dep-bad");

    const select = screen.getByRole("combobox", { name: "Filter by state" });
    expect(select).toHaveValue("failed");
    await user.selectOptions(select, "");
    expect(screen.getByText("dep-old")).toBeInTheDocument();
  });

  it("makes only the tile that needs a person loud, and colours by category", async () => {
    mockUseApi.mockReturnValue(api() as ApiClient);
    renderWithProviders(<DeployPage />);
    await screen.findByText("dep-old");

    expect(tile("Failed")).toHaveClass("urgent");
    expect(tile("Failed")).toHaveClass("tone-red");
    expect(tile("Deployments")).not.toHaveClass("urgent");
    expect(tile("Deployments")).toHaveClass("tone-neutral");
    expect(tile("Success rate")).toHaveClass("tone-green");
    expect(tile("Success rate")).not.toHaveClass("urgent");
  });

  it("says which zero a zero is, and drops the alarm from it", async () => {
    // "Failed 0" in saturated red reads as a perfect score; and a bare 0 does
    // not say whether nothing failed or nothing ran.
    mockUseApi.mockReturnValue(api([RUNS[1] as Deployment]) as ApiClient);
    renderWithProviders(<DeployPage />);
    await screen.findByText("dep-ok");

    expect(tile("Failed")).toHaveTextContent("0");
    expect(tile("Failed")).not.toHaveClass("urgent");
    expect(tile("Failed")).toHaveTextContent("No run has failed");
    // The rate's own "nothing measured" case is a dash with a reason, not a
    // zero — so it carries no zero label at all.
    expect(tile("Success rate")).toHaveTextContent("100%");
  });
});
