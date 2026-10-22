import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { I18nProvider, useLanguage, useT } from "./context";
import { resolveLanguage } from "./resolve";
import { translate, setActiveLanguage } from "./core";
import { en } from "./en";
import { es } from "./es";

function Probe() {
  const t = useT();
  const { setLanguage } = useLanguage();
  return (
    <div>
      <span data-testid="out">{t("common.actions.save")}</span>
      <button onClick={() => setLanguage("es")}>go</button>
    </div>
  );
}

describe("i18n core", () => {
  afterEach(() => setActiveLanguage("en"));

  it("defaults to English without a provider, so existing tests need no wrapper", () => {
    render(<Probe />);
    expect(screen.getByTestId("out")).toHaveTextContent(en.common.actions.save);
  });

  it("switches every consumer at once, without remounting", () => {
    render(<I18nProvider initial="en"><Probe /></I18nProvider>);
    act(() => screen.getByText("go").click());
    expect(screen.getByTestId("out")).toHaveTextContent(es.common.actions.save);
  });

  it("interpolates named placeholders", () => {
    setActiveLanguage("en");
    expect(translate("common.count.selected", { count: 3 })).toBe("3 selected");
  });

  it("pluralises the selected count in Spanish", () => {
    setActiveLanguage("es");
    expect(translate("common.count.selected", { count: 1 })).toBe("1 seleccionado");
    expect(translate("common.count.selected", { count: 3 })).toBe("3 seleccionados");
  });

  it("returns the key rather than throwing when a truncated key lands on a branch", () => {
    expect(translate("common.count" as never)).toBe("common.count");
  });

  it("does not interpolate inherited object properties", () => {
    expect(translate("common.count.selected", { count: 2, constructor: "x" })).toBe("2 selected");
  });

  it("applies the provider's initial language before the first paint", () => {
    function Lang() {
      const t = useT();
      return <span data-testid="first">{t("common.actions.save")}</span>;
    }
    const seen: string[] = [];
    function Spy() {
      seen.push(document.documentElement.lang);
      return <Lang />;
    }
    render(<I18nProvider initial="es"><Spy /></I18nProvider>);
    expect(seen[0]).toBe("es");
    expect(screen.getByTestId("first")).toHaveTextContent(es.common.actions.save);
  });

  it("chooses the plural form with Intl.PluralRules", () => {
    setActiveLanguage("es");
    expect(translate("common.count.items", { count: 1 })).toBe("1 elemento");
    expect(translate("common.count.items", { count: 2 })).toBe("2 elementos");
    setActiveLanguage("en");
  });

  it("resolves the language in the spec's order", () => {
    expect(resolveLanguage("es", "en", "en-US")).toBe("es");
    expect(resolveLanguage("en-GB", "es", "es-ES")).toBe("en");
    expect(resolveLanguage(null, "es", "en-US")).toBe("es");
    expect(resolveLanguage(null, null, "es-CO")).toBe("es");
    expect(resolveLanguage("fr", null, "fr-FR")).toBe("en");
    expect(resolveLanguage("fr", "es", "en-US")).toBe("es");
    expect(resolveLanguage("fr", null, "es-CO")).toBe("es");
  });

  it("has the same placeholders in every Spanish string as in its English one", () => {
    const leaves = (o: unknown, path = ""): [string, string][] =>
      typeof o === "string"
        ? [[path, o]]
        : Object.entries(o as Record<string, unknown>).flatMap(([k, v]) =>
            leaves(v, path ? `${path}.${k}` : k),
          );
    const vars = (s: string) => [...s.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort().join(",");
    const esLeaves = new Map(leaves(es));
    for (const [path, text] of leaves(en)) {
      const base = path.replace(/\.(one|other|many)$/, "");
      const spanish = esLeaves.get(path) ?? esLeaves.get(`${base}.other`);
      expect(spanish, path).toBeDefined();
      expect(vars(spanish!), path).toBe(vars(text));
    }
  });
});
