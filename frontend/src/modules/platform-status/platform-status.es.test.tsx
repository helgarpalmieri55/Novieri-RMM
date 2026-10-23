/**
 * The platform status page in Spanish: the verdict banner, the four tiles, the
 * three tables, the loading and failed states, and the zero-state tiles.
 * Probe, process and module names are the server's identifiers: the ones the
 * catalogue knows read in Spanish, and the raw key stays beside them.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { en } from "../../i18n/en";
import { es } from "../../i18n/es";
import { expectNoEnglish, renderInSpanish } from "../../i18n/test-utils";
import type { ApiClient } from "../../lib/api";
import { humaniseEnum } from "../../lib/labels";
import { PlatformStatusPage } from "./PlatformStatusPage";
import { TABLER } from "../../shell/tabler";
import type { PlatformStatus } from "./types";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const STATUS: PlatformStatus = {
  overall: "degraded",
  probes: [
    { name: "database", status: "ok", duration_ms: 4, reason: null, detail: null },
    { name: "vault", status: "down", duration_ms: 812, reason: "sealed", detail: { sealed: true } },
    { name: "jwks", status: "ok", duration_ms: 2, reason: null, detail: { keys: 2 } },
    {
      name: "backup.nightly",
      status: "degraded",
      duration_ms: 0,
      reason: "stale",
      detail: { configured: true, hours_since_success: 30, ever_run: false },
    },
  ],
  processes: [
    { process: "api", instances: 2, healthy_instances: 2, oldest_age_s: 6, status: "ok", version: "1.4.0" },
    { process: "gateway", instances: 3, healthy_instances: 1, oldest_age_s: 5400, status: "degraded", version: null },
  ],
  modules: [
    { name: "service_desk", routes: 12, job_types: 4 },
    { name: "patch", routes: 1, job_types: 1 },
  ],
};

function mockApi(status: PlatformStatus | Promise<never> | Error) {
  mockUseApi.mockReturnValue({
    getPlatformStatus: () =>
      status instanceof Error ? Promise.reject(status) : status instanceof Promise ? status : Promise.resolve(status),
    me: () =>
      Promise.resolve({
        user_id: "u1",
        org_id: "o1",
        email: "a@b.c",
        display_name: "Alex Morgan",
        permissions: ["platform.status.read"],
        timezone: "Europe/Madrid",
      }),
  } as unknown as ApiClient);
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderInSpanish(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <PlatformStatusPage />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  mockUseApi.mockReset();
});

describe("the platform status page in Spanish", () => {
  it("shows the verdict, the tiles and the three tables in Spanish", async () => {
    mockApi(STATUS);
    const { container } = renderPage();
    expect(screen.getByRole("heading", { name: "Estado de la plataforma" })).toBeInTheDocument();
    const banner = await screen.findByRole("alert");
    expect(banner).toHaveTextContent("2 de 4 sondas de dependencias están fallando: vault sealed, backup.nightly stale.");
    expect(banner).toHaveTextContent("La bóveda está sellada");
    expect(within(banner).getByRole("link", { name: "Abrir grupos de errores" })).toBeInTheDocument();
    expect(screen.getByText(/Se vuelven a leer cada 15 segundos\./)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sondear de nuevo" })).toBeInTheDocument();

    // Tiles, with Spanish plurals.
    expect(screen.getByText("Sondas de dependencias")).toBeInTheDocument();
    expect(screen.getByText("Más lenta: Bóveda, 812 ms")).toBeInTheDocument();
    expect(screen.getByText("de 4 sondas")).toBeInTheDocument();
    expect(screen.getByText("Procesos con menos instancias de las necesarias")).toBeInTheDocument();
    expect(screen.getByText("de 2 procesos")).toBeInTheDocument();
    expect(screen.getByText("13 rutas · 5 tipos de tarea")).toBeInTheDocument();

    // Dependencies: names, reasons, details.
    const dependencies = container.querySelector<HTMLElement>("#dependencies")!;
    expect(within(dependencies).getByRole("columnheader", { name: "Motivo" })).toBeInTheDocument();
    expect(within(dependencies).getByText("Bóveda")).toBeInTheDocument();
    expect(within(dependencies).getByText("Copia de seguridad nocturna")).toBeInTheDocument();
    expect(within(dependencies).getByText("Sellada")).toBeInTheDocument();
    expect(within(dependencies).getByText("Desactualizada")).toBeInTheDocument();
    expect(within(dependencies).getByText("Bóveda sellada: sí")).toBeInTheDocument();
    expect(within(dependencies).getByText("Claves: 2")).toBeInTheDocument();
    expect(
      within(dependencies).getByText("Configurado: sí · Horas desde el último éxito: 30 · Se ha ejecutado alguna vez: no"),
    ).toBeInTheDocument();
    expect(within(dependencies).getAllByText("Esta sonda no indicó ningún código de motivo")).not.toHaveLength(0);
    // The raw keys stay beside the words.
    expect(within(dependencies).getByText("backup.nightly")).toBeInTheDocument();

    // Processes: the shortfall, the hours with a Spanish decimal comma, a missing version.
    const processes = container.querySelector<HTMLElement>("#processes")!;
    expect(within(processes).getByText("1 / 3 en buen estado")).toBeInTheDocument();
    expect(within(processes).getByText("1,5 h")).toBeInTheDocument();
    expect(within(processes).getByText("Este proceso no indicó su versión")).toBeInTheDocument();
    expect(within(processes).getByText("Degradado")).toBeInTheDocument();

    // Modules.
    const modules = container.querySelector<HTMLElement>("#modules")!;
    expect(within(modules).getByText("Mesa de ayuda")).toBeInTheDocument();
    expect(within(modules).getByText("Parches")).toBeInTheDocument();
    expect(within(modules).getByText("Registro, no estado de salud: aquí no se sondea nada.")).toBeInTheDocument();
    // "service" is part of the recorded module identifier `service_desk`, and "gateway" is
    // the recorded name of a process. "stale" is the recorded reason code of a stale heartbeat,
    // and since 10b also a catalogue fragment (the reporting enum word for a stale scan).
    expectNoEnglish(container, ["service", "gateway", "stale"]);
  });

  it("words the banner for a process short of instances, and the zero tiles", async () => {
    mockApi({
      overall: "ok",
      probes: [{ name: "database", status: "ok", duration_ms: 4, reason: null, detail: null }],
      processes: [{ process: "worker", instances: 2, healthy_instances: 1, oldest_age_s: 30, status: "ok", version: "1" }],
      modules: [],
    });
    const { container } = renderPage();
    expect(await screen.findByRole("alert")).toHaveTextContent("1 proceso tiene menos instancias de las necesarias.");
    expect(screen.getByText("Todas las sondas respondieron correctamente")).toBeInTheDocument();
    expect(screen.getByText("No se registró nada al arrancar")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("says in Spanish when it is loading and when the read failed", async () => {
    mockApi(new Promise<never>(() => {}));
    const first = renderPage();
    expect(await screen.findByText("Cargando…")).toBeInTheDocument();
    first.unmount();

    mockApi(new Error("boom"));
    const { container } = renderPage();
    expect(await screen.findAllByRole("alert")).not.toHaveLength(0);
    expectNoEnglish(container);
  });
});

describe("the platform status page when the server words the probe names", () => {
  it("looks the glyph up by the probe's key, not by the Spanish name the server printed", async () => {
    // `name` follows the request's language; `key` is the identifier and never does.
    mockApi({
      overall: "ok",
      probes: [
        { key: "vault", name: "Bóveda", status: "ok", duration_ms: 3, reason: null, detail: null },
        { key: "backup.nightly", name: "Copia de seguridad nocturna", status: "ok", duration_ms: 0, reason: null, detail: null },
      ],
      processes: [],
      modules: [],
    });
    const { container } = renderPage();
    const dependencies = await waitForDependencies(container);
    const rows = Array.from(dependencies.querySelectorAll("tbody tr"));
    expect(rows).toHaveLength(2);
    const [vault, backup] = rows as HTMLElement[];
    // The words are the server's, the identifier sits beneath them.
    expect(within(vault!).getByText("Bóveda")).toBeInTheDocument();
    expect(within(vault!).getByText("vault")).toBeInTheDocument();
    expect(within(backup!).getByText("Copia de seguridad nocturna")).toBeInTheDocument();
    expect(within(backup!).getByText("backup.nightly")).toBeInTheDocument();
    // The glyph resolved from the key: a lock for the vault, an archive for the backup.
    expect(pathsOf(vault!)).toEqual(pathsIn(TABLER["lock"]));
    expect(pathsOf(backup!)).toEqual(pathsIn(TABLER["archive"]));
  });

  it("falls back to the name for an older payload that carries no key", async () => {
    mockApi({
      overall: "ok",
      probes: [{ name: "vault", status: "ok", duration_ms: 3, reason: null, detail: null }],
      processes: [],
      modules: [],
    });
    const { container } = renderPage();
    const dependencies = await waitForDependencies(container);
    const row = dependencies.querySelector("tbody tr") as HTMLElement;
    expect(within(row).getByText("Bóveda")).toBeInTheDocument(); // the catalogue's word for "vault"
    expect(pathsOf(row)).toEqual(pathsIn(TABLER["lock"]));
  });
});

/** The `d` of every path in a row's leading glyph (the sprite markup serialises differently). */
function pathsOf(row: HTMLElement): string[] {
  return Array.from(row.querySelector("svg")!.querySelectorAll("path")).map((p) => p.getAttribute("d") ?? "");
}

function pathsIn(markup: string | undefined): string[] {
  return Array.from((markup ?? "").matchAll(/ d="([^"]+)"/g)).map((m) => m[1] ?? "");
}

async function waitForDependencies(container: HTMLElement): Promise<HTMLElement> {
  await screen.findByRole("columnheader", { name: "Motivo" });
  return container.querySelector<HTMLElement>("#dependencies")!;
}

describe("the platform status labels", () => {
  it("keep English exactly as the derivation always read", () => {
    const enums = en.enums as Record<string, Record<string, string>>;
    // The one label the old page spelled out by hand (`DETAIL_LABEL`).
    const handWritten: Record<string, string> = { "PlatformProbeDetail.sealed": "Vault sealed" };
    for (const type of ["ReasonCode", "PlatformProbe", "PlatformProbeDetail", "PlatformModule"]) {
      for (const [value, label] of Object.entries(enums[type]!)) {
        expect(label, `${type}.${value}`).toBe(handWritten[`${type}.${value}`] ?? humaniseEnum(value));
      }
    }
  });

  it("have a Spanish label for every English one", () => {
    const spanish = es.enums as Record<string, Record<string, string>>;
    const english = en.enums as Record<string, Record<string, string>>;
    for (const type of ["ReasonCode", "PlatformProbe", "PlatformProbeDetail", "PlatformModule"]) {
      expect(Object.keys(spanish[type]!).sort()).toEqual(Object.keys(english[type]!).sort());
    }
  });
});
