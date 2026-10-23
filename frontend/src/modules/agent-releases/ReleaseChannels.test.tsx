/**
 * "Mark as stable" / "Move back to pilot".
 *
 * The route changes EVERY platform's package of a version at once, and the
 * channel decides which rings a rollout may reach
 * (`agent_update.service._require_stable`). So the confirmation has to say
 * which version, how many packages, and what the channel means -- and nothing
 * is sent until it is answered.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { AgentRelease } from "../../lib/api";
import { ApiError } from "../../lib/api";
import { ReleaseChannels, compareVersions, summariseVersions } from "./ReleaseChannels";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

function release(overrides: Partial<AgentRelease> = {}): AgentRelease {
  return {
    id: `${overrides.version ?? "1.4.0"}-${overrides.os ?? "linux"}-${overrides.arch ?? "amd64"}`,
    version: "1.4.0",
    os: "linux",
    arch: "amd64",
    size_bytes: 10,
    sha256: "a".repeat(64),
    signed: false,
    notes: "",
    published_at: null,
    has_artifact: true,
    channel: "pilot",
    release_signed: true,
    ...overrides,
  };
}

function setup(
  releases: AgentRelease[],
  promote: (version: string, channel: string) => Promise<unknown> = vi.fn(() =>
    Promise.resolve({ version: "", channel: "stable", rows: 0 }),
  ),
  remove: (version: string) => Promise<unknown> = vi.fn((version: string) =>
    Promise.resolve({ version, removed: 1, history_cleared: 0, files_not_removed: [] }),
  ),
) {
  mockUseApi.mockReturnValue({ promoteAgentReleaseVersion: promote, deleteAgentReleaseVersion: remove });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidate = vi.spyOn(queryClient, "invalidateQueries");
  render(
    <QueryClientProvider client={queryClient}>
      <ReleaseChannels releases={releases} />
    </QueryClientProvider>,
  );
  return { promote, remove, invalidate };
}

function versionRow(version: string): HTMLElement {
  return within(screen.getByRole("table", { name: "Release channels" })).getByText(version).closest("tr")!;
}

beforeEach(() => {
  mockUseApi.mockReset();
});

describe("summariseVersions", () => {
  it("groups by version, newest first, and says when a version's packages disagree", () => {
    const versions = summariseVersions([
      release({ version: "1.9.0" }),
      release({ version: "1.10.0", channel: "stable" }),
      release({ version: "1.10.0", os: "windows", channel: "pilot", release_signed: false }),
      release({ version: "1.10.0", os: "darwin", has_artifact: false, release_signed: false }),
    ]);
    expect(versions).toEqual([
      { version: "1.10.0", channel: "mixed", packages: 3, unsigned: 1 },
      { version: "1.9.0", channel: "pilot", packages: 1, unsigned: 0 },
    ]);
  });

  it("ranks a release above its own pre-releases, as the server does", () => {
    // Codex review on #87: string order put 1.5.0-rc.1 first, so the page
    // showed the rc as newest and folded the stable build away.
    const order = ["1.5.0-rc.1", "1.4.9", "1.5.0", "1.5.0-rc.2", "1.10.0-beta", "1.5.0-rc.10"]
      .map((version) => release({ version, id: version }))
      .sort((a, b) => compareVersions(a.version, b.version))
      .map((r) => r.version);
    expect(order).toEqual(["1.10.0-beta", "1.5.0", "1.5.0-rc.10", "1.5.0-rc.2", "1.5.0-rc.1", "1.4.9"]);
  });
});

describe("ReleaseChannels", () => {
  it("promotes nothing until the confirmation is answered, and says what stable means", async () => {
    const { promote, invalidate } = setup([
      release(),
      release({ os: "windows", id: "w" }),
    ]);

    await userEvent.click(within(versionRow("1.4.0")).getByRole("button", { name: "Mark as stable…" }));
    expect(promote).not.toHaveBeenCalled();

    const dialog = screen.getByRole("dialog", { name: "Mark as stable" });
    expect(within(dialog).getByText("Version 1.4.0")).toBeInTheDocument();
    expect(dialog).toHaveTextContent("A stable release can be rolled out to every ring.");
    expect(dialog).toHaveTextContent("A pilot release can be rolled out only to the pilot ring.");
    expect(dialog).toHaveTextContent("all 2 packages of 1.4.0");

    await userEvent.click(within(dialog).getByRole("button", { name: "Mark as stable" }));
    expect(promote).toHaveBeenCalledWith("1.4.0", "stable");
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["agent-releases"] });
  });

  it("sends nothing when the confirmation is cancelled", async () => {
    const { promote } = setup([release()]);
    await userEvent.click(screen.getByRole("button", { name: "Mark as stable…" }));
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Cancel" }));
    expect(promote).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("warns, before promoting, about packages agents will refuse", async () => {
    setup([release(), release({ os: "windows", id: "w", release_signed: false })]);
    await userEvent.click(screen.getByRole("button", { name: "Mark as stable…" }));
    expect(screen.getByRole("dialog")).toHaveTextContent(
      "1 of them has no release signature recorded, and agents will refuse to update to it.",
    );
  });

  it("offers a stable version only the way back, and says what pilot means", async () => {
    const { promote } = setup([release({ channel: "stable" })]);
    const row = versionRow("1.4.0");
    expect(within(row).getByText("Stable")).toBeInTheDocument();
    expect(within(row).queryByRole("button", { name: "Mark as stable…" })).not.toBeInTheDocument();

    await userEvent.click(within(row).getByRole("button", { name: "Move back to pilot…" }));
    const dialog = screen.getByRole("dialog", { name: "Move back to pilot" });
    expect(dialog).toHaveTextContent("A pilot release can be rolled out only to the pilot ring.");
    expect(dialog).toHaveTextContent(/No rollout of this version can start or move on to any other ring/);

    await userEvent.click(within(dialog).getByRole("button", { name: "Move back to pilot" }));
    expect(promote).toHaveBeenCalledWith("1.4.0", "pilot");
  });

  it("offers both directions for a version whose packages disagree", async () => {
    setup([release({ channel: "stable" }), release({ os: "windows", id: "w", channel: "pilot" })]);
    const row = versionRow("1.4.0");
    expect(within(row).getByText("Partly stable")).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: "Mark as stable…" })).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: "Move back to pilot…" })).toBeInTheDocument();
  });

  it("says why a promotion was refused, as a sentence", async () => {
    setup(
      [release()],
      vi.fn(() => Promise.reject(new ApiError(404, JSON.stringify({ detail: "no release rows for version '1.4.0'" })))),
    );
    await userEvent.click(screen.getByRole("button", { name: "Mark as stable…" }));
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Mark as stable" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("no release rows for version '1.4.0'");
  });
});

describe("removing a version", () => {
  it("removes nothing until asked twice, then every package of that version and nothing else", async () => {
    const { remove, invalidate } = setup([
      release(),
      release({ os: "windows" }),
      release({ version: "1.5.0" }),
    ]);

    await userEvent.click(within(versionRow("1.4.0")).getByRole("button", { name: "Remove…" }));
    expect(remove).not.toHaveBeenCalled();

    const dialog = screen.getByRole("dialog", { name: "Remove this version" });
    expect(within(dialog).getByText("Version 1.4.0")).toBeInTheDocument();
    expect(dialog).toHaveTextContent("all 2 packages of 1.4.0");
    expect(dialog).toHaveTextContent("Machines already running 1.4.0 keep running it.");

    await userEvent.click(within(dialog).getByRole("button", { name: "Remove version" }));
    // One request for the whole version: the server removes all or none.
    await waitFor(() => expect(remove).toHaveBeenCalledTimes(1));
    expect(remove).toHaveBeenCalledWith("1.4.0");
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ["agent-releases"] }));
  });

  it("says which stored files were left behind when the catalogue rows are gone", async () => {
    const leftover = vi.fn(() =>
      Promise.resolve({
        version: "1.4.0",
        removed: 1,
        history_cleared: 0,
        files_not_removed: ["agent-releases/1.4.0/its-agent-linux-amd64"],
      }),
    );
    setup([release()], undefined, leftover);
    await userEvent.click(within(versionRow("1.4.0")).getByRole("button", { name: "Remove…" }));
    await userEvent.click(
      within(screen.getByRole("dialog", { name: "Remove this version" })).getByRole("button", {
        name: "Remove version",
      }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /1 stored file could not be deleted.*its-agent-linux-amd64/,
    );
  });

  it("says nothing was removed when a rollout still uses the version", async () => {
    const refused = vi.fn(() =>
      Promise.reject(
        new ApiError(409, JSON.stringify({ detail: "an active update rollout still uses this release" })),
      ),
    );
    const { invalidate } = setup([release()], undefined, refused);

    await userEvent.click(within(versionRow("1.4.0")).getByRole("button", { name: "Remove…" }));
    await userEvent.click(
      within(screen.getByRole("dialog", { name: "Remove this version" })).getByRole("button", {
        name: "Remove version",
      }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(/Nothing was removed.*still uses this release/);
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["agent-releases"] });
  });
});
