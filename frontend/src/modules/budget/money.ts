/**
 * How this module prints money.
 *
 * TWO SHAPES FOR ONE AMOUNT, and the reason is the KPI row. The tiles printed
 * the wire's Decimal string with the ISO code glued to it as the unit slot --
 * "76010.40EUR", "8445.60EUR", "101347.20EUR": no separator, no symbol, no
 * space. v3's tile row for this page draws a short human figure instead
 * (`{k:'FY26 budget',icon:'wallet',v:'€412k'}` in the prototype's
 * `P['budget']`), and the "% of plan" tile beside these already reads that
 * way. So the FIGURE is compact -- and because a compact figure has rounded
 * away real money, the EXACT amount is drawn under it in the tile's `z` slot.
 * Nothing is lost; the same fact is stated twice, once to glance at and once
 * to read.
 *
 * THE STRING IS STILL THE RECORD. `exactMoney` never parses the amount: it
 * groups the integer digits and keeps the fraction verbatim, because a
 * Decimal that has been through a float is no longer the number the server
 * sent. `compactMoney` is the one place in this module besides the chart's
 * geometry that calls `Number()`, and it is display-only -- an approximation
 * that says so by being visibly rounded, with the exact string right beneath
 * it.
 *
 * en-GB, NOT THE AMBIENT LOCALE, matching `lib/datetime.ts`, which pins its
 * formatters for the same reason: a figure that changes shape with the
 * machine's locale is a figure two operators cannot read back to each other.
 */

import { getLanguage } from "../../i18n/core";
import { intlLocale } from "../../lib/datetime";

const LOCALE = (): "en-GB" | "es" => intlLocale();

/** The locale's decimal mark, read from `Intl` rather than assumed. */
function decimalMark(): string {
  return new Intl.NumberFormat(LOCALE()).formatToParts(1.1).find((p) => p.type === "decimal")?.value ?? ".";
}

/** A well-formed ISO 4217 code, which is all `Intl` will accept. */
function usable(currency: string | undefined): currency is string {
  return typeof currency === "string" && /^[A-Za-z]{3}$/.test(currency);
}

/**
 * The wire's Decimal string with its integer part grouped and its fraction
 * untouched: `"76010.40"`, `"EUR"` -> `"76,010.40 EUR"`. An amount that is
 * not a plain decimal (it never is, but the wire is not ours) is returned
 * verbatim rather than mangled.
 */
export function exactMoney(amount: string, currency?: string): string {
  const match = /^(-?)(\d+)(?:\.(\d+))?$/.exec(amount.trim());
  if (!match) return currency ? `${amount} ${currency}` : amount;
  const [, sign, whole, fraction] = match;
  // Integer digits only, so this grouping cannot move a decimal point.
  const grouped = Number(whole).toLocaleString(LOCALE());
  // The fraction stays the server's digits, but the mark in front of it is the
  // locale's: "." in English, "," in Spanish (where "." groups thousands).
  const figure = `${sign}${grouped}${fraction ? `${decimalMark()}${fraction}` : ""}`;
  return currency ? `${figure} ${currency}` : figure;
}

/**
 * The same amount as a KPI figure: `"76010.40"`, `"EUR"` -> `"€76k"`.
 *
 * THE SUFFIX IS LOWER-CASED BY HAND, and it has to be: the design file writes
 * `€412k`, and en-GB's own compact suffix is `k` in Node's ICU but `K` in
 * Chromium's -- the same call, two answers, one of them not the design's. So
 * the case is ours to state rather than to inherit, and the tests and the
 * browser agree. Only the trailing multiplier letter is touched; the number,
 * the symbol and their order stay whatever the locale says.
 *
 * A currency that is not the locale's own is disambiguated by `Intl` itself
 * -- USD reads `US$76k` here, which is the correct answer for a console whose
 * base currency may be anything and which never says "$" meaning one of five
 * dollars.
 *
 * With no usable currency (the settings query failed, so the base currency is
 * unknown) the figure carries no symbol at all -- a wrong symbol is worse
 * than none -- and `exactMoney` is left to carry no code either.
 */
export function compactMoney(amount: string, currency?: string): string {
  const value = Number(amount);
  if (!Number.isFinite(value)) return amount;
  const options: Intl.NumberFormatOptions = usable(currency)
    ? { style: "currency", currency, notation: "compact" }
    : { notation: "compact" };
  const figure = new Intl.NumberFormat(LOCALE(), options).format(value);
  // English ONLY. The multiplier letter is the last character in en-GB ("€76K"),
  // so lower-casing it is lower-casing the multiplier. In Spanish the multiplier
  // is "mil" or an upper-case "M" ("1,2 M", "76 mil €") and the design has no
  // lower-case form of it: "1,2 m" would read as metres.
  return getLanguage() === "en" ? figure.replace(/[KMBT]$/, (suffix) => suffix.toLowerCase()) : figure;
}

/**
 * A chart's figure to two places, for its data table. English keeps the plain
 * `toFixed(2)` the chart has always printed; Spanish writes it as the language
 * does ("1.234,50"), the way `exactMoney` already does beside it.
 */
export function chartFigure(value: number): string {
  if (getLanguage() === "en") return value.toFixed(2);
  return new Intl.NumberFormat(LOCALE(), { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(value);
}
