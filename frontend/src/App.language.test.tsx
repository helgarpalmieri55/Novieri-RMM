/**
 * The language, as `App` wires it: a choice is saved to the account, a failed
 * save is said out loud and not reverted, and the saved language on `/me`
 * decides what the console opens in.
 */

import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

const apiRef = vi.hoisted(() => ({ current: undefined as unknown }));
vi.mock("./lib/ApiContext", () => ({
  useApi: () => apiRef.current,
  ApiProvider: ({ children }: { children: React.ReactNode }) => children,
}));

const { LanguageBridge, Shell } = await import("./App");
const { sessionEnded } = await import("./lib/sessionEnd");
const { useLanguage } = await import("./i18n/context");
const { getLanguage, setActiveLanguage } = await import("./i18n/core");

/**
 * The tree inside a query client, SIGNED IN unless told otherwise: the bridge
 * saves a language to the account only while `/me` is known and no 401 has
 * been seen (`lib/session.ts::hasSession`, plan decision D20). Seeded with
 * the profile `MeProbe` reads first, so its first answer is the same one.
 */
function withClient(node: React.ReactNode, { signedIn = true }: { signedIn?: boolean } = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  if (signedIn) client.setQueryData(["me"], { locale: "en" });
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

describe("LanguageBridge without a session (D20)", () => {
  /**
   * THE BRIDGE IS ALSO ON THE SIGN-IN PAGES: it sits outside `RequireAuth`.
   * There, a switch used to `PATCH /me` with no session; the 401 sent the
   * browser to sign in, from the sign-in page, which loaded the page again.
   * The choice stays in this browser (`itms.lang`) and nothing is sent.
   */
  it("saves nothing to an account on a page reached without one", async () => {
    const updateMe = vi.fn().mockResolvedValue({});
    apiRef.current = { updateMe };
    render(withClient(<LanguageBridge><Picker /></LanguageBridge>, { signedIn: false }));
    await userEvent.click(screen.getByRole("button", { name: "current en" }));
    expect(getLanguage()).toBe("es");
    expect(updateMe).not.toHaveBeenCalled();
  });

  it("saves nothing once the session has ended, though /me is still cached", async () => {
    const updateMe = vi.fn().mockResolvedValue({});
    apiRef.current = { updateMe };
    render(withClient(<LanguageBridge><Picker /></LanguageBridge>));
    act(() => sessionEnded());
    await userEvent.click(screen.getByRole("button", { name: "current en" }));
    expect(getLanguage()).toBe("es");
    expect(updateMe).not.toHaveBeenCalled();
  });
});

describe("LanguageBridge: the sign-in pages", () => {
  // RE-POINTED (own authentication phase 1): these pinned a note for a
  // language the identity provider's sign-in pages did not take (a flag
  // `PATCH /me` used to carry). Those pages are this console's own now and
  // the flag is gone (Task 8), so a save that worked says nothing at all --
  // which is what is pinned now.
  it("says nothing after a save that worked: the sign-in pages are the console's own", async () => {
    const updateMe = vi.fn().mockResolvedValue({ locale: "es" });
    apiRef.current = { updateMe, me: vi.fn().mockResolvedValue({ locale: "es" }) };
    render(withClient(<LanguageBridge><Picker /></LanguageBridge>));
    await userEvent.click(screen.getByRole("button", { name: "current en" }));
    await waitFor(() => expect(updateMe).toHaveBeenCalledTimes(1));
    // Let the promise chain behind the click settle before asserting an absence.
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(screen.queryByText(/páginas de inicio de sesión no se pudieron/)).toBeNull();
    // Kept from the test this re-points: a save that worked is not a failure.
    expect(screen.queryByText("No se pudo guardar el idioma.")).toBeNull();
    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(getLanguage()).toBe("es");
  });

  it.each([{ locale: "es" }, { locale: "es", display_name: "Ada" }, {}])(
    "is silent whatever the saved profile carries (%o)",
    async (answer) => {
      const updateMe = vi.fn().mockResolvedValue(answer);
      apiRef.current = { updateMe, me: vi.fn().mockResolvedValue({ locale: "es" }) };
      render(withClient(<LanguageBridge><Picker /></LanguageBridge>));
      await userEvent.click(screen.getByRole("button", { name: "current en" }));
      await waitFor(() => expect(updateMe).toHaveBeenCalledTimes(1));
      // Let the promise chain behind the click settle before asserting an absence.
      await new Promise((resolve) => setTimeout(resolve, 50));
      expect(screen.queryByRole("status")).toBeNull();
    },
  );
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
