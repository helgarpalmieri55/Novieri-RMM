/**
 * Your profile in Spanish: the form, the refusal of a clean save, the three
 * credential steps and their failure, the loading and failed-to-load states.
 * The save bar (`modules/settings/SaveBar`) is shared with the Settings page
 * and speaks Spanish with it, so these renders take no allowance for it.
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

const { startAccountAction } = vi.hoisted(() => ({ startAccountAction: vi.fn() }));

vi.mock("../../lib/auth", () => ({ accessToken: () => "test-token", startAccountAction }));
vi.mock("../../lib/ApiContext", () => ({ useApi: () => apiClient() }));

beforeEach(() => {
  installDefaults();
  startAccountAction.mockReset();
  startAccountAction.mockResolvedValue(undefined);
  setActiveLanguage("es");
  calls.me.mockResolvedValue({ ...ME_FIXTURE, locale: "es", permissions: [...ALL_PERMISSIONS] });
});

describe("your profile in Spanish", () => {
  it("shows the form, the notes and the credential steps in Spanish", async () => {
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
    expect(screen.getByRole("button", { name: "Cambiar contraseña" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Administrar dispositivo MFA" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Añadir una clave de acceso" })).toBeInTheDocument();
    expect(screen.getByText(/Primero se te pedirá tu contraseña actual/)).toBeInTheDocument();
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

  it("says in Spanish when a credential step cannot be started", async () => {
    const user = userEvent.setup();
    startAccountAction.mockRejectedValue(new Error("proveedor caído"));
    const { container } = renderProfilePage();
    await user.click(await screen.findByRole("button", { name: "Cambiar contraseña" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("No se pudo iniciar ese paso, así que no se cambió nada de tu inicio de sesión.");
    expect(alert).toHaveTextContent("proveedor caído");
    expect(startAccountAction).toHaveBeenCalledWith("UPDATE_PASSWORD");
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
