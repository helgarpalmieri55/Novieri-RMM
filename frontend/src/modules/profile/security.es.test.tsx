/**
 * Your security in Spanish: the six panels, their states and their buttons, a
 * removal's confirmation and the recent confirmation's dialog. The passkeys'
 * names, the devices' labels, the browsers' own strings and the addresses are
 * recorded data and read as recorded.
 */

import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { setActiveLanguage } from "../../i18n/core";
import { expectNoEnglish } from "../../i18n/test-utils";
import { apiClient, calls, installDefaults, renderSecurityPage, stepUpRequired } from "../settings/__tests__/harness";

vi.mock("@simplewebauthn/browser", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@simplewebauthn/browser")>()),
  startRegistration: vi.fn(),
  startAuthentication: vi.fn(),
}));
vi.mock("../../lib/ApiContext", () => ({ useApi: () => apiClient() }));

/** Recorded words the fixture puts on screen. "Phone" is also an English catalogue word. */
const RECORDED = ["Work laptop", "Phone", "Firefox on Windows", "Safari on macOS"];

beforeEach(() => {
  installDefaults();
  setActiveLanguage("es");
});

describe("your security in Spanish", () => {
  it("words the six panels, their states and their buttons", async () => {
    const { container } = renderSecurityPage();
    expect(await screen.findByRole("heading", { name: "Claves de acceso" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Tu seguridad" })).toBeInTheDocument();
    for (const title of ["Contraseña", "Aplicación de autenticación", "Códigos de recuperación", "Equipos recordados", "Sesiones"]) {
      expect(screen.getByRole("heading", { name: title })).toBeInTheDocument();
    }
    expect(screen.getByLabelText("Contraseña actual")).toBeInTheDocument();
    expect(screen.getByLabelText("Repite la contraseña nueva")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cambiar contraseña" })).toBeInTheDocument();
    expect(screen.getByText("Sincronizada")).toBeInTheDocument();
    expect(screen.getByText("Configurada")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Pasar a otra aplicación" })).toBeInTheDocument();
    expect(screen.getByText("Quedan 8 de 10")).toBeInTheDocument();
    expect(screen.getByText("Este equipo")).toBeInTheDocument();
    expect(screen.getByText("Esta sesión")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Olvidar Safari on macOS" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cerrar la sesión desde 10.0.0.9" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Añadir una clave de acceso" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Generar códigos nuevos" })).toBeInTheDocument();
    expectNoEnglish(container, RECORDED);
  });

  it("words a removal's confirmation, the recent confirmation and its cancellation", async () => {
    calls.removePasskey.mockRejectedValue(stepUpRequired());
    const user = userEvent.setup();
    renderSecurityPage();
    await user.click(await screen.findByRole("button", { name: "Quitar la clave de acceso Work laptop" }));
    const confirm = await screen.findByRole("dialog", { name: "Quitar clave de acceso" });
    expect(within(confirm).getByText(/^Ya no podrás iniciar sesión con ella\./)).toBeInTheDocument();
    await user.click(within(confirm).getByRole("button", { name: "Quitar clave de acceso" }));

    const stepUp = await screen.findByRole("dialog", { name: "Confirma que eres tú" });
    expect(await within(stepUp).findByLabelText("Código de tu aplicación de autenticación")).toBeInTheDocument();
    expect(within(stepUp).getByRole("button", { name: "Usar una clave de acceso" })).toBeInTheDocument();
    expect(within(stepUp).getByText("Usar un código de recuperación")).toBeInTheDocument();
    await user.click(within(stepUp).getByRole("button", { name: "Cancelar" }));
    expect(await screen.findByText("No se cambió nada: se cerró la confirmación.")).toBeInTheDocument();
  });

  it("asks for the second factor without claiming when it was last confirmed", async () => {
    // FIX ROUND (Task 12 review, Minor 6).
    calls.removePasskey.mockRejectedValue(stepUpRequired());
    const user = userEvent.setup();
    renderSecurityPage();
    await user.click(await screen.findByRole("button", { name: "Quitar la clave de acceso Work laptop" }));
    await user.click(
      within(await screen.findByRole("dialog", { name: "Quitar clave de acceso" })).getByRole("button", {
        name: "Quitar clave de acceso",
      }),
    );
    const stepUp = await screen.findByRole("dialog", { name: "Confirma que eres tú" });
    expect(within(stepUp).getByText("Confirma tu segundo factor para continuar.")).toBeInTheDocument();
    expect(stepUp).not.toHaveTextContent(/minutos/);
  });

  it("says what a password change ended, in Spanish", async () => {
    // FIX ROUND (Task 12 review, Minor 5): the server ends the other sessions too.
    const user = userEvent.setup();
    renderSecurityPage();
    await user.type(await screen.findByLabelText("Contraseña actual"), "una frase antigua mía");
    await user.type(screen.getByLabelText("Contraseña nueva"), "caballo correcto grapa batería");
    await user.type(screen.getByLabelText("Repite la contraseña nueva"), "caballo correcto grapa batería");
    await user.click(screen.getByRole("button", { name: "Cambiar contraseña" }));
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Se cambió tu contraseña. Se cerraron tus otras sesiones y los equipos que tenías recordados vuelven a pedir tu segundo factor.",
    );
  });
});
