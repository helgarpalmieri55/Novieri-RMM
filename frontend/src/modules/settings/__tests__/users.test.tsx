/**
 * Users tab — the identity directory and its lifecycle actions.
 *
 * TWO PROPERTIES CARRY MOST OF THIS FILE.
 *
 * 1. A destructive action always confirms, and the confirmation names WHO.
 *    "Are you sure?" over a table of nine people is not a confirmation.
 * 2. The device-group SCOPE on a role assignment is reachable. The model has
 *    supported it since the RBAC engine was written and no surface has ever
 *    exposed it; "technician on Warehouse only" is the second axis of what a
 *    person may reach, and without it the tab answers only half the question.
 */

import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  AUDITOR_ROLE,
  CASEY_USER,
  OPERATOR_USER,
  PLANTED_SECRET,
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

async function openUsers() {
  const user = userEvent.setup();
  renderSettings();
  await user.click(await screen.findByRole("tab", { name: "Users" }));
  return user;
}

/**
 * Disable, Enable, Reset password, Resend enrolment and Delete live behind a
 * closed `<details>` per row (narrow-viewport fix: six inline buttons made
 * the ACTIONS cell 262px tall at 390px wide). A real person opens it before
 * any of those five is reachable, so a test reaching for one does the same
 * -- reaching straight for the button and relying on jsdom's leniency about
 * closed-`<details>` content would let this regress back to always-visible
 * silently.
 */
async function openMoreActions(user: ReturnType<typeof userEvent.setup>, row: HTMLElement) {
  await user.click(within(row).getByText("More actions"));
}

describe("Settings — Users tab", () => {
  it("says it is loading rather than rendering an empty directory", async () => {
    calls.listUsers.mockReturnValue(new Promise(() => {}));
    await openUsers();
    expect(await screen.findByText("Loading…")).toBeInTheDocument();
  });

  it("says the directory is empty rather than rendering a blank table", async () => {
    calls.listUsers.mockResolvedValue({ users: [] });
    await openUsers();
    expect(await screen.findByText(/No users in this organization yet/i)).toBeInTheDocument();
  });

  it("lists email, status and roles", async () => {
    await openUsers();
    const row = await screen.findByRole("row", { name: /operator@example\.com/ });
    expect(within(row).getByText("active")).toBeInTheDocument();
    // Re-pointed: the directory now shows the role as a chip, "Technician"
    // (`docs/design/settings-language.md` "Users" -- "org-admin" -> "Org
    // admin"), not the raw wire value "technician".
    expect(within(row).getByText(/technician/i)).toBeInTheDocument();

    const casey = screen.getByRole("row", { name: /casey@example\.com/ });
    expect(within(casey).getByText("disabled")).toBeInTheDocument();
    expect(within(casey).getByText(/No roles/i)).toBeInTheDocument();
  });

  it("never renders a credential the server should not have sent (invariant 3)", async () => {
    await openUsers();
    await screen.findByRole("row", { name: /casey@example\.com/ });
    expect(document.body.textContent).not.toContain(PLANTED_SECRET);
  });

  it("creates a user with roles and reports what became of the enrolment email", async () => {
    const user = await openUsers();
    await user.type(await screen.findByLabelText("Email"), "newbie@example.com");
    await user.type(screen.getByLabelText("Display name"), "New Bie");
    // Re-pointed: the fieldset legend "Roles to grant" -> "Give them access
    // to" (`docs/design/settings-language.md` "Users", verbatim). Same
    // fieldset, same checkboxes. Re-pointed again: the checkbox label is now
    // humanized ("Auditor", not "auditor") -- `roleDisplayName`, the same
    // transform the directory chip uses.
    await user.click(
      within(screen.getByRole("group", { name: "Give them access to" })).getByLabelText("Auditor"),
    );
    await user.click(screen.getByRole("button", { name: "Create user" }));

    await waitFor(() => expect(calls.createUser).toHaveBeenCalledTimes(1));
    expect(calls.createUser).toHaveBeenCalledWith({
      email: "newbie@example.com",
      display_name: "New Bie",
      role_ids: [AUDITOR_ROLE.id],
    });
    expect(await screen.findByRole("status")).toHaveTextContent(/enrolment email was sent/i);
  });

  it("a failed enrolment email is reported, not swallowed (spec §2.3)", async () => {
    calls.createUser.mockResolvedValue({
      ...CASEY_USER,
      status: "active",
      enrolment_email_sent: false,
      enrolment_email_error: "Keycloak answered 500",
    });
    const user = await openUsers();
    await user.type(await screen.findByLabelText("Email"), "newbie@example.com");
    await user.click(screen.getByRole("button", { name: "Create user" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/enrolment email was not sent/i);
    expect(alert).toHaveTextContent(/Keycloak answered 500/);
    expect(alert).toHaveTextContent(/Resend enrolment/i);
  });

  /**
   * CYCLE 9. The pasted "Device group id" field, help text and all, is a
   * picker now, fed by the same endpoint the Device groups page lists from
   * -- it has served `GET /api/v1/device-groups` since that page shipped,
   * and this field no longer has to paste around the gap.
   */
  it("offers device groups from a picker rather than a pasted id", async () => {
    const user = await openUsers();
    await user.click(await screen.findByRole("button", { name: "Manage roles for operator@example.com" }));

    const panel = await screen.findByRole("region", { name: /Roles for operator@example\.com/ });
    const field = within(panel).getByLabelText(/device group/i);
    expect(field).toHaveAttribute("role", "combobox");
    expect(screen.queryByText(/No endpoint serves device groups yet/i)).not.toBeInTheDocument();

    await user.click(field);
    expect(await within(panel).findByRole("option", { name: /Warehouse/ })).toBeInTheDocument();
  });

  it("assigns a role scoped to one device group", async () => {
    const user = await openUsers();
    await user.click(await screen.findByRole("button", { name: "Manage roles for operator@example.com" }));

    const panel = await screen.findByRole("region", { name: /Roles for operator@example\.com/ });
    // Re-pointed: `docs/design/settings-language.md` "Users" renames the
    // field "Role to grant" -> "Role" verbatim. Same control, same select.
    await user.selectOptions(within(panel).getByLabelText("Role"), AUDITOR_ROLE.id);
    // Chosen, not typed: the picker's value can only be an id it was given.
    await user.click(within(panel).getByLabelText(/device group/i));
    await user.click(await within(panel).findByRole("option", { name: /Warehouse/ }));
    await user.click(within(panel).getByRole("button", { name: "Assign role" }));

    await waitFor(() => expect(calls.assignUserRole).toHaveBeenCalledTimes(1));
    expect(calls.assignUserRole).toHaveBeenCalledWith(OPERATOR_USER.id, {
      role_id: AUDITOR_ROLE.id,
      device_group_id: "44444444-4444-4444-8444-444444444444",
    });
  });

  it("an org-wide grant sends no device group at all", async () => {
    const user = await openUsers();
    await user.click(await screen.findByRole("button", { name: "Manage roles for operator@example.com" }));
    const panel = await screen.findByRole("region", { name: /Roles for operator@example\.com/ });
    // Re-pointed for the same rename as above.
    await user.selectOptions(within(panel).getByLabelText("Role"), AUDITOR_ROLE.id);
    await user.click(within(panel).getByRole("button", { name: "Assign role" }));

    await waitFor(() => expect(calls.assignUserRole).toHaveBeenCalledTimes(1));
    expect(calls.assignUserRole).toHaveBeenCalledWith(OPERATOR_USER.id, {
      role_id: AUDITOR_ROLE.id,
      device_group_id: null,
    });
  });

  it("never offers super-admin as a grantable role", async () => {
    calls.listRoles.mockResolvedValue({ roles: [...ROLES_FIXTURE.roles, SUPER_ADMIN_ROLE] });
    const user = await openUsers();
    await user.click(await screen.findByRole("button", { name: "Manage roles for operator@example.com" }));
    const panel = await screen.findByRole("region", { name: /Roles for operator@example\.com/ });
    expect(
      within(panel).queryByRole("option", { name: "super-admin" }),
    ).not.toBeInTheDocument();
    // Re-pointed: the option's label is humanized too (`roleDisplayName`).
    expect(within(panel).getByRole("option", { name: "Auditor" })).toBeInTheDocument();
  });

  /**
   * NEW test (language-sweep gap: a role name read three ways on this tab --
   * `roleDisplayName`'s own comment names them). The directory chip and the
   * "Roles for …" list already read humanized; the grant `<select>` and the
   * create-user checkboxes were the two surfaces still printing the raw wire
   * value, one click and one scroll away from a chip reading the same role
   * capitalized. Both are pinned here so a future call site that grows its
   * own copy of the transform (instead of importing `roleDisplayName`) goes
   * red.
   */
  it("shows humanized role names in the create-user checkboxes and the grant select, not the raw wire value", async () => {
    const user = await openUsers();

    const checkboxes = screen.getByRole("group", { name: "Give them access to" });
    expect(within(checkboxes).getByText("Auditor")).toBeInTheDocument();
    expect(within(checkboxes).queryByText("auditor")).not.toBeInTheDocument();

    await user.click(await screen.findByRole("button", { name: "Manage roles for operator@example.com" }));
    const panel = await screen.findByRole("region", { name: /Roles for operator@example\.com/ });
    expect(within(panel).getByRole("option", { name: "Auditor" })).toBeInTheDocument();
    expect(within(panel).queryByText("auditor")).not.toBeInTheDocument();
  });

  it("shows effective permissions annotated with the role that granted each and its scope", async () => {
    const user = await openUsers();
    await user.click(
      await screen.findByRole("button", { name: "Effective permissions for operator@example.com" }),
    );
    const panel = await screen.findByRole("region", {
      name: /Effective permissions for operator@example\.com/,
    });
    await waitFor(() =>
      expect(calls.getUserEffectivePermissions).toHaveBeenCalledWith(OPERATOR_USER.id),
    );
    const orgWide = await within(panel).findByRole("row", { name: /devices\.read/ });
    // Re-pointed: "Granted by" now reads the humanized role name, same
    // transform as the directory chip (`roleDisplayName`, `./types.ts`) --
    // it used to stop at the chip and leave this column on the raw wire
    // value "technician", which is the language-sweep gap this pins shut.
    expect(within(orgWide).getByText("Technician")).toBeInTheDocument();
    // Re-pointed: `docs/design/settings-language.md` glossary renders an
    // org-wide grant as "everywhere", never "organization-wide".
    expect(within(orgWide).getByText(/everywhere/i)).toBeInTheDocument();

    const scoped = within(panel).getByRole("row", { name: /devices\.manage/ });
    // Re-pointed for the same reason: "warehouse-technician" -> "Warehouse
    // technician".
    expect(within(scoped).getByText("Warehouse technician")).toBeInTheDocument();
    // CYCLE 9. The scope now names the GROUP, resolved from the same list
    // the picker offers -- the raw id was the finding, not the fix.
    expect(within(scoped).getByText(/limited to Warehouse/i)).toBeInTheDocument();
    expect(within(scoped).queryByText(/44444444-4444-4444-8444-444444444444/)).not.toBeInTheDocument();
  });

  /**
   * NEW test (language-sweep gap 1). The directory chip humanized the role
   * name ("technician" -> "Technician") but three other surfaces on this
   * SAME tab kept reading the raw wire value: the Roles panel's own list,
   * the effective-permissions "Granted by" column, and the
   * revoke-confirmation dialog. That is worse than reading raw everywhere,
   * because the operator can no longer tell whether two different-looking
   * strings name the same role or two different ones. This pins all four
   * surfaces to the one word, for the one role, in one render -- so a
   * regression in any of them (including a future call site that grows its
   * own copy of the transform instead of importing `roleDisplayName`) goes
   * red here.
   */
  it("a role name reads the same way in the directory chip, the roles panel, Granted by, and the revoke dialog", async () => {
    const user = await openUsers();
    const row = await screen.findByRole("row", { name: /operator@example\.com/ });

    // 1. Directory chip.
    expect(within(row).getByRole("listitem")).toHaveTextContent("Technician · everywhere");

    // 2. Roles panel. Re-pointed: the em dash this test used to pin
    // ("Technician — everywhere") was itself the surviving instance of the
    // exact construction the rewrite existed to kill -- same tab, one click
    // from the chip that already read "Technician · everywhere". The `·`
    // suffix is what this now asserts, same as the chip above.
    await user.click(within(row).getByRole("button", { name: "Manage roles for operator@example.com" }));
    const rolesPanel = await screen.findByRole("region", { name: /Roles for operator@example\.com/ });
    expect(within(rolesPanel).getByRole("listitem")).toHaveTextContent("Technician · everywhere");
    expect(within(rolesPanel).queryByText(/—/)).not.toBeInTheDocument();

    // 3. Effective permissions "Granted by".
    await user.click(
      within(row).getByRole("button", { name: "Effective permissions for operator@example.com" }),
    );
    const permsPanel = await screen.findByRole("region", {
      name: /Effective permissions for operator@example\.com/,
    });
    await within(permsPanel).findByRole("row", { name: /devices\.read/ });
    expect(within(permsPanel).getByText("Technician")).toBeInTheDocument();

    // 4. Revoke-confirmation dialog.
    await user.click(
      within(rolesPanel).getByRole("button", {
        name: "Remove Technician (everywhere) from operator@example.com",
      }),
    );
    const dialog = await screen.findByRole("dialog");
    expect(
      within(dialog).getByText(/Remove Technician from operator@example\.com\?/),
    ).toBeInTheDocument();
  });

  it.each([
    ["Disable operator@example.com", "Disable", () => calls.setUserEnabled],
    ["Reset password for operator@example.com", "Send reset email", () => calls.resetUserPassword],
    ["Delete operator@example.com", "Delete", () => calls.deleteUser],
  ])("%s confirms first, and the confirmation names the person", async (trigger, verb, mock) => {
    const user = await openUsers();
    const row = await screen.findByRole("row", { name: /operator@example\.com/ });
    await openMoreActions(user, row);
    await user.click(within(row).getByRole("button", { name: trigger }));
    const dialog = await screen.findByRole("dialog");
    // The PROMPT names them, not only the confirm button. The trailing "?"
    // is what pins this to the sentence: without it the assertion passes on
    // a dialog reading "Disable this user?" over a button that happens to
    // carry the address, which is not the same screen at all.
    expect(
      within(dialog).getByText(/operator@example\.com\?/),
      "the confirmation sentence must name the person",
    ).toBeInTheDocument();
    expect(dialog).toHaveTextContent("operator@example.com");
    expect(mock()).not.toHaveBeenCalled();

    await user.click(within(dialog).getByRole("button", { name: `${verb} operator@example.com` }));
    await waitFor(() => expect(mock()).toHaveBeenCalled());
  });

  it("revoking a role confirms, and names both the role and the person", async () => {
    const user = await openUsers();
    await user.click(await screen.findByRole("button", { name: "Manage roles for operator@example.com" }));
    const panel = await screen.findByRole("region", { name: /Roles for operator@example\.com/ });
    await user.click(
      within(panel).getByRole("button", {
        // The name carries the SCOPE now: three grants of one role are three
        // different rows, and a control that cannot be told from its
        // neighbour is not a usable control. Re-pointed: `describeGrantScope`
        // now reads "everywhere", never "organization-wide"
        // (`docs/design/settings-language.md` glossary). Re-pointed again:
        // the role name in this accessible name is now humanized
        // ("Technician", not "technician") -- the same transform the
        // visible list text beside it uses, so a screen reader and a
        // sighted operator hear/read the same word for the same role.
        name: "Remove Technician (everywhere) from operator@example.com",
      }),
    );

    const dialog = await screen.findByRole("dialog");
    // Re-pointed: the revoke-confirmation dialog also reads the humanized
    // name now (gap 1 of the language sweep -- it used to be the third
    // place, after the Roles panel and "Granted by", that still showed the
    // raw wire value "technician").
    expect(
      within(dialog).getByText(/Remove Technician from operator@example\.com\?/),
    ).toBeInTheDocument();
    expect(calls.removeUserRole).not.toHaveBeenCalled();

    await user.click(
      within(dialog).getByRole("button", { name: "Remove role from operator@example.com" }),
    );
    // Addressed to the ASSIGNMENT, not the role: the same role may be held
    // twice at different device-group scopes.
    await waitFor(() =>
      expect(calls.removeUserRole).toHaveBeenCalledWith(
        OPERATOR_USER.id,
        OPERATOR_USER.roles[0]?.assignment_id,
      ),
    );
  });

  it("disable calls the server with enabled=false and re-reads the directory", async () => {
    const user = await openUsers();
    const row = await screen.findByRole("row", { name: /operator@example\.com/ });
    await openMoreActions(user, row);
    await user.click(within(row).getByRole("button", { name: "Disable operator@example.com" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Disable operator@example.com" }));
    await waitFor(() => expect(calls.setUserEnabled).toHaveBeenCalledWith(OPERATOR_USER.id, false));
    await waitFor(() => expect(calls.listUsers).toHaveBeenCalledTimes(2));
  });

  it("resending enrolment is not destructive and needs no confirmation", async () => {
    const user = await openUsers();
    const row = await screen.findByRole("row", { name: /casey@example\.com/ });
    await openMoreActions(user, row);
    await user.click(within(row).getByRole("button", { name: "Resend enrolment to casey@example.com" }));
    await waitFor(() => expect(calls.resendUserEnrolment).toHaveBeenCalledWith(CASEY_USER.id));
  });

  /**
   * NEW test (design review, 390x844: row height 262px, ACTIONS squeezed to
   * 169px, six buttons stacked about five deep, the column itself off the
   * edge of a 360px scroller -- see this cell's own comment in
   * `UsersTab.tsx`). Roles and Permissions stay directly in the cell; the
   * other four sit behind a native `<details>`, closed by default, so most
   * of what used to always render no longer has to. Not an invented
   * overflow menu -- there is no menu or popover component in this kit --
   * a `<details>` needs no positioning and is keyboard-operable for free.
   */
  it("keeps Disable, Reset password, Resend enrolment and Delete behind a closed disclosure per row, not always inline", async () => {
    const user = await openUsers();
    const row = await screen.findByRole("row", { name: /operator@example\.com/ });

    // Roles and Permissions need no disclosure -- what most rows are worked
    // on for stays one click away, same as before.
    expect(
      within(row).getByRole("button", { name: "Manage roles for operator@example.com" }),
    ).toBeInTheDocument();
    expect(
      within(row).getByRole("button", { name: "Effective permissions for operator@example.com" }),
    ).toBeInTheDocument();

    const summary = within(row).getByText("More actions");
    const disclosure = summary.closest("details");
    expect(disclosure, "the quieter actions live in a native <details>").not.toBeNull();
    expect(disclosure).not.toHaveAttribute("open");

    await openMoreActions(user, row);
    expect(disclosure).toHaveAttribute("open");
    for (const name of [
      "Disable operator@example.com",
      "Reset password for operator@example.com",
      "Resend enrolment to operator@example.com",
      "Delete operator@example.com",
    ]) {
      expect(within(row).getByRole("button", { name }), name).toBeInTheDocument();
    }
  });

  /**
   * NEW test (spec: "Six equal buttons per row read as a toolbar with no
   * primary"). Roles and Permissions stay the row's two ordinary bordered
   * buttons; Disable, Reset password and Resend enrolment step down to the
   * console's existing quiet `ghost` look; Delete goes further still --
   * danger-tinted text, no border at all -- so it reads as set apart rather
   * than a fourth button shouting in the same row. Pinned on class names
   * because that IS the hierarchy: `Button`'s `variant` is what changes the
   * weight, and a regression here is a regression back to six identical
   * buttons even if every aria-label stays correct.
   */
  it("gives the action row a hierarchy instead of six equal buttons", async () => {
    const user = await openUsers();
    const row = await screen.findByRole("row", { name: /operator@example\.com/ });
    await openMoreActions(user, row);

    const ordinary = [
      within(row).getByRole("button", { name: "Manage roles for operator@example.com" }),
      within(row).getByRole("button", { name: "Effective permissions for operator@example.com" }),
    ];
    const quiet = [
      within(row).getByRole("button", { name: "Disable operator@example.com" }),
      within(row).getByRole("button", { name: "Reset password for operator@example.com" }),
      within(row).getByRole("button", { name: "Resend enrolment to operator@example.com" }),
    ];
    const deleteBtn = within(row).getByRole("button", { name: "Delete operator@example.com" });

    for (const btn of ordinary) {
      expect(btn.className.split(/\s+/), "Roles/Permissions stay ordinary buttons").not.toContain(
        "ghost",
      );
    }
    for (const btn of quiet) {
      expect(btn.className.split(/\s+/), "the three secondary actions go quiet").toContain(
        "ghost",
      );
      expect(btn.className.split(/\s+/)).not.toContain("danger");
    }
    expect(deleteBtn.className.split(/\s+/), "Delete is borderless").toContain("ghost");
    expect(deleteBtn.className.split(/\s+/), "Delete keeps the critical ink").toContain("danger");

    // QUIETER MUST NOT MEAN EASIER TO HIT: Delete still opens the same named,
    // confirming dialog, unchanged by the styling pass.
    await user.click(deleteBtn);
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/operator@example\.com\?/)).toBeInTheDocument();
  });

  it("without users.manage every write control is DISABLED with the reason, never absent", async () => {
    installDefaults(["users.read", "roles.read"]);
    const user = await openUsers();
    const row = await screen.findByRole("row", { name: /operator@example\.com/ });
    // A disabled control is still worth opening the disclosure for -- it
    // stays visible so people learn the feature exists, per the sentence
    // this test itself pins below.
    await openMoreActions(user, row);

    expect(screen.getByRole("button", { name: "Create user" }), "Create user").toBeDisabled();
    for (const name of [
      "Disable operator@example.com",
      "Delete operator@example.com",
      "Reset password for operator@example.com",
    ]) {
      expect(within(row).getByRole("button", { name }), name).toBeDisabled();
    }
    expect(screen.getAllByText(/requires the users\.manage permission/i).length).toBeGreaterThan(0);
    // Reading is still reading: the directory itself renders.
    expect(screen.getByRole("row", { name: /casey@example\.com/ })).toBeInTheDocument();
  });
});

/**
 * DUPLICATE GRANTS — the console's half of the `user_roles` defect.
 *
 * The database accepted the same org-wide grant without limit until migration
 * 0073 (a unique constraint containing a nullable column enforces nothing when
 * that column is NULL), and one deployment held `super-admin` three times. The
 * index stops new ones; these tests are about what the console does with the
 * rows a deployment ALREADY holds, and with the case the index deliberately
 * still allows — one role held both org-wide and on a group.
 */
describe("Settings — Users tab, duplicate role grants", () => {
  const DUPLICATED_USER = {
    id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
    email: "triple@example.com",
    display_name: "Triple Grant",
    status: "active",
    roles: [
      {
        assignment_id: "d1111111-1111-4111-8111-111111111111",
        role_id: TECHNICIAN_ROLE.id,
        role_name: "technician",
        device_group_id: null,
      },
      {
        assignment_id: "d2222222-2222-4222-8222-222222222222",
        role_id: TECHNICIAN_ROLE.id,
        role_name: "technician",
        device_group_id: null,
      },
      {
        assignment_id: "d3333333-3333-4333-8333-333333333333",
        role_id: TECHNICIAN_ROLE.id,
        role_name: "technician",
        device_group_id: null,
      },
      {
        assignment_id: "d4444444-4444-4444-8444-444444444444",
        role_id: TECHNICIAN_ROLE.id,
        role_name: "technician",
        device_group_id: "44444444-4444-4444-8444-444444444444",
      },
    ],
  };

  beforeEach(() => {
    calls.listUsers.mockResolvedValue({ users: [DUPLICATED_USER] });
  });

  it("the directory draws ONE chip for three identical grants, and says there are three", async () => {
    // Re-pointed: `docs/design/settings-language.md` "Users" replaces
    // `technician — organization-wide (3 identical grants)` with a chip,
    // "Technician · everywhere", collapsing the repeat count to "×3" rather
    // than the parenthetical. This is the same collapsing behaviour under
    // new words and a chip instead of a line — `summariseRoleGrants` is
    // untouched, so this genuinely re-points the assertion rather than
    // weakening it.
    await openUsers();
    const row = await screen.findByRole("row", { name: /triple@example\.com/ });

    const lines = within(row).getAllByRole("listitem");
    expect(lines, "three identical rows must not become three chips").toHaveLength(2);
    expect(lines[0]).toHaveTextContent("Technician · everywhere ×3");
  });

  it("the same role at a different scope is a separate chip, never folded into the count", async () => {
    await openUsers();
    const row = await screen.findByRole("row", { name: /triple@example\.com/ });

    const lines = within(row).getAllByRole("listitem");
    // CYCLE 9. The scope names the GROUP now, resolved from the same list
    // the picker offers -- the raw id was the finding, not the fix.
    expect(lines[1]).toHaveTextContent("Technician · limited to Warehouse");
    expect(lines[1]).not.toHaveTextContent("×");
  });

  it("a role held once reads plainly, with no count at all", async () => {
    calls.listUsers.mockResolvedValue({ users: [OPERATOR_USER] });
    await openUsers();
    const row = await screen.findByRole("row", { name: /operator@example\.com/ });

    expect(within(row).getByRole("listitem")).toHaveTextContent("Technician · everywhere");
    expect(within(row).getByRole("listitem")).not.toHaveTextContent("×");
  });

  it("the roles panel keeps every grant revocable and labels the duplicates", async () => {
    const user = await openUsers();
    await user.click(await screen.findByRole("button", { name: "Manage roles for triple@example.com" }));
    const panel = await screen.findByRole("region", { name: /Roles for triple@example\.com/ });

    // COLLAPSING HERE WOULD STRAND THE OPERATOR: a revoke is addressed to one
    // assignment_id, and this panel is the only surface that can clear the
    // extra rows.
    // Re-pointed: the accessible name now carries the humanized role name
    // ("Remove Technician …", not "Remove technician …").
    expect(within(panel).getAllByRole("button", { name: /^Remove Technician/ })).toHaveLength(4);
    expect(within(panel).getByText(/duplicate 1 of 3/)).toBeInTheDocument();
    expect(within(panel).getByText(/duplicate 3 of 3/)).toBeInTheDocument();
  });

  it("each duplicate's remove button has a name that tells it from its neighbours", async () => {
    const user = await openUsers();
    await user.click(await screen.findByRole("button", { name: "Manage roles for triple@example.com" }));
    const panel = await screen.findByRole("region", { name: /Roles for triple@example\.com/ });

    const names = within(panel)
      .getAllByRole("button", { name: /^Remove Technician/ })
      .map((button) => button.getAttribute("aria-label"));
    expect(new Set(names).size, "identical accessible names are indistinguishable controls").toBe(
      4,
    );
    // Re-pointed: `describeGrantScope` reads "limited to <group>", never
    // "device group <group>" (`docs/design/settings-language.md` glossary),
    // and the role name is now humanized ("Technician", not "technician") --
    // the same word the visible list text and the directory chip use for
    // this role. Re-pointed again (cycle 9): the group is named, not its id.
    expect(names[3]).toBe(
      "Remove Technician (limited to Warehouse) from triple@example.com",
    );
  });

  it("revoking one duplicate is addressed to THAT row", async () => {
    const user = await openUsers();
    await user.click(await screen.findByRole("button", { name: "Manage roles for triple@example.com" }));
    const panel = await screen.findByRole("region", { name: /Roles for triple@example\.com/ });
    await user.click(
      within(panel).getByRole("button", {
        // Re-pointed for the same glossary rename as above, plus the
        // humanized role name.
        name: "Remove Technician (everywhere, duplicate 2 of 3) from triple@example.com",
      }),
    );

    const dialog = await screen.findByRole("dialog");
    await user.click(
      within(dialog).getByRole("button", { name: "Remove role from triple@example.com" }),
    );
    await waitFor(() =>
      expect(calls.removeUserRole).toHaveBeenCalledWith(
        DUPLICATED_USER.id,
        "d2222222-2222-4222-8222-222222222222",
      ),
    );
  });
});
