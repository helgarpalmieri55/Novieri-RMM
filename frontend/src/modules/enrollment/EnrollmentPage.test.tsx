/**
 * Enrollment & certificates.
 *
 * Beyond the three states, two properties are worth pinning:
 *
 * * **The two tabs are gated independently.** Certificates come from
 *   `GET /devices` (`devices.read`); tokens come from `GET /enrollment-tokens`
 *   (`enrollment.manage`). A technician who holds only the first must not be
 *   offered a token table that answers 403 — and must not have the request
 *   fired on their behalf either.
 *
 * * **The one-time token is treated as a one-time token.** The plaintext is
 *   served exactly once by the server. The page must say so, and must let the
 *   operator drop it; a bootstrap credential that lingers on screen (or in
 *   query state) after it has been copied is a credential lying around.
 *
 * CYCLES 7 AND 8 ADD A THIRD, and it is the one the audit filed twice: NOTHING
 * ON THIS PAGE MINTS OR DESTROYS A CREDENTIAL WITHOUT BEING ASKED TWICE. The
 * tests below are written so that a regression to the audited behaviour — a
 * "Create token" click that writes, an empty `max_uses` that means unlimited, a
 * revoke that fires on one click, a dismiss that destroys an uncopied token —
 * fails here rather than in the field.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../lib/api";
import type { ApiClient, Device } from "../../lib/api";
import { ENROLLMENT_TOKENS_PAGE_SIZE } from "./api";
import { EnrollmentPage } from "./EnrollmentPage";
import type { EnrollmentTokenListItem, EnrollmentTokenPage } from "./types";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const listEnrollmentTokens = vi.fn();
const createEnrollmentToken = vi.fn();
const revokeEnrollmentToken = vi.fn();
const revokeAgent = vi.fn();
// SPREAD OVER THE REAL MODULE rather than re-declared. The four request
// functions are stubbed; the CONSTANTS are the module's own, so
// `ENROLLMENT_TOKENS_PAGE_SIZE` below is the number the page actually pages
// by. A hand-written copy here would let the page and its test agree on a page
// size the console does not use.
vi.mock("./api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./api")>()),
  listEnrollmentTokens: (...a: unknown[]) => listEnrollmentTokens(...a),
  createEnrollmentToken: (...a: unknown[]) => createEnrollmentToken(...a),
  revokeEnrollmentToken: (...a: unknown[]) => revokeEnrollmentToken(...a),
  revokeAgent: (...a: unknown[]) => revokeAgent(...a),
}));

// Far enough in the future that "expired" is never a clock artefact.
const FUTURE = "2099-01-01T00:00:00Z";
const PAST = "2020-01-01T00:00:00Z";

function token(overrides: Partial<EnrollmentTokenListItem> = {}): EnrollmentTokenListItem {
  return {
    id: "tttttttt-0000-0000-0000-000000000001",
    name: "Bogota HQ workstations",
    expires_at: FUTURE,
    max_uses: 50,
    use_count: 4,
    revoked: false,
    ...overrides,
  };
}

function device(overrides: Partial<Device> = {}): Device {
  return {
    id: "11111111-1111-1111-1111-111111111111",
    hostname: "ws-alpha",
    fqdn: "ws-alpha.corp.local",
    os_family: "windows",
    os_version: "11 23H2",
    architecture: "amd64",
    device_group_id: null,
    status: "active",
    agent_status: "online",
    last_seen_at: "2026-08-16T09:00:00Z",
    cert_not_after: "2027-03-12T00:00:00Z",
    cert_state: "ok",
    cert_revoked: false,
    // Asset fields (asset-inventory spec §2). Defaulted to what every
    // agent-enrolled device is, so these tests describe the same fleet they
    // did before assets existed.
    lifecycle: "in_service" as const,
    asset_type: "computer" as const,
    source: "agent" as const,
    // An enrolled machine proved its own identity at enrolment; nobody
    // had to vouch for the row (asset-inventory spec §3).
    provenance: "agent" as const,
    description: null,
    primary_ip: null,
    owner: null,
    location: null,
    ...overrides,
  };
}

const listDevices = vi.fn();

function renderPage(options: {
  permissions?: string[];
  tokens?: () => Promise<unknown>;
  devices?: () => Promise<unknown>;
  /** `/me`'s `timezone` — the zone every date on this page is rendered in. */
  timezone?: string | null;
  /** `/me` itself failing, which is not the same thing as holding nothing. */
  meFails?: boolean;
  /** The URL to open the page at. The tab now lives in `?tab=`, so a test can
   *  arrive the way a stat tile's link arrives. */
  entry?: string;
} = {}) {
  /**
   * `GET /enrollment-tokens` HIDES REVOKED TOKENS, COUNTS THEM, AND PAGES, so
   * the mock has to do all three — a fake that ignored `include_revoked` or
   * served every row whatever the `limit` would let the page pass a test the
   * server would fail. Every `options.tokens` below returns the org's WHOLE
   * token set, exactly as the table does; the filtering, the counting and the
   * slicing happen here, where the server does them.
   *
   * `total` IS THE MATCHING ROWS, NOT THE PAGE, and `revoked_count` is the
   * org's revocations whatever page is being served. Getting either wrong here
   * would hide the defect these tests exist to catch.
   */
  const rows = options.tokens ?? (() => Promise.resolve([token()]));
  listEnrollmentTokens.mockImplementation(
    async (
      _api: unknown,
      params: {
        include_revoked?: boolean;
        unlimited_only?: boolean;
        limit?: number;
        offset?: number;
      } = {},
    ) => {
      const all = (await rows()) as EnrollmentTokenListItem[];
      // STILL REDEEMABLE AND UNBOUNDED, which is what the server counts: a
      // revoked or expired row is history, and a badge that counted history
      // would cry wolf on every deployment that ever issued one.
      const stillUnlimited = (row: EnrollmentTokenListItem) =>
        row.max_uses == null && !row.revoked && new Date(row.expires_at).getTime() > Date.now();
      let matching = params.include_revoked ? all : all.filter((row) => !row.revoked);
      if (params.unlimited_only) matching = matching.filter(stillUnlimited);
      const offset = params.offset ?? 0;
      const limit = params.limit ?? matching.length;
      return {
        items: matching.slice(offset, offset + limit),
        total: matching.length,
        revoked_count: all.filter((row) => row.revoked).length,
        // ORG-WIDE AND INDEPENDENT OF THE PAGE AND OF THE FILTER, exactly like
        // `revoked_count`: it is the number the callout advertises, and one
        // computed from the page would understate it on the deployment with
        // the most of them.
        unlimited_count: all.filter(stillUnlimited).length,
      } satisfies EnrollmentTokenPage;
    },
  );
  listDevices.mockImplementation(
    options.devices ?? (() => Promise.resolve({ items: [device()], total: 1 })),
  );
  mockUseApi.mockReturnValue({
    me: () =>
      options.meFails
        ? Promise.reject(new ApiError(503, ""))
        : Promise.resolve({
            user_id: "u",
            org_id: "o",
            email: "op@example.com",
            display_name: "Operator",
            permissions: options.permissions ?? ["devices.read", "enrollment.manage"],
            timezone: options.timezone ?? null,
          }),
    listDevices,
  } as unknown as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const ui: ReactElement = (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[options.entry ?? "/enrollment"]}>
        <EnrollmentPage />
      </MemoryRouter>
    </QueryClientProvider>
  );
  return render(ui);
}

/** The three deliberate steps, as a helper, so each test can assert about the
 *  step it is actually about. */
async function issueToken(fields: { name?: string; hours?: string; maxUses?: string } = {}) {
  await userEvent.click(screen.getByRole("button", { name: "Create token" }));
  if (fields.name !== undefined) {
    await userEvent.type(screen.getByLabelText("Token name"), fields.name);
  }
  if (fields.hours !== undefined) {
    const hours = screen.getByLabelText("Lifetime in hours");
    await userEvent.clear(hours);
    await userEvent.type(hours, fields.hours);
  }
  if (fields.maxUses !== undefined) {
    const uses = screen.getByLabelText("Maximum uses");
    await userEvent.clear(uses);
    await userEvent.type(uses, fields.maxUses);
  }
  await userEvent.click(screen.getByRole("button", { name: "Issue token…" }));
}

/** The clipboard jsdom does not provide. Returned so a test can make it fail. */
function stubClipboard(writeText: () => Promise<void>) {
  Object.defineProperty(navigator, "clipboard", {
    value: { writeText },
    configurable: true,
  });
}

beforeEach(() => {
  mockUseApi.mockReset();
  listDevices.mockReset();
  listEnrollmentTokens.mockReset();
  createEnrollmentToken.mockReset();
  revokeEnrollmentToken.mockReset();
  revokeAgent.mockReset();
});

afterEach(() => {
  Reflect.deleteProperty(navigator, "clipboard");
});

describe("EnrollmentPage", () => {
  it("says it is loading rather than showing zero tokens", async () => {
    renderPage({ tokens: () => new Promise<never>(() => {}) });
    expect(await screen.findByText("Loading…")).toBeInTheDocument();
    expect(screen.queryByText(/No enrollment tokens have been issued/i)).not.toBeInTheDocument();
  });

  it("says the token request FAILED rather than showing zero tokens", async () => {
    // A broken enrollment API and an org that has issued nothing must not look
    // the same: one of them means no new device can be enrolled right now.
    renderPage({ tokens: () => Promise.reject(new Error("boom")) });
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText(/No enrollment tokens have been issued/i)).not.toBeInTheDocument();
  });

  it("says there are no tokens when there are genuinely none", async () => {
    renderPage({ tokens: () => Promise.resolve([]) });
    expect(await screen.findByText("No enrollment tokens have been issued.")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("derives token state from the row rather than from a status field", async () => {
    // The API serves no "state"; it serves `revoked`, `expires_at`, `use_count`
    // and `max_uses`. Every state below is computed from those.
    renderPage({
      tokens: () =>
        Promise.resolve([
          token(),
          token({ id: "2", name: "revoked one", revoked: true }),
          token({ id: "3", name: "expired one", expires_at: PAST }),
          token({ id: "4", name: "used up", use_count: 50, max_uses: 50 }),
        ]),
    });
    expect(await screen.findByText("Active")).toBeInTheDocument();
    expect(screen.getByText("Expired")).toBeInTheDocument();
    expect(screen.getByText("Fully used")).toBeInTheDocument();
    // `revoked` is the one state hidden by default, so this asks for it. The
    // other three are NOT hidden: expired and fully-used tokens are inert but
    // were never withdrawn by anybody, and the operator's complaint was about
    // revocations.
    expect(screen.queryByText("Revoked")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("checkbox", { name: /Show revoked tokens/ }));
    expect(await screen.findByText("Revoked")).toBeInTheDocument();
  });

  it("counts stat cards from the responses, not from the mock's figures", async () => {
    renderPage({
      devices: () =>
        Promise.resolve({
          items: [
            device(),
            device({ id: "2", hostname: "b", agent_status: "offline", cert_state: "expiring" }),
            device({ id: "3", hostname: "c", cert_state: "expired" }),
          ],
          total: 3,
        }),
      // DISTINCT names: two rows sharing one made `findByText` match both, and
      // a value constant across every fixture is untested by construction.
      tokens: () =>
        Promise.resolve([token(), token({ id: "2", name: "retired batch", revoked: true })]),
    });
    // The revoked token is HIDDEN, and the count beside the toggle is what says
    // it exists. The stat tile counts ACTIVE tokens, which never included it.
    await screen.findByText("Bogota HQ workstations");
    expect(screen.queryByText("retired batch")).not.toBeInTheDocument();
    expect(screen.getByText("Show revoked tokens (1 hidden)")).toBeInTheDocument();
    const values = [...document.querySelectorAll(".stat-card")].map(
      (card) => card.querySelector(".stat-value")?.textContent,
    );
    // 3 enrolled; 2 online (the offline one is the `expiring` row); 2 needing
    // certificate attention (expiring + expired, never `renewing`); 1 active
    // token of the 2 issued.
    expect(values).toEqual(["3", "2", "2", "1"]);
    // The prototype's own cards read 286 / 272 / 94% / 4 / 2.
    expect(values).not.toContain("286");
    expect(values).not.toContain("94%");
  });

  /* ------------------------------------------------------- issuing a token */

  it('"Create token" reveals the form and writes nothing', async () => {
    // THE AUDITED DEFECT, [HIGH]: this click minted a live, unlimited-use
    // bootstrap credential, and only then showed the fields that would have
    // configured it.
    renderPage();
    await screen.findByText("Bogota HQ workstations");
    expect(screen.queryByLabelText("Token name")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Create token" }));

    expect(screen.getByLabelText("Token name")).toBeInTheDocument();
    expect(screen.getByLabelText("Lifetime in hours")).toBeInTheDocument();
    expect(screen.getByLabelText("Maximum uses")).toBeInTheDocument();
    expect(createEnrollmentToken).not.toHaveBeenCalled();
  });

  // DESIGN REVIEW M6. Closed, "Issue an enrollment token" was a heading, a
  // subtitle and one line of prose whose entire content was "use the button
  // above" -- a card with nothing in it and an instruction to look somewhere
  // else. `EmptyState` is the console's one shape for "there is nothing
  // here" (/software's "No software selected", /bcp/report's "No period
  // chosen", /security's "Select an event"), and this panel now wears it.
  //
  // AND EXACTLY ONE "Create token" ON THE PAGE. An `action` on the empty
  // state would be a second control with the same accessible name as the
  // page head's, sixty pixels below it, calling the same toggle. Thirteen
  // tests in this file ask for that button by name and would find two.
  it("gives the closed issue panel the console's empty state, not a card with a sentence in it", async () => {
    renderPage();
    await screen.findByText("Bogota HQ workstations");

    const empty = document.querySelector(".empty") as HTMLElement;
    expect(empty).not.toBeNull();
    // The glyph, the noun phrase, and WHY it is empty -- EmptyState's three
    // required parts.
    expect(empty.querySelector(".empty-icon")).not.toBeNull();
    expect(within(empty).getByRole("heading", { level: 3 })).toHaveTextContent(
      "No token is being issued",
    );
    expect(empty).toHaveTextContent(/Choose Create token to set a name/);

    expect(screen.getAllByRole("button", { name: "Create token" })).toHaveLength(1);
  });

  it("refuses to issue an unnamed token, and says which field is missing", async () => {
    // Every token in the audited deployment read "(unnamed)", so no operator
    // could tell what any of them was for.
    renderPage();
    await screen.findByText("Bogota HQ workstations");
    await issueToken();

    expect(await screen.findByText(/Token name is required/)).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(createEnrollmentToken).not.toHaveBeenCalled();
  });

  it("asks for confirmation, naming the token and its limit, before issuing anything", async () => {
    createEnrollmentToken.mockResolvedValue({
      id: "new",
      token: "ENR-SECRET-VALUE",
      name: "Bogota rollout",
      expires_at: FUTURE,
      max_uses: 1,
    });
    renderPage();
    await screen.findByText("Bogota HQ workstations");
    await issueToken({ name: "Bogota rollout" });

    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("Bogota rollout");
    expect(dialog).toHaveTextContent(/valid for 72 hours/);
    expect(dialog).toHaveTextContent(/redeemed once/);
    expect(dialog).toHaveTextContent(/shown once/);
    // Still nothing sent: the dialog is the ask, not the act.
    expect(createEnrollmentToken).not.toHaveBeenCalled();

    await userEvent.click(within(dialog).getByRole("button", { name: "Issue token" }));
    expect(createEnrollmentToken).toHaveBeenCalledWith(expect.anything(), {
      name: "Bogota rollout",
      expires_in_hours: 72,
      max_uses: 1,
    });
  });

  it("issues nothing when the confirmation is cancelled", async () => {
    renderPage();
    await screen.findByText("Bogota HQ workstations");
    await issueToken({ name: "Bogota rollout" });
    const dialog = await screen.findByRole("dialog");

    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));

    expect(createEnrollmentToken).not.toHaveBeenCalled();
    // The form is still filled in, so cancelling costs nothing but the click.
    expect(screen.getByLabelText("Token name")).toHaveValue("Bogota rollout");
  });

  it("defaults to a single use rather than to unlimited", async () => {
    // `max_uses: null` is what the API reads as unlimited, and a blank field is
    // what sent it. Every token in the audited deployment was `0 / ∞`.
    createEnrollmentToken.mockResolvedValue({
      id: "new",
      token: "ENR-SECRET-VALUE",
      name: "one machine",
      expires_at: FUTURE,
      max_uses: 1,
    });
    renderPage();
    await screen.findByText("Bogota HQ workstations");
    await issueToken({ name: "one machine" });
    await userEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Issue token" }),
    );

    const body = createEnrollmentToken.mock.calls[0]?.[1] as { max_uses: number | null };
    expect(body.max_uses).toBe(1);
    expect(body.max_uses).not.toBeNull();
  });

  it("offers no way to mint an unlimited token at all", async () => {
    // THE OPT-IN IS GONE WITH THE SERVER'S ABILITY TO HONOUR IT. The audit fix
    // changed the DEFAULT and left the capability: "Allow unlimited uses" was
    // one click, and the result was a credential that enrols machines into
    // this organization without bound until it expires.
    // `POST /enrollment-tokens` now requires `max_uses` and caps it, so a
    // checkbox here could only ever produce a 422.
    renderPage();
    await screen.findByText("Bogota HQ workstations");
    await userEvent.click(screen.getByRole("button", { name: "Create token" }));

    expect(
      screen.queryByRole("checkbox", { name: /unlimited/i }),
    ).not.toBeInTheDocument();
    // Non-vacuity: the form IS open and its use field is there to be found.
    expect(screen.getByLabelText("Maximum uses")).toBeInTheDocument();
  });

  it("refuses a use count above the server's ceiling, before the round trip", async () => {
    // `>= 1` ALONE WAS NEVER A BOUND. With the unlimited box gone, an
    // unbounded token spelled `2147483647` would be the same credential with
    // extra digits; the server 422s above `MAX_TOKEN_USES` and this says so
    // first.
    renderPage();
    await screen.findByText("Bogota HQ workstations");
    await issueToken({ name: "everything", maxUses: "1000000" });

    expect(
      await screen.findByText(/Maximum uses must be a whole number between 1 and 1000/),
    ).toBeInTheDocument();
    expect(createEnrollmentToken).not.toHaveBeenCalled();
  });

  it("rejects a lifetime the server would reject, before the round trip", async () => {
    renderPage();
    await screen.findByText("Bogota HQ workstations");
    await issueToken({ name: "too long", hours: "5000" });

    expect(await screen.findByText(/Lifetime must be a whole number of hours/)).toBeInTheDocument();
    expect(createEnrollmentToken).not.toHaveBeenCalled();
  });

  it("marks an unlimited token in the table in words, not only as a glyph", async () => {
    renderPage({ tokens: () => Promise.resolve([token({ max_uses: null })]) });
    const row = (await screen.findByText("Bogota HQ workstations")).closest("tr") as HTMLElement;
    expect(row).toHaveTextContent("unlimited");
    // Non-vacuity: a bounded token is not labelled unlimited.
    expect(within(row).queryByText("∞")).not.toBeInTheDocument();
  });

  it("does not call a bounded token unlimited", async () => {
    renderPage();
    const row = (await screen.findByText("Bogota HQ workstations")).closest("tr") as HTMLElement;
    expect(row).not.toHaveTextContent("unlimited");
    expect(row).toHaveTextContent("4 / 50");
  });

  /* ------------------------------------------------- the one-time plaintext */

  it("stacks the reveal so the token chip does not paint over the instruction above it", async () => {
    // WHAT THE OPERATOR SAW: the black token chip drawn ON TOP of "The control
    // plane stores only a hash of it and cannot show it again", with the two
    // buttons then drawn on top of the chip. Reported as the reveal "covering"
    // the notice.
    //
    // WHY. The reveal was a `<br />`-separated INLINE flow inside `.notice`,
    // and `.code` is `padding: 12px` on a `<code>` element. Vertical padding on
    // an inline box does not grow the line box it sits in — CSS 2.1 §10.6.1,
    // and the reason `overflow-x: auto` in that same rule is also inert there.
    // So a 12px-padded chip on a 20px line bled 12px into the line above and
    // 12px into the line below and painted over both.
    //
    // THE PIXELS ARE THE EVIDENCE (browser, 1440x900); jsdom lays nothing out.
    // What jsdom CAN see is the property that made the overlap possible: the
    // padded element's display. A block-level box grows to contain its own
    // padding, an inline one does not, so this assertion fails on exactly the
    // markup that produced the screenshot and passes on the fix.
    createEnrollmentToken.mockResolvedValue({
      id: "new",
      token: "ENR-SECRET-VALUE",
      name: "Bogota rollout",
      expires_at: FUTURE,
      max_uses: 1,
    });
    renderPage();
    await screen.findByText("Bogota HQ workstations");
    await issueToken({ name: "Bogota rollout" });
    await userEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Issue token" }),
    );

    const chip = await screen.findByText("ENR-SECRET-VALUE");
    expect(chip).toHaveClass("code");
    expect(getComputedStyle(chip).display).toBe("block");

    // And the sentence it used to cover is still a sentence of its own, not a
    // fragment of one long inline run broken by `<br />`.
    const notice = chip.closest(".notice") as HTMLElement;
    expect(notice).not.toBeNull();
    expect(notice.querySelectorAll("br")).toHaveLength(0);
    expect(notice).toHaveTextContent("Copy this token now. It is shown once.");
    expect(notice).toHaveTextContent(
      "This server keeps only a fingerprint of it and cannot show it again.",
    );
  });

  it("shows the issued token once, says so, and lets it be dropped once copied", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    stubClipboard(writeText);
    createEnrollmentToken.mockResolvedValue({
      id: "new",
      token: "ENR-SECRET-VALUE",
      name: "Bogota rollout",
      expires_at: FUTURE,
      max_uses: 1,
    });
    renderPage();
    await screen.findByText("Bogota HQ workstations");
    await issueToken({ name: "Bogota rollout" });
    await userEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Issue token" }),
    );

    expect(await screen.findByText("ENR-SECRET-VALUE")).toBeInTheDocument();
    expect(screen.getByText(/shown once/i)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /Copy token/ }));
    expect(writeText).toHaveBeenCalledWith("ENR-SECRET-VALUE");
    expect(await screen.findByRole("status")).toHaveTextContent(/Copied/i);

    // Copied, so dismissing needs no second ask — the confirmation belongs on
    // the path where the only copy would be destroyed.
    await userEvent.click(screen.getByRole("button", { name: /I have copied it/ }));
    expect(screen.queryByText("ENR-SECRET-VALUE")).not.toBeInTheDocument();
  });

  it("asks before destroying a token the operator never copied", async () => {
    createEnrollmentToken.mockResolvedValue({
      id: "new",
      token: "ENR-SECRET-VALUE",
      name: "Bogota rollout",
      expires_at: FUTURE,
      max_uses: 1,
    });
    renderPage();
    await screen.findByText("Bogota HQ workstations");
    await issueToken({ name: "Bogota rollout" });
    await userEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Issue token" }),
    );
    await screen.findByText("ENR-SECRET-VALUE");

    await userEvent.click(screen.getByRole("button", { name: /I have copied it/ }));

    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("Bogota rollout");
    expect(dialog).toHaveTextContent(/only copy/);
    // Backing out keeps the plaintext on screen.
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.getByText("ENR-SECRET-VALUE")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /I have copied it/ }));
    await userEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", {
        name: "Dismiss without copying",
      }),
    );
    expect(screen.queryByText("ENR-SECRET-VALUE")).not.toBeInTheDocument();
  });

  it("says the copy FAILED rather than letting the operator believe it worked", async () => {
    // `navigator.clipboard` is undefined outside a secure context and rejects
    // when permission is denied. A silent failure here is the worst shape this
    // screen has: the operator dismisses the only copy of a live credential.
    stubClipboard(() => Promise.reject(new Error("denied")));
    createEnrollmentToken.mockResolvedValue({
      id: "new",
      token: "ENR-SECRET-VALUE",
      name: "Bogota rollout",
      expires_at: FUTURE,
      max_uses: 1,
    });
    renderPage();
    await screen.findByText("Bogota HQ workstations");
    await issueToken({ name: "Bogota rollout" });
    await userEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Issue token" }),
    );
    await screen.findByText("ENR-SECRET-VALUE");

    await userEvent.click(screen.getByRole("button", { name: /Copy token/ }));

    expect(await screen.findByText(/refused clipboard access/i)).toBeInTheDocument();
    expect(screen.queryByText(/Copied to the clipboard/i)).not.toBeInTheDocument();
    // And the dismiss still asks, because nothing was copied.
    await userEvent.click(screen.getByRole("button", { name: /I have copied it/ }));
    expect(await screen.findByRole("dialog")).toHaveTextContent(/only copy/);
  });

  /* ------------------------------------------------------------- revocation */

  it("names the token in a confirmation before revoking it", async () => {
    // Revoking an agent drops its live connection and forces a re-enrollment;
    // revoking a token invalidates a bootstrap credential in flight. Neither
    // should be one stray click away, and neither should be confirmed by a
    // control that does not say WHICH row it is about.
    renderPage();
    await screen.findByText("Bogota HQ workstations");
    await userEvent.click(screen.getByRole("button", { name: "Revoke" }));
    expect(revokeEnrollmentToken).not.toHaveBeenCalled();

    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("Bogota HQ workstations");
    expect(dialog).toHaveTextContent(/stops working immediately/);
    await userEvent.click(within(dialog).getByRole("button", { name: "Revoke token" }));
    // `(api, tokenId)` since the call was folded onto the shared ApiClient.
    expect(revokeEnrollmentToken).toHaveBeenCalledWith(expect.anything(), token().id);
  });

  it("revokes nothing when the confirmation is cancelled", async () => {
    renderPage();
    await screen.findByText("Bogota HQ workstations");
    await userEvent.click(screen.getByRole("button", { name: "Revoke" }));
    await userEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Cancel" }),
    );
    expect(revokeEnrollmentToken).not.toHaveBeenCalled();
  });

  it("names the DEVICE in the agent-certificate confirmation", async () => {
    // The mistake a confirmation exists to catch is having the wrong row. The
    // inline "Confirm revoke" pair this replaced could not say whose
    // certificate was about to be destroyed.
    renderPage();
    await screen.findByText("Bogota HQ workstations");
    await userEvent.click(screen.getByRole("tab", { name: /Certificates/ }));
    await screen.findByText("ws-alpha");
    await userEvent.click(screen.getByRole("button", { name: /Revoke agent/ }));

    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("ws-alpha");
    expect(dialog).toHaveTextContent(/enrolled again with a new token/);
    expect(revokeAgent).not.toHaveBeenCalled();
    await userEvent.click(within(dialog).getByRole("button", { name: "Revoke certificate" }));
    expect(revokeAgent).toHaveBeenCalledWith(expect.anything(), device().id);
  });

  it("never opens a nameless confirmation when the hostname is null", async () => {
    // `hostname` is nullable on the wire. A confirmation whose subject is blank
    // is exactly the failure the subject exists to prevent -- the operator
    // cannot tell which certificate they are about to destroy.
    renderPage({
      devices: () =>
        Promise.resolve({ items: [device({ hostname: null, fqdn: "ws-beta.corp.local" })], total: 1 }),
    });
    await screen.findByText("Bogota HQ workstations");
    await userEvent.click(screen.getByRole("tab", { name: /Certificates/ }));
    await userEvent.click(await screen.findByRole("button", { name: /Revoke agent/ }));

    const subject = (await screen.findByRole("dialog")).querySelector(".confirm-subject");
    expect(subject?.textContent?.trim()).toBe("ws-beta.corp.local");
  });

  it("gates the two tabs on their own permission, and does not fetch what it hides", async () => {
    renderPage({ permissions: ["devices.read"] });
    expect(await screen.findByText("ws-alpha")).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: /Enrollment tokens/ })).not.toBeInTheDocument();
    expect(listEnrollmentTokens).not.toHaveBeenCalled();
    // Non-vacuity: with the code, the tab and the request are both there.
    expect(screen.getByRole("tab", { name: /Certificates/ })).toBeInTheDocument();
    expect(listDevices).toHaveBeenCalled();
  });

  it("says the permission LOOKUP failed rather than looking like a denial", async () => {
    // Permissions default to `[]` when `/me` fails, which hides both gated tabs
    // -- leaving a page indistinguishable from the one a read-only technician
    // is meant to see. "Ask an admin for access" and "retry" are different
    // next actions.
    renderPage({ meFails: true });

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/unavailable right now/i);
    expect(alert).toHaveTextContent(/not a statement that you lack them/i);
    expect(screen.queryByRole("tab", { name: /Enrollment tokens/ })).not.toBeInTheDocument();
  });

  it("offers no revoke action to an operator who cannot revoke", async () => {
    renderPage({ permissions: ["devices.read"] });
    await screen.findByText("ws-alpha");
    expect(screen.queryByRole("button", { name: /Revoke agent/ })).not.toBeInTheDocument();
  });

  /**
   * DATES (cycle 5) AND FAILURES (cycle 4).
   *
   * Both cycles land hardest on this page, because both of its numbers are
   * about a credential: WHEN a token dies, and WHETHER a revoke took. A token
   * expiry read five hours early retires a credential that is still live; a
   * revoke that failed silently leaves one alive that the operator believes is
   * dead. The second is the worse direction and it had no test at all.
   */
  it("renders token expiry in the signed-in user's zone", async () => {
    renderPage({
      timezone: "Asia/Tokyo",
      tokens: () => Promise.resolve([token({ expires_at: "2026-08-16T23:30:00Z" })]),
    });
    // 23:30Z is 08:30 the NEXT DAY in Tokyo.
    expect(await screen.findByText("17 Aug 2026, 08:30")).toBeInTheDocument();
    expect(screen.queryByText(/8\/16\/2026/)).not.toBeInTheDocument();
  });

  it("renders a certificate expiry as a date in that same zone", async () => {
    renderPage({
      timezone: "Asia/Tokyo",
      devices: () =>
        Promise.resolve({
          items: [device({ cert_not_after: "2027-03-11T20:00:00Z" })],
          total: 1,
        }),
    });
    await userEvent.click(await screen.findByRole("tab", { name: /Certificates/ }));
    // 20:00Z on the 11th is the 12th in Tokyo. A certificate expiry off by a
    // whole day is the same defect as the five-hour shift, with a longer fuse.
    expect(await screen.findByText("12 Mar 2027")).toBeInTheDocument();
  });

  it("renders an absent certificate expiry as an em dash, never Invalid Date", async () => {
    renderPage({
      devices: () =>
        Promise.resolve({
          items: [device({ cert_not_after: null, cert_state: null, last_seen_at: null })],
          total: 1,
        }),
    });
    await userEvent.click(await screen.findByRole("tab", { name: /Certificates/ }));
    const row = (await screen.findByText("ws-alpha")).closest("tr") as HTMLElement;
    // THE CELLS, not the row. `row.textContent` is satisfied by the em dash in
    // any OTHER column, which is how the first version of this test passed
    // against `new Date(null)` -- an expression that is not "Invalid Date" at
    // all but the EPOCH: a certificate that reads as having expired in 1970.
    const cells = [...row.querySelectorAll("td")].map((cell) => cell.textContent?.trim());
    expect(cells[2]).toBe("—");
    expect(cells[3]).toBe("—");
    expect(row.textContent).not.toMatch(/1970|Invalid Date/);
  });

  it("says why a token was not created, in the server's own words", async () => {
    // It used to say "The token was not created" for every refusal alike: a
    // 403, a duplicate name and a rejected `max_uses` are three different
    // things to do next.
    createEnrollmentToken.mockRejectedValue(
      new ApiError(422, JSON.stringify({
        detail: [{ type: "greater_than", loc: ["body", "max_uses"], msg: "Input should be greater than 0" }],
      })),
    );
    renderPage();
    await screen.findByText("Bogota HQ workstations");
    await issueToken({ name: "Bogota rollout" });
    await userEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Issue token" }),
    );

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/Max uses: Input should be greater than 0/);
    expect(alert).toHaveTextContent(/No token was issued/);
    // The raw payload never reaches the screen.
    expect(alert.textContent).not.toContain("greater_than");
    expect(alert.textContent).not.toContain('"loc"');
  });

  it("says a token revoke FAILED rather than leaving the row looking handled", async () => {
    // THE SWALLOWED DESTRUCTIVE FAILURE. `revokeEnrollmentToken` had no
    // `onError` at all, so a bodyless 503 -- what a proxy in front of this API
    // returns -- moved nothing on screen and said nothing. The operator walks
    // away believing a bootstrap credential is dead.
    revokeEnrollmentToken.mockRejectedValue(new ApiError(503, ""));
    renderPage();
    await screen.findByText("Bogota HQ workstations");
    await userEvent.click(screen.getByRole("button", { name: "Revoke" }));
    await userEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Revoke token" }),
    );

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/unavailable right now/i);
    expect(alert).toHaveTextContent(/NOT revoked/);
    // The row is still there to try again on, and the token still reads active.
    expect(screen.getByText("Active")).toBeInTheDocument();
  });

  it("clears that failure when the operator backs out of a fresh confirmation", async () => {
    revokeEnrollmentToken.mockRejectedValue(new ApiError(503, ""));
    renderPage();
    await screen.findByText("Bogota HQ workstations");
    await userEvent.click(screen.getByRole("button", { name: "Revoke" }));
    await userEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Revoke token" }),
    );
    await screen.findByRole("alert");

    await userEvent.click(screen.getByRole("button", { name: "Revoke" }));
    await userEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Cancel" }),
    );
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("says an agent revoke FAILED rather than leaving the certificate looking revoked", async () => {
    revokeAgent.mockRejectedValue(new ApiError(403, ""));
    renderPage();
    await screen.findByText("Bogota HQ workstations");
    await userEvent.click(screen.getByRole("tab", { name: /Certificates/ }));
    await screen.findByText("ws-alpha");
    await userEvent.click(screen.getByRole("button", { name: /Revoke agent/ }));
    await userEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Revoke certificate" }),
    );

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/do not have permission/i);
    expect(alert).toHaveTextContent(/NOT revoked/);
  });

  it("shows no column or panel the API cannot fill", async () => {
    renderPage();
    await screen.findByText("Bogota HQ workstations");
    await userEvent.click(screen.getByRole("tab", { name: /Certificates/ }));
    await screen.findByText("ws-alpha");
    const head = document.querySelector("thead") as HTMLElement;
    // Serial, thumbprint and issuer are in the design and in no response.
    expect(head.textContent).not.toMatch(/Serial/);
    expect(head.textContent).not.toMatch(/Issuer/);
    // Non-vacuity: the columns that DO have data are present.
    expect(head.textContent).toMatch(/Certificate/);
    expect(head.textContent).toMatch(/Expires/);
    // Agent versions, release rings and the idle-budget panel are absent.
    expect(screen.queryByText(/release ring/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Idle budget/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/1\.8\.4/)).not.toBeInTheDocument();
  });
  /* ------------------------------------------ no dead tiles (visual cycle) */

  /**
   * "All tiles and all numbers or kpis need to be clickable and direct the
   * user to the specific menus, i dont want dead tiles everywhere" — the
   * operator, verbatim. `StatCard` requires `to`, so the compiler catches an
   * omitted destination; it cannot catch a destination that goes nowhere
   * useful, and that is what these three assert.
   */
  it("renders every stat tile as a link with a real destination", async () => {
    renderPage();
    await screen.findByText("Bogota HQ workstations");

    const tiles = [...document.querySelectorAll(".stat-card")];
    expect(tiles).toHaveLength(4);

    for (const tile of tiles) {
      // AN ANCHOR, not a div with onClick: keyboard reachable, middle-
      // clickable into a new tab, and pasteable into an incident channel.
      expect(tile.tagName).toBe("A");
      const href = tile.getAttribute("href");
      expect(href).toBeTruthy();
      expect(href).not.toBe("#");
    }

    // THE EXACT DESTINATIONS, PINNED — including the filters. A tile that
    // silently loses `?state=certs` opens three hundred healthy devices when
    // the operator asked for the two that are expiring, and no assertion of
    // the form "it has an href" can see that happen.
    expect(tiles.map((tile) => tile.getAttribute("href"))).toEqual([
      "/devices",
      "/devices?state=online",
      "/devices?state=certs",
      "/enrollment?tab=tokens",
    ]);
  });

  it("honours the ?tab= the token tile links to, so that href is not decorative", async () => {
    // NON-VACUITY for the row above. `/enrollment?tab=tokens` is only a real
    // destination if this page reads `?tab=`; before this cycle the tab lived
    // in component state and that URL would have changed nothing on screen —
    // a dead tile wearing an href. Certificates is used rather than tokens
    // because tokens is the DEFAULT tab, so landing on it proves nothing.
    renderPage({ entry: "/enrollment?tab=certificates" });

    expect(await screen.findByRole("tab", { name: /Certificates/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByRole("tab", { name: /Enrollment tokens/ })).toHaveAttribute(
      "aria-selected",
      "false",
    );
  });

  it("says WHICH zero each tile is reading", async () => {
    renderPage({
      devices: () => Promise.resolve({ items: [], total: 0 }),
      tokens: () => Promise.resolve([]),
    });
    await screen.findByText("No enrollment tokens have been issued.");

    // "0 certificates expiring" and "no certificate state was ever recorded"
    // are the same pixel otherwise; the audit found nine such tiles on one
    // page where nothing had been measured at all.
    expect(screen.getByText("No endpoint has enrolled")).toBeInTheDocument();
    expect(screen.getByText("No agent is connected")).toBeInTheDocument();
    expect(screen.getByText("Nothing expiring or expired")).toBeInTheDocument();
    expect(screen.getByText("No token is live")).toBeInTheDocument();

    // AND the zero drops the alarm treatment: "0 certificate attention"
    // wearing a saturated gold fill reads as an emergency, not a clean bill.
    const certs = [...document.querySelectorAll(".stat-label")].find(
      (node) => node.textContent === "Certificate attention",
    ) as HTMLElement;
    expect((certs.closest(".stat-card") as HTMLElement).className).not.toContain("urgent");
  });
  /* -------------------------------------- revoked entries: hidden, not gone */

  /**
   * The operator's report was "revoked certificates are still present on the
   * certificates page, there should be an option to remove them". These pin
   * the resolution and, as importantly, its limit: revoked entries leave the
   * default view and NOTHING on this page deletes one. A revocation is written
   * to the hash-chained audit log, and for an agent certificate the `agents`
   * row is the only mapping from a serial in the CA's CRL back to a machine.
   */

  it("does not ask the server for revoked tokens until the operator does", async () => {
    // SERVER-SIDE, not a client-side hide. `GET /device-groups?include_archived`
    // set this convention; a filter the browser applies to rows it already
    // downloaded leaves the dead credentials on the wire forever.
    renderPage({
      tokens: () => Promise.resolve([token(), token({ id: "2", name: "withdrawn", revoked: true })]),
    });
    await screen.findByText("Bogota HQ workstations");
    expect(listEnrollmentTokens).toHaveBeenCalledWith(expect.anything(), {
      include_revoked: false,
      unlimited_only: false,
      limit: ENROLLMENT_TOKENS_PAGE_SIZE,
      offset: 0,
    });

    await userEvent.click(screen.getByRole("checkbox", { name: /Show revoked tokens/ }));
    expect(await screen.findByText("withdrawn")).toBeInTheDocument();
    expect(listEnrollmentTokens).toHaveBeenCalledWith(expect.anything(), {
      include_revoked: true,
      unlimited_only: false,
      limit: ENROLLMENT_TOKENS_PAGE_SIZE,
      offset: 0,
    });
    expect(screen.getByText("Show revoked tokens (1 shown)")).toBeInTheDocument();
  });

  it("asks for one page of tokens, not every token the org ever issued", async () => {
    // THE DEFECT. `GET /enrollment-tokens` was unbounded and tokens only
    // accumulate — one per device onboarded, one per provisioning run, and
    // none ever deleted. On the one screen an operator sits on while
    // onboarding a fleet, that response grows until it times out.
    const many = Array.from({ length: ENROLLMENT_TOKENS_PAGE_SIZE + 3 }, (_unused, index) =>
      token({ id: `t-${index}`, name: `token-${index}` }),
    );
    renderPage({ tokens: () => Promise.resolve(many) });

    await screen.findByText("token-0");
    expect(listEnrollmentTokens).toHaveBeenCalledWith(expect.anything(), {
      include_revoked: false,
      unlimited_only: false,
      limit: ENROLLMENT_TOKENS_PAGE_SIZE,
      offset: 0,
    });
    // The page past the cap is genuinely absent, not merely unrendered.
    expect(screen.queryByText(`token-${ENROLLMENT_TOKENS_PAGE_SIZE}`)).not.toBeInTheDocument();
  });

  it("pages through the tokens rather than calling page one the list", async () => {
    // WIRED, not merely available. A console that asks for a bounded page and
    // then offers no way to reach the second one has replaced a slow, complete
    // list with a fast, silently truncated one.
    const many = Array.from({ length: ENROLLMENT_TOKENS_PAGE_SIZE + 3 }, (_unused, index) =>
      token({ id: `t-${index}`, name: `token-${index}` }),
    );
    renderPage({ tokens: () => Promise.resolve(many) });

    await screen.findByText("token-0");
    // The count is stated, and it is the ORG's count rather than the page's:
    // an operator has to be able to tell a short page from the end of a list.
    expect(
      screen.getByText(`Page 1 of 2, ${ENROLLMENT_TOKENS_PAGE_SIZE + 3} tokens`),
    ).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Next tokens" }));
    expect(
      await screen.findByText(`token-${ENROLLMENT_TOKENS_PAGE_SIZE}`),
    ).toBeInTheDocument();
    expect(screen.queryByText("token-0")).not.toBeInTheDocument();
    expect(listEnrollmentTokens).toHaveBeenCalledWith(expect.anything(), {
      include_revoked: false,
      unlimited_only: false,
      limit: ENROLLMENT_TOKENS_PAGE_SIZE,
      offset: ENROLLMENT_TOKENS_PAGE_SIZE,
    });

    await userEvent.click(screen.getByRole("button", { name: "Previous tokens" }));
    expect(await screen.findByText("token-0")).toBeInTheDocument();
  });

  it("offers no pager when every token fits on one page", async () => {
    renderPage({ tokens: () => Promise.resolve([token()]) });
    await screen.findByText("Bogota HQ workstations");
    expect(screen.queryByRole("button", { name: "Next tokens" })).not.toBeInTheDocument();
  });

  it("goes back to the first page when the operator reveals revoked tokens", async () => {
    // The list the offset indexes into CHANGES when the filter does, so page
    // three of the live tokens is not page three of all of them. Staying put
    // would land the operator somewhere neither they nor the page chose.
    const many = Array.from({ length: ENROLLMENT_TOKENS_PAGE_SIZE + 3 }, (_unused, index) =>
      token({ id: `t-${index}`, name: `token-${index}` }),
    );
    renderPage({ tokens: () => Promise.resolve(many) });

    await screen.findByText("token-0");
    await userEvent.click(screen.getByRole("button", { name: "Next tokens" }));
    await screen.findByText(`token-${ENROLLMENT_TOKENS_PAGE_SIZE}`);

    await userEvent.click(screen.getByRole("checkbox", { name: /Show revoked tokens/ }));
    expect(await screen.findByText("token-0")).toBeInTheDocument();
    expect(listEnrollmentTokens).toHaveBeenCalledWith(expect.anything(), {
      include_revoked: true,
      unlimited_only: false,
      limit: ENROLLMENT_TOKENS_PAGE_SIZE,
      offset: 0,
    });
  });

  it("does not strand the operator on a page that ran off the end", async () => {
    // A revoke shrinks the default list under their feet. Without a clamp the
    // table renders empty under an empty-state that says no token was ever
    // issued — a claim about the organization made from an overshot offset.
    const many = Array.from({ length: ENROLLMENT_TOKENS_PAGE_SIZE + 1 }, (_unused, index) =>
      token({ id: `t-${index}`, name: `token-${index}` }),
    );
    let live = many;
    revokeEnrollmentToken.mockImplementation(async (_api: unknown, id: string) => {
      live = live.filter((row) => row.id !== id);
      return { revoked: true };
    });
    renderPage({ tokens: () => Promise.resolve(live) });

    await screen.findByText("token-0");
    await userEvent.click(screen.getByRole("button", { name: "Next tokens" }));
    const last = await screen.findByText(`token-${ENROLLMENT_TOKENS_PAGE_SIZE}`);
    expect(last).toBeInTheDocument();

    const row = last.closest("tr") as HTMLElement;
    await userEvent.click(within(row).getByRole("button", { name: "Revoke" }));
    await userEvent.click(screen.getByRole("button", { name: "Revoke token" }));

    expect(await screen.findByText("token-0")).toBeInTheDocument();
    expect(
      screen.queryByText("No enrollment tokens have been issued."),
    ).not.toBeInTheDocument();
  });

  it("distinguishes 'no revoked tokens' from 'revoked tokens you cannot see'", async () => {
    renderPage({ tokens: () => Promise.resolve([token()]) });
    await screen.findByText("Bogota HQ workstations");
    expect(
      screen.getByText("Show revoked tokens (none have been revoked)"),
    ).toBeInTheDocument();
  });

  it("hides a revoked agent certificate and says how many it hid", async () => {
    // THE DEFECT THE OPERATOR SAW. `cert_state` is derived from
    // `cert_not_after` alone, so before `cert_revoked` a revoked certificate
    // rendered `ok` and kept its Revoke button: revoking a machine changed
    // nothing on this screen at all.
    renderPage({
      devices: () =>
        Promise.resolve({
          items: [
            device(),
            device({ id: "2", hostname: "ws-retired", cert_revoked: true }),
          ],
          total: 2,
        }),
    });
    await userEvent.click(await screen.findByRole("tab", { name: /Certificates/ }));
    expect(screen.getByText("ws-alpha")).toBeInTheDocument();
    expect(screen.queryByText("ws-retired")).not.toBeInTheDocument();
    expect(screen.getByText("Show revoked certificates (1 hidden)")).toBeInTheDocument();

    // And it stops saying "hidden" once it is not.
    await userEvent.click(screen.getByRole("checkbox", { name: /Show revoked certificates/ }));
    expect(await screen.findByText("ws-retired")).toBeInTheDocument();
    expect(screen.getByText("Show revoked certificates (1 shown)")).toBeInTheDocument();
  });

  it("shows a revoked certificate as revoked, and offers nothing to revoke", async () => {
    renderPage({
      devices: () =>
        Promise.resolve({
          items: [device({ id: "2", hostname: "ws-retired", cert_revoked: true })],
          total: 1,
        }),
    });
    await userEvent.click(await screen.findByRole("tab", { name: /Certificates/ }));
    await userEvent.click(screen.getByRole("checkbox", { name: /Show revoked certificates/ }));

    const row = (await screen.findByText("ws-retired")).closest("tr") as HTMLElement;
    // `ok` is what this row said before, and it was wrong rather than merely
    // unhelpful.
    expect(within(row).getByText("Revoked")).toBeInTheDocument();
    expect(within(row).queryByText("ok")).not.toBeInTheDocument();
    expect(within(row).queryByRole("button", { name: "Revoke agent" })).not.toBeInTheDocument();
  });

  it("does not let the search box resurrect a hidden revoked certificate", async () => {
    // The search reads `cert_state`, which for a revoked device still says
    // `ok`. Typing into it must not be a second, accidental way to unhide a
    // row the toggle is holding back -- one control decides that.
    renderPage({
      devices: () =>
        Promise.resolve({
          items: [device({ id: "2", hostname: "ws-retired", cert_revoked: true })],
          total: 1,
        }),
    });
    await userEvent.click(await screen.findByRole("tab", { name: /Certificates/ }));
    await userEvent.type(screen.getByLabelText("Search certificates"), "ws-retired");
    expect(screen.queryByText("ws-retired")).not.toBeInTheDocument();
    expect(await screen.findByText("No certificate matches that filter.")).toBeInTheDocument();
  });

  it("does not claim 'none have been revoked' before it has been told", async () => {
    // The count is a fact about a response. Beside a spinner or a failed
    // request it would be an assertion from a screen that has not been told --
    // the same defect as a stat tile rendering 0 for a read that never landed.
    renderPage({
      tokens: () => new Promise<never>(() => {}),
      devices: () => Promise.reject(new Error("boom")),
    });
    expect(await screen.findByText("Loading…")).toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: /Show revoked tokens/ })).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("tab", { name: /Certificates/ }));
    expect(
      screen.queryByRole("checkbox", { name: /Show revoked certificates/ }),
    ).not.toBeInTheDocument();
  });

  it("says WHICH filter emptied the certificate table", async () => {
    // "No certificate matches that filter" over an untouched search box sends
    // an operator hunting for something they never set.
    renderPage({
      devices: () =>
        Promise.resolve({
          items: [device({ cert_revoked: true })],
          total: 1,
        }),
    });
    await userEvent.click(await screen.findByRole("tab", { name: /Certificates/ }));
    expect(
      await screen.findByText(/Every issued certificate has been revoked/),
    ).toBeInTheDocument();
  });

  it("offers no way to delete a revoked token or certificate", async () => {
    // THE HALF OF THE REQUEST THAT WAS REFUSED, pinned so a later cycle has to
    // argue with it rather than quietly add a button. Deleting an `agents` row
    // orphans its serial in the CA's CRL, which lives outside this database;
    // deleting an `enrollment_tokens` row is a foreign-key violation the
    // moment a provisioning run points at it.
    renderPage({
      tokens: () => Promise.resolve([token({ revoked: true })]),
      devices: () =>
        Promise.resolve({
          items: [device({ cert_revoked: true })],
          total: 1,
        }),
    });
    await screen.findByRole("checkbox", { name: /Show revoked tokens/ });
    await userEvent.click(screen.getByRole("checkbox", { name: /Show revoked tokens/ }));
    await screen.findByText("Bogota HQ workstations");
    for (const label of [/delete/i, /remove/i, /purge/i, /forget/i]) {
      expect(screen.queryByRole("button", { name: label })).not.toBeInTheDocument();
    }

    await userEvent.click(screen.getByRole("tab", { name: /Certificates/ }));
    await userEvent.click(screen.getByRole("checkbox", { name: /Show revoked certificates/ }));
    await screen.findByText("ws-alpha");
    for (const label of [/delete/i, /remove/i, /purge/i, /forget/i]) {
      expect(screen.queryByRole("button", { name: label })).not.toBeInTheDocument();
    }
  });
  // ------------------------------------------------------------------- //
  // THE GRANDFATHERED CREDENTIALS. `max_uses` defaulted to null on
  // `POST /enrollment-tokens`, and null means unlimited, so a deployment can
  // be holding live tokens that enrol machines without bound. Nothing
  // rewrites those rows -- re-reading a stored null as a number would fail at
  // the endpoint as a 403 indistinguishable from a revocation -- so the only
  // thing that makes the number fall is a human revoking them, and this is
  // what puts one in front of the button.
  // ------------------------------------------------------------------- //

  it("says how many tokens have no use limit, and offers to list them", async () => {
    renderPage({
      tokens: () =>
        Promise.resolve([
          token({ id: "bounded", name: "bounded token" }),
          token({ id: "u1", name: "legacy fleet token", max_uses: null }),
          token({ id: "u2", name: "legacy lab token", max_uses: null }),
        ]),
    });
    const callout = await screen.findByRole("alert");
    expect(callout).toHaveTextContent("2 enrollment tokens have no use limit.");
    expect(callout).toHaveTextContent(/can enrol any number of machines/);

    await userEvent.click(within(callout).getByRole("button", { name: "Show them" }));
    expect(listEnrollmentTokens).toHaveBeenLastCalledWith(expect.anything(), {
      include_revoked: false,
      unlimited_only: true,
      limit: ENROLLMENT_TOKENS_PAGE_SIZE,
      offset: 0,
    });
    expect(await screen.findByText("legacy fleet token")).toBeInTheDocument();
    expect(screen.queryByText("bounded token")).not.toBeInTheDocument();
  });

  it("counts only the tokens that could still be redeemed", async () => {
    // A revoked or expired unlimited token is HISTORY. A callout that counted
    // it would cry wolf on every deployment that ever issued one, and an
    // operator who is warned about nothing stops reading the warning.
    renderPage({
      tokens: () =>
        Promise.resolve([
          token({ id: "u1", name: "live", max_uses: null }),
          token({ id: "u2", name: "expired", max_uses: null, expires_at: PAST }),
          token({ id: "u3", name: "revoked", max_uses: null, revoked: true }),
        ]),
    });
    const callout = await screen.findByRole("alert");
    expect(callout).toHaveTextContent("1 enrollment token has no use limit.");
  });

  it("says nothing at all when every token is bounded", async () => {
    // NON-VACUITY FOR THE TWO ABOVE, and a deliberate silence: "you hold 0
    // unlimited tokens" is a sentence that trains an operator to skip the box
    // on the day it says 3.
    renderPage();
    await screen.findByText("Bogota HQ workstations");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("checkbox", { name: /no use limit/ }),
    ).not.toBeInTheDocument();
  });
});
