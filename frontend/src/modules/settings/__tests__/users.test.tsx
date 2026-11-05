/**
 * Users tab — the identity directory and its lifecycle actions.
 *
 * TWO PROPERTIES CARRY MOST OF THIS FILE.
 *
 * 1. A destructive action always confirms, and the confirmation names WHO.
 *    "Are you sure?" over a table of nine people is not a confirmation.
 * 2. The SCOPE on a role assignment is reachable: "technician on Warehouse
 *    only" is the second axis of what a person may reach, and without it the
 *    tab answers only half the question. It was a device group until the MSP
 *    clients model (spec §2.1); it is a CLIENT now, or every client.
 */

import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../../lib/api";
import {
  AUDITOR_ROLE,
  CASEY_USER,
  CLIENTS_FIXTURE,
  DANA_USER,
  INVITED_USER,
  OPERATOR_USER,
  PLANTED_SECRET,
  ROLES_FIXTURE,
  SUPER_ADMIN_ROLE,
  TECHNICIAN_ROLE,
  WAREHOUSE_CLIENT,
  apiClient,
  calls,
  installDefaults,
  mutationCacheText,
  renderSettings,
  stepUpRequired,
} from "./harness";

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
 * The lifecycle actions -- Disable, Reset second factors and Sign out
 * everywhere for an account in use, Enable for a disabled one, Resend and
 * Cancel for an invitation (own authentication design §7) -- and Delete live
 * behind a closed `<details>` per row (narrow-viewport fix: six inline buttons
 * made the ACTIONS cell 262px tall at 390px wide). A real person opens it
 * before any of them is reachable, so a test reaching for one does the same
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

  /**
   * RE-POINTED (own authentication design §7) from "creates a user with roles
   * and reports what became of the enrolment email": an account starts as an
   * INVITATION, carrying what the person may reach on which client -- the
   * roles panel's (role, client) pairs -- and no password anywhere in the form.
   */
  it("invites a person with a role on one client and a role on every client", async () => {
    const user = await openUsers();
    const invite = await screen.findByRole("region", { name: "Invite a person" });
    await user.type(within(invite).getByLabelText("Email"), "newbie@example.com");
    await user.type(within(invite).getByLabelText("Display name"), "New Bie");

    await user.selectOptions(await within(invite).findByLabelText("Role"), AUDITOR_ROLE.id);
    await user.click(within(invite).getByLabelText(/client/i));
    await user.click(await within(invite).findByRole("option", { name: /Warehouse/ }));
    await user.click(within(invite).getByRole("button", { name: "Add this access" }));

    await user.selectOptions(within(invite).getByLabelText("Role"), TECHNICIAN_ROLE.id);
    await user.click(within(invite).getByLabelText(/client/i));
    await user.click(await within(invite).findByRole("option", { name: /Every client/ }));
    await user.click(within(invite).getByRole("button", { name: "Add this access" }));

    const chosen = within(invite).getAllByRole("listitem");
    expect(chosen).toHaveLength(2);
    expect(chosen[0]).toHaveTextContent("Auditor · limited to Warehouse");
    expect(chosen[1]).toHaveTextContent("Technician · everywhere");
    expect(within(invite).queryByLabelText(/password/i)).not.toBeInTheDocument();

    await user.click(within(invite).getByRole("button", { name: "Send invitation" }));
    await waitFor(() => expect(calls.inviteUser).toHaveBeenCalledTimes(1));
    expect(calls.inviteUser).toHaveBeenCalledWith({
      email: "newbie@example.com",
      display_name: "New Bie",
      grants: [
        { role_id: AUDITOR_ROLE.id, client_id: WAREHOUSE_CLIENT.id },
        { role_id: TECHNICIAN_ROLE.id, client_id: null },
      ],
    });
    expect(await screen.findByRole("status")).toHaveTextContent("An invitation was emailed to newbie@example.com.");
    expect(within(invite).getByLabelText<HTMLInputElement>("Email")).toHaveValue("");
  });

  /**
   * RE-POINTED from "a failed enrolment email is reported, not swallowed":
   * without mail an invitation is no longer an account nobody can reach -- the
   * server answers with the link, ONCE, and the tab shows it to pass on. It is
   * held in the component's state only, and gone when the operator says so.
   */
  it("shows the link once when no mail is configured, and forgets it when told", async () => {
    calls.inviteUser.mockResolvedValue({
      user_id: INVITED_USER.id,
      invite_link: "https://itms.example.test/invite#one-time-token",
    });
    const user = await openUsers();
    const invite = await screen.findByRole("region", { name: "Invite a person" });
    await user.type(within(invite).getByLabelText("Email"), "newbie@example.com");
    await user.click(within(invite).getByRole("button", { name: "Send invitation" }));

    const shown = await screen.findByRole("region", { name: "Invitation link for newbie@example.com" });
    expect(within(shown).getByLabelText<HTMLInputElement>("Invitation link")).toHaveValue(
      "https://itms.example.test/invite#one-time-token",
    );
    expect(within(shown).getByText(/It is shown only now/)).toBeInTheDocument();

    await user.click(within(shown).getByRole("button", { name: "Done, I have passed it on" }));
    expect(screen.queryByRole("region", { name: /Invitation link/ })).not.toBeInTheDocument();
    expect(document.body.innerHTML).not.toContain("one-time-token");
  });

  it("keeps no invitation link in the mutation cache once it is passed on", async () => {
    // FIX ROUND (Task 12 review, Important 1): `reset()` only detaches the
    // mutation; TanStack keeps a detached one, link and all, for its `gcTime`.
    calls.inviteUser.mockResolvedValue({
      user_id: INVITED_USER.id,
      invite_link: "https://itms.example.test/invite#one-time-token",
    });
    const user = userEvent.setup();
    const { queryClient } = renderSettings("/settings?tab=users");
    const invite = await screen.findByRole("region", { name: "Invite a person" });
    await user.type(within(invite).getByLabelText("Email"), "newbie@example.com");
    await user.click(within(invite).getByRole("button", { name: "Send invitation" }));
    const shown = await screen.findByRole("region", { name: "Invitation link for newbie@example.com" });
    await user.click(within(shown).getByRole("button", { name: "Done, I have passed it on" }));

    await waitFor(() => expect(mutationCacheText(queryClient)).not.toContain("one-time-token"));
  });

  it("keeps no resent invitation link in the mutation cache once it is passed on", async () => {
    calls.listUsers.mockResolvedValue({ users: [OPERATOR_USER, INVITED_USER] });
    calls.resendInvite.mockResolvedValue({ invite_link: "https://itms.example.test/invite#fresh-token" });
    const user = userEvent.setup();
    const { queryClient } = renderSettings("/settings?tab=users");
    const row = await screen.findByRole("row", { name: /invitee@example\.com/ });
    await openMoreActions(user, row);
    await user.click(within(row).getByRole("button", { name: "Resend the invitation to invitee@example.com" }));
    const shown = await screen.findByRole("region", { name: "Invitation link for invitee@example.com" });
    await user.click(within(shown).getByRole("button", { name: "Done, I have passed it on" }));

    await waitFor(() => expect(mutationCacheText(queryClient)).not.toContain("fresh-token"));
  });

  it("does not send an access chosen and not added", async () => {
    const user = await openUsers();
    const invite = await screen.findByRole("region", { name: "Invite a person" });
    await user.type(within(invite).getByLabelText("Email"), "newbie@example.com");
    await user.selectOptions(await within(invite).findByLabelText("Role"), AUDITOR_ROLE.id);

    expect(within(invite).getByRole("button", { name: "Add this access" })).toBeDisabled();
    expect(within(invite).getByRole("button", { name: "Send invitation" })).toBeDisabled();
    expect(within(invite).getByText("Add the access you chose, or clear it, before sending.")).toBeInTheDocument();
    expect(calls.inviteUser).not.toHaveBeenCalled();
  });

  /**
   * CYCLE 9. The pasted "Device group id" field, help text and all, became a
   * picker fed by a list endpoint rather than a pasted id.
   *
   * RE-POINTED (MSP clients, phase 1): a grant is scoped to ONE CLIENT or to
   * every client (`user_roles.client_id`, spec §2.1), and device groups no
   * longer scope grants -- so the picker lists `GET /api/v1/clients`, and the
   * device-group field is gone rather than kept beside it, since a group id
   * sent as a scope is a 422.
   */
  it("offers clients from a picker rather than a pasted id", async () => {
    const user = await openUsers();
    await user.click(await screen.findByRole("button", { name: "Manage roles for operator@example.com" }));

    const panel = await screen.findByRole("region", { name: /Roles for operator@example\.com/ });
    const field = within(panel).getByLabelText(/client/i);
    expect(field).toHaveAttribute("role", "combobox");
    expect(within(panel).queryByLabelText(/device group/i)).not.toBeInTheDocument();

    await user.click(field);
    expect(await within(panel).findByRole("option", { name: /Warehouse/ })).toBeInTheDocument();
    expect(within(panel).getByRole("option", { name: /Toeshee Group/ })).toBeInTheDocument();
    // The unassigned client takes no grant (the server answers 422), so it is
    // not offered.
    expect(within(panel).queryByRole("option", { name: /Unassigned/ })).not.toBeInTheDocument();
  });

  it("assigns a role scoped to one client", async () => {
    const user = await openUsers();
    await user.click(await screen.findByRole("button", { name: "Manage roles for operator@example.com" }));

    const panel = await screen.findByRole("region", { name: /Roles for operator@example\.com/ });
    // Re-pointed: `docs/design/settings-language.md` "Users" renames the
    // field "Role to grant" -> "Role" verbatim. Same control, same select.
    await user.selectOptions(within(panel).getByLabelText("Role"), AUDITOR_ROLE.id);
    // Chosen, not typed: the picker's value can only be an id it was given.
    await user.click(within(panel).getByLabelText(/client/i));
    await user.click(await within(panel).findByRole("option", { name: /Warehouse/ }));
    // RE-POINTED (final review, T11R-I1): the button names the scope it
    // grants, so the click that sends a grant says where it applies.
    await user.click(within(panel).getByRole("button", { name: "Assign role on Warehouse" }));

    await waitFor(() => expect(calls.assignUserRole).toHaveBeenCalledTimes(1));
    // RE-POINTED: `client_id`, the key the server's `RoleAssign` takes now.
    expect(calls.assignUserRole).toHaveBeenCalledWith(OPERATOR_USER.id, {
      role_id: AUDITOR_ROLE.id,
      client_id: "44444444-4444-4444-8444-444444444444",
    });
  });

  it("a grant on every client sends no client at all", async () => {
    const user = await openUsers();
    await user.click(await screen.findByRole("button", { name: "Manage roles for operator@example.com" }));
    const panel = await screen.findByRole("region", { name: /Roles for operator@example\.com/ });
    // Re-pointed for the same rename as above.
    await user.selectOptions(within(panel).getByLabelText("Role"), AUDITOR_ROLE.id);
    // RE-POINTED (final review, T11R-I1): every client is a CHOICE, made in
    // the picker like any client -- never what an empty box means.
    await user.click(within(panel).getByLabelText(/client/i));
    await user.click(await within(panel).findByRole("option", { name: /Every client/ }));
    await user.click(within(panel).getByRole("button", { name: "Assign role on every client" }));

    await waitFor(() => expect(calls.assignUserRole).toHaveBeenCalledTimes(1));
    // RE-POINTED: `null` is every client, including clients created later.
    expect(calls.assignUserRole).toHaveBeenCalledWith(OPERATOR_USER.id, {
      role_id: AUDITOR_ROLE.id,
      client_id: null,
    });
  });

  /**
   * FINAL REVIEW, T11R-I1. The scope used to be "whatever the box holds, and
   * an empty box is every client". A person who typed "Ware" and did not
   * click the option saw "Ware" in the box and granted EVERY client. Now:
   * the scope is a committed choice -- a client, or "Every client" -- the
   * button says which, and Assign is disabled while the box holds text that
   * is not a choice.
   */
  it("assigns nothing until a scope is chosen", async () => {
    const user = await openUsers();
    await user.click(await screen.findByRole("button", { name: "Manage roles for operator@example.com" }));
    const panel = await screen.findByRole("region", { name: /Roles for operator@example\.com/ });
    await user.selectOptions(within(panel).getByLabelText("Role"), AUDITOR_ROLE.id);
    expect(within(panel).getByRole("button", { name: "Assign role" })).toBeDisabled();
  });

  it("typed text that is not a chosen client cannot assign", async () => {
    const user = await openUsers();
    await user.click(await screen.findByRole("button", { name: "Manage roles for operator@example.com" }));
    const panel = await screen.findByRole("region", { name: /Roles for operator@example\.com/ });
    await user.selectOptions(within(panel).getByLabelText("Role"), AUDITOR_ROLE.id);
    const field = within(panel).getByLabelText(/client/i);

    // Typed, never clicked: nothing is chosen, so nothing can be assigned.
    await user.type(field, "Ware");
    const assign = within(panel).getByRole("button", { name: /^Assign role/ });
    expect(assign).toBeDisabled();
    await user.click(assign);
    expect(calls.assignUserRole).not.toHaveBeenCalled();

    // A client chosen, then other text typed over it: still not a choice.
    // (Back to the picker first: the click above left it.)
    await user.click(field);
    await user.click(await within(panel).findByRole("option", { name: /Warehouse/ }));
    expect(within(panel).getByRole("button", { name: "Assign role on Warehouse" })).toBeEnabled();
    await user.clear(field);
    await user.type(field, "Toe");
    expect(within(panel).getByRole("button", { name: /^Assign role/ })).toBeDisabled();
    await user.click(await within(panel).findByRole("option", { name: /Toeshee Group/ }));
    await user.click(within(panel).getByRole("button", { name: "Assign role on Toeshee Group" }));

    await waitFor(() => expect(calls.assignUserRole).toHaveBeenCalledTimes(1));
    expect(calls.assignUserRole).toHaveBeenCalledWith(OPERATOR_USER.id, {
      role_id: AUDITOR_ROLE.id,
      client_id: "org-1",
    });
  });

  it("names an archived client's grant, and does not offer the archived client", async () => {
    const archived = { ...WAREHOUSE_CLIENT, id: "77777777-7777-4777-8777-777777777777", name: "Old Co", status: "archived" };
    calls.listClients.mockImplementation((params?: { include_archived?: boolean }) =>
      Promise.resolve(params?.include_archived ? [...CLIENTS_FIXTURE, archived] : CLIENTS_FIXTURE),
    );
    calls.listUsers.mockResolvedValue({
      users: [
        {
          ...OPERATOR_USER,
          roles: [{ assignment_id: "a-old", role_id: TECHNICIAN_ROLE.id, role_name: "technician", client_id: archived.id }],
        },
        CASEY_USER,
      ],
    });
    const user = await openUsers();
    await user.click(await screen.findByRole("button", { name: "Manage roles for operator@example.com" }));
    const panel = await screen.findByRole("region", { name: /Roles for operator@example\.com/ });

    // The name map reads archived clients too: the grant says whose it is.
    expect(await within(panel).findByText("· limited to Old Co")).toBeInTheDocument();
    expect(within(panel).queryByText(new RegExp(archived.id))).not.toBeInTheDocument();
    expect(calls.listClients).toHaveBeenCalledWith({ include_archived: true });
    // ...and the choices still leave it out: an archived client takes no grant.
    await user.click(within(panel).getByLabelText(/client/i));
    expect(await within(panel).findByRole("option", { name: /Warehouse/ })).toBeInTheDocument();
    expect(within(panel).queryByRole("option", { name: /Old Co/ })).not.toBeInTheDocument();
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
   * value. RE-POINTED (own authentication design §7): the create form's
   * checkboxes are the invitation's role select now. Both selects read the
   * humanized name, so a future call site that grows its own copy of the
   * transform (instead of importing `roleDisplayName`) goes red.
   */
  it("shows humanized role names in the invitation's role select and the grant select, not the raw wire value", async () => {
    const user = await openUsers();
    const invite = await screen.findByRole("region", { name: "Invite a person" });
    expect(await within(invite).findByRole("option", { name: "Auditor" })).toBeInTheDocument();
    expect(within(invite).queryByRole("option", { name: "auditor" })).not.toBeInTheDocument();

    await user.click(await screen.findByRole("button", { name: "Manage roles for operator@example.com" }));
    const panel = await screen.findByRole("region", { name: /Roles for operator@example\.com/ });
    expect(within(panel).getByRole("option", { name: "Auditor" })).toBeInTheDocument();
    expect(within(panel).queryByText("auditor")).not.toBeInTheDocument();
  });

  it("shows effective permissions grouped by client, with the role that granted each", async () => {
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
    // RE-POINTED (MSP clients phase 3, design §4.2): the "Applies to" column
    // became the group a grant sits in -- every client first, then each client.
    // "Granted by" still reads the humanized role name (`roleDisplayName`,
    // `./types.ts`), never the raw wire value; and the client is named from
    // `GET /clients`, never by its id.
    const every = await within(panel).findByRole("table", { name: "Every client" });
    const orgWide = within(every).getByRole("row", { name: /devices\.read/ });
    expect(within(orgWide).getByText("Technician")).toBeInTheDocument();
    const warehouse = within(panel).getByRole("table", { name: "Warehouse" });
    const scoped = within(warehouse).getByRole("row", { name: /devices\.manage/ });
    expect(within(scoped).getByText("Warehouse technician")).toBeInTheDocument();
    expect(within(panel).getAllByRole("table").map((table) => table.getAttribute("aria-label"))).toEqual([
      "Every client",
      "Warehouse",
    ]);
    expect(within(panel).queryByText(/44444444-4444-4444-8444-444444444444/)).not.toBeInTheDocument();
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
    ["Disable operator@example.com", "Disable", () => calls.setUserEnabled, "operator@example.com"],
    // RE-POINTED (design §7): the password reset by email is the person's own
    // now (§2.8); an administrator resets the second factors. RE-POINTED
    // again (final review D2, Minor 2): at SOMEONE ELSE -- the caller's own
    // row offers no reset, because the server refuses it.
    ["Reset second factors for dana@example.com", "Reset second factors for", () => calls.resetUserFactors, "dana@example.com"],
    ["Sign out operator@example.com everywhere", "Sign out", () => calls.endUserSessions, "operator@example.com"],
    ["Delete operator@example.com", "Delete", () => calls.deleteUser, "operator@example.com"],
  ])("%s confirms first, and the confirmation names the person", async (trigger, verb, mock, email) => {
    calls.listUsers.mockResolvedValue({ users: [OPERATOR_USER, CASEY_USER, DANA_USER] });
    const user = await openUsers();
    const row = await screen.findByRole("row", { name: new RegExp(email.replace(/\./g, "\\.")) });
    await openMoreActions(user, row);
    await user.click(within(row).getByRole("button", { name: trigger }));
    const dialog = await screen.findByRole("dialog");
    // The PROMPT names them, not only the confirm button. The trailing "?"
    // is what pins this to the sentence: without it the assertion passes on
    // a dialog reading "Disable this user?" over a button that happens to
    // carry the address, which is not the same screen at all.
    expect(
      within(dialog).getByText(new RegExp(`${email.replace(/\./g, "\\.")}\\?`)),
      "the confirmation sentence must name the person",
    ).toBeInTheDocument();
    expect(dialog).toHaveTextContent(email);
    expect(mock()).not.toHaveBeenCalled();

    await user.click(within(dialog).getByRole("button", { name: `${verb} ${email}` }));
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
    // twice at different scopes (every client, and one client).
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

  it("resending an invitation is not destructive and needs no confirmation", async () => {
    // RE-POINTED (design §7): it was the enrolment mail on every row; it is the
    // invitation now, offered on an invitation's row only.
    calls.listUsers.mockResolvedValue({ users: [OPERATOR_USER, INVITED_USER] });
    const user = await openUsers();
    const row = await screen.findByRole("row", { name: /invitee@example\.com/ });
    expect(within(row).getByText("Invitation pending")).toBeInTheDocument();
    await openMoreActions(user, row);
    expect(within(row).queryByRole("button", { name: "Reset second factors for invitee@example.com" })).not.toBeInTheDocument();

    await user.click(within(row).getByRole("button", { name: "Resend the invitation to invitee@example.com" }));
    await waitFor(() => expect(calls.resendInvite).toHaveBeenCalledWith(INVITED_USER.id));
    expect(await screen.findByRole("status")).toHaveTextContent("The invitation was emailed to invitee@example.com again.");
  });

  it("shows a resent invitation's fresh link once when no mail is configured", async () => {
    calls.listUsers.mockResolvedValue({ users: [OPERATOR_USER, INVITED_USER] });
    calls.resendInvite.mockResolvedValue({ invite_link: "https://itms.example.test/invite#fresh-token" });
    const user = await openUsers();
    const row = await screen.findByRole("row", { name: /invitee@example\.com/ });
    await openMoreActions(user, row);
    await user.click(within(row).getByRole("button", { name: "Resend the invitation to invitee@example.com" }));

    const shown = await screen.findByRole("region", { name: "Invitation link for invitee@example.com" });
    expect(within(shown).getByLabelText<HTMLInputElement>("Invitation link")).toHaveValue(
      "https://itms.example.test/invite#fresh-token",
    );
  });

  it("cancelling an invitation confirms, names the person, and goes to its own route", async () => {
    calls.listUsers.mockResolvedValue({ users: [OPERATOR_USER, INVITED_USER] });
    const user = await openUsers();
    const row = await screen.findByRole("row", { name: /invitee@example\.com/ });
    await openMoreActions(user, row);
    await user.click(within(row).getByRole("button", { name: "Cancel the invitation to invitee@example.com" }));

    const dialog = await screen.findByRole("dialog", { name: "Cancel invitation" });
    expect(within(dialog).getByText(/invitee@example\.com\?/)).toBeInTheDocument();
    expect(calls.cancelInvite).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole("button", { name: "Cancel the invitation to invitee@example.com" }));
    await waitFor(() => expect(calls.cancelInvite).toHaveBeenCalledWith(INVITED_USER.id));
  });

  it("says, before cancelling, that the address can never be invited again (C18, review C, Minor 4)", async () => {
    // The server marks the account deleted, and the address stays taken
    // (`ux_users_org_lower_email_staff` covers deleted rows): it said "You
    // can invite them again later".
    calls.listUsers.mockResolvedValue({ users: [OPERATOR_USER, INVITED_USER] });
    const user = await openUsers();
    const row = await screen.findByRole("row", { name: /invitee@example\.com/ });
    await openMoreActions(user, row);
    await user.click(within(row).getByRole("button", { name: "Cancel the invitation to invitee@example.com" }));

    const dialog = await screen.findByRole("dialog", { name: "Cancel invitation" });
    expect(dialog).toHaveTextContent("This address cannot be invited again");
    expect(dialog).not.toHaveTextContent(/invite them again later/);
  });

  /**
   * FIX ROUND (Task 12 review, Minor 4). The server answers 409 for anyone
   * without a pending invitation, with a translated `detail` and NO `code`
   * (plan decision 8): the console says that sentence, once.
   */
  it("says the server's own sentence, once, when a resend is refused with 409", async () => {
    calls.listUsers.mockResolvedValue({ users: [OPERATOR_USER, INVITED_USER] });
    calls.resendInvite.mockRejectedValue(
      new ApiError(409, JSON.stringify({ detail: "this person has no pending invitation" })),
    );
    const user = await openUsers();
    const row = await screen.findByRole("row", { name: /invitee@example\.com/ });
    await openMoreActions(user, row);
    await user.click(within(row).getByRole("button", { name: "Resend the invitation to invitee@example.com" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("this person has no pending invitation");
    expect(screen.getAllByText("this person has no pending invitation")).toHaveLength(1);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("says the server's own sentence, once, inside the dialog when a cancel is refused with 409", async () => {
    calls.listUsers.mockResolvedValue({ users: [OPERATOR_USER, INVITED_USER] });
    calls.cancelInvite.mockRejectedValue(
      new ApiError(409, JSON.stringify({ detail: "this person has no pending invitation" })),
    );
    const user = await openUsers();
    const row = await screen.findByRole("row", { name: /invitee@example\.com/ });
    await openMoreActions(user, row);
    await user.click(within(row).getByRole("button", { name: "Cancel the invitation to invitee@example.com" }));
    const dialog = await screen.findByRole("dialog", { name: "Cancel invitation" });
    await user.click(within(dialog).getByRole("button", { name: "Cancel the invitation to invitee@example.com" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("this person has no pending invitation");
    expect(screen.getAllByText("this person has no pending invitation")).toHaveLength(1);
  });

  // RE-POINTED (final review D2, Minor 2): these two targeted the caller's
  // own row (`ME_FIXTURE.user_id === OPERATOR_USER.id`) and expected the
  // reset to succeed -- an outcome the real server refuses
  // (`service._refuse_self`), after a step-up that may have spent a
  // single-use recovery code. They reset someone else now.
  it("resetting someone's second factors asks for your own when the server says it is stale", async () => {
    calls.listUsers.mockResolvedValue({ users: [OPERATOR_USER, DANA_USER] });
    calls.resetUserFactors.mockRejectedValueOnce(stepUpRequired()).mockResolvedValueOnce(undefined);
    const user = await openUsers();
    const row = await screen.findByRole("row", { name: /dana@example\.com/ });
    await openMoreActions(user, row);
    await user.click(within(row).getByRole("button", { name: "Reset second factors for dana@example.com" }));
    const confirm = await screen.findByRole("dialog", { name: "Reset second factors" });
    await user.click(within(confirm).getByRole("button", { name: "Reset second factors for dana@example.com" }));

    const stepUp = await screen.findByRole("dialog", { name: "Confirm it's you" });
    await user.type(await within(stepUp).findByLabelText("Code from your authenticator app"), "123456");
    await user.click(within(stepUp).getByRole("button", { name: "Confirm" }));

    await waitFor(() => expect(calls.resetUserFactors).toHaveBeenCalledTimes(2));
    expect(calls.resetUserFactors).toHaveBeenLastCalledWith(DANA_USER.id);
    expect(calls.stepUpTotp).toHaveBeenCalledExactlyOnceWith("123456");
    expect(await screen.findByRole("status")).toHaveTextContent("The second factors of dana@example.com were reset.");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("shows the server's own sentence for a reset that needs the recovery command, keeps the dialog open, and does not retry", async () => {
    // Operator decision, 2026-11-05: `reset_factors` answers 409
    // `reset_needs_recovery` when the password was set from an emailed reset
    // link and no second factor has been used since. The server's real body
    // (`{detail, code}`, as C14 asks of every double).
    const sentence =
      "This person's password was set with an emailed reset link, and no second factor has been used since. Resetting their sign-in methods now would hand the account to whoever holds that mailbox. Use the server's recovery command instead, and hand the link to the person directly.";
    calls.listUsers.mockResolvedValue({ users: [OPERATOR_USER, DANA_USER] });
    calls.resetUserFactors.mockRejectedValue(
      new ApiError(409, JSON.stringify({ detail: sentence, code: "reset_needs_recovery" })),
    );
    const user = await openUsers();
    const row = await screen.findByRole("row", { name: /dana@example\.com/ });
    await openMoreActions(user, row);
    await user.click(within(row).getByRole("button", { name: "Reset second factors for dana@example.com" }));
    const confirm = await screen.findByRole("dialog", { name: "Reset second factors" });
    await user.click(within(confirm).getByRole("button", { name: "Reset second factors for dana@example.com" }));

    expect(await within(confirm).findByRole("alert")).toHaveTextContent(sentence);
    expect(confirm).not.toHaveTextContent(/something went wrong|went wrong/i);
    // Still open, nothing more sent, and no second-factor prompt: a 409 is not a stale step-up.
    expect(screen.getByRole("dialog", { name: "Reset second factors" })).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: "Confirm it's you" })).not.toBeInTheDocument();
    expect(calls.resetUserFactors).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("The second factors of dana@example.com were reset.")).not.toBeInTheDocument();

    // The person closes it.
    await user.click(within(confirm).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog", { name: "Reset second factors" })).not.toBeInTheDocument();
    expect(calls.resetUserFactors).toHaveBeenCalledTimes(1);
  });

  it("says, before the reset, that it signs the person out everywhere now (D2, Minor 3)", async () => {
    calls.listUsers.mockResolvedValue({ users: [OPERATOR_USER, DANA_USER] });
    const user = await openUsers();
    const row = await screen.findByRole("row", { name: /dana@example\.com/ });
    await openMoreActions(user, row);
    await user.click(within(row).getByRole("button", { name: "Reset second factors for dana@example.com" }));
    const confirm = await screen.findByRole("dialog", { name: "Reset second factors" });
    expect(confirm).toHaveTextContent(/every session they have open is signed out now/);
    expect(calls.resetUserFactors).not.toHaveBeenCalled();
  });

  it("does not offer to reset your own second factors, and points at Your security instead", async () => {
    calls.listUsers.mockResolvedValue({ users: [OPERATOR_USER, DANA_USER] });
    const user = await openUsers();
    const own = await screen.findByRole("row", { name: /operator@example\.com/ });
    await openMoreActions(user, own);

    const reset = within(own).getByRole("button", { name: "Reset second factors for operator@example.com" });
    expect(reset).toBeDisabled();
    expect(reset).toHaveAccessibleDescription("Your own second factors are changed in Your security.");
    expect(within(own).getByRole("link", { name: "Your security" })).toHaveAttribute("href", "/profile/security");
    await user.click(reset);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(calls.resetUserFactors).not.toHaveBeenCalled();

    // Someone else's row still offers it.
    const other = screen.getByRole("row", { name: /dana@example\.com/ });
    await openMoreActions(user, other);
    expect(within(other).getByRole("button", { name: "Reset second factors for dana@example.com" })).toBeEnabled();
    expect(within(other).queryByRole("link", { name: "Your security" })).not.toBeInTheDocument();
  });

  it("resets nothing when the confirmation is closed, and says so inside the dialog", async () => {
    calls.listUsers.mockResolvedValue({ users: [OPERATOR_USER, DANA_USER] });
    calls.resetUserFactors.mockRejectedValue(stepUpRequired());
    const user = await openUsers();
    const row = await screen.findByRole("row", { name: /dana@example\.com/ });
    await openMoreActions(user, row);
    await user.click(within(row).getByRole("button", { name: "Reset second factors for dana@example.com" }));
    const confirm = await screen.findByRole("dialog", { name: "Reset second factors" });
    await user.click(within(confirm).getByRole("button", { name: "Reset second factors for dana@example.com" }));

    const stepUp = await screen.findByRole("dialog", { name: "Confirm it's you" });
    await user.click(within(stepUp).getByRole("button", { name: "Cancel" }));

    expect(await within(confirm).findByRole("alert")).toHaveTextContent("Nothing was changed: the confirmation was closed.");
    expect(calls.resetUserFactors).toHaveBeenCalledTimes(1);
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
  it("keeps Disable, Reset second factors, Sign out everywhere and Delete behind a closed disclosure per row, not always inline", async () => {
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
    // RE-POINTED (design §7): "Reset password" and "Resend enrolment" were
    // Keycloak's mails; an account in use offers resetting its second factors
    // and signing it out everywhere.
    for (const name of [
      "Disable operator@example.com",
      "Reset second factors for operator@example.com",
      "Sign out operator@example.com everywhere",
      "Delete operator@example.com",
    ]) {
      expect(within(row).getByRole("button", { name }), name).toBeInTheDocument();
    }
  });

  /**
   * NEW test (spec: "Six equal buttons per row read as a toolbar with no
   * primary"). Roles and Permissions stay the row's two ordinary bordered
   * buttons; Disable, Reset second factors and Sign out everywhere step down to the
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
    // RE-POINTED (design §7): the three quiet actions of an account in use.
    const quiet = [
      within(row).getByRole("button", { name: "Disable operator@example.com" }),
      within(row).getByRole("button", { name: "Reset second factors for operator@example.com" }),
      within(row).getByRole("button", { name: "Sign out operator@example.com everywhere" }),
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

    // RE-POINTED (design §7): "Create user" is "Send invitation".
    expect(screen.getByRole("button", { name: "Send invitation" }), "Send invitation").toBeDisabled();
    for (const name of [
      "Disable operator@example.com",
      "Delete operator@example.com",
      "Reset second factors for operator@example.com",
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
 * still allows — one role held both on every client and on one client.
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
        client_id: null,
      },
      {
        assignment_id: "d2222222-2222-4222-8222-222222222222",
        role_id: TECHNICIAN_ROLE.id,
        role_name: "technician",
        client_id: null,
      },
      {
        assignment_id: "d3333333-3333-4333-8333-333333333333",
        role_id: TECHNICIAN_ROLE.id,
        role_name: "technician",
        client_id: null,
      },
      {
        assignment_id: "d4444444-4444-4444-8444-444444444444",
        role_id: TECHNICIAN_ROLE.id,
        role_name: "technician",
        client_id: "44444444-4444-4444-8444-444444444444",
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
