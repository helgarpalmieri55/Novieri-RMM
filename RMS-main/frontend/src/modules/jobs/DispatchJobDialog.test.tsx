/**
 * Dispatching a signed job: what the console must refuse to do quietly.
 *
 * The audit walked /devices → Run job and found five things. Three were closed
 * in earlier cycles and are still asserted here, because the code that closed
 * them was rewritten in this one:
 *
 *   1. [HIGH] a successful dispatch gave NO confirmation — the dialog simply
 *      vanished, which looks the same as a request that died on the way.
 *   2. [MEDIUM] the panel was not a `<form>` while its button claimed
 *      `type="submit"`, so Enter did nothing at all.
 *   3. [BLOCKER-adjacent] a bodyless 503 rendered as an empty string, so a
 *      failed dispatch was indistinguishable from a good one.
 *
 * And two are the subject of cycle 9:
 *
 *   4. [HIGH] "Production job-type dropdown exposes a developer test job called
 *      `hello`, pre-filled with `ping`" — and `hello` was the default.
 *   5. [HIGH] "Jobs can be dispatched to an offline agent with no warning …
 *      the operator believes they have taken an action; in reality the job sits
 *      in `queued` indefinitely."
 *
 * Finding 5 is why the confirmation exists at all: `app/api/v1/jobs.py` has no
 * cancel route, so the last moment an operator can stop a wrong job is before
 * it is signed.
 *
 * The old "[HIGH] Job dispatch accepts an empty Message with no validation" is
 * closed by DELETION — `hello` was the only dispatchable type carrying params,
 * so there is no Message field left to leave empty. What survives is the same
 * required-field machinery, now guarding the job type itself.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, type ApiClient, type Device, type Job, type Me } from "../../lib/api";
import { DispatchJobDialog } from "./DispatchJobDialog";

const dispatchJob = vi.fn();
const me = vi.fn();
const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const ME: Me = {
  user_id: "u1",
  org_id: "o1",
  email: "operator@example.com",
  display_name: "Operator",
  permissions: ["jobs.dispatch"],
  timezone: "America/New_York",
};

const DEVICE = {
  id: "11111111-1111-1111-1111-111111111111",
  hostname: "ws-alpha",
  agent_status: "online",
  last_seen_at: "2026-08-20T09:00:00Z",
} as Device;

const OFFLINE = { ...DEVICE, agent_status: "offline" } as Device;
/** No `agents` row: warehouse stock, a switch — nothing signed can reach it. */
const ASSET = { ...DEVICE, agent_status: null, last_seen_at: null } as Device;

function job(overrides: Partial<Job> = {}): Job {
  return {
    id: "22222222-2222-2222-2222-222222222222",
    type: "inventory.collect",
    device_id: DEVICE.id,
    status: "queued",
    attempts: 0,
    params: {},
    result: null,
    issued_at: "2026-08-20T09:00:00Z",
    expires_at: "2026-08-20T09:05:00Z",
    started_at: null,
    finished_at: null,
    ...overrides,
  } as Job;
}

function renderDialog(device: Device = DEVICE, onClose = () => {}) {
  mockUseApi.mockReturnValue({ dispatchJob, me } as unknown as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <DispatchJobDialog device={device} onClose={onClose} />
    </QueryClientProvider>,
  );
}

/** Choose a job type and drive the dialog through its confirmation. */
async function dispatchOnce(user: ReturnType<typeof userEvent.setup>, type = "inventory.collect") {
  await user.selectOptions(screen.getByLabelText("Job type"), type);
  await user.click(screen.getByRole("button", { name: "Send" }));
  await user.click(await screen.findByRole("button", { name: "Send job" }));
}

beforeEach(() => {
  dispatchJob.mockReset().mockResolvedValue(job());
  me.mockReset().mockResolvedValue(ME);
  mockUseApi.mockReset();
});

describe("DispatchJobDialog", () => {
  it("does not offer the developer test job type", async () => {
    // Finding 4. `hello` is a smoke-test fixture; the backend still accepts it
    // for scripted use, but it must not be one click away in the console.
    renderDialog();
    const options = [...screen.getByLabelText("Job type").querySelectorAll("option")].map(
      (option) => (option as HTMLOptionElement).value,
    );

    expect(options).not.toContain("hello");
    expect(options).toContain("inventory.collect");
  });

  it("selects no job type by default, and says so when Dispatch is pressed", async () => {
    // The audit's own remedy: "Default to no selection so the user must make a
    // deliberate choice." A default of `hello` meant the path of least
    // resistance dispatched a meaningless job.
    const user = userEvent.setup();
    renderDialog();
    expect((screen.getByLabelText("Job type") as HTMLSelectElement).value).toBe("");

    await user.click(screen.getByRole("button", { name: "Send" }));

    expect(dispatchJob).not.toHaveBeenCalled();
    expect(await screen.findByRole("alert")).toHaveTextContent("Job type is required");
    // And it must not have jumped to the confirmation either.
    expect(screen.queryByRole("button", { name: "Send job" })).not.toBeInTheDocument();
  });

  it("describes what the chosen job type will do", async () => {
    // "An operator cannot tell what `inventory.collect` does, how long it
    // takes, or whether it is disruptive."
    const user = userEvent.setup();
    renderDialog();
    await user.selectOptions(screen.getByLabelText("Job type"), "inventory.collect");

    expect(screen.getByText(/Re-reads hardware, software and network interfaces/)).toBeInTheDocument();
    expect(screen.getByText(/Non-disruptive/)).toBeInTheDocument();
  });

  it("asks before signing anything, naming the machine", async () => {
    // Cycle 8's rule: the SUBJECT is the load-bearing part of a confirmation —
    // it is what lets somebody notice they have the wrong row selected. With
    // no cancel route on the API, this is the only stop that exists.
    const user = userEvent.setup();
    renderDialog();
    await user.selectOptions(screen.getByLabelText("Job type"), "inventory.collect");
    await user.click(screen.getByRole("button", { name: "Send" }));

    const confirm = await screen.findByRole("dialog", { name: "Send this job to the device" });
    expect(confirm).toHaveTextContent("ws-alpha");
    expect(confirm).toHaveTextContent(/cannot be cancelled/i);
    expect(dispatchJob).not.toHaveBeenCalled();
  });

  it("sends nothing when the confirmation is cancelled", async () => {
    const user = userEvent.setup();
    renderDialog();
    await user.selectOptions(screen.getByLabelText("Job type"), "inventory.collect");
    await user.click(screen.getByRole("button", { name: "Send" }));
    // Scoped: the form behind it has a Cancel of its own, and clicking that
    // one would prove nothing about the confirmation.
    const confirm = await screen.findByRole("dialog", { name: "Send this job to the device" });
    await user.click(within(confirm).getByRole("button", { name: "Cancel" }));

    expect(dispatchJob).not.toHaveBeenCalled();
    expect(
      screen.queryByRole("dialog", { name: "Send this job to the device" }),
    ).not.toBeInTheDocument();
  });

  it("warns that an offline agent will not run the job now", async () => {
    // Finding 5, on the form itself — before the operator has committed to
    // anything, because that is when the fact is useful.
    renderDialog(OFFLINE);

    const warning = await screen.findByRole("status");
    expect(warning).toHaveTextContent(/offline/i);
    expect(warning).toHaveTextContent(/queue/i);
    // The operator's profile zone: 09:00Z is 05:00 in New York. A warning that
    // reported the last contact in the browser's zone would be the very defect
    // cycle 5 closed. Awaited because the zone arrives with `/me`, and the
    // warning is on screen before it does.
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent("20 Aug 2026, 05:00"),
    );
  });

  it("repeats the offline state in the confirmation", async () => {
    const user = userEvent.setup();
    renderDialog(OFFLINE);
    await user.selectOptions(screen.getByLabelText("Job type"), "inventory.collect");
    await user.click(screen.getByRole("button", { name: "Send" }));

    expect(await screen.findByRole("dialog", { name: "Send this job to the device" })).toHaveTextContent(
      /agent is offline/i,
    );
  });

  it("says nothing about being offline when the agent is online", async () => {
    // A warning that is always there is a warning nobody reads.
    renderDialog();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("refuses a target with no agent, and says why beside the control", async () => {
    // An asset — stock, a switch — has no `agents` row, so no signed job can
    // reach it. This is the one case where the button is disabled, and the
    // reason is on screen: a disabled button that never says why IS the silent
    // no-op this product was full of.
    renderDialog(ASSET);

    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    expect(screen.getByText(/No agent has enrolled on this asset/)).toBeInTheDocument();
  });

  it("confirms the dispatch instead of vanishing", async () => {
    // Finding 1. The dialog stays put and the server's 201 is announced; a
    // closing dialog is not evidence that a job was queued.
    const user = userEvent.setup();
    const onClose = vi.fn();
    renderDialog(DEVICE, onClose);
    await dispatchOnce(user);

    expect(await screen.findByRole("status")).toHaveTextContent(/sent/i);
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: /Run job on ws-alpha/ })).toBeInTheDocument();
  });

  it("never claims success optimistically when the server refuses", async () => {
    // Finding 3: the bodyless 503 the form kernel exists for. `String(error)`
    // rendered nothing here, so a failed dispatch looked like a good one.
    const user = userEvent.setup();
    dispatchJob.mockRejectedValue(new ApiError(503, ""));
    renderDialog();
    await dispatchOnce(user);

    const alert = await screen.findByRole("alert");
    expect(alert.textContent?.trim()).not.toBe("");
    expect(alert).toHaveTextContent(/unavailable/i);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("sends the chosen type and the row's device id, with no params", async () => {
    const user = userEvent.setup();
    renderDialog();
    await dispatchOnce(user);

    expect(dispatchJob).toHaveBeenCalledTimes(1);
    expect(dispatchJob.mock.calls[0]?.[0]).toEqual({
      type: "inventory.collect",
      device_id: DEVICE.id,
      params: {},
    });
  });

  it("puts its controls in a real form, so submitting it is the dispatch path", async () => {
    // Finding 2: the panel was not a `<form>` while its button claimed
    // `type="submit"`, so the button was an orphan control and native submit
    // semantics — Enter, and everything built on them — did nothing.
    //
    // DRIVEN THROUGH `submit` RATHER THAN A KEYSTROKE. Implicit submission is
    // a text-input behaviour; this form has no text input left (the Message
    // field went with `hello`), so a `{Enter}` test would assert something
    // neither jsdom nor a browser does with a `<select>` focused. What is
    // testable, and what the finding is actually about, is that the form
    // exists and its submit handler is the one that asks before signing.
    const user = userEvent.setup();
    const { container } = renderDialog();
    await user.selectOptions(screen.getByLabelText("Job type"), "inventory.collect");

    const form = container.querySelector("form");
    expect(form).not.toBeNull();
    expect(form?.querySelector('button[type="submit"]')).not.toBeNull();
    fireEvent.submit(form as HTMLFormElement);

    expect(await screen.findByRole("dialog", { name: "Send this job to the device" })).toBeInTheDocument();
    expect(dispatchJob).not.toHaveBeenCalled();
  });

  it("is a bounded modal task with a real heading, not loose controls on the page", async () => {
    // "[MEDIUM] The Run job panel is visually unstyled and escapes the card it
    // belongs to … the heading 'Run job on Toeshee-sentinel' is unstyled body
    // text (not a heading element), and 'Job type [select] Message [input]'
    // sit on a single line with no container, no padding, no border … The
    // panel does not read as a distinct, modal-like task; it reads as page
    // debris."
    renderDialog();

    const dialog = await screen.findByRole("dialog", { name: "Run job on ws-alpha" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    // A REAL HEADING, which is the half a class name cannot fake.
    expect(
      within(dialog).getByRole("heading", { name: "Run job on ws-alpha" }),
    ).toBeInTheDocument();
    // Inside the modal's own body, not loose on the page background.
    expect(dialog.querySelector(".modal-body form")).not.toBeNull();
    expect(screen.getByLabelText("Job type").closest(".modal-body")).not.toBeNull();
  });

  it("does not dispatch when Cancel is pressed on the form", async () => {
    // `ui.tsx`'s Button sets no `type`, and an untyped button inside a form
    // submits it — wrapping the panel in a `<form>` would have turned Cancel
    // into a second Dispatch.
    const user = userEvent.setup();
    const onClose = vi.fn();
    renderDialog(DEVICE, onClose);
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(dispatchJob).not.toHaveBeenCalled();
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
