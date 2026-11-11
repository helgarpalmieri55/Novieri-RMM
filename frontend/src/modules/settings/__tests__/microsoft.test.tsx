/**
 * Settings -> Microsoft sign-in (own-auth phase 2, Task 12, decision 6; plan
 * D33-D34; operator decision C4): the application, Novieri's tenant and the
 * platform's certificate -- shown to be uploaded to Entra, with its expiry,
 * and made anew on request; no secret is ever typed. The redirect address to
 * register; `orgs.manage` on every client to save.
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { setActiveLanguage } from "../../../i18n/core";
import { expectNoEnglish } from "../../../i18n/test-utils";
import { ApiError } from "../../../lib/api";
import { saveBlob } from "../../../lib/download";
import { MICROSOFT_SETTINGS_FIXTURE, apiClient, calls, installDefaults, renderSettings } from "./harness";

vi.mock("../../../lib/ApiContext", () => ({ useApi: () => apiClient() }));
// jsdom follows no download: what is handed to the browser is what is checked.
vi.mock("../../../lib/download", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../../lib/download")>()),
  saveBlob: vi.fn(),
}));

const APP = "3f2504e0-4f89-41d3-9a0c-0305e82c3301";
const TENANT = "9a1b2c3d-4e5f-4a6b-8c7d-0e1f2a3b4c5d";
const PEM = "-----BEGIN CERTIFICATE-----\nMIIBszCCAVmgAwIBAgIU\n-----END CERTIFICATE-----\n";
const WITH_CERTIFICATE = {
  ...MICROSOFT_SETTINGS_FIXTURE,
  app_client_id: APP,
  configured: true,
  certificate: PEM,
  certificate_thumbprint: "q1w2e3r4t5y6u7i8o9p0a1s2d3f4g5h6j7k8l9z0x1c",
  certificate_expires_at: "2027-11-01T09:00:00Z",
};
const NEXT_PEM = "-----BEGIN CERTIFICATE-----\nMIIBtDCCAVqgAwIBAgIU\n-----END CERTIFICATE-----\n";
const WAITING = {
  ...WITH_CERTIFICATE,
  next_certificate: NEXT_PEM,
  next_certificate_thumbprint: "z9x8c7v6b5n4m3l2k1j0h9g8f7d6s5a4p3o2i1u0y9t",
  next_certificate_expires_at: "2028-10-27T09:00:00Z",
};

beforeEach(() => {
  installDefaults();
  vi.mocked(saveBlob).mockClear();
});

/** What a `Blob` holds, read the way a browser would (jsdom's `Blob` has no `text()`). */
function blobText(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsText(blob);
  });
}

describe("Microsoft sign-in", () => {
  it("shows the address to register, and asks for no secret", async () => {
    renderSettings("/settings?tab=microsoft");
    const tab = await screen.findByRole("tabpanel");
    expect(await within(tab).findByDisplayValue(MICROSOFT_SETTINGS_FIXTURE.redirect_uri)).toBeInTheDocument();
    expect(within(tab).queryByLabelText(/secret/i)).not.toBeInTheDocument();
    expect(within(tab).getByText("No certificate yet. Enter the application's ID and ask for one.")).toBeInTheDocument();
  });

  it("shows the certificate to upload to Entra, its thumbprint and when it expires", async () => {
    calls.getMicrosoftSettings.mockResolvedValue(WITH_CERTIFICATE);
    renderSettings("/settings?tab=microsoft");
    const tab = await screen.findByRole("tabpanel");
    expect(await within(tab).findByLabelText("Certificate to upload to the application")).toHaveValue(PEM);
    expect(within(tab).getByText(WITH_CERTIFICATE.certificate_thumbprint)).toBeInTheDocument();
    expect(within(tab).getByText(/Expires/)).toBeInTheDocument();
  });

  it("warns when the certificate has run out", async () => {
    calls.getMicrosoftSettings.mockResolvedValue({ ...WITH_CERTIFICATE, certificate_expires_at: "2020-01-01T00:00:00Z" });
    renderSettings("/settings?tab=microsoft");
    const tab = await screen.findByRole("tabpanel");
    expect(
      await within(tab).findByText(
        "The certificate has expired: nobody can sign in with Microsoft until you ask for a new one, upload it and switch to it.",
      ),
    ).toBeInTheDocument();
  });

  it("does not tell a person to ask for a new certificate when one is already waiting (expired)", async () => {
    calls.getMicrosoftSettings.mockResolvedValue({ ...WAITING, certificate_expires_at: "2020-01-01T00:00:00Z" });
    renderSettings("/settings?tab=microsoft");
    const tab = await screen.findByRole("tabpanel");
    expect(
      await within(tab).findByText(
        "The certificate has expired: nobody can sign in with Microsoft until you upload the new certificate that is waiting and switch to it.",
      ),
    ).toBeInTheDocument();
    expect(within(tab).queryByText(/until you ask for a new one/)).not.toBeInTheDocument();
  });

  it("does not tell a person to ask for a new certificate when one is already waiting (expiring)", async () => {
    const soon = new Date(Date.now() + 10 * 24 * 60 * 60 * 1000).toISOString();
    calls.getMicrosoftSettings.mockResolvedValue({ ...WAITING, certificate_expires_at: soon });
    renderSettings("/settings?tab=microsoft");
    const tab = await screen.findByRole("tabpanel");
    expect(
      await within(tab).findByText(
        "The certificate expires within 30 days. Upload the new certificate that is waiting and switch to it before then.",
      ),
    ).toBeInTheDocument();
    expect(within(tab).queryByText(/Ask for a new one/)).not.toBeInTheDocument();
  });

  it("hands each certificate to the browser under the file name Entra is given", async () => {
    calls.getMicrosoftSettings.mockResolvedValue(WAITING);
    const user = userEvent.setup();
    renderSettings("/settings?tab=microsoft");
    const tab = await screen.findByRole("tabpanel");
    await user.click(await within(tab).findByRole("button", { name: "Download the certificate" }));
    await user.click(within(tab).getByRole("button", { name: "Download the new certificate" }));
    const saved = vi.mocked(saveBlob).mock.calls;
    expect(saved.map(([, name]) => name)).toEqual(["itms-sign-in.cer", "itms-sign-in-new.cer"]);
    expect(await blobText(saved[0]![0])).toBe(PEM);
    expect(await blobText(saved[1]![0])).toBe(NEXT_PEM);
  });

  it("says, before another new certificate is made, that it replaces the one waiting", async () => {
    calls.getMicrosoftSettings.mockResolvedValue(WAITING);
    const user = userEvent.setup();
    renderSettings("/settings?tab=microsoft");
    const tab = await screen.findByRole("tabpanel");
    await user.click(await within(tab).findByRole("checkbox", { name: "Make a new certificate when I save" }));
    expect(
      within(tab).getByText("This replaces the new certificate still waiting. Upload the one it makes instead."),
    ).toBeInTheDocument();
    expect(within(tab).queryByText(/The new certificate waits beside the one in use/)).not.toBeInTheDocument();
  });

  it("saves every value, with a first certificate asked for", async () => {
    const user = userEvent.setup();
    renderSettings("/settings?tab=microsoft");
    const tab = await screen.findByRole("tabpanel");
    await user.click(await within(tab).findByRole("switch", { name: "People may sign in with Microsoft" }));
    await user.type(within(tab).getByLabelText("Application (client) ID"), APP);
    await user.type(within(tab).getByLabelText("Novieri's Microsoft organization (tenant ID)"), TENANT);
    await user.click(within(tab).getByRole("checkbox", { name: "Make a new certificate when I save" }));
    await user.click(within(tab).getByRole("button", { name: "Save changes" }));
    await waitFor(() =>
      expect(calls.putMicrosoftSettings).toHaveBeenCalledExactlyOnceWith({
        enabled: true,
        app_client_id: APP,
        staff_tenant_id: TENANT,
        new_certificate: true,
        activate_certificate: false,
      }),
    );
  });

  it("says, before a new certificate is made, that it waits beside the one in use", async () => {
    calls.getMicrosoftSettings.mockResolvedValue(WITH_CERTIFICATE);
    const user = userEvent.setup();
    renderSettings("/settings?tab=microsoft");
    const tab = await screen.findByRole("tabpanel");
    await user.click(await within(tab).findByRole("checkbox", { name: "Make a new certificate when I save" }));
    expect(
      within(tab).getByText(
        "The new certificate waits beside the one in use, which keeps signing people in. Upload it to the application in Entra, then switch to it here.",
      ),
    ).toBeInTheDocument();
  });

  it("shows both certificates while the new one waits, each with its expiry, and switches when told", async () => {
    calls.getMicrosoftSettings.mockResolvedValue(WAITING);
    const user = userEvent.setup();
    renderSettings("/settings?tab=microsoft");
    const tab = await screen.findByRole("tabpanel");
    expect(await within(tab).findByLabelText("Certificate to upload to the application")).toHaveValue(PEM);
    expect(within(tab).getByLabelText("New certificate, waiting: upload it beside the one in use")).toHaveValue(NEXT_PEM);
    expect(within(tab).getByText(WAITING.certificate_thumbprint)).toBeInTheDocument();
    expect(within(tab).getByText(WAITING.next_certificate_thumbprint)).toBeInTheDocument();
    expect(within(tab).getAllByText(/Expires/)).toHaveLength(2);
    expect(
      within(tab).getByText(
        "Switch only after uploading it: from then on sign-in signs with the new certificate, and Entra refuses it until it has it.",
      ),
    ).toBeInTheDocument();
    await user.click(within(tab).getByRole("button", { name: "I uploaded it: switch to the new certificate" }));
    await waitFor(() =>
      expect(calls.putMicrosoftSettings).toHaveBeenCalledExactlyOnceWith({
        enabled: false,
        app_client_id: APP,
        staff_tenant_id: null,
        new_certificate: false,
        activate_certificate: true,
      }),
    );
  });

  it("says that another application stops sign-in with Microsoft until its certificate is uploaded", async () => {
    calls.getMicrosoftSettings.mockResolvedValue(WITH_CERTIFICATE);
    const user = userEvent.setup();
    renderSettings("/settings?tab=microsoft");
    const tab = await screen.findByRole("tabpanel");
    const appId = await within(tab).findByLabelText("Application (client) ID");
    expect(within(tab).queryByText(/Another application needs a certificate of its own/)).not.toBeInTheDocument();
    await user.clear(appId);
    await user.type(appId, TENANT);
    expect(
      within(tab).getByText(
        "Another application needs a certificate of its own, so tick Make a new certificate when I save. From that save on, nobody signs in with Microsoft until you upload it to that application.",
      ),
    ).toBeInTheDocument();
  });

  it("refuses an identifier that is not one before asking", async () => {
    const user = userEvent.setup();
    renderSettings("/settings?tab=microsoft");
    const tab = await screen.findByRole("tabpanel");
    await user.type(await within(tab).findByLabelText("Application (client) ID"), "not-a-guid");
    expect(within(tab).getByText("Each identifier is a GUID, as Microsoft shows it: 8-4-4-4-12 hexadecimal digits.")).toBeInTheDocument();
    expect(calls.putMicrosoftSettings).not.toHaveBeenCalled();
  });

  it("warns a month before the certificate runs out", async () => {
    const soon = new Date(Date.now() + 10 * 24 * 60 * 60 * 1000).toISOString();
    calls.getMicrosoftSettings.mockResolvedValue({ ...WITH_CERTIFICATE, certificate_expires_at: soon });
    renderSettings("/settings?tab=microsoft");
    const tab = await screen.findByRole("tabpanel");
    expect(
      await within(tab).findByText(
        "The certificate expires within 30 days. Ask for a new one, upload it and switch to it before then.",
      ),
    ).toBeInTheDocument();
  });

  it("does not switch certificates while the form has unsaved changes", async () => {
    calls.getMicrosoftSettings.mockResolvedValue(WAITING);
    const user = userEvent.setup();
    renderSettings("/settings?tab=microsoft");
    const tab = await screen.findByRole("tabpanel");
    const activate = await within(tab).findByRole("button", { name: "I uploaded it: switch to the new certificate" });
    expect(activate).toBeEnabled();
    await user.type(within(tab).getByLabelText("Novieri's Microsoft organization (tenant ID)"), TENANT);
    expect(activate).toBeDisabled();
  });

  it("says the server's sentence when two first saves meet (409)", async () => {
    calls.putMicrosoftSettings.mockRejectedValue(
      new ApiError(409, JSON.stringify({ detail: "another save happened at the same time; reload and try again" })),
    );
    const user = userEvent.setup();
    renderSettings("/settings?tab=microsoft");
    const tab = await screen.findByRole("tabpanel");
    await user.type(await within(tab).findByLabelText("Application (client) ID"), APP);
    await user.click(within(tab).getByRole("button", { name: "Save changes" }));
    expect(await within(tab).findByRole("alert")).toHaveTextContent(
      "another save happened at the same time; reload and try again",
    );
  });

  it("speaks Spanish, both certificates and the switch included", async () => {
    calls.getMicrosoftSettings.mockResolvedValue(WAITING);
    setActiveLanguage("es");
    const { container } = renderSettings("/settings?tab=microsoft");
    const tab = await screen.findByRole("tabpanel");
    expect(await within(tab).findByLabelText("Certificado para subir a la aplicación")).toHaveValue(PEM);
    expect(within(tab).getByLabelText("Certificado nuevo, en espera: súbelo junto al que está en uso")).toHaveValue(NEXT_PEM);
    expect(within(tab).getByRole("button", { name: "Ya lo subí: cambiar al certificado nuevo" })).toBeEnabled();
    expect(within(tab).getByRole("switch", { name: "Se puede entrar con Microsoft" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Entrar con Microsoft" })).toHaveAttribute("aria-selected", "true");
    expectNoEnglish(container, ["Toeshee Group", "Operator"]);
  });

  it("is not offered without orgs.manage", async () => {
    installDefaults(["users.read"]);
    renderSettings();
    await screen.findByRole("tab", { name: /Organization/ });
    expect(screen.queryByRole("tab", { name: /Microsoft sign-in/ })).not.toBeInTheDocument();
    expect(calls.getMicrosoftSettings).not.toHaveBeenCalled();
  });
});
