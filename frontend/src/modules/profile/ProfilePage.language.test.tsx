import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { getLanguage, setActiveLanguage } from "../../i18n/core";
import { ALL_PERMISSIONS, ME_FIXTURE, apiClient, calls, installDefaults, renderProfilePage } from "../settings/__tests__/harness";

vi.mock("../../lib/auth", () => ({ accessToken: () => "test-token", startAccountAction: vi.fn() }));
vi.mock("../../lib/ApiContext", () => ({ useApi: () => apiClient() }));

beforeEach(() => {
  installDefaults();
  setActiveLanguage("en");
  try { localStorage.clear(); } catch { /* ignore */ }
});

describe("Your profile: language", () => {
  it("offers English and Español, saves the choice once and switches the console", async () => {
    calls.me.mockResolvedValue({ ...ME_FIXTURE, permissions: [...ALL_PERMISSIONS] });
    calls.updateMe.mockResolvedValue({ ...ME_FIXTURE, locale: "es" });
    renderProfilePage();
    const select = await screen.findByLabelText<HTMLSelectElement>("Language");
    expect([...select.options].map((o) => o.value)).toEqual(["en", "es"]);
    await userEvent.selectOptions(select, "es");
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(getLanguage()).toBe("es"));
    expect(calls.updateMe).toHaveBeenCalledTimes(1);
    expect(calls.updateMe).toHaveBeenCalledWith(expect.objectContaining({ locale: "es" }));
  });

  it("an unrelated save does not change the language when the account has none saved", async () => {
    // Case A: no saved locale, the console is in Spanish because of the browser.
    setActiveLanguage("es");
    calls.me.mockResolvedValue({ ...ME_FIXTURE, locale: null, permissions: [...ALL_PERMISSIONS] });
    calls.updateMe.mockResolvedValue({ ...ME_FIXTURE, locale: null, phone: "+57 1" });
    renderProfilePage();
    const select = await screen.findByLabelText<HTMLSelectElement>("Idioma");
    expect(select).toHaveValue("es");
    // The console is in Spanish here, so the field carries its Spanish name now
    // that the whole page is translated (this test predated that).
    const phone = await screen.findByLabelText("Teléfono");
    await userEvent.clear(phone);
    await userEvent.type(phone, "+57 1");
    await userEvent.click(screen.getByRole("button", { name: "Guardar cambios" }));
    await waitFor(() => expect(calls.updateMe).toHaveBeenCalledTimes(1));
    expect(calls.updateMe.mock.calls[0]![0]).toMatchObject({ phone: "+57 1", locale: null });
    expect(getLanguage()).toBe("es");
  });

  it("an unsupported saved language shows the language in use, not English", async () => {
    setActiveLanguage("es");
    calls.me.mockResolvedValue({ ...ME_FIXTURE, locale: "fr-FR", permissions: [...ALL_PERMISSIONS] });
    renderProfilePage();
    expect(await screen.findByLabelText<HTMLSelectElement>("Idioma")).toHaveValue("es");
  });

  it("says so, without blocking, when the sign-in pages could not be switched", async () => {
    calls.me.mockResolvedValue({ ...ME_FIXTURE, permissions: [...ALL_PERMISSIONS] });
    // The refetch after the save serves what was saved, as the real API does.
    calls.updateMe.mockImplementation(async () => {
      calls.me.mockResolvedValue({ ...ME_FIXTURE, locale: "es", permissions: [...ALL_PERMISSIONS] });
      return { ...ME_FIXTURE, locale: "es", identity_locale_synced: false };
    });
    renderProfilePage();
    const select = await screen.findByLabelText<HTMLSelectElement>("Language");
    await userEvent.selectOptions(select, "es");
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));
    // The save went through and the console switched, in Spanish.
    await waitFor(() => expect(getLanguage()).toBe("es"));
    const note = await screen.findByText(/Guardado\. Las páginas de inicio de sesión no se pudieron cambiar/);
    expect(note.closest("p")).toHaveAttribute("role", "status");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("shows the English note when the console switches to English", async () => {
    setActiveLanguage("es");
    calls.me.mockResolvedValue({ ...ME_FIXTURE, locale: "es", permissions: [...ALL_PERMISSIONS] });
    calls.updateMe.mockImplementation(async () => {
      calls.me.mockResolvedValue({ ...ME_FIXTURE, locale: "en", permissions: [...ALL_PERMISSIONS] });
      return { ...ME_FIXTURE, locale: "en", identity_locale_synced: false };
    });
    renderProfilePage();
    const select = await screen.findByLabelText<HTMLSelectElement>("Idioma");
    await userEvent.selectOptions(select, "en");
    await userEvent.click(screen.getByRole("button", { name: "Guardar cambios" }));
    await waitFor(() => expect(getLanguage()).toBe("en"));
    expect(
      await screen.findByText(/Saved\. The sign-in pages could not be switched to this language/),
    ).toBeInTheDocument();
  });

  it("shows no note when the sign-in pages followed", async () => {
    calls.me.mockResolvedValue({ ...ME_FIXTURE, permissions: [...ALL_PERMISSIONS] });
    calls.updateMe.mockImplementation(async () => {
      calls.me.mockResolvedValue({ ...ME_FIXTURE, locale: "es", permissions: [...ALL_PERMISSIONS] });
      return { ...ME_FIXTURE, locale: "es", identity_locale_synced: true };
    });
    renderProfilePage();
    await userEvent.selectOptions(await screen.findByLabelText<HTMLSelectElement>("Language"), "es");
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(getLanguage()).toBe("es"));
    await waitFor(() => expect(calls.me.mock.calls.length).toBeGreaterThan(1));
    expect(screen.queryByText(/sign-in pages could not be switched/)).toBeNull();
    expect(screen.queryByText(/páginas de inicio de sesión no se pudieron/)).toBeNull();
  });
});
