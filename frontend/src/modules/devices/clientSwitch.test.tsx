/**
 * AN EDIT NEVER CARRIES TO ANOTHER CLIENT (coordinator ruling 12, fix round 1
 * of wave O1). Each form of this wave whose record depends on the client
 * chosen at the top: a new device group (filed under that client), a push
 * authorization (one client's network), a credential binding (that client's
 * credentials) and a subnet scan (that client's subnets). Each is edited for
 * client A, the top bar switches to client B, and the form shows B's (empty)
 * values and Save sends nothing of A's edit.
 *
 * WHAT DOES IT is the shell, not the pages: `ClientScopeProvider` keys
 * everything under it by the choice (`lib/clientScope.tsx`, "a new key is a
 * new page: every query asked again, every form empty"). These tests mount the
 * real provider around each page, so a page that kept a draft outside that
 * tree -- in a module-level store, the URL, or a cache the switch does not
 * empty -- would go red here.
 */
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { fixtureClient, fixtureRegistry } from "../../fixtures/client";
import type { Me, MeClient } from "../../lib/api";
import { forgetStoredClient, setSelectedClient } from "../../lib/clientChoice";
import { ClientScopeProvider } from "../../lib/clientScope";
import { renderPage } from "../../test/renderPage";
import { PushAuthorizationsPage } from "../agent-push/PushAuthorizationsPage";
import { PushCredentialScopesPage } from "../agent-push/PushCredentialScopesPage";
import { CandidatesPage } from "./CandidatesPage";
import { GroupsPage } from "./GroupsPage";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));
vi.mock("../../lib/ws", () => ({ useLiveEvents: () => undefined }));

/** Two customers, both UUIDs: the top bar reads back only a UUID as a client. */
const CLIENT_A = "0b6a2c1e-6d2f-4a51-9c3e-2f4d1a7b8c01";
const CLIENT_B = "5c9e1d2f-3a4b-4c5d-8e6f-7a8b9c0d1e02";

/** The fixture API, its session holding every code in two customers, and spies on the four writes. */
function api() {
  const registry = fixtureRegistry();
  const base = (registry.me as () => Me)();
  const codes = base.permissions;
  const customer = (id: string, name: string): MeClient => ({
    id,
    name,
    kind: "customer",
    enabled_modules: [],
    permissions: codes,
  });
  const writes = {
    createDeviceGroup: vi.fn(registry.createDeviceGroup),
    createPushAuthorization: vi.fn(registry.createPushAuthorization),
    bindPushCredentialScope: vi.fn(registry.bindPushCredentialScope),
    dispatchIpamSweep: vi.fn(registry.dispatchIpamSweep),
  };
  mockUseApi.mockReturnValue(
    fixtureClient({
      ...registry,
      ...writes,
      me: () => ({ ...base, clients: [customer(CLIENT_A, "Acme Dental"), customer(CLIENT_B, "Zeta Foods")] }),
    }),
  );
  return writes;
}

function scoped(path: string, page: ReactElement) {
  setSelectedClient(CLIENT_A);
  return renderPage(path, <ClientScopeProvider>{page}</ClientScopeProvider>);
}

afterEach(() => {
  act(() => setSelectedClient(null));
  forgetStoredClient();
});

describe("switching the client drops an unsaved edit (ruling 12)", () => {
  it("a new device group", async () => {
    const writes = api();
    scoped("/device-groups?new=group", <GroupsPage />);
    const dialog = await screen.findByRole("dialog", { name: "New device group" });
    await userEvent.type(within(dialog).getByLabelText("Name"), "acme-desks");

    act(() => setSelectedClient(CLIENT_B));

    await waitFor(() =>
      expect(within(screen.getByRole("dialog", { name: "New device group" })).getByLabelText("Name")).toHaveValue(""),
    );
    await userEvent.click(screen.getByRole("button", { name: "Create group" }));
    expect(writes.createDeviceGroup).not.toHaveBeenCalled();
  });

  it("a push authorization", async () => {
    const writes = api();
    scoped("/agent-push/authorizations?new=authorization", <PushAuthorizationsPage />);
    const dialog = await screen.findByRole("dialog", { name: "Authorize a prefix" });
    await userEvent.type(within(dialog).getByPlaceholderText("10.4.7.0/24"), "10.4.7.0/24");

    act(() => setSelectedClient(CLIENT_B));

    await waitFor(() => expect(screen.getByPlaceholderText("10.4.7.0/24")).toHaveValue(""));
    await userEvent.click(screen.getByRole("button", { name: "Authorize" }));
    expect(writes.createPushAuthorization).not.toHaveBeenCalled();
  });

  it("a credential binding", async () => {
    const writes = api();
    scoped("/agent-push/credential-scopes?new=scope", <PushCredentialScopesPage />);
    const dialog = await screen.findByRole("dialog", { name: "Bind a credential to a prefix" });
    await userEvent.type(within(dialog).getByPlaceholderText("10.4.7.0/24"), "10.4.7.0/24");

    act(() => setSelectedClient(CLIENT_B));

    await waitFor(() => expect(screen.getByPlaceholderText("10.4.7.0/24")).toHaveValue(""));
    await userEvent.click(screen.getByRole("button", { name: "Bind" }));
    expect(writes.bindPushCredentialScope).not.toHaveBeenCalled();
  });

  it("a subnet scan", async () => {
    const writes = api();
    scoped("/discovered?new=scan", <CandidatesPage />);
    const dialog = await screen.findByRole("dialog", { name: /scan a subnet/i });
    const picker = within(dialog).getByRole("combobox");
    // The first subnet the picker offers, once the subnets have answered.
    const value = await waitFor(() => {
      const offered = within(dialog)
        .getAllByRole<HTMLOptionElement>("option")
        .find((item) => item.value !== "");
      expect(offered).toBeDefined();
      return (offered as HTMLOptionElement).value;
    });
    await userEvent.selectOptions(picker, value);
    expect(picker).toHaveValue(value);

    act(() => setSelectedClient(CLIENT_B));

    await waitFor(() =>
      expect(within(screen.getByRole("dialog", { name: /scan a subnet/i })).getByRole("combobox")).toHaveValue(""),
    );
    expect(writes.dispatchIpamSweep).not.toHaveBeenCalled();
  });
});
