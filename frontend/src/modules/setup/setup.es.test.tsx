/**
 * First boot in Spanish: the claim form, its refusal, and the completed screen.
 * The server's own words (a refusal's `detail`, an invitation error) are shown
 * as the server sent them; everything the page owns is Spanish.
 */

import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderInSpanish, expectNoEnglish } from "../../i18n/test-utils";

const signIn = vi.fn();
vi.mock("../../lib/auth", () => ({
  signIn: () => signIn(),
  accessToken: () => null,
}));

const { SetupPage } = await import("./SetupPage");

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
  signIn.mockReset().mockResolvedValue(undefined);
  vi.stubGlobal("fetch", vi.fn(async () => respond(claimReply)));
});

afterEach(() => {
  vi.unstubAllGlobals();
});

async function fillTheForm(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText("Código de instalación"), "codigo");
  await user.type(screen.getByLabelText("Tu nombre"), "Dana Okafor");
  await user.type(screen.getByLabelText("Tu correo electrónico"), "dana@example.com");
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
    expect(screen.getByText(/Recibirás un enlace para elegir una contraseña/)).toBeInTheDocument();
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

  it("shows the completed screen in Spanish, with the sign-in failure and a failed invitation", async () => {
    const user = userEvent.setup();
    signIn.mockRejectedValue(new Error("proveedor caído"));
    claimReply = { status: 201, body: { ...CLAIMED, invitation_sent: false, invitation_error: null } };
    const { container } = renderInSpanish(<SetupPage />);
    await fillTheForm(user);
    await user.click(screen.getByRole("button", { name: "Finalizar configuración" }));

    expect(await screen.findByRole("heading", { name: "Configuración completada" })).toBeInTheDocument();
    expect(screen.getByText("dana@example.com", { selector: "b" })).toBeInTheDocument();
    expect(screen.getByText(/No se pudo enviar el correo de invitación/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Ir a iniciar sesión" }));
    await vi.waitFor(() => expect(screen.getAllByRole("alert")).toHaveLength(2));
    expect(screen.getByText(/No se pudo iniciar sesión\. .*proveedor caído/)).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("says where to find the invitation when it was sent", async () => {
    const user = userEvent.setup();
    claimReply = { status: 201, body: CLAIMED };
    const { container } = renderInSpanish(<SetupPage />);
    await fillTheForm(user);
    await user.click(screen.getByRole("button", { name: "Finalizar configuración" }));
    expect(await screen.findByText(/Revisa esa bandeja de entrada/)).toBeInTheDocument();
    expectNoEnglish(container);
  });
});
