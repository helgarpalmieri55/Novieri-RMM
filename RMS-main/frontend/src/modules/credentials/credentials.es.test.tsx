/**
 * The credentials page in Spanish: the list, its tiles and tabs, the
 * checkout and delete dialogs, the create form for each kind of type, and
 * the prerequisites panel. What is STORED (a credential's name, the account
 * the operator types) reads as recorded; the labels around it are Spanish.
 * No secret value is ever part of a catalogue string (invariant 3): the
 * checkout test reveals a fixture secret and asserts it appears only as the
 * value the API returned.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { en } from "../../i18n/en";
import { es } from "../../i18n/es";
import { leaves } from "../../i18n/test-utils";
import { setActiveLanguage } from "../../i18n/core";
import { expectNoEnglish, renderInSpanish } from "../../i18n/test-utils";
import type { ApiClient, Credential, Me } from "../../lib/api";
import { CredentialsPage } from "./CredentialsPage";
import { credentialTypeDescription, credentialTypeLabel, credentialTypeNoun, CREDENTIAL_TYPES } from "./credentialTypes";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

function credential(overrides: Partial<Credential> = {}): Credential {
  return {
    id: "11111111-1111-1111-1111-111111111111",
    name: "Domain support admin",
    type: "windows_admin",
    device_group_id: null,
    created_at: "2026-08-01T00:00:00Z",
    last_used_at: null,
    has_secret: true,
    ...overrides,
  };
}

const ME: Me = { permissions: ["credentials.manage", "agent.push.scope.manage"] } as Me;
const LIST = [
  credential(),
  credential({ id: "2", name: "Linux support key", type: "ssh_key", last_used_at: "2026-08-15T09:00:00Z" }),
  credential({ id: "3", name: "Branch push", type: "push_install", has_secret: false }),
];

function renderPage(overrides: Partial<ApiClient> = {}, entry = "/credentials") {
  const api: Partial<ApiClient> = {
    me: vi.fn().mockResolvedValue(ME),
    listCredentials: vi.fn().mockResolvedValue(LIST),
    listPushCredentialScopes: vi.fn().mockResolvedValue([]),
    ...overrides,
  };
  mockUseApi.mockReturnValue(api as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = renderInSpanish(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[entry]}>
        <CredentialsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...view, api };
}

beforeEach(() => {
  mockUseApi.mockReset();
});

describe("the credentials page in Spanish", () => {
  it("words the list, tiles, tabs and prerequisites in Spanish and leaves stored names alone", async () => {
    const { container } = renderPage();
    expect(await screen.findByText("Domain support admin")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Credenciales" })).toBeInTheDocument();
    expect(screen.getByText("Contraseñas y claves para sesiones remotas y aprovisionamiento, almacenadas cifradas.")).toBeInTheDocument();
    // Tiles.
    expect(screen.getByText("Credenciales guardadas")).toBeInTheDocument();
    expect(screen.getByText("1 retirada al menos una vez")).toBeInTheDocument();
    expect(screen.getByText("Tipos en uso")).toBeInTheDocument();
    expect(screen.getByText("5 tipos disponibles")).toBeInTheDocument();
    // Tabs, with their counts.
    expect(screen.getByRole("tab", { name: /Todas las credenciales/ })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Nunca retiradas/ })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /^Retiradas/ })).toBeInTheDocument();
    // The table.
    const table = screen.getByRole("table");
    for (const header of ["Nombre", "Estado", "Último uso", "Acciones"]) {
      expect(within(table).getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    expect(within(table).getByText("Administrador de Windows")).toBeInTheDocument();
    expect(within(table).getByText("Clave SSH")).toBeInTheDocument();
    expect(within(table).getByText("Instalación remota")).toBeInTheDocument();
    expect(within(table).getAllByText("En la bóveda")).toHaveLength(2);
    expect(within(table).getByText("Sin secreto")).toBeInTheDocument();
    expect(within(table).getAllByText("Nunca retirada")).toHaveLength(2);
    expect(within(table).getAllByRole("button", { name: "Retirar" })).toHaveLength(3);
    expect(screen.getByRole("button", { name: "Nueva credencial" })).toBeInTheDocument();
    expect(screen.getByText("Cómo se protegen los secretos")).toBeInTheDocument();
    expect(screen.getByText(/Retira cualquier credencial desde su fila/)).toBeInTheDocument();
    // The prerequisites panel for an unassigned push install credential.
    expect(await screen.findByText("Antes de poder usar una credencial guardada")).toBeInTheDocument();
    expect(screen.getByText(/Branch push no está asignada a ningún rango de direcciones/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Asignarla a un rango" })).toBeInTheDocument();
    // "Domain" is the word that opens the recorded credential name "Domain support admin".
    expectNoEnglish(container, ["Branch push", "Domain support admin", "Domain", "Linux support key"]);
  });

  it("filters by tab and words the empty states", async () => {
    const user = userEvent.setup();
    const { container } = renderPage({ listCredentials: vi.fn().mockResolvedValue([credential()]) }, "/credentials?state=used");
    expect(await screen.findByText("Ninguna credencial coincide con esta vista.")).toBeInTheDocument();
    expect(screen.getByRole("tabpanel", { name: "Retiradas" })).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: /Todas las credenciales/ }));
    expect(await screen.findByText("Domain support admin")).toBeInTheDocument();
    // "Domain" opens the recorded credential name.
    expectNoEnglish(container, ["Domain support admin", "Domain"]);
  });

  it("says in Spanish that the vault is empty", async () => {
    const { container } = renderPage({ listCredentials: vi.fn().mockResolvedValue([]) });
    expect(await screen.findByText("No hay credenciales guardadas.")).toBeInTheDocument();
    expect(screen.getByText("La bóveda está vacía")).toBeInTheDocument();
    expect(screen.getByText(/Guarda una credencial más arriba/)).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("asks for a reason in Spanish, refuses a short one, and reveals once", async () => {
    const user = userEvent.setup();
    const checkoutCredential = vi.fn().mockResolvedValue({ secret: "fixture-secret-not-a-real-value" });
    renderPage({ checkoutCredential });
    await screen.findByText("Domain support admin");
    await user.click(screen.getAllByRole("button", { name: "Retirar" })[0]!);
    const dialog = await screen.findByRole("dialog", { name: "Retirar Domain support admin" });
    expect(within(dialog).getByText("Cada retirada queda registrada en el registro de auditoría. Indica un motivo.")).toBeInTheDocument();
    expect(within(dialog).getByPlaceholderText("p. ej., ticket de la mesa de ayuda 4821")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Revelar" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("El campo Motivo es obligatorio");
    await user.type(within(dialog).getByLabelText("Motivo"), "abc");
    await user.click(within(dialog).getByRole("button", { name: "Revelar" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "El motivo se escribe en el registro de auditoría: indica al menos una referencia de ticket",
    );
    expect(checkoutCredential).not.toHaveBeenCalled();
    // "Domain" opens the recorded credential name in the dialog's title.
    expectNoEnglish(dialog, ["Domain"]);

    await user.type(within(dialog).getByLabelText("Motivo"), "-4821");
    await user.click(within(dialog).getByRole("button", { name: "Revelar" }));
    const revealed = await screen.findByRole("dialog", { name: "Domain support admin" });
    expect(within(revealed).getByText("Se muestra una sola vez. Cópialo ahora: no volverá a mostrarse.")).toBeInTheDocument();
    // The secret is the API's value, shown as returned.
    expect(within(revealed).getByText("fixture-secret-not-a-real-value")).toBeInTheDocument();
    expect(within(revealed).getByRole("button", { name: "Copiar" })).toBeInTheDocument();
    expect(within(revealed).getByRole("button", { name: "Listo" })).toBeInTheDocument();
    // "Domain" opens the recorded credential name in the dialog's title.
    expectNoEnglish(revealed, ["fixture-secret-not-a-real-value", "Domain"]);
  });

  it("confirms a delete in Spanish, naming the type, and words a refusal", async () => {
    const user = userEvent.setup();
    const deleteCredential = vi.fn().mockRejectedValue(new Error("nope"));
    renderPage({ deleteCredential, listCredentials: vi.fn().mockResolvedValue([LIST[1]!]) });
    await screen.findByText("Linux support key");
    await user.click(screen.getByRole("button", { name: /Más acciones para Linux support key/ }));
    await user.click(await screen.findByRole("menuitem", { name: "Eliminar" }));
    const dialog = await screen.findByRole("dialog", { name: "Eliminar la credencial guardada" });
    expect(within(dialog).getByText("Eliminar la credencial guardada")).toBeInTheDocument();
    expect(within(dialog).getByText(/que retiren esta credencial \(clave SSH\) fallarán/)).toBeInTheDocument();
    expectNoEnglish(dialog, ["Linux support key"]);
    await user.click(within(dialog).getByRole("button", { name: "Eliminar la credencial" }));
    expect(await screen.findByText(/No se eliminó esa credencial: sigue guardada\./)).toBeInTheDocument();
  });

  it("words the create form, its type hints and the Windows domain warning in Spanish", async () => {
    const user = userEvent.setup();
    const createCredential = vi.fn().mockResolvedValue(credential());
    renderPage({ createCredential });
    await screen.findByText("Domain support admin");
    await user.click(screen.getByRole("button", { name: "Nueva credencial" }));
    const dialog = await screen.findByRole("dialog", { name: "Nueva credencial" });
    expect(within(dialog).getByLabelText("Tipo")).toBeInTheDocument();
    expect(within(dialog).getByRole("option", { name: "Cuenta de dominio de Windows" })).toBeInTheDocument();
    expect(within(dialog).getByText(/Una cuenta de administrador local o de dominio/)).toBeInTheDocument();
    // An empty submit names the field in Spanish.
    await user.click(within(dialog).getByRole("button", { name: "Guardar" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("El campo Nombre es obligatorio");
    expect(createCredential).not.toHaveBeenCalled();
    expectNoEnglish(dialog);

    await user.selectOptions(within(dialog).getByLabelText("Tipo"), "windows_domain");
    expect(within(dialog).getByText("No uses una cuenta de administrador de dominio.")).toBeInTheDocument();
    expect(within(dialog).getByText(/Escribe la cuenta como usuario@tu\.dominio/)).toBeInTheDocument();
    await user.type(within(dialog).getByLabelText("Cuenta (usuario@dominio)"), "svc-push@corp.example.com");
    expect(within(dialog).getByText(/Dominio Kerberos: CORP\.EXAMPLE\.COM/)).toBeInTheDocument();
    expect(within(dialog).getByLabelText("Controlador de dominio (opcional, host o host:puerto)")).toBeInTheDocument();
    expect(within(dialog).getByLabelText("Certificado de la CA (PEM opcional, para la confianza de HTTPS/5986)")).toBeInTheDocument();
    expect(within(dialog).getByPlaceholderText("Se busca mediante DNS si se deja en blanco")).toBeInTheDocument();
    // A malformed account is refused in Spanish.
    await user.clear(within(dialog).getByLabelText("Cuenta (usuario@dominio)"));
    await user.type(within(dialog).getByLabelText("Nombre"), "Corp push");
    await user.type(within(dialog).getByLabelText("Cuenta (usuario@dominio)"), "CORP\\svc");
    await user.type(within(dialog).getByLabelText("Contraseña"), "pw");
    await user.click(within(dialog).getByRole("button", { name: "Guardar" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "La cuenta debe tener el formato usuario@dominio, p. ej., svc-push@corp.example.com",
    );
    // "Active" is the product name Active Directory, written the same in Spanish.
    expectNoEnglish(dialog, ["Corp push", "Active"]);
  });
});

describe("a live language switch", () => {
  it("re-words the page and the type labels on a mounted page", async () => {
    renderPage();
    expect(await screen.findByText("Domain support admin")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Credenciales" })).toBeInTheDocument();
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("heading", { name: "Credentials" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Never checked out/ })).toBeInTheDocument();
    expect(screen.getByText("Windows administrator")).toBeInTheDocument();
    expect(screen.getByText("3 types available".replace("3", "5"))).toBeInTheDocument();
  });
});

describe("a refused form held across a language switch", () => {
  it("re-words the 'required' complaint on a mounted dialog", async () => {
    const user = userEvent.setup();
    renderPage({ createCredential: vi.fn() });
    await screen.findByText("Domain support admin");
    await user.click(screen.getByRole("button", { name: "Nueva credencial" }));
    const dialog = await screen.findByRole("dialog", { name: "Nueva credencial" });
    await user.click(within(dialog).getByRole("button", { name: "Guardar" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("El campo Nombre es obligatorio");
    act(() => setActiveLanguage("en"));
    expect(within(screen.getByRole("dialog", { name: "New credential" })).getByRole("alert")).toHaveTextContent("Name is required");
  });

  it("re-words the checkout reason complaint on a mounted dialog", async () => {
    const user = userEvent.setup();
    renderPage({ checkoutCredential: vi.fn() });
    await screen.findByText("Domain support admin");
    await user.click(screen.getAllByRole("button", { name: "Retirar" })[0]!);
    const dialog = await screen.findByRole("dialog", { name: "Retirar Domain support admin" });
    await user.type(within(dialog).getByLabelText("Motivo"), "abc");
    await user.click(within(dialog).getByRole("button", { name: "Revelar" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("El motivo se escribe en el registro de auditoría");
    act(() => setActiveLanguage("en"));
    expect(within(screen.getByRole("dialog")).getByRole("alert")).toHaveTextContent("Reason is written to the audit log");
  });
});

describe("credential type words", () => {
  it("have a Spanish label, description and sentence noun for every offered type", () => {
    setActiveLanguage("es");
    for (const { value } of CREDENTIAL_TYPES) {
      expect(credentialTypeLabel(value)).toBe(es.enums.CredentialType[value]);
      expect(credentialTypeDescription(value)).toBeTruthy();
      expect(credentialTypeNoun(value)).toBe(es.credentials.types[value].noun);
    }
  });

  it("derive an unknown type's label and keep it out of the descriptions", () => {
    setActiveLanguage("es");
    expect(credentialTypeLabel("smart_card")).toBe("Smart card");
    expect(credentialTypeDescription("smart_card")).toBeNull();
  });

  it("never put a secret value in a catalogue string", () => {
    // Only labels and instructions: nothing that looks like a stored value.
    for (const catalogue of [en.credentials, es.credentials]) {
      for (const [path, value] of leaves(catalogue)) {
        expect(value, path).not.toMatch(/fixture-secret|BEGIN [A-Z ]*PRIVATE KEY/);
      }
    }
  });
});

describe("the credentials page waits for its data", () => {
  it("shows the loading text in Spanish", async () => {
    renderPage({ listCredentials: () => new Promise<never>(() => {}) });
    await waitFor(() => expect(screen.getByText("Cargando…")).toBeInTheDocument());
  });
});
