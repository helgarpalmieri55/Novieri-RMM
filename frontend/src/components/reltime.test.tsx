import { act, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { setActiveLanguage } from "../i18n/core";
import { formatDateTime, NO_DATE } from "../lib/datetime";
import { RelTime } from "./reltime";

const NOW = Date.parse("2026-09-23T12:00:00Z");

describe("RelTime", () => {
  it("reads as relative time, with the absolute time on hover", () => {
    render(<RelTime iso="2026-09-23T11:55:00Z" zone="Europe/Lisbon" now={NOW} />);
    const time = screen.getByText("5 minutes ago");
    expect(time.tagName).toBe("TIME");
    expect(time).toHaveAttribute("datetime", "2026-09-23T11:55:00Z");
    expect(time).toHaveAttribute("title", formatDateTime("2026-09-23T11:55:00Z", "Europe/Lisbon"));
    expect(time.className).not.toContain("mono");
  });

  it("says an absent time is absent, with nothing to hover", () => {
    render(<RelTime iso={null} now={NOW} />);
    const dash = screen.getByText(NO_DATE);
    expect(dash.tagName).toBe("SPAN");
    expect(dash).not.toHaveAttribute("title");
  });

  // The kit's absent mark (P3 adoption pass): "n/a", never the em dash it was.
  it("draws the absent mark, n/a, for an absent time", () => {
    const { container } = render(<RelTime iso={null} now={NOW} />);
    expect(container.textContent).toBe("n/a");
  });

  it("draws n/d in Spanish, and follows a switch back", () => {
    setActiveLanguage("es");
    try {
      const { container } = render(<RelTime iso={undefined} now={NOW} />);
      expect(container.textContent).toBe("n/d");
      act(() => setActiveLanguage("en"));
      expect(container.textContent).toBe("n/a");
    } finally {
      setActiveLanguage("en");
    }
  });
});
