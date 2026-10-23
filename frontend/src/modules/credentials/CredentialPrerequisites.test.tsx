import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient, Credential } from "../../lib/api";
import { expectCta, expectNoPanel, meWith, renderPanel } from "../../test/prerequisiteHarness";
import { CredentialPrerequisites } from "./CredentialPrerequisites";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const TITLE = "Before a saved credential can be used";

const credential = (id: string, name: string, type: string) => ({ id, name, type }) as unknown as Credential;
const scope = (credential_id: string, unbound = false) => ({
  id: `s-${credential_id}`,
  credential_id,
  credential_name: credential_id,
  prefix: "10.0.0.0/24",
  unbound_at: unbound ? "2026-09-01T00:00:00Z" : null,
});

function stub(scopes: unknown[], permissions = ["credentials.read", "agent.push.scope.manage"]) {
  const api = { me: meWith(permissions), listPushCredentialScopes: vi.fn().mockResolvedValue(scopes) };
  mockUseApi.mockReturnValue(api as unknown as ApiClient);
  return api;
}

describe("CredentialPrerequisites", () => {
  it("names a push install credential assigned to no range and links to assign it", async () => {
    stub([scope("c2"), scope("c1", true)]);
    renderPanel(
      <CredentialPrerequisites
        credentials={[credential("c1", "Branch push", "push_install"), credential("c2", "HQ push", "push_install")]}
      />,
    );
    await expectCta("Assign it to a range", "/agent-push/credential-scopes");
    expect(screen.getByText(/Branch push is assigned to no address range/)).toBeInTheDocument();
  });

  it("counts a Windows domain account as a push credential that needs a range", async () => {
    // A `windows_domain` credential is presented by a Windows push only on the
    // ranges it is assigned to, exactly as a `push_install` one is (D10
    // overturned 2026-09-28).
    stub([]);
    renderPanel(<CredentialPrerequisites credentials={[credential("c3", "Corp push", "windows_domain")]} />);
    await expectCta("Assign it to a range", "/agent-push/credential-scopes");
    expect(screen.getByText(/Corp push is assigned to no address range/)).toBeInTheDocument();
  });

  it("is absent when every push install credential is assigned", async () => {
    const api = stub([scope("c1")]);
    renderPanel(<CredentialPrerequisites credentials={[credential("c1", "Branch push", "push_install")]} />);
    await expectNoPanel(TITLE, () => expect(api.listPushCredentialScopes).toHaveBeenCalledWith(false));
  });

  it("asks nothing and shows nothing when no push install credential is saved", async () => {
    const api = stub([]);
    renderPanel(<CredentialPrerequisites credentials={[credential("c1", "Admin", "windows_admin")]} />);
    await expectNoPanel(TITLE, () => expect(api.me).toHaveBeenCalled());
    expect(api.listPushCredentialScopes).not.toHaveBeenCalled();
  });

  it("says it cannot be checked without the push scope permission", async () => {
    const api = stub([], ["credentials.read"]);
    renderPanel(<CredentialPrerequisites credentials={[credential("c1", "Branch push", "push_install")]} />);
    expect(await screen.findByText(/cannot be checked from this account/)).toBeInTheDocument();
    expect(api.listPushCredentialScopes).not.toHaveBeenCalled();
  });
});
