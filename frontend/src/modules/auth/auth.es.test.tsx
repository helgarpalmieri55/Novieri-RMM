/**
 * The pages reached without a session, in Spanish. Glossary
 * (`i18n/es/index.ts`): a passkey is a «clave de acceso», signing in is
 * «iniciar sesión», a computer is an «equipo».
 */
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { setActiveLanguage } from "../../i18n/core";
import { expectNoEnglish } from "../../i18n/test-utils";
import { ApiError } from "../../lib/api";
import { forgetCodes, handOverCodes } from "./flow";
import { TEN_CODES, authApi, calls, installDefaults, renderAuth } from "./__tests__/harness";

vi.mock("../../lib/ApiContext", () => ({ useApi: () => authApi() }));

vi.mock("@simplewebauthn/browser", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@simplewebauthn/browser")>()),
  browserSupportsWebAuthn: () => true,
}));

beforeEach(() => {
  installDefaults();
  forgetCodes();
  setActiveLanguage("es");
});

describe("the sign-in pages in Spanish", () => {
  it("sign in, with its refusal", async () => {
    calls.login.mockRejectedValue(new ApiError(401, ""));
    const user = userEvent.setup();
    const { container } = renderAuth("/sign-in");
    expect(screen.getByRole("heading", { name: "Iniciar sesión" })).toBeInTheDocument();
    await user.type(screen.getByLabelText("Correo electrónico"), "dana@example.test");
    await user.type(screen.getByLabelText("Contraseña"), "una frase larga");
    await user.click(screen.getByRole("button", { name: "Continuar" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Ese correo electrónico y esa contraseña no corresponden a ninguna cuenta de aquí.",
    );
    expect(screen.getByRole("link", { name: "¿Olvidaste tu contraseña?" })).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("the second factor, passkey first, with the remember box", () => {
    const { container } = renderAuth("/sign-in/factor", { methods: ["passkey", "totp", "recovery"], rememberDays: 15 });
    expect(screen.getByRole("heading", { name: "Confirma que eres tú" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Usar una clave de acceso" })).toBeInTheDocument();
    expect(screen.getByLabelText("Código de seis dígitos de tu aplicación de autenticación")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "No volver a preguntar en este equipo durante 15 días" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Usar un código de recuperación" })).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("the first factor", () => {
    const { container } = renderAuth("/sign-in/enroll");
    expect(screen.getByRole("heading", { name: "Añade un segundo factor" })).toBeInTheDocument();
    expect(screen.getByLabelText("Nombre de esta clave de acceso")).toHaveValue("Mi clave de acceso");
    expect(screen.getByRole("button", { name: "Configurar una aplicación de autenticación" })).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("the recovery codes", () => {
    handOverCodes(TEN_CODES);
    const { container } = renderAuth("/sign-in/recovery-codes");
    expect(screen.getByRole("heading", { name: "Guarda tus códigos de recuperación" })).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "He guardado estos códigos en un lugar seguro" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Continuar a la consola" })).toBeDisabled();
    expectNoEnglish(container);
  });

  it("the forgotten password, and its one answer", async () => {
    const user = userEvent.setup();
    const { container } = renderAuth("/forgot-password");
    await user.type(screen.getByLabelText("Correo electrónico"), "dana@example.test");
    await user.click(screen.getByRole("button", { name: "Enviar el enlace" }));
    expect(await screen.findByRole("status")).toHaveTextContent(/el enlace va de camino/);
    expectNoEnglish(container);
  });

  it("the new password, with a refusal in Spanish", async () => {
    const user = userEvent.setup();
    const { container } = renderAuth("/reset-password#tok");
    await user.type(screen.getByLabelText("Contraseña nueva"), "corta");
    await user.type(screen.getByLabelText("La misma contraseña otra vez"), "corta");
    await user.click(screen.getByRole("button", { name: "Cambiar la contraseña" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Usa al menos 12 caracteres.");
    // The rule is true for a technician and for a contact (final review F, item 1).
    expect(
      screen.getByText(
        "Al menos 12 caracteres (15 para el portal de soporte), los que quieras, espacios incluidos. Una frase que recuerdes funciona bien.",
      ),
    ).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("the server's recovery link", async () => {
    const { container } = renderAuth("/recover#tok");
    expect(await screen.findByRole("heading", { name: "Recupera tu cuenta" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Recuperar mi cuenta" })).toBeInTheDocument();
    expect(screen.getByText(/Opcional: elige ahora una contraseña nueva/)).toBeInTheDocument();
    expect(screen.getByLabelText("Contraseña nueva")).toHaveAttribute("type", "password");
    expectNoEnglish(container);
  });

  it("the server's recovery link, when an emailed link set the password", async () => {
    calls.getRecovery.mockResolvedValue({ email: "owner@example.test", password_required: true });
    const { container } = renderAuth("/recover#tok");
    expect(await screen.findByText(/Tu contraseña se puso por última vez con un enlace enviado por correo/)).toBeInTheDocument();
    expect(screen.getByLabelText("Contraseña nueva")).toHaveAttribute("type", "password");
    expect(screen.getByLabelText("La misma contraseña otra vez")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Recuperar mi cuenta" })).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("the invitation", async () => {
    const { container } = renderAuth("/invite#tok");
    expect(await screen.findByRole("heading", { name: "Te damos la bienvenida, Dana Okafor" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Guardar la contraseña y continuar" })).toBeInTheDocument();
    expectNoEnglish(container, ["Dana Okafor"]);
  });
});
