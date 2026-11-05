/**
 * Your profile in Spanish: the form, the refusal of a clean save, the link to
 * your security, the loading and failed-to-load states. The save bar
 * (`modules/settings/SaveBar`) is shared with the Settings page and speaks
 * Spanish with it, so these renders take no allowance for it.
 *
 * RE-POINTED (own-auth Tasks 10 and 12): the three Keycloak credential steps
 * and their failure became one link to the platform's own page, first named
 * "Administrar el inicio de sesión y la seguridad" and now "Abrir tu
 * seguridad", after the page it opens.
 */

import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { setActiveLanguage } from "../../i18n/core";
import { expectNoEnglish } from "../../i18n/test-utils";
import { ApiError } from "../../lib/api";
import {
  ALL_PERMISSIONS,
  ME_FIXTURE,
  apiClient,
  calls,
  installDefaults,
  renderProfilePage,
} from "../settings/__tests__/harness";

vi.mock("../../lib/ApiContext", () => ({ useApi: () => apiClient() }));

beforeEach(() => {
  installDefaults();
  setActiveLanguage("es");
  calls.me.mockResolvedValue({ ...ME_FIXTURE, locale: "es", permissions: [...ALL_PERMISSIONS] });
});

describe("your profile in Spanish", () => {
  it("shows the form, the notes and the way to your security in Spanish", async () => {
    const { container } = renderProfilePage();
    expect(screen.getByText("Cargando…")).toBeInTheDocument();
    expect(await screen.findByLabelText("Nombre para mostrar")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Tu perfil" })).toBeInTheDocument();
    expect(screen.getByLabelText("Teléfono")).toBeInTheDocument();
    expect(screen.getByLabelText("Zona horaria")).toHaveAttribute("placeholder", "p. ej., Europe/Lisbon");
    expect(screen.getByLabelText("Idioma")).toHaveValue("es");
    expect(screen.getByText(/Has iniciado sesión como operator@example\.com\./)).toBeInTheDocument();
    expect(screen.getByText(/Nada en esta plataforma envía SMS/)).toBeInTheDocument();
    expect(screen.getByText("Inicio de sesión y seguridad")).toBeInTheDocument();
    expect(screen.getByText("Tu contraseña, tus segundos factores, tus equipos recordados y tus sesiones")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Abrir tu seguridad" })).toBeInTheDocument();
    expectNoEnglish(container, ["Operator"]);
  });

  it("refuses a clean save in Spanish", async () => {
    const user = userEvent.setup();
    renderProfilePage();
    await screen.findByLabelText("Nombre para mostrar");
    await user.click(screen.getByRole("button", { name: "Guardar cambios" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("No hay cambios sin guardar");
    expect(calls.updateMe).not.toHaveBeenCalled();
  });

  it("links to your security in Spanish, and to nowhere else", async () => {
    // RE-POINTED from "says in Spanish when a credential step cannot be
    // started" (Task 10: "says in Spanish where sign-in and security are
    // managed"): there is no step at another application to fail to start.
    const { container } = renderProfilePage();
    const link = await screen.findByRole("link", { name: "Abrir tu seguridad" });
    expect(link).toHaveAttribute("href", "/profile/security");
    expect(screen.getByText("Inicio de sesión y seguridad")).toBeInTheDocument();
    expect(screen.getByText(/^Cambia tu contraseña, añade o quita una clave de acceso/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expectNoEnglish(container, ["Operator"]);
  });

  it("says in Spanish why the profile did not load", async () => {
    calls.me.mockRejectedValue(new ApiError(403, ""));
    const { container } = renderProfilePage();
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/permiso/i);
    expect(screen.queryByLabelText("Nombre para mostrar")).not.toBeInTheDocument();
    expectNoEnglish(container);
  });
});
