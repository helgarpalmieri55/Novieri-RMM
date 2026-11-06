/**
 * First boot in Spanish: the claim form with the owner's password, its
 * refusals, and the way on to sign in. The server's own words (a refusal's
 * `detail`) are shown as the server sent them; everything the page owns is
 * Spanish.
 */

import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderInSpanish, expectNoEnglish } from "../../i18n/test-utils";

const { navigation } = await import("../../lib/session");
const { SetupPage } = await import("./SetupPage");

let assign: ReturnType<typeof vi.spyOn>;

type Reply = { status: number; body: unknown };
let claimReply: Reply;

function respond({ status, body }: Reply): Response {
  return {
    ok: status < 400,
    status,
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  } as Response;
}

beforeEach(() => {
  assign = vi.spyOn(navigation, "assign").mockImplementation(() => {});
  vi.stubGlobal("fetch", vi.fn(async () => respond(claimReply)));
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function fillTheForm(user: ReturnType<typeof userEvent.setup>, password = "una frase larga que recuerdo") {
  await user.type(screen.getByLabelText("Código de instalación"), "codigo");
  await user.type(screen.getByLabelText("Tu nombre"), "Dana Okafor");
  await user.type(screen.getByLabelText("Tu correo electrónico"), "dana@example.com");
  await user.type(screen.getByLabelText("Contraseña nueva"), password);
  await user.type(screen.getByLabelText("La misma contraseña otra vez"), password);
  await user.type(screen.getByLabelText("Nombre de la organización"), "Toeshee");
}

const CLAIMED = {
  organization_id: "00000000-0000-0000-0000-0000000000aa",
  organization_slug: "toeshee",
  user_id: "00000000-0000-0000-0000-0000000000bb",
  email: "dana@example.com",
  invitation_sent: true,
  invitation_error: null,
};

describe("the setup screen in Spanish", () => {
  it("shows the claim form in Spanish, and a 422 in Spanish", async () => {
    const user = userEvent.setup();
    const { container } = renderInSpanish(<SetupPage />);
    expect(screen.getByRole("heading", { name: "Configura esta instalación" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Finalizar configuración" })).toBeDisabled();
    expect(screen.getByText(/El primer administrador inicia sesión con este correo y esta contraseña/)).toBeInTheDocument();
    expectNoEnglish(container);

    await fillTheForm(user);
    claimReply = { status: 422, body: { detail: [{ type: "missing", loc: ["body", "code"], msg: "Field required" }] } };
    await user.click(screen.getByRole("button", { name: "Finalizar configuración" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Este campo es obligatorio.");
    // "Code" is the wire field `code`, humanised as the prefix of the 422 sentence
    // (form.tsx humaniseField); the budget module's "Code" label made it a catalogue fragment.
    expectNoEnglish(container, ["Code"]);
  });

  it("says in Spanish when the server cannot be reached", async () => {
    const user = userEvent.setup();
    vi.stubGlobal("fetch", vi.fn(async () => Promise.reject(new TypeError("red caída"))));
    const { container } = renderInSpanish(<SetupPage />);
    await fillTheForm(user);
    await user.click(screen.getByRole("button", { name: "Finalizar configuración" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("No se pudo contactar con el servidor, así que no se creó nada.");
    expectNoEnglish(container);
  });

  // RE-POINTED (own-auth Task 10, then the coordinator's Task 11 decision):
  // the completed screen, its failed invitation and its "Ir a iniciar sesión"
  // button are gone -- the owner has a password and goes to sign in at once.
  it("goes to sign in once the installation is claimed", async () => {
    const user = userEvent.setup();
    claimReply = { status: 201, body: CLAIMED };
    const { container } = renderInSpanish(<SetupPage />);
    await fillTheForm(user);
    await user.click(screen.getByRole("button", { name: "Finalizar configuración" }));
    await vi.waitFor(() => expect(assign).toHaveBeenCalledWith("/sign-in"));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expectNoEnglish(container);
  });

  // RE-POINTED: was "says where to find the invitation when it was sent".
  it("refuses a short password in Spanish, before claiming anything", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(async () => respond(claimReply));
    vi.stubGlobal("fetch", fetchMock);
    const { container } = renderInSpanish(<SetupPage />);
    await fillTheForm(user, "corta");
    await user.click(screen.getByRole("button", { name: "Finalizar configuración" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Usa al menos 12 caracteres.");
    expect(fetchMock).not.toHaveBeenCalled();
    expectNoEnglish(container);
  });
});
