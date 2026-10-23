/**
 * "Publish a release": the console path that replaces two `curl` calls and a
 * `PUT release-signature` from `deploy/README.md`.
 *
 * The rule these tests hold hardest is the digest comparison. The control
 * plane stores whatever release signature it is given and never verifies it;
 * the agent verifies it, in the field, at update time. So a signature attached
 * to bytes it does not cover is not caught anywhere before an agent refuses the
 * update -- except here, by comparing the SIGNATURES line's sha256 with the
 * digest the SERVER computed over the upload.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../lib/api";
import type { AgentRelease, AgentReleaseCreate } from "../../lib/api";
import { PublishReleaseDialog } from "./PublishReleaseDialog";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const SHA_LINUX = "1".repeat(64);
const SHA_WINDOWS = "2".repeat(64);
const SIG_LINUX = `${"L".repeat(86)}==`;
const SIG_WINDOWS = `${"W".repeat(86)}==`;

const SIGNATURES =
  `${SHA_LINUX}  its-agent-linux-amd64  linux/amd64  ${SIG_LINUX}\n` +
  `${SHA_WINDOWS}  its-agent-windows-amd64.exe  windows/amd64  ${SIG_WINDOWS}\n`;

function row(body: AgentReleaseCreate, sha256 = ""): AgentRelease {
  return {
    id: `id-${body.os}-${body.arch}`,
    version: body.version,
    os: body.os,
    arch: body.arch,
    size_bytes: sha256 ? 10 : 0,
    sha256,
    signed: body.signed,
    notes: body.notes,
    published_at: null,
    has_artifact: sha256 !== "",
  };
}

/** What the server's digest of each platform's upload will be. */
type Digests = Record<string, string>;

function setup(options: { digests?: Digests; create?: (body: AgentReleaseCreate) => Promise<AgentRelease> } = {}) {
  const digests: Digests = options.digests ?? {
    "linux/amd64": SHA_LINUX,
    "windows/amd64": SHA_WINDOWS,
  };
  const created = new Map<string, AgentReleaseCreate>();
  const api = {
    createAgentRelease: vi.fn(
      options.create ??
        ((body: AgentReleaseCreate) => {
          created.set(`id-${body.os}-${body.arch}`, body);
          return Promise.resolve(row(body));
        }),
    ),
    uploadAgentReleaseArtifact: vi.fn((id: string) => {
      const [, os, arch] = id.split("-");
      const body = created.get(id) ?? { version: "1.4.0", os: os!, arch: arch!, signed: false, notes: "" };
      return Promise.resolve(row(body, digests[`${os}/${arch}`] ?? "f".repeat(64)));
    }),
    setAgentReleaseSignature: vi.fn(() => Promise.resolve()),
    deleteAgentRelease: vi.fn(() => Promise.resolve()),
  };
  mockUseApi.mockReturnValue(api);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidate = vi.spyOn(queryClient, "invalidateQueries");
  const onClose = vi.fn();
  render(
    <QueryClientProvider client={queryClient}>
      <PublishReleaseDialog onClose={onClose} />
    </QueryClientProvider>,
  );
  return { api, invalidate, onClose };
}

const bin = (name: string) => new File([new Uint8Array(16)], name);

async function chooseBinaries(...names: string[]) {
  await userEvent.upload(screen.getByLabelText("Agent binaries"), names.map(bin));
}

async function chooseSignatures(text = SIGNATURES) {
  await userEvent.upload(screen.getByLabelText("Signatures file"), new File([text], "SIGNATURES"));
  await screen.findByText(/signatures? read/);
}

async function typeVersion(version = "1.4.0") {
  await userEvent.type(screen.getByRole("textbox", { name: "Version" }), version);
}

async function submit() {
  await userEvent.click(screen.getByRole("button", { name: "Publish" }));
}

/** The results table's row for one file. */
function fileRow(name: string): HTMLElement {
  return screen.getByText(name).closest("tr")!;
}

beforeEach(() => {
  mockUseApi.mockReset();
});

describe("PublishReleaseDialog", () => {
  it("names each binary's platform from its file name, in words", async () => {
    setup();
    await chooseBinaries("its-agent-linux-amd64", "its-agent-windows-amd64.exe", "its-agent-darwin-arm64");

    expect(within(fileRow("its-agent-linux-amd64")).getByText("Linux (64-bit Intel/AMD)")).toBeInTheDocument();
    expect(within(fileRow("its-agent-windows-amd64.exe")).getByText("Windows (64-bit Intel/AMD)")).toBeInTheDocument();
    expect(within(fileRow("its-agent-darwin-arm64")).getByText("macOS (Apple silicon)")).toBeInTheDocument();
  });

  it("asks for the platform of a file it cannot name, and publishes nothing until it has one", async () => {
    const { api } = setup({ digests: {} });
    await chooseBinaries("its-agent-linux-amd64 (1)");
    await typeVersion();
    await submit();

    expect(await screen.findByText("Choose the platform for its-agent-linux-amd64 (1).")).toBeInTheDocument();
    expect(api.createAgentRelease).not.toHaveBeenCalled();

    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: "Operating system for its-agent-linux-amd64 (1)" }),
      "linux",
    );
    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: "Processor for its-agent-linux-amd64 (1)" }),
      "arm64",
    );
    await submit();

    await screen.findByText(/1 of 1 file published/);
    expect(api.createAgentRelease).toHaveBeenCalledWith({
      version: "1.4.0",
      os: "linux",
      arch: "arm64",
      signed: false,
      notes: "",
    });
  });

  it("refuses a version the release workflow would refuse", async () => {
    const { api } = setup();
    await chooseBinaries("its-agent-linux-amd64");
    await typeVersion("v1.4.0");
    await submit();

    expect(await screen.findByText(/plain release number such as 1\.4\.0/)).toBeInTheDocument();
    expect(api.createAgentRelease).not.toHaveBeenCalled();
  });

  it("creates, uploads and records the release signature that matches the upload", async () => {
    const { api, invalidate } = setup();
    await typeVersion();
    await chooseBinaries("its-agent-linux-amd64", "its-agent-windows-amd64.exe");
    await chooseSignatures();
    expect(screen.getByText(/2 signatures read\./)).toBeInTheDocument();
    await submit();

    await screen.findByText(/2 of 2 files published/);
    // `signed` is the code-signing claim, and these binaries are not
    // code-signed; the release signature travels on its own route.
    for (const call of api.createAgentRelease.mock.calls) {
      expect(call[0]).toMatchObject({ version: "1.4.0", signed: false });
    }
    expect(api.setAgentReleaseSignature).toHaveBeenCalledWith("id-linux-amd64", SIG_LINUX);
    expect(api.setAgentReleaseSignature).toHaveBeenCalledWith("id-windows-amd64", SIG_WINDOWS);
    expect(screen.getAllByText("Published, release signature recorded")).toHaveLength(2);
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["agent-releases"] });
  });

  it("does NOT send a signature for an upload whose checksum differs, and says so for that file", async () => {
    const { api } = setup({
      digests: { "linux/amd64": "9".repeat(64), "windows/amd64": SHA_WINDOWS },
    });
    await typeVersion();
    await chooseBinaries("its-agent-linux-amd64", "its-agent-windows-amd64.exe");
    await chooseSignatures();
    await submit();

    await screen.findByText(/2 of 2 files published/);
    expect(api.setAgentReleaseSignature).toHaveBeenCalledTimes(1);
    expect(api.setAgentReleaseSignature).toHaveBeenCalledWith("id-windows-amd64", SIG_WINDOWS);
    expect(api.setAgentReleaseSignature).not.toHaveBeenCalledWith("id-linux-amd64", expect.anything());

    const linux = fileRow("its-agent-linux-amd64");
    expect(within(linux).getByText("Failed")).toBeInTheDocument();
    expect(within(linux).getByText(/checksums differ/)).toBeInTheDocument();
    expect(within(linux).getByText(/entry was removed, so you can publish this file again/)).toBeInTheDocument();
    expect(api.deleteAgentRelease).toHaveBeenCalledWith("id-linux-amd64");
  });

  it("says when the signatures file has no line for a platform, and publishes it unsigned", async () => {
    const { api } = setup({ digests: { "linux/arm64": SHA_LINUX } });
    await typeVersion();
    await chooseBinaries("its-agent-linux-arm64");
    await chooseSignatures();
    expect(within(fileRow("its-agent-linux-arm64")).getByText("No line for this platform")).toBeInTheDocument();
    await submit();

    await screen.findByText(/1 of 1 file published/);
    expect(api.setAgentReleaseSignature).not.toHaveBeenCalled();
    expect(screen.getByText("Published without a release signature")).toBeInTheDocument();
  });

  it("reports an unreadable signatures line rather than dropping it", async () => {
    setup();
    await chooseSignatures(`${SIGNATURES}garbage\n`);
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Line 3 of the signatures file is not in the expected form.",
    );
  });

  it("carries on after one file is refused, and shows the server's reason for it", async () => {
    const { api, invalidate } = setup({
      create: (body) =>
        body.os === "linux"
          ? Promise.reject(
              new ApiError(
                409,
                JSON.stringify({ detail: "a release already exists for this version, OS and architecture" }),
              ),
            )
          : Promise.resolve(row(body)),
    });
    await typeVersion();
    await chooseBinaries("its-agent-linux-amd64", "its-agent-windows-amd64.exe");
    await chooseSignatures();
    await submit();

    await screen.findByText(/1 of 2 files published/);
    const linux = fileRow("its-agent-linux-amd64");
    expect(within(linux).getByText("Failed")).toBeInTheDocument();
    expect(
      within(linux).getByText("a release already exists for this version, OS and architecture"),
    ).toBeInTheDocument();
    expect(within(fileRow("its-agent-windows-amd64.exe")).getByText("Published, release signature recorded")).toBeInTheDocument();
    expect(api.uploadAgentReleaseArtifact).toHaveBeenCalledTimes(1);
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["agent-releases"] });
  });

  it("takes every downloaded asset at once: reads SIGNATURES, leaves the other side files out", async () => {
    const { api } = setup();
    await typeVersion();
    await userEvent.upload(screen.getByLabelText("Agent binaries"), [
      bin("its-agent-linux-amd64"),
      new File([SIGNATURES], "SIGNATURES"),
      bin("SHA256SUMS"),
      bin("release-signing-key.pub"),
    ]);
    await screen.findByText(/2 signatures read\./);
    expect(screen.getByText("SHA256SUMS, release-signing-key.pub")).toBeInTheDocument();
    await submit();

    await screen.findByText(/1 of 1 file published/);
    expect(api.createAgentRelease).toHaveBeenCalledTimes(1);
    expect(api.setAgentReleaseSignature).toHaveBeenCalledWith("id-linux-amd64", SIG_LINUX);
  });
});
