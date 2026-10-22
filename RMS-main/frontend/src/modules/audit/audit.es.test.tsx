/**
 * The audit log in Spanish: the page's own words (tiles, tabs, filters,
 * headers, the integrity views) and the labels it puts around an entry. What
 * is STORED in an entry (the action code, the ids, the names, the hash) is
 * never rewritten or translated (spec §3.6): the tests below assert it reads
 * exactly as recorded.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { en } from "../../i18n/en";
import { es } from "../../i18n/es";
import { setActiveLanguage } from "../../i18n/core";
import { expectNoEnglish, renderInSpanish } from "../../i18n/test-utils";
import { ApiError } from "../../lib/api";
import type { ApiClient } from "../../lib/api";
import { humaniseEnum } from "../../lib/labels";
import { AuditPage } from "./AuditPage";
import { auditActionLabel, auditActorTypeLabel, auditTargetTypeLabel } from "./labels";
import type { AuditEventOut, AuditListResponse, AuditVerifyResponse } from "./types";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const listAuditEvents = vi.fn();
const verifyAuditChain = vi.fn();
vi.mock("./api", () => ({
  AUDIT_EVENTS_KEY: "audit-events",
  AUDIT_VERIFY_KEY: "audit-verify",
  listAuditEvents: (...args: unknown[]) => listAuditEvents(...args),
  verifyAuditChain: (...args: unknown[]) => verifyAuditChain(...args),
}));

const HASH = "0123456789abcdef0123456789abcdef";

function event(overrides: Partial<AuditEventOut> = {}): AuditEventOut {
  return {
    id: "aaaaaaaa-0000-0000-0000-000000000001",
    ts: "2026-08-16T14:32:18Z",
    actor_type: "user",
    actor_id: "bbbbbbbb-0000-0000-0000-000000000001",
    action: "job.dispatch",
    target_type: "device",
    target_id: "SRV-FS-01",
    metadata: {},
    hash: HASH,
    ...overrides,
  };
}

const ITEMS: AuditListResponse = {
  total: 3,
  items: [
    event(),
    event({ id: "a2", action: "job.dispatch", actor_type: "system", actor_id: null, target_type: "job", target_id: "job-3" }),
    event({
      id: "a3",
      action: "credential.checkout",
      actor_type: "agent",
      actor_display_name: "Helena Ruiz",
      target_type: "credential",
      target_id: "cred-1",
    }),
  ],
};

function renderPage(
  options: {
    events?: () => Promise<AuditListResponse>;
    verify?: () => Promise<AuditVerifyResponse>;
    permissions?: string[];
    at?: string;
  } = {},
) {
  listAuditEvents.mockImplementation(options.events ?? (() => Promise.resolve(ITEMS)));
  verifyAuditChain.mockImplementation(
    options.verify ?? (() => Promise.resolve({ ok: true, count: 3, broken_at: null })),
  );
  mockUseApi.mockReturnValue({
    me: () =>
      Promise.resolve({
        user_id: "u",
        org_id: "o",
        email: "op@example.com",
        display_name: "Operador",
        permissions: options.permissions ?? ["audit.read"],
        timezone: "Europe/Madrid",
      }),
    listDevices: vi.fn(() => Promise.resolve({ items: [], total: 0 })),
  } as unknown as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderInSpanish(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[options.at ?? "/audit"]}>
        <AuditPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mockUseApi.mockReset();
  listAuditEvents.mockReset();
  verifyAuditChain.mockReset();
});

describe("the audit log in Spanish", () => {
  it("shows the event search in Spanish and leaves the stored entry text alone", async () => {
    const user = userEvent.setup();
    const { container } = renderPage();
    expect(screen.getByRole("heading", { name: "Registro de auditoría" })).toBeInTheDocument();
    // The labels around the entry are Spanish...
    const table = await screen.findByRole("table");
    expect(within(table).getAllByText("Tarea enviada")).toHaveLength(2);
    expect(within(table).getByText("Credencial retirada")).toBeInTheDocument();
    expect(within(table).getByText("Sistema")).toBeInTheDocument();
    expect(within(table).getByText("Dispositivo")).toBeInTheDocument();
    expect(within(table).getByRole("columnheader", { name: "Hash de la cadena" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Búsqueda de eventos" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Verificar la cadena" })).toBeInTheDocument();
    expect(screen.getByLabelText("Buscar en la página cargada por acción, actor o destino")).toBeInTheDocument();

    // ...and what is stored reads exactly as recorded.
    expect(within(table).getAllByText("job.dispatch")).toHaveLength(2);
    expect(within(table).getByText("credential.checkout")).toBeInTheDocument();
    expect(within(table).getByText("Helena Ruiz")).toBeInTheDocument();
    expect(within(table).getByText("SRV-FS-01")).toBeInTheDocument();
    expect(within(table).getAllByTitle(HASH).length).toBeGreaterThan(0);

    // The tiles.
    expect(screen.getByText("Eventos de auditoría")).toBeInTheDocument();
    expect(screen.getByText("Todo el registro que guarda esta organización")).toBeInTheDocument();
    expect(screen.getByText("Sobre todo «Tarea enviada», 2 de 3")).toBeInTheDocument();
    expect(await screen.findByText("Contados por el recorrido del servidor, no por esta página")).toBeInTheDocument();

    // Copying names the hash it copied.
    const copy = screen.getAllByRole("button", { name: `Copiar el hash completo de la cadena ${HASH}` })[0]!;
    await user.click(copy);
    expect(await screen.findByText(/Hash completo de la cadena copiado:/)).toBeInTheDocument();
    // "credential" is the stored action `credential.checkout`, shown exactly as recorded.
    expectNoEnglish(container, ["credential"]);
  });

  it("shows a verified chain in Spanish, with its Spanish plural", async () => {
    const { container } = renderPage({ at: "/audit?tab=integrity" });
    expect(await screen.findByText("Cadena verificada")).toBeInTheDocument();
    expect(screen.getByText(/3 eventos verificados desde el origen hasta el último/)).toBeInTheDocument();
    expect(screen.getByText("Integridad de la cadena de hashes")).toBeInTheDocument();
    expect(screen.getByText("verificada")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("words a broken chain and an unrun verification in Spanish", async () => {
    const broken = renderPage({
      at: "/audit?tab=integrity",
      verify: () => Promise.resolve({ ok: false, count: 1, broken_at: "evt-9" }),
    });
    const banners = await screen.findAllByRole("alert");
    expect(banners[0]).toHaveTextContent("Ha FALLADO la verificación de la cadena de hashes.");
    expect(banners[0]).toHaveTextContent("a partir del evento evt-9.");
    expect(banners[1]).toHaveTextContent("La verificación recorrió 1 evento y se detuvo en evt-9.");
    expect(screen.getByText("ROTA")).toBeInTheDocument();
    expectNoEnglish(broken.container);
    broken.unmount();

    const unknown = renderPage({
      at: "/audit?tab=integrity",
      verify: () => Promise.reject(new ApiError(403, "")),
    });
    expect(await screen.findByText("La verificación no se ejecutó.")).toBeInTheDocument();
    expect(screen.getByText("no")).toBeInTheDocument();
    expect(screen.getByText("Sin verificar")).toBeInTheDocument();
    expectNoEnglish(unknown.container);
  });

  it("says in Spanish when the account may not read the log, and when it is empty", async () => {
    const denied = renderPage({ permissions: [] });
    expect(await screen.findByText("No se muestra el registro de auditoría")).toBeInTheDocument();
    expect(screen.getByText("Esta cuenta no tiene audit.read, así que no se muestra el registro de auditoría.")).toBeInTheDocument();
    expectNoEnglish(denied.container);
    denied.unmount();

    const empty = renderPage({ events: () => Promise.resolve({ items: [], total: 0 }) });
    expect(await screen.findByText("Aún no hay eventos de auditoría registrados.")).toBeInTheDocument();
    expectNoEnglish(empty.container);
  });
});

describe("a live language switch", () => {
  it("re-words the 'mostly' tile foot on a mounted page", async () => {
    renderPage();
    expect(await screen.findByText("Sobre todo «Tarea enviada», 2 de 3")).toBeInTheDocument();
    act(() => setActiveLanguage("en"));
    expect(screen.getByText("Mostly “Job sent”, 2 of 3")).toBeInTheDocument();
    expect(screen.queryByText(/Sobre todo/)).not.toBeInTheDocument();
  });
});

describe("the audit action labels", () => {
  it("fall back to the derived English for a code the console has no words for, and keep the code", () => {
    // Not catalogued: the label is the derivation (and the raw code is shown beside it by the page).
    expect(auditActionLabel("ipam.sweep_rejected")).toBe("IPAM sweep rejected");
  });

  it("keep the English the page always showed, and have Spanish for each", () => {
    const english = en.enums as Record<string, Record<string, string>>;
    const spanish = es.enums as Record<string, Record<string, string>>;
    for (const type of ["AuditAction", "AuditActorType", "AuditTargetType"]) {
      expect(Object.keys(spanish[type]!).sort()).toEqual(Object.keys(english[type]!).sort());
    }
    // Actor and target kinds are exactly the derivation; actions are the hand-written map.
    for (const type of ["AuditActorType", "AuditTargetType"]) {
      for (const [value, label] of Object.entries(english[type]!)) {
        expect(label, `${type}.${value}`).toBe(humaniseEnum(value));
      }
    }
    expect(auditActionLabel("job.dispatch")).toBe("Job sent");
    expect(auditActionLabel("remote.session.credential_used")).toBe("Credential used for a remote session");
  });
});

// --------------------------------------------------------------------------
// The vocabulary the page gained: every action code and target kind the
// backend records now has words (auditVocabulary.coverage.test.ts holds that it
// stays so). These render real rows, because a catalogue entry that is never
// reached from the page is not a translation.

function rowOf(table: HTMLElement, storedCode: string): HTMLElement {
  const row = within(table).getByText(storedCode).closest("tr");
  expect(row, `no row for ${storedCode}`).not.toBeNull();
  return row as HTMLElement;
}

/**
 * The row's own words, for `expectNoEnglish`: the stored action code (the
 * monospace line under the label) is what the log recorded and is never
 * translated, and its words ("sweep_rejected") would read as English. It is
 * taken out of a COPY of the row rather than allowed by word, so nothing else
 * on the row gets a pass for it.
 */
function labelsOf(row: HTMLElement): HTMLElement {
  const copy = row.cloneNode(true) as HTMLElement;
  copy.querySelectorAll(".mono").forEach((stored) => stored.remove());
  return copy;
}

describe("the audit vocabulary in Spanish", () => {
  it("words an action that used to read as derived English, and keeps its stored code", async () => {
    // `ipam.sweep_rejected` had no entry: the Spanish page said "IPAM sweep rejected".
    renderPage({
      events: () =>
        Promise.resolve({
          total: 2,
          items: [
            event({ id: "v1", action: "ipam.sweep_rejected", actor_type: "agent", actor_id: null, target_type: "ipam_subnet", target_id: "10.2.0.0/24" }),
            event({ id: "v2", action: "jobs.dispatched", actor_type: "system", actor_id: null, target_type: "job", target_id: "job-9" }),
          ],
        }),
    });
    const table = await screen.findByRole("table");
    const sweep = rowOf(table, "ipam.sweep_rejected");
    expect(within(sweep).getByText("Barrido rechazado")).toBeInTheDocument();
    expect(within(sweep).getByText("Agente")).toBeInTheDocument();
    expect(within(sweep).getByText("10.2.0.0/24")).toBeInTheDocument();
    // A real job event: the backend's code is `jobs.dispatched`, not the console's old `job.dispatch`.
    const dispatched = rowOf(table, "jobs.dispatched");
    expect(within(dispatched).getByText("Tarea despachada")).toBeInTheDocument();
    expectNoEnglish(labelsOf(sweep));
    expectNoEnglish(labelsOf(dispatched));
  });

  it("words a target kind that used to read as derived English", async () => {
    // `ipam_subnet` and `platform_error_group` had no entry: "IPAM subnet", "Platform error group".
    renderPage({
      events: () =>
        Promise.resolve({
          total: 2,
          items: [
            event({ id: "t1", action: "ipam.subnet_created", target_type: "ipam_subnet", target_id: "10.2.0.0/24" }),
            event({ id: "t2", action: "platform_error.resolved", target_type: "platform_error_group", target_id: "grp-4" }),
          ],
        }),
    });
    const table = await screen.findByRole("table");
    const subnet = rowOf(table, "ipam.subnet_created");
    expect(within(subnet).getByText("Subred creada")).toBeInTheDocument();
    expect(within(subnet).getByText("Subred de IPAM")).toBeInTheDocument();
    expectNoEnglish(labelsOf(subnet));
    const group = rowOf(table, "platform_error.resolved");
    expect(within(group).getByText("Grupo de errores resuelto")).toBeInTheDocument();
    expect(within(group).getByText("Grupo de errores de la plataforma")).toBeInTheDocument();
    expectNoEnglish(labelsOf(group));
  });

  it("still derives English for a code that is in no catalogue, and shows the stored code beside it", async () => {
    // Pins the fallback: a backend code this console has not met reads as the
    // derivation (never blank, never hidden), in either language.
    expect(auditActionLabel("zz.not_real")).toBe("Zz not real");
    expect(auditTargetTypeLabel("zz_not_real_kind")).toBe("Zz not real kind");
    expect(auditActorTypeLabel("zz_not_real_actor")).toBe("Zz not real actor");
    renderPage({
      events: () =>
        Promise.resolve({
          total: 1,
          items: [event({ id: "z1", action: "zz.not_real", target_type: "zz_not_real_kind", target_id: "zz-1" })],
        }),
    });
    const table = await screen.findByRole("table");
    const row = rowOf(table, "zz.not_real");
    expect(within(row).getByText("Zz not real")).toBeInTheDocument();
    expect(within(row).getByText("Zz not real kind")).toBeInTheDocument();
    expect(within(row).getByText("zz.not_real")).toBeInTheDocument();
  });

  it("re-words a mounted row on a language switch, and its search follows the language", async () => {
    const user = userEvent.setup();
    renderPage({
      events: () =>
        Promise.resolve({
          total: 2,
          items: [
            event({ id: "s1", action: "ipam.sweep_rejected", target_type: "ipam_subnet", target_id: "10.2.0.0/24" }),
            event({ id: "s2", action: "jobs.dispatched", target_type: "job", target_id: "job-9" }),
          ],
        }),
    });
    const table = await screen.findByRole("table");
    expect(within(table).getByText("Barrido rechazado")).toBeInTheDocument();
    // The search matches the label the reader sees...
    await user.type(screen.getByLabelText("Buscar en la página cargada por acción, actor o destino"), "barrido");
    expect(within(table).getByText("ipam.sweep_rejected")).toBeInTheDocument();
    expect(within(table).queryByText("jobs.dispatched")).not.toBeInTheDocument();
    // ...and after a switch it matches the new language's label, not a cached one.
    act(() => setActiveLanguage("en"));
    expect(screen.getByText("No loaded audit event matches that filter.")).toBeInTheDocument();
    act(() => setActiveLanguage("es"));
    expect(within(await screen.findByRole("table")).getByText("Barrido rechazado")).toBeInTheDocument();
    await user.clear(screen.getByLabelText("Buscar en la página cargada por acción, actor o destino"));
    act(() => setActiveLanguage("en"));
    expect(within(screen.getByRole("table")).getByText("IPAM sweep rejected")).toBeInTheDocument();
    expect(within(screen.getByRole("table")).getByText("IPAM subnet")).toBeInTheDocument();
    expect(within(screen.getByRole("table")).getByText("Jobs dispatched")).toBeInTheDocument();
  });
});
