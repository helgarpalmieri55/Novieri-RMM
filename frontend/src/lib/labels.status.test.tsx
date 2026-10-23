import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { en } from "../i18n/en";
import { es } from "../i18n/es";
import { setActiveLanguage } from "../i18n/core";
import { expectNoEnglish, renderInSpanish } from "../i18n/test-utils";
import { Attributes } from "../components/attributes";
import { StatusPill } from "../shell/components";
import { STATUS_WORDS } from "../shell/viz";
import { humaniseEnum, humaniseStatus } from "./labels";

afterEach(() => setActiveLanguage("en"));

const WORDS = Object.keys(en.enums.StatusWord);

// Words `kindOf` knows that are a severity code or a colour, not a status a
// person is shown as text: they have no label to translate.
const NOT_LABELS = new Set([
  "crit", "warn", "info", "primary", "violet", "purple", "gold", "orange", "amber",
  "red", "green", "blue", "p1", "p2", "p3", "p4", "sev-1", "sev1", "n-a",
]);

describe("the shared status vocabulary", () => {
  it("has a Spanish word for every English one, and no extra", () => {
    expect(Object.keys(es.enums.StatusWord).sort()).toEqual([...WORDS].sort());
  });

  it("covers every status word the pill colouring knows, bar severity codes and colours", () => {
    const missing = STATUS_WORDS.filter((w) => !NOT_LABELS.has(w)).filter(
      (w) => !(w.replace(/-/g, "_") in en.enums.StatusWord),
    );
    expect(missing, `status words with no enums.StatusWord entry: ${missing.join(", ")}`).toEqual([]);
  });

  it("keeps English exactly as the derivation always read", () => {
    for (const word of WORDS) {
      expect(humaniseStatus(word), word).toBe(humaniseEnum(word));
    }
    expect(humaniseStatus("Awaiting approval")).toBe("Awaiting approval");
    expect(humaniseStatus("some_unlisted_word")).toBe("Some unlisted word");
  });

  it("reads in Spanish, and falls back to the derivation for a word it does not have", () => {
    setActiveLanguage("es");
    expect(humaniseStatus("failed")).toBe("Fallido");
    expect(humaniseStatus("In-Progress")).toBe("En curso");
    expect(humaniseStatus("OK")).toBe("Correcto");
    expect(humaniseStatus("some_unlisted_word")).toBe("Some unlisted word");
  });

  it("is what a pill and an attribute list say when nobody passed a label", () => {
    const { container } = renderInSpanish(
      <div>
        <StatusPill value="offline" />
        <StatusPill value="failed" />
        <StatusPill value="Hecho" label="Hecho" />
        <Attributes value={{ estado: "needs_attention", activo: true }} empty="nada" raw={false} />
      </div>,
    );
    expect(screen.getByText("Sin conexión")).toBeInTheDocument();
    expect(screen.getByText("Fallido")).toBeInTheDocument();
    expect(screen.getByText("Requiere atención")).toBeInTheDocument();
    expect(screen.getByText("Sí")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("re-labels a mounted pill when the language is switched", () => {
    render(<StatusPill value="pending" />);
    expect(screen.getByText("Pending")).toBeInTheDocument();
    act(() => setActiveLanguage("es"));
    expect(screen.getByText("Pendiente")).toBeInTheDocument();
    expect(screen.queryByText("Pending")).not.toBeInTheDocument();
  });
});
