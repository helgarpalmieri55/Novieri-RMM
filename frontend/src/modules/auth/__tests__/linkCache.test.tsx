/**
 * The two pages that open a link token in the console -- `/invite#<token>`
 * and `/recover#<token>` -- and what they leave in the query cache (final
 * review D1, Minor 4).
 *
 * THE TOKEN WAS IN A QUERY KEY (`["auth", "invite", token]`), so it stayed in
 * the cache for TanStack's default five minutes after the page unmounted,
 * while the person was on `/sign-in/enroll`. And `LanguageBridge` invalidates
 * every query but `me` on a language switch, which `staleTime: Infinity` does
 * not stop, so the "asked once" comments were false: each switch spent one
 * unit of the per-IP `_LINK` limit the accept step shares, and a refetch that
 * failed (a 429) replaced the password form with an error card.
 *
 * Rendered under the REAL `LanguageBridge`, and the switch is the card's own
 * language button: the path a person takes.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setActiveLanguage } from "../../../i18n/core";
import { ApiError } from "../../../lib/api";
import { authApi, calls, installDefaults, queryCacheText } from "./harness";

vi.mock("../../../lib/ApiContext", () => ({
  useApi: () => authApi(),
  ApiProvider: ({ children }: { children: React.ReactNode }) => children,
}));

const { LanguageBridge } = await import("../../../App");
const { InvitePage } = await import("../InvitePage");
const { RecoverPage } = await import("../RecoverPage");

const GOOD = "a long phrase I can remember";

function renderLink(path: string): QueryClient {
  const url = new URL(path, "https://itms.example");
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <LanguageBridge>
        <MemoryRouter initialEntries={[{ pathname: url.pathname, hash: url.hash }]}>
          <Routes>
            <Route path="/invite" element={<InvitePage />} />
            <Route path="/recover" element={<RecoverPage />} />
            <Route path="/sign-in/enroll" element={<p>first factor</p>} />
          </Routes>
        </MemoryRouter>
      </LanguageBridge>
    </QueryClientProvider>,
  );
  return queryClient;
}

beforeEach(() => {
  installDefaults();
});

afterEach(() => {
  setActiveLanguage("en");
  try {
    localStorage.clear();
  } catch {
    /* storage blocked */
  }
});

describe("the invitation page", () => {
  it("asks once, keeps the token out of the cache, and asks nothing again when the language changes", async () => {
    const user = userEvent.setup();
    const queryClient = renderLink("/invite#tok-invite-1");
    expect(await screen.findByRole("heading", { name: "Welcome, Dana Okafor" })).toBeInTheDocument();
    expect(calls.getInvite).toHaveBeenCalledExactlyOnceWith("tok-invite-1");
    expect(queryCacheText(queryClient)).not.toContain("tok-invite-1");

    await user.click(screen.getByRole("button", { name: "Español" }));

    expect(await screen.findByRole("heading", { name: "Te damos la bienvenida, Dana Okafor" })).toBeInTheDocument();
    expect(calls.getInvite).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText("Contraseña nueva")).toBeInTheDocument();
  });

  it("keeps the password form when a refetch would have been refused", async () => {
    calls.getInvite
      .mockResolvedValueOnce({ email: "dana@example.test", display_name: "Dana Okafor" })
      .mockRejectedValue(new ApiError(429, JSON.stringify({ code: "rate_limited", wait_s: 30 })));
    const user = userEvent.setup();
    renderLink("/invite#tok-invite-1");
    await screen.findByLabelText("New password");

    await user.click(screen.getByRole("button", { name: "Español" }));

    await screen.findByRole("heading", { name: "Te damos la bienvenida, Dana Okafor" });
    expect(screen.getByLabelText("Contraseña nueva")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("leaves nothing of the invitation in the cache once the page is gone", async () => {
    const user = userEvent.setup();
    const queryClient = renderLink("/invite#tok-invite-1");
    await user.type(await screen.findByLabelText("New password"), GOOD);
    await user.type(screen.getByLabelText("The same password again"), GOOD);
    await user.click(screen.getByRole("button", { name: "Set password and continue" }));

    expect(await screen.findByText("first factor")).toBeInTheDocument();
    await waitFor(() => expect(queryClient.getQueryCache().findAll({ queryKey: ["auth"] })).toEqual([]));
    expect(queryCacheText(queryClient)).not.toContain("dana@example.test");
  });
});

describe("the recovery page", () => {
  it("asks once, keeps the token out of the cache, and asks nothing again when the language changes", async () => {
    const user = userEvent.setup();
    const queryClient = renderLink("/recover#tok-recover-1");
    expect(await screen.findByRole("heading", { name: "Recover your account" })).toBeInTheDocument();
    expect(calls.getRecovery).toHaveBeenCalledExactlyOnceWith("tok-recover-1");
    expect(queryCacheText(queryClient)).not.toContain("tok-recover-1");

    await user.click(screen.getByRole("button", { name: "Español" }));

    expect(await screen.findByRole("heading", { name: "Recupera tu cuenta" })).toBeInTheDocument();
    expect(calls.getRecovery).toHaveBeenCalledTimes(1);
  });

  it("leaves nothing of the recovery in the cache once the page is gone", async () => {
    const user = userEvent.setup();
    const queryClient = renderLink("/recover#tok-recover-1");
    await user.click(await screen.findByRole("button", { name: "Recover my account" }));

    expect(await screen.findByText("first factor")).toBeInTheDocument();
    await waitFor(() => expect(queryClient.getQueryCache().findAll({ queryKey: ["auth"] })).toEqual([]));
    expect(queryCacheText(queryClient)).not.toContain("owner@example.test");
  });
});
