import { afterEach, describe, expect, it } from "vitest";
import { describeWhen, formatDate, formatDateTime } from "./datetime";
import { describeSeconds, formatCount } from "./format";
import { humaniseEnum } from "./labels";
import { setActiveLanguage } from "../i18n/core";
import { exactMoney } from "../modules/budget/money";

const ISO = "2026-10-09T14:05:00Z";
describe("dates follow the language", () => {
  afterEach(() => setActiveLanguage("en"));
  it("English is unchanged (en-GB)", () => {
    expect(formatDate(ISO, "UTC")).toBe("09 Oct 2026");
  });
  it("Spanish uses es", () => {
    setActiveLanguage("es");
    expect(formatDate(ISO, "UTC")).toBe("09 oct 2026");
    expect(formatDateTime(ISO, "UTC")).toContain("14:05");
  });
  it("relative words are translated", () => {
    setActiveLanguage("es");
    const now = Date.parse(ISO) + 5 * 60_000;
    expect(describeWhen(ISO, now)).toBe("hace 5 minutos");
    expect(describeWhen(ISO, Date.parse(ISO) + 10_000)).toBe("ahora mismo");
  });
});

describe("relative times, durations and counts keep English byte-identical", () => {
  afterEach(() => setActiveLanguage("en"));
  const t0 = Date.parse(ISO);
  it("English relative phrases", () => {
    expect(describeWhen(ISO, t0 + 10_000)).toBe("just now");
    expect(describeWhen(ISO, t0 + 60_000)).toBe("1 minute ago");
    expect(describeWhen(ISO, t0 + 5 * 60_000)).toBe("5 minutes ago");
    expect(describeWhen(ISO, t0 - 3 * 3_600_000)).toBe("in 3 hours");
    expect(describeWhen(ISO, t0 - 3 * 86_400_000)).toBe("in 3 days");
    expect(describeWhen(ISO, t0 + 86_400_000 * 2)).toBe("2 days ago");
  });
  it("Spanish relative phrases, singular and future", () => {
    setActiveLanguage("es");
    expect(describeWhen(ISO, t0 + 60_000)).toBe("hace 1 minuto");
    expect(describeWhen(ISO, t0 - 3 * 3_600_000)).toBe("en 3 horas");
    expect(describeWhen(ISO, t0 - 3 * 86_400_000)).toBe("en 3 días");
  });
  it("describeSeconds", () => {
    expect(describeSeconds(90)).toBe("90 seconds");
    expect(describeSeconds(900)).toBe("15 minutes");
    expect(describeSeconds(7200)).toBe("2 hours");
    expect(describeSeconds(86400)).toBe("1 day");
    setActiveLanguage("es");
    expect(describeSeconds(90)).toBe("90 segundos");
    expect(describeSeconds(86400)).toBe("1 día");
  });
  it("formatCount groups per language", () => {
    expect(formatCount(1234567)).toBe("1,234,567");
    setActiveLanguage("es");
    expect(formatCount(1234567)).toBe("1.234.567");
  });
});

describe("humaniseEnum with a type", () => {
  afterEach(() => setActiveLanguage("en"));
  it("a typed value uses the catalogue, an unknown one falls back to humanise", () => {
    expect(humaniseEnum("awaiting_consent", "RemoteSessionState")).toBe("Awaiting consent");
    expect(humaniseEnum("brand_new_state", "RemoteSessionState")).toBe("Brand new state");
    expect(humaniseEnum("sla_breach")).toBe("SLA breach");
    setActiveLanguage("es");
    expect(humaniseEnum("awaiting_consent", "RemoteSessionState")).toBe("Esperando consentimiento");
    expect(humaniseEnum("brand_new_state", "RemoteSessionState")).toBe("Brand new state");
  });
});

describe("money follows the language", () => {
  afterEach(() => setActiveLanguage("en"));
  it("keeps the server's fraction digits and the locale's marks", () => {
    expect(exactMoney("76010.40", "EUR")).toBe("76,010.40 EUR");
    setActiveLanguage("es");
    expect(exactMoney("76010.40", "EUR")).toBe("76.010,40 EUR");
  });
});
