/**
 * Cycle 8: destructive actions ask first.
 *
 * Auditor A, HIGH: "Delete destroys a record instantly with no confirmation,
 * and is styled identically to the non-destructive actions beside it."
 * Auditor B, BLOCKER: "A security control is mutated and persisted by a single
 * dropdown change with no Save, no confirmation and no feedback of any kind."
 * Plus: Resolve as a one-click irreversible state change with no way to
 * reopen; "Create token" minting a live enrollment credential with no form.
 *
 * WHAT A CONFIRMATION IS FOR, since it is easy to build one that only adds a
 * click: it must say WHAT is about to happen, to WHICH thing, and whether it
 * can be undone. "Are you sure?" answers none of those and trains people to
 * click through.
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { ConfirmDialog } from "./confirm";

const user = userEvent.setup();

function open(props: Partial<Parameters<typeof ConfirmDialog>[0]> = {}) {
  const onConfirm = vi.fn();
  const onCancel = vi.fn();
  render(
    <ConfirmDialog
      open
      title="Delete health profile"
      body="Devices using this profile fall back to the default."
      subject="Frontline laptops"
      confirmLabel="Delete profile"
      destructive
      onConfirm={onConfirm}
      onCancel={onCancel}
      {...props}
    />,
  );
  return { onConfirm, onCancel };
}

describe("ConfirmDialog", () => {
  it("names the thing it is about to act on", () => {
    open();
    // Not "Are you sure?": the subject is what lets somebody notice they have
    // the wrong row selected, which is the mistake a confirmation exists to
    // catch.
    expect(screen.getByRole("dialog")).toHaveTextContent("Frontline laptops");
    expect(screen.getByRole("dialog")).toHaveTextContent(/fall back to the default/);
  });

  it("does nothing until the confirming control is used", async () => {
    const { onConfirm, onCancel } = open();

    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("marks the destructive control as destructive", async () => {
    const { onConfirm } = open();
    const confirm = screen.getByRole("button", { name: "Delete profile" });

    // "Styled identically to the non-destructive actions beside it" is the
    // finding; a shared class is how every later confirmation inherits the fix.
    expect(confirm).toHaveClass("danger");

    await user.click(confirm);
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it("does not mark a reversible action as destructive", () => {
    open({ destructive: false, confirmLabel: "Resolve group" });

    expect(screen.getByRole("button", { name: "Resolve group" })).not.toHaveClass("danger");
  });

  it("cancels on Escape, which is the reflex people already have", async () => {
    const { onConfirm, onCancel } = open();

    await user.keyboard("{Escape}");

    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("starts with focus on cancel, not on the destructive control", async () => {
    open();

    // A dialog that opens with Delete focused turns a stray Enter -- from the
    // keypress that opened it -- into the deletion.
    expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();
  });

  it("can require the subject to be typed, for the irreversible ones", async () => {
    const { onConfirm } = open({ requireTyping: true });
    const confirm = screen.getByRole("button", { name: "Delete profile" });

    expect(confirm).toBeDisabled();

    await user.type(screen.getByLabelText(/type/i), "Frontline laptops");

    expect(confirm).toBeEnabled();
    await user.click(confirm);
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  /* --- the keyboard contract, which a browser found and jsdom can keep ---- */

  it("keeps Tab inside the dialog, forwards and backwards", async () => {
    // MEASURED IN CHROMIUM: four Tabs out of the open delete confirmation
    // reached the rail's "Dashboard" link, behind the scrim, with the dialog
    // still open and `aria-modal="true"` asserting that it could not happen.
    // A sighted keyboard user could tab past a destructive confirmation and
    // activate whatever was behind it. WCAG 2.2 SC 2.4.3.
    open({ requireTyping: true });
    const dialog = screen.getByRole("dialog");

    for (let i = 0; i < 8; i += 1) {
      await user.keyboard("{Tab}");
      expect(dialog.contains(document.activeElement)).toBe(true);
    }
    for (let i = 0; i < 8; i += 1) {
      await user.keyboard("{Shift>}{Tab}{/Shift}");
      expect(dialog.contains(document.activeElement)).toBe(true);
    }
  });

  it("leaves the blocked confirm button out of the cycle", async () => {
    // It is `disabled`, so it is not a tab stop -- and that is the point of
    // building the trap on the same structural facts the browser uses rather
    // than on a visibility test jsdom cannot answer.
    open({ requireTyping: true });
    const confirm = screen.getByRole("button", { name: "Delete profile" });

    for (let i = 0; i < 6; i += 1) {
      await user.keyboard("{Tab}");
      expect(confirm).not.toHaveFocus();
    }
  });

  it("gives focus back to whatever opened it", async () => {
    // After Escape, focus used to be on <body>: the next Tab restarted at the
    // skip link, thirty-odd stops from the row the operator was working on.
    function Harness() {
      const [asking, setAsking] = useState(false);
      return (
        <>
          <button type="button" onClick={() => setAsking(true)}>
            Delete device
          </button>
          <ConfirmDialog
            open={asking}
            title="Delete device"
            body="Its inventory and job history go with it."
            subject="ws-alpha.corp.local"
            confirmLabel="Delete"
            destructive
            onConfirm={() => setAsking(false)}
            onCancel={() => setAsking(false)}
          />
        </>
      );
    }
    render(<Harness />);
    const trigger = screen.getByRole("button", { name: "Delete device" });
    await user.click(trigger);
    expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();

    await user.keyboard("{Escape}");
    expect(trigger).toHaveFocus();
  });

  it("renders nothing when closed", () => {
    render(
      <ConfirmDialog
        open={false}
        title="t"
        body="b"
        subject="s"
        confirmLabel="c"
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});
