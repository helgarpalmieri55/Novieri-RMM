/**
 * The three overlay components -- Drawer, CommandPalette, Toaster.
 *
 * These render against jsdom, same as `AppShell.test.tsx`: no browser, no
 * CSS engine, so nothing here can assert on the transcribed `overlays.css`
 * rules actually painting anything. What IS asserted, and matters just as
 * much: the controlled open/close contract, that Escape and the palette's
 * keyboard navigation actually move focus/selection rather than existing
 * only in a comment, and that a toast leaves the stack on its own timer AND
 * on a button press -- the two different removal paths the component owns.
 *
 * THE PALETTE STOPPED BEING A DIALOG, so four of its assertions below describe
 * a different control from the one they were written against. They are
 * REWRITTEN, not deleted: a focus trap and a scrim were real properties that
 * a real test was measuring, and the honest replacement for "Tab is held
 * inside this" is "Tab leaves this, and leaving closes it" -- which is a
 * claim of the same weight about the control that now exists. Deleting them
 * would have left the inline panel with less coverage than the modal had.
 */

import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { CommandPalette, type CommandPaletteItem } from "./CommandPalette";
import { Drawer } from "./Drawer";
import { Toaster, type ToastItem } from "./Toaster";

describe("Drawer", () => {
  it("renders nothing when closed", () => {
    render(
      <Drawer open={false} onClose={vi.fn()} title="DEV-004">
        <p>body content</p>
      </Drawer>,
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.queryByText("body content")).not.toBeInTheDocument();
  });

  it("renders title, subtitle, body and footer when open", () => {
    render(
      <Drawer
        open
        onClose={vi.fn()}
        title="DEV-004"
        subtitle="Windows 11 · Bogota HQ"
        footer={<button>Edit record</button>}
      >
        <p>body content</p>
      </Drawer>,
    );
    const dialog = screen.getByRole("dialog", { name: "DEV-004" });
    expect(dialog).toBeInTheDocument();
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(screen.getByText("Windows 11 · Bogota HQ")).toBeInTheDocument();
    expect(screen.getByText("body content")).toBeInTheDocument();
    // The design's own close icon button, and the caller-supplied footer
    // action -- two different buttons, both present.
    expect(screen.getByRole("button", { name: "Close" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Edit record" })).toBeInTheDocument();
  });

  it("closes on Escape", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(
      <Drawer open onClose={onClose} title="DEV-004">
        <p>body</p>
      </Drawer>,
    );
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("closes on the close button and on the overlay click", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(
      <Drawer open onClose={onClose} title="DEV-004">
        <p>body</p>
      </Drawer>,
    );
    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  // An aria-modal surface that leaves focus on the button behind it tells a
  // screen reader the page is inert while the caret is still on the page.
  it("takes focus when it opens", () => {
    render(
      <Drawer open onClose={vi.fn()} title="DEV-004">
        <p>body</p>
      </Drawer>,
    );
    expect(screen.getByRole("dialog", { name: "DEV-004" }).contains(document.activeElement)).toBe(
      true,
    );
  });

  // A confirmation raised from inside the drawer owns its own Escape; one
  // keypress must not cancel it AND throw away the drawer behind it.
  it("leaves Escape to a dialog stacked above it", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(
      <Drawer open onClose={onClose} title="DEV-004">
        <div role="dialog" aria-modal="true" aria-label="Confirm">
          <button type="button">Keep</button>
        </div>
      </Drawer>,
    );
    screen.getByRole("button", { name: "Keep" }).focus();
    await user.keyboard("{Escape}");
    expect(onClose).not.toHaveBeenCalled();
  });
});

describe("CommandPalette", () => {
  function items(onSelect: (id: string) => void = vi.fn()): CommandPaletteItem[] {
    return [
      { id: "devices", label: "Devices & Inventory", sub: "Fleet", type: "module", icon: "devices", onSelect: () => onSelect("devices") },
      { id: "dev-004", label: "DEV-004 · Laptop", sub: "Windows 11 · Bogota HQ", type: "device", icon: "devices", onSelect: () => onSelect("dev-004") },
      { id: "tkt-118", label: "TKT-118 · VPN drops", sub: "Priya Patel · Open", type: "ticket", icon: "ticket", onSelect: () => onSelect("tkt-118") },
    ];
  }

  it("keeps the field and drops the panel when closed", () => {
    // THE FIELD IS PERMANENT NOW. It is a control in the top bar, not a
    // surface that appears; what opens and closes is the result panel under
    // it. A closed search that rendered nothing at all would be the button
    // this replaced.
    render(<CommandPalette open={false} onClose={vi.fn()} items={items()} />);
    expect(screen.getByRole("combobox", { name: "Find or ask" })).toBeInTheDocument();
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(screen.queryByRole("option")).not.toBeInTheDocument();
  });

  it("points aria-controls at the list only while the list exists", () => {
    // axe, `aria-valid-attr-value`: an `aria-controls` naming an id that is
    // not in the document is a broken reference, and the listbox is only in
    // the document while the panel is open. The reference has to come and go
    // with the thing it references.
    const { rerender } = render(<CommandPalette open={false} onClose={vi.fn()} items={items()} />);
    const field = screen.getByRole("combobox");
    expect(field).not.toHaveAttribute("aria-controls");
    expect(field).toHaveAttribute("aria-expanded", "false");

    rerender(<CommandPalette open onClose={vi.fn()} items={items()} />);
    expect(screen.getByRole("combobox")).toHaveAttribute("aria-controls", "palette-results");
    expect(screen.getByRole("combobox")).toHaveAttribute("aria-expanded", "true");
    expect(document.getElementById("palette-results")).toBeInTheDocument();
  });

  it("lists every item when the query is empty, first item selected", () => {
    render(<CommandPalette open onClose={vi.fn()} items={items()} />);
    const list = screen.getByRole("listbox");
    expect(within(list).getAllByRole("option")).toHaveLength(3);
    expect(within(list).getByRole("option", { name: /Devices & Inventory/ })).toHaveAttribute("aria-selected", "true");
  });

  it("filters results against label, sub and type, matching the prototype's join", async () => {
    const user = userEvent.setup();
    render(<CommandPalette open onClose={vi.fn()} items={items()} />);
    await user.type(screen.getByRole("combobox"), "vpn");
    const list = screen.getByRole("listbox");
    expect(within(list).getAllByRole("option")).toHaveLength(1);
    expect(within(list).getByRole("option", { name: /TKT-118/ })).toBeInTheDocument();
  });

  it("shows the design's empty state for no matches", async () => {
    const user = userEvent.setup();
    render(<CommandPalette open onClose={vi.fn()} items={items()} />);
    await user.type(screen.getByRole("combobox"), "no such record anywhere");
    expect(screen.getByText("No records match that")).toBeInTheDocument();
    expect(screen.queryByRole("option")).not.toBeInTheDocument();
  });

  /* --- Find or Ask ------------------------------------------------------ */

  it("offers the assistant the words that matched nothing", async () => {
    // THE WHOLE POINT. A question phrased as a question matches no record by
    // construction; the old palette answered it with "No results" and a shrug.
    const user = userEvent.setup();
    const onAsk = vi.fn();
    render(
      <CommandPalette
        open
        onClose={vi.fn()}
        items={items()}
        ask={{ onAsk, label: "the Assistant" }}
      />,
    );
    await user.type(screen.getByRole("combobox"), "which machines missed patch night");

    await user.click(screen.getByRole("button", { name: /Ask the Assistant/ }));
    expect(onAsk).toHaveBeenCalledWith("which machines missed patch night");
  });

  it("offers it beside real results too, not only when nothing matched", async () => {
    const user = userEvent.setup();
    render(
      <CommandPalette open onClose={vi.fn()} items={items()} ask={{ onAsk: vi.fn(), label: "the Assistant" }} />,
    );
    await user.type(screen.getByRole("combobox"), "DEV-004");

    expect(screen.getAllByRole("option").length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: /Ask the Assistant/ })).toBeInTheDocument();
  });

  it("stays out of the way until something is typed", async () => {
    render(
      <CommandPalette open onClose={vi.fn()} items={items()} ask={{ onAsk: vi.fn(), label: "the Assistant" }} />,
    );
    expect(screen.queryByRole("button", { name: /Ask the Assistant/ })).toBeNull();
  });

  it("never offers it to an operator who cannot reach the assistant", async () => {
    // `ask` is omitted when the caller lacks `ai.use`. An affordance pointing
    // at a 403 is worse than no affordance.
    const user = userEvent.setup();
    render(<CommandPalette open onClose={vi.fn()} items={items()} />);
    await user.type(screen.getByRole("combobox"), "anything at all");
    expect(screen.queryByRole("button", { name: /Ask/ })).toBeNull();
  });

  it("sends the question on Enter when nothing matched", async () => {
    // Enter is what a person presses after typing a question. With no result
    // to activate it used to do nothing at all.
    const user = userEvent.setup();
    const onAsk = vi.fn();
    render(
      <CommandPalette open onClose={vi.fn()} items={items()} ask={{ onAsk, label: "the Assistant" }} />,
    );
    await user.type(screen.getByRole("combobox"), "why did backup verification fail{Enter}");
    expect(onAsk).toHaveBeenCalledWith("why did backup verification fail");
  });

  it("keeps Enter on a real match as a navigation, not a question", async () => {
    // The negative control: if Enter always asked, the palette would stop
    // being a search box.
    const user = userEvent.setup();
    const onAsk = vi.fn();
    const onSelect = vi.fn();
    render(
      <CommandPalette
        open
        onClose={vi.fn()}
        items={items(onSelect)}
        ask={{ onAsk, label: "the Assistant" }}
      />,
    );
    await user.type(screen.getByRole("combobox"), "DEV-004{Enter}");
    expect(onSelect).toHaveBeenCalled();
    expect(onAsk).not.toHaveBeenCalled();
  });

  it("does not swallow the first character typed into it", async () => {
    // WHAT THE SEEDING WAS FOR. The old top bar was a `<button>`, so the
    // keystroke that opened the palette had to be caught, held, and handed to
    // an input that did not exist yet -- an `initialQuery` prop existed for
    // exactly that handover. A real field needs none of it, and the operator-
    // facing behaviour is what has to survive: type at the search, and what
    // you typed is in the search.
    const user = userEvent.setup();
    function Harness() {
      const [open, setOpen] = useState(false);
      return (
        <CommandPalette
          open={open}
          onOpen={() => setOpen(true)}
          onClose={() => setOpen(false)}
          items={items()}
        />
      );
    }
    render(<Harness />);
    const field = screen.getByRole("combobox");
    field.focus();
    await user.keyboard("DEV-0");

    expect(field).toHaveValue("DEV-0");
    expect(screen.getByRole("option", { name: /DEV-004/ })).toBeInTheDocument();
  });

  it("moves the active selection with ArrowDown/ArrowUp", async () => {
    const user = userEvent.setup();
    render(<CommandPalette open onClose={vi.fn()} items={items()} />);
    const input = screen.getByRole("combobox");

    await user.type(input, "{ArrowDown}");
    expect(screen.getByRole("option", { name: /DEV-004/ })).toHaveAttribute("aria-selected", "true");

    await user.type(input, "{ArrowDown}");
    expect(screen.getByRole("option", { name: /TKT-118/ })).toHaveAttribute("aria-selected", "true");

    // Clamped at the end, not wrapped.
    await user.type(input, "{ArrowDown}");
    expect(screen.getByRole("option", { name: /TKT-118/ })).toHaveAttribute("aria-selected", "true");

    await user.type(input, "{ArrowUp}");
    expect(screen.getByRole("option", { name: /DEV-004/ })).toHaveAttribute("aria-selected", "true");
  });

  it("activates the selected item and closes on Enter", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    const onSelect = vi.fn();
    render(<CommandPalette open onClose={onClose} items={items(onSelect)} />);

    await user.type(screen.getByRole("combobox"), "{ArrowDown}{Enter}");

    expect(onSelect).toHaveBeenCalledExactlyOnceWith("dev-004");
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("activates on click without needing Enter", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    const onSelect = vi.fn();
    render(<CommandPalette open onClose={onClose} items={items(onSelect)} />);

    await user.click(screen.getByRole("option", { name: /TKT-118/ }));

    expect(onSelect).toHaveBeenCalledExactlyOnceWith("tkt-118");
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  /* --- what the browser pass found, and what jsdom can hold ------------- */

  it("is a combobox owning a listbox, and is no longer a dialog", async () => {
    // WAS: `role="dialog" aria-modal="true"`, which told a screen reader the
    // console behind it was out of play. It no longer is -- there is no scrim
    // and the page stays live -- so claiming `aria-modal` would now be a lie
    // to the one audience that cannot see that it is one.
    //
    // What replaces it is the pattern the control was already half wearing: a
    // text field carrying `aria-controls` and `aria-activedescendant` at a
    // listbox is the APG combobox, and saying so is the accessible half of
    // taking the dialog away.
    render(<CommandPalette open onClose={vi.fn()} items={items()} />);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    const field = screen.getByRole("combobox", { name: "Find or ask" });
    expect(field).toHaveAttribute("aria-autocomplete", "list");
    expect(field).not.toHaveAttribute("aria-modal");
    expect(screen.getByRole("listbox", { name: "Search results" })).toBeInTheDocument();
  });

  it("names its results list", async () => {
    // axe, serious, 4.1.2: "aria-label attribute does not exist or is empty"
    // on `#palette-results`. A listbox is read as an ARIA input field and
    // needs a name of its own.
    render(<CommandPalette open onClose={vi.fn()} items={items()} />);
    expect(screen.getByRole("listbox", { name: "Search results" })).toBeInTheDocument();
  });

  it("keeps the empty state OUT of the listbox", async () => {
    // axe, CRITICAL, 1.3.1: "Element has children which are not allowed: h3".
    // A listbox may own only `option` and `group`, and the no-results state
    // -- a heading and a paragraph -- was rendering inside it. That is the
    // state a searching user hits most often.
    const user = userEvent.setup();
    render(<CommandPalette open onClose={vi.fn()} items={items()} />);
    await user.type(screen.getByRole("combobox"), "no such record anywhere");

    const list = screen.getByRole("listbox");
    expect(within(list).queryByRole("heading")).toBeNull();
    expect(list.contains(screen.getByText("No records match that"))).toBe(false);
    // Still mounted, because the input's `aria-controls` points at it.
    expect(list).toBeInTheDocument();
  });

  it("answers Escape from anywhere in it, not only from its input", async () => {
    // The handler was an `onKeyDown` on the input. One Tab out of it -- which
    // the browser pass did, landing on "Enroll device" on the page behind --
    // left a surface no keypress could dismiss. It then became a DOCUMENT
    // listener, which was right for a modal and wrong for this: a non-modal
    // popup answering Escape pressed anywhere on a page it does not own is
    // the same overreach in the other direction. It is bound to the wrapper,
    // which is exactly the field and its results.
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(<CommandPalette open onClose={onClose} items={items()} />);

    const first = screen.getAllByRole("option")[0];
    if (!first) throw new Error("expected an option to focus");
    first.focus();
    await user.keyboard("{Escape}");

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("lets Tab leave, and closes when it does", async () => {
    // THE INVERSION OF THE ASSERTION THIS REPLACES, and it is deliberate.
    // The modal trapped Tab, which was correct FOR A MODAL: a surface that
    // claims the page behind it is out of play must not then hand the keyboard
    // back to it. Nothing claims that any more. An inline popup that held the
    // keyboard would be a control the operator could not tab out of -- the
    // defect, not the feature -- so the guard now measures the opposite fact.
    const user = userEvent.setup();
    function Harness() {
      const [open, setOpen] = useState(true);
      return (
        <>
          <CommandPalette open={open} onClose={() => setOpen(false)} items={items()} />
          <button type="button">Enroll device</button>
        </>
      );
    }
    render(<Harness />);
    expect(screen.getByRole("listbox")).toBeInTheDocument();

    // Out of the field, through the results, and out of the control entirely.
    for (let i = 0; i < 6 && screen.queryByRole("listbox"); i += 1) {
      await user.keyboard("{Tab}");
    }

    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Enroll device" })).toHaveFocus();
  });

  it("takes focus when the panel opens and keeps it on Escape", async () => {
    // WHERE FOCUS RETURNS TO, when the field is its own trigger: the field.
    // The modal had to remember an opener because it appeared somewhere else
    // and the trigger it came from was the only way back. Nothing moved here,
    // so the correct restore is the one that does nothing -- and dropping
    // focus on <body>, which is what the modal did for a whole cycle when its
    // `autoFocus` raced the hook recording the opener, is still the failure.
    //
    // The shell owns the OTHER case: `/` pressed from elsewhere on the page
    // does have somewhere to give focus back to. `AppShell.test.tsx` holds
    // that half, and the browser harness measures it for real.
    const user = userEvent.setup();
    function Harness() {
      const [open, setOpen] = useState(false);
      return (
        <CommandPalette
          open={open}
          onOpen={() => setOpen(true)}
          onClose={() => setOpen(false)}
          items={items()}
        />
      );
    }
    render(<Harness />);
    const field = screen.getByRole("combobox", { name: "Find or ask" });
    await user.click(field);
    expect(screen.getByRole("listbox")).toBeInTheDocument();
    expect(field).toHaveFocus();

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(field).toHaveFocus();
  });

  it("closes on Escape without activating anything", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    const onSelect = vi.fn();
    render(<CommandPalette open onClose={onClose} items={items(onSelect)} />);

    await user.type(screen.getByRole("combobox"), "{Escape}");

    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onSelect).not.toHaveBeenCalled();
  });
});

describe("Toaster", () => {
  function toast(overrides: Partial<ToastItem> = {}): ToastItem {
    return { id: "t1", title: "Signed job dispatched", copy: "The envelope was queued.", ...overrides };
  }

  it("renders a toast's title and copy", () => {
    render(<Toaster toasts={[toast()]} onDismiss={vi.fn()} />);
    expect(screen.getByRole("status")).toBeInTheDocument();
    expect(screen.getByText("Signed job dispatched")).toBeInTheDocument();
    expect(screen.getByText("The envelope was queued.")).toBeInTheDocument();
  });

  it("dismisses on the dismiss button without waiting for the timer", async () => {
    const user = userEvent.setup();
    const onDismiss = vi.fn();
    render(<Toaster toasts={[toast()]} onDismiss={onDismiss} durationMs={60_000} />);
    await user.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(onDismiss).toHaveBeenCalledExactlyOnceWith("t1");
  });

  it("auto-dismisses after its duration elapses, and not a moment before", () => {
    vi.useFakeTimers();
    try {
      const onDismiss = vi.fn();
      render(<Toaster toasts={[toast()]} onDismiss={onDismiss} durationMs={3950} />);

      act(() => {
        vi.advanceTimersByTime(3949);
      });
      expect(onDismiss).not.toHaveBeenCalled();

      act(() => {
        vi.advanceTimersByTime(1);
      });
      expect(onDismiss).toHaveBeenCalledExactlyOnceWith("t1");
    } finally {
      vi.useRealTimers();
    }
  });

  it("renders success without a tone modifier and error/info with one, per tone", () => {
    const { container, rerender } = render(<Toaster toasts={[toast({ id: "s", tone: "success" })]} onDismiss={vi.fn()} />);
    expect(container.querySelector(".toast-icon")).not.toHaveClass("red");
    expect(container.querySelector(".toast-icon")).not.toHaveClass("info");

    rerender(<Toaster toasts={[toast({ id: "e", tone: "error" })]} onDismiss={vi.fn()} />);
    expect(container.querySelector(".toast-icon")).toHaveClass("red");

    rerender(<Toaster toasts={[toast({ id: "i", tone: "info" })]} onDismiss={vi.fn()} />);
    expect(container.querySelector(".toast-icon")).toHaveClass("info");
  });

  it("stacks multiple toasts and dismisses only the one pressed", async () => {
    const user = userEvent.setup();
    const onDismiss = vi.fn();
    render(
      <Toaster
        toasts={[toast({ id: "a", title: "First" }), toast({ id: "b", title: "Second" })]}
        onDismiss={onDismiss}
        durationMs={60_000}
      />,
    );
    const statuses = screen.getAllByRole("status");
    expect(statuses).toHaveLength(2);
    const second = statuses[1];
    if (!second) throw new Error("expected a second toast");

    await user.click(within(second).getByRole("button", { name: "Dismiss" }));
    expect(onDismiss).toHaveBeenCalledExactlyOnceWith("b");
  });
});
