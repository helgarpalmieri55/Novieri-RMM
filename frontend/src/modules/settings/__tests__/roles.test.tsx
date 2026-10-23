/**
 * Roles tab — `GET /permissions`, `GET/POST/PATCH/DELETE /roles`,
 * `PUT /roles/{id}/permissions`.
 *
 * THE MATRIX IS THE PRODUCT (spec §1.1), so most of this file is about the
 * matrix being the SERVER'S matrix. The catalogue fixture is deliberately not
 * the platform's real 92 codes: a console carrying its own copy of the code
 * list would pass a test written against the real one and would silently stop
 * showing codes added later. Here the fixture is small and invented, and one
 * test feeds a module name that exists nowhere in this repository.
 */

import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  AUDITOR_ROLE,
  ROLES_FIXTURE,
  SUPER_ADMIN_ROLE,
  TECHNICIAN_ROLE,
  apiClient,
  calls,
  installDefaults,
  renderSettings,
} from "./harness";

vi.mock("../../../lib/auth", () => ({
  accessToken: () => "test-token",
  oidcAuthority: "https://id.example.test/realms/it-suite",
}));

vi.mock("../../../lib/ApiContext", () => ({ useApi: () => apiClient() }));

beforeEach(() => {
  installDefaults();
});

async function openRoles() {
  const user = userEvent.setup();
  renderSettings();
  await user.click(await screen.findByRole("tab", { name: "Roles" }));
  return user;
}

describe("Settings — Roles tab", () => {
  it("says it is loading rather than rendering an empty catalogue", async () => {
    calls.listRoles.mockReturnValue(new Promise(() => {}));
    calls.listPermissions.mockReturnValue(new Promise(() => {}));
    await openRoles();
    expect(await screen.findByText("Loading…")).toBeInTheDocument();
  });

  it("says the organization has no roles rather than rendering a blank table", async () => {
    calls.listRoles.mockResolvedValue({ roles: [] });
    await openRoles();
    expect(
      await screen.findByText(/No roles are defined for this organization yet/i),
    ).toBeInTheDocument();
  });

  it("lists each role with how many users hold it and where it comes from", async () => {
    await openRoles();
    // `/i`: the row's name is the cell text, and that cell now reads
    // "Technician" (`roleDisplayName`, same transform the Users tab's chip
    // uses) rather than the raw wire value -- the row lookups below match
    // either case rather than pinning to one, since the point of this test
    // is the counts, not the casing (that has its own test below).
    const technician = await screen.findByRole("row", { name: /technician/i });
    expect(within(technician).getByText("7")).toBeInTheDocument();
    expect(within(technician).getByText(/Platform-wide/i)).toBeInTheDocument();

    const auditor = screen.getByRole("row", { name: /auditor/i });
    expect(within(auditor).getByText("0")).toBeInTheDocument();
    expect(within(auditor).getByText(/This organization/i)).toBeInTheDocument();
  });

  /**
   * NEW test (a role reads three ways gap, fixed). `TECHNICIAN_ROLE` and
   * `AUDITOR_ROLE` are single words, so a regression back to the raw wire
   * value would not show up in the tests above -- "technician" and
   * "Technician" read identically in a case-insensitive row lookup. A
   * hyphenated name is what tells them apart: this pins the Roles tab's own
   * table to `roleDisplayName` ("org-admin" -> "Org admin"), the same
   * transform the Users tab's directory chip already used, so a role
   * genuinely reads the SAME way on both tabs rather than three ways on one.
   */
  it("shows the role's name humanized, the same transform the Users tab chip uses", async () => {
    const orgAdminRole = {
      id: "44444444-4444-4444-8444-444444444444",
      name: "org-admin",
      description: "Runs the organization",
      is_global: false,
      assigned_users: 2,
      codes: [],
    };
    calls.listRoles.mockResolvedValue({ roles: [orgAdminRole] });
    await openRoles();

    expect(await screen.findByText("Org admin")).toBeInTheDocument();
    expect(screen.queryByText("org-admin")).not.toBeInTheDocument();
  });

  /**
   * GENUINELY NEW COVERAGE (language pass, 2026-09-11,
   * `docs/design/settings-language.md` "Roles"). The user count and the
   * permission count are numbers in a table, not identifiers, so they want
   * tabular figures rather than the code font `mono` sets -- the same
   * distinction `.table-foot .count` already draws for a list's own result
   * count. This pins the class, not just the digits, so a regression back to
   * `mono` (which also happens to render the right digits) goes red.
   */
  it("sets the role's counts in tabular figures, not the code font", async () => {
    await openRoles();
    const technician = await screen.findByRole("row", { name: /technician/i });
    const userCount = within(technician).getByText("7");
    expect(userCount).toHaveClass("count");
    expect(userCount).not.toHaveClass("mono");
    // `TECHNICIAN_ROLE` carries one code (`devices.read`).
    const codeCount = within(technician).getByText("1");
    expect(codeCount).toHaveClass("count");
    expect(codeCount).not.toHaveClass("mono");
  });

  it("builds the matrix from the server's catalogue, grouped by module", async () => {
    const user = await openRoles();
    await user.click(await screen.findByRole("button", { name: "Edit permissions for technician" }));

    for (const module of ["devices", "roles", "users", "budget"]) {
      const group = await screen.findByRole("group", { name: module });
      expect(group).toBeInTheDocument();
    }
    const devices = screen.getByRole("group", { name: "devices" });
    expect(within(devices).getByLabelText("devices.read")).toBeChecked();
    expect(within(devices).getByLabelText("devices.manage")).not.toBeChecked();
    // A code the server did not serve is not invented by the console.
    expect(screen.queryByLabelText("patch.approve")).not.toBeInTheDocument();
  });

  it("renders a module it has never heard of — the catalogue is never hardcoded here", async () => {
    calls.listPermissions.mockResolvedValue({
      permissions: [
        {
          code: "quantum.entangle",
          module: "quantum",
          action: "entangle",
          description: "A module this repository does not contain",
        },
      ],
    });
    const user = await openRoles();
    await user.click(await screen.findByRole("button", { name: "Edit permissions for technician" }));
    const group = await screen.findByRole("group", { name: "quantum" });
    expect(within(group).getByLabelText("quantum.entangle")).toBeInTheDocument();
  });

  it("shows how many users the edit affects BEFORE anything is applied", async () => {
    const user = await openRoles();
    await user.click(await screen.findByRole("button", { name: "Edit permissions for technician" }));
    expect(
      await screen.findByText(/7 users hold technician and will be affected by this change/i),
    ).toBeInTheDocument();
    expect(calls.setRolePermissions).not.toHaveBeenCalled();
  });

  it("round-trips a matrix change: the whole code set is sent, and the server's answer is shown", async () => {
    // The re-read after the write returns what the server STORED. Leaving the
    // stale fixture in place would let the form claim "Saved." over a code set
    // the server never confirmed.
    const stored = { ...TECHNICIAN_ROLE, codes: ["devices.read", "devices.manage"] };
    calls.listRoles
      .mockResolvedValueOnce(ROLES_FIXTURE)
      .mockResolvedValue({ roles: [stored, AUDITOR_ROLE] });
    const user = await openRoles();
    await user.click(await screen.findByRole("button", { name: "Edit permissions for technician" }));
    await user.click(await screen.findByLabelText("devices.manage"));
    await user.click(screen.getByRole("button", { name: "Save permissions" }));

    await waitFor(() => expect(calls.setRolePermissions).toHaveBeenCalledTimes(1));
    expect(calls.setRolePermissions).toHaveBeenCalledWith(TECHNICIAN_ROLE.id, [
      "devices.manage",
      "devices.read",
    ]);
    // Re-read after the write: the screen shows what the server stored.
    await waitFor(() => expect(calls.listRoles).toHaveBeenCalledTimes(2));
    expect(await screen.findByRole("status")).toHaveTextContent(/Saved/);
    expect(await screen.findByLabelText("devices.manage")).toBeChecked();
  });

  it("says out loud that editing a seeded role took a copy for this organization", async () => {
    const copyId = "99999999-9999-4999-8999-999999999999";
    calls.setRolePermissions.mockResolvedValue({
      role_id: copyId,
      codes: ["devices.read", "devices.manage"],
      affected_users: 7,
    });
    // After the clone this organization's list holds the COPY, and no longer
    // the platform-wide row — which is exactly what makes the notice load-bearing.
    calls.listRoles.mockResolvedValueOnce(ROLES_FIXTURE).mockResolvedValue({
      roles: [
        { ...TECHNICIAN_ROLE, id: copyId, is_global: false, codes: ["devices.read", "devices.manage"] },
        AUDITOR_ROLE,
      ],
    });
    const user = await openRoles();
    await user.click(await screen.findByRole("button", { name: "Edit permissions for technician" }));
    await user.click(await screen.findByLabelText("devices.manage"));
    await user.click(screen.getByRole("button", { name: "Save permissions" }));

    expect(
      await screen.findByText(
        /technician was copied into your organization and you are editing the copy/i,
      ),
    ).toBeInTheDocument();
  });

  it("never offers super-admin, even when the server lists it", async () => {
    calls.listRoles.mockResolvedValue({ roles: [...ROLES_FIXTURE.roles, SUPER_ADMIN_ROLE] });
    await openRoles();
    await screen.findByRole("row", { name: /technician/i });
    expect(screen.queryByText("super-admin")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Edit permissions for super-admin" }),
    ).not.toBeInTheDocument();
  });

  it("without roles.manage the controls are DISABLED with the reason, never absent", async () => {
    installDefaults(["roles.read"]);
    const user = await openRoles();
    await user.click(await screen.findByRole("button", { name: "Edit permissions for technician" }));

    // Still visible — hiding it teaches people the feature does not exist.
    expect(await screen.findByLabelText("devices.manage")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Save permissions" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Create role" })).toBeDisabled();
    expect(screen.getAllByText(/requires the roles\.manage permission/i).length).toBeGreaterThan(0);
  });

  it("creates a role through the server and re-reads the list", async () => {
    const user = await openRoles();
    await user.type(await screen.findByLabelText("New role name"), "warehouse-technician");
    await user.type(screen.getByLabelText("Description"), "Technician, warehouse only");
    await user.click(screen.getByRole("button", { name: "Create role" }));

    await waitFor(() => expect(calls.createRole).toHaveBeenCalledTimes(1));
    expect(calls.createRole).toHaveBeenCalledWith({
      name: "warehouse-technician",
      description: "Technician, warehouse only",
      codes: [],
    });
  });

  it("deleting a role confirms, and the confirmation names the role", async () => {
    const user = await openRoles();
    await user.click(await screen.findByRole("button", { name: "Delete role auditor" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/Delete the role auditor\?/)).toBeInTheDocument();
    expect(calls.deleteRole).not.toHaveBeenCalled();

    await user.click(within(dialog).getByRole("button", { name: "Delete auditor" }));
    await waitFor(() => expect(calls.deleteRole).toHaveBeenCalledWith(AUDITOR_ROLE.id));
  });

  /**
   * NEW tests (language sweep gap 3, `docs/design/settings-language.md`
   * "Roles"). `RolesTab.tsx` was touched only to move two counts off
   * `.mono` in the earlier pass; its own copy still said "Scope", "Grant
   * them" and "assignments" -- the exact internal nouns the glossary bans
   * from the screen. Wire values and the two column values ("Platform-wide"
   * / "This organization") are unchanged; only the words around them are.
   */
  it("names the decision instead of the column: no bare 'Scope' header", async () => {
    await openRoles();
    // The two values a role can carry here are untouched -- only the column
    // header's own word changes.
    expect(screen.queryByText("Scope")).not.toBeInTheDocument();
    expect(screen.getByText("Belongs to")).toBeInTheDocument();
    const technician = await screen.findByRole("row", { name: /technician/i });
    expect(within(technician).getByText(/Platform-wide/i)).toBeInTheDocument();
  });

  it("the create-role panel does not say 'Grant'", async () => {
    await openRoles();
    expect(screen.queryByText(/\bgrant\b/i)).not.toBeInTheDocument();
    expect(
      screen.getByText("Created with no permissions. Give them access in the matrix above."),
    ).toBeInTheDocument();
  });

  it("the delete-role dialog does not say 'assignments'", async () => {
    const user = await openRoles();
    await user.click(await screen.findByRole("button", { name: "Delete role auditor" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).queryByText(/assignments?/i)).not.toBeInTheDocument();
    expect(
      within(dialog).getByText(/remove their access on the\s+Users tab first/i),
    ).toBeInTheDocument();
  });
});
