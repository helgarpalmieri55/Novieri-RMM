import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../../lib/api";
import { ApiError } from "../../../lib/api";
import { PushAuthorizationsPage } from "../PushAuthorizationsPage";
import { PushCredentialScopesPage } from "../PushCredentialScopesPage";
import { PUSH_PLATFORMS } from "../PushReadiness";
import type { HostKeyOut, PushAuthorizationOut, PushCredentialScopeOut } from "../types";

// Component tests against RENDERED OUTPUT, with the auth-backed API client
// mocked so nothing here touches fetch -- mirrors
// `modules/ipam/__tests__/ipam.test.tsx`.
//
// **EVERY ASSERTION BELOW IS ABOUT A CLAIM THE SCREEN MAKES TO AN OPERATOR WHO
// IS ABOUT TO AUTHORIZE CREDENTIALED LOGINS ON SOMEBODY'S NETWORK.** Not about
// wording: a reach stated in mask lengths instead of addresses, a revoked
// grant rendered as active, a "Revoke" button offered to someone who does not
// hold the code, a picker offering a credential the server will refuse, or a
// screen that lets an operator believe one grant finished the job. A test that
// asserted only "the page rendered" would pass against every one of those.

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const SCOPE_MANAGE = "agent.push.scope.manage";
const CREDENTIALS_READ = "credentials.read";
const SITE_READ = "site.read";
const RELEASE_READ = "agent.release.read";
const PUSH_RUN = "agent.push.run";

function renderWithProviders(node: React.ReactNode) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/agent-push/authorizations"]}>{node}</MemoryRouter>
    </QueryClientProvider>,
  );
}

function stubApi(overrides: Partial<ApiClient> & { permissions?: string[] } = {}): ApiClient {
  // `credentials.read` BESIDE THE SCOPE CODE: the readiness panel asks `/me`
  // before it reads the vault's list, and says "cannot be checked from this
  // account" to an account without it -- which is not what most tests here
  // are about. The two newer readiness steps' codes are left out on purpose,
  // so every test not about them sees them as uncheckable, never as done.
  const { permissions = [SCOPE_MANAGE, CREDENTIALS_READ], ...rest } = overrides;
  const api = {
    me: vi.fn().mockResolvedValue({
      user_id: "u1",
      org_id: "o1",
      email: "a@b.c",
      display_name: "A",
      permissions,
    }),
    listPushAuthorizations: vi.fn().mockResolvedValue([]),
    createPushAuthorization: vi.fn(),
    revokePushAuthorization: vi.fn().mockResolvedValue(undefined),
    listPushCredentialScopes: vi.fn().mockResolvedValue([]),
    bindPushCredentialScope: vi.fn(),
    unbindPushCredentialScope: vi.fn().mockResolvedValue(undefined),
    listCredentials: vi.fn().mockResolvedValue([]),
    listSiteServers: vi.fn().mockResolvedValue([]),
    listAgentReleases: vi.fn().mockResolvedValue([]),
    listPushHostKeys: vi.fn().mockResolvedValue([]),
    ...rest,
  } as unknown as ApiClient;
  mockUseApi.mockReturnValue(api);
  return api;
}

/** A credential-free WinRM probe's row (design §2.4): readiness, no host key. */
function winrmProbe(overrides: Partial<HostKeyOut> = {}): HostKeyOut {
  return {
    address: "10.20.4.31",
    transport: "winrm",
    observed_fingerprint: "winrm",
    observed_key_type: "",
    observed_at: "2026-09-28T09:00:00Z",
    confirmed_fingerprint: null,
    confirmed_at: null,
    confirmed_by: null,
    installable: false,
    winrm_https: false,
    winrm_http: false,
    kdc_reachable: false,
    ...overrides,
  };
}

function sshProbe(): HostKeyOut {
  return winrmProbe({
    address: "10.20.4.40",
    transport: "ssh",
    observed_fingerprint: "SHA256:AAAA",
    observed_key_type: "ssh-ed25519",
  });
}

function grant(overrides: Partial<PushAuthorizationOut> = {}): PushAuthorizationOut {
  return {
    id: "g1",
    prefix: "10.0.0.0/8",
    address_count: 16777216,
    note: "onboarding",
    authorized_by: "u1",
    authorized_at: "2026-08-20T09:00:00Z",
    expires_at: null,
    revoked_at: null,
    revoked_by: null,
    ...overrides,
  };
}

function scope(overrides: Partial<PushCredentialScopeOut> = {}): PushCredentialScopeOut {
  return {
    id: "s1",
    credential_id: "c1",
    credential_name: "branch-installer",
    prefix: "10.4.7.0/24",
    address_count: 256,
    note: "",
    bound_by: "u1",
    bound_at: "2026-08-20T09:00:00Z",
    unbound_at: null,
    unbound_by: null,
    ...overrides,
  };
}

describe("PushAuthorizationsPage", () => {
  it("states every grant's reach in addresses, not in mask lengths", async () => {
    // "/8" is two characters and 16,777,216 machines. Misreading a scope is
    // how a credential ends up presented on a segment nobody meant to touch,
    // and this column is the only thing that stops it.
    stubApi({ listPushAuthorizations: vi.fn().mockResolvedValue([grant()]) });
    renderWithProviders(<PushAuthorizationsPage />);
    expect(await screen.findByText("16,777,216 addresses")).toBeTruthy();
  });

  it("says a grant is one of four bounds rather than letting it read as permission to push", async () => {
    // The failure this guards against is an operator authorizing a prefix and
    // believing the push is now allowed. Three other refusals still stand.
    stubApi();
    renderWithProviders(<PushAuthorizationsPage />);
    const body = await screen.findByText(/one of four checks/i);
    expect(body).toBeTruthy();
    // "vouched for", not "promoted": CLAUDE.md invariant 2a(d) records that
    // the word was replaced the day a sweep started writing asset rows nobody
    // selected. The screen states the first bound in the same words as the
    // invariant, and it did not -- the panel carried the superseded wording
    // while the note under the table carried the current one, on one screen.
    expect(document.body.textContent).toContain("a person vouched for");
    expect(document.body.textContent).not.toContain("promoted device record");
    expect(document.body.textContent).toContain("site server");
  });

  it("says an IPAM scan authorization grants nothing here", async () => {
    // The assumption most likely to be made, and exactly wrong: authorizing an
    // ICMP echo is not authorizing an administrative login. It is stated on
    // the grant form -- the control that acts on the assumption -- rather than
    // in the page description it used to trail.
    stubApi();
    renderWithProviders(<PushAuthorizationsPage />);
    expect(
      await screen.findByText(
        /IPAM scan authorization over the same prefix authorizes nothing here/i,
      ),
    ).toBeTruthy();
  });

  it("warns that the target cannot be verified, on the screen and not in a tooltip", async () => {
    // Spec §10.4 point 7 is explicit that this is a sentence, not a tooltip.
    stubApi();
    renderWithProviders(<PushAuthorizationsPage />);
    expect(
      await screen.findByText(
        /presents administrative credentials to machines that are not managed by this platform/i,
      ),
    ).toBeTruthy();
  });

  it("says plainly that nothing here dispatches a push, and how the installer connects", async () => {
    // These screens configure; they never push. A console that implied
    // otherwise would make an operator believe a push happened. Until
    // 2026-09-28 this said a Windows target is refused, which stopped being
    // true when D10 was overturned: Windows is pushed over WinRM with
    // Kerberos only (spec-push-install.md D10, design §2.3). Saying Windows is
    // refused now would send an operator to the install link for nothing, and
    // saying less than "Kerberos only" would hide why a push that offers NTLM
    // fails.
    stubApi();
    renderWithProviders(<PushAuthorizationsPage />);
    await screen.findByRole("heading", { name: "Push authorizations" });
    expect(document.body.textContent).toMatch(
      /Nothing on these screens dispatches a push/i,
    );
    expect(document.body.textContent).toMatch(/Windows over WinRM, with Kerberos only/i);
    expect(document.body.textContent).not.toMatch(/Windows target is refused/i);
    expect(document.body.textContent).not.toMatch(/SSH-first/i);
    expect(document.body.textContent).not.toMatch(/not built yet/i);
  });

  it("distinguishes an empty list from an unauthorized one", async () => {
    // "Nothing is authorized" and "you cannot see it" are opposite facts.
    // The empty state names the consequence rather than saying "no rows".
    stubApi();
    renderWithProviders(<PushAuthorizationsPage />);
    expect(
      await screen.findByText(/No address range is authorized for push install/i),
    ).toBeTruthy();
  });

  it("offers Revoke to a scope manager and to nobody else", async () => {
    // TWO RENDERS OF THE SAME ROW. A single render could not tell "the control
    // is gated" from "the control is missing". This is cosmetic -- the server
    // refuses regardless -- but a button that always 403s is a broken console.
    stubApi({ listPushAuthorizations: vi.fn().mockResolvedValue([grant()]) });
    const managed = renderWithProviders(<PushAuthorizationsPage />);
    expect(await screen.findByRole("button", { name: "Revoke" })).toBeTruthy();
    managed.unmount();

    stubApi({
      listPushAuthorizations: vi.fn().mockResolvedValue([grant()]),
      permissions: ["agent.push.run"],
    });
    renderWithProviders(<PushAuthorizationsPage />);
    await screen.findByText("10.0.0.0/8");
    expect(screen.queryByRole("button", { name: "Revoke" })).toBeNull();
  });

  it("renders a revoked grant as revoked, not as expired or active", async () => {
    // Three statuses, three facts. A revoked grant was withdrawn by a person;
    // an expired one lapsed on a clock. Collapsing them hides which happened.
    stubApi({
      listPushAuthorizations: vi
        .fn()
        .mockResolvedValue([grant({ revoked_at: "2026-08-22T00:00:00Z" })]),
    });
    renderWithProviders(<PushAuthorizationsPage />);
    expect(await screen.findByText("Revoked")).toBeTruthy();
    expect(screen.queryByText("Active")).toBeNull();
  });

  it("previews what a grant would open before it is made", async () => {
    // Deny-by-default only helps if the person granting knows what they are
    // opening. The preview is sized from what is typed, before any round trip.
    stubApi();
    renderWithProviders(<PushAuthorizationsPage />);
    const field = await screen.findByPlaceholderText("10.4.7.0/24");
    await userEvent.type(field, "10.0.0.0/8");
    await waitFor(() =>
      expect(screen.getByText(/16,777,216 addresses in reach/)).toBeTruthy(),
    );
    // ...and it says so about a WIDE grant specifically, because /8 is four
    // characters and nothing narrows it afterwards except revoking it.
    expect(document.body.textContent).toMatch(/wide authorization/i);
  });

  it("refuses to preview something that is not a prefix", async () => {
    stubApi();
    renderWithProviders(<PushAuthorizationsPage />);
    const field = await screen.findByPlaceholderText("10.4.7.0/24");
    await userEvent.type(field, "server-17.corp.example");
    await waitFor(() => expect(screen.getByText(/That is not a prefix/)).toBeTruthy());
    expect((screen.getByRole("button", { name: "Authorize" }) as HTMLButtonElement).disabled).toBe(
      true,
    );
  });
});

describe("PushCredentialScopesPage", () => {
  it("names the credential and never anything that could reconstruct its secret", async () => {
    stubApi({ listPushCredentialScopes: vi.fn().mockResolvedValue([scope()]) });
    renderWithProviders(<PushCredentialScopesPage />);
    expect(await screen.findByText("branch-installer")).toBeTruthy();
    const body = document.body.textContent ?? "";
    for (const forbidden of ["ciphertext", "vault_ref", "wrapped_dek"]) {
      expect(body).not.toContain(forbidden);
    }
  });

  it("names the permission in words, never as its code", async () => {
    // Stated on the bind form -- the control the permission gates -- and in
    // the words an administrator finds on the role screen.
    stubApi();
    renderWithProviders(<PushCredentialScopesPage />);
    const stated = await screen.findByText(/Binding and unbinding need/);
    expect(stated.textContent).toContain("permission to manage push install scopes");
    expect(stated.textContent).toContain("separate from permission to run a push");
    expect(stated.textContent).not.toMatch(/agent\.push\.scope\.manage/);
  });

  it("says the credential should not be a domain admin, where the choice is made", async () => {
    // The platform cannot check what rights an account holds in somebody
    // else's directory. It can refuse to pretend the choice does not matter.
    stubApi();
    renderWithProviders(<PushCredentialScopesPage />);
    expect(await screen.findByText(/not Domain Admin/i)).toBeTruthy();
  });

  it("explains an empty picker instead of leaving a blank dropdown", async () => {
    // "There are no push credentials yet" and "you cannot read the vault" are
    // different facts with different remedies, and a bare empty select says
    // neither.
    stubApi();
    renderWithProviders(<PushCredentialScopesPage />);
    expect(await screen.findByText(/No push credential exists yet/i)).toBeTruthy();
  });

  it("offers only push credentials in the picker", async () => {
    // The server refuses any other type. A picker listing the domain
    // remote-control account would teach an operator that the dedication rule
    // is an obstacle rather than the point. A Windows domain account is a push
    // credential since D10 was overturned (PUSHABLE_CREDENTIAL_TYPES), so it
    // is offered, and named by what it is rather than by its wire type.
    stubApi({
      listCredentials: vi.fn().mockResolvedValue([
        { id: "c1", name: "branch-installer", type: "push_install", has_secret: true },
        { id: "c2", name: "domain-admin", type: "windows_admin", has_secret: true },
        { id: "c3", name: "corp-push", type: "windows_domain", has_secret: true },
      ]),
    });
    renderWithProviders(<PushCredentialScopesPage />);
    await screen.findByText(/Bind a credential to a prefix/);
    await waitFor(() => expect(screen.getByRole("option", { name: "branch-installer" })).toBeTruthy());
    expect(screen.getByRole("option", { name: /^corp-push · Windows domain/ })).toBeTruthy();
    expect(screen.queryByRole("option", { name: /domain-admin/ })).toBeNull();
    expect(screen.queryByText(/windows_domain/)).toBeNull();
  });

  it("keeps Bind disabled until both a credential and a prefix are chosen", async () => {
    // Both halves are the bound: a credential with no prefix binds nothing,
    // and a prefix with no credential binds nothing to it.
    stubApi({
      listCredentials: vi
        .fn()
        .mockResolvedValue([
          { id: "c1", name: "branch-installer", type: "push_install", has_secret: true },
        ]),
    });
    renderWithProviders(<PushCredentialScopesPage />);
    const button = (await screen.findByRole("button", { name: "Bind" })) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    await userEvent.type(await screen.findByPlaceholderText("10.4.7.0/24"), "10.4.7.0/24");
    await waitFor(() => expect(button.disabled).toBe(true));
    await userEvent.selectOptions(screen.getByRole("combobox"), "c1");
    await waitFor(() => expect(button.disabled).toBe(false));
  });

  it("says binding authorizes nothing on its own", async () => {
    stubApi();
    renderWithProviders(<PushCredentialScopesPage />);
    const field = await screen.findByPlaceholderText("10.4.7.0/24");
    await userEvent.type(field, "10.4.7.0/24");
    await waitFor(() =>
      expect(screen.getByText(/Binding does not authorize anything on its own/i)).toBeTruthy(),
    );
  });

  it("renders an unbound scope as unbound, because the row survives", async () => {
    stubApi({
      listPushCredentialScopes: vi
        .fn()
        .mockResolvedValue([scope({ unbound_at: "2026-08-22T00:00:00Z" })]),
    });
    renderWithProviders(<PushCredentialScopesPage />);
    expect(await screen.findByText("Unbound")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Unbind" })).toBeNull();
  });
});

/**
 * THE MODULE'S TAB STRIP, which announced itself as something it was not and
 * sat in the wrong place while doing it.
 */
describe("PushNav", () => {
  it("is a navigation of links, not a tablist it cannot honour", async () => {
    // It carried `role="tablist"` with `role="tab"` children and none of what
    // that promises -- no `aria-controls`, no roving tabindex, no arrow keys.
    // Two links with the current one marked is the truth about it, and it
    // keeps what a button would have lost: an href to middle-click, copy or
    // open in a new tab.
    stubApi();
    renderWithProviders(<PushAuthorizationsPage />);
    await screen.findByRole("heading", { name: "Push authorizations" });

    expect(screen.queryByRole("tablist")).toBeNull();
    const strip = screen.getByRole("navigation", { name: "Push install" });
    const links = [...strip.querySelectorAll("a")];
    expect(links.map((link) => link.getAttribute("href"))).toEqual([
      "/agent-push/authorizations",
      "/agent-push/credential-scopes",
    ]);
    expect(links[0]?.getAttribute("aria-current")).toBe("page");
    expect(links[1]?.getAttribute("aria-current")).toBeNull();
  });

  it("sits under the head, where every other tabbed page puts it", async () => {
    // Above the head it reads as an eyebrow band between the breadcrumb and
    // the h1. /audit, /credentials, /patch and /remote all order it head,
    // then strip.
    stubApi();
    const { container } = renderWithProviders(<PushCredentialScopesPage />);
    const heading = await screen.findByRole("heading", { name: "Credential scopes" });
    const strip = screen.getByRole("navigation", { name: "Push install" });
    expect(
      heading.compareDocumentPosition(strip) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(container.querySelector("h1")).toBe(heading);
  });
});

describe("Before a push can run", () => {
  it("says no push credential is saved and links straight to adding one", async () => {
    stubApi();
    renderWithProviders(<PushAuthorizationsPage />);
    await screen.findByText(/None is saved, so every push is refused/);
    const add = screen.getByRole("link", { name: "Add a push install credential" });
    expect(add).toHaveAttribute("href", "/credentials?new=push_install");
    // Nothing further is offered until the first piece exists.
    expect(screen.getByText("Save a credential first.")).toBeInTheDocument();
  });

  it("counts only push install credentials, then asks for a range assignment", async () => {
    stubApi({
      listCredentials: vi.fn().mockResolvedValue([
        { id: "c2", name: "domain-admin", type: "windows_admin", has_secret: true },
        { id: "c1", name: "branch-installer", type: "push_install", has_secret: true },
      ]),
    });
    renderWithProviders(<PushAuthorizationsPage />);
    await screen.findByText("1 credential is saved for push install.");
    expect(screen.queryByRole("link", { name: "Add a push install credential" })).toBeNull();
    expect(screen.getByRole("link", { name: "Assign it to a range" })).toHaveAttribute(
      "href",
      "/agent-push/credential-scopes",
    );
  });

  it("marks every piece done when all three are in place", async () => {
    stubApi({
      listCredentials: vi
        .fn()
        .mockResolvedValue([{ id: "c1", name: "branch-installer", type: "push_install", has_secret: true }]),
      listPushCredentialScopes: vi.fn().mockResolvedValue([scope()]),
      listPushAuthorizations: vi.fn().mockResolvedValue([grant()]),
    });
    renderWithProviders(<PushCredentialScopesPage />);
    await screen.findByText("1 assignment is in place.");
    expect(screen.getByText("1 range is authorized.")).toBeInTheDocument();
    const list = screen.getByText("1 range is authorized.").closest("ol")!;
    expect(within(list).getAllByText("Done")).toHaveLength(3);
  });

  it("does not count an expired authorization as one in force", async () => {
    stubApi({
      listPushAuthorizations: vi
        .fn()
        .mockResolvedValue([grant({ expires_at: "2020-01-01T00:00:00Z" })]),
    });
    renderWithProviders(<PushCredentialScopesPage />);
    expect(
      await screen.findByText("No range has an authorization in force, so every push is refused."),
    ).toBeInTheDocument();
  });

  it("says a failed check could not be made rather than reporting nothing authorized", async () => {
    stubApi({
      listPushAuthorizations: vi
        .fn()
        .mockRejectedValue(new ApiError(503, JSON.stringify({ detail: "database unavailable" }))),
    });
    renderWithProviders(<PushCredentialScopesPage />);
    expect(await screen.findByText(/This could not be checked\. .*database unavailable/)).toBeInTheDocument();
    expect(screen.queryByText(/No range has an authorization in force/)).toBeNull();
  });

  it("still checks assignments when the credential list cannot be read", async () => {
    stubApi({
      listCredentials: vi
        .fn()
        .mockRejectedValue(new ApiError(403, JSON.stringify({ detail: "requires credentials.read" }))),
      listPushCredentialScopes: vi.fn().mockResolvedValue([scope()]),
    });
    renderWithProviders(<PushCredentialScopesPage />);
    expect(await screen.findByText("1 assignment is in place.")).toBeInTheDocument();
    expect(screen.queryByText("Save a credential first.")).toBeNull();
  });
});

describe("Before a push can run: site server and agent package", () => {
  const pushCredential = { id: "c1", name: "branch-installer", type: "push_install", has_secret: true };
  const siteServer = (segments: string[], overrides: Record<string, unknown> = {}) => ({
    id: `ss-${segments.join("-")}`,
    device_id: "d1",
    name: "rack-b4-relay",
    segments,
    roles: [],
    enabled: true,
    notes: "",
    ipam_site_id: null,
    agent_status: "online",
    agent_last_seen_at: null,
    ...overrides,
  });
  const release = (os: string, arch: string) => ({
    id: `rel-${os}-${arch}`,
    version: "1.8.2",
    os,
    arch,
    size_bytes: 1,
    sha256: "ab",
    signed: true,
    notes: "",
    published_at: "2026-09-01T00:00:00Z",
    has_artifact: true,
  });
  const ALL = [SCOPE_MANAGE, CREDENTIALS_READ, SITE_READ, RELEASE_READ];

  it("says no site server serves an authorized range, and links to promoting one", async () => {
    // The preflight's `no_site_server` refusal: every packet to an unenrolled
    // machine leaves from a site server on its segment, with no fallback
    // (invariant 2a(a)). Until now the panel said nothing about it.
    const api = stubApi({
      permissions: ALL,
      listPushAuthorizations: vi.fn().mockResolvedValue([grant({ prefix: "10.30.1.0/24" })]),
      listSiteServers: vi.fn().mockResolvedValue([siteServer(["10.20.4.0/24"])]),
    });
    renderWithProviders(<PushAuthorizationsPage />);
    expect(await screen.findByText(/No enabled site server serves 10\.30\.1\.0\/24\./)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Promote a site server" })).toHaveAttribute("href", "/site-servers");
    // Only enabled servers count, which is what the preflight asks for.
    expect(api.listSiteServers).toHaveBeenCalledWith({ enabled: true });
  });

  it("says a range is only partly served when some of its addresses have no site server", async () => {
    stubApi({
      permissions: ALL,
      listPushAuthorizations: vi.fn().mockResolvedValue([grant({ prefix: "10.4.0.0/16" })]),
      listSiteServers: vi.fn().mockResolvedValue([siteServer(["10.4.7.0/24"])]),
    });
    renderWithProviders(<PushAuthorizationsPage />);
    expect(await screen.findByText(/10\.4\.0\.0\/16 is only partly served\./)).toBeInTheDocument();
  });

  it("counts a range two site servers cover between them as served, and says what it did not check", async () => {
    stubApi({
      permissions: ALL,
      listPushAuthorizations: vi.fn().mockResolvedValue([grant({ prefix: "10.4.6.0/23" })]),
      listSiteServers: vi
        .fn()
        .mockResolvedValue([siteServer(["10.4.6.0/24"]), siteServer(["10.4.7.0/24"], { id: "ss-2" })]),
    });
    renderWithProviders(<PushAuthorizationsPage />);
    const detail = await screen.findByText(/Every authorized range is inside an enabled site server.s segment\./);
    expect(detail.textContent).toContain("connected right now is not checked here");
    expect(screen.queryByRole("link", { name: "Promote a site server" })).toBeNull();
  });

  it("waits for an authorized range before asking about site servers", async () => {
    stubApi({ permissions: ALL });
    renderWithProviders(<PushAuthorizationsPage />);
    expect(await screen.findByText("Authorize a range first.")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Promote a site server" })).toBeNull();
  });

  it("says an account that cannot see site servers cannot check, and does not ask", async () => {
    const api = stubApi({
      listPushAuthorizations: vi.fn().mockResolvedValue([grant({ prefix: "10.30.1.0/24" })]),
    });
    renderWithProviders(<PushAuthorizationsPage />);
    expect(
      await screen.findByText("This cannot be checked from this account: it needs permission to view site servers."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Promote a site server" })).toBeNull();
    expect(api.listSiteServers).not.toHaveBeenCalled();
  });

  it("says no agent package is published for what a push installs onto, and links to publishing one", async () => {
    stubApi({ permissions: ALL, listAgentReleases: vi.fn().mockResolvedValue([release("windows", "386")]) });
    renderWithProviders(<PushCredentialScopesPage />);
    // A 32-bit Windows package does not count: dispatch accepts amd64 and
    // arm64 only (`agent_push/dispatch.py`).
    expect(await screen.findByText(/no agent package is published for any of them/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Publish an agent package" })).toHaveAttribute(
      "href",
      "/agent-packages#releases",
    );
  });

  it("names the push platforms a published package covers, and the ones it does not", async () => {
    stubApi({ permissions: ALL, listAgentReleases: vi.fn().mockResolvedValue([release("linux", "amd64")]) });
    renderWithProviders(<PushCredentialScopesPage />);
    const detail = await screen.findByText(/^Published for Linux \(64-bit Intel\/AMD\)\./);
    expect(detail.textContent).toContain("Nothing yet for");
    expect(detail.textContent).toContain("macOS (Apple silicon)");
    expect(screen.queryByRole("link", { name: "Publish an agent package" })).toBeNull();
  });

  it("says an account that cannot see agent packages cannot check", async () => {
    const api = stubApi();
    renderWithProviders(<PushCredentialScopesPage />);
    expect(
      await screen.findByText("This cannot be checked from this account: it needs permission to install agents."),
    ).toBeInTheDocument();
    expect(api.listAgentReleases).not.toHaveBeenCalled();
  });

  it("counts a published Windows package now that Windows is pushed", async () => {
    // D10 overturned (2026-09-28): dispatch accepts windows amd64 and arm64.
    stubApi({ permissions: ALL, listAgentReleases: vi.fn().mockResolvedValue([release("windows", "amd64")]) });
    renderWithProviders(<PushCredentialScopesPage />);
    const detail = await screen.findByText(/^Published for Windows \(64-bit Intel\/AMD\)\./);
    expect(detail.textContent).toContain("Windows (64-bit ARM)");
    expect(screen.queryByRole("link", { name: "Publish an agent package" })).toBeNull();
  });

  it("pushes onto Windows amd64 and arm64 as well as Linux and macOS", () => {
    expect(PUSH_PLATFORMS).toEqual(
      expect.arrayContaining([
        { os: "windows", arch: "amd64" },
        { os: "windows", arch: "arm64" },
        { os: "linux", arch: "amd64" },
        { os: "darwin", arch: "arm64" },
      ]),
    );
  });

  it("marks all seven done, and keeps the panel on screen when they are", async () => {
    stubApi({
      permissions: [...ALL, PUSH_RUN],
      listCredentials: vi.fn().mockResolvedValue([pushCredential]),
      listPushCredentialScopes: vi.fn().mockResolvedValue([scope()]),
      listPushAuthorizations: vi.fn().mockResolvedValue([grant({ prefix: "10.20.4.0/24" })]),
      listSiteServers: vi.fn().mockResolvedValue([siteServer(["10.20.0.0/16"])]),
      listAgentReleases: vi.fn().mockResolvedValue([release("linux", "amd64"), release("darwin", "arm64")]),
      listPushHostKeys: vi.fn().mockResolvedValue([winrmProbe({ winrm_https: true, kdc_reachable: true })]),
    });
    renderWithProviders(<PushCredentialScopesPage />);
    await screen.findByText(/Every authorized range is inside/);
    await screen.findByText(/^Published for/);
    await screen.findByText(/^WinRM answered on 1 of 1 Windows machine probed/);
    expect(screen.getByRole("heading", { name: "Before a push can run" })).toBeInTheDocument();
    const list = screen.getByText("1 range is authorized.").closest("ol")!;
    expect(within(list).getAllByText("Done")).toHaveLength(7);
  });
});

describe("Before a push can run: Windows over WinRM", () => {
  // Design §2.4: the credential-free WinRM probe records whether WinRM
  // answered on the target and whether a domain controller was reachable from
  // the site server. These two items are what an operator on a Windows estate
  // opens the panel to find out, and until 2026-09-28 the panel said instead
  // that Windows was refused.
  const ALL = [SCOPE_MANAGE, CREDENTIALS_READ, SITE_READ, RELEASE_READ, PUSH_RUN];
  const WINRM = "WinRM answers on the target";
  const KDC = "A domain controller is reachable from the site server";

  function item(label: string): HTMLElement {
    return screen.getByText(label).closest("li")!;
  }

  it("marks both done when a probed Windows target answered WinRM and a KDC was reachable", async () => {
    const api = stubApi({
      permissions: ALL,
      listPushHostKeys: vi.fn().mockResolvedValue([winrmProbe({ winrm_https: true, kdc_reachable: true })]),
    });
    renderWithProviders(<PushCredentialScopesPage />);
    await screen.findByText(/^WinRM answered on 1 of 1 Windows machine probed/);
    expect(within(item(WINRM)).getByText("Done")).toBeInTheDocument();
    expect(within(item(KDC)).getByText("Done")).toBeInTheDocument();
    expect(item(WINRM).textContent).toContain("HTTPS");
    expect(api.listPushHostKeys).toHaveBeenCalled();
  });

  it("says both are missing when neither port answered and no KDC was reachable, pointing at the Group Policy runbook", async () => {
    stubApi({
      permissions: ALL,
      listPushHostKeys: vi
        .fn()
        .mockResolvedValue([winrmProbe({ winrm_https: false, winrm_http: false, kdc_reachable: false })]),
    });
    renderWithProviders(<PushCredentialScopesPage />);
    await screen.findByText(/WinRM answered on none of the 1 Windows machine probed/);
    expect(within(item(WINRM)).queryByText("Done")).toBeNull();
    expect(within(item(KDC)).queryByText("Done")).toBeNull();
    expect(item(WINRM).textContent).toMatch(/Enable WinRM by Group Policy/);
    expect(item(WINRM).textContent).toContain("deploy/WINDOWS-PUSH.md");
    expect(item(KDC).textContent).toContain("deploy/WINDOWS-PUSH.md");
    // Review I2: advisory, and says why.
    expect(item(KDC).textContent).toMatch(/not found via DNS — fine if the credential names a domain controller/);
    // The item rendered in the missing state, not as uncheckable or pending.
    expect(item(WINRM).getAttribute("aria-current")).toBe("step");
    expect(item(KDC).getAttribute("aria-current")).toBe("step");
  });

  it("does not count an SSH probe as a Windows machine, and says nothing has been probed", async () => {
    stubApi({
      permissions: ALL,
      listPushHostKeys: vi.fn().mockResolvedValue([sshProbe()]),
    });
    renderWithProviders(<PushCredentialScopesPage />);
    const detail = await screen.findAllByText(/No Windows machine has been probed yet/);
    expect(detail).toHaveLength(2);
    expect(within(item(WINRM)).queryByText("Done")).toBeNull();
  });

  it("says an account that cannot run a push cannot check, and does not ask", async () => {
    const api = stubApi({ permissions: [SCOPE_MANAGE, CREDENTIALS_READ] });
    renderWithProviders(<PushCredentialScopesPage />);
    const refusals = await screen.findAllByText(
      "This cannot be checked from this account: it needs permission to run push installs.",
    );
    expect(refusals).toHaveLength(2);
    expect(api.listPushHostKeys).not.toHaveBeenCalled();
  });

  it("no longer says Windows is refused, anywhere on either screen", async () => {
    stubApi({ permissions: ALL });
    renderWithProviders(<PushCredentialScopesPage />);
    await screen.findByText(WINRM);
    expect(document.body.textContent).not.toMatch(/Windows (target|machine)s? (is|are) refused/i);
    expect(document.body.textContent).not.toMatch(/SSH-first/i);
    expect(document.body.textContent).not.toMatch(/a Windows package does not count/i);
  });

  it("names no wire value on screen", async () => {
    stubApi({
      permissions: ALL,
      listPushHostKeys: vi.fn().mockResolvedValue([winrmProbe({ winrm_http: true })]),
    });
    renderWithProviders(<PushCredentialScopesPage />);
    await screen.findByText(/^WinRM answered on 1 of 1/);
    const text = document.body.textContent ?? "";
    for (const wire of ["winrm_https", "winrm_http", "kdc_reachable", "windows_domain", "winrm "]) {
      expect(text).not.toContain(wire);
    }
  });

  it("warns, standing, that a Windows domain account holds the domain while a push runs", async () => {
    // Design §2.3 "What remains": the site server holds the password in memory
    // for the push, so a Domain Admin credential makes a compromised site
    // server a compromised domain. The operator chose a Domain Admin (Q7); the
    // console says what that means rather than refusing it.
    stubApi({
      permissions: ALL,
      listCredentials: vi
        .fn()
        .mockResolvedValue([{ id: "c3", name: "corp-push", type: "windows_domain", has_secret: true }]),
    });
    renderWithProviders(<PushCredentialScopesPage />);
    const detail = await screen.findByText(/^1 credential is saved for push install\./);
    expect(detail.textContent).toMatch(/Domain Admin/);
    expect(detail.textContent).toMatch(/compromised domain/);
  });
});
