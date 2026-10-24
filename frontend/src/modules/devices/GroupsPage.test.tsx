/**
 * The device groups console (cycle 1 of the device-group management design).
 *
 * FOUR PROPERTIES THIS FILE EXISTS TO HOLD, each one a finding the UX
 * remediation programme closed elsewhere and this page could reopen:
 *
 *   1. THE TREE RENDERS THE SERVER'S ORDER. `(precedence, name)` is what
 *      `inventory/rules.py` walks to decide which membership rule claims a
 *      device, and `ORDER BY` runs in the database's collation. A page that
 *      re-sorted with `localeCompare` would answer "which rule wins"
 *      differently from the code that decides it. Falsified below by feeding a
 *      deliberately non-alphabetical order and asserting the DOM matches it
 *      rather than the alphabet.
 *   2. THE FIRST ROW IS NOT A GROUP. `device_group_id IS NULL` is the ORG
 *      DEFAULT to every policy resolver, so the ungrouped count is the widest
 *      scope on the screen and the only one with no id.
 *   3. NO RAW JSON AND NO HAND-TYPED UUID. No textarea, no free-text id field,
 *      and the archive refusal rendered as places to go rather than as the
 *      `blocked_by` object it arrives as.
 *   4. EVERY WRITE CONTROL IS GATED ON `devices.manage` — presentation only,
 *      since the server decides, but a control that always 403s is the defect
 *      the closeout filed against half the console.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type {
  ApiClient,
  Device,
  DeviceGroup,
  DeviceGroupPage,
  DeviceGroupReferences,
  GroupRules,
  Me,
  MembershipEvaluation,
  RulePreview,
} from "../../lib/api";
import { ApiError } from "../../lib/api";
import {
  GroupsPage,
  buildTree,
  factPath,
  fromDocument,
  parseBlockedBy,
  previewSentence,
  toDocument,
  valueControl,
} from "./GroupsPage";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

function group(overrides: Partial<DeviceGroup> = {}): DeviceGroup {
  return {
    id: "g-1",
    name: "servers",
    parent_group_id: null,
    description: "",
    dynamic: false,
    device_count: 0,
    precedence: 0,
    archived_at: null,
    ...overrides,
  };
}

function device(overrides: Partial<Device> = {}): Device {
  return {
    id: "d-1",
    hostname: "ws-01",
    fqdn: null,
    os_family: "windows",
    os_version: "11",
    architecture: "x86_64",
    device_group_id: null,
    status: "active",
    agent_status: "online",
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

const listDeviceGroups = vi.fn();
const listDevices = vi.fn();
const getDeviceGroup = vi.fn();
const previewDeviceGroupRules = vi.fn();
const createDeviceGroup = vi.fn();
const updateDeviceGroup = vi.fn();
const archiveDeviceGroup = vi.fn();
const unarchiveDeviceGroup = vi.fn();
const addDeviceGroupMembers = vi.fn();
const removeDeviceGroupMember = vi.fn();

/** `/me`'s `org_id`: the organisation's, and its internal client's, id. */
const ORG = "00000000-0000-4000-8000-00000000000a";

function renderPage(permissions: string[] = ["devices.read", "devices.manage"]) {
  mockUseApi.mockReturnValue({
    listDeviceGroups,
    listDevices,
    getDeviceGroup,
    previewDeviceGroupRules,
    createDeviceGroup,
    updateDeviceGroup,
    archiveDeviceGroup,
    unarchiveDeviceGroup,
    addDeviceGroupMembers,
    removeDeviceGroupMember,
    // `org_id` is the internal client's id (MSP clients, phase 1): a new
    // group, and the preview of one, are filed under it.
    me: vi.fn().mockResolvedValue({ permissions, org_id: ORG } as Me),
  } as unknown as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const ui: ReactElement = (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <GroupsPage />
      </MemoryRouter>
    </QueryClientProvider>
  );
  return render(ui);
}

/**
 * A page with a RECENT sweep behind it, unless a test says otherwise.
 *
 * DEFAULTED RATHER THAN OMITTED, because "the sweep is healthy" is the state
 * every test that is about something else needs: leave `evaluation` null and
 * every one of them renders the "never re-evaluated" alert, which would put a
 * second `role="alert"` on the page and quietly change what the tests that
 * assert on a specific failure resolve to.
 */
function evaluated(overrides: Partial<MembershipEvaluation> = {}): MembershipEvaluation {
  return {
    last_evaluated_at: new Date(Date.now() - 60_000).toISOString(),
    last_full_pass_at: new Date(Date.now() - 60_000).toISOString(),
    full_pass: false,
    groups_evaluated: 1,
    devices_evaluated: 3,
    devices_moved: 0,
    devices_without_inventory: 0,
    interval_seconds: 300,
    ...overrides,
  };
}

function page(
  items: DeviceGroup[],
  ungrouped = 0,
  evaluation: MembershipEvaluation | null = evaluated(),
): DeviceGroupPage {
  return { items, total: items.length, ungrouped_device_count: ungrouped, evaluation };
}

beforeEach(() => {
  mockUseApi.mockReset();
  listDeviceGroups.mockReset().mockResolvedValue(page([group()]));
  listDevices.mockReset().mockResolvedValue({ items: [], total: 0 });
  createDeviceGroup.mockReset().mockResolvedValue(group());
  updateDeviceGroup.mockReset().mockResolvedValue(group());
  archiveDeviceGroup.mockReset().mockResolvedValue(undefined);
  unarchiveDeviceGroup.mockReset().mockResolvedValue(group());
  addDeviceGroupMembers.mockReset().mockResolvedValue({ results: [] });
  removeDeviceGroupMember.mockReset().mockResolvedValue(undefined);
  getDeviceGroup.mockReset().mockResolvedValue({
    group: group(),
    rules: null,
    references: references(),
  });
  previewDeviceGroupRules.mockReset().mockResolvedValue({
    matched: 0,
    devices_without_inventory: 0,
    sample: [],
    sample_truncated: false,
  });
});

function references(over: Partial<DeviceGroupReferences> = {}): DeviceGroupReferences {
  return {
    devices: 0,
    child_groups: 0,
    policies: 0,
    patch_policies: 0,
    backup_policies: 0,
    remote_policies: 0,
    health_assignments: 0,
    credentials: 0,
    ...over,
  };
}

describe("ordering", () => {
  it("renders the server's sequence and never the alphabet", async () => {
    // Deliberately non-alphabetical: `zulu` first because its precedence is
    // lower, which is exactly what the evaluator would do. A page sorting
    // client-side would put `alpha` first and disagree with the code that
    // decides which rule claims a device.
    listDeviceGroups.mockResolvedValue(
      page([
        group({ id: "g-z", name: "zulu", precedence: -1 }),
        group({ id: "g-a", name: "alpha", precedence: 0 }),
        group({ id: "g-m", name: "mike", precedence: 9 }),
      ]),
    );
    renderPage();

    const rows = await screen.findAllByRole("row");
    // Row 0 is the header, row 1 is the ungrouped row (see below).
    const names = rows.slice(2).map((row) => within(row).getAllByRole("cell")[0]?.textContent);
    expect(names).toEqual(["zulu", "alpha", "mike"]);
  });

  it("nests children under parents without reordering either", () => {
    // A unit on the pure function, because the DOM assertion above cannot
    // distinguish "kept the order" from "sorted into the same order".
    const rows = buildTree([
      group({ id: "b", name: "b" }),
      group({ id: "b-child", name: "z-child", parent_group_id: "b" }),
      group({ id: "b-child-2", name: "a-child", parent_group_id: "b" }),
      group({ id: "a", name: "a" }),
    ]);
    expect(rows.map((row) => [row.group.id, row.depth])).toEqual([
      ["b", 0],
      ["b-child", 1],
      ["b-child-2", 1],
      ["a", 0],
    ]);
  });

  it("shows a group whose parent is not on the page rather than dropping it", () => {
    // An archived parent is absent from the default list. A row that vanishes
    // is worse than a row shown one level too high.
    const rows = buildTree([group({ id: "orphan", parent_group_id: "gone" })]);
    expect(rows).toEqual([{ group: rows[0]?.group, depth: 0 }]);
  });

  /**
   * THE INDENT MOVES THE WHOLE ROW LEAD, NOT JUST THE WORDS (design review
   * I4).
   *
   * The review measured `Build runners` starting ~20px right of its siblings
   * while every building glyph in the column shared one x, and read the GROUP
   * column as ragged rather than as nested — correctly, because an indent that
   * leaves the leading glyph behind is not a nesting cue. The depth was real
   * (that fixture row's parent is `Production servers`); the CUE was not.
   *
   * ASSERTED ON THE CELL, because that is the element that has to carry it: on
   * the two text divs inside it, glyph and words disagree.
   * FALSIFIER: move the `paddingLeft` back onto `.cell-title` / `.cell-sub`
   * and both halves of this fail.
   */
  it("indents a nested row by the cell, so its glyph steps in with its name", async () => {
    listDeviceGroups.mockResolvedValue(
      page([
        group({ id: "g-srv", name: "Production servers" }),
        group({ id: "g-build", name: "Build runners", parent_group_id: "g-srv" }),
      ]),
    );
    renderPage();

    const parent = (await screen.findByText("Production servers")).closest("td") as HTMLElement;
    const child = (await screen.findByText("Build runners")).closest("td") as HTMLElement;

    // Depth 0 states no measure of its own and keeps `td`'s stylesheet padding.
    expect(parent.getAttribute("style")).toBeNull();
    // Depth 1 restates that 12px and adds the 18px step, on the CELL.
    expect(child.style.paddingLeft).toBe("30px");

    // And nothing inside the cell indents on its own any more — that is what
    // used to leave the glyph behind.
    for (const inner of child.querySelectorAll(".cell-title, .cell-sub")) {
      expect((inner as HTMLElement).style.paddingLeft).toBe("");
    }
  });

  /**
   * AND THE NESTING IS AVAILABLE WITHOUT SEEING IT. 18px of whitespace is not
   * a fact a screen reader can report, and `aria-level` belongs to a
   * `treegrid`, which this plain table is not. The parent is named instead.
   */
  it("names the parent for a reader that cannot see the indent", async () => {
    listDeviceGroups.mockResolvedValue(
      page([
        group({ id: "g-srv", name: "Production servers" }),
        group({ id: "g-build", name: "Build runners", parent_group_id: "g-srv" }),
      ]),
    );
    renderPage();

    const child = (await screen.findByText("Build runners")).closest("td") as HTMLElement;
    expect(child.textContent).toContain("inside Production servers");
    // A top-level row says nothing of the sort.
    const parent = (await screen.findByText("Production servers")).closest("td") as HTMLElement;
    expect(parent.textContent).not.toContain("inside");
  });

  /**
   * AND IT AGREES WITH `buildTree`. A group whose parent is not on this page
   * is drawn at the top level; describing it as nested would then be a
   * sentence the layout contradicts.
   */
  it("says nothing about a parent that is not on the page", async () => {
    listDeviceGroups.mockResolvedValue(
      page([group({ id: "orphan", name: "Orphan group", parent_group_id: "gone" })]),
    );
    renderPage();

    const row = (await screen.findByText("Orphan group")).closest("td") as HTMLElement;
    expect(row.getAttribute("style")).toBeNull();
    expect(row.textContent).not.toContain("inside");
  });
});

describe("the organisation default", () => {
  it("sizes the scope that has no id, as the first row", async () => {
    listDeviceGroups.mockResolvedValue(page([group({ device_count: 3 })], 17));
    renderPage();

    const rows = await screen.findAllByRole("row");
    const first = within(rows[1] as HTMLElement);
    expect(first.getByText("No group")).toBeTruthy();
    expect(
      first.getByText(/Governed by the organisation default policy/),
    ).toBeTruthy();
    expect(first.getAllByRole("cell")[2]?.textContent).toBe("17");
  });
});

describe("permissions", () => {
  it("offers no write control to an operator holding only devices.read", async () => {
    renderPage(["devices.read"]);
    await screen.findByText("servers");

    expect(screen.queryByRole("button", { name: "New group" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Edit" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Archive" })).toBeNull();
  });

  it("offers them to a holder of devices.manage -- the same assertions, inverted", async () => {
    renderPage();
    await screen.findByText("servers");

    expect(screen.getByRole("button", { name: "New group" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Edit" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Archive" })).toBeTruthy();
  });
});

describe("the editor", () => {
  it("takes a parent by picker and a precedence by number, and no JSON anywhere", async () => {
    listDeviceGroups.mockResolvedValue(
      page([group({ id: "g-p", name: "parent" }), group({ id: "g-c", name: "child" })]),
    );
    const user = userEvent.setup();
    const { container } = renderPage();
    await screen.findByText("parent");

    await user.click(screen.getByRole("button", { name: "New group" }));

    // Closeout rec. 4 ("no raw JSON in the UI") and rec. 6 ("kill the
    // hand-typed UUID"), asserted structurally rather than by reading copy.
    expect(container.querySelector("textarea")).toBeNull();
    expect(screen.getByRole("combobox", { name: "Parent group (optional)" })).toBeTruthy();

    await user.type(screen.getByLabelText("Name"), "workstations");
    await user.clear(screen.getByLabelText("Precedence"));
    await user.type(screen.getByLabelText("Precedence"), "5");
    await user.click(screen.getByRole("button", { name: "Create group" }));

    expect(createDeviceGroup).toHaveBeenCalledWith({
      name: "workstations",
      description: "",
      parent_group_id: null,
      precedence: 5,
      // Explicitly `null`, not omitted: this group is hand-picked, and on a
      // PATCH the difference between "absent" and "null" is the difference
      // between leaving a rules document alone and clearing it.
      rules: null,
      // RE-POINTED (MSP clients, phase 1): a group organises one client's
      // devices and `POST /device-groups` requires it. Until the client
      // selector (phase 3) that is the internal client, whose id is `/me`'s
      // `org_id`.
      client_id: ORG,
    });
  });

  it("never sends a client when it edits a group, whose client is fixed", async () => {
    // `client_id` is fixed for a group's life; the server's update model has
    // no such field, so sending one would be a 422 on every edit.
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("servers");

    await user.click(screen.getByRole("button", { name: "Edit" }));
    await user.click(await screen.findByRole("button", { name: "Save group" }));

    await waitFor(() => expect(updateDeviceGroup).toHaveBeenCalledTimes(1));
    expect(updateDeviceGroup.mock.calls[0]?.[1]).not.toHaveProperty("client_id");
  });

  it("refuses to send a group with no name, and says which field", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("servers");

    await user.click(screen.getByRole("button", { name: "New group" }));
    await user.click(screen.getByRole("button", { name: "Create group" }));

    expect(createDeviceGroup).not.toHaveBeenCalled();
    expect(screen.getByRole("alert").textContent).toContain("Name is required");
  });
});

describe("archiving", () => {
  it("confirms before archiving, naming the group and what an archive does", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("servers");

    await user.click(screen.getByRole("button", { name: "Archive" }));
    const dialog = screen.getByRole("dialog", { name: "Archive device group" });
    expect(within(dialog).getByText("servers")).toBeTruthy();
    expect(archiveDeviceGroup).not.toHaveBeenCalled();

    await user.click(within(dialog).getByRole("button", { name: "Archive group" }));
    expect(archiveDeviceGroup).toHaveBeenCalledWith("g-1");
  });

  it("renders the 409 as places to go, not as a JSON blob", async () => {
    // The body the server sends: `blocked_by` beside `detail`, counted per
    // kind. A refusal the operator cannot act on is a dead end.
    archiveDeviceGroup.mockRejectedValue(
      new ApiError(
        409,
        JSON.stringify({
          detail: "this group is still in use",
          blocked_by: {
            devices: 12,
            child_groups: 1,
            policies: 0,
            patch_policies: 1,
            backup_policies: 0,
            remote_policies: 0,
            health_assignments: 0,
            credentials: 0,
            // A key an older server sent: a role grant named a group until
            // grants were scoped by client (MSP clients spec §2.1), and the
            // server has not counted them since. Never drawn.
            role_grants: 2,
          },
        }),
      ),
    );
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("servers");

    await user.click(screen.getByRole("button", { name: "Archive" }));
    await user.click(screen.getByRole("button", { name: "Archive group" }));

    const dialog = await screen.findByRole("dialog", {
      name: "servers is still in use",
    });
    expect(within(dialog).getByRole("link", { name: /12 devices/ }).getAttribute("href")).toBe(
      "/devices",
    );
    expect(within(dialog).getByRole("link", { name: /1 patch policy/ })).toBeTruthy();
    // RE-POINTED: a role grant no longer names a group, so it is no longer a
    // blocker the console lists -- even from a body that still carries it.
    expect(within(dialog).queryByText(/role grant/)).toBeNull();
    // The blockers that are zero are absent, and nothing on screen is the raw
    // object.
    expect(within(dialog).queryByText(/blocked_by/)).toBeNull();
    expect(within(dialog).queryByText(/backup polic/)).toBeNull();
    // `child_groups` is the one blocker with nowhere to send them: the thing
    // holding the group is this page.
    expect(within(dialog).getByText(/1 child group beneath it/)).toBeTruthy();
    expect(
      within(dialog).queryByRole("link", { name: /child group/ }),
    ).toBeNull();
  });

  it("offers unarchive on an archived row, and nothing else", async () => {
    listDeviceGroups.mockResolvedValue(
      page([group({ archived_at: "2026-08-22T09:00:00Z" })]),
    );
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("servers");

    expect(screen.queryByRole("button", { name: "Archive" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Edit" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Unarchive" }));
    expect(unarchiveDeviceGroup).toHaveBeenCalledWith("g-1");
  });

  it("asks the server for archived rows only when the operator asks for them", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("servers");
    expect(listDeviceGroups).toHaveBeenCalledWith({ include_archived: false });

    await user.click(screen.getByRole("checkbox", { name: "Show archived" }));
    expect(listDeviceGroups).toHaveBeenCalledWith({ include_archived: true });
  });
});

describe("membership", () => {
  it("adds and removes by picker, never by typed id", async () => {
    listDevices.mockResolvedValue({
      items: [device({ id: "d-in", hostname: "in-group", device_group_id: "g-1" }), device()],
      total: 2,
    });
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("servers");

    await user.click(screen.getByRole("button", { name: "Members" }));
    const dialog = screen.getByRole("dialog", { name: "Members of servers" });
    expect(within(dialog).getByText("in-group")).toBeTruthy();

    await user.click(within(dialog).getByRole("button", { name: "Remove" }));
    expect(removeDeviceGroupMember).toHaveBeenCalledWith("g-1", "d-in");

    await user.click(within(dialog).getByRole("combobox", { name: "Add a device" }));
    await user.click(within(dialog).getByRole("option", { name: /ws-01/ }));
    await user.click(within(dialog).getByRole("button", { name: "Add to group" }));
    expect(addDeviceGroupMembers).toHaveBeenCalledWith("g-1", ["d-1"]);
  });

  it("offers no hand placement on a rule-driven group, and says why", async () => {
    listDeviceGroups.mockResolvedValue(page([group({ dynamic: true })]));
    listDevices.mockResolvedValue({ items: [device()], total: 1 });
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("servers");

    await user.click(screen.getByRole("button", { name: "Members" }));
    const dialog = screen.getByRole("dialog", { name: "Members of servers" });
    expect(
      within(dialog).getByText(/devices cannot be placed in this group by hand/),
    ).toBeTruthy();
    expect(within(dialog).queryByRole("button", { name: "Add to group" })).toBeNull();
  });
});

describe("parseBlockedBy", () => {
  it("returns null for anything that is not a 409 carrying the object", () => {
    expect(parseBlockedBy(new ApiError(500, "boom"))).toBeNull();
    expect(parseBlockedBy(new ApiError(409, "not json"))).toBeNull();
    expect(parseBlockedBy(new ApiError(409, '{"detail":"no"}'))).toBeNull();
    expect(parseBlockedBy(new Error("nope"))).toBeNull();
  });
});

describe("empty and failed states", () => {
  it("says what a group is for when there are none", async () => {
    listDeviceGroups.mockResolvedValue(page([]));
    renderPage();
    expect(await screen.findByText("No groups yet")).toBeTruthy();
  });

  it("reports a failed load rather than rendering an empty fleet", async () => {
    listDeviceGroups.mockRejectedValue(new ApiError(403, "forbidden"));
    renderPage();
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.queryByText("No groups yet")).toBeNull();
  });
});

/**
 * A group holds computers, so an empty inventory is the one thing from another
 * module this page depends on (docs/design/cross-module-guidance.md §2,
 * Device groups). Read from the fleet counts every device list carries.
 */
describe("before a group has computers to hold", () => {
  const fleetPage = (total: number) => ({
    items: [],
    total: 0,
    state_counts: { all: 0, online: 0, offline: 0, assets: 0, certs: 0 },
    fleet: { total, managed: total, online: total, cert_attention: 0 },
  });

  it("sends an empty inventory to add a computer", async () => {
    listDevices.mockResolvedValue(fleetPage(0));
    renderPage();
    const link = await screen.findByRole("link", { name: "Add a computer" });
    expect(link).toHaveAttribute("href", "/devices/onboard");
    expect(screen.getByText("Before a group has computers to hold")).toBeInTheDocument();
  });

  it("is absent once a computer is recorded", async () => {
    listDevices.mockResolvedValue(fleetPage(4));
    renderPage();
    await screen.findByText("servers");
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(screen.queryByText("Before a group has computers to hold")).not.toBeInTheDocument();
  });

  it("says it cannot be checked from an account without the device permission", async () => {
    listDevices.mockResolvedValue(fleetPage(0));
    renderPage(["devices.manage"]);
    expect(await screen.findByText(/cannot be checked from this account/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Add a computer" })).not.toBeInTheDocument();
  });
});

/**
 * THE RULES BUILDER — cycle 2.
 *
 * A rules document decides which machines a signed job reaches, so these are
 * not tests of a form. Each one holds a property that, if it broke, would let
 * an operator aim a group at a set they did not choose:
 *
 *   * the fact list is a `<select>`, so `os_familly` cannot be typed — and
 *     "is not" HOLDS on a fact the evaluator does not know, so that typo would
 *     claim every device in the organisation;
 *   * the value control follows the fact's TYPE, because the evaluator
 *     compares with `==` and a boolean fact against the string "true" matches
 *     nothing while looking exactly right;
 *   * the reach is on screen before the save, and the no-inventory count is
 *     in the sentence, because a device that has never collected cannot be
 *     matched by any rule and an operator not told that concludes the rule is
 *     broken.
 */

describe("the rules vocabulary, as data", () => {
  it("coerces a value to the type the evaluator will compare it against", () => {
    // A byte count typed into a text box is a string, and `17179869184 == "17179869184"`
    // is FALSE in the evaluator -- a rule that reads right and claims nobody.
    expect(
      toDocument([{ kind: "flat", base: "ram_bytes", op: "eq", value: "17179869184" }]),
    ).toEqual({ all: [{ fact: "ram_bytes", op: "eq", value: 17179869184 }] });

    // `gt`/`lt` call `float()` on both sides, so the number is not optional.
    expect(
      toDocument([{ kind: "flat", base: "ram_bytes", op: "gt", value: "8000" }]),
    ).toEqual({ all: [{ fact: "ram_bytes", op: "gt", value: 8000 }] });

    // A known boolean fact sends a boolean.
    expect(
      toDocument([{ kind: "custom", base: "domain_joined", op: "eq", value: "true" }]),
    ).toEqual({ all: [{ fact: "facts.domain_joined", op: "eq", value: true }] });

    // And a string stays a string.
    expect(
      toDocument([{ kind: "flat", base: "hostname", op: "contains", value: "wks" }]),
    ).toEqual({ all: [{ fact: "hostname", op: "contains", value: "wks" }] });
  });

  it("emits nothing while a row is incomplete, rather than a document the server would refuse", () => {
    // A prefix with no remainder is `facts.`, which is a 422 -- and there is
    // nothing to preview yet either.
    expect(toDocument([{ kind: "custom", base: "  ", op: "eq", value: "x" }])).toBeNull();
    expect(toDocument([{ kind: "flat", base: "hostname", op: "eq", value: "" }])).toBeNull();
    expect(toDocument([])).toBeNull();
  });

  it("round-trips a stored document back into rows", () => {
    const rules: GroupRules = {
      all: [
        { fact: "os_family", op: "eq", value: "windows" },
        { fact: "facts.domain_joined", op: "eq", value: true },
        { fact: "software:7-Zip", op: "contains", value: "23." },
      ],
    };
    const rows = fromDocument(rules);
    expect(rows.map((row) => row.kind)).toEqual(["flat", "custom", "software"]);
    expect(rows.map(factPath)).toEqual([
      "os_family",
      "facts.domain_joined",
      "software:7-Zip",
    ]);
    expect(toDocument(rows)).toEqual(rules);
  });

  it("picks the control from the op and the fact, not from the value", () => {
    expect(valueControl({ kind: "flat", base: "ram_bytes", op: "gt", value: "" })).toBe(
      "number",
    );
    expect(
      valueControl({ kind: "custom", base: "domain_joined", op: "eq", value: "" }),
    ).toBe("boolean");
    expect(valueControl({ kind: "flat", base: "hostname", op: "eq", value: "" })).toBe(
      "text",
    );
    // A boolean fact under `gt` is a number box, because `gt` is what decides
    // how the evaluator will read it.
    expect(
      valueControl({ kind: "custom", base: "domain_joined", op: "gt", value: "" }),
    ).toBe("number");
  });

  it("names both numbers, including the devices no rule can ever match", () => {
    const preview: RulePreview = {
      matched: 37,
      devices_without_inventory: 18,
      sample: [],
      sample_truncated: true,
    };
    expect(previewSentence(preview, 412)).toBe(
      "Matches 37 of 412 devices. 18 devices have no inventory yet and cannot be matched.",
    );
    // The second sentence is dropped only when it would be a lie.
    expect(previewSentence({ ...preview, devices_without_inventory: 0 }, 412)).toBe(
      "Matches 37 of 412 devices.",
    );
  });
});

describe("the rules builder", () => {
  it("offers the closed fact list as a select, and no textarea anywhere", async () => {
    const user = userEvent.setup();
    const { container } = renderPage();
    await screen.findByText("servers");

    await user.click(screen.getByRole("button", { name: "New group" }));
    await user.selectOptions(
      screen.getByLabelText("Membership"),
      "Rule-driven: devices matched by their inventory",
    );

    // Closeout rec. 4: no raw JSON in the UI. A `DeviceGroup.rules` textarea
    // would have reintroduced it and rec. 6 in one control.
    expect(container.querySelector("textarea")).toBeNull();

    const fact = screen.getByLabelText("Fact") as HTMLSelectElement;
    const options = Array.from(fact.options).map((option) => option.value);
    // Exactly the evaluator's flat namespace, plus the two open prefixes.
    expect(options).toEqual([
      "hostname",
      "os_family",
      "os_version",
      "architecture",
      "ram_bytes",
      "serial",
      "facts.",
      "software:",
    ]);
    // `os_familly` is not on the list, so the disaster rule cannot be typed.
    expect(options).not.toContain("os_familly");
  });

  it("reveals a second labelled input for the two open-ended prefixes", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("servers");

    await user.click(screen.getByRole("button", { name: "New group" }));
    await user.selectOptions(
      screen.getByLabelText("Membership"),
      "Rule-driven: devices matched by their inventory",
    );
    expect(screen.queryByLabelText("Fact key")).toBeNull();

    await user.selectOptions(screen.getByLabelText("Fact"), "facts.");
    expect(screen.getByLabelText("Fact key")).toBeTruthy();

    await user.selectOptions(screen.getByLabelText("Fact"), "software:");
    expect(
      screen.getByLabelText("Software name (exactly as reported)"),
    ).toBeTruthy();
  });

  it("shows what the rule would claim, with both numbers, before it is saved", async () => {
    previewDeviceGroupRules.mockResolvedValue({
      matched: 37,
      devices_without_inventory: 18,
      sample: [
        {
          device_id: "d-1",
          hostname: "wks-042",
          current_group_id: null,
          current_group_name: null,
        },
      ],
      sample_truncated: true,
    } satisfies RulePreview);
    listDevices.mockResolvedValue({ items: [], total: 412 });

    const user = userEvent.setup();
    renderPage();
    await screen.findByText("servers");

    await user.click(screen.getByRole("button", { name: "New group" }));
    await user.selectOptions(
      screen.getByLabelText("Membership"),
      "Rule-driven: devices matched by their inventory",
    );
    await user.type(screen.getByLabelText("Value"), "windows");

    expect(
      await screen.findByText(
        "Matches 37 of 412 devices. 18 devices have no inventory yet and cannot be matched.",
      ),
    ).toBeTruthy();

    // The sample says where each device is NOW, because a rule TAKES a device
    // rather than adding one -- and "organisation default" is a policy set,
    // not an absence.
    expect(screen.getByText(/wks-042: organisation default/)).toBeTruthy();
    expect(previewDeviceGroupRules).toHaveBeenCalledWith(
      { all: [{ fact: "os_family", op: "eq", value: "windows" }] },
      null,
      // RE-POINTED: a group not saved yet has no client of its own, so the
      // preview names the one it will be created in -- the internal client.
      ORG,
    );
  });

  it("does not preview a half-written rule, and says so instead", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("servers");

    await user.click(screen.getByRole("button", { name: "New group" }));
    await user.selectOptions(
      screen.getByLabelText("Membership"),
      "Rule-driven: devices matched by their inventory",
    );

    expect(
      screen.getByText(/Finish the conditions above to see which devices/),
    ).toBeTruthy();
    // PAST THE DEBOUNCE, or this asserts only that 400ms has not elapsed --
    // which would pass with the guard deleted.
    await new Promise((resolve) => setTimeout(resolve, 600));
    expect(previewDeviceGroupRules).not.toHaveBeenCalled();
  });

  it("loads an existing group's document rather than presenting it as having none", async () => {
    const dynamic = group({ id: "g-dyn", name: "windows", dynamic: true });
    listDeviceGroups.mockResolvedValue(page([dynamic]));
    getDeviceGroup.mockResolvedValue({
      group: dynamic,
      rules: { all: [{ fact: "os_family", op: "eq", value: "windows" }] },
      references: references(),
    });

    const user = userEvent.setup();
    renderPage();
    await screen.findByText("windows");
    await user.click(screen.getByRole("button", { name: "Edit" }));

    // Opening the form WITHOUT this fetch would present a rule-driven group as
    // having no rules, and saving would then clear them -- the paged list route
    // deliberately does not carry the document.
    expect(getDeviceGroup).toHaveBeenCalledWith("g-dyn");
    const membership = await screen.findByLabelText("Membership");
    expect((membership as HTMLSelectElement).value).toBe("dynamic");
    expect((screen.getByLabelText("Fact") as HTMLSelectElement).value).toBe("os_family");
    expect((screen.getByLabelText("Value") as HTMLInputElement).value).toBe("windows");
  });

  it("warns before the save when the group already holds hand-placed devices", async () => {
    const occupied = group({ id: "g-occ", name: "servers", device_count: 12 });
    listDeviceGroups.mockResolvedValue(page([occupied]));
    getDeviceGroup.mockResolvedValue({
      group: occupied,
      rules: null,
      references: references({ devices: 12 }),
    });

    const user = userEvent.setup();
    renderPage();
    await screen.findByText("servers");
    await user.click(screen.getByRole("button", { name: "Edit" }));
    await user.selectOptions(
      await screen.findByLabelText("Membership"),
      "Rule-driven: devices matched by their inventory",
    );

    // The server answers 409. Saying so here is what stops the operator
    // discovering it after composing the rule.
    expect(screen.getByText(/already holds 12 hand-placed devices/)).toBeTruthy();
  });

  it("sends null to clear a document, which moves nobody", async () => {
    const dynamic = group({ id: "g-dyn", name: "windows", dynamic: true });
    listDeviceGroups.mockResolvedValue(page([dynamic]));
    getDeviceGroup.mockResolvedValue({
      group: dynamic,
      rules: { all: [{ fact: "os_family", op: "eq", value: "windows" }] },
      references: references(),
    });

    const user = userEvent.setup();
    renderPage();
    await screen.findByText("windows");
    await user.click(screen.getByRole("button", { name: "Edit" }));
    await user.selectOptions(
      await screen.findByLabelText("Membership"),
      "Hand-picked: devices added one by one",
    );
    await user.click(screen.getByRole("button", { name: "Save group" }));

    // `null`, not omitted. A rule only ever CLAIMS a device, never releases
    // one, so clearing the document leaves the members exactly where they are
    // and the group becomes hand-picked holding the same machines -- which is
    // what makes a rule-driven group emptiable, and therefore archivable.
    expect(updateDeviceGroup).toHaveBeenCalledWith(
      "g-dyn",
      expect.objectContaining({ rules: null }),
    );
  });
  // ------------------------------------------------------------------- //
  // "A TIMESTAMP, NEVER ALONE" (design §5.3, §9). The page used to state the
  // INTERVAL -- "membership can be up to five minutes behind" -- because the
  // sweep's only durable record was an audit event, and reading it needs
  // `audit.read`, a code this page's `devices.read` gate does not entitle.
  // The sentence was true and could not distinguish "swept four minutes ago"
  // from "the worker has been down since Tuesday".
  // ------------------------------------------------------------------- //

  it("says when membership was last re-evaluated, and what that pass did", async () => {
    listDeviceGroups.mockResolvedValue(
      page(
        [group()],
        2,
        evaluated({
          // Past the "just now" floor, which is 90 seconds: below the beat's
          // own resolution a count of minutes is noise, not information.
          last_evaluated_at: new Date(Date.now() - 4 * 60_000).toISOString(),
          devices_evaluated: 7,
          devices_moved: 3,
        }),
      ),
    );
    renderPage();

    const line = await screen.findByRole("status");
    expect(line).toHaveTextContent("Membership was last re-evaluated");
    expect(line).toHaveTextContent("4 minutes ago");
    // THE COUNTS BESIDE THE TIMESTAMP. A time on its own says the worker ran;
    // these say what it did, which is what makes a `device_count` in the table
    // readable rather than merely recent.
    expect(line).toHaveTextContent("7 devices whose inventory had changed");
    expect(line).toHaveTextContent("moved 3");
  });

  it("warns when the sweep is further behind than its own schedule", async () => {
    // TWO BEATS, NOT ONE. A single missed beat is a restart or a slow run and
    // means nothing; two consecutive misses mean the worker is not running,
    // which is the finding this record exists to surface. The threshold is a
    // multiple of the SERVER's `interval_seconds`, never a literal.
    listDeviceGroups.mockResolvedValue(
      page([group()], 0, evaluated({
        last_evaluated_at: new Date(Date.now() - 40 * 60_000).toISOString(),
        interval_seconds: 300,
      })),
    );
    renderPage();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("40 minutes ago");
    expect(alert).toHaveTextContent("longer ago than the 5-minute schedule");
    expect(alert).toHaveTextContent(/worker may not be running/);
  });

  it("does not report a never-run sweep as a recent one", async () => {
    // `null` IS A THIRD STATE AND MUST READ AS ONE. An organization whose
    // first sweep has not landed and one whose worker died are different
    // sentences, and rendering the first as "just now" is the exact defect
    // the whole record was added to fix.
    listDeviceGroups.mockResolvedValue(page([group()], 0, null));
    renderPage();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("never been re-evaluated");
    expect(alert).not.toHaveTextContent("just now");
    expect(alert).not.toHaveTextContent("ago");
  });

  it("claims nothing about the sweep while the list has not loaded", async () => {
    // A CLAIM ABOUT DATA, MADE FROM A SCREEN THAT HAS NOT BEEN TOLD. A failed
    // request must not render "never re-evaluated" -- that is an assertion
    // about the organization, not about the request.
    listDeviceGroups.mockRejectedValue(new ApiError(503, "unavailable"));
    renderPage();

    await screen.findByText(/unavailable|error|failed/i);
    expect(screen.queryByText(/last re-evaluated/)).not.toBeInTheDocument();
    expect(screen.queryByText(/never been re-evaluated/)).not.toBeInTheDocument();
  });
});
