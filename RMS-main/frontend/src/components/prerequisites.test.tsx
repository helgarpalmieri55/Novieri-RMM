import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { ApiError } from "../lib/api";
import type { MePermissions } from "../lib/mePermissions";
import {
  allPrerequisitesDone,
  combineQueries,
  prerequisiteFromQuery,
  Prerequisites,
  type Prerequisite,
  type QueryLike,
} from "./prerequisites";

// What these assert is what an operator is TOLD about a dependency on another
// module: that it is missing (and where to fix it), that it is fine, that it
// is still being looked at, or that this account cannot look. The failure the
// shared helper exists to prevent is the fourth collapsing into the first -- a
// check the caller could not make, rendered as "missing".

function renderPanel(node: React.ReactNode) {
  return render(<MemoryRouter>{node}</MemoryRouter>);
}

const done: Prerequisite = { label: "A computer runs the agent", state: "done", detail: "3 computers run the agent." };
const missing: Prerequisite = {
  label: "A device group exists",
  state: "missing",
  severity: "crit",
  detail: "No device group exists yet.",
  cta: { to: "/device-groups", label: "Create a device group" },
};

function me(status: MePermissions["status"], codes: readonly string[] = []): MePermissions {
  return {
    status,
    codes,
    has: (code) => status === "ready" && codes.includes(code),
    lacks: (code) => status === "ready" && !codes.includes(code),
  };
}

function query<T>(overrides: Partial<QueryLike<T>>): QueryLike<T> {
  return { data: undefined, error: null, isSuccess: false, isError: false, ...overrides };
}

describe("<Prerequisites>", () => {
  it("lists every item with its state, and links a missing one to where it is fixed", () => {
    renderPanel(<Prerequisites title="Before you start" subtitle="What this page needs." items={[done, missing]} />);
    expect(screen.getByRole("heading", { name: "Before you start" })).toBeInTheDocument();
    const list = screen.getByRole("list");
    expect(within(list).getAllByRole("listitem")).toHaveLength(2);
    expect(within(list).getByText("Done")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Create a device group" })).toHaveAttribute("href", "/device-groups");
    // The CTA is a real link styled as a button: an href to open in a new tab.
    expect(screen.getByRole("link", { name: "Create a device group" })).toHaveClass("btn");
    // The severity rides on the step, so the bar is not the only carrier.
    expect(screen.getByText("No device group exists yet.").closest("li")).toHaveClass("step", "now", "crit");
  });

  it("renders nothing when every item is done, unless the page asks to keep it", () => {
    const { container, unmount } = renderPanel(<Prerequisites title="Before you start" items={[done]} />);
    expect(container).toBeEmptyDOMElement();
    unmount();

    renderPanel(<Prerequisites title="Before you start" items={[done]} showWhenDone />);
    expect(screen.getByRole("heading", { name: "Before you start" })).toBeInTheDocument();
    expect(screen.getByText("Done")).toBeInTheDocument();
  });

  it("stays out of the way while the only open items are still being checked", () => {
    // A page that is set up would otherwise flash a checklist on every visit.
    const checking: Prerequisite = { label: "An agent package is published", state: "checking", detail: "Checking…" };
    const { container, unmount } = renderPanel(<Prerequisites title="Before you start" items={[done, checking]} />);
    expect(container).toBeEmptyDOMElement();
    unmount();

    // ...but a page that keeps the panel says it is checking, not that it is fine.
    renderPanel(<Prerequisites title="Before you start" items={[done, checking]} showWhenDone />);
    expect(screen.getByText("Checking…")).toBeInTheDocument();
    expect(screen.getByText("To do")).toBeInTheDocument();
  });

  it("keeps showing an item this account cannot check, as a warning and not as missing", () => {
    const unchecked: Prerequisite = {
      label: "An agent package is published",
      state: "unchecked",
      detail: "This cannot be checked from this account.",
    };
    renderPanel(<Prerequisites title="Before you start" items={[done, unchecked]} />);
    const step = screen.getByText("This cannot be checked from this account.").closest("li");
    expect(step).toHaveClass("warn");
    expect(step).not.toHaveClass("now");
    expect(within(step!).getByText("To do")).toBeInTheDocument();
  });

  it("offers a CTA only on a missing item, so a done item keeps its Done pill", () => {
    renderPanel(
      <Prerequisites
        title="Before you start"
        items={[{ ...done, cta: { to: "/devices/onboard", label: "Add a computer" } }, missing]}
      />,
    );
    expect(screen.queryByRole("link", { name: "Add a computer" })).toBeNull();
    expect(screen.getByText("Done")).toBeInTheDocument();
  });

  it("renders a waiting item as to do, with the reason it waits", () => {
    renderPanel(
      <Prerequisites
        title="Before you start"
        items={[missing, { label: "The group has a pilot ring", state: "waiting", detail: "Create a group first." }]}
      />,
    );
    const step = screen.getByText("Create a group first.").closest("li");
    expect(step).not.toHaveClass("now");
    expect(within(step!).getByText("To do")).toBeInTheDocument();
  });

  it("allPrerequisitesDone is true only when every item is done", () => {
    expect(allPrerequisitesDone([done])).toBe(true);
    expect(allPrerequisitesDone([done, missing])).toBe(false);
    expect(allPrerequisitesDone([])).toBe(true);
  });
});

describe("prerequisiteFromQuery", () => {
  const base = {
    label: "A device group exists",
    isDone: (data: number) => data > 0,
    done: (data: number) => `${data} groups exist.`,
    missing: {
      detail: "No device group exists yet.",
      severity: "warn" as const,
      cta: { to: "/device-groups", label: "Create a device group" },
    },
  };

  it("says it is checking while the query has not answered", () => {
    expect(prerequisiteFromQuery({ ...base, query: query<number>({}) })).toEqual({
      label: "A device group exists",
      state: "checking",
      detail: "Checking…",
    });
  });

  it("marks the item done, with the caller's detail, when the predicate holds", () => {
    const item = prerequisiteFromQuery({ ...base, query: query({ data: 2, isSuccess: true }) });
    expect(item).toEqual({ label: "A device group exists", state: "done", detail: "2 groups exist." });
  });

  it("marks the item missing, with its severity and CTA, when the predicate fails", () => {
    const item = prerequisiteFromQuery({ ...base, query: query({ data: 0, isSuccess: true }) });
    expect(item).toMatchObject({
      state: "missing",
      severity: "warn",
      detail: "No device group exists yet.",
      cta: { to: "/device-groups", label: "Create a device group" },
    });
  });

  it("lets the missing branch depend on the data", () => {
    const item = prerequisiteFromQuery({
      ...base,
      missing: (data: number) => ({ detail: `Only ${data}.`, severity: "crit" }),
      query: query({ data: 0, isSuccess: true }),
    });
    expect(item).toMatchObject({ state: "missing", severity: "crit", detail: "Only 0." });
  });

  it("says a failed check could not be made, with the server's reason, and never that it is missing", () => {
    const item = prerequisiteFromQuery({
      ...base,
      query: query<number>({
        isError: true,
        error: new ApiError(503, JSON.stringify({ detail: "database unavailable" })),
      }),
    });
    expect(item.state).toBe("unchecked");
    expect(String(item.detail)).toMatch(/^This could not be checked\. .*database unavailable/);
  });

  it("reads a refusal as this account being unable to check", () => {
    const item = prerequisiteFromQuery({
      ...base,
      query: query<number>({ isError: true, error: new ApiError(403, "forbidden") }),
    });
    expect(item.state).toBe("unchecked");
    expect(String(item.detail)).toContain("cannot be checked from this account");
  });

  it("says an account without the permission cannot check, whatever the query returned", () => {
    // The query here answered "none" -- which is what a caller that cannot see
    // the rows would be told by a lenient server. Reporting it would say
    // "missing" to someone who simply cannot look.
    const item = prerequisiteFromQuery({
      ...base,
      query: query({ data: 0, isSuccess: true }),
      permission: { code: "devices.read", me: me("ready", ["notification.read"]) },
    });
    expect(item.state).toBe("unchecked");
    expect(item.detail).toBe("This cannot be checked from this account: it needs permission to view devices.");
    expect(item.cta).toBeUndefined();
  });

  it("does not treat a permission list that has not loaded as a missing permission", () => {
    const item = prerequisiteFromQuery({
      ...base,
      query: query({ data: 2, isSuccess: true }),
      permission: { code: "devices.read", me: me("loading") },
    });
    expect(item.state).toBe("done");
  });

  it("checks normally when the account holds the permission", () => {
    const item = prerequisiteFromQuery({
      ...base,
      query: query({ data: 0, isSuccess: true }),
      permission: { code: "devices.read", me: me("ready", ["devices.read"]) },
    });
    expect(item.state).toBe("missing");
  });

  it("combines two queries: done only when both answered, failed when either failed", () => {
    const ok = query({ data: 1, isSuccess: true });
    const pending = query<number>({});
    const failed = query<number>({ isError: true, error: new ApiError(503, "down") });
    expect(combineQueries(ok, pending)).toMatchObject({ isSuccess: false, isError: false, data: undefined });
    expect(combineQueries(ok, ok)).toMatchObject({ isSuccess: true, data: [1, 1] });
    expect(combineQueries(pending, failed)).toMatchObject({ isError: true, error: failed.error });
  });

  it("waits on an earlier step, without consulting the query", () => {
    const item = prerequisiteFromQuery({
      ...base,
      query: query({ data: 0, isSuccess: true }),
      waitingOn: "Save a credential first.",
    });
    expect(item).toEqual({ label: "A device group exists", state: "waiting", detail: "Save a credential first." });
  });
});
