import { render, screen } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import { BRAND_NAME, NovieriLockup, NovieriSymbol, NovieriWordmark, PRODUCT_NAME } from "./brand";

const MARCA = resolve(import.meta.dirname, "../../../docs/design/marca");

describe("the brand", () => {
  it("is called what the operator named it (decision A3)", () => {
    expect(PRODUCT_NAME).toBe("Novieri RMM");
    expect(BRAND_NAME).toBe("Novieri");
  });

  it("draws the operator's own symbol, path for path", () => {
    const source = readFileSync(resolve(MARCA, "novieri-symbol.svg"), "utf8");
    const { container } = render(<NovieriSymbol />);
    const drawn = container.querySelector("path")?.getAttribute("d");
    expect(source).toContain(`d="${drawn}"`);
  });

  it("draws the operator's own wordmark, path for path", () => {
    const source = readFileSync(resolve(MARCA, "novieri-wordmark.svg"), "utf8");
    const { container } = render(<NovieriWordmark />);
    const paths = [...container.querySelectorAll("path")].map((path) => path.getAttribute("d"));
    expect(paths).toHaveLength(7);
    for (const d of paths) expect(source).toContain(`d="${d}"`);
  });

  it("names itself to assistive technology once, and the symbol is decorative unless labelled", () => {
    render(<NovieriWordmark />);
    expect(screen.getByRole("img", { name: "Novieri" })).toBeInTheDocument();
    const { container } = render(<NovieriSymbol tile />);
    expect(container.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
  });

  it("puts the product's full name on the lockup", () => {
    render(<NovieriLockup />);
    expect(screen.getByRole("img", { name: "Novieri RMM" })).toBeInTheDocument();
  });

  it("ships the favicon the operator approved, and the page links it", () => {
    const approved = readFileSync(resolve(MARCA, "novieri-favicon.svg"), "utf8");
    const shipped = readFileSync(resolve(import.meta.dirname, "../../public/favicon.svg"), "utf8");
    expect(shipped).toBe(approved);
    const html = readFileSync(resolve(import.meta.dirname, "../../index.html"), "utf8");
    expect(html).toContain('<link rel="icon" type="image/svg+xml" href="/favicon.svg" />');
    expect(html).toContain("<title>Novieri RMM</title>");
  });
});
