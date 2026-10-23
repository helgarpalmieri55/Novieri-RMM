/**
 * The shared modal's keyboard and focus contract.
 *
 * WHY THIS FILE IS WORTH ITS OWN SUITE. Both auditors filed the same four gaps
 * against every modal in the product, and one of them counted them: "Add
 * channel" on /notifications, "Add mailbox" on /email-intake, "Provision a
 * device" on /provisioning, "Promote a device" on /site-servers, "New license"
 * on /licenses, "New subscription" on /subscriptions — one component, six
 * pages, the same four gaps. Their conclusion was that "one fix to the shared
 * modal component repairs at least six dialogs". This is the test that keeps
 * that one fix fixed; without it, six findings are closed by a single
 * `useEffect` that nothing watches.
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { Dialog, Field } from "./ui";

const user = userEvent.setup();

function Harness({ onClose = vi.fn() }: { onClose?: () => void }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        Add channel
      </button>
      <Dialog
        open={open}
        onClose={() => {
          setOpen(false);
          onClose();
        }}
        title="Add channel"
      >
        <label>
          Name
          <input />
        </label>
      </Dialog>
    </>
  );
}

describe("Dialog", () => {
  // The Drawer's close button carries the x glyph; the Dialog's used to be an
  // empty bordered box with only an accessible name.
  it("draws the close glyph inside its close button", async () => {
    render(<Harness />);
    await user.click(screen.getByRole("button", { name: "Add channel" }));
    const close = screen.getByRole("button", { name: "Close" });
    expect(close.querySelector("svg")).not.toBeNull();
  });

  it("closes on Escape", async () => {
    const onClose = vi.fn();
    render(<Harness onClose={onClose} />);
    await user.click(screen.getByRole("button", { name: "Add channel" }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    await user.keyboard("{Escape}");

    expect(onClose).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("moves focus into the dialog, onto the dialog itself", async () => {
    // NOT onto the first field. A dialog that focuses its first input skips
    // its own heading, so a screen-reader user hears "Name, edit text" with no
    // idea which dialog they are in.
    render(<Harness />);
    await user.click(screen.getByRole("button", { name: "Add channel" }));

    expect(screen.getByRole("dialog")).toHaveFocus();
  });

  it("gives focus back to whatever opened it", async () => {
    // Without this a keyboard user who closes a modal restarts tabbing from
    // the top of the document — thirty-odd stops from the button they pressed.
    render(<Harness />);
    const trigger = screen.getByRole("button", { name: "Add channel" });
    await user.click(trigger);
    await user.keyboard("{Escape}");

    expect(trigger).toHaveFocus();
  });

  it("locks the page behind it, and unlocks on close", async () => {
    render(<Harness />);
    expect(document.body.style.overflow).toBe("");

    await user.click(screen.getByRole("button", { name: "Add channel" }));
    expect(document.body.style.overflow).toBe("hidden");

    await user.keyboard("{Escape}");
    expect(document.body.style.overflow).toBe("");
  });

  it("restores the previous overflow rather than blanking it", async () => {
    // A nested surface that had already locked scrolling must not be undone
    // by this one closing.
    document.body.style.overflow = "clip";
    render(<Harness />);
    await user.click(screen.getByRole("button", { name: "Add channel" }));
    await user.keyboard("{Escape}");

    expect(document.body.style.overflow).toBe("clip");
    document.body.style.overflow = "";
  });

  it("offers a labelled close control", async () => {
    render(<Harness />);
    await user.click(screen.getByRole("button", { name: "Add channel" }));

    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("binds no key handler while it is shut", async () => {
    // The listener is added inside the open branch; a global Escape handler
    // living past the close would swallow Escape for whatever is behind it.
    const onClose = vi.fn();
    render(<Harness onClose={onClose} />);

    await user.keyboard("{Escape}");
    expect(onClose).not.toHaveBeenCalled();
  });

  it("keeps Tab inside it, forwards and backwards", async () => {
    // `aria-modal="true"` tells assistive technology that nothing behind this
    // is reachable. Until the trap landed, that was simply untrue: the
    // browser pass tabbed four times out of an open confirmation and reached
    // the rail's "Dashboard" link with the dialog still up.
    render(<Harness />);
    await user.click(screen.getByRole("button", { name: "Add channel" }));
    const dialog = screen.getByRole("dialog");

    for (let i = 0; i < 6; i += 1) {
      await user.keyboard("{Tab}");
      expect(dialog.contains(document.activeElement)).toBe(true);
    }
    for (let i = 0; i < 6; i += 1) {
      await user.keyboard("{Shift>}{Tab}{/Shift}");
      expect(dialog.contains(document.activeElement)).toBe(true);
    }
  });

  it("cycles rather than dead-ends at the last control", async () => {
    render(<Harness />);
    await user.click(screen.getByRole("button", { name: "Add channel" }));
    const close = screen.getByRole("button", { name: "Close" });
    const name = screen.getByRole("textbox");

    // The panel takes focus first, so the first Tab reaches the first stop.
    await user.keyboard("{Tab}");
    expect(close).toHaveFocus();
    await user.keyboard("{Tab}");
    expect(name).toHaveFocus();
    await user.keyboard("{Tab}");
    expect(close).toHaveFocus();
    await user.keyboard("{Shift>}{Tab}{/Shift}");
    expect(name).toHaveFocus();
  });

  it("counts its scroll locks rather than saving and restoring one each", async () => {
    // THE ORDER BUG THIS REPLACES, WHICH THREE DEPLOY TESTS CAUGHT. Every
    // surface used to remember the `overflow` it found and put that value
    // back. A ConfirmDialog raised over an open Dialog therefore remembered
    // "hidden", and when the two unmounted in the same commit the ORDER of
    // the two cleanups decided whether the page stayed locked forever.
    // Counting removes the question.
    function Stacked() {
      const [outer, setOuter] = useState(false);
      const [inner, setInner] = useState(false);
      return (
        <>
          <button type="button" onClick={() => setOuter(true)}>
            Open outer
          </button>
          <Dialog open={outer} onClose={() => setOuter(false)} title="Outer" dismissible={!inner}>
            <button type="button" onClick={() => setInner(true)}>
              Open inner
            </button>
          </Dialog>
          <Dialog open={inner} onClose={() => setInner(false)} title="Inner">
            <button type="button" onClick={() => { setInner(false); setOuter(false); }}>
              Close both
            </button>
          </Dialog>
        </>
      );
    }

    render(<Stacked />);
    await user.click(screen.getByRole("button", { name: "Open outer" }));
    expect(document.body.style.overflow).toBe("hidden");
    await user.click(screen.getByRole("button", { name: "Open inner" }));
    expect(document.body.style.overflow).toBe("hidden");

    // Both close in one commit -- the case where order used to decide.
    await user.click(screen.getByRole("button", { name: "Close both" }));
    expect(document.body.style.overflow).toBe("");
  });

  it("keeps its heading and its accessible name", async () => {
    render(<Harness />);
    await user.click(screen.getByRole("button", { name: "Add channel" }));

    const dialog = screen.getByRole("dialog", { name: "Add channel" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(screen.getByRole("heading", { name: "Add channel" })).toBeInTheDocument();
  });
});

/**
 * THE FIELD'S SPAN MODIFIER, WHICH IS THE ONLY HALF OF THE DIALOG LAYOUT A
 * JSDOM TEST CAN HOLD.
 *
 * jsdom lays nothing out: it has no cascade and no grid, so no assertion here
 * can say a dialog's fields sit two to a row or that a textarea spans both
 * columns. What it CAN say is that the class the stylesheet needs is on the
 * element -- and that is precisely the half that was missing. `.field.full`
 * declares `grid-column: 1 / -1` and nothing else, so a `Field` that cannot
 * emit `full` cannot span, and the design's own `formField(label, control,
 * full)` has no expression in this component at all.
 */
describe("Field", () => {
  it("carries `.field` alone by default", () => {
    render(<Field label="Port">{null}</Field>);
    const field = screen.getByText("Port").closest("label");
    expect(field).toHaveClass("field");
    expect(field).not.toHaveClass("full");
  });

  it("adds the design's `full` modifier when asked to span the grid", () => {
    render(<Field label="Description" full>{null}</Field>);
    const field = screen.getByText("Description").closest("label");
    expect(field).toHaveClass("field");
    expect(field).toHaveClass("full");
  });
});
