/**
 * Cycle 6: choosing a thing, instead of typing its UUID.
 *
 * Auditor A found five fields whose only input was a raw UUID typed by hand —
 * "Target ID", two "Device ID"s, "Requester ID", "Assign to" — plus a "Link
 * action" that requires knowing one. Their words: "requires the operator to
 * type a raw device UUID by hand — no picker, no search, no validation."
 *
 * WHAT MAKES THIS WORSE THAN INCONVENIENT. A UUID typed by hand is a UUID that
 * can be typed WRONG, and the fields in question dispatch signed jobs, start
 * remote sessions and run backups. The failure of a mistyped one is not a
 * validation message; it is the right action against the wrong machine.
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { EntityPicker } from "./picker";

const user = userEvent.setup();

const DEVICES = [
  { id: "11111111-1111-4111-8111-111111111111", label: "sentinel", sublabel: "linux · online" },
  { id: "22222222-2222-4222-8222-222222222222", label: "workstation-04", sublabel: "windows · offline" },
  { id: "33333333-3333-4333-8333-333333333333", label: "build-runner", sublabel: "linux · online" },
];

function picker(props: Partial<Parameters<typeof EntityPicker>[0]> = {}) {
  const onChange = vi.fn();
  render(
    <EntityPicker
      label="Device"
      options={DEVICES}
      value=""
      onChange={onChange}
      loading={false}
      emptyHint="Enrol a device first."
      {...props}
    />,
  );
  return { onChange };
}

describe("EntityPicker", () => {
  it("offers the names people know, and returns the id the API needs", async () => {
    const { onChange } = picker();

    await user.click(screen.getByLabelText("Device"));
    await user.click(await screen.findByRole("option", { name: /workstation-04/ }));

    expect(onChange).toHaveBeenCalledWith("22222222-2222-4222-8222-222222222222");
  });

  it("narrows as you type, so a long fleet is usable", async () => {
    picker();

    await user.type(screen.getByLabelText("Device"), "build");

    expect(await screen.findByRole("option", { name: /build-runner/ })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /workstation-04/ })).not.toBeInTheDocument();
  });

  it("shows the distinguishing detail, not just the name", async () => {
    // Two machines can share a name; "linux · online" is what tells them
    // apart, and it is the same information that stops somebody dispatching to
    // a machine that cannot answer.
    picker();

    await user.click(screen.getByLabelText("Device"));

    expect(await screen.findByRole("option", { name: /windows · offline/ })).toBeInTheDocument();
  });

  it("says what to do when there is nothing to choose", async () => {
    // Auditor A and B between them found five dropdowns that are simply empty
    // on a fresh install, with no explanation — the whole of cycle 7. An empty
    // picker must say what would fill it.
    picker({ options: [] });

    await user.click(screen.getByLabelText("Device"));

    expect(await screen.findByText("Enrol a device first.")).toBeInTheDocument();
  });

  it("says it is still loading rather than looking empty", async () => {
    picker({ options: [], loading: true });

    await user.click(screen.getByLabelText("Device"));

    expect(await screen.findByText(/loading/i)).toBeInTheDocument();
    // An empty list and a list that has not arrived are different states, and
    // the empty hint would be a lie during the second.
    expect(screen.queryByText("Enrol a device first.")).not.toBeInTheDocument();
  });

  it("shows the chosen thing by name once it is chosen", () => {
    picker({ value: "11111111-1111-4111-8111-111111111111" });

    // Not the UUID: after choosing, the field must still read as the thing.
    expect(screen.getByLabelText("Device")).toHaveValue("sentinel");
  });

  it("does not silently keep a value that is no longer offered", () => {
    // A device deleted while the form was open. Rendering a blank field with a
    // live id behind it is how a job goes to a machine nobody chose.
    picker({ value: "99999999-9999-4999-8999-999999999999" });

    expect(screen.getByLabelText("Device")).toHaveValue("");
  });

  /**
   * Review finding 3. `grep -n "describedby\|describedBy" src/components/picker.tsx`
   * returned nothing before this: not one of 39 call sites could point a
   * screen reader at anything from the picker's own input, including an
   * orphan note explaining that an unresolvable id is still in the form and
   * still being submitted. `describedBy` is additive — omitted, the input
   * carries no `aria-describedby` at all, which is the pre-existing 39-site
   * behaviour this must not disturb.
   */
  it("carries a caller-supplied description onto the input", () => {
    picker({ describedBy: "orphan-note" });

    expect(screen.getByLabelText("Device")).toHaveAttribute("aria-describedby", "orphan-note");
  });

  it("carries no aria-describedby when none is supplied", () => {
    picker();

    expect(screen.getByLabelText("Device")).not.toHaveAttribute("aria-describedby");
  });

  /**
   * Review finding 8. `emptyHint` shows only when `options.length === 0`
   * (`matches.length === 0` alone is not enough — a live query with no
   * matches against a non-empty list is "No match.", not this). A caller
   * with no reason to show it must not be forced to supply one.
   */
  it("does not require emptyHint when options is never empty", async () => {
    const { onChange } = picker({ emptyHint: undefined });

    await user.click(screen.getByLabelText("Device"));
    await user.click(await screen.findByRole("option", { name: /workstation-04/ }));

    expect(onChange).toHaveBeenCalledWith("22222222-2222-4222-8222-222222222222");
  });
});
