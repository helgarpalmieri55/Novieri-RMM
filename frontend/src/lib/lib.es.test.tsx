/**
 * The shared library text in Spanish: permission sentences, list joining and
 * the prerequisite detectors' words. These have no page of their own (they
 * are read by every module), so each `it` renders what a module would render
 * from them and asserts that no English catalogue value is on it.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { setActiveLanguage } from "../i18n/core";
import { expectNoEnglish, renderInSpanish } from "../i18n/test-utils";
import type { AgentRelease, ApiClient } from "./api";
import {
  ADD_COMPUTER_CTA,
  CREATE_GROUP_CTA,
  useFleetPrerequisite,
  useGroupsPrerequisite,
  useInstallerPrerequisite,
  useRulesPrerequisite,
} from "./detectors";
import { conjunctionBefore, joinWords } from "./joinWords";
import { permissionPhrase, permissionsSentence } from "./permissions";

const mockUseApi = vi.fn();
vi.mock("./ApiContext", () => ({ useApi: () => mockUseApi() }));

const fleet = (over: Partial<{ total: number; managed: number; online: number }> = {}) => ({
  items: [],
  total: 0,
  limit: 1,
  offset: 0,
  state_counts: { all: 0, online: 0, offline: 0, assets: 0, certs: 0 },
  fleet: { total: 0, managed: 0, online: 0, cert_attention: 0, ...over },
});

function stubApi(over: Record<string, unknown> = {}) {
  mockUseApi.mockReturnValue({
    me: vi.fn().mockResolvedValue({
      user_id: "u1", org_id: "o1", email: "a@b.c", display_name: "A",
      permissions: ["devices.read", "agent.release.read", "notification.read"],
    }),
    listDevices: vi.fn().mockResolvedValue(fleet()),
    listDeviceGroups: vi.fn().mockResolvedValue({ items: [], total: 0, ungrouped_device_count: 0, last_evaluated_at: null }),
    listAgentReleases: vi.fn().mockResolvedValue([]),
    listNotificationRules: vi.fn().mockResolvedValue([]),
    ...over,
  } as unknown as ApiClient);
}

const wrapper = ({ children }: { children: ReactNode }) => (
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    {children}
  </QueryClientProvider>
);

async function item<T extends { state: string }>(hook: () => T): Promise<T> {
  setActiveLanguage("es");
  const { result } = renderHook(hook, { wrapper });
  await waitFor(() => expect(result.current.state).not.toBe("checking"));
  return result.current;
}

const release = (over: Partial<AgentRelease> = {}): AgentRelease => ({
  id: "r1", version: "1.8.2", os: "linux", arch: "amd64", size_bytes: 1, sha256: "ab", signed: true,
  notes: "", published_at: "2026-09-01T00:00:00Z", has_artifact: true, ...over,
});

describe("lists and permission sentences in Spanish", () => {
  it("joins with y / o, and bends them before an i- or o-sound", () => {
    setActiveLanguage("es");
    expect(joinWords(["uno"])).toBe("uno");
    expect(joinWords(["Windows", "Linux"])).toBe("Windows y Linux");
    expect(joinWords(["Windows", "Linux", "macOS"], "or")).toBe("Windows, Linux o macOS");
    expect(joinWords(["Linux", "IPv6"])).toBe("Linux e IPv6");
    expect(joinWords(["hielo", "hierro"])).toBe("hielo y hierro");
    expect(joinWords(["Linux", "Intel"])).toBe("Linux e Intel");
    expect(joinWords(["Windows", "otro"], "or")).toBe("Windows u otro");
    expect(joinWords(["Windows", "Mac"], "or")).toBe("Windows o Mac");
    expect(conjunctionBefore("hongo", "or")).toBe("u");
  });

  it("keeps the English words in English", () => {
    expect(joinWords(["Linux", "IPv6"])).toBe("Linux and IPv6");
    expect(joinWords(["a", "b", "c"], "or")).toBe("a, b or c");
  });

  it("says what a missing permission lets a person do", () => {
    setActiveLanguage("es");
    expect(permissionPhrase("devices.read")).toBe("ver dispositivos");
    expect(permissionPhrase("widgets.read")).toBe("ver widgets");
    expect(permissionPhrase("some_thing.manage")).toBe("administrar some thing");
    expect(permissionsSentence([])).toBe("permiso para hacer esto");
    expect(permissionsSentence(["devices.read"])).toBe("permiso para ver dispositivos");
    expect(permissionsSentence(["devices.read", "patch.read"])).toBe("permiso para ver dispositivos y ver actualizaciones");
    expect(permissionsSentence(["devices.read", "patch.read", "agent.release.read"])).toBe(
      "permiso para ver dispositivos, ver actualizaciones e instalar agentes",
    );
  });
});

describe("the prerequisite detectors in Spanish", () => {
  it("reads an empty fleet in Spanish, with a Spanish way out", async () => {
    stubApi();
    for (const need of ["asset", "agent", "online"] as const) {
      const found = await item(() => useFleetPrerequisite(need));
      const { container } = renderInSpanish(
        <p>{found.label}. {found.detail} {found.cta?.label}</p>,
      );
      expect(found.state).toBe("missing");
      expectNoEnglish(container);
    }
    const asset = await item(() => useFleetPrerequisite("asset"));
    expect(asset.detail).toBe("Aún no hay ningún equipo registrado.");
    expect(asset.cta).toEqual({ to: "/devices/onboard", label: "Añadir un equipo" });
  });

  it("counts computers with Spanish plurals", async () => {
    stubApi({ listDevices: vi.fn().mockResolvedValue(fleet({ total: 1, managed: 1, online: 1 })) });
    expect((await item(() => useFleetPrerequisite("asset"))).detail).toBe("1 equipo está registrado.");
    expect((await item(() => useFleetPrerequisite("agent"))).detail).toBe("1 equipo ejecuta el agente.");
    expect((await item(() => useFleetPrerequisite("online"))).detail).toBe("1 de 1 equipo con el agente está conectado.");

    stubApi({ listDevices: vi.fn().mockResolvedValue(fleet({ total: 7, managed: 5, online: 3 })) });
    expect((await item(() => useFleetPrerequisite("asset"))).detail).toBe("7 equipos están registrados.");
    expect((await item(() => useFleetPrerequisite("agent"))).detail).toBe("5 equipos ejecutan el agente.");
    expect((await item(() => useFleetPrerequisite("online"))).detail).toBe("3 de 5 equipos con el agente están conectados.");

    stubApi({ listDevices: vi.fn().mockResolvedValue(fleet({ total: 4, managed: 4, online: 0 })) });
    const offline = await item(() => useFleetPrerequisite("online"));
    expect(offline.detail).toBe("Ninguno de los 4 equipos que ejecutan el agente está conectado ahora mismo.");
    expect(offline.cta?.label).toBe("Ver equipos sin conexión");
  });

  it("reads groups, packages and notification rules in Spanish", async () => {
    stubApi();
    const groups = await item(() => useGroupsPrerequisite());
    expect(groups.label).toBe("Existe un grupo de dispositivos");
    expect(groups.detail).toBe("Aún no existe ningún grupo de dispositivos.");
    expect(groups.cta?.label).toBe("Crear un grupo de dispositivos");

    const installer = await item(() => useInstallerPrerequisite());
    expect(installer.detail).toBe("Aún no hay un paquete del agente publicado, así que no hay nada que instalar.");
    expect(installer.cta?.label).toBe("Publicar un paquete del agente");
    const forWindows = await item(() => useInstallerPrerequisite({ platforms: [{ os: "windows" }, { os: "darwin" }] }));
    expect(forWindows.detail).toBe("Aún no hay un paquete del agente publicado para Windows o macOS.");

    stubApi({ listAgentReleases: vi.fn().mockResolvedValue([release()]) });
    const published = await item(() => useInstallerPrerequisite());
    // The platform name itself belongs to the agent-releases module's labels.
    expect(published.detail).toMatch(/^Publicado para Linux \(.+\)\.$/);

    const rules = await item(() => useRulesPrerequisite("health."));
    expect(rules.detail).toBe("Ninguna regla de notificación activa envía esto a nadie, así que nadie recibe el aviso.");
    expect(rules.cta?.label).toBe("Enviar esto a alguien");
  });

  it("reads the shared calls to action from the active language when they are rendered", () => {
    expect(ADD_COMPUTER_CTA.label).toBe("Add a computer");
    setActiveLanguage("es");
    expect(ADD_COMPUTER_CTA.label).toBe("Añadir un equipo");
    expect(CREATE_GROUP_CTA).toEqual({ to: "/device-groups", label: "Crear un grupo de dispositivos" });
  });
});
