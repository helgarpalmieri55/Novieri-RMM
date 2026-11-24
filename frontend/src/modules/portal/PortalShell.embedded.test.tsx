/**
 * THE PORTAL'S FRAME, EMBEDDED (redesign plan, Task 3, pre-flight N1).
 * `/__preview` draws the frame inside the console's own `<main>` so the
 * browser pass measures it; a second `<main>` there is three axe findings
 * (`landmark-main-is-top-level`, `landmark-no-duplicate-main`,
 * `landmark-unique`). Embedded, the frame is a named region and nobody's
 * session; unembedded, it is what a contact gets today.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../lib/api";
import { CLIENT_STORAGE_KEY, setSelectedClient } from "../../lib/clientChoice";
import { hadSession, resetSessionEnd } from "../../lib/sessionEnd";
import { PortalShell } from "./PortalShell";

const ANDES = "5e0c3a7d-2b9f-4d61-8a4e-6f1b0c9d2e57";
const portalMe = vi.fn().mockResolvedValue({
  user_id: "contact-1",
  kind: "contact",
  email: "ana.gomez@andes.example",
  display_name: "Ana Gómez",
  timezone: null,
  permissions: ["portal.ticket.read"],
});
vi.mock("../../lib/ApiContext", () => ({ useApi: () => ({ portalMe }) as unknown as ApiClient }));

function renderFrame(ui: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/__preview"]}>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  setSelectedClient(null);
  resetSessionEnd();
});

describe("the portal's frame, embedded in a page", () => {
  it("draws no second main: it is a region named by the portal", async () => {
    const { container } = renderFrame(
      <main>
        <PortalShell embedded>
          <p>A portal page draws here.</p>
        </PortalShell>
      </main>,
    );
    expect(await screen.findByText("Ana Gómez")).toBeInTheDocument();
    expect(container.querySelectorAll("main")).toHaveLength(1);
    expect(screen.getByRole("region", { name: "Support portal" })).toHaveTextContent("A portal page draws here.");
  });

  it("is nobody's session: it keeps the stored client and records no person", async () => {
    setSelectedClient(ANDES);
    renderFrame(
      <PortalShell embedded>
        <p>A portal page draws here.</p>
      </PortalShell>,
    );
    expect(await screen.findByText("Ana Gómez")).toBeInTheDocument();
    expect(localStorage.getItem(CLIENT_STORAGE_KEY)).toBe(ANDES);
    expect(hadSession()).toBe(false);
  });

  it("is not vacuous: the frame a contact gets still draws its main and forgets the stored client", async () => {
    setSelectedClient(ANDES);
    renderFrame(
      <PortalShell>
        <p>A portal page draws here.</p>
      </PortalShell>,
    );
    expect(await screen.findByText("Ana Gómez")).toBeInTheDocument();
    expect(screen.getByRole("main")).toHaveTextContent("A portal page draws here.");
    expect(localStorage.getItem(CLIENT_STORAGE_KEY)).not.toBe(ANDES);
    expect(hadSession()).toBe(true);
  });
});
