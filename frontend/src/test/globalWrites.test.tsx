/**
 * CONFIGURATION EVERY CLIENT SHARES IS OFFERED FOR WRITING ONLY TO SOMEONE
 * HOLDING THE CODE ON EVERY CLIENT (phase 3 design §2).
 *
 * The routes behind these controls are in `GLOBAL_WRITES`
 * (`backend/tests/test_global_writes.py`): the server refuses them, 403, to a
 * scope that is not every client. So each screen offers the write only with
 * the code in /me's `permissions_everywhere`, and keeps showing the screen --
 * and any write on it that is ONE client's -- with the code in `permissions`.
 *
 * INVARIANT 6: THIS GATES NOTHING; the server does. What is pinned is that the
 * console does not draw a control that can only answer 403.
 *
 * Every read a screen makes beyond /me (and the few named in `answers`) stays
 * in flight, so the only thing that differs between a row's two renders is
 * /me.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient, Me } from "../lib/api";
import { AgentReleasesPage } from "../modules/agent-releases/AgentReleasesPage";
import { AutonomyDialPage } from "../modules/ai-automation/AutonomyDialPage";
import { BudgetAdminPage } from "../modules/budget/BudgetAdminPage";
import { PackagesPage } from "../modules/deploy/PackagesPage";
import { ScriptsPage } from "../modules/deploy/ScriptsPage";
import { EmailIntakeAdminPage } from "../modules/email-intake/EmailIntakeAdminPage";
import { HealthPage } from "../modules/health/HealthPage";
import { ProductsPage } from "../modules/licenses/ProductsPage";
import { ReviewQueue } from "../modules/licenses/ReviewQueue";
import { VendorsPage } from "../modules/licenses/VendorsPage";
import { NotificationAdminPage } from "../modules/notification/NotificationAdminPage";
import { ImagesPage } from "../modules/provisioning/ImagesPage";
import { ProfilesPage } from "../modules/provisioning/ProfilesPage";
import { ServiceDeskAdminPage } from "../modules/service-desk/ServiceDeskAdminPage";
import { SlaAdminPage } from "../modules/sla/SlaAdminPage";

const mockUseApi = vi.fn();
vi.mock("../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

interface Screen {
  route: string;
  page: () => ReactElement;
  /** What the person holds on SOME client: the read, and the write. */
  held: string[];
  /** The code the global write needs on every client. */
  code: string;
  /** The control that writes configuration every client shares. */
  write: string;
  /** A control on the same screen that writes ONE client's data: it stays. */
  stays?: string;
  /** Reads that answer (by `ApiClient` method), for controls drawn per row. */
  answers?: Record<string, unknown>;
}

const SCREENS: Screen[] = [
  { route: "/notifications", page: () => <NotificationAdminPage />, held: ["notification.read", "notification.admin"], code: "notification.admin", write: "Add channel" },
  { route: "/sla", page: () => <SlaAdminPage />, held: ["sla.read", "sla.admin"], code: "sla.admin", write: "Add calendar", stays: "Add policy" },
  { route: "/service-desk/admin", page: () => <ServiceDeskAdminPage />, held: ["ticket.read", "ticket.admin"], code: "ticket.admin", write: "New queue" },
  { route: "/email-intake", page: () => <EmailIntakeAdminPage />, held: ["email_intake.read", "email_intake.admin"], code: "email_intake.admin", write: "Add mailbox" },
  { route: "/deploy/packages", page: () => <PackagesPage />, held: ["deploy.read", "deploy.package.manage"], code: "deploy.package.manage", write: "New package" },
  { route: "/deploy/scripts", page: () => <ScriptsPage />, held: ["deploy.read", "deploy.script.manage"], code: "deploy.script.manage", write: "New script" },
  { route: "/licenses/vendors", page: () => <VendorsPage />, held: ["licenses.read", "licenses.manage"], code: "licenses.manage", write: "New vendor" },
  { route: "/licenses/products", page: () => <ProductsPage />, held: ["licenses.read", "licenses.manage"], code: "licenses.manage", write: "New product" },
  {
    route: "/licenses/review",
    page: () => <ReviewQueue />,
    held: ["licenses.read", "licenses.manage"],
    code: "licenses.manage",
    write: "Map",
    stays: "Ignore",
    answers: { reviewQueue: [{ name: "7-Zip", publisher: "Igor Pavlov", device_count: 4 }] },
  },
  { route: "/budget/admin", page: () => <BudgetAdminPage />, held: ["budget.read", "budget.manage"], code: "budget.manage", write: "New rate", stays: "New category" },
  { route: "/provisioning/images", page: () => <ImagesPage />, held: ["provisioning.read", "provisioning.manage"], code: "provisioning.manage", write: "New image" },
  { route: "/provisioning/profiles", page: () => <ProfilesPage />, held: ["provisioning.read", "provisioning.manage"], code: "provisioning.manage", write: "New profile" },
  { route: "/agent-packages", page: () => <AgentReleasesPage />, held: ["agent.release.read", "agent.release.manage"], code: "agent.release.manage", write: "Publish a release…" },
  { route: "/agent-packages, self-update", page: () => <AgentReleasesPage />, held: ["agent.release.read", "agent.update.read", "agent.update.rollout"], code: "agent.update.rollout", write: "New rollout…" },
  { route: "/assistant/autonomy", page: () => <AutonomyDialPage />, held: ["ai.admin"], code: "ai.admin", write: "Save changes" },
  {
    route: "/health",
    page: () => <HealthPage />,
    held: ["health.read", "health.admin"],
    code: "health.admin",
    write: "Create",
    answers: { listHealthProfiles: [], listHealthAssignments: [], listApprovedActions: [] },
  },
];

function meWith(held: string[], everywhere: string[]): Me {
  return {
    user_id: "u1",
    org_id: "o1",
    email: "op@example.com",
    display_name: "Operator",
    timezone: null,
    permissions: held,
    permissions_everywhere: everywhere,
  };
}

/** `/me` and the named reads answer; every other read stays in flight. */
function stubApi(me: Me, answers: Record<string, unknown> = {}): ReturnType<typeof vi.fn> {
  const meFn = vi.fn().mockResolvedValue(me);
  const known: Record<string, unknown> = { me: meFn };
  for (const [name, value] of Object.entries(answers)) known[name] = vi.fn().mockResolvedValue(value);
  const api = new Proxy(known, {
    get: (target, key) =>
      typeof key !== "string" || key === "then"
        ? undefined
        : (target[key] ?? vi.fn().mockReturnValue(new Promise(() => {}))),
  });
  mockUseApi.mockReturnValue(api as unknown as ApiClient);
  return meFn;
}

function renderPage(page: ReactElement): void {
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>{page}</MemoryRouter>
    </QueryClientProvider>,
  );
}

describe.each(SCREENS)("$route", ({ page, held, code, write, stays, answers }) => {
  it("offers the write to a holder of the code on every client", async () => {
    stubApi(meWith(held, held), answers);
    renderPage(page());
    expect(await screen.findByRole("button", { name: write })).toBeInTheDocument();
  });

  it("does not offer it to a holder of the code on some clients only", async () => {
    const me = stubApi(meWith(held, held.filter((one) => one !== code)), answers);
    renderPage(page());
    await waitFor(() => expect(me).toHaveBeenCalled());
    if (stays) {
      // The screen is still there, and its one-client writes with it.
      expect(await screen.findByRole("button", { name: stays })).toBeInTheDocument();
    } else {
      await new Promise((resolve) => setTimeout(resolve, 20));
    }
    expect(screen.queryByRole("button", { name: write })).toBeNull();
  });
});
