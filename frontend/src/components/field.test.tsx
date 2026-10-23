/**
 * THE HELP SLOT, AND THE ONE DEFECT IT EXISTS TO MAKE IMPOSSIBLE.
 *
 * `Field` wraps its control INSIDE the `<label>` -- that is how the caption
 * and the control come to be one stack without an `htmlFor`/`id` pair at 300
 * call sites. The cost is that ANYTHING rendered inside that label joins the
 * control's accessible NAME. `/agent-push/authorizations` shipped a field
 * whose accessible name was its entire help sentence, and it surfaced only
 * because an unrelated guard went red.
 *
 * So the help renders OUTSIDE the label and is wired with `aria-describedby`.
 * A description is announced after the name, on request, and does not become
 * the thing the control is called. The first case below is the guard: move
 * the `<p>` back inside the `<label>` and it goes red.
 *
 * The other two are the additive contract five later tasks build on. A
 * `Field` with no `help` must emit exactly what it emitted before -- no
 * wrapper, no `aria-describedby`, the `field full` class where `full` is
 * asked for -- and a caller that already pointed `aria-describedby` at its
 * own error message must keep it, because clobbering it silences the error
 * rather than the hint.
 */

import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Field } from "./ui";

describe("Field help", () => {
  it("keeps help out of the field's accessible name", () => {
    render(
      <Field label="Transcript retention" help="Days">
        <input className="input-field" />
      </Field>,
    );
    const control = screen.getByRole("textbox", { name: "Transcript retention" });
    expect(control).toBeInTheDocument();
    expect(control).toHaveAccessibleDescription("Days");
    // THE DEFECT THIS EXISTS FOR: help inside the <label> joins the NAME.
    expect(control).not.toHaveAccessibleName(/Days/);
  });

  it("adds no describedby when there is no help", () => {
    render(
      <Field label="Port">
        <input className="input-field" />
      </Field>,
    );
    expect(screen.getByRole("textbox")).not.toHaveAttribute("aria-describedby");
  });

  it("keeps a describedby the caller already set", () => {
    render(
      <Field label="Port" help="1-65535">
        <input className="input-field" aria-describedby="port-error" />
      </Field>,
    );
    const id = screen.getByRole("textbox").getAttribute("aria-describedby") ?? "";
    expect(id.split(" ")).toContain("port-error");
    expect(id.split(" ").length).toBe(2);
  });

  /**
   * STRICTLY ADDITIVE, WHICH IS THE PART ~300 EXISTING CALL SITES DEPEND ON.
   * None of them passes `help`, so none of them may gain a wrapper element or
   * lose the `full` modifier off the label they already style against.
   */
  it("renders the label alone, with the design's modifier, when no help is given", () => {
    const { container } = render(
      <Field label="Description" full>
        <input className="input-field" />
      </Field>,
    );
    const label = screen.getByText("Description").closest("label");
    expect(label).toHaveClass("field", "full");
    expect(container.querySelector(".field-wrap")).toBeNull();
    expect(label?.parentElement).toBe(container);
  });

  it("moves the span modifier to the wrapper, so the cell is the whole stack", () => {
    // `.field.full` declares `grid-column: 1 / -1` and nothing else. With help
    // present the grid's child is the wrapper, so the modifier has to travel
    // with it -- left on the inner label it would name a grid it is no longer
    // a child of, which is the same silent no-op `.form-grid`'s absence was.
    const { container } = render(
      <Field label="Description" full help="One clause">
        <textarea className="input-field" />
      </Field>,
    );
    const wrap = container.querySelector(".field-wrap");
    expect(wrap).toHaveClass("field-wrap", "full");
    const label = screen.getByText("Description").closest("label");
    expect(label).toHaveClass("field");
    expect(label).not.toHaveClass("full");
  });

  /**
   * THE HALF THE MARKUP CANNOT GUARANTEE, AND THEREFORE HAS TO SHOUT ABOUT.
   *
   * The help renders outside the label whatever `children` is; the WIRING is
   * what needs `children` to be a single element that passes the props it is
   * handed to the control. A fragment, an array (three call sites pass one
   * today, and `ScanAuthorizationsPage` is a `<select>` plus the very `<p>` a
   * later task moves into `help=`), a bare string, or a component that drops
   * unknown props all render a paragraph that looks like help and describes
   * nothing -- with the class right, the position right and `tsc` clean.
   *
   * That is the same silent-no-op class the rest of this file exists to
   * prevent, so it is not left silent. The two cases below are the pair: it
   * fires when the wiring did not land, and it stays quiet for the ordinary
   * single-element field, because a diagnostic that cries wolf on the common
   * case gets muted and then it is silent again.
   */
  describe("when the describedby cannot reach the control", () => {
    afterEach(() => {
      vi.restoreAllMocks();
    });

    it("says so, loudly, rather than rendering help that describes nothing", () => {
      const spoke = vi.spyOn(console, "error").mockImplementation(() => {});
      render(
        <Field label="Scan window" help="UTC">
          {[
            <select key="control" className="input-field" aria-label="Scan window" />,
            <p key="note" className="note">
              Weekends only.
            </p>,
          ]}
        </Field>,
      );
      expect(spoke).toHaveBeenCalled();
      const said = spoke.mock.calls.map((call) => String(call[0])).join("\n");
      expect(said).toContain("Scan window");
      expect(said).toContain("aria-describedby");
    });

    it("stays silent for the ordinary single-element field", () => {
      const spoke = vi.spyOn(console, "error").mockImplementation(() => {});
      render(
        <Field label="Transcript retention" help="Days">
          <input className="input-field" />
        </Field>,
      );
      expect(screen.getByRole("textbox")).toHaveAccessibleDescription("Days");
      expect(spoke).not.toHaveBeenCalled();
    });

    /**
     * THE CASE THE REF+EFFECT DESIGN EXISTS FOR. A component that accepts
     * `children` (so it type-checks against `ReactElement`) but drops props
     * it does not recognise is indistinguishable from a well-behaved control
     * until it has actually rendered -- no static check on `children` before
     * render can see that `aria-describedby` never reached the DOM. Replace
     * the ref+effect body with `isValidElement(children) && children.type
     * !== Fragment` (a static check) and this goes green for the wrong
     * reason: `Dropper` passes that check (it is a single, non-Fragment
     * element) and the static version would never look at what actually
     * landed on the DOM, so it would stay silent here.
     */
    it("says so when a component drops the props it does not recognise", () => {
      const spoke = vi.spyOn(console, "error").mockImplementation(() => {});
      function Dropper({ className }: { className?: string }) {
        return <input className={className} />;
      }
      render(
        <Field label="Scan window" help="UTC">
          <Dropper className="input-field" />
        </Field>,
      );
      expect(spoke).toHaveBeenCalled();
      const said = spoke.mock.calls.map((call) => String(call[0])).join("\n");
      expect(said).toContain("Scan window");
      expect(said).toContain("aria-describedby");
    });

    /**
     * PINS THE `children.type !== Fragment` EXCLUSION (ui.tsx:769). Without
     * it, `cloneElement` is attempted on the Fragment itself, and React logs
     * its own "Invalid prop `aria-describedby` supplied to `React.Fragment`"
     * warning -- a different message, from a different source, that this
     * test must not accept as satisfying it. The assertion below checks for
     * Field's own wording specifically, so a passing run only means Field's
     * diagnostic fired, not merely that *some* console.error happened to.
     */
    it("says so for a Fragment child, without picking up React's own Fragment warning", () => {
      // A fresh element on every render (not one reused reference) --
      // otherwise React bails out of reconciling an unchanged subtree and
      // the second render this case depends on never actually happens.
      const tree = () => (
        <Field label="Base currency" help="ISO 4217">
          <>
            <input className="input-field" />
          </>
        </Field>
      );
      const spoke = vi.spyOn(console, "error").mockImplementation(() => {});
      const { rerender } = render(tree());
      // The clone only exists on a SECOND pass through Field's render: React
      // does not validate a Fragment's own props on first mount (it takes a
      // different, unvalidated path there), only once it reconciles a
      // Fragment against a previous one on update -- see ui.tsx:762-766.
      rerender(tree());
      expect(spoke).toHaveBeenCalled();
      for (const call of spoke.mock.calls) {
        // Every call must be FIELD's diagnostic. React's own Fragment
        // warning ("Invalid prop `%s` supplied to `React.Fragment`...") is a
        // distinct format string that names neither the field's label nor
        // "aria-describedby" in its own text -- so if it appears alongside
        // Field's message, this loop is what catches it.
        expect(String(call[0])).toContain("Base currency");
        expect(String(call[0])).toContain("is not announced");
        expect(String(call[0])).toContain("aria-describedby");
      }
    });
  });

  it("renders the help as the design's `.help` paragraph", () => {
    render(
      <Field label="Base currency" help={<>ISO 4217. Defaults to <code>USD</code>.</>}>
        <input className="input-field" />
      </Field>,
    );
    const control = screen.getByRole("textbox");
    const id = control.getAttribute("aria-describedby") ?? "";
    const help = document.getElementById(id);
    expect(help?.tagName).toBe("P");
    expect(help).toHaveClass("help");
    // ReactNode, not string: the later tasks put a <code> unit in here.
    expect(help?.textContent).toBe("ISO 4217. Defaults to USD.");
  });
});
