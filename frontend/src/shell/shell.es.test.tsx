/**
 * The shell in Spanish: the chrome around every page, its navigation model, the
 * search panel and the shared presentation components. Each `it` renders a
 * surface with the language set to Spanish and asserts that no English
 * catalogue value is on it (`expectNoEnglish`), plus a few Spanish phrases so
 * an empty render cannot pass.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { useT } from "../i18n/context";
import { setActiveLanguage, translate } from "../i18n/core";
import { expectNoEnglish, renderInSpanish } from "../i18n/test-utils";
import type { ApiClient, Me } from "../lib/api";
import { AppShell } from "./AppShell";
import { AreaPage } from "./AreaPage";
import { Drawer } from "./Drawer";
import { healthLabel, platformLabel } from "./healthSignal";
import { crumbFor, OPTIONAL_MODULES, portalCrumbFor, searchDestinations } from "./nav";
import { searchPrompt } from "./recordSearch";
import { Pager, Progress, QueryState, StatCard } from "./components";
import { Toaster } from "./Toaster";
import { Chips, Heat, Sparkline, Steps, Timeline } from "./viz";

const mockUseApi = vi.fn();
vi.mock("../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const EVERYTHING = [
  "devices.read", "health.read", "ticket.read", "patch.read", "deploy.read", "backup.read",
  "remote.control", "ai.use", "docs.read", "change.read", "bcp.read", "reporting.read",
  "licenses.read", "subscriptions.read", "budget.read", "ipam.read", "notification.read",
  "email_intake.read", "sla.read", "portal.ticket.read", "agent.release.read",
  "agent.push.scope.manage", "site.read", "provisioning.read", "ai.admin", "jobs.read",
  "credentials.read", "audit.read", "platform.status.read", "platform.errors.read", "orgs.manage",
];

// Words the shell shows verbatim in both languages: product, brand and
// operator-supplied names (the fixture's org, person and role).
const ALLOW = ["ITMS", "Operations", "Alex Morgan", "Administrator"];

/** What `App.tsx`'s Shell does: the crumb and the page list are read from the
 *  nav model on every render, and `useT()` re-renders it on a language switch. */
function Page({ path, ...props }: { path: string } & Partial<React.ComponentProps<typeof AppShell>>) {
  useT();
  const crumb = crumbFor(path);
  return (
    <MemoryRouter initialEntries={[path]}>
      <AppShell
        permissions={EVERYTHING}
        orgName="Operations"
        section={crumb.section}
        sectionTo={crumb.sectionTo}
        title={crumb.title}
        badges={{ "/health": 3 }}
        health={{ openAlerts: 3, platform: "degraded" }}
        user={{ name: "Alex Morgan", email: "alex@toeshee.com", role: "Administrator" }}
        onSignOut={vi.fn()}
        onAsk={vi.fn()}
        searchItems={searchDestinations(EVERYTHING, OPTIONAL_MODULES).map((page) => ({
          id: page.to,
          label: page.label,
          sub: page.within,
          type: translate("shell.app.pageType"),
          icon: page.icon,
          onSelect: vi.fn(),
        }))}
        {...props}
      >
        <p>contenido</p>
      </AppShell>
    </MemoryRouter>
  );
}

const shell = (path: string, props: Partial<React.ComponentProps<typeof AppShell>> = {}) => (
  <Page path={path} {...props} />
);

describe("the shell in Spanish", () => {
  it("draws the whole chrome, with the Admin area open, in Spanish", () => {
    const { container } = renderInSpanish(shell("/jobs", { sessionExpiring: true, onSignIn: vi.fn() }));

    const rail = screen.getByRole("navigation", { name: "Principal" });
    expect(within(rail).getByText("Día a día")).toBeInTheDocument();
    expect(within(rail).getByRole("link", { name: /Dispositivos/ })).toBeInTheDocument();
    expect(within(rail).getByRole("link", { name: /Copias de seguridad/ })).toBeInTheDocument();
    expect(within(rail).getByText("Gobernanza")).toBeInTheDocument();
    expect(within(rail).getByText("Activos y gasto")).toBeInTheDocument();
    // The Admin area is open (the route is under it), with its section headings.
    expect(within(rail).getByText("Mesa de ayuda")).toBeInTheDocument();
    expect(within(rail).getByRole("link", { name: /Historial de tareas/ })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Añadir un equipo" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Preguntar al Asistente" })).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Ruta de navegación" })).toHaveTextContent("Administración");
    expect(screen.getByRole("status")).toHaveTextContent("Tu sesión ha caducado");
    expect(screen.getByRole("button", { name: "Iniciar sesión de nuevo" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Contraer la barra lateral" })).toBeInTheDocument();
    expect(document.title).toBe("Historial de tareas · ITMS");
    expectNoEnglish(container, ALLOW);
  });

  it("names the health and platform glyphs in Spanish", () => {
    const { container } = renderInSpanish(shell("/dashboard"));
    expect(screen.getByRole("link", { name: /Estado de salud y alertas, una dependencia de la plataforma está degradada, 3 requieren atención/ })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Estado de la plataforma, una dependencia está degradada" })).toBeInTheDocument();
    expectNoEnglish(container, ALLOW);
  });

  it("opens the search panel in Spanish, with the page list and the ask row", async () => {
    const user = userEvent.setup();
    const { container } = renderInSpanish(shell("/dashboard"));
    const field = screen.getByRole("combobox", { name: "Buscar o preguntar" });
    await user.click(field);
    expect(screen.getByRole("listbox", { name: "Resultados de la búsqueda" })).toBeInTheDocument();
    expect(within(screen.getByRole("listbox")).getByRole("option", { name: /Dispositivos/ })).toBeInTheDocument();
    expectNoEnglish(container, ALLOW);

    await user.type(field, "zzzz");
    expect(screen.getByText("Preguntar al Asistente")).toBeInTheDocument();
    expect(screen.getByText("Ningún registro coincide")).toBeInTheDocument();
    expect(screen.getByText(/puede hacérsela al Asistente\./)).toBeInTheDocument();
    expectNoEnglish(container, ALLOW);
  });

  it("re-labels a mounted shell when the language is switched", () => {
    const { container } = render(shell("/devices"));
    expect(screen.getByRole("navigation", { name: "Main" })).toBeInTheDocument();
    act(() => setActiveLanguage("es"));
    expect(screen.getByRole("navigation", { name: "Principal" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Añadir un equipo" })).toBeInTheDocument();
    expectNoEnglish(container, ALLOW);
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("navigation", { name: "Main" })).toBeInTheDocument();
  });

  it("reads the search prompts and crumbs from the active language", () => {
    setActiveLanguage("es");
    expect(searchPrompt(["devices.read", "ticket.read", "ai.use"], false)).toEqual({
      placeholder: "Buscar dispositivos, direcciones, tickets o páginas, o hacer una pregunta",
      hint: "La búsqueda coincide con nombres y direcciones de dispositivos, números y asuntos de tickets y nombres de páginas.",
    });
    expect(searchPrompt([], false).placeholder).toBe("Buscar páginas");
    expect(searchPrompt([], true).placeholder).toBe("Busca uno de tus tickets");
    expect(crumbFor("/devices/onboard")).toEqual({ section: "Dispositivos", sectionTo: "/devices", title: "Añadir un equipo" });
    expect(crumbFor("/profile")).toEqual({ section: "Tu cuenta", sectionTo: undefined, title: "ITMS" });
    expect(portalCrumbFor("/portal/new").title).toBe("Nuevo ticket");
    expect(healthLabel({ openAlerts: 0, platform: null })).toBe("Estado de salud y alertas, sin alertas abiertas");
    expect(healthLabel({ openAlerts: 1, platform: "down" })).toBe(
      "Estado de salud y alertas, una dependencia de la plataforma está caída, 1 requiere atención",
    );
    expect(platformLabel("ok")).toBe("Estado de la plataforma, todas las dependencias están en buen estado");
  });

  it("lists an area's pages in Spanish", async () => {
    mockUseApi.mockReturnValue({
      me: vi.fn().mockResolvedValue({ permissions: ["notification.read", "audit.read"] } as unknown as Me),
    } as unknown as ApiClient);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { container } = renderInSpanish(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <AreaPage to="/admin" />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(await screen.findByRole("heading", { name: "Administración", level: 1 })).toBeInTheDocument();
    expect(screen.getByText("Configura cómo funciona la consola para tu organización.")).toBeInTheDocument();
    expect(await screen.findByRole("table", { name: "Mesa de ayuda" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Abrir Registro de auditoría" })).toBeInTheDocument();
    expect(screen.getByText("Quién hizo qué y cuándo.")).toBeInTheDocument();
    expectNoEnglish(container, ALLOW);
  });

  it("says an area is closed to the account in Spanish", async () => {
    mockUseApi.mockReturnValue({
      me: vi.fn().mockResolvedValue({ permissions: ["docs.read"] } as unknown as Me),
    } as unknown as ApiClient);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { container } = renderInSpanish(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <AreaPage to="/admin" />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(await screen.findByText(/Nada en esta área está disponible para tu cuenta/)).toBeInTheDocument();
    expectNoEnglish(container, ALLOW);
  });
});

describe("the shared presentation components in Spanish", () => {
  it("draws stat cards, pagers, progress, loading and empty states in Spanish", () => {
    const onPage = vi.fn();
    const { container } = renderInSpanish(
      <MemoryRouter>
        <StatCard label="Equipos" value="12" icon="devices" to="/devices" trend={{ direction: "up", label: "+2" }} />
        <StatCard label="Alertas" value="0" icon="health" to="/health" pending />
        <Pager page={0} pageCount={3} onPrevious={onPage} onNext={onPage} scopeName="Dispositivos" />
        <Progress value={40} />
        <QueryState loading error={null} empty={false} emptyLabel="x">
          <p>hecho</p>
        </QueryState>
        <QueryState loading={false} error={new Error("falló")} empty={false} emptyLabel="x">
          <p>hecho</p>
        </QueryState>
      </MemoryRouter>,
    );
    expect(screen.getByText("Subida,")).toBeInTheDocument();
    expect(screen.getByText("n/d")).toBeInTheDocument();
    expect(screen.getByText("Página 1 de 3")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Siguiente, página de Dispositivos" })).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "Progreso" })).toBeInTheDocument();
    expect(screen.getByText("Cargando…")).toBeInTheDocument();
    expectNoEnglish(container, ALLOW);
  });

  it("draws the visual primitives with Spanish accessible names", () => {
    const { container } = renderInSpanish(
      <div>
        <Sparkline values={[1, 3, 2]} />
        <Heat cells={["crit", "ok", "ok"]} />
        <Timeline runs={[{ at: "02:00", kind: "warn", label: "parche" }]} ticks={["00", "12"]} />
        <Steps items={[
          { label: "uno", state: "done" },
          { label: "dos", state: "now" },
          { label: "tres", state: "todo" },
        ]} />
        <Chips items={["a", "b"]} active={0} onSelect={vi.fn()} />
      </div>,
    );
    expect(screen.getByRole("img", { name: "Tendencia en 3 puntos, de 1 a 2" })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "3 elementos: 1 crítico, 2 correctos" })).toBeInTheDocument();
    expect(screen.getByText("advertencia")).toBeInTheDocument();
    expect(screen.getByText("Hecho")).toBeInTheDocument();
    expect(screen.getByText("Ahora")).toBeInTheDocument();
    expect(screen.getByText("Pendiente")).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Filtrar" })).toBeInTheDocument();
    expectNoEnglish(container, ALLOW);
  });

  it("closes a drawer and dismisses a toast with Spanish names", () => {
    const { container } = renderInSpanish(
      <div>
        <Drawer open onClose={vi.fn()} title="Detalle">
          <p>cuerpo</p>
        </Drawer>
        <Toaster toasts={[{ id: "1", title: "Guardado", tone: "success" }]} onDismiss={vi.fn()} />
      </div>,
    );
    expect(screen.getByRole("button", { name: "Cerrar" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Descartar" })).toBeInTheDocument();
    expectNoEnglish(container, ALLOW);
  });
});
