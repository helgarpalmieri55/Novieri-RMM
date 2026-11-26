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

type Drawn = { transform: string | null; d: string | null; fillRule: string | null };

/** What an operator's SVG draws, as the attributes the component must reproduce. */
function drawn(svg: Element): Drawn[] {
  return [...svg.querySelectorAll("path")].map((path) => ({
    // No transform and `translate(0 0)` place a path alike (the lockup's R has none).
    transform: path.getAttribute("transform") ?? "translate(0 0)",
    d: path.getAttribute("d"),
    fillRule: path.getAttribute("fill-rule"),
  }));
}

function parse(file: string): Document {
  return new DOMParser().parseFromString(readFileSync(resolve(MARCA, file), "utf8"), "image/svg+xml");
}

describe("the brand, attribute for attribute", () => {
  it("places each wordmark letter where the file does, and keeps the ring's even-odd rule", () => {
    const source = drawn(parse("novieri-wordmark.svg").documentElement);
    const { container } = render(<NovieriWordmark />);
    const rendered = drawn(container.querySelector("svg")!);
    expect(rendered).toEqual(source);
    expect(rendered[1]?.fillRule).toBe("evenodd");
    expect(container.querySelector("svg")).toHaveAttribute("viewBox", "-0.1 -0.6 112.9 21.2");
  });

  it("draws the lockup's divider, descriptor transform and R, M, M paths as the file does", () => {
    const doc = parse("novieri-lockup.svg").documentElement;
    const { container } = render(<NovieriLockup />);
    const svg = container.querySelector("svg")!;
    expect(svg).toHaveAttribute("viewBox", doc.getAttribute("viewBox"));

    const letters = [...doc.children].filter((node) => node.tagName === "path");
    expect(drawn(svg).slice(0, 7)).toEqual(
      letters.map((path) => ({ transform: path.getAttribute("transform") ?? "translate(0 0)", d: path.getAttribute("d"), fillRule: path.getAttribute("fill-rule") })),
    );

    const rule = doc.querySelector("rect")!;
    const rendered = svg.querySelector("rect")!;
    for (const attribute of ["x", "y", "width", "height"]) expect(rendered.getAttribute(attribute)).toBe(rule.getAttribute(attribute));

    const group = doc.querySelector("g")!;
    const descriptor = svg.querySelector("g")!;
    expect(descriptor.getAttribute("transform")).toBe(group.getAttribute("transform"));
    expect(drawn(descriptor)).toEqual(drawn(group));
    expect(drawn(descriptor)).toHaveLength(3);
  });
});
