/**
 * A PAGE'S h1 IS ITS NAME IN THE NAVIGATION (coordinator ruling, fix round 1 of
 * wave O1, every wave): the rail entry's own catalogue value -- or, for a page
 * the rail does not list, the label the shell's navigation model gives it, the
 * same one the browser tab carries -- so the rail, the crumb, the tab and the
 * heading cannot name one page two ways. Checked in both languages, against
 * the catalogue rather than a copy of its words. A record (the device, a push
 * run) is named by its record instead, and is not here.
 */
import { screen } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { fixtureClient } from "../../fixtures/client";
import { setActiveLanguage, translate, type MessageKey } from "../../i18n/core";
import { renderPage } from "../../test/renderPage";
import { AgentReleasesPage } from "../agent-releases/AgentReleasesPage";
import { PushAuthorizationsPage } from "../agent-push/PushAuthorizationsPage";
import { PushCredentialScopesPage } from "../agent-push/PushCredentialScopesPage";
import { PushRunsPage } from "../agent-push/PushRunsPage";
import { SoftwarePage } from "../inventory/SoftwarePage";
import { OnboardPage } from "../onboarding/OnboardPage";
import { CandidatesPage } from "./CandidatesPage";
import { DevicesPage } from "./DevicesPage";
import { GroupsPage } from "./GroupsPage";

const mockUseApi = vi.fn(() => fixtureClient());
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));
vi.mock("../../lib/ws", () => ({ useLiveEvents: () => undefined }));

afterEach(() => setActiveLanguage("en"));

const PAGES: readonly { route: string; page: () => ReactElement; label: MessageKey }[] = [
  { route: "/devices", page: () => <DevicesPage />, label: "shell.nav.item.devices" },
  { route: "/software", page: () => <SoftwarePage />, label: "shell.nav.page.installedSoftware" },
  { route: "/discovered", page: () => <CandidatesPage />, label: "shell.nav.page.discovered" },
  { route: "/device-groups", page: () => <GroupsPage />, label: "shell.nav.item.deviceGroups" },
  { route: "/devices/onboard", page: () => <OnboardPage />, label: "shell.nav.page.addComputer" },
  { route: "/devices/push-runs", page: () => <PushRunsPage />, label: "shell.nav.page.pushRuns" },
  { route: "/agent-push/authorizations", page: () => <PushAuthorizationsPage />, label: "shell.nav.item.pushInstall" },
  // A sub-page under the rail's Push install (the authorizations page): its own
  // name, which its tab puts in front (general ruling 1b; final review I2).
  { route: "/agent-push/credential-scopes", page: () => <PushCredentialScopesPage />, label: "agentPush.scopes.title" },
  { route: "/agent-packages", page: () => <AgentReleasesPage />, label: "shell.nav.item.agentPackages" },
];

describe("each page's h1 is its navigation label", () => {
  for (const language of ["en", "es"] as const) {
    for (const { route, page, label } of PAGES) {
      it(`${route} (${language})`, async () => {
        setActiveLanguage(language);
        renderPage(route, page());
        expect((await screen.findByRole("heading", { level: 1 })).textContent).toBe(translate(label));
      });
    }
  }
});
