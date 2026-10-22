/**
 * Starting a remote session: the click that used to do nothing, the device
 * that could not answer, and the consent escalation that was one dropdown
 * value away.
 *
 * CYCLE 3/4 (already landed, kept here): the audit filed [MEDIUM] "Start
 * session with no device selected is a silent no-op" -- `disabled={...
 * !deviceId || !reason}` on a panel that was not a `<form>`, so pressing Start
 * moved nothing, named neither field, and Enter did nothing either.
 *
 * CYCLES 6/8 (this pass): [HIGH] "A remote session can be started against a
 * device that is offline, and 'Unattended' mode is one dropdown click away
 * with no additional confirmation".
 *
 * SCREEN SHARE (spec-screen-share.md, 2026-09-23): the only session type, so
 * the protocol select and the credential picker are gone -- a screen session
 * runs in the user's own console and the server refuses a credential on it.
 *
 * EVERY TEST BELOW WAS FALSIFIED -- see the note at the foot of the file.
 *
 * Harness mirrors `SessionsPage.test.tsx`: the auth-backed API client is
 * mocked (no real fetch, no real WebSocket) and the mutation runs for real.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, type ApiClient, type Device, type Page } from "../../lib/api";
import { StartSessionDialog } from "./StartSessionDialog";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

function device(overrides: Partial<Device> = {}): Device {
  return {
    id: "d1",
    hostname: "workstation-1",
    fqdn: null,
    os_family: "windows",
    os_version: "11",
    architecture: "x64",
    device_group_id: null,
    status: "active",
    agent_status: "online",
    last_seen_at: null,
    cert_not_after: null,
    cert_state: null,
    cert_revoked: false,
    lifecycle: "in_service" as const,
    asset_type: "computer" as const,
    source: "agent" as const,
    // An enrolled machine proved its own identity at enrolment; nobody
    // had to vouch for the row (asset-inventory spec §3).
    provenance: "agent" as const,
    description: null,
    primary_ip: null,
    owner: null,
    location: null,
    ...overrides,
  };
}

/** The fleet the audit actually walked: one device, and it was offline. */
const OFFLINE = device({ id: "d2", hostname: "Toeshee-sentinel", os_family: "linux", agent_status: "offline" });

const DEVICES: Device[] = [device(), OFFLINE];

let api: Partial<ApiClient>;

function renderDialog(onClose = () => {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <StartSessionDialog open onClose={onClose} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function dialog(): HTMLElement {
  return screen.getByRole("dialog", { name: "Start remote session" });
}

/**
 * Open the picker and click a row.
 *
 * `getByRole("combobox", ...)`, not `getByLabelText`: the picker's own listbox
 * carries `aria-label={label}` too, so the plain label query is ambiguous
 * whenever the list happens to be open. Deliberately NOT `user.type(...)` --
 * the box filters, it does not accept, and a test that typed would assert a
 * behaviour the picker does not have.
 */
async function pick(
  user: ReturnType<typeof userEvent.setup>,
  pickerLabel: string,
  optionName: RegExp,
) {
  await user.click(screen.getByRole("combobox", { name: pickerLabel }));
  await user.click(await screen.findByRole("option", { name: optionName }));
}

/** The device list is fetched, so nothing is selectable on first paint. */
async function loadedDevices(): Promise<void> {
  await screen.findByRole("combobox", { name: "Device" });
}

beforeEach(() => {
  api = {
    listDevices: vi.fn().mockResolvedValue({ items: DEVICES, total: 2 } satisfies Page<Device>),
    listCredentials: vi.fn().mockResolvedValue([]),
    startRemoteSession: vi.fn().mockResolvedValue({
      session_id: "s2",
      state: "awaiting_consent",
      consent_mode: "attended",
    }),
  };
  mockUseApi.mockReset().mockReturnValue(api as ApiClient);
});

describe("StartSessionDialog", () => {
  it("leaves Start session enabled with no device selected", async () => {
    // THE RULE: a disabled button that never says why IS the silent no-op.
    // Disabled is reserved for a request in flight, not for input the
    // technician has not supplied yet.
    renderDialog();
    await loadedDevices();

    expect(screen.getByRole("button", { name: "Start session" })).toBeEnabled();
  });

  it("refuses with no device selected and says which fields are at fault", async () => {
    // The finding itself. The request must not leave, and the refusal must
    // name the fields as they are labelled on screen — "Device", not
    // `device_id`.
    const user = userEvent.setup();
    renderDialog();
    await loadedDevices();

    await user.click(screen.getByRole("button", { name: "Start session" }));

    expect(api.startRemoteSession).not.toHaveBeenCalled();
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Device is required");
    expect(alert).toHaveTextContent("Reason is required");
  });

  it("names only Reason once a device is chosen", async () => {
    const user = userEvent.setup();
    renderDialog();
    await loadedDevices();

    await pick(user, "Device", /workstation-1/);
    await user.click(screen.getByRole("button", { name: "Start session" }));

    expect(api.startRemoteSession).not.toHaveBeenCalled();
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Reason is required");
    expect(alert).not.toHaveTextContent("Device is required");
  });

  it("treats a whitespace-only Reason as empty", async () => {
    // `" "` is truthy, which is how an unexplained session — one whose audit
    // entry records nothing a reviewer can read — got past `!reason`.
    const user = userEvent.setup();
    renderDialog();
    await loadedDevices();

    await pick(user, "Device", /workstation-1/);
    await user.type(screen.getByLabelText("Reason"), "   ");
    await user.click(screen.getByRole("button", { name: "Start session" }));

    expect(api.startRemoteSession).not.toHaveBeenCalled();
    expect(await screen.findByRole("alert")).toHaveTextContent("Reason is required");
  });

  it("refuses a one-character Reason, because that is not an audit record", async () => {
    // AUDIT [HIGH]: "Making it optional guarantees a proportion of sessions
    // will have no recorded reason". Required alone buys "x"; the rule has to
    // reject the character typed to get past the rule.
    const user = userEvent.setup();
    renderDialog();
    await loadedDevices();

    await pick(user, "Device", /workstation-1/);
    await user.type(screen.getByLabelText("Reason"), "x");
    await user.click(screen.getByRole("button", { name: "Start session" }));

    expect(api.startRemoteSession).not.toHaveBeenCalled();
    expect(await screen.findByRole("alert")).toHaveTextContent(/audit log/i);
  });

  it("submits on Enter from the Reason field", async () => {
    // The panel was not a `<form>`, so the submit control was an orphan and
    // Enter did nothing at all on this route.
    const user = userEvent.setup();
    renderDialog();
    await loadedDevices();

    await pick(user, "Device", /workstation-1/);
    await user.type(screen.getByLabelText("Reason"), " helpdesk ticket 4821{Enter}");

    expect(api.startRemoteSession).toHaveBeenCalledWith({
      device_id: "d1",
      protocol: "screen",
      mode: "attended",
      // Trimmed on the way out, so what the audit log records is what the
      // required-field check passed.
      reason: "helpdesk ticket 4821",
    });
  });

  it("tells the technician when the server refuses, instead of showing nothing", async () => {
    // The bodyless 503 the form kernel exists for: `String(start.error)` on an
    // ApiError with an empty body rendered nothing, so a session that failed
    // to open looked exactly like one that did.
    api.startRemoteSession = vi.fn().mockRejectedValue(new ApiError(503, ""));
    const user = userEvent.setup();
    renderDialog();
    await loadedDevices();

    await pick(user, "Device", /workstation-1/);
    await user.type(screen.getByLabelText("Reason"), "helpdesk ticket 4821");
    await user.click(screen.getByRole("button", { name: "Start session" }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent?.trim()).not.toBe("");
    expect(alert).toHaveTextContent(/unavailable/i);
  });

  it("does not start a session when Cancel is pressed", async () => {
    // `ui.tsx`'s Button sets no `type`, and an untyped button inside a form
    // submits it — wrapping the panel in a `<form>` would have turned Cancel
    // into a second Start session.
    const onClose = vi.fn();
    const user = userEvent.setup();
    renderDialog(onClose);
    await loadedDevices();

    await pick(user, "Device", /workstation-1/);
    await user.type(screen.getByLabelText("Reason"), "helpdesk ticket 4821");
    await user.click(within(dialog()).getByRole("button", { name: "Cancel" }));

    expect(api.startRemoteSession).not.toHaveBeenCalled();
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

// ---------------------------------------------------------------------------
// AUDIT [HIGH], /remote → Start remote session → "Device" and "Mode":
//
//   "The stat card reads 'Online devices 0', yet the Device dropdown offers
//   `Toeshee-sentinel` ... Nothing marks it as offline in the dropdown, which
//   lists it as a plain selectable option ... Offering an offline device for a
//   *live interactive* session guarantees a failed or indefinitely hanging
//   connection with no explanation."
//
//   "'Unattended' is a peer option selected the same way, with no warning, no
//   confirmation step and no additional authorisation ... That is a
//   privacy-significant escalation and it is presented as an ordinary dropdown
//   value with equal weight to the safe default."
// ---------------------------------------------------------------------------

describe("the device a session cannot reach", () => {
  it("shows agent state on every option, so an unreachable device is visible before it is chosen", async () => {
    // The picker header's own rule: the sublabel "is also what stops somebody
    // dispatching to a machine that cannot answer". Not "offline" alone —
    // what offline MEANS for a live interactive session.
    const user = userEvent.setup();
    renderDialog();
    await loadedDevices();

    await user.click(screen.getByRole("combobox", { name: "Device" }));

    expect(
      await screen.findByRole("option", { name: /workstation-1\s*windows · online/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("option", { name: /Toeshee-sentinel\s*linux · offline · cannot connect/ }),
    ).toBeInTheDocument();
  });

  it("does not dispatch to an offline device until the consequence is confirmed", async () => {
    const user = userEvent.setup();
    renderDialog();
    await loadedDevices();

    await pick(user, "Device", /Toeshee-sentinel/);
    await user.type(screen.getByLabelText("Reason"), "helpdesk ticket 4821");
    await user.click(screen.getByRole("button", { name: "Start session" }));

    expect(api.startRemoteSession).not.toHaveBeenCalled();
    const confirm = await screen.findByRole("dialog", {
      name: "Start a session on a device that cannot answer",
    });
    // The SUBJECT is the load-bearing part — it is what lets somebody notice
    // they have the wrong machine.
    expect(within(confirm).getByText("Toeshee-sentinel")).toBeInTheDocument();
    expect(confirm).toHaveTextContent(/is offline/i);
  });

  it("starts against the offline device once the technician confirms", async () => {
    const user = userEvent.setup();
    renderDialog();
    await loadedDevices();

    await pick(user, "Device", /Toeshee-sentinel/);
    await user.type(screen.getByLabelText("Reason"), "helpdesk ticket 4821");
    await user.click(screen.getByRole("button", { name: "Start session" }));
    await user.click(await screen.findByRole("button", { name: "Start session anyway" }));

    expect(api.startRemoteSession).toHaveBeenCalledWith({
      device_id: "d2",
      protocol: "screen",
      mode: "attended",
      reason: "helpdesk ticket 4821",
    });
  });

  it("sends nothing when the confirmation is cancelled", async () => {
    // A confirmation whose Cancel still dispatched would be worse than none.
    const user = userEvent.setup();
    renderDialog();
    await loadedDevices();

    await pick(user, "Device", /Toeshee-sentinel/);
    await user.type(screen.getByLabelText("Reason"), "helpdesk ticket 4821");
    await user.click(screen.getByRole("button", { name: "Start session" }));

    const confirm = await screen.findByRole("dialog", {
      name: "Start a session on a device that cannot answer",
    });
    await user.click(within(confirm).getByRole("button", { name: "Cancel" }));

    expect(api.startRemoteSession).not.toHaveBeenCalled();
    // And the start dialog is still there, with the work still in it.
    expect(screen.getByLabelText("Reason")).toHaveValue("helpdesk ticket 4821");
  });

  it("does not interrupt an ordinary attended session on an online device", async () => {
    // The other half of the finding, and the one that keeps confirmations
    // worth reading: the safe path must not grow a click.
    const user = userEvent.setup();
    renderDialog();
    await loadedDevices();

    await pick(user, "Device", /workstation-1/);
    await user.type(screen.getByLabelText("Reason"), "helpdesk ticket 4821");
    await user.click(screen.getByRole("button", { name: "Start session" }));

    expect(api.startRemoteSession).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("button", { name: "Start session anyway" })).not.toBeInTheDocument();
  });
});

describe("unattended mode, which is access without consent", () => {
  it("says so beside the control, before the technician commits", async () => {
    const user = userEvent.setup();
    renderDialog();
    await loadedDevices();

    await user.selectOptions(screen.getByLabelText("Mode"), "unattended");

    expect(screen.getByText(/Nobody at the device is asked to consent/i)).toBeInTheDocument();
  });

  it("says the server decides, so a technician is not surprised by an attended session", async () => {
    // Policy and the remote.unattended permission are checked server-side;
    // without both the session silently runs attended (spec §3.4).
    const user = userEvent.setup();
    renderDialog();
    await loadedDevices();

    await user.selectOptions(screen.getByLabelText("Mode"), "unattended");

    expect(screen.getByText(/otherwise the session runs attended/i)).toBeInTheDocument();
  });

  it("does not dispatch an unattended session until the escalation is confirmed", async () => {
    const user = userEvent.setup();
    renderDialog();
    await loadedDevices();

    await pick(user, "Device", /workstation-1/);
    await user.selectOptions(screen.getByLabelText("Mode"), "unattended");
    await user.type(screen.getByLabelText("Reason"), "helpdesk ticket 4821");
    await user.click(screen.getByRole("button", { name: "Start session" }));

    expect(api.startRemoteSession).not.toHaveBeenCalled();
    const confirm = await screen.findByRole("dialog", { name: "Start an unattended session" });
    expect(within(confirm).getByText("workstation-1")).toBeInTheDocument();
    expect(confirm).toHaveTextContent(/WITHOUT asking the person at that device to consent/);
    // Named as the technician's decision, because that is what the audit log
    // will say.
    expect(confirm).toHaveTextContent(/audit log names you/i);
  });

  it("sends mode=unattended only after the confirmation is accepted", async () => {
    const user = userEvent.setup();
    renderDialog();
    await loadedDevices();

    await pick(user, "Device", /workstation-1/);
    await user.selectOptions(screen.getByLabelText("Mode"), "unattended");
    await user.type(screen.getByLabelText("Reason"), "helpdesk ticket 4821");
    await user.click(screen.getByRole("button", { name: "Start session" }));
    await user.click(await screen.findByRole("button", { name: "Start unattended session" }));

    expect(api.startRemoteSession).toHaveBeenCalledWith({
      device_id: "d1",
      protocol: "screen",
      mode: "unattended",
      reason: "helpdesk ticket 4821",
    });
  });
});

describe("screen share, the only session type", () => {
  it("offers no protocol and no credential, and does not load the vault", async () => {
    // The helper runs in the logged-on user's session: there is no logon to
    // perform, and the server refuses a credential_id on a screen session.
    renderDialog();
    await loadedDevices();

    expect(screen.queryByLabelText("Protocol")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("combobox", { name: /Credential/ }),
    ).not.toBeInTheDocument();
    expect(api.listCredentials).not.toHaveBeenCalled();
  });

  it("shows the server's refusal when the device's agent cannot share its screen", async () => {
    api.startRemoteSession = vi
      .fn()
      .mockRejectedValue(new ApiError(409, JSON.stringify({ detail: "agent too old to share its screen" })));
    const user = userEvent.setup();
    renderDialog();
    await loadedDevices();

    await pick(user, "Device", /workstation-1/);
    await user.type(screen.getByLabelText("Reason"), "helpdesk ticket 4821");
    await user.click(screen.getByRole("button", { name: "Start session" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/too old to share its screen/i);
  });

  it("opens from a device or a ticket with the device chosen and the reason filled in", async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <StartSessionDialog open onClose={() => {}} initialDeviceId="d1" initialReason="Ticket SD-12: printer" />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    const user = userEvent.setup();
    await loadedDevices();
    expect(screen.getByLabelText("Reason")).toHaveValue("Ticket SD-12: printer");
    await user.click(screen.getByRole("button", { name: "Start session" }));
    expect(api.startRemoteSession).toHaveBeenCalledWith({
      device_id: "d1",
      protocol: "screen",
      mode: "attended",
      reason: "Ticket SD-12: printer",
    });
  });
});

// ---------------------------------------------------------------------------
// FALSIFICATION. Every test in this file was run against a deliberately broken
// implementation and confirmed RED before the implementation was restored:
//
//   * `submit()` calling `start.mutate()` unconditionally (no confirmation
//     gate) -> the four dispatch-gating tests fail.
//   * `devicePickerOptions` returning `sublabel: undefined` -> the agent-state
//     test fails.
//   * `START_RULES` reason rule without `validate` -> the one-character reason
//     is accepted and that test fails.
//   * the credential picker restored -> the screen-share test fails.
// ---------------------------------------------------------------------------
