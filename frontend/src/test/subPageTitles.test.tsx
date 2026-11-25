/**
 * EVERY SUB-PAGE NAMES ITSELF IN THE TAB (general ruling 1b; DESIGN.md "a
 * unique tab title on every page"; PRODUCT.md principle 1).
 *
 * A page with its own rail entry is titled by the shell: "<entry> · Novieri
 * RMM". A page under that entry with no rail entry of its own puts its h1's
 * catalogue name in front: "<page> · <entry> · Novieri RMM", as
 * `licenses/VendorsPage.tsx` does. A record shown on such a page names the
 * record first: "<record> · <page> · <entry> · Novieri RMM". The final review
 * found ten families where the sub-pages left the tab to the shell, so several
 * pages shared one title; one test per family pins them, in English and
 * Spanish.
 *
 * The page half is the shell's own (`crumbFor`, which `AppShell` reads), so
 * these tests break if a page moves under another rail entry without its tab
 * following. The API is the fixture registry harmony walks.
 */
import { cleanup, screen, waitFor } from "@testing-library/react";
import { useEffect, useState, type ReactElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { fixtureClient, fixtureRegistry, type FixtureMap } from "../fixtures/client";
import { useT } from "../i18n/context";
import { setActiveLanguage } from "../i18n/core";
import type { ApiClient } from "../lib/api";
import { BackupPolicyEditor } from "../modules/backup/BackupPolicyEditor";
import { CoveragePage } from "../modules/backup/CoveragePage";
import { OffsitePage } from "../modules/backup/OffsitePage";
import { RepositoriesPage } from "../modules/backup/RepositoriesPage";
import { PushAuthorizationsPage } from "../modules/agent-push/PushAuthorizationsPage";
import { PushCredentialScopesPage } from "../modules/agent-push/PushCredentialScopesPage";
import { BcpReport } from "../modules/bcp/BcpReport";
import { TestRecorder } from "../modules/bcp/TestRecorder";
import { PackagesPage } from "../modules/deploy/PackagesPage";
import { ScriptsPage } from "../modules/deploy/ScriptsPage";
import { PatchPolicyEditor } from "../modules/patch/PatchPolicyEditor";
import { ProfilePage } from "../modules/profile/ProfilePage";
import { SecurityPage } from "../modules/profile/SecurityPage";
import { ImagesPage } from "../modules/provisioning/ImagesPage";
import { ProfilesPage } from "../modules/provisioning/ProfilesPage";
import { RecordingPlayer } from "../modules/remote/RecordingPlayer";
import { MyWorkPage } from "../modules/service-desk/MyWorkPage";
import { ServiceDeskAdminPage } from "../modules/service-desk/ServiceDeskAdminPage";
import { crumbFor } from "../shell/nav";
import { documentTitle, RecordTitleProvider } from "../shell/title";
import { renderPage } from "./renderPage";

const mockUseApi = vi.fn();
vi.mock("../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

beforeEach(() => mockUseApi.mockReturnValue(fixtureClient(fixtureRegistry()) as ApiClient));
afterEach(() => {
  // Unmount first: a language change re-renders whatever is still mounted.
  cleanup();
  setActiveLanguage("en");
});

/**
 * What `AppShell` does for the tab, from the shell's own crumb for this path
 * (so `/profile`, which sits under no rail entry, is the product alone, as it
 * is in the console), with the record half a page gives `useRecordTitle`.
 */
function InShell({ path, children }: { path: string; children: ReactNode }) {
  useT();
  const [record, setRecord] = useState<string | undefined>();
  const page = crumbFor(path).title;
  useEffect(() => {
    document.title = documentTitle(page, record);
  }, [page, record]);
  return <RecordTitleProvider value={setRecord}>{children}</RecordTitleProvider>;
}

/** One sub-page: where it is, what draws it, and the tab it must have in each language. */
interface Case {
  readonly path: string;
  readonly pattern?: string;
  readonly element: ReactElement;
  readonly en: string;
  readonly es: string;
  /** Fixtures this page needs answered differently in jsdom. */
  readonly over?: FixtureMap;
}

async function expectTitle({ path, pattern, element, over }: Case, title: string) {
  if (over) mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), ...over }) as ApiClient);
  const pathname = path.split("?")[0] ?? path;
  const view = renderPage(path, <InShell path={pathname}>{element}</InShell>, pattern ?? pathname);
  await screen.findAllByRole("heading", { level: 1 });
  await waitFor(() => expect(document.title).toBe(title));
  view.unmount();
}

function family(name: string, cases: readonly Case[]) {
  describe(`${name}: every page has its own tab title (ruling 1b)`, () => {
    it("in English", async () => {
      for (const c of cases) await expectTitle(c, c.en);
      // Unique within the family, and none is the rail entry's own title.
      expect(new Set(cases.map((c) => c.en)).size).toBe(cases.length);
    });

    it("in Spanish", async () => {
      setActiveLanguage("es");
      for (const c of cases) await expectTitle(c, c.es);
      expect(new Set(cases.map((c) => c.es)).size).toBe(cases.length);
    });
  });
}

family("backup", [
  { path: "/backup/repositories", element: <RepositoriesPage />, en: "Repositories · Backup · Novieri RMM", es: "Repositorios · Copias de seguridad · Novieri RMM" },
  { path: "/backup/policies", element: <BackupPolicyEditor />, en: "Backup policies · Backup · Novieri RMM", es: "Políticas de copia de seguridad · Copias de seguridad · Novieri RMM" },
  { path: "/backup/coverage", element: <CoveragePage />, en: "Coverage · Backup · Novieri RMM", es: "Cobertura · Copias de seguridad · Novieri RMM" },
  { path: "/backup/offsite", element: <OffsitePage />, en: "Off-site copy · Backup · Novieri RMM", es: "Copia externa · Copias de seguridad · Novieri RMM" },
]);

family("continuity tests", [
  { path: "/bcp/report", element: <BcpReport />, en: "DR test period report · Continuity tests · Novieri RMM", es: "Informe de pruebas de recuperación por periodo · Pruebas de continuidad · Novieri RMM" },
  // A record page names its record: the test as the plan's list calls it, its type.
  {
    path: "/bcp/tests/test-1",
    pattern: "/bcp/tests/:testId",
    element: <TestRecorder />,
    en: "Restore drill · DR test · Continuity tests · Novieri RMM",
    es: "Simulacro de restauración · Prueba de recuperación · Pruebas de continuidad · Novieri RMM",
  },
  {
    path: "/bcp/tests/test-1/report",
    pattern: "/bcp/tests/:testId/report",
    element: <BcpReport />,
    en: "DR test report · Continuity tests · Novieri RMM",
    es: "Informe de la prueba de recuperación · Pruebas de continuidad · Novieri RMM",
  },
]);

family("software", [
  { path: "/deploy/packages", element: <PackagesPage />, en: "Packages · Software · Novieri RMM", es: "Paquetes · Software · Novieri RMM" },
  { path: "/deploy/scripts", element: <ScriptsPage />, en: "Scripts · Software · Novieri RMM", es: "Scripts · Software · Novieri RMM" },
]);

family("imaging", [
  { path: "/provisioning/images", element: <ImagesPage />, en: "Images · Imaging · Novieri RMM", es: "Imágenes · Imágenes de sistema · Novieri RMM" },
  { path: "/provisioning/profiles", element: <ProfilesPage />, en: "Profiles · Imaging · Novieri RMM", es: "Perfiles · Imágenes de sistema · Novieri RMM" },
]);

family("tickets", [
  { path: "/service-desk/my-work", element: <MyWorkPage />, en: "My work · Tickets · Novieri RMM", es: "Mi trabajo · Tickets · Novieri RMM" },
  { path: "/service-desk/admin", element: <ServiceDeskAdminPage />, en: "Service desk settings · Tickets · Novieri RMM", es: "Configuración de la mesa de ayuda · Tickets · Novieri RMM" },
]);

family("patching", [
  { path: "/patch/policies", element: <PatchPolicyEditor />, en: "Patch policies · Patching · Novieri RMM", es: "Políticas de parches · Parches · Novieri RMM" },
]);

// The authorizations page IS the rail entry ("Push install"), so the shell's
// title is its own; credential scopes is the sub-page, under its own name.
family("push install", [
  { path: "/agent-push/authorizations", element: <PushAuthorizationsPage />, en: "Push install · Novieri RMM", es: "Instalación remota · Novieri RMM" },
  { path: "/agent-push/credential-scopes", element: <PushCredentialScopesPage />, en: "Credential scopes · Push install · Novieri RMM", es: "Ámbitos de credenciales · Instalación remota · Novieri RMM" },
]);

// The live session names the machine; its recording names the machine AND
// that it is the recording, so the two tabs never read the same.
family("remote sessions", [
  {
    path: "/remote/sess-1/recording",
    pattern: "/remote/:sessionId/recording",
    element: <RecordingPlayer />,
    // The fixture's recording is a Blob, which the fixture client's
    // structuredClone turns into a plain object in jsdom. The tab is named
    // from the session and its device, so the file itself is left unanswered.
    over: { getRemoteRecording: () => new Promise<Blob>(() => {}) },
    en: "lis-ws-041 · Session recording · Remote sessions · Novieri RMM",
    es: "lis-ws-041 · Grabación de la sesión · Sesiones remotas · Novieri RMM",
  },
]);

// Reached from the account menu, under no rail entry: the shell's half is the
// product alone, so each page's own name is all that tells the two apart.
family("your account", [
  { path: "/profile", element: <ProfilePage />, en: "Your profile · Novieri RMM", es: "Tu perfil · Novieri RMM" },
  { path: "/profile/security", element: <SecurityPage />, en: "Your security · Novieri RMM", es: "Tu seguridad · Novieri RMM" },
]);

describe("the credential scopes page's h1 (ruling 1b)", () => {
  it("is its own name, not the rail entry it sits under", async () => {
    renderPage("/agent-push/credential-scopes", <PushCredentialScopesPage />);
    expect(await screen.findByRole("heading", { level: 1, name: "Credential scopes" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { level: 1, name: "Push install" })).toBeNull();
  });
});
