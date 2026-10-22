/**
 * The error monitor in Spanish: the banners, tiles, filters, group table, the
 * events panel, the empty states and the resolve / reopen flow with its
 * confirmation and its announcement. Exception types, raise sites and
 * correlation ids are the server's own data and are shown as captured.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { expectNoEnglish, renderInSpanish } from "../../i18n/test-utils";
import type { ApiClient, Me } from "../../lib/api";
import { ERROR_GROUPS_LIST_LIMIT } from "./api";
import { ErrorMonitorPage } from "./ErrorMonitorPage";
import type { ErrorGroup, ErrorGroupDetail, ErrorGroupList } from "./types";

// The fixture group's module is the stored path `app.jobs.dispatch`; the word "jobs"
// in it is data (a Python module), not copy, and is also an English catalogue
// fragment now that the jobs module has its own plural.
const STORED = ["jobs"];

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const ME: Me = {
  user_id: "u1",
  org_id: "o1",
  email: "operator@example.com",
  display_name: "Operador",
  permissions: ["platform.errors.read", "platform.errors.write"],
};

const OPEN_GROUP: ErrorGroup = {
  id: "11111111-1111-1111-1111-111111111111",
  fingerprint: "abc123def4567890",
  exc_type: "ValueError",
  module: "app.jobs.dispatch",
  lineno: 142,
  qualname: "dispatch_job",
  where: "worker",
  first_seen: "2026-08-10T12:00:00Z",
  last_seen: "2026-08-15T09:30:00Z",
  event_count: 7,
  resolved_at: null,
};

const RESOLVED_GROUP: ErrorGroup = {
  id: "22222222-2222-2222-2222-222222222222",
  fingerprint: "9990001112223334",
  exc_type: "TimeoutError",
  module: "app.gateway.conn",
  lineno: 88,
  qualname: "handle_connect",
  where: "gateway",
  first_seen: "2026-08-01T00:00:00Z",
  last_seen: "2026-08-02T00:00:00Z",
  event_count: 3,
  resolved_at: "2026-08-03T00:00:00Z",
};

const DETAIL: ErrorGroupDetail = {
  group: OPEN_GROUP,
  events: [
    {
      id: "33333333-3333-3333-3333-333333333333",
      occurred_at: "2026-08-15T09:30:00Z",
      request_id: "req-abc-999",
      org_id: null,
      user_id: null,
      job_id: "44444444-4444-4444-4444-444444444444",
      device_id: null,
    },
  ],
};

let api: Partial<ApiClient>;

function setList(list: ErrorGroupList, me: Me = ME) {
  api = {
    me: vi.fn().mockResolvedValue(me),
    listErrorGroups: vi.fn().mockResolvedValue(list),
    getErrorGroup: vi.fn().mockResolvedValue(DETAIL),
    resolveErrorGroup: vi.fn().mockResolvedValue({ ...OPEN_GROUP, resolved_at: "2026-08-15T10:00:00Z" }),
    unresolveErrorGroup: vi.fn().mockResolvedValue({ ...RESOLVED_GROUP, resolved_at: null }),
  };
  mockUseApi.mockReturnValue(api as ApiClient);
}

function renderPage(entry = "/error-monitor") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderInSpanish(
    <MemoryRouter initialEntries={[entry]}>
      <QueryClientProvider client={queryClient}>
        <ErrorMonitorPage />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  mockUseApi.mockReset();
});

describe("the error monitor in Spanish", () => {
  it("shows the banner, the tiles, the filters and the table in Spanish", async () => {
    setList({ groups: [OPEN_GROUP, RESOLVED_GROUP], dropped: 2 });
    const { container } = renderPage();
    const openRow = (await screen.findByText("ValueError")).closest("tr") as HTMLElement;
    const resolvedRow = screen.getByText("TimeoutError").closest("tr") as HTMLElement;

    expect(screen.getByRole("heading", { name: "Monitor de errores" })).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Se descartaron 2 capturas por desbordamiento de la cola de captura",
    );
    expect(screen.getByText("Grupos con huella")).toBeInTheDocument();
    expect(screen.getByText("Eventos capturados")).toBeInTheDocument();

    expect(screen.getByLabelText("Buscar grupos de errores")).toHaveAttribute(
      "placeholder",
      "Busca por tipo, módulo, función o punto de origen",
    );
    expect(screen.getByRole("combobox", { name: "Filtrar por estado de triaje" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Todos los puntos" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Pasarela" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Visto en los últimos 7 días" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Más eventos" })).toBeInTheDocument();

    expect(within(openRow).getByText("Proceso de trabajo")).toBeInTheDocument();
    expect(within(openRow).getByText("Abierto")).toBeInTheDocument();
    expect(within(resolvedRow).getByText("Resuelto")).toBeInTheDocument();
    expect(within(openRow).getByRole("button", { name: "Resolver…" })).toBeInTheDocument();
    expect(within(resolvedRow).getByRole("button", { name: "Reabrir" })).toBeInTheDocument();
    expect(within(openRow).getByRole("button", { name: "Eventos de ValueError" })).toBeInTheDocument();
    expect(within(container).getByRole("columnheader", { name: "Punto de origen" })).toBeInTheDocument();
    expectNoEnglish(container, STORED);
  });

  it("shows the events of a group, saying why a correlation id is blank", async () => {
    const user = userEvent.setup();
    setList({ groups: [OPEN_GROUP, RESOLVED_GROUP], dropped: 0 });
    const { container } = renderPage();
    await user.click(await screen.findByRole("button", { name: "Eventos de ValueError" }));
    expect(await screen.findByRole("heading", { name: "Eventos recientes" })).toBeInTheDocument();
    expect(screen.getByText("req-abc-999")).toBeInTheDocument();
    expect(
      screen.getByText("No había ninguna organización en curso cuando se capturó esta ocurrencia"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("No había ningún usuario con sesión iniciada cuando se capturó esta ocurrencia"),
    ).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "ID de solicitud" })).toBeInTheDocument();
    expectNoEnglish(container, STORED);
  });

  it("confirms and announces a resolve, and a reopen, in Spanish", async () => {
    const user = userEvent.setup();
    setList({ groups: [OPEN_GROUP, RESOLVED_GROUP], dropped: 0 });
    const { container } = renderPage();
    const openRow = (await screen.findByText("ValueError")).closest("tr") as HTMLElement;
    await user.click(within(openRow).getByRole("button", { name: "Resolver…" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Resolver grupo de errores")).toBeInTheDocument();
    expect(within(dialog).getByText(/Al resolverlo, este grupo sale del recuento de abiertos/)).toBeInTheDocument();
    expectNoEnglish(dialog, STORED);
    await user.click(within(dialog).getByRole("button", { name: "Resolver grupo" }));
    expect(await screen.findByText(/Se resolvió ValueError en app\.jobs\.dispatch:142\./)).toBeInTheDocument();

    const resolvedRow = screen.getByText("TimeoutError").closest("tr") as HTMLElement;
    await user.click(within(resolvedRow).getByRole("button", { name: "Reabrir" }));
    expect(await screen.findByText(/Se reabrió TimeoutError en app\.gateway\.conn:88\./)).toBeInTheDocument();
    expectNoEnglish(container, STORED);
  });

  it("says in Spanish that a reader without permission cannot resolve", async () => {
    setList({ groups: [OPEN_GROUP], dropped: 0 }, { ...ME, permissions: ["platform.errors.read"] });
    const { container } = renderPage();
    await screen.findByText("ValueError");
    expect(
      screen.getByText("Resolver o reabrir requiere permiso para administrar los errores de la plataforma"),
    ).toBeInTheDocument();
    expectNoEnglish(container, STORED);
  });

  it("words the empty states, the clear-filters button and the truncation notice in Spanish", async () => {
    setList({ groups: [], dropped: 0 });
    const first = renderPage();
    expect(await screen.findByText("No hay grupos de errores. Nada ha fallado.")).toBeInTheDocument();
    expectNoEnglish(first.container);
    first.unmount();

    setList({ groups: [OPEN_GROUP], dropped: 0 });
    const filtered = renderPage("/error-monitor?q=zzz");
    expect(await screen.findByText(/Ningún grupo de errores coincide con estos filtros/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Borrar filtros" })).toBeInTheDocument();
    expectNoEnglish(filtered.container);
    filtered.unmount();

    const many: ErrorGroup[] = Array.from({ length: ERROR_GROUPS_LIST_LIMIT }, (_, i) => ({
      ...OPEN_GROUP,
      id: `g-${i}`,
      lineno: i,
    }));
    setList({ groups: many, dropped: 1 });
    const truncated = renderPage();
    await screen.findAllByText("ValueError");
    const alerts = screen.getAllByRole("alert");
    expect(alerts[0]).toHaveTextContent("Se descartó 1 captura por desbordamiento");
    expect(alerts[1]).toHaveTextContent(`Se muestran los ${ERROR_GROUPS_LIST_LIMIT} grupos más recientes.`);
    expectNoEnglish(truncated.container, STORED);
  });
});
