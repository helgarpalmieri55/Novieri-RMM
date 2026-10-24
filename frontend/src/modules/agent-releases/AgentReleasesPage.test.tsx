/**
 * Platform → Agent packages.
 *
 * The three states first, as on every migrated page: an operator staring at an
 * empty list must be able to tell a broken backend from a genuinely empty
 * catalogue.
 *
 * Then two honesty requirements that are specific to this screen, and both come
 * from `agent/packaging/README.md` rather than from taste:
 *
 *   1. The Windows service and the macOS daemon **have never been executed by
 *      anybody on this project**. A Windows row that renders identically to the
 *      Linux one implies a parity that does not exist, and implies it most
 *      convincingly to whoever is deciding whether to trust it.
 *   2. A release with no uploaded artefact cannot produce an install command.
 *      Offering the button anyway produces a 409 at the click, which is a
 *      worse place to learn it than the row.
 *
 *   3. **"Install command" MINTS A CREDENTIAL.** It reads like a disclosure and
 *      it is not: the click calls `POST /agent/releases/{id}/install-command`,
 *      which issues a live single-use enrollment token. Auditor B filed the
 *      identical shape on /enrollment as [HIGH] — "Create token mints a live
 *      enrollment credential instantly, no form to fill, no confirmation" — so
 *      the confirmation here is pinned the same way.
 *
 * Mocks the auth-backed API client, mirroring `modules/devices/DevicesPage.test.tsx`.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../lib/api";
import type { AgentRelease, ApiClient } from "../../lib/api";
import { AgentReleasesPage } from "./AgentReleasesPage";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

function release(overrides: Partial<AgentRelease> = {}): AgentRelease {
  return {
    id: "11111111-1111-1111-1111-111111111111",
    version: "873d666",
    os: "linux",
    arch: "amd64",
    size_bytes: 18_874_368,
    sha256: "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90",
    signed: false,
    notes: "",
    published_at: "2026-08-18T09:00:00Z",
    has_artifact: true,
    ...overrides,
  };
}

function renderPage(
  listAgentReleases: () => Promise<unknown>,
  generateAgentInstallCommand?: () => Promise<unknown>,
  /** `/me`'s `timezone`; the page reads it only to render the token expiry. */
  timezone: string | null = null,
  /**
   * `/me`'s `permissions`. Empty is the REALISTIC default, not a convenience:
   * `agent.release.manage` moved to the platform-operator role in migration
   * 0070, so the everyday `org-admin` who reads this page does not hold it,
   * and the empty catalogue has to be honest with that person specifically.
   */
  permissions: string[] = [],
) {
  mockUseApi.mockReturnValue({
    me: () =>
      Promise.resolve({
        user_id: "u",
        org_id: "o",
        email: "op@example.com",
        display_name: "Operator",
        permissions,
        timezone,
      }),
    listAgentReleases,
    generateAgentInstallCommand,
  } as unknown as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const ui: ReactElement = (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <AgentReleasesPage />
      </MemoryRouter>
    </QueryClientProvider>
  );
  return render(ui);
}

/** Click through the ask and the act. Every test that wants the command needs
 *  both steps, and the point of the change is that one is not enough. */
async function generateCommand() {
  await userEvent.click(screen.getByRole("button", { name: /install command/i }));
  await userEvent.click(
    within(await screen.findByRole("dialog")).getByRole("button", { name: "Generate command" }),
  );
}

/** The clipboard jsdom does not provide. Returned so a test can make it fail. */
function stubClipboard(writeText: () => Promise<void>) {
  Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
}

beforeEach(() => {
  mockUseApi.mockReset();
});

afterEach(() => {
  Reflect.deleteProperty(navigator, "clipboard");
});

describe("AgentReleasesPage", () => {
  it("shows the newest version first and folds older versions away until asked", async () => {
    // 2026-09-24: sorted by platform, 1.4.0 and 1.5.0 interleaved row by row.
    renderPage(() =>
      Promise.resolve([
        release({ id: "old-mac", version: "1.4.0", os: "darwin", arch: "arm64" }),
        release({ id: "new-mac", version: "1.5.0", os: "darwin", arch: "arm64" }),
        release({ id: "new-linux", version: "1.5.0" }),
      ]),
    );
    const table = await screen.findByRole("table");
    expect(within(table).getAllByText("1.5.0")).toHaveLength(2);
    expect(within(table).queryByText("1.4.0")).toBeNull();

    await userEvent.click(screen.getByRole("button", { name: "Show older versions (1.4.0)" }));
    const rows = within(screen.getByRole("table")).getAllByRole("row").slice(1);
    expect(rows.map((row) => within(row).getByText(/^1\.\d\.0$/).textContent)).toEqual([
      "1.5.0",
      "1.5.0",
      "1.4.0",
    ]);
  });

  it("says it is loading rather than showing an empty catalogue", async () => {
    renderPage(() => new Promise<never>(() => {}));
    expect(await screen.findByText("Loading…")).toBeInTheDocument();
  });

  it("says the request FAILED rather than showing an empty catalogue", async () => {
    renderPage(() => Promise.reject(new Error("boom")));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText(/No agent packages/i)).not.toBeInTheDocument();
  });

  it("says the catalogue is empty when it genuinely is", async () => {
    renderPage(() => Promise.resolve([]));
    expect(await screen.findByText(/No agent packages published yet/i)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("lists a release with the checksum an operator verifies the download against", async () => {
    renderPage(() => Promise.resolve([release()]));
    expect(await screen.findByText("873d666")).toBeInTheDocument();
    expect(screen.getByText(/linux/)).toBeInTheDocument();
    // Truncated for width, so assert on the prefix the page actually renders.
    expect(screen.getByText(/a1b2c3d4/)).toBeInTheDocument();
  });

  it("says a Windows package has not been tested on a real machine, and what to do about it", async () => {
    renderPage(() => Promise.resolve([release({ id: "2", os: "windows" })]));
    expect(await screen.findByText(/Not yet tested on a real Windows machine/)).toBeInTheDocument();
    expect(screen.getByText(/Try it on one computer you can reach before rolling it out/)).toBeInTheDocument();
  });

  it("says a macOS package has not been tested on a real Mac, and names the platform plainly", async () => {
    renderPage(() => Promise.resolve([release({ id: "3", os: "darwin", arch: "arm64" })]));
    expect(await screen.findByText(/Not yet tested on a real Mac/)).toBeInTheDocument();
    expect(screen.getByText("macOS (Apple silicon)")).toBeInTheDocument();
  });

  it("does NOT mark Linux, which is the tested path", async () => {
    // Non-vacuity for the two above: a page that stamped every row would pass
    // them and tell an operator nothing.
    renderPage(() => Promise.resolve([release()]));
    await screen.findByText("873d666");
    expect(screen.queryByText(/not yet tested/i)).not.toBeInTheDocument();
  });

  it("says a release is unsigned rather than saying nothing", async () => {
    renderPage(() => Promise.resolve([release({ signed: false })]));
    expect(await screen.findByText(/unsigned/i)).toBeInTheDocument();
  });

  it("names code signing and the release signature apart, and says which channel a release is on", async () => {
    // Two different signatures, and until now the page had one column called
    // "Signature" for the first of them. An operator deciding whether agents
    // will take an update needs the SECOND -- and must not read "Unsigned"
    // (no Authenticode) as "agents will refuse it", or the reverse.
    renderPage(() =>
      Promise.resolve([
        release({ signed: false, release_signed: true, channel: "stable" }),
        release({
          id: "22222222-2222-2222-2222-222222222222",
          arch: "arm64",
          signed: false,
          release_signed: false,
          channel: "pilot",
        }),
      ]),
    );
    await screen.findAllByText("873d666");

    expect(screen.getByRole("columnheader", { name: "Code signing" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Release signature" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Channel" })).toBeInTheDocument();
    expect(screen.getAllByText("Unsigned")).toHaveLength(2);
    expect(screen.getByText("Recorded")).toBeInTheDocument();
    expect(screen.getByText("Not recorded")).toBeInTheDocument();
    expect(screen.getByText("Stable")).toBeInTheDocument();
    expect(screen.getByText("Pilot")).toBeInTheDocument();
  });

  it("treats a release the server sends without a channel as pilot, which is the server's default", async () => {
    renderPage(() => Promise.resolve([release()]));
    await screen.findByText("873d666");
    expect(screen.getByText("Pilot")).toBeInTheDocument();
    expect(screen.getByText("Not recorded")).toBeInTheDocument();
  });

  it("offers publishing, as the page's one primary, only to an account that can publish", async () => {
    // A convenience, not a gate: every publishing route checks
    // agent.release.manage itself. It saves an org-admin a 403.
    renderPage(() => Promise.resolve([release()]));
    await screen.findByText("873d666");
    expect(screen.queryByRole("button", { name: /publish a release/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /mark as stable/i })).not.toBeInTheDocument();
  });

  it("gives a publisher the publish button in the page head and the channel controls", async () => {
    renderPage(() => Promise.resolve([release()]), undefined, null, ["agent.release.manage"]);
    // Twice: the catalogue row, and the version's row under "Release channels".
    await screen.findAllByText("873d666");

    const publish = await screen.findByRole("button", { name: /publish a release/i });
    expect(publish).toHaveClass("primary");
    expect(publish.closest(".ph")).not.toBeNull();
    expect(document.querySelectorAll(".btn.primary")).toHaveLength(1);
    expect(screen.getByRole("button", { name: /mark as stable/i })).toBeInTheDocument();

    await userEvent.click(publish);
    expect(await screen.findByRole("dialog", { name: "Publish a release" })).toBeInTheDocument();
  });

  it("will not offer an install command for a release with no artefact", async () => {
    renderPage(() => Promise.resolve([release({ has_artifact: false, sha256: "" })]));
    await screen.findByText("873d666");
    const button = screen.getByRole("button", { name: /install command/i });
    expect(button).toBeDisabled();
    expect(screen.getByText(/no file uploaded$/i)).toBeInTheDocument();
  });

  it("draws both row actions plain, the one-machine install first, and no primary in the page body", async () => {
    // The thing the operator could not find is still first in the row. But
    // v3 draws every per-row action as a plain button and keeps a page's one
    // primary in the head; this page has no head action (nothing publishes
    // from here), so the body carries no primary at all. Five primaries in
    // one table was the review finding this pins against.
    renderPage(() => Promise.resolve([release()]));
    await screen.findByText("873d666");

    const install = screen.getByRole("button", { name: /install command/i });
    const managed = screen.getByRole("button", { name: /managed deployment/i });
    expect(install).not.toHaveClass("primary");
    expect(managed).not.toHaveClass("primary");
    expect(install.compareDocumentPosition(managed) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    // The page renders without the shell here, so every primary on the
    // document is one this page painted.
    expect(document.querySelectorAll(".btn.primary")).toHaveLength(0);
  });

  it("offers a direct download of the binary for the release's platform", async () => {
    // "per-OS download of the artefact". The install command curls the same
    // file, but an operator who wants the bytes to stage by hand should not have
    // to reverse-engineer them out of a shell one-liner. The URL is a presigned
    // GET carrying no enrollment token.
    const generate = vi.fn().mockResolvedValue({
      release: release(),
      token: "tok_abc123",
      token_expires_at: "2026-08-18T13:00:00Z",
      download_url: "https://itms.example/agent-releases/873d666/its-agent-linux-amd64?sig=x",
      download_expires_in_s: 900,
      sha256: "a1b2c3d4",
      command: "curl …",
    });
    renderPage(() => Promise.resolve([release()]), generate);
    await screen.findByText("873d666");
    await generateCommand();

    const link = await screen.findByRole("link", { name: /download the agent binary/i });
    expect(link).toHaveAttribute(
      "href",
      "https://itms.example/agent-releases/873d666/its-agent-linux-amd64?sig=x",
    );
  });

  it("shows the command, and says the token is single-use", async () => {
    const generate = vi.fn().mockResolvedValue({
      release: release(),
      token: "tok_abc123",
      token_expires_at: "2026-08-18T13:00:00Z",
      download_url: "https://itms.example/agent-releases/873d666/its-agent-linux-amd64?sig=x",
      download_expires_in_s: 900,
      sha256: "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90",
      command: "curl -fsSL '…' -o /tmp/its-agent && …",
    });
    renderPage(() => Promise.resolve([release()]), generate);
    await screen.findByText("873d666");

    await generateCommand();

    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    const tokenLine = screen.getByText(/single use/i).closest("p");
    // The TOKEN's expiry, specifically. The dialog also states when the
    // download link expires, and a bare /expires/i matches both -- which would
    // let the token's expiry be dropped entirely without failing this test.
    expect(tokenLine?.textContent).toMatch(/expires/i);
  });

  it("renders the token expiry in the signed-in user's zone", async () => {
    // The one figure on this screen a person acts on. Read five hours early,
    // it is a token abandoned while it is still good; read five hours late, it
    // is an install that fails at the endpoint with no explanation.
    const generate = vi.fn().mockResolvedValue({
      release: release(),
      token: "tok_abc123",
      token_expires_at: "2026-08-18T23:30:00Z",
      download_url: "https://itms.example/x",
      download_expires_in_s: 900,
      sha256: "a1b2c3d4",
      command: "curl …",
    });
    renderPage(() => Promise.resolve([release()]), generate, "Asia/Tokyo");
    await screen.findByText("873d666");
    await generateCommand();

    const tokenLine = (await screen.findByText(/single use/i)).closest("p");
    // 23:30Z is 08:30 the NEXT DAY in Tokyo.
    expect(tokenLine?.textContent).toContain("19 Aug 2026, 08:30");
  });

  it("says why an install command could not be generated, as a sentence", async () => {
    // `String(generate.error)` rendered "Error: " and nothing else for the
    // bodyless 503 an ApiError carries as `message: ""` -- the exact shape
    // behind the audit's swallowed-failure BLOCKER.
    const generate = vi.fn().mockRejectedValue(new ApiError(503, ""));
    renderPage(() => Promise.resolve([release()]), generate);
    await screen.findByText("873d666");
    await generateCommand();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/unavailable right now/i);
    expect(alert.textContent).not.toMatch(/^Error:/);
    // And no dialog opened, so nothing suggests a token was minted.
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("translates a validation refusal instead of printing its payload", async () => {
    const generate = vi.fn().mockRejectedValue(
      new ApiError(422, JSON.stringify({
        detail: [{ type: "int_parsing", loc: ["body", "ttl_hours"], msg: "Input should be a valid integer" }],
      })),
    );
    renderPage(() => Promise.resolve([release()]), generate);
    await screen.findByText("873d666");
    await generateCommand();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/Ttl hours: Input should be a valid integer/);
    expect(alert.textContent).not.toContain("int_parsing");
    expect(alert.textContent).not.toContain('"loc"');
  });

  it("counts from the data rather than from a constant", async () => {
    renderPage(() =>
      Promise.resolve([
        release(),
        // Distinct versions: the catalogue is unique on (version, os, arch), so
        // three rows sharing "873d666" is a shape the API cannot return, and it
        // makes every findByText below ambiguous.
        release({ id: "2", version: "a11ce55", os: "windows", has_artifact: false, sha256: "" }),
        release({ id: "3", version: "b0bcafe", os: "darwin" }),
      ]),
    );
    // The tile counts the whole catalogue, including versions the table folds
    // away behind "Show older versions".
    await waitFor(() => {
      const values = [...document.querySelectorAll(".stat-value")].map((n) => n.textContent);
      expect(values[0]).toBe("3");
    });
  });
  it("mints nothing until the confirmation is answered, and names the release in it", async () => {
    // THE AUDITED SHAPE, one screen over: a button whose label describes a
    // disclosure while the click issues a live bootstrap credential.
    const generate = vi.fn();
    renderPage(() => Promise.resolve([release()]), generate);
    await screen.findByText("873d666");

    await userEvent.click(screen.getByRole("button", { name: /install command/i }));

    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("873d666 · Linux (64-bit Intel/AMD)");
    expect(dialog).toHaveTextContent(/creates a one-time enrollment token/i);
    expect(dialog).toHaveTextContent(/expires in 4 hours/i);
    expect(generate).not.toHaveBeenCalled();

    await userEvent.click(within(dialog).getByRole("button", { name: "Generate command" }));
    expect(generate).toHaveBeenCalledTimes(1);
    // MSP clients, phase 1: the token the command carries is minted for a
    // site, and until the client selector (phase 3) that is the internal
    // client's "Principal" site, whose id is the organisation's -- `/me`'s
    // `org_id`, "o" in `renderPage`.
    expect(generate).toHaveBeenCalledWith("11111111-1111-1111-1111-111111111111", 4, "o");
  });

  it("mints nothing when the confirmation is cancelled", async () => {
    const generate = vi.fn();
    renderPage(() => Promise.resolve([release()]), generate);
    await screen.findByText("873d666");

    await userEvent.click(screen.getByRole("button", { name: /install command/i }));
    await userEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Cancel" }),
    );

    expect(generate).not.toHaveBeenCalled();
    expect(screen.queryByText(/single use/i)).not.toBeInTheDocument();
  });

  it("copies the command rather than making the operator select a token by hand", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    stubClipboard(writeText);
    const generate = vi.fn().mockResolvedValue({
      release: release(),
      token: "tok_abc123",
      token_expires_at: "2026-08-18T13:00:00Z",
      download_url: "https://itms.example/x",
      download_expires_in_s: 900,
      sha256: "a1b2c3d4",
      command: "curl -fsSL '…' -o /tmp/its-agent && /tmp/its-agent enroll --token tok_abc123",
    });
    renderPage(() => Promise.resolve([release()]), generate);
    await screen.findByText("873d666");
    await generateCommand();

    await userEvent.click(await screen.findByRole("button", { name: /Copy command/ }));

    expect(writeText).toHaveBeenCalledWith(
      "curl -fsSL '…' -o /tmp/its-agent && /tmp/its-agent enroll --token tok_abc123",
    );
    expect(await screen.findByRole("status")).toHaveTextContent(/Copied/i);
  });

  it("says the copy FAILED rather than letting the operator paste nothing", async () => {
    stubClipboard(() => Promise.reject(new Error("denied")));
    const generate = vi.fn().mockResolvedValue({
      release: release(),
      token: "tok_abc123",
      token_expires_at: "2026-08-18T13:00:00Z",
      download_url: "https://itms.example/x",
      download_expires_in_s: 900,
      sha256: "a1b2c3d4",
      command: "curl …",
    });
    renderPage(() => Promise.resolve([release()]), generate);
    await screen.findByText("873d666");
    await generateCommand();

    await userEvent.click(await screen.findByRole("button", { name: /Copy command/ }));

    expect(await screen.findByText(/refused clipboard access/i)).toBeInTheDocument();
    expect(screen.queryByText(/Copied to the clipboard/i)).not.toBeInTheDocument();
  });
  /* -------------------------------------- the empty catalogue explains itself */

  /**
   * "No agent packages." — the operator, on the deployed console, reporting it
   * as a fault.
   *
   * It is not a fault: nothing has ever been published, and nothing ever
   * publishes itself. The platform never builds, fetches or pushes the agent
   * (spec §1), CI emits no downloadable binary (`.github/workflows/ci.yml`
   * cross-compiles to `/dev/null`), and there is deliberately no publish
   * button on this page. Every one of those is a defensible decision and their
   * SUM was a screen that said "0" and stopped.
   *
   * That is the dead tile the operator ruled out, one level up: a page whose
   * emptiness carries no way to make it non-empty. So the empty state has to
   * name the cause and the next step, and these tests hold it to both.
   */
  it("says WHY the catalogue is empty, not just that it is", async () => {
    renderPage(() => Promise.resolve([]));
    await screen.findByText("No agent packages published yet.");

    // The cause, in the words that make it not-a-bug: nobody has published.
    expect(screen.getByText(/never builds, fetches or pushes the agent/i)).toBeInTheDocument();
  });

  it("does not put build or publish steps in front of an installing operator", async () => {
    // "the steps to build an agent shouldn't be there, just the installation
    // packages" -- the operator, on the deployed console. An org-admin who
    // INSTALLS is not the audience for build.sh, the register/upload curls, or
    // a local sha256sum: those belong to whoever PUBLISHES a release, and since
    // migration 0070 that is a different person entirely (agent.release.manage,
    // which an org-admin does not hold). The build recipe was written for the
    // publisher and rendered on the installer's screen; it is gone from here.
    renderPage(() => Promise.resolve([]));
    await screen.findByText("No agent packages published yet.");

    expect(screen.queryByText(/packaging\/build\.sh/)).not.toBeInTheDocument();
    expect(screen.queryByText(/\/api\/v1\/agent\/releases/)).not.toBeInTheDocument();
    expect(screen.queryByText(/sha256sum/i)).not.toBeInTheDocument();
    // Not even the runbook link: an operator who only installs is pointed at
    // no publish mechanics at all.
    expect(screen.queryByText(/deploy\/README\.md/)).not.toBeInTheDocument();
  });

  it("tells an org-admin that publishing is not theirs to do, and why", async () => {
    // Migration 0070 moved `agent.release.manage` to `super-admin` -- the
    // catalogue is global, so a publisher writes the binary EVERY tenant's
    // endpoints execute. The console cannot grant that role by design, so
    // "just try it" ends in a 403 the page could have predicted.
    renderPage(() => Promise.resolve([]));
    await screen.findByText("No agent packages published yet.");

    expect(screen.queryByText(/agent\.release\.manage/)).not.toBeInTheDocument();
    expect(screen.getByText(/not something your account can do/i)).toBeInTheDocument();
    expect(screen.getByText(/permission to publish agent packages/i)).toBeInTheDocument();
    expect(screen.getByText(/ask whoever runs this deployment/i)).toBeInTheDocument();
  });

  it("points a publisher at the runbook, not at an inline recipe", async () => {
    // Non-vacuity for the test above, and the other half of the audience split:
    // the one person who CAN publish is handed the runbook -- not the curl
    // recipe that used to sit on the installer's screen for everyone.
    renderPage(() => Promise.resolve([]), undefined, null, ["agent.release.manage"]);
    await screen.findByText("No agent packages published yet.");

    expect(screen.queryByText(/cannot be granted from this console/i)).not.toBeInTheDocument();
    expect(screen.getByText(/your account can publish/i)).toBeInTheDocument();
    // The runbook link, gated to the manager.
    expect(screen.getByText(/deploy\/README\.md/)).toBeInTheDocument();
    // Still a pointer, never the mechanics.
    expect(screen.queryByText(/packaging\/build\.sh/)).not.toBeInTheDocument();
    expect(screen.queryByText(/sha256sum/i)).not.toBeInTheDocument();
  });

  it("keeps the explanation out of the way once something IS published", async () => {
    renderPage(() => Promise.resolve([release()]));
    await screen.findByText("873d666");

    expect(screen.queryByText(/never builds, fetches or pushes the agent/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/packaging\/build\.sh/)).not.toBeInTheDocument();
  });

  it("does not explain an empty catalogue when the catalogue failed to load", async () => {
    // A 403 or a 503 is NOT "nobody has published yet", and publishing
    // instructions in front of a failed request send the operator to do work
    // that will not fix it.
    renderPage(() => Promise.reject(new ApiError(503, "")));
    await screen.findByRole("alert");

    expect(screen.queryByText(/never builds, fetches or pushes the agent/i)).not.toBeInTheDocument();
  });

  /* ------------------------------------------ no dead tiles (visual cycle) */

  /**
   * "All tiles and all numbers or kpis need to be clickable and direct the
   * user to the specific menus, i dont want dead tiles everywhere" — the
   * operator, verbatim. `StatCard` requires `to`, so a MISSING destination is
   * a compile error. A destination that goes nowhere is not, and on this page
   * that is the live risk: the catalogue is served here and rendered nowhere
   * else, so all three tiles point at a fragment of this same page. A fragment
   * with no matching element scrolls nowhere — a dead tile wearing an href —
   * so the element is asserted rather than assumed.
   */
  it("renders every stat tile as a link with a real destination", async () => {
    renderPage(() => Promise.resolve([release()]));
    await screen.findByText("873d666");

    const tiles = [...document.querySelectorAll(".stat-card")];
    expect(tiles).toHaveLength(3);

    for (const tile of tiles) {
      // AN ANCHOR, not a div with onClick: keyboard reachable and middle-
      // clickable into a new tab.
      expect(tile.tagName).toBe("A");
      const href = tile.getAttribute("href");
      expect(href).toBeTruthy();
      expect(href).not.toBe("#");
    }

    expect(tiles.map((tile) => tile.getAttribute("href"))).toEqual([
      "/agent-packages#releases",
      "/agent-packages#releases",
      "/agent-packages#releases",
    ]);
  });

  it("every fragment a tile points at is an element that exists on the page", async () => {
    // THE HALF THAT ACTUALLY DECAYS. `#releases` is a valid href whether or
    // not anything carries that id, so a test that only reads hrefs stays
    // green while all three tiles become scroll-to-nowhere. This reads the DOM.
    renderPage(() => Promise.resolve([release()]));
    await screen.findByText("873d666");

    const fragments = [...document.querySelectorAll(".stat-card")]
      .map((tile) => tile.getAttribute("href") ?? "")
      .filter((href) => href.includes("#"))
      .map((href) => decodeURIComponent(href.slice(href.indexOf("#") + 1)));

    // Non-vacuity: a row that stopped using fragments would iterate zero
    // times and assert nothing at all.
    expect(fragments).toHaveLength(3);
    for (const id of fragments) {
      expect(document.getElementById(id)).not.toBeNull();
    }
  });

  it("says WHICH zero each tile is reading, and none of them is an alarm", async () => {
    renderPage(() => Promise.resolve([]));
    await screen.findByText("No agent packages published yet.");

    expect(screen.getByText("Nothing published yet")).toBeInTheDocument();
    // Specifically NOT "0 counted": a catalogue with no uploaded artefact is a
    // catalogue nobody can install from, which is a different fact.
    expect(screen.getByText("No release has a file uploaded")).toBeInTheDocument();
    expect(screen.getByText("No platform is covered")).toBeInTheDocument();

    // A published-package catalogue is a shelf, not a queue. Nothing on this
    // screen means somebody has to act now, so nothing here carries weight.
    expect(document.querySelectorAll(".stat-card.urgent")).toHaveLength(0);
  });

  it("does not render a zero label while the catalogue is still unread", async () => {
    // A PENDING QUERY IS NOT AN EMPTY CATALOGUE. `items` defaults to `[]`, so
    // without the guard all three tiles read "0" during the first paint and a
    // zero label would be describing a measurement that never happened.
    renderPage(() => new Promise<never>(() => {}));
    await screen.findByText("Loading…");

    const values = [...document.querySelectorAll(".stat-value")].map((n) => n.textContent);
    expect(values).toEqual(["\u2014", "\u2014", "\u2014"]);
    expect(screen.queryByText("Nothing published yet")).not.toBeInTheDocument();
  });
});

describe("the install link panel on Agent packages", () => {
  function renderWith(permissions: string[], getInstallLink = vi.fn().mockResolvedValue({
    enabled: false, has_code: false, networks: [], disable_at: null,
    hourly_limit: 50, enabled_at: null, joined_since_enabled: 0,
  })) {
    const me = vi.fn().mockResolvedValue({
      user_id: "u", org_id: "o", email: "a@example.com", display_name: "A", permissions, timezone: null,
    });
    mockUseApi.mockReturnValue({
      me, listAgentReleases: () => Promise.resolve([]), getInstallLink,
    } as unknown as ApiClient);
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <AgentReleasesPage />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    return { me, getInstallLink };
  }

  it("is offered to whoever may manage the link", async () => {
    const { getInstallLink } = renderWith(["enrollment.link.manage"]);
    expect(await screen.findByRole("heading", { name: "Install link" })).toBeInTheDocument();
    // One link per site now (MSP clients, phase 1): the panel manages the
    // internal client's "Principal" site, whose id is `/me`'s `org_id`.
    expect(getInstallLink).toHaveBeenCalledWith("o");
  });

  it("is not offered, and not asked for, without that permission", async () => {
    const { me, getInstallLink } = renderWith(["agent.release.read"]);
    await waitFor(() => expect(me).toHaveBeenCalled());
    await screen.findByRole("heading", { name: "Published releases" });
    expect(screen.queryByRole("heading", { name: "Install link" })).not.toBeInTheDocument();
    expect(getInstallLink).not.toHaveBeenCalled();
  });
});
