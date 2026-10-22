/**
 * The maintenance-window painter: a 7×24 grid that emits the five-field cron
 * expression the server's `window_open` parses (`backend/app/core/schedule.py`),
 * and never stops the operator expressing what the field could express before
 * it existed.
 *
 * THE MODEL IS A WINDOW, NOT A BITMAP. A cron `0 2-5 * * 6` is "these hours
 * on these days", so the painted cells are always the product of a set of
 * hours and a set of days -- which is exactly what a single expression can
 * say. The tests below hold both directions of the translation, the keyboard
 * contract, and the escape hatch for anything a grid cannot draw.
 */

import { render, screen, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { WeekPainter, cronToWindow, windowToCron } from "./weekpainter";

const user = userEvent.setup();

function Harness({ initial }: { initial: string | null }) {
  const [value, setValue] = useState<string | null>(initial);
  return (
    <>
      <WeekPainter value={value} onChange={setValue} label="Maintenance window" />
      <output data-testid="cron">{value ?? "(none)"}</output>
    </>
  );
}

const cell = (name: string) => screen.getByRole("gridcell", { name });

describe("cron <-> window", () => {
  it("reads a window whose minute is 0, hours a range, dom and month *, dow a list", () => {
    expect(cronToWindow("0 2-5 * * 6")).toEqual({ hours: [2, 3, 4, 5], days: [6] });
    expect(cronToWindow("0 22,23 * * 1,2,3,4,5")).toEqual({ hours: [22, 23], days: [1, 2, 3, 4, 5] });
    expect(cronToWindow("0 2-5,22 * * 1-5")).toEqual({ hours: [2, 3, 4, 5, 22], days: [1, 2, 3, 4, 5] });
  });

  it("folds cron's second Sunday (7) onto its first (0)", () => {
    expect(cronToWindow("0 3 * * 7")).toEqual({ hours: [3], days: [0] });
    expect(cronToWindow("0 3 * * 0,7")).toEqual({ hours: [3], days: [0] });
  });

  it("refuses anything the grid cannot draw", () => {
    for (const custom of [
      "30 2 * * 6", // a minute the grid has no row for
      "0 */6 * * *", // a step
      "0 2 1 * *", // a day of the month
      "0 2 * 3 *", // a month
      "0 2 * * mon", // a name
      "0 2 * * *  *", // six fields
      "0 25 * * 1", // out of range
      "", // nothing
    ]) {
      expect(cronToWindow(custom), custom).toBeNull();
    }
  });

  it("emits `0 <hours> * * <dows>` with the hours as compact ranges", () => {
    expect(windowToCron({ hours: [2, 3, 4, 5], days: [6] })).toBe("0 2-5 * * 6");
    expect(windowToCron({ hours: [22, 23, 2, 3, 4, 5], days: [5, 1, 3] })).toBe("0 2-5,22-23 * * 1,3,5");
    expect(windowToCron({ hours: [7], days: [0] })).toBe("0 7 * * 0");
  });

  it("round-trips every expression the grid can draw", () => {
    for (const cron of ["0 2-5 * * 6", "0 0-23 * * 0,1,2,3,4,5,6", "0 9-17 * * 1,2,3,4,5", "0 1,3,5 * * 2"]) {
      const window = cronToWindow(cron);
      expect(window, cron).not.toBeNull();
      expect(cronToWindow(windowToCron(window!))).toEqual(window);
    }
  });
});

describe("WeekPainter", () => {
  it("draws a 7 by 24 grid of cells, each named by its day and hour", () => {
    render(<Harness initial={null} />);
    expect(screen.getByRole("grid", { name: "Maintenance window" })).toBeInTheDocument();
    expect(screen.getAllByRole("gridcell")).toHaveLength(7 * 24);
    expect(cell("Tuesday 02:00")).toBeInTheDocument();
    expect(cell("Sunday 23:00")).toBeInTheDocument();
  });

  it("shows the window a cron describes, and nothing else", () => {
    render(<Harness initial="0 2-5 * * 6" />);
    expect(cell("Saturday 02:00")).toHaveAttribute("aria-selected", "true");
    expect(cell("Saturday 05:00")).toHaveAttribute("aria-selected", "true");
    expect(cell("Saturday 06:00")).toHaveAttribute("aria-selected", "false");
    expect(cell("Friday 02:00")).toHaveAttribute("aria-selected", "false");
    expect(screen.getAllByRole("gridcell", { selected: true })).toHaveLength(4);
  });

  it("emits the expression when a cell is painted", async () => {
    render(<Harness initial={null} />);
    await user.click(cell("Tuesday 02:00"));
    expect(screen.getByTestId("cron")).toHaveTextContent("0 2 * * 2");
    await user.click(cell("Tuesday 03:00"));
    expect(screen.getByTestId("cron")).toHaveTextContent("0 2-3 * * 2");
  });

  it("keeps the same hours on every painted day, which is what one expression can say", async () => {
    render(<Harness initial="0 2-3 * * 2" />);
    await user.click(cell("Thursday 02:00"));
    // Thursday joins the window with Tuesday's hours, not with one hour of its own.
    expect(cell("Thursday 03:00")).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("cron")).toHaveTextContent("0 2-3 * * 2,4");
  });

  it("unpainting shrinks the window: an hour on a one-day window, a day on a multi-day one", async () => {
    render(<Harness initial="0 2-3 * * 2,4" />);
    await user.click(cell("Thursday 02:00"));
    expect(screen.getByTestId("cron")).toHaveTextContent("0 2-3 * * 2");
    await user.click(cell("Tuesday 03:00"));
    expect(screen.getByTestId("cron")).toHaveTextContent("0 2 * * 2");
  });

  it("reports an empty grid as no expression rather than an invalid one", async () => {
    render(<Harness initial="0 2 * * 2" />);
    await user.click(cell("Tuesday 02:00"));
    expect(screen.getByTestId("cron")).toHaveTextContent("(none)");
  });

  it("is one tab stop, moves with the arrow keys and toggles with Space", async () => {
    render(<Harness initial={null} />);
    const cells = screen.getAllByRole("gridcell");
    expect(cells.filter((c) => c.getAttribute("tabindex") === "0")).toHaveLength(1);

    cell("Monday 00:00").focus();
    await user.keyboard("{ArrowRight}{ArrowRight}{ArrowDown}");
    expect(cell("Tuesday 02:00")).toHaveFocus();
    await user.keyboard(" ");
    expect(cell("Tuesday 02:00")).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("cron")).toHaveTextContent("0 2 * * 2");
    await user.keyboard(" ");
    expect(cell("Tuesday 02:00")).toHaveAttribute("aria-selected", "false");
  });

  it("extends the window with Shift and an arrow", async () => {
    render(<Harness initial={null} />);
    cell("Tuesday 02:00").focus();
    await user.keyboard(" ");
    await user.keyboard("{Shift>}{ArrowRight}{ArrowRight}{ArrowDown}{/Shift}");
    // A rectangle from the anchor to the focus: hours 2-4 on Tuesday and Wednesday.
    expect(screen.getByTestId("cron")).toHaveTextContent("0 2-4 * * 2,3");
    expect(cell("Wednesday 04:00")).toHaveFocus();
  });

  it("stays inside the grid at its edges", async () => {
    render(<Harness initial={null} />);
    cell("Monday 00:00").focus();
    await user.keyboard("{ArrowUp}{ArrowLeft}");
    expect(cell("Monday 00:00")).toHaveFocus();
    cell("Sunday 23:00").focus();
    await user.keyboard("{ArrowDown}{ArrowRight}");
    expect(cell("Sunday 23:00")).toHaveFocus();
  });

  it("shows an expression the grid cannot draw as custom, disables the grid, and keeps the field editable", async () => {
    const onChange = vi.fn();
    render(<WeekPainter value="30 2 * * 6" onChange={onChange} label="Backup window" />);
    expect(screen.getByText(/custom expression/i)).toBeInTheDocument();
    for (const gridcell of screen.getAllByRole("gridcell")) expect(gridcell).toBeDisabled();

    const field = screen.getByRole("textbox", { name: /cron expression/i });
    expect(field).toHaveValue("30 2 * * 6");
    fireEvent.change(field, { target: { value: "0 2 * * 6" } });
    expect(onChange).toHaveBeenLastCalledWith("0 2 * * 6");
  });

  it("lets the Advanced field change a drawable window too, and the grid follows", () => {
    render(<Harness initial="0 2 * * 6" />);
    const field = screen.getByRole("textbox", { name: /cron expression/i });
    fireEvent.change(field, { target: { value: "0 4-5 * * 1" } });
    expect(cell("Monday 04:00")).toHaveAttribute("aria-selected", "true");
    expect(cell("Saturday 02:00")).toHaveAttribute("aria-selected", "false");
  });

  it("says what is selected in words, so the grid is not the only carrier", () => {
    render(<Harness initial="0 2-5 * * 6" />);
    expect(screen.getByText(/Saturday, 02:00 to 06:00/)).toBeInTheDocument();
  });
});
