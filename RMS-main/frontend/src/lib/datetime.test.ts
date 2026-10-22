/**
 * Cycle 5: the five-hour shift.
 *
 * "Times are shifted by 5 hours between entry and display — what you type is
 * not what you see" (auditor A, HIGH), reproduced by auditor B in a third
 * module and filed there as a BLOCKER: "every datetime in the product is
 * displayed 5 hours earlier than it was entered".
 *
 * FIVE IS NOT A COINCIDENCE. The operator is at UTC-5. `<input
 * type="datetime-local">` yields a WALL-CLOCK string with no zone --
 * "2026-08-20T14:00" -- and sending that to an API that reads it as UTC
 * records 14:00Z for a moment that was 19:00Z. Rendering it back through the
 * browser's local zone then shows 09:00. The product loses five hours on the
 * way in and reports it confidently on the way out.
 *
 * So the fix is on BOTH sides and neither alone is enough: attach the offset
 * when reading a local field, and format through one function on the way out.
 */

import { describe, expect, it } from "vitest";

import { describeWhen, formatDateTime, formatDate, NO_DATE, toInstant } from "./datetime";

const BOGOTA = "America/Bogota"; // UTC-5, no DST — the operator's own zone.

describe("toInstant", () => {
  it("keeps the wall-clock time the user typed", () => {
    // 14:00 in Bogota is 19:00Z. Sending "2026-08-20T14:00" unqualified is
    // what loses the five hours.
    expect(toInstant("2026-08-20T14:00", BOGOTA)).toBe("2026-08-20T19:00:00.000Z");
  });

  it("round-trips: what was typed is what is shown", () => {
    // THE PROPERTY THE AUDITORS TESTED BY HAND. Everything else here is
    // mechanism; this is the defect.
    const typed = "2026-08-20T14:30";

    expect(formatDateTime(toInstant(typed, BOGOTA), BOGOTA)).toContain("14:30");
  });

  it("handles a zone that observes daylight saving on both sides of the change", () => {
    // Bogota has no DST, so a bug that assumed a fixed offset would pass every
    // test above. New York moves, and the same wall-clock hour is a different
    // instant in March and in December.
    expect(toInstant("2026-01-15T12:00", "America/New_York")).toBe("2026-01-15T17:00:00.000Z");
    expect(toInstant("2026-07-15T12:00", "America/New_York")).toBe("2026-07-15T16:00:00.000Z");
  });

  it("passes a value that already carries a zone through unchanged", () => {
    expect(toInstant("2026-08-20T19:00:00.000Z", BOGOTA)).toBe("2026-08-20T19:00:00.000Z");
  });

  it("returns an empty string for an empty field rather than inventing a date", () => {
    // An untouched optional datetime input. `new Date("")` is Invalid Date, and
    // sending "Invalid Date" or the epoch would both be a value the user never
    // entered.
    expect(toInstant("", BOGOTA)).toBe("");
  });
});

describe("formatDateTime", () => {
  it("renders in the zone it is given, not the browser's", () => {
    // The operator's profile carries a timezone (cycle 2 added it to /me). A
    // technician in another office reading the same ticket should see the same
    // moment, described in their own zone -- and both should be able to say
    // which zone that is.
    const instant = "2026-08-20T19:00:00.000Z";

    expect(formatDateTime(instant, BOGOTA)).toContain("14:00");
    expect(formatDateTime(instant, "Europe/London")).toContain("20:00");
  });

  it("is one format, so the same field does not read two ways on one page", () => {
    // Auditor A: "The same device reports two different 'last contact' times
    // in two different date formats."
    const a = formatDateTime("2026-08-20T19:00:00.000Z", BOGOTA);
    const b = formatDateTime("2026-01-02T03:04:00.000Z", BOGOTA);

    // One regex, not a digit-blanked comparison: blanking digits leaves the
    // month abbreviation, and "Aug" differing from "Jan" is the formatter
    // working rather than a defect.
    const SHAPE = /^\d{2} \w{3} \d{4}, \d{2}:\d{2}$/;
    expect(a).toMatch(SHAPE);
    expect(b).toMatch(SHAPE);
  });

  it("says so when there is no date, rather than printing Invalid Date", () => {
    for (const empty of ["", null, undefined]) {
      expect(formatDateTime(empty, BOGOTA)).toBe("—");
    }
    expect(formatDateTime("not-a-date", BOGOTA)).toBe("—");
  });

  it("falls back to the browser's zone when the profile has none", () => {
    // `timezone` is nullable on /me. An unset preference must not become
    // "Invalid Date" or UTC-pretending-to-be-local.
    expect(formatDateTime("2026-08-20T19:00:00.000Z", null)).not.toBe("—");
  });
});

describe("formatDate", () => {
  it("uses the same zone rules as the datetime formatter", () => {
    // 19:00Z on the 20th is still the 20th in Bogota; 23:30Z is the 20th
    // there and the 21st in London. A date rendered in the wrong zone is off
    // by a whole day, which is worse than five hours and harder to notice.
    expect(formatDate("2026-08-20T23:30:00.000Z", BOGOTA)).toContain("20");
    expect(formatDate("2026-08-20T23:30:00.000Z", "Europe/London")).toContain("21");
  });
});

describe("describeWhen", () => {
  const now = Date.parse("2026-09-23T12:00:00Z");

  it("says the past in words", () => {
    expect(describeWhen("2026-09-23T11:59:30Z", now)).toBe("just now");
    expect(describeWhen("2026-09-23T11:55:00Z", now)).toBe("5 minutes ago");
    expect(describeWhen("2026-09-23T11:00:00Z", now)).toBe("1 hour ago");
    expect(describeWhen("2026-09-21T12:00:00Z", now)).toBe("2 days ago");
  });

  it("says the future in words, for an expiry", () => {
    expect(describeWhen("2026-09-26T12:00:00Z", now)).toBe("in 3 days");
    expect(describeWhen("2026-09-23T12:30:00Z", now)).toBe("in 30 minutes");
  });

  it("falls back to the date when the distance is too long to read as a count", () => {
    expect(describeWhen("2027-04-08T12:00:00Z", now, "UTC")).toBe(formatDate("2027-04-08T12:00:00Z", "UTC"));
  });

  it("says a missing instant is missing", () => {
    expect(describeWhen(null, now)).toBe(NO_DATE);
    expect(describeWhen("not a date", now)).toBe(NO_DATE);
  });
});
