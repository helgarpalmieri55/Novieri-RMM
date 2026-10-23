import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

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
});
