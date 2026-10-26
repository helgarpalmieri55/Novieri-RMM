/**
 * Adding an asset by hand (asset-inventory spec §2).
 *
 * TWO THINGS THIS FORM MUST GET RIGHT, and both are about what it does NOT
 * send:
 *
 *   1. A boxed laptop has no hostname. A form that required one would make the
 *      warehouse case — half the reason this feature exists — impossible to
 *      enter, and the server accepts a record whose entire identity is a
 *      description.
 *   2. An empty field is OMITTED, not sent as "". The API validates
 *      `primary_ip` as an address, and `""` is not one — so a form that sent
 *      every field would 422 on every asset whose IP the operator did not know.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../lib/api";
import { ApiError } from "../../lib/api";
import { AddAssetDialog } from "./AddAssetDialog";

const createAsset = vi.fn();
const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

/**
 * The site a hand-made asset is filed under (MSP clients, phase 1: `POST
 * /devices` requires `site_id`). The Devices page passes the internal client's
 * "Principal" site, whose id is the organisation's; any id stands in for it.
 */
const SITE = "00000000-0000-4000-8000-0000000000aa";

function renderDialog(onClose: () => void = () => {}, siteId: string | null = SITE) {
  mockUseApi.mockReturnValue({ createAsset } as unknown as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const ui = (open: boolean) => (
    <QueryClientProvider client={queryClient}>
      <AddAssetDialog open={open} onClose={onClose} siteId={siteId} />
    </QueryClientProvider>
  );
  const result = render(ui(true));
  // The dialog is CLOSED BY THE PARENT, not by unmounting -- which is exactly
  // why mutation state survives it. `reopen` reproduces that.
  return {
    ...result,
    close: () => result.rerender(ui(false)),
    reopen: () => result.rerender(ui(true)),
  };
}

beforeEach(() => {
  createAsset.mockReset().mockResolvedValue({ id: "1" });
  mockUseApi.mockReset();
});

describe("AddAssetDialog", () => {
  it("accepts an asset with a description and nothing else", async () => {
    renderDialog();
    await userEvent.type(
      screen.getByLabelText(/description/i),
      "boxed laptop, PO 4471",
    );
    await userEvent.click(screen.getByRole("button", { name: /add asset/i }));

    expect(createAsset).toHaveBeenCalledTimes(1);
    const body = createAsset.mock.calls[0]?.[0] as Record<string, unknown>;
    expect(body.description).toBe("boxed laptop, PO 4471");
    // Not sent as "": the server would refuse an empty IP as malformed.
    expect(body).not.toHaveProperty("hostname");
    expect(body).not.toHaveProperty("primary_ip");
    // The server validates a DNS name as a hostname and "" is not one.
    expect(body).not.toHaveProperty("fqdn");
    expect(body).not.toHaveProperty("os_family");
    expect(body).not.toHaveProperty("architecture");
  });

  it("sends name, IP and lifecycle when the operator gives them", async () => {
    renderDialog();
    await userEvent.type(screen.getByLabelText(/^name$/i), "core-sw-01");
    await userEvent.type(screen.getByLabelText(/ip address/i), "172.22.104.1");
    await userEvent.selectOptions(screen.getByLabelText(/lifecycle/i), "unmanageable");
    await userEvent.selectOptions(screen.getByLabelText(/^type$/i), "network");
    await userEvent.click(screen.getByRole("button", { name: /add asset/i }));

    expect(createAsset).toHaveBeenCalledWith({
      // RE-POINTED: the site is part of the body now (see `SITE`).
      site_id: SITE,
      hostname: "core-sw-01",
      primary_ip: "172.22.104.1",
      asset_type: "network",
      lifecycle: "unmanageable",
    });
  });

  it("files the asset under the site it was given", async () => {
    renderDialog();
    await userEvent.type(screen.getByLabelText(/description/i), "boxed laptop, PO 4471");
    await userEvent.click(screen.getByRole("button", { name: /add asset/i }));

    const body = createAsset.mock.calls[0]?.[0] as Record<string, unknown>;
    expect(body.site_id).toBe(SITE);
  });

  it("sends nothing, and says why, while it does not know the site", async () => {
    // The site comes from `/me` (the internal client's "Principal" site has
    // the organisation's id). Before `/me` has answered, or if it failed,
    // there is no site to name -- and a body without one is a 422 the
    // operator could do nothing about.
    renderDialog(() => {}, null);
    await userEvent.type(screen.getByLabelText(/description/i), "boxed laptop, PO 4471");
    await userEvent.click(screen.getByRole("button", { name: /add asset/i }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/account has not loaded/i);
    expect(createAsset).not.toHaveBeenCalled();
  });

  it("records the DNS name, OS and processor a push needs, and says why", async () => {
    // Windows push is Kerberos only: a ticket is issued for a NAME, never an
    // address, so a machine that has never run the agent needs its DNS name
    // typed here (design 2026-09-28 §2.3). Push also picks the package from
    // the declared OS and processor, and refuses when either is unset.
    renderDialog();
    const dns = screen.getByLabelText(/^dns name$/i);
    expect(dns).toHaveAccessibleDescription(
      "The computer's full name, e.g. pc1.corp.example.com — Windows push needs it.",
    );
    await userEvent.type(screen.getByLabelText(/^name$/i), "PC1");
    await userEvent.type(dns, "pc1.corp.example.com");
    await userEvent.selectOptions(screen.getByLabelText(/operating system/i), "windows");
    await userEvent.selectOptions(screen.getByLabelText(/processor/i), "amd64");
    await userEvent.click(screen.getByRole("button", { name: /add asset/i }));

    expect(createAsset).toHaveBeenCalledWith(
      expect.objectContaining({
        hostname: "PC1",
        fqdn: "pc1.corp.example.com",
        os_family: "windows",
        architecture: "amd64",
      }),
    );
  });

  it("never offers to set `source`, which the server stamps", async () => {
    // A row that could claim an agent wrote it defeats the one field an
    // operator uses to work out why a row looks wrong.
    renderDialog();
    expect(screen.queryByLabelText(/source/i)).not.toBeInTheDocument();
  });

  it("shows the error rather than closing silently", async () => {
    createAsset.mockRejectedValue(new Error("422 primary_ip is not an address"));
    renderDialog();
    await userEvent.type(screen.getByLabelText(/ip address/i), "not-an-ip");
    await userEvent.click(screen.getByRole("button", { name: /add asset/i }));
    expect(await screen.findByText(/not an address/i)).toBeInTheDocument();
  });
});

/**
 * What the operator is told when the server refuses (UX remediation cycle 4).
 *
 * This dialog rendered `String(create.error)`, which is three different
 * defects at once: a bodyless 503 -- the audit's BLOCKER -- becomes the single
 * word "Error"; a FastAPI 422 becomes its raw Pydantic JSON on the screen; and
 * neither carries a `role`, so somebody who pressed the button and is looking
 * at the field they just typed in is told nothing at all.
 */
describe("what the operator is told when it fails", () => {
  it("announces a bodyless 503 as a sentence rather than the word Error", async () => {
    createAsset.mockRejectedValue(new ApiError(503, ""));
    renderDialog();
    await userEvent.click(screen.getByRole("button", { name: /add asset/i }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/unavailable right now/i);
    expect(alert).toHaveTextContent(/Nothing was changed/i);
    expect(alert.textContent).not.toBe("Error");
  });

  it("translates a Pydantic 422 into the field's name and the problem", async () => {
    createAsset.mockRejectedValue(
      new ApiError(
        422,
        JSON.stringify({
          detail: [
            {
              type: "value_error",
              loc: ["body", "primary_ip"],
              msg: "value is not a valid IPv4 address",
              // The echo of what the user typed. It must not reach the screen.
              input: "not-an-ip",
            },
          ],
        }),
      ),
    );
    renderDialog();
    await userEvent.type(screen.getByLabelText(/ip address/i), "not-an-ip");
    await userEvent.click(screen.getByRole("button", { name: /add asset/i }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Primary ip: value is not a valid IPv4 address");
    // Not the payload, and not the echoed input -- a value echo is how a
    // secret field's contents reach a screenshot (invariant 3's third door).
    expect(alert.textContent).not.toMatch(/value_error|"loc"|\{/);
  });

  it("draws the refusal with the product's error surface, not one of its own", async () => {
    // THE THEME MISMATCH THE OPERATOR REPORTED, AND WHAT IT ACTUALLY WAS.
    // This dialog was the last create form in the module that never adopted
    // the form kernel: it hand-drew its failure line as
    // `<p role="alert" className="mb-2 text-sm text-[var(--red)]">`. That is
    // 14px -- the ARTICLE ramp's H4 -- in `--red`, the FILL token, three feet
    // from "New device group", whose failure line is `.form-error`: 12px in
    // `--red-ink`, the tone the ink family exists to carry. DESIGN.md's
    // console ramp is 17/15/13/12 and says of the three ramps "these do not
    // merge".
    //
    // A class name in JSX is not a component; the rule it selects is -- so
    // this asserts the alert wears the shared rule rather than a hand-drawn
    // copy of roughly the same idea.
    createAsset.mockRejectedValue(new ApiError(503, ""));
    renderDialog();
    await userEvent.click(screen.getByRole("button", { name: /add asset/i }));

    const alert = await screen.findByRole("alert");
    expect(alert.className.split(/\s+/)).toContain("form-error");
    expect(document.querySelector('[class*="text-[var(--red)]"]')).toBeNull();
  });

  it("says what did NOT happen, not only what went wrong", async () => {
    // "The server failed on this request" read as advice about the form
    // rather than as a refusal. The kernel's `failedMessage` names the
    // outcome first, in the SAME alert so it is announced as one thing.
    createAsset.mockRejectedValue(new ApiError(500, ""));
    renderDialog();
    await userEvent.click(screen.getByRole("button", { name: /add asset/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/The asset was not added\./);
  });

  it("does not accuse the operator of a mistake they have not made yet", async () => {
    // The component stays mounted while `open` is false, so the failed
    // mutation's error outlived the dialog: cancel after a 422, reopen, and a
    // fresh untouched form was already showing the last refusal.
    createAsset.mockRejectedValue(new ApiError(422, "primary_ip is not an address"));
    const { close, reopen } = renderDialog();
    await userEvent.type(screen.getByLabelText(/ip address/i), "not-an-ip");
    await userEvent.click(screen.getByRole("button", { name: /add asset/i }));
    await screen.findByRole("alert");

    await userEvent.click(screen.getByRole("button", { name: /cancel/i }));
    close();
    reopen();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

/**
 * The dialog is a real form (UX remediation cycle 3's kernel).
 *
 * Enter did nothing here. `components/ui.tsx`'s Button sets no `type` and
 * there was no `<form>` for one to submit, so the single most natural way to
 * finish a six-field form was inert -- while the same keystroke in "New
 * device group", the next dialog along in this module, worked.
 */
describe("finishing the form from the keyboard", () => {
  it("submits on Enter, the way every other create dialog in the module does", async () => {
    renderDialog();
    await userEvent.type(screen.getByLabelText(/^name$/i), "core-sw-01{Enter}");
    expect(createAsset).toHaveBeenCalledTimes(1);
    expect((createAsset.mock.calls[0]?.[0] as Record<string, unknown>).hostname).toBe(
      "core-sw-01",
    );
  });

  it("does not submit when Cancel is pressed", async () => {
    // `type="button"` is load-bearing inside a real `<form>`: an untyped
    // button submits, so the control whose entire purpose is to ABANDON the
    // form would send it.
    const onClose = vi.fn();
    renderDialog(onClose);
    await userEvent.type(screen.getByLabelText(/^name$/i), "core-sw-01");
    await userEvent.click(screen.getByRole("button", { name: /cancel/i }));
    expect(createAsset).not.toHaveBeenCalled();
    expect(onClose).toHaveBeenCalled();
  });
});
