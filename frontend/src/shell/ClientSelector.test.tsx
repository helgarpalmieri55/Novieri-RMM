/**
 * The top bar's client selector (phase 3 design §1.1): "All my clients", then
 * the person's clients from /me by name, Unassigned last. A choice is stored
 * (`itms.client`) and narrows every request from then on.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, renderHook, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { setActiveLanguage } from "../i18n/core";
import type { ApiClient, Me, MeClient } from "../lib/api";
import { CLIENT_STORAGE_KEY, selectedClient, setSelectedClient } from "../lib/clientChoice";
import { ClientScopeProvider } from "../lib/clientScope";
import { ClientSelector, useClientMark } from "./ClientSelector";

const mockUseApi = vi.fn();
vi.mock("../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const ACME = "0b6a2c1e-6d2f-4a51-9c3e-2f4d1a7b8c01";
const ZETA = "5c9e1d2f-3a4b-4c5d-8e6f-7a8b9c0d1e02";
const INTERNAL = "9f8e7d6c-5b4a-4321-8fed-cba987654303";
const UNASSIGNED = "1a2b3c4d-5e6f-4789-8abc-def012345604";

function client(id: string, name: string, kind: MeClient["kind"] = "customer"): MeClient {
  return { id, name, kind, enabled_modules: [], permissions: ["devices.read"] };
}

const ME: Me = {
  user_id: "u1",
  org_id: INTERNAL,
  email: "tech@example.com",
  display_name: "Tech",
  permissions: ["devices.read"],
  clients: [
    client(ZETA, "Zeta Foods"),
    client(UNASSIGNED, "Unassigned", "unassigned"),
    client(INTERNAL, "Toeshee", "internal"),
    client(ACME, "Acme Dental"),
  ],
};

function renderSelector(me: Me = ME) {
  const meFn = vi.fn().mockResolvedValue(me);
  mockUseApi.mockReturnValue({ me: meFn } as unknown as ApiClient);
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <ClientSelector />
    </QueryClientProvider>,
  );
  return meFn;
}

describe("ClientSelector", () => {
  it("offers All my clients, then the person's clients by name, Unassigned last", async () => {
    renderSelector();
    const box = await screen.findByRole("combobox", { name: "Client" });
    expect(within(box).getAllByRole("option").map((option) => option.textContent)).toEqual([
      "All my clients",
      "Acme Dental",
      "Toeshee",
      "Zeta Foods",
      "Unassigned",
    ]);
    expect(box).toHaveValue("all");
  });

  it("stores the choice, and every client as `all`", async () => {
    const user = userEvent.setup();
    renderSelector();
    const box = await screen.findByRole("combobox", { name: "Client" });

    await user.selectOptions(box, ACME);
    expect(selectedClient()).toBe(ACME);
    expect(window.localStorage.getItem(CLIENT_STORAGE_KEY)).toBe(ACME);
    expect(box).toHaveValue(ACME);

    await user.selectOptions(box, "all");
    expect(selectedClient()).toBeNull();
    expect(window.localStorage.getItem(CLIENT_STORAGE_KEY)).toBe("all");
  });

  it("is not drawn for someone /me lists no client for", async () => {
    const me = renderSelector({ ...ME, clients: [] });
    await waitFor(() => expect(me).toHaveBeenCalled());
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.queryByRole("combobox")).toBeNull();
  });

  it("speaks the person's language", async () => {
    setActiveLanguage("es");
    renderSelector();
    const box = await screen.findByRole("combobox", { name: "Cliente" });
    expect(within(box).getByRole("option", { name: "Todos mis clientes" })).toBeInTheDocument();
    expect(within(box).getByRole("option", { name: "Sin asignar" })).toBeInTheDocument();
  });
});

/**
 * INSIDE THE PROVIDER, a change starts the page again (`lib/clientScope.tsx`'s
 * keyed fragment), and the shell with it: the `<select>` that fired `change`
 * is unmounted and a new one drawn. What that owes the person who made the
 * change: their focus back on the control they were using (WCAG 2.4.3, and
 * Chrome fires `change` on every arrow key on a closed select), and a
 * selector that stays on screen while `/me` is asked again -- not one that
 * vanishes and takes the top bar's layout with it.
 */
describe("a change through the selector, inside the provider", () => {
  const ORDER = ["All my clients", "Acme Dental", "Toeshee", "Zeta Foods", "Unassigned"];

  function renderInScope(me: ReturnType<typeof vi.fn>) {
    mockUseApi.mockReturnValue({ me } as unknown as ApiClient);
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <button type="button">elsewhere</button>
        <ClientScopeProvider>
          <ClientSelector />
        </ClientScopeProvider>
      </QueryClientProvider>,
    );
  }

  it("gives focus back to the new selector when the change was made through it", async () => {
    const user = userEvent.setup();
    renderInScope(vi.fn().mockResolvedValue(ME));
    const before = await screen.findByRole("combobox", { name: "Client" });

    await user.selectOptions(before, ACME);

    const after = await screen.findByRole("combobox", { name: "Client" });
    // NON-VACUITY: the page did start again, so this is a new element.
    expect(before.isConnected).toBe(false);
    expect(after).not.toBe(before);
    expect(after).toHaveValue(ACME);
    expect(document.activeElement).toBe(after);
  });

  it("does not take focus for a change made in another tab", async () => {
    renderInScope(vi.fn().mockResolvedValue(ME));
    const before = await screen.findByRole("combobox", { name: "Client" });
    const elsewhere = screen.getByRole("button", { name: "elsewhere" });
    elsewhere.focus();

    window.localStorage.setItem(CLIENT_STORAGE_KEY, ACME);
    act(() => {
      window.dispatchEvent(new StorageEvent("storage", { key: CLIENT_STORAGE_KEY, newValue: ACME }));
    });

    await waitFor(() => expect(screen.getByRole("combobox", { name: "Client" })).toHaveValue(ACME));
    expect(before.isConnected).toBe(false);
    expect(document.activeElement).toBe(elsewhere);
  });

  it("stays drawn, with the person's clients, while /me is asked again", async () => {
    let answer: (me: Me) => void = () => undefined;
    const me = vi
      .fn()
      .mockResolvedValueOnce(ME)
      .mockImplementation(
        () =>
          new Promise<Me>((resolve) => {
            answer = resolve;
          }),
      );
    const user = userEvent.setup();
    renderInScope(me);
    await user.selectOptions(await screen.findByRole("combobox", { name: "Client" }), ACME);

    // Asked again under the new choice, and not answered yet.
    await waitFor(() => expect(me).toHaveBeenCalledTimes(2));
    const box = screen.getByRole("combobox", { name: "Client" });
    expect(box).toHaveValue(ACME);
    expect(within(box).getAllByRole("option").map((option) => option.textContent)).toEqual(ORDER);
    expect(box).toBeEnabled();
    expect(document.activeElement).toBe(box);

    await act(async () => answer(ME));
    expect(screen.getByRole("combobox", { name: "Client" })).toBe(box);
  });
});

/**
 * WHAT THE PHONE'S TOP BAR MARKS (operator decision 4, 2026-11-02): the
 * client chosen, as a monogram and its full name, for `AppShell`'s drawer
 * toggle. Nothing with every client chosen.
 */
describe("useClientMark", () => {
  function markOf(choice: string | null) {
    setSelectedClient(choice);
    const me = vi.fn().mockResolvedValue(ME);
    mockUseApi.mockReturnValue({ me } as unknown as ApiClient);
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const hook = renderHook(() => useClientMark(), {
      wrapper: ({ children }: { children: ReactNode }) => (
        <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
      ),
    });
    return { ...hook, me };
  }

  it("marks nothing with every client chosen", async () => {
    const { result, me } = markOf(null);
    await waitFor(() => expect(me).toHaveBeenCalled());
    await act(async () => undefined);
    expect(result.current).toBeUndefined();
  });

  it("marks the chosen client by its initials, with its whole name", async () => {
    const { result } = markOf(ACME);
    await waitFor(() => expect(result.current).toEqual({ short: "AD", name: "Acme Dental" }));
  });

  it("names Unassigned in the person's language, and marks it so", async () => {
    const { result } = markOf(UNASSIGNED);
    await waitFor(() => expect(result.current).toEqual({ short: "UN", name: "Unassigned" }));
    act(() => setActiveLanguage("es"));
    expect(result.current).toEqual({ short: "SA", name: "Sin asignar" });
  });

  it("marks nothing for a choice /me does not list", async () => {
    const { result, me } = markOf("7e6d5c4b-3a29-4180-9fed-cba987654305");
    await waitFor(() => expect(me).toHaveBeenCalled());
    await act(async () => undefined);
    expect(result.current).toBeUndefined();
  });

  it("keeps the mark while /me is asked again after a change", async () => {
    // The list the selector keeps (`LastListed`) is what the mark is read
    // from, so it does not blink off on every change.
    const me = vi.fn().mockResolvedValueOnce(ME).mockReturnValue(new Promise<Me>(() => {}));
    mockUseApi.mockReturnValue({ me } as unknown as ApiClient);
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { result } = renderHook(() => useClientMark(), {
      wrapper: ({ children }: { children: ReactNode }) => (
        <QueryClientProvider client={queryClient}>
          <ClientScopeProvider>{children}</ClientScopeProvider>
        </QueryClientProvider>
      ),
    });
    await waitFor(() => expect(me).toHaveBeenCalledTimes(1));
    await act(async () => undefined);

    act(() => setSelectedClient(ZETA));

    await waitFor(() => expect(me).toHaveBeenCalledTimes(2));
    expect(result.current).toEqual({ short: "ZF", name: "Zeta Foods" });
  });
});
