import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { RowMenu } from "./rowmenu";

describe("RowMenu", () => {
  it("keeps its actions behind one named button until asked", async () => {
    const user = userEvent.setup();
    const onDelete = vi.fn();
    render(<RowMenu subject="nightly-wsus" items={[{ label: "Delete", onSelect: onDelete, destructive: true }]} />);

    expect(screen.queryByRole("menuitem", { name: "Delete" })).toBeNull();
    const button = screen.getByRole("button", { name: "More actions for nightly-wsus" });
    await user.click(button);

    const item = screen.getByRole("menuitem", { name: "Delete" });
    expect(item).toHaveClass("menu-item-destructive");
    expect(item).toHaveFocus();
    await user.click(item);
    expect(onDelete).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("closes on Escape and gives focus back to the button", async () => {
    const user = userEvent.setup();
    render(<RowMenu subject="x" items={[{ label: "Edit", onSelect: vi.fn() }, { label: "Delete", onSelect: vi.fn() }]} />);
    const button = screen.getByRole("button", { name: "More actions for x" });
    await user.click(button);
    await user.keyboard("{ArrowDown}");
    expect(screen.getByRole("menuitem", { name: "Delete" })).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("menu")).toBeNull();
    expect(button).toHaveFocus();
  });

  it("does not select a disabled item", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    render(<RowMenu subject="x" items={[{ label: "Revoke", onSelect, disabled: true }]} />);
    await user.click(screen.getByRole("button", { name: "More actions for x" }));
    expect(screen.getByRole("menuitem", { name: "Revoke" })).toBeDisabled();
  });
});
