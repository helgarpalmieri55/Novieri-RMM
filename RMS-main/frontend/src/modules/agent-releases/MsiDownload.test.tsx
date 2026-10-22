/**
 * The MSI for Group Policy (GPO MSI spec 2026-09-30 §2.1).
 *
 * What is pinned: nothing is offered while the link is off; the link just
 * generated is pre-filled; the code (never the link's host) goes to the API
 * with the architecture; the file is saved under the server's name and its
 * SHA-256 is shown; a refusal says nothing was built; a paste that is not a
 * link is caught before any request.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../lib/api";
import type { ApiClient } from "../../lib/api";
import { MsiDownload, codeFromLink } from "./MsiDownload";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));
const saveBlob = vi.fn();
vi.mock("../../lib/download", () => ({ saveBlob: (...args: unknown[]) => saveBlob(...args) }));

const CODE = "AbCdEfGhIjKlMnOpQrStUvWxYz012_-9";
const LINK = `https://itms.example.com/i/${CODE}`;
const SHA = "3f".repeat(32);

let lastQueryClient: QueryClient;

function renderBlock(api: Partial<ApiClient>, props: { linkOn: boolean; revealed: string | null }) {
  mockUseApi.mockReturnValue(api as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  lastQueryClient = queryClient;
  return render(
    <QueryClientProvider client={queryClient}>
      <MsiDownload {...props} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mockUseApi.mockReset();
  saveBlob.mockReset();
});

describe("codeFromLink", () => {
  it("takes the code from a link or a bare code, and nothing else", () => {
    expect(codeFromLink(LINK)).toBe(CODE);
    expect(codeFromLink(`  ${LINK}/ `)).toBe(CODE);
    expect(codeFromLink(CODE)).toBe(CODE);
    expect(codeFromLink("https://itms.example.com/i/short")).toBeNull();
    expect(codeFromLink(`${CODE}x`)).toBeNull();
    expect(codeFromLink("")).toBeNull();
  });
});

describe("MsiDownload", () => {
  it("offers nothing while the link is off", () => {
    renderBlock({}, { linkOn: false, revealed: null });
    expect(screen.getByText(/Switch the link on to download the MSI/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Download MSI/ })).not.toBeInTheDocument();
  });

  it("downloads with the link just generated and shows the file's SHA-256", async () => {
    const blob = new Blob(["MSI"]);
    const downloadInstallLinkMsi = vi
      .fn()
      .mockResolvedValue({ blob, filename: "ITMS-Agent-itms.example.com-arm64.msi", sha256: SHA });
    renderBlock({ downloadInstallLinkMsi }, { linkOn: true, revealed: LINK });
    expect(screen.getByLabelText("Install link")).toHaveValue(LINK);
    await userEvent.click(screen.getByRole("button", { name: "Download MSI for Group Policy (ARM)" }));
    expect(downloadInstallLinkMsi).toHaveBeenCalledWith({ code: CODE, arch: "arm64" });
    expect(await screen.findByText(SHA)).toBeInTheDocument();
    expect(saveBlob).toHaveBeenCalledWith(blob, "ITMS-Agent-itms.example.com-arm64.msi");
    expect(screen.getByRole("status")).toHaveTextContent("ITMS-Agent-itms.example.com-arm64.msi");
  });

  it("takes a pasted link when none is on screen, and refuses a paste that is not one", async () => {
    const downloadInstallLinkMsi = vi.fn().mockResolvedValue({ blob: new Blob([]), filename: "x.msi", sha256: SHA });
    renderBlock({ downloadInstallLinkMsi }, { linkOn: true, revealed: null });
    const amd = screen.getByRole("button", { name: "Download MSI for Group Policy (64-bit Intel or AMD)" });
    expect(amd).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Install link"), "not a link");
    expect(screen.getByRole("alert")).toHaveTextContent("That is not an install link");
    expect(amd).toBeDisabled();
    await userEvent.clear(screen.getByLabelText("Install link"));
    await userEvent.type(screen.getByLabelText("Install link"), LINK);
    await userEvent.click(amd);
    expect(downloadInstallLinkMsi).toHaveBeenCalledWith({ code: CODE, arch: "amd64" });
  });

  it("says no MSI was built when the server refuses", async () => {
    const downloadInstallLinkMsi = vi.fn().mockRejectedValue(
      new ApiError(409, JSON.stringify({ detail: "That is not this organisation's current install link." })),
    );
    renderBlock({ downloadInstallLinkMsi }, { linkOn: true, revealed: LINK });
    await userEvent.click(screen.getByRole("button", { name: "Download MSI for Group Policy (64-bit Intel or AMD)" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("No MSI was built.");
    expect(screen.getByRole("alert")).toHaveTextContent("not this organisation's current install link");
    expect(saveBlob).not.toHaveBeenCalled();
  });

  it("keeps only the file's name and SHA-256 once the download is handed to the browser, never the MSI", async () => {
    const blob = new Blob(["MSI bytes"]);
    const downloadInstallLinkMsi = vi
      .fn()
      .mockResolvedValue({ blob, filename: "ITMS-Agent-itms.example.com-amd64.msi", sha256: SHA });
    renderBlock({ downloadInstallLinkMsi }, { linkOn: true, revealed: LINK });
    await userEvent.click(screen.getByRole("button", { name: "Download MSI for Group Policy (64-bit Intel or AMD)" }));
    expect(await screen.findByText(SHA)).toBeInTheDocument();
    expect(saveBlob).toHaveBeenCalledWith(blob, "ITMS-Agent-itms.example.com-amd64.msi");
    // The mutation's result is what React Query keeps: the name and the digest only.
    const [mutation] = lastQueryClient.getMutationCache().getAll();
    expect(mutation?.state.data).toEqual({ filename: "ITMS-Agent-itms.example.com-amd64.msi", sha256: SHA });
    expect(Object.values(mutation?.state.data ?? {}).some((value) => value instanceof Blob)).toBe(false);
  });
});
