/**
 * The load-failure surface, which every list in the product goes through.
 *
 * NINE OF THE FOURTEEN AGENTS adopting the error kernel across the modules
 * reported this independently, in their own words, having been told they could
 * not edit shared files: `QueryState` accepts `error: unknown` and DISCARDS
 * it, rendering one fixed sentence for every cause and announcing none of them.
 *
 * So a 403 you lack permission for, a 404 for something deleted while you were
 * looking at it, and a 500 all read "That did not load. Retry, or check the
 * platform status page." — advice that is wrong for two of those three, since
 * retrying a 403 never helps and the status page has nothing to say about it.
 *
 * That is the same defect cycle 4 fixed on the SUBMIT path, on the READ path,
 * and it is larger: every list, every table, every detail panel.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ApiError } from "../lib/api";
import { QueryState } from "./components";

function renderFailure(error: unknown) {
  render(
    <QueryState loading={false} error={error} empty={false} emptyLabel="Nothing here.">
      <p>the list</p>
    </QueryState>,
  );
}

describe("QueryState, when the query failed", () => {
  it("says what actually went wrong", () => {
    renderFailure(new ApiError(403, ""));

    expect(screen.getByRole("alert")).toHaveTextContent(/permission/i);
    expect(screen.queryByText("the list")).not.toBeInTheDocument();
  });

  it("distinguishes causes that need different responses from the reader", () => {
    // "Retry, or check the platform status page" is advice for an outage. It
    // is wrong for a 403 — retrying never helps — and wrong for a 404.
    renderFailure(new ApiError(404, ""));
    expect(screen.getByRole("alert")).toHaveTextContent(/no longer exists/i);
  });

  it("announces the failure rather than leaving it to be noticed", () => {
    // A list that silently becomes a sentence is a change a screen reader user
    // has no way to detect.
    renderFailure(new ApiError(500, ""));

    const alert = screen.getByRole("alert");
    expect(alert).toBeInTheDocument();
    expect(alert).toHaveTextContent(/server/i);
  });

  it("never prints a raw payload, whatever the server sent", () => {
    renderFailure(new ApiError(422, JSON.stringify({ detail: [{ loc: ["query", "page"] }] })));

    expect(screen.getByRole("alert").textContent ?? "").not.toContain("{");
  });

  it("still renders the children when there is no error", () => {
    render(
      <QueryState loading={false} error={null} empty={false} emptyLabel="Nothing here.">
        <p>the list</p>
      </QueryState>,
    );

    expect(screen.getByText("the list")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
