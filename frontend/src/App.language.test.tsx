/**
 * The language, as `App` wires it: a choice is saved to the account, a failed
 * save is said out loud and not reverted, and the saved language on `/me`
 * decides what the console opens in.
 */

import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("./lib/auth", () => ({
  safeReturnTo: () => "/",
  signIn: () => Promise.resolve(),
  completeSignIn: () => Promise.resolve(null),
  isAuthenticated: () => true,
  initAuth: () => Promise.resolve(null),
  signOut: () => Promise.resolve(),
  accessToken: () => null,
  oidcAuthority: "https://itms.example/realms/it-suite",
  isSessionExpiring: () => false,
  onSessionExpiring: () => () => {},
  startAccountAction: () => Promise.resolve(),
  userManager: { events: { addUserLoaded: () => {}, addUserUnloaded: () => {} } },
}));

const apiRef = vi.hoisted(() => ({ current: undefined as unknown }));
vi.mock("./lib/ApiContext", () => ({
  useApi: () => apiRef.current,
  ApiProvider: ({ children }: { children: React.ReactNode }) => children,
}));

const { LanguageBridge, Shell } = await import("./App");
const { useLanguage } = await import("./i18n/context");
const { getLanguage, setActiveLanguage } = await import("./i18n/core");

function withClient(node: React.ReactNode, client = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  return <QueryClientProvider client={client}>{node}</QueryClientProvider>;
}

function MeProbe() {
  // Stands in for the profile page: something that reads the cached `/me`.
  const me = useQuery({ queryKey: ["me"], queryFn: () => (apiRef.current as { me: () => Promise<{ locale: string }> }).me() });
  return <p>saved {me.data?.locale ?? "none"}</p>;
}

function Picker() {
  const { language, setLanguage } = useLanguage();
  return <button onClick={() => setLanguage("es")}>current {language}</button>;
}

afterEach(() => {
  cleanup();
  setActiveLanguage("en");
  try { localStorage.clear(); } catch { /* ignore */ }
});

describe("LanguageBridge", () => {
  it("saves a chosen language to the account", async () => {
    const updateMe = vi.fn().mockResolvedValue({});
    apiRef.current = { updateMe };
    render(withClient(<LanguageBridge><Picker /></LanguageBridge>));
    await userEvent.click(screen.getByRole("button", { name: "current en" }));
    expect(updateMe).toHaveBeenCalledWith({ locale: "es" });
  });

  it("says so, in the new language, when the save fails, and keeps the choice", async () => {
    apiRef.current = { updateMe: vi.fn().mockRejectedValue(new Error("down")) };
    render(withClient(<LanguageBridge><Picker /></LanguageBridge>));
    await userEvent.click(screen.getByRole("button", { name: "current en" }));
    expect(await screen.findByRole("status")).toHaveTextContent("No se pudo guardar el idioma.");
    expect(getLanguage()).toBe("es");
  });
});

describe("LanguageBridge: the sign-in pages", () => {
  it("says so, without an error tone, when the save worked but the sign-in pages did not follow", async () => {
    apiRef.current = {
      updateMe: vi.fn().mockResolvedValue({ locale: "es", identity_locale_synced: false }),
      me: vi.fn().mockResolvedValue({ locale: "es" }),
    };
    render(withClient(<LanguageBridge><Picker /></LanguageBridge>));
    await userEvent.click(screen.getByRole("button", { name: "current en" }));
    const toast = await screen.findByText(/Guardado\. Las páginas de inicio de sesión no se pudieron cambiar/);
    expect(toast).toBeInTheDocument();
    expect(screen.queryByText("No se pudo guardar el idioma.")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(getLanguage()).toBe("es");
  });

  it.each([true, null, undefined])("is silent when identity_locale_synced is %s", async (flag) => {
    const updateMe = vi.fn().mockResolvedValue({ locale: "es", identity_locale_synced: flag });
    apiRef.current = { updateMe, me: vi.fn().mockResolvedValue({ locale: "es" }) };
    render(withClient(<LanguageBridge><Picker /></LanguageBridge>));
    await userEvent.click(screen.getByRole("button", { name: "current en" }));
    await waitFor(() => expect(updateMe).toHaveBeenCalledTimes(1));
    // Let the promise chain behind the click settle before asserting an absence.
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(screen.queryByRole("status")).toBeNull();
  });
});

describe("LanguageBridge: the cached /me", () => {
  it("is refreshed after a successful save, so the profile page never seeds a stale locale", async () => {
    const me = vi.fn().mockResolvedValueOnce({ locale: "en" }).mockResolvedValue({ locale: "es" });
    apiRef.current = { updateMe: vi.fn().mockResolvedValue({}), me };
    render(withClient(<LanguageBridge><Picker /><MeProbe /></LanguageBridge>));
    expect(await screen.findByText("saved en")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "current en" }));
    expect(await screen.findByText("saved es")).toBeInTheDocument();
    expect(me).toHaveBeenCalledTimes(2);
  });
});

describe("Shell: the saved language", () => {
  it("opens the console in the language saved on the account", async () => {
    apiRef.current = {
      me: vi.fn().mockResolvedValue({
        user_id: "u1", display_name: "Ada", email: "ada@example.test",
        org_id: "org-1", roles: [], permissions: [], locale: "es-CO",
      }),
      getCurrentOrg: vi.fn().mockResolvedValue({ id: "org-1", name: "Toeshee" }),
    };
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter><Shell><p>page</p></Shell></MemoryRouter>
      </QueryClientProvider>,
    );
    await waitFor(() => expect(getLanguage()).toBe("es"));
  });
});

describe("LanguageBridge, server-worded data", () => {
  function ServerWorded({ fetches }: { fetches: string[] }) {
    // Stands in for a screen whose text the server wrote in the request's
    // language (report prose, check-type hints, refusals).
    const report = useQuery({
      queryKey: ["reports"],
      queryFn: () => {
        fetches.push(getLanguage());
        return Promise.resolve(getLanguage() === "es" ? "Informe" : "Report");
      },
    });
    return <p>worded {report.data ?? "…"}</p>;
  }

  function ProfilePicker() {
    const { setLanguage } = useLanguage();
    return <button onClick={() => setLanguage("es", { persist: false })}>from profile</button>;
  }

  it("refetches what the server worded when the language changes from the menu", async () => {
    const user = userEvent.setup();
    const fetches: string[] = [];
    const me = vi.fn().mockResolvedValue({ locale: "en" });
    apiRef.current = { me, updateMe: vi.fn().mockResolvedValue({ locale: "es" }) };
    render(withClient(<LanguageBridge><ServerWorded fetches={fetches} /><MeProbe /><Picker /></LanguageBridge>));
    expect(await screen.findByText("worded Report")).toBeInTheDocument();
    await screen.findByText("saved en");
    const meCallsBefore = me.mock.calls.length;
    await user.click(screen.getByRole("button", { name: "current en" }));
    expect(await screen.findByText("worded Informe")).toBeInTheDocument();
    expect(fetches).toEqual(["en", "es"]);
    // `/me` is refreshed by the save, once, never by the language change itself.
    await waitFor(() => expect(me.mock.calls.length).toBe(meCallsBefore + 1));
  });

  it("refetches it when the profile page changes the language without saving through the bridge", async () => {
    const user = userEvent.setup();
    const fetches: string[] = [];
    const updateMe = vi.fn();
    apiRef.current = { me: vi.fn().mockResolvedValue({ locale: "en" }), updateMe };
    render(withClient(<LanguageBridge><ServerWorded fetches={fetches} /><ProfilePicker /></LanguageBridge>));
    expect(await screen.findByText("worded Report")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "from profile" }));
    expect(await screen.findByText("worded Informe")).toBeInTheDocument();
    expect(updateMe).not.toHaveBeenCalled();
  });
});
