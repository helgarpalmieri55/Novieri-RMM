/**
 * Backup in Spanish: the points page (tiles, filter, table, the Run dialog and
 * the restore dialog with its refusals and its confirmation), the policies
 * page and its editor, coverage, repositories, the off-site copy page with the
 * recovery-key wizard and the storage dialog, a device's own Backup tab, the
 * three readiness panels and the sentences the module builds outside React.
 * Device, repository and policy names, paths and the server's own error text
 * read as recorded; the words around them are Spanish. No secret value is
 * used anywhere: the keys typed below are fixtures, not credentials.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { setActiveLanguage, translate } from "../../i18n/core";
import { expectNoEnglish, renderInSpanish } from "../../i18n/test-utils";
import { ApiError } from "../../lib/api";
import type { ApiClient, Device, DeviceGroup, Me } from "../../lib/api";
import { BackupPointsPage } from "./BackupPointsPage";
import { BackupPolicyEditor } from "./BackupPolicyEditor";
import { BackupReadiness, OffsiteReadiness, PolicyReadiness } from "./BackupReadiness";
import {
  COVERAGE_LABEL,
  OFFSITE_LABEL,
  OFFSITE_TARGET_STATE_LABEL,
  NOT_CRASH_CONSISTENT,
  backupPointStateLabel,
  backupRunStateLabel,
  notCrashConsistent,
  repositoryStateLabel,
  restoreTestLabel,
  warningText,
} from "./backupState";
import { BACKUP_NAV_ITEMS } from "./backupNav";
import { CoveragePage } from "./CoveragePage";
import { DeviceBackupTab } from "./DeviceBackupTab";
import { OffsitePage, RecoveryKeyWizard, TEST_STEP_LABEL, TargetDialog } from "./OffsitePage";
import {
  DEVICE_PICKER_EMPTY_HINT,
  devicePickerEmptyHint,
  devicePickerOptions,
  groupPickerOptions,
  repositoryPickerEmptyHint,
  repositoryPickerOptions,
} from "./pickerOptions";
import { RepositoriesPage } from "./RepositoriesPage";
import { runQueuedMessage } from "./runMessage";
import type { BackupManifest, BackupPoint, BackupPolicy, OffsiteTarget, RecoveryKey, Repository } from "./types";
import { windowLabel } from "./windowLabel";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

function renderPage(ui: ReactElement, path = "/backup") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderInSpanish(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

const ME: Me = {
  user_id: "u1",
  org_id: "o1",
  email: "operator@example.com",
  display_name: "Operator",
  permissions: [
    "backup.read",
    "backup.run",
    "backup.restore",
    "backup.policy.manage",
    "backup.offsite.manage",
    "backup.recovery.manage",
    "devices.read",
  ],
};

// ------------------------------------------------------------------ fixtures

function device(over: Partial<Device> & Pick<Device, "id">): Device {
  return {
    hostname: "WKS-042",
    fqdn: null,
    os_family: "windows",
    os_version: "11",
    architecture: "x86_64",
    device_group_id: null,
    status: "active",
    agent_status: "online",
    last_seen_at: "2026-01-02T00:00:00.000Z",
    cert_not_after: null,
    cert_state: "ok",
    cert_revoked: false,
    lifecycle: "in_service",
    asset_type: "computer",
    source: "agent",
    provenance: "agent",
    description: null,
    primary_ip: null,
    owner: null,
    location: null,
    ...over,
  };
}

const DEVICES = {
  items: [
    device({ id: "dev1" }),
    device({ id: "dev2", hostname: "SRV-02", os_family: "linux", agent_status: "offline" }),
    device({ id: "dev3", hostname: "SW-CORE-1", os_family: null, agent_status: null, asset_type: "network", source: "manual" }),
  ],
  total: 3,
};

const REPO: Repository = {
  id: "repo1",
  client_id: "o1",
  name: "bóveda-principal",
  storage_ref: "s3://copias/principal",
  state: "active",
  dedup_params: {},
  created_at: "2026-01-01T00:00:00.000Z",
  offsite_target_id: null,
};
const REPO_OFF: Repository = { ...REPO, id: "repo2", name: "bóveda-antigua", state: "disabled", offsite_target_id: "t1" };

const POINTS: BackupPoint[] = [
  {
    id: "pt1",
    device_id: "dev1",
    repository_id: "repo1",
    backup_run_id: "run1",
    taken_at: "2026-01-02T00:00:00.000Z",
    size: 1024,
    state: "complete",
    verified_at: null,
    restore_tested_at: null,
    crash_consistent: false,
    warnings: ["files_changed_during_read", "codigo_nuevo"],
  },
  {
    id: "pt2",
    device_id: "dev2",
    repository_id: "repo1",
    backup_run_id: "run2",
    taken_at: "2026-01-03T00:00:00.000Z",
    size: 2048,
    state: "failed",
    verified_at: null,
    restore_tested_at: null,
  },
  {
    id: "pt3",
    device_id: "dev2",
    repository_id: "repo1",
    backup_run_id: "run3",
    taken_at: "2026-01-04T00:00:00.000Z",
    size: 2048,
    state: "pruned",
    verified_at: "2026-01-04T01:00:00.000Z",
    restore_tested_at: "2026-01-04T02:00:00.000Z",
    restore_test_scope: "sampled",
  },
  {
    id: "pt4",
    device_id: "dev1",
    repository_id: "repo1",
    backup_run_id: "run4",
    taken_at: "2026-01-05T00:00:00.000Z",
    size: 4096,
    state: "complete",
    verified_at: "2026-01-05T01:00:00.000Z",
    restore_tested_at: "2026-01-05T02:00:00.000Z",
    restore_test_scope: "full",
  },
];

const REPORT = {
  devices_total: 3,
  devices_covered: 1,
  coverage_rate: 0.33,
  run_counts_by_state: { success: 3, failed: 1 },
  success_rate: 0.75,
  points_needing_verification: 1,
  dedup: { bytes_protected_total: 0, bytes_stored_total: 0, dedup_ratio: 1, savings_pct: 0 },
};
const EMPTY_REPORT = { ...REPORT, devices_covered: 0, run_counts_by_state: {}, success_rate: 0, points_needing_verification: 0 };

const MANIFEST: BackupManifest = {
  schema_version: 1,
  streams: [
    {
      type: "files",
      entries: [
        { path: "/etc/app/config.yaml", kind: "file", mode: 0o644, size: 512, mtime: 0, chunk_ids: ["c1"], target: null },
        { path: "/var/datos", kind: "dir", mode: 0o755, size: 2048, mtime: 0, chunk_ids: [], target: null },
      ],
    },
  ],
};

const GROUP: DeviceGroup = {
  id: "grp1",
  client_id: "o1",
  name: "Servidores de Lisboa",
  parent_group_id: null,
  description: "",
  dynamic: false,
  device_count: 3,
  precedence: 0,
  archived_at: null,
};
const GROUPS = { items: [GROUP, { ...GROUP, id: "grp2", name: "Un solo equipo", device_count: 1 }], total: 2, ungrouped_device_count: 0, evaluation: null };

function policy(over: Partial<BackupPolicy> = {}): BackupPolicy {
  return {
    id: "pol1",
    client_id: "o1",
    device_group_id: null,
    repository_id: "repo1",
    sources: {},
    window_cron: null,
    retention_gfs: {},
    enabled: true,
    ...over,
  };
}
const POLICIES: BackupPolicy[] = [
  policy(),
  policy({ id: "pol2", device_group_id: "grp1", window_cron: "0 1-4 * * *", enabled: true }),
  policy({ id: "pol3", window_cron: "*/7 3 * * *", enabled: false }),
];

const KEY: RecoveryKey = {
  id: "k1",
  fingerprint: "0123456789abcdef0123456789abcdef",
  public_key: "cHVibGljLWtleS1ieXRlcy0wMTIzNDU2Nzg5YWJjZGVm",
  proven_at: null,
  challenge_expires_at: "2026-09-24T12:00:00Z",
  created_at: "2026-09-23T12:00:00Z",
  retired_at: null,
};

const TARGET: OffsiteTarget = {
  id: "t1",
  name: "Wasabi UE",
  endpoint: "https://s3.eu-central-1.wasabisys.com",
  region: "eu-central-1",
  bucket: "acme-externo",
  prefix: "itms",
  addressing: "path",
  lock_mode: "compliance",
  immutable_days: 30,
  retention_gfs: { daily: 14 },
  max_mbps: null,
  state: "failing",
  test_requested_at: null,
  tested_at: "2026-09-23T12:00:00Z",
  test_steps: [
    { step: "connect", ok: true, detail: "" },
    { step: "object_lock", ok: false, detail: "" },
  ],
  last_ok_at: null,
  last_error: "El bucket no tiene Object Lock activado.",
};
const TARGET_OK: OffsiteTarget = { ...TARGET, id: "t2", name: "NAS de Oporto", endpoint: null, state: "ok", immutable_days: 1, test_steps: [], last_error: null, tested_at: null };

const COVERAGE = {
  meta: {},
  totals: { devices: 4, covered: 1, stale: 1, failing: 1, unprotected: 1 },
  rows: [
    { device_id: "d1", hostname: "FS-01", repository_name: "bóveda-principal", coverage: "covered", offsite: "current", last_point_taken_at: "2026-09-23T02:05:00Z", last_offsite_at: "2026-09-23T03:00:00Z", last_restore_tested_at: null },
    { device_id: "d2", hostname: "WEB-02", repository_name: null, coverage: "unprotected", offsite: "off", last_point_taken_at: null, last_offsite_at: null, last_restore_tested_at: null },
    { device_id: "d3", hostname: "DB-03", repository_name: "bóveda-principal", coverage: "stale", offsite: "behind", last_point_taken_at: "2026-09-01T02:05:00Z", last_offsite_at: null, last_restore_tested_at: "2026-09-02T02:05:00Z", crash_consistent: false },
    { device_id: "d4", hostname: "APP-04", repository_name: "bóveda-principal", coverage: "failing", offsite: "off", last_point_taken_at: "2026-09-20T02:05:00Z", last_offsite_at: null, last_restore_tested_at: null },
  ],
};

function baseApi(over: Record<string, unknown> = {}) {
  const api = {
    me: vi.fn().mockResolvedValue(ME),
    listBackupPoints: vi.fn().mockResolvedValue(POINTS),
    listDevices: vi.fn().mockResolvedValue(DEVICES),
    listBackupRepositories: vi.fn().mockResolvedValue([REPO]),
    listBackupPolicies: vi.fn().mockResolvedValue(POLICIES),
    listDeviceGroups: vi.fn().mockResolvedValue(GROUPS),
    getBackupReport: vi.fn().mockResolvedValue(REPORT),
    getBackupManifest: vi.fn().mockResolvedValue(MANIFEST),
    runBackup: vi.fn().mockResolvedValue({ job_ids: ["j1"] }),
    restoreBackupPoint: vi.fn().mockResolvedValue({ job_ids: ["r1"] }),
    verifyBackupPoint: vi.fn().mockResolvedValue({ job_ids: ["v1"] }),
    upsertBackupPolicy: vi.fn().mockResolvedValue(POLICIES[0]),
    createBackupRepository: vi.fn().mockResolvedValue(REPO),
    runReport: vi.fn().mockResolvedValue(COVERAGE),
    listRecoveryKeys: vi.fn().mockResolvedValue([]),
    listOffsiteTargets: vi.fn().mockResolvedValue([TARGET, TARGET_OK]),
    registerRecoveryKey: vi.fn().mockResolvedValue({ key: KEY, challenge: "Q0hBTExFTkdF" }),
    proveRecoveryKey: vi.fn().mockResolvedValue({ ...KEY, proven_at: "2026-09-23T12:05:00Z" }),
    newRecoveryKeyChallenge: vi.fn().mockResolvedValue({ key: KEY, challenge: "Q0hBTExFTkdF" }),
    setRepositoryOffsite: vi.fn().mockResolvedValue({}),
    testOffsiteTarget: vi.fn().mockResolvedValue({ ...TARGET, state: "testing" }),
    createOffsiteTarget: vi.fn().mockResolvedValue(TARGET),
    updateOffsiteTarget: vi.fn().mockResolvedValue(TARGET),
    getDeviceBackup: vi.fn().mockResolvedValue({
      device_id: "dev1",
      last_good_point: { backup_point_id: "pt1", taken_at: "2026-01-02T00:00:00Z", size: 1, repository_id: "repo1" },
      last_run_state: "missed",
      last_run_finished_at: "2026-01-02T00:05:00Z",
      verified_at: null,
      restore_tested_at: "2026-01-03T00:00:00Z",
      repository: { id: "repo1", name: "bóveda-principal" },
    }),
    ...over,
  };
  mockUseApi.mockReturnValue(api as unknown as ApiClient);
  return api;
}

beforeEach(() => {
  baseApi();
});

const SERVER_TEXT = ["El bucket no tiene Object Lock activado."];

// -------------------------------------------------------------- points page

describe("the backup points page, in Spanish", () => {
  it("words the head, the tabs, the tiles, the filter, the table and the pills", async () => {
    const { container } = renderPage(<BackupPointsPage />);
    expect(await screen.findByRole("heading", { name: "Copia de seguridad y restauración" })).toBeInTheDocument();
    await screen.findByText("pt1");
    expect(screen.getByRole("button", { name: "Ejecutar copia de seguridad" })).toBeInTheDocument();
    expect(screen.getByRole("tablist", { name: "Copia de seguridad" })).toBeInTheDocument();
    for (const tab of ["Puntos de copia", "Cobertura", "Repositorios", "Políticas", "Copia externa"]) {
      expect(screen.getByRole("tab", { name: tab })).toBeInTheDocument();
    }
    // The tiles.
    expect(screen.getByText("Éxito de las copias")).toBeInTheDocument();
    expect(screen.getByText("75 %")).toBeInTheDocument();
    expect(screen.getByText("Equipos protegidos")).toBeInTheDocument();
    expect(screen.getByText("1 / 3")).toBeInTheDocument();
    expect(screen.getByText("Tareas fallidas")).toBeInTheDocument();
    expect(screen.getByText("Requieren verificación")).toBeInTheDocument();
    // The filter and the panel.
    expect(screen.getByText("Puntos de restauración")).toBeInTheDocument();
    expect(screen.getByText("Un punto solo queda demostrado cuando se ha probado su restauración.")).toBeInTheDocument();
    const filter = screen.getByRole("combobox", { name: "Filtrar los puntos de copia" });
    expect(within(filter).getByRole("option", { name: "Todos los puntos de copia" })).toBeInTheDocument();
    expect(within(filter).getByRole("option", { name: "Pendientes de verificación" })).toBeInTheDocument();
    for (const header of ["Dispositivo", "Repositorio", "Tomado", "Tamaño", "Estado", "Datos comprobados", "Restauración probada"]) {
      expect(screen.getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    expect(screen.getByText("Datos comprobados")).toHaveAttribute(
      "title",
      "Si se han comprobado los datos almacenados del punto en busca de corrupción.",
    );
    expect(screen.getByText("Restauración probada")).toHaveAttribute(
      "title",
      "Si se han restaurado archivos de este punto, como prueba.",
    );
    // The rows.
    const row = (id: string) => screen.getByText(id).closest("tr") as HTMLElement;
    expect(within(row("pt1")).getByText("Completo")).toBeInTheDocument();
    expect(within(row("pt1")).getByText("No consistente ante fallos: los archivos se leyeron mientras estaban en uso")).toBeInTheDocument();
    expect(within(row("pt1")).getByText("Algunos archivos cambiaron mientras se copiaban.")).toBeInTheDocument();
    expect(within(row("pt1")).getByText("codigo_nuevo")).toBeInTheDocument();
    expect(within(row("pt1")).getByText("No verificado")).toBeInTheDocument();
    expect(within(row("pt1")).getByText("No probado")).toBeInTheDocument();
    expect(within(row("pt2")).getByText("Fallido")).toBeInTheDocument();
    expect(within(row("pt3")).getByText("Eliminado por retención")).toBeInTheDocument();
    expect(within(row("pt3")).getByText("Verificado")).toBeInTheDocument();
    expect(within(row("pt3")).getByText("Probado (muestra)")).toBeInTheDocument();
    expect(within(row("pt4")).getByText("Probado")).toBeInTheDocument();
    expect(within(row("pt2")).getByRole("button", { name: "Restaurar" })).toHaveAttribute(
      "title",
      "Esta ejecución falló, así que no se capturó ningún punto completo desde el que restaurar.",
    );
    expect(within(row("pt3")).getByRole("button", { name: "Restaurar" })).toHaveAttribute(
      "title",
      "Este punto superó la retención del repositorio y ya no se almacena.",
    );
    expectNoEnglish(container, ["WKS-042", "SRV-02"]);
  });

  it("says nothing has run, and what each empty answer means", async () => {
    baseApi({ listBackupPoints: vi.fn().mockResolvedValue([]), getBackupReport: vi.fn().mockResolvedValue(EMPTY_REPORT) });
    const { container } = renderPage(<BackupPointsPage />);
    expect(await screen.findByText("Todavía no hay puntos de copia registrados.")).toBeInTheDocument();
    expect(screen.getByText("Nunca ejecutada")).toBeInTheDocument();
    expect(screen.getByText("Nunca se ha ejecutado ninguna copia de seguridad")).toBeInTheDocument();
    expect(screen.getByText("No se ha capturado ningún punto de copia")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("says a filter found nothing, and that no run failed", async () => {
    const user = userEvent.setup();
    baseApi({
      listBackupPoints: vi.fn().mockResolvedValue([POINTS[3]]),
      getBackupReport: vi.fn().mockResolvedValue({ ...REPORT, run_counts_by_state: { success: 2 }, points_needing_verification: 0 }),
    });
    const { container } = renderPage(<BackupPointsPage />);
    await screen.findByText("pt4");
    expect(screen.getByText("Ninguna copia de seguridad ha fallado")).toBeInTheDocument();
    expect(screen.getByText("Todos los puntos completados están verificados")).toBeInTheDocument();
    await user.selectOptions(screen.getByRole("combobox", { name: "Filtrar los puntos de copia" }), "pending");
    expect(await screen.findByText("Ningún punto de copia está pendiente de verificación.")).toBeInTheDocument();
    expectNoEnglish(container, ["WKS-042"]);
  });

  it("re-words the page when the language is switched", async () => {
    renderPage(<BackupPointsPage />);
    await screen.findByText("pt1");
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("heading", { name: "Backup & Restore" })).toBeInTheDocument();
    expect(screen.getByText("75%")).toBeInTheDocument();
    const row = screen.getByText("pt3").closest("tr") as HTMLElement;
    expect(within(row).getByText("Pruned")).toBeInTheDocument();
    expect(within(row).getByText("Tested (sample)")).toBeInTheDocument();
    expect(within(screen.getByText("pt1").closest("tr") as HTMLElement).getByText("Some files changed while they were being copied.")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Off-site copy" })).toBeInTheDocument();
  });
});

describe("the Run backup dialog, in Spanish", () => {
  async function open(user: ReturnType<typeof userEvent.setup>) {
    renderPage(<BackupPointsPage />);
    await screen.findByText("pt1");
    await user.click(screen.getByRole("button", { name: "Ejecutar copia de seguridad" }));
    return screen.findByRole("dialog", { name: "Ejecutar copia de seguridad" });
  }

  it("names the missing device on submit, and re-words the refusal on a switch", async () => {
    const user = userEvent.setup();
    const dialog = await open(user);
    await user.click(within(dialog).getByRole("button", { name: "Ejecutar" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("El campo Dispositivo es obligatorio.");
    expectNoEnglish(dialog);
    act(() => setActiveLanguage("en"));
    expect(within(screen.getByRole("dialog", { name: "Run backup" })).getByRole("alert")).toHaveTextContent(
      "Device is required",
    );
  });

  it("words the picker's sublabels and says what was queued", async () => {
    const user = userEvent.setup();
    const api = baseApi();
    const dialog = await open(user);
    await user.click(within(dialog).getByRole("combobox", { name: "Dispositivo" }));
    expect(await screen.findByRole("option", { name: /WKS-042.*windows · en línea/ })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /SRV-02.*linux · sin conexión/ })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /SW-CORE-1.*sin agente/ })).toBeInTheDocument();
    expectNoEnglish(dialog, ["WKS-042", "SRV-02", "SW-CORE-1"]);
    await user.click(screen.getByRole("option", { name: /WKS-042/ }));
    await user.click(within(dialog).getByRole("button", { name: "Ejecutar" }));
    expect(await within(dialog).findByRole("status")).toHaveTextContent("Copia de seguridad en cola: 1 tarea enviada.");
    expect(api.runBackup).toHaveBeenCalledWith("dev1");
    act(() => setActiveLanguage("en"));
    expect(within(screen.getByRole("dialog", { name: "Run backup" })).getByRole("status")).toHaveTextContent(
      "Backup queued — 1 job dispatched.",
    );
  });

  it("says nothing was dispatched when the run created no job", async () => {
    const user = userEvent.setup();
    baseApi({ runBackup: vi.fn().mockResolvedValue({ job_ids: [] }) });
    const dialog = await open(user);
    await user.click(within(dialog).getByRole("combobox", { name: "Dispositivo" }));
    await user.click(await screen.findByRole("option", { name: /WKS-042/ }));
    await user.click(within(dialog).getByRole("button", { name: "Ejecutar" }));
    expect(await within(dialog).findByRole("status")).toHaveTextContent(
      "No se envió nada: no se creó ninguna tarea de copia de seguridad para ese dispositivo.",
    );
    expectNoEnglish(dialog, ["WKS-042"]);
  });

  it("explains the dead end when there are no devices, and links to adding a computer", async () => {
    const user = userEvent.setup();
    baseApi({ listDevices: vi.fn().mockResolvedValue({ items: [], total: 0 }) });
    const dialog = await open(user);
    await user.click(await within(dialog).findByRole("combobox", { name: "Dispositivo" }));
    expect(await within(dialog).findByText(devicePickerEmptyHint())).toBeInTheDocument();
    expect(within(dialog).getByRole("link", { name: "Añadir un equipo" })).toBeInTheDocument();
    expectNoEnglish(dialog);
  });
});

describe("the restore dialog, in Spanish", () => {
  async function open(user: ReturnType<typeof userEvent.setup>) {
    renderPage(<BackupPointsPage />);
    await screen.findByText("pt1");
    await user.click(within(screen.getByText("pt1").closest("tr") as HTMLElement).getByRole("button", { name: "Restaurar" }));
    await screen.findByText("/etc/app/config.yaml");
    return screen.getByRole("dialog", { name: "Restaurar un punto de copia" });
  }
  const submit = (dialog: HTMLElement) => within(dialog).getAllByRole("button", { name: "Restaurar" }).at(-1) as HTMLElement;

  it("words the manifest, the targets and the verify and restore controls", async () => {
    const user = userEvent.setup();
    const dialog = await open(user);
    expect(within(dialog).getByText(/WKS-042 · tomado el/)).toBeInTheDocument();
    expect(within(dialog).getByText("Destino de la restauración")).toBeInTheDocument();
    expect(within(dialog).getByText("Ubicación original")).toBeInTheDocument();
    expect(within(dialog).getByText("— sobrescribe los archivos que hay ahora en este dispositivo")).toBeInTheDocument();
    expect(within(dialog).getByText("Ubicación alternativa")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Verificar" })).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Cancelar" })).toBeInTheDocument();
    await user.click(within(dialog).getByRole("radio", { name: "Ubicación alternativa" }));
    expect(within(dialog).getByLabelText("Ruta de destino alternativa")).toBeInTheDocument();
    expectNoEnglish(dialog, ["WKS-042"]);
  });

  it("names the missing selection and the missing path, and re-words them on a switch", async () => {
    const user = userEvent.setup();
    const dialog = await open(user);
    await user.click(within(dialog).getByRole("radio", { name: "Ubicación alternativa" }));
    await user.click(submit(dialog));
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("Selecciona al menos un archivo o carpeta que restaurar");
    expect(alert).toHaveTextContent("La ruta de destino alternativa es obligatoria al restaurar en una ubicación alternativa");
    expectNoEnglish(dialog, ["WKS-042"]);
    act(() => setActiveLanguage("en"));
    const again = within(screen.getByRole("dialog", { name: "Restore backup point" })).getByRole("alert");
    expect(again).toHaveTextContent("Select at least one file or folder to restore");
    expect(again).toHaveTextContent("Alternate target path is required when restoring to an alternate location");
  });

  it("asks before overwriting the original location, with the count agreed", async () => {
    const user = userEvent.setup();
    const api = baseApi();
    const dialog = await open(user);
    await user.click(within(dialog).getAllByRole("checkbox")[0]!);
    await user.click(submit(dialog));
    const confirm = await screen.findByRole("dialog", { name: "Restaurar archivos en este dispositivo" });
    expect(confirm).toHaveTextContent("Se volverá a escribir 1 elemento del punto de copia tomado el");
    expect(confirm).toHaveTextContent(
      "Van a sus rutas originales en el dispositivo y sustituyen lo que hay ahora. Después no se podrá recuperar el contenido actual.",
    );
    expect(confirm).toHaveTextContent("La restauración se ejecuta como una tarea firmada y no se puede cancelar desde la consola.");
    expectNoEnglish(confirm, ["WKS-042"]);
    await user.click(within(confirm).getByRole("button", { name: "Restaurar archivos" }));
    await waitFor(() => expect(api.restoreBackupPoint).toHaveBeenCalled());
  });

  it("agrees the count for two items", async () => {
    const user = userEvent.setup();
    const dialog = await open(user);
    for (const box of within(dialog).getAllByRole("checkbox")) await user.click(box);
    await user.click(submit(dialog));
    const confirm = await screen.findByRole("dialog", { name: "Restaurar archivos en este dispositivo" });
    expect(confirm).toHaveTextContent("Se volverán a escribir 2 elementos del punto de copia tomado el");
  });

  it("words a failed verification, with the server's reason as recorded", async () => {
    const user = userEvent.setup();
    baseApi({ verifyBackupPoint: vi.fn().mockRejectedValue(new ApiError(409, "El repositorio no responde.")) });
    const dialog = await open(user);
    await user.click(within(dialog).getByRole("button", { name: "Verificar" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("El repositorio no responde.");
    expectNoEnglish(dialog, ["WKS-042", "El repositorio no responde."]);
  });

  it("says the point has no entries", async () => {
    const user = userEvent.setup();
    baseApi({ getBackupManifest: vi.fn().mockResolvedValue({ schema_version: 1, streams: [] }) });
    renderPage(<BackupPointsPage />);
    await screen.findByText("pt1");
    await user.click(within(screen.getByText("pt1").closest("tr") as HTMLElement).getByRole("button", { name: "Restaurar" }));
    expect(await screen.findByText("Este punto de copia no tiene entradas.")).toBeInTheDocument();
  });
});

// ------------------------------------------------------------------ policies

describe("the backup policies page, in Spanish", () => {
  it("words the head, the tiles, the filter, the table and the windows in words", async () => {
    const { container } = renderPage(<BackupPolicyEditor />, "/backup/policies");
    expect(await screen.findByRole("heading", { name: "Políticas de copia de seguridad" })).toBeInTheDocument();
    await screen.findByText("Servidores de Lisboa");
    expect(screen.getByRole("button", { name: "Nueva política" })).toBeInTheDocument();
    expect(screen.getByText("Escriben en 1 repositorio")).toBeInTheDocument();
    expect(screen.getByText("1 sujeta a una ventana de mantenimiento")).toBeInTheDocument();
    expect(screen.getByText("Configuradas pero sin programar nada")).toBeInTheDocument();
    const filter = screen.getByRole("combobox", { name: "Filtrar las políticas por estado" });
    for (const option of ["Todas las políticas", "Habilitadas", "Deshabilitadas"]) {
      expect(within(filter).getByRole("option", { name: option })).toBeInTheDocument();
    }
    for (const header of ["Ámbito", "Repositorio", "Ventana", "Estado"]) {
      expect(screen.getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    // A group-less policy is ONE client's default (MSP clients phase 2, Task
    // 13), so its scope no longer says "every device with an agent".
    expect(screen.getAllByText("Todos los dispositivos del cliente que ninguna política de grupo cubre")).toHaveLength(2);
    expect(screen.getAllByText("Siempre abierta")).toHaveLength(1);
    expect(screen.getByText("Todos los días, de 01:00 a 05:00")).toBeInTheDocument();
    expect(screen.getByText("*/7 3 * * *")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("says why there are no policies", async () => {
    baseApi({ listBackupPolicies: vi.fn().mockResolvedValue([]) });
    const { container } = renderPage(<BackupPolicyEditor />, "/backup/policies");
    expect(await screen.findByText("No hay políticas de copia de seguridad configuradas.")).toBeInTheDocument();
    expect(screen.getByText("No hay ninguna política de copia de seguridad configurada")).toBeInTheDocument();
    expect(screen.getByText("No existe ninguna política que habilitar")).toBeInTheDocument();
    expect(screen.getByText("No existe ninguna política que deshabilitar")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("says every policy is disabled, and no policy is held to a window", async () => {
    baseApi({ listBackupPolicies: vi.fn().mockResolvedValue([POLICIES[2]]) });
    const first = renderPage(<BackupPolicyEditor />, "/backup/policies");
    await screen.findByText("*/7 3 * * *");
    expect(screen.getByText("Todas las políticas están deshabilitadas, así que no hay nada programado")).toBeInTheDocument();
    expectNoEnglish(first.container);
    first.unmount();
    baseApi({ listBackupPolicies: vi.fn().mockResolvedValue([POLICIES[0]]) });
    const second = renderPage(<BackupPolicyEditor />, "/backup/policies");
    await screen.findByText("Siempre abierta");
    expect(screen.getByText("Ninguna sujeta a una ventana")).toBeInTheDocument();
    expect(screen.getByText("Todas las políticas están habilitadas")).toBeInTheDocument();
    expectNoEnglish(second.container);
  });

  it("opens the editor, names the missing repository, and re-words it on a switch", async () => {
    const user = userEvent.setup();
    renderPage(<BackupPolicyEditor />, "/backup/policies");
    await screen.findByText("Servidores de Lisboa");
    await user.click(screen.getByRole("button", { name: "Nueva política" }));
    const dialog = await screen.findByRole("dialog", { name: "Nueva política" });
    expect(within(dialog).getByText("Ventana de copia (UTC, opcional)")).toBeInTheDocument();
    expect(within(dialog).getByText("Habilitada")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("combobox", { name: "Se aplica a" }));
    expect(
      await screen.findByRole("option", {
        name: /Predeterminada del cliente.*Todos los dispositivos del cliente que ninguna política de grupo cubre/,
      }),
    ).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /Servidores de Lisboa.*3 dispositivos/ })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /Un solo equipo.*1 dispositivo$/ })).toBeInTheDocument();
    await user.click(screen.getByRole("option", { name: /Un solo equipo/ }));
    await user.click(within(dialog).getByRole("button", { name: "Guardar" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("El campo Repositorio es obligatorio.");
    expectNoEnglish(dialog, ["Servidores de Lisboa", "Un solo equipo"]);
    act(() => setActiveLanguage("en"));
    expect(within(screen.getByRole("dialog", { name: "New policy" })).getByRole("alert")).toHaveTextContent(
      "Repository is required",
    );
  });

  it("opens the editor for an existing policy and explains an empty repository list", async () => {
    const user = userEvent.setup();
    baseApi({ listBackupRepositories: vi.fn().mockResolvedValue([]) });
    renderPage(<BackupPolicyEditor />, "/backup/policies");
    await screen.findByText("Servidores de Lisboa");
    await user.click(within(screen.getByText("Servidores de Lisboa").closest("tr") as HTMLElement).getByRole("button", { name: "Editar" }));
    const dialog = await screen.findByRole("dialog", { name: "Editar política" });
    await user.click(within(dialog).getByRole("combobox", { name: "Repositorio" }));
    expect(await within(dialog).findByText(repositoryPickerEmptyHint())).toBeInTheDocument();
    expect(within(dialog).getByRole("link", { name: "Crear un repositorio de copia de seguridad" })).toBeInTheDocument();
    expectNoEnglish(dialog, ["Servidores de Lisboa"]);
  });
});

// ------------------------------------------------------------------ coverage

describe("the coverage page, in Spanish", () => {
  it("words the tiles, the filter, the verdicts and the off-site column", async () => {
    const { container } = renderPage(<CoveragePage />, "/backup/coverage");
    expect(await screen.findByRole("heading", { name: "Cobertura" })).toBeInTheDocument();
    await screen.findByText("FS-01");
    expect(screen.getByText("Qué dispositivos tienen una copia de seguridad reciente que se puede restaurar, y cuáles no.")).toBeInTheDocument();
    const filter = screen.getByRole("combobox", { name: "Mostrar dispositivos" });
    for (const option of ["Todos los dispositivos", "Protegido", "Desactualizado", "Con fallos", "Sin protección"]) {
      expect(within(filter).getByRole("option", { name: option })).toBeInTheDocument();
    }
    for (const header of ["Dispositivo", "Protección", "Última copia", "Copia externa", "Última restauración de prueba"]) {
      expect(screen.getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    const row = (host: string) => screen.getByText(host).closest("tr") as HTMLElement;
    expect(within(row("FS-01")).getByText("Protegido")).toBeInTheDocument();
    expect(within(row("FS-01")).getByText(/Al día/)).toBeInTheDocument();
    expect(within(row("WEB-02")).getByText("Sin protección")).toBeInTheDocument();
    expect(within(row("WEB-02")).getByText("Sin política de copia de seguridad")).toBeInTheDocument();
    expect(within(row("WEB-02")).getByText(/Desactivada/)).toBeInTheDocument();
    expect(within(row("DB-03")).getByText("Desactualizado")).toBeInTheDocument();
    expect(within(row("DB-03")).getByText(/Atrasada/)).toBeInTheDocument();
    expect(within(row("DB-03")).getByText("No consistente ante fallos: los archivos se leyeron mientras estaban en uso")).toBeInTheDocument();
    expect(within(row("APP-04")).getByText("Con fallos")).toBeInTheDocument();
    expect(within(row("WEB-02")).getAllByText("Nunca").length).toBeGreaterThan(0);
    expectNoEnglish(container, ["FS-01", "WEB-02", "DB-03", "APP-04"]);
  });

  it("says why there are no devices, and that a filter found none", async () => {
    baseApi({ runReport: vi.fn().mockResolvedValue({ meta: {}, totals: {}, rows: [] }) });
    const first = renderPage(<CoveragePage />, "/backup/coverage");
    expect(await screen.findByText("Todavía no hay dispositivos.")).toBeInTheDocument();
    expect(screen.getByText("Ningún dispositivo tiene una copia de seguridad reciente")).toBeInTheDocument();
    expect(screen.getByText("Ningún dispositivo va con retraso")).toBeInTheDocument();
    expect(screen.getByText("Ninguna copia de seguridad está fallando")).toBeInTheDocument();
    expect(screen.getByText("Todos los dispositivos tienen una política de copia de seguridad")).toBeInTheDocument();
    expectNoEnglish(first.container);
    first.unmount();
    const second = renderPage(<CoveragePage />, "/backup/coverage?coverage=stale");
    expect(await screen.findByText("Ningún dispositivo coincide con ese filtro.")).toBeInTheDocument();
    expectNoEnglish(second.container);
  });
});

// -------------------------------------------------------------- repositories

describe("the repositories page, in Spanish", () => {
  it("words the head, the tiles, the filter, the table and the states", async () => {
    baseApi({ listBackupRepositories: vi.fn().mockResolvedValue([REPO, REPO_OFF]) });
    const { container } = renderPage(<RepositoriesPage />, "/backup/repositories");
    expect(await screen.findByRole("heading", { name: "Repositorios" })).toBeInTheDocument();
    await screen.findByText("bóveda-principal");
    expect(screen.getByText("Almacenes de objetos donde llegan las copias de seguridad de este parque.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Nuevo repositorio" })).toBeInTheDocument();
    const filter = screen.getByRole("combobox", { name: "Filtrar los repositorios por estado" });
    for (const option of ["Todos los repositorios", "Activos", "No activos"]) {
      expect(within(filter).getByRole("option", { name: option })).toBeInTheDocument();
    }
    for (const header of ["Nombre", "Estado", "Registrado"]) {
      expect(screen.getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    expect(within(screen.getByText("bóveda-principal").closest("tr") as HTMLElement).getByText("Activo")).toBeInTheDocument();
    expect(within(screen.getByText("bóveda-antigua").closest("tr") as HTMLElement).getByText("Deshabilitado")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("says why there are no repositories", async () => {
    baseApi({ listBackupRepositories: vi.fn().mockResolvedValue([]) });
    const { container } = renderPage(<RepositoriesPage />, "/backup/repositories");
    expect(await screen.findByText("No hay repositorios registrados.")).toBeInTheDocument();
    expect(screen.getByText("No hay ningún repositorio registrado")).toBeInTheDocument();
    expect(screen.getByText("Ningún repositorio puede aceptar datos de copia")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("names the missing name of a new repository, and re-words it on a switch", async () => {
    const user = userEvent.setup();
    renderPage(<RepositoriesPage />, "/backup/repositories");
    await screen.findByText("bóveda-principal");
    await user.click(screen.getByRole("button", { name: "Nuevo repositorio" }));
    const dialog = await screen.findByRole("dialog", { name: "Nuevo repositorio" });
    await user.click(within(dialog).getByRole("button", { name: "Crear" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("El campo Nombre es obligatorio.");
    expectNoEnglish(dialog);
    act(() => setActiveLanguage("en"));
    expect(within(screen.getByRole("dialog", { name: "New repository" })).getByRole("alert")).toHaveTextContent(
      "Name is required",
    );
  });
});

// ------------------------------------------------------------------- off-site

describe("the off-site copy page, in Spanish", () => {
  it("words the empty recovery key panel, the storage table and the repositories", async () => {
    baseApi({ listBackupRepositories: vi.fn().mockResolvedValue([REPO, REPO_OFF]) });
    const { container } = renderPage(<OffsitePage />, "/backup/offsite");
    expect(await screen.findByRole("heading", { name: "Copia externa" })).toBeInTheDocument();
    expect(await screen.findByText("Aún no hay clave de recuperación. No se pueden activar las copias externas hasta que configures una.")).toBeInTheDocument();
    expect(screen.getByText("Te permite leer la copia externa si alguna vez se pierde este servidor. Solo la tienes tú.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Configurar la clave de recuperación" })).toBeInTheDocument();
    expect((await screen.findAllByText("Wasabi UE")).length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "Añadir almacenamiento externo" })).toBeInTheDocument();
    for (const header of ["Nombre", "Estado", "Bloqueado durante", "Última prueba", "Acciones"]) {
      expect(screen.getAllByRole("columnheader", { name: header }).length).toBeGreaterThan(0);
    }
    // The storage's name is also an option of the unavailable-storage select.
    const failing = screen.getAllByText("Wasabi UE").map((el) => el.closest("tr")).find((tr) => tr?.textContent?.includes("acme-externo")) as HTMLElement;
    expect(within(failing).getByText("No utilizable")).toBeInTheDocument();
    expect(within(failing).getByText("acme-externo en https://s3.eu-central-1.wasabisys.com")).toBeInTheDocument();
    expect(within(failing).getByText("30 días")).toBeInTheDocument();
    expect(within(failing).getByText("✓ Puede conectarse")).toBeInTheDocument();
    expect(within(failing).getByText("✗ Object Lock está activado")).toBeInTheDocument();
    expect(within(failing).getByRole("button", { name: "Probar" })).toBeInTheDocument();
    expect(within(failing).getByRole("button", { name: "Editar" })).toBeInTheDocument();
    const ok = screen.getAllByText("NAS de Oporto").map((el) => el.closest("tr")).find((tr) => tr?.textContent?.includes("Listo")) as HTMLElement;
    expect(within(ok).getByText("Listo")).toBeInTheDocument();
    expect(within(ok).getByText("1 día")).toBeInTheDocument();
    expect(within(ok).getByText("Nunca")).toBeInTheDocument();
    // The repositories panel.
    expect(screen.getByText("Copia externa de estos repositorios")).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Copia externa de bóveda-principal" })).toBeInTheDocument();
    expect(within(screen.getByRole("combobox", { name: "Copia externa de bóveda-principal" })).getByRole("option", { name: "Desactivada" })).toBeInTheDocument();
    expect(within(screen.getByRole("combobox", { name: "Copia externa de bóveda-antigua" })).getByRole("option", { name: "NAS de Oporto" })).toBeInTheDocument();
    expectNoEnglish(container, [...SERVER_TEXT, "Object Lock", "Wasabi UE", "NAS de Oporto"]);
  });

  it("words a confirmed and an unconfirmed recovery key", async () => {
    const user = userEvent.setup();
    baseApi({ listRecoveryKeys: vi.fn().mockResolvedValue([KEY]) });
    const first = renderPage(<OffsitePage />, "/backup/offsite");
    expect(await screen.findByText("Aún sin confirmar")).toBeInTheDocument();
    expect(screen.getByText("Huella 0123456789abcdef0123456789abcdef")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Confirmar la clave" }));
    const dialog = await screen.findByRole("dialog", { name: "Confirmar la clave de recuperación" });
    expect(dialog).toHaveTextContent("Esta clave aún no se ha confirmado. Pide una comprobación nueva para demostrar que sigues teniendo el archivo de la clave.");
    expect(within(dialog).getByRole("button", { name: "Pedir una comprobación nueva" })).toBeInTheDocument();
    expectNoEnglish(dialog);
    await user.click(within(dialog).getByRole("button", { name: "Cancelar" }));
    first.unmount();
    baseApi({ listRecoveryKeys: vi.fn().mockResolvedValue([{ ...KEY, proven_at: "2026-09-23T12:05:00Z" }]) });
    const second = renderPage(<OffsitePage />, "/backup/offsite");
    expect(await screen.findByText(/Confirmada el/)).toBeInTheDocument();
    expect(screen.getByText("Confirmada")).toBeInTheDocument();
    expectNoEnglish(second.container, [...SERVER_TEXT, "Wasabi UE", "NAS de Oporto"]);
  });

  it("re-words the page when the language is switched", async () => {
    baseApi({ listRecoveryKeys: vi.fn().mockResolvedValue([KEY]) });
    renderPage(<OffsitePage />, "/backup/offsite");
    await screen.findByText("Wasabi UE");
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("heading", { name: "Off-site copy" })).toBeInTheDocument();
    expect(screen.getByText("Recovery key")).toBeInTheDocument();
    expect(screen.getByText("Not confirmed yet")).toBeInTheDocument();
    const row = screen.getByText("Wasabi UE").closest("tr") as HTMLElement;
    expect(within(row).getByText("Not usable")).toBeInTheDocument();
    expect(within(row).getByText("✗ Object Lock is on")).toBeInTheDocument();
    expect(within(row).getByText("30 days")).toBeInTheDocument();
  });

  it("starts a test of the storage", async () => {
    const user = userEvent.setup();
    const api = baseApi();
    renderPage(<OffsitePage />, "/backup/offsite");
    await screen.findByText("Wasabi UE");
    await user.click(within(screen.getByText("Wasabi UE").closest("tr") as HTMLElement).getByRole("button", { name: "Probar" }));
    await waitFor(() => expect(api.testOffsiteTarget).toHaveBeenCalledWith("t1"));
  });
});

describe("the recovery key wizard, in Spanish", () => {
  it("shows the key-making instructions, with the command left as written", async () => {
    const { container } = renderPage(<RecoveryKeyWizard open onClose={() => {}} />, "/backup/offsite");
    const dialog = await screen.findByRole("dialog", { name: "Configurar la clave de recuperación" });
    expect(dialog).toHaveTextContent(
      "Genera la clave en un equipo que no sea este servidor, preferiblemente uno sin conexión. Ejecuta:",
    );
    expect(within(dialog).getByText("python3 itms_recover.py keygen --out recovery.key")).toBeInTheDocument();
    expect(dialog).toHaveTextContent("Escribe la clave privada en recovery.key e imprime la clave pública.");
    expect(dialog).toHaveTextContent("ese archivo es la única forma de leer la copia externa, y nadie puede volver a crearlo.");
    expect(within(dialog).getByText("recovery.key").className).toContain("mono");
    expect(within(dialog).getByText("La línea que empieza por itms-recovery-public:")).toBeInTheDocument();
    expect(within(dialog).getByText("He guardado el archivo de la clave privada en dos lugares seguros.")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Siguiente" })).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("refuses an unrelated line and an unticked box, and re-words both on a switch", async () => {
    const user = userEvent.setup();
    const api = baseApi();
    renderPage(<RecoveryKeyWizard open onClose={() => {}} />, "/backup/offsite");
    const dialog = await screen.findByRole("dialog", { name: "Configurar la clave de recuperación" });
    await user.type(within(dialog).getByRole("textbox"), "no es una clave");
    await user.click(within(dialog).getByRole("button", { name: "Siguiente" }));
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("Pega la línea que empieza por itms-recovery-public:");
    expect(alert).toHaveTextContent("Confirma primero que el archivo de la clave privada está guardado en dos lugares seguros.");
    expect(api.registerRecoveryKey).not.toHaveBeenCalled();
    expectNoEnglish(dialog);
    act(() => setActiveLanguage("en"));
    const again = within(screen.getByRole("dialog", { name: "Set up the recovery key" })).getByRole("alert");
    expect(again).toHaveTextContent("Paste the line that starts with itms-recovery-public:");
    expect(again).toHaveTextContent("Confirm the private key file is stored in two safe places first.");
  });

  it("walks the key through its check and confirmation", async () => {
    const user = userEvent.setup();
    const api = baseApi();
    renderPage(<RecoveryKeyWizard open onClose={() => {}} />, "/backup/offsite");
    const dialog = await screen.findByRole("dialog", { name: "Configurar la clave de recuperación" });
    await user.type(within(dialog).getByRole("textbox"), "itms-recovery-public:QUJD");
    await user.click(within(dialog).getByRole("checkbox"));
    await user.click(within(dialog).getByRole("button", { name: "Siguiente" }));
    expect(api.registerRecoveryKey).toHaveBeenCalledWith("itms-recovery-public:QUJD");
    const prove = await screen.findByRole("dialog", { name: "Demuestra que tienes la clave" });
    expect(prove).toHaveTextContent("En el equipo que guarda la clave, ejecuta esto y pega la respuesta que imprime.");
    expect(within(prove).getByText(/prove --key recovery.key --challenge 'Q0hBTExFTkdF'/)).toBeInTheDocument();
    await user.click(within(prove).getByRole("button", { name: "Confirmar" }));
    expect(await within(prove).findByRole("alert")).toHaveTextContent("El campo Respuesta es obligatorio.");
    expectNoEnglish(prove);
    act(() => setActiveLanguage("en"));
    expect(within(screen.getByRole("dialog", { name: "Prove you have the key" })).getByRole("alert")).toHaveTextContent(
      "Answer is required",
    );
    act(() => setActiveLanguage("es"));
    await user.type(within(screen.getByRole("dialog")).getByLabelText("Respuesta"), "YW5zd2Vy");
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Confirmar" }));
    expect(api.proveRecoveryKey).toHaveBeenCalledWith("k1", "YW5zd2Vy");
    const done = await screen.findByRole("dialog", { name: "Clave de recuperación confirmada" });
    expect(done).toHaveTextContent(
      "La clave de recuperación está configurada. Ya puedes activar las copias externas. Mantén el archivo de la clave privada fuera de línea: este servidor nunca lo tuvo y no puede devolvértelo.",
    );
    expect(within(done).getByRole("button", { name: "Listo" })).toBeInTheDocument();
    expectNoEnglish(done);
  });

  it("shows the server's reason, as recorded, when the answer does not match", async () => {
    const user = userEvent.setup();
    baseApi({ proveRecoveryKey: vi.fn().mockRejectedValue(new ApiError(422, "La respuesta no coincide.")) });
    renderPage(<RecoveryKeyWizard open onClose={() => {}} pending={KEY} />, "/backup/offsite");
    const confirm = await screen.findByRole("dialog", { name: "Confirmar la clave de recuperación" });
    await user.click(within(confirm).getByRole("button", { name: "Pedir una comprobación nueva" }));
    const prove = await screen.findByRole("dialog", { name: "Demuestra que tienes la clave" });
    await user.type(within(prove).getByLabelText("Respuesta"), "x");
    await user.click(within(prove).getByRole("button", { name: "Confirmar" }));
    expect(await within(prove).findByRole("alert")).toHaveTextContent("La respuesta no coincide.");
  });
});

describe("the off-site storage dialog, in Spanish", () => {
  it("words a new storage, and names every missing field", async () => {
    const user = userEvent.setup();
    const api = baseApi();
    renderPage(<TargetDialog open onClose={() => {}} />, "/backup/offsite");
    const dialog = await screen.findByRole("dialog", { name: "Añadir almacenamiento externo" });
    expect(dialog).toHaveTextContent("Crea primero el bucket con Object Lock activado: no se puede añadir después.");
    for (const label of ["Nombre", "Dirección del almacenamiento", "Región", "Bucket", "Carpeta del bucket", "Clave de acceso", "Clave secreta", "Bloquear cada copia durante (días)", "Copias diarias que conservar", "Copias semanales que conservar", "Copias mensuales que conservar", "Copias anuales que conservar", "Límite de velocidad de subida (Mbit/s)"]) {
      expect(within(dialog).getByText(label)).toBeInTheDocument();
    }
    expect(within(dialog).getByText("Déjala en blanco para Amazon S3.")).toBeInTheDocument();
    expect(within(dialog).getByText("Déjalo en blanco para no poner límite.")).toBeInTheDocument();
    expect(within(dialog).getByText("Nadie puede borrar ni modificar una copia bloqueada, ni siquiera quien tome el control de este servidor.")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Añadir" }));
    const alert = await within(dialog).findByRole("alert");
    for (const field of ["Nombre", "Bucket", "Clave de acceso", "Clave secreta"]) {
      expect(alert).toHaveTextContent(`El campo ${field} es obligatorio`);
    }
    expect(api.createOffsiteTarget).not.toHaveBeenCalled();
    expectNoEnglish(dialog);
    act(() => setActiveLanguage("en"));
    expect(within(screen.getByRole("dialog", { name: "Add off-site storage" })).getByRole("alert")).toHaveTextContent("Secret key is required");
  });

  it("words the lock range and the both-or-neither rule for an edited storage", async () => {
    const user = userEvent.setup();
    renderPage(<TargetDialog open target={TARGET} onClose={() => {}} />, "/backup/offsite");
    const dialog = await screen.findByRole("dialog", { name: "Editar almacenamiento externo" });
    expect(within(dialog).getAllByText("Déjala en blanco para conservar la actual.")).toHaveLength(2);
    const lock = within(dialog).getByLabelText("Bloquear cada copia durante (días)");
    await user.clear(lock);
    await user.type(lock, "120");
    await user.type(within(dialog).getByLabelText("Clave de acceso"), "AKIA-FIXTURE");
    await user.click(within(dialog).getByRole("button", { name: "Guardar" }));
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("Entre 1 y 90 días.");
    expect(alert).toHaveTextContent("Indica tanto la clave de acceso como la clave secreta, o deja ambas en blanco.");
    expectNoEnglish(dialog, ["acme-externo", "https://s3.eu-central-1.wasabisys.com", "eu-central-1"]);
    act(() => setActiveLanguage("en"));
    expect(within(screen.getByRole("dialog", { name: "Edit off-site storage" })).getByRole("alert")).toHaveTextContent("Between 1 and 90 days.");
  });
});

describe("the off-site storage dialog keeps the secret out of its refusal snapshot", () => {
  it("still judges a secret given without its access key, and a complete new storage", async () => {
    const user = userEvent.setup();
    const api = baseApi();
    renderPage(<TargetDialog open target={TARGET} onClose={() => {}} />, "/backup/offsite");
    const dialog = await screen.findByRole("dialog", { name: "Editar almacenamiento externo" });
    await user.type(within(dialog).getByLabelText("Clave secreta"), "fixture-secret-not-real");
    await user.click(within(dialog).getByRole("button", { name: "Guardar" }));
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("Indica tanto la clave de acceso como la clave secreta, o deja ambas en blanco.");
    expect(alert).not.toHaveTextContent("fixture-secret-not-real");
    expect(api.updateOffsiteTarget).not.toHaveBeenCalled();
  });

  it("refuses a blank secret on a new storage, as it always did", async () => {
    const user = userEvent.setup();
    const api = baseApi();
    renderPage(<TargetDialog open onClose={() => {}} />, "/backup/offsite");
    const dialog = await screen.findByRole("dialog", { name: "Añadir almacenamiento externo" });
    await user.type(within(dialog).getByLabelText("Nombre"), "NAS");
    await user.type(within(dialog).getByLabelText("Bucket"), "acme");
    await user.type(within(dialog).getByLabelText("Clave de acceso"), "AKIA-FIXTURE");
    await user.type(within(dialog).getByLabelText("Clave secreta"), "   ");
    await user.click(within(dialog).getByRole("button", { name: "Añadir" }));
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("El campo Clave secreta es obligatorio");
    expect(api.createOffsiteTarget).not.toHaveBeenCalled();
  });

  it("sends a complete new storage with both keys", async () => {
    const user = userEvent.setup();
    const api = baseApi();
    renderPage(<TargetDialog open onClose={() => {}} />, "/backup/offsite");
    const dialog = await screen.findByRole("dialog", { name: "Añadir almacenamiento externo" });
    await user.type(within(dialog).getByLabelText("Nombre"), "NAS");
    await user.type(within(dialog).getByLabelText("Bucket"), "acme");
    await user.type(within(dialog).getByLabelText("Clave de acceso"), "AKIA-FIXTURE");
    await user.type(within(dialog).getByLabelText("Clave secreta"), "fixture-secret-not-real");
    await user.click(within(dialog).getByRole("button", { name: "Añadir" }));
    await waitFor(() => expect(api.createOffsiteTarget).toHaveBeenCalled());
    expect(api.createOffsiteTarget.mock.calls[0]?.[0]).toMatchObject({ secret_access_key: "fixture-secret-not-real" });
  });
});

// ------------------------------------------------------------------ device tab

describe("a device's Backup tab, in Spanish", () => {
  it("words the facts and the Run now control", async () => {
    const { container } = renderPage(<DeviceBackupTab deviceId="dev1" />);
    expect(await screen.findByText("Estado de la copia de seguridad")).toBeInTheDocument();
    expect(await screen.findByText("bóveda-principal")).toBeInTheDocument();
    for (const label of ["Estado de la última ejecución", "Última ejecución finalizada", "Repositorio", "Último punto correcto", "Verificado", "Restauración probada"]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
    expect(screen.getByText("Omitida")).toBeInTheDocument();
    expect(screen.getByText("no verificado")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Ejecutar ahora" })).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("says what was queued, and re-words it on a switch", async () => {
    const user = userEvent.setup();
    baseApi({ runBackup: vi.fn().mockResolvedValue({ job_ids: ["a", "b"] }) });
    renderPage(<DeviceBackupTab deviceId="dev1" />);
    await screen.findByText("bóveda-principal");
    await user.click(screen.getByRole("button", { name: "Ejecutar ahora" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Copia de seguridad en cola: 2 tareas enviadas.");
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("status")).toHaveTextContent("Backup queued — 2 jobs dispatched.");
  });

  it("says no run, no repository and nothing tested", async () => {
    baseApi({
      getDeviceBackup: vi.fn().mockResolvedValue({
        device_id: "dev1",
        last_good_point: null,
        last_run_state: null,
        last_run_finished_at: null,
        verified_at: null,
        restore_tested_at: null,
        repository: null,
      }),
    });
    const { container } = renderPage(<DeviceBackupTab deviceId="dev1" />);
    expect(await screen.findByText("No hay ninguna ejecución de copia registrada")).toBeInTheDocument();
    expect(screen.getByText("Ningún repositorio asignado")).toBeInTheDocument();
    expect(screen.getByText("no probada")).toBeInTheDocument();
    expectNoEnglish(container);
  });
});

// ------------------------------------------------------------------ readiness

describe("the readiness panels, in Spanish", () => {
  const fleet = (managed: number) => ({
    items: [],
    total: managed,
    state_counts: { all: 0, online: 0, offline: 0, assets: 0, certs: 0 },
    fleet: { total: managed, managed, online: managed, cert_attention: 0 },
  });

  it("sends an org with no repository to create one, and holds the policy step", async () => {
    baseApi({ listBackupRepositories: vi.fn().mockResolvedValue([]), listBackupPolicies: vi.fn().mockResolvedValue([]), listDevices: vi.fn().mockResolvedValue(fleet(0)) });
    const { container } = renderPage(<BackupReadiness />);
    expect(await screen.findByText("Antes de que puedan ejecutarse copias de seguridad")).toBeInTheDocument();
    expect(screen.getByText("Lo que tiene que estar configurado en otras pantallas antes de que esta página copie algo.")).toBeInTheDocument();
    expect(await screen.findByText("Existe un repositorio de copia de seguridad")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Crear un repositorio" })).toHaveAttribute("href", "/backup/repositories");
    expect(screen.getByText("Crea primero un repositorio.")).toBeInTheDocument();
    expect(screen.getByText("Una copia de seguridad se ejecuta en el propio equipo, a través del agente, y todavía ningún equipo lo ejecuta.")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("words a policy gap in the singular and the plural", async () => {
    baseApi({ listBackupPolicies: vi.fn().mockResolvedValue([policy({ enabled: false })]), listDevices: vi.fn().mockResolvedValue(fleet(3)) });
    const first = renderPage(<BackupReadiness />);
    expect(await screen.findByText(/Existe 1 política pero ninguna está habilitada/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Habilitar una política" })).toBeInTheDocument();
    expect(screen.getByText("Hay 1 repositorio configurado.")).toBeInTheDocument();
    first.unmount();
    baseApi({ listBackupPolicies: vi.fn().mockResolvedValue([policy({ enabled: false }), policy({ id: "p9", enabled: false })]), listBackupRepositories: vi.fn().mockResolvedValue([REPO, REPO_OFF]), listDevices: vi.fn().mockResolvedValue(fleet(3)) });
    const second = renderPage(<BackupReadiness />);
    expect(await screen.findByText(/Existen 2 políticas pero ninguna está habilitada/)).toBeInTheDocument();
    expect(screen.getByText("Hay 2 repositorios configurados.")).toBeInTheDocument();
    expectNoEnglish(second.container);
  });

  it("says no policy exists, and counts enabled policies when there are some", async () => {
    baseApi({ listBackupPolicies: vi.fn().mockResolvedValue([]), listDevices: vi.fn().mockResolvedValue(fleet(3)) });
    const first = renderPage(<BackupReadiness />);
    expect(await screen.findByText(/No existe ninguna política de copia de seguridad/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Crear una política de copia de seguridad" })).toBeInTheDocument();
    first.unmount();
    baseApi({ listDevices: vi.fn().mockResolvedValue(fleet(0)) });
    const second = renderPage(<BackupReadiness />);
    expect(await screen.findByText("Hay 2 políticas habilitadas.")).toBeInTheDocument();
    expectNoEnglish(second.container);
  });

  it("words the policy panel", async () => {
    baseApi({ listDeviceGroups: vi.fn().mockResolvedValue({ items: [], total: 0, ungrouped_device_count: 0, last_evaluated_at: null }) });
    const { container } = renderPage(<PolicyReadiness />);
    expect(await screen.findByText("Antes de que una política pueda copiar algo")).toBeInTheDocument();
    expect(screen.getByText("Lo que una política necesita de otras pantallas.")).toBeInTheDocument();
    expect(await screen.findByText("Existe un grupo de dispositivos para una política más específica")).toBeInTheDocument();
    expect(screen.getByText("Sin un grupo de dispositivos, una política solo puede cubrir todos los dispositivos.")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("words the off-site panel for each gap", async () => {
    baseApi({ listOffsiteTargets: vi.fn().mockResolvedValue([]) });
    const first = renderPage(<OffsiteReadiness />);
    expect(await screen.findByText("Antes de hacer copia externa de los repositorios")).toBeInTheDocument();
    expect(await screen.findByText("No hay ninguna clave de recuperación configurada, así que no se puede hacer copia externa de ningún repositorio.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Configurar una clave de recuperación" })).toBeInTheDocument();
    expect(screen.getByText("No se ha añadido ningún almacenamiento externo, así que las copias de seguridad solo existen en este servidor.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Probar el almacenamiento externo" })).toBeInTheDocument();
    expectNoEnglish(first.container);
    first.unmount();
    baseApi({ listRecoveryKeys: vi.fn().mockResolvedValue([KEY]), listOffsiteTargets: vi.fn().mockResolvedValue([TARGET]) });
    const second = renderPage(<OffsiteReadiness />);
    expect(await screen.findByText("Hay una clave de recuperación registrada pero aún sin confirmar, así que no se puede hacer copia externa de ningún repositorio.")).toBeInTheDocument();
    expect(screen.getByText("Se ha añadido almacenamiento externo pero no ha superado su prueba, así que no se copia nada en él.")).toBeInTheDocument();
    expectNoEnglish(second.container);
    first.unmount();
  });

  it("re-words a mounted panel when the language is switched", async () => {
    baseApi({ listBackupPolicies: vi.fn().mockResolvedValue([]), listDevices: vi.fn().mockResolvedValue(fleet(3)) });
    renderPage(<BackupReadiness />);
    await screen.findByText("Existe un repositorio de copia de seguridad");
    act(() => setActiveLanguage("en"));
    expect(screen.getByText("Before backups can run")).toBeInTheDocument();
    expect(screen.getByText("A backup repository exists")).toBeInTheDocument();
  });
});

// -------------------------------------------------- built outside React

describe("the sentences the module builds outside React, in Spanish", () => {
  it("words the label tables and follows a switch", () => {
    setActiveLanguage("es");
    expect(COVERAGE_LABEL.stale).toBe("Desactualizado");
    expect(OFFSITE_LABEL.behind).toBe("Atrasada");
    expect(OFFSITE_TARGET_STATE_LABEL.untested).toBe("Aún sin probar");
    expect(OFFSITE_TARGET_STATE_LABEL.testing).toBe("Probando…");
    expect(TEST_STEP_LABEL.delete_refused).toBe("Las copias bloqueadas no se pueden borrar");
    expect(Object.keys(COVERAGE_LABEL)).toEqual(["covered", "stale", "failing", "unprotected"]);
    setActiveLanguage("en");
    expect(COVERAGE_LABEL.stale).toBe("Out of date");
    expect(OFFSITE_LABEL.behind).toBe("Behind");
    expect(OFFSITE_TARGET_STATE_LABEL.ok).toBe("Ready");
    expect(TEST_STEP_LABEL.connect).toBe("Can connect");
  });

  it("words the nav strip, the warnings and the restore-test labels", () => {
    setActiveLanguage("es");
    expect(BACKUP_NAV_ITEMS.map((item) => item.label)).toEqual(["Puntos de copia", "Cobertura", "Repositorios", "Políticas", "Copia externa"]);
    expect(warningText("sacl_not_captured")).toBe("No se copió la configuración de auditoría de los archivos.");
    expect(warningText("algo_desconocido")).toBe("algo_desconocido");
    expect(restoreTestLabel(null)).toBe("No probado");
    expect(restoreTestLabel("2026-01-01", "sampled")).toBe("Probado (muestra)");
    expect(restoreTestLabel("2026-01-01", "full")).toBe("Probado");
    expect(notCrashConsistent()).toBe("No consistente ante fallos: los archivos se leyeron mientras estaban en uso");
    setActiveLanguage("en");
    expect(BACKUP_NAV_ITEMS[0]?.label).toBe("Backup points");
    expect(warningText("sacl_not_captured")).toBe("Audit settings on files were not copied.");
    expect(notCrashConsistent()).toBe(NOT_CRASH_CONSISTENT);
  });

  it("words the state pills, and a state nobody catalogued reads through the shared words", () => {
    setActiveLanguage("es");
    expect(backupPointStateLabel("pruned")).toBe("Eliminado por retención");
    expect(backupRunStateLabel("missed")).toBe("Omitida");
    expect(backupRunStateLabel("success")).toBe("Correcta");
    expect(repositoryStateLabel("disabled")).toBe("Deshabilitado");
    expect(backupRunStateLabel("succeeded")).toBe("Correcto");
    setActiveLanguage("en");
    expect(backupPointStateLabel("pruned")).toBe("Pruned");
    expect(backupRunStateLabel("missed")).toBe("Missed");
    expect(backupRunStateLabel("succeeded")).toBe("Succeeded");
    expect(repositoryStateLabel("active")).toBe("Active");
  });

  it("puts a no-break space before the percent sign", () => {
    setActiveLanguage("es");
    expect(translate("backup.points.percent", { percent: 75 })).toBe("75\u00a0%");
    setActiveLanguage("en");
    expect(translate("backup.points.percent", { percent: 75 })).toBe("75%");
  });

  it("words the queued sentence with the count agreed", () => {
    setActiveLanguage("es");
    expect(runQueuedMessage({ job_ids: ["a"] })).toBe("Copia de seguridad en cola: 1 tarea enviada.");
    expect(runQueuedMessage({ job_ids: ["a", "b", "c"] })).toBe("Copia de seguridad en cola: 3 tareas enviadas.");
    expect(runQueuedMessage(undefined)).toBe(
      "No se envió nada: no se creó ninguna tarea de copia de seguridad para ese dispositivo.",
    );
    setActiveLanguage("en");
    expect(runQueuedMessage({ job_ids: ["a"] })).toBe("Backup queued — 1 job dispatched.");
    expect(runQueuedMessage({ job_ids: ["a", "b"] })).toBe("Backup queued — 2 jobs dispatched.");
  });

  it("words a window in words, a day range, a list of days and several spans", () => {
    setActiveLanguage("es");
    expect(windowLabel("0 2-3 * * 1-5")).toBe("Entre semana, de 02:00 a 04:00");
    expect(windowLabel("0 0-23 * * 6,0")).toBe("Fines de semana, todo el día");
    expect(windowLabel("0 1,2,22 * * *")).toBe("Todos los días, de 01:00 a 03:00 y de 22:00 a 23:00");
    expect(windowLabel("0 3 * * 1-4")).toBe("lun a jue, de 03:00 a 04:00");
    expect(windowLabel("0 3 * * 1,3")).toBe("lun, mié, de 03:00 a 04:00");
    setActiveLanguage("en");
    expect(windowLabel("0 2-3 * * 1-5")).toBe("Weekdays, 02:00 to 04:00");
    expect(windowLabel("0 3 * * 1-4")).toBe("Mon to Thu, 03:00 to 04:00");
  });

  it("words the picker options, the empty hints and follows a switch", () => {
    setActiveLanguage("es");
    expect(devicePickerOptions(DEVICES.items).map((o) => o.sublabel)).toEqual([
      "windows · en línea",
      "linux · sin conexión",
      "sin agente",
    ]);
    expect(groupPickerOptions([GROUP, { ...GROUP, id: "g2", device_count: 1 }]).map((o) => o.sublabel)).toEqual([
      "3 dispositivos",
      "1 dispositivo",
    ]);
    expect(repositoryPickerOptions([REPO, REPO_OFF]).map((o) => o.sublabel)).toEqual([
      "s3://copias/principal · activo",
      "s3://copias/principal · deshabilitado",
    ]);
    expect(devicePickerEmptyHint()).toMatch(/Dispositivos → Añadir un equipo/);
    expect(repositoryPickerEmptyHint()).toMatch(/pestaña Repositorios/);
    setActiveLanguage("en");
    expect(devicePickerOptions(DEVICES.items).map((o) => o.sublabel)).toEqual(["windows · online", "linux · offline", "no agent"]);
    expect(repositoryPickerOptions([REPO_OFF])[0]?.sublabel).toBe("s3://copias/principal · disabled");
    expect(devicePickerEmptyHint()).toBe(DEVICE_PICKER_EMPTY_HINT);
  });
});
