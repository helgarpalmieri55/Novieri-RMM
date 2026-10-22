import { describe, expect, it } from "vitest";

import {
  moveWidget,
  moveWidgetTo,
  nextSize,
  resolveLayout,
  setWidgetHidden,
  setWidgetSize,
  type WidgetDefault,
  type WidgetPlacement,
} from "../layout";

const AVAILABLE: WidgetDefault[] = [
  { id: "endpoints", defaultSize: "small" },
  { id: "alerts", defaultSize: "small" },
  { id: "recent-devices", defaultSize: "medium" },
];

function placed(id: string, over: Partial<WidgetPlacement> = {}): WidgetPlacement {
  return { id, size: "small", hidden: false, ...over };
}

describe("resolveLayout reconciles a stored arrangement with this build", () => {
  it("returns the declared defaults when nobody has arranged anything", () => {
    // `null` is "never customized" and is NOT the same as an empty list, which
    // the server refuses to store for exactly this reason.
    expect(resolveLayout(null, AVAILABLE)).toEqual([
      placed("endpoints"),
      placed("alerts"),
      placed("recent-devices", { size: "medium" }),
    ]);
  });

  it("keeps the operator's order and their sizes", () => {
    const stored = [placed("recent-devices", { size: "wide" }), placed("endpoints")];
    const resolved = resolveLayout(stored, AVAILABLE);
    expect(resolved.slice(0, 2)).toEqual(stored);
  });

  it("drops an id this build has no widget for", () => {
    // A widget a later release removed, or a browser a version ahead. Rendering
    // it would be rendering nothing in a box.
    const resolved = resolveLayout([placed("a-widget-from-2027"), placed("alerts")], AVAILABLE);
    expect(resolved.map((entry) => entry.id)).not.toContain("a-widget-from-2027");
    expect(resolved[0]?.id).toBe("alerts");
  });

  it("appends a widget the stored arrangement never heard of, VISIBLE", () => {
    // THE DEFECT THIS PREVENTS: hiding what the stored row does not name makes
    // every new dashboard feature invisible to exactly the operators who use
    // the product enough to have customized it.
    const resolved = resolveLayout([placed("alerts")], AVAILABLE);
    expect(resolved.map((entry) => entry.id)).toEqual(["alerts", "endpoints", "recent-devices"]);
    expect(resolved.every((entry) => !entry.hidden)).toBe(true);
  });

  it("renders a duplicated id once", () => {
    // The server validates the document's shape, not its uniqueness. Two
    // copies would be one widget rendered twice and two React nodes with one
    // key, so it is enforced here, where the consequence is.
    const resolved = resolveLayout([placed("alerts"), placed("alerts")], AVAILABLE);
    expect(resolved.filter((entry) => entry.id === "alerts")).toHaveLength(1);
  });
});

describe("rearranging", () => {
  const layout = [placed("a"), placed("b"), placed("c")];

  it("swaps a widget with its neighbour", () => {
    expect(moveWidget(layout, "b", -1).map((entry) => entry.id)).toEqual(["b", "a", "c"]);
    expect(moveWidget(layout, "b", 1).map((entry) => entry.id)).toEqual(["a", "c", "b"]);
  });

  it("returns the same array when the move would fall off an end", () => {
    // Identity, not a copy: the caller skips a save it does not need, and the
    // button that would do nothing is disabled rather than silently inert.
    expect(moveWidget(layout, "a", -1)).toBe(layout);
    expect(moveWidget(layout, "c", 1)).toBe(layout);
    expect(moveWidget(layout, "nope", 1)).toBe(layout);
  });

  it("moves a widget to an arbitrary index without duplicating it", () => {
    expect(moveWidgetTo(layout, "a", 2).map((entry) => entry.id)).toEqual(["b", "c", "a"]);
    expect(moveWidgetTo(layout, "c", 0).map((entry) => entry.id)).toEqual(["c", "a", "b"]);
    expect(moveWidgetTo(layout, "a", 0)).toBe(layout);
  });

  it("resizes and hides in place, leaving position untouched", () => {
    // Hiding must not move a widget: putting it back has to restore where it
    // was, or "hide" and "forget where this was" become the same gesture.
    const hidden = setWidgetHidden(layout, "b", true);
    expect(hidden.map((entry) => entry.id)).toEqual(["a", "b", "c"]);
    expect(hidden[1]?.hidden).toBe(true);
    expect(setWidgetSize(layout, "b", "wide")[1]?.size).toBe("wide");
  });

  it("cycles size through all three and back", () => {
    expect(nextSize("small")).toBe("medium");
    expect(nextSize("medium")).toBe("wide");
    expect(nextSize("wide")).toBe("small");
  });
});
