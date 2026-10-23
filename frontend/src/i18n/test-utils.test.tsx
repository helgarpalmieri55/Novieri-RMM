import { describe, expect, it } from "vitest";
import { englishFragments, expectNoEnglish, renderInSpanish } from "./test-utils";
import { useT } from "./context";

function Good() { const t = useT(); return <p>{t("common.actions.save")}</p>; }
function Bad() { return <p>Save</p>; }

describe("expectNoEnglish", () => {
  it("passes a translated component", () => {
    const { container } = renderInSpanish(<Good />);
    expectNoEnglish(container);
  });
  it("catches a hard-coded English string", () => {
    const { container } = renderInSpanish(<Bad />);
    expect(() => expectNoEnglish(container)).toThrow(/Save/);
  });
});

describe("expectNoEnglish, the edges", () => {
  it("does not flag text that is identical in both languages", () => {
    const { container } = renderInSpanish(<p>English Español</p>);
    expectNoEnglish(container);
  });
  it("does not find an English word inside a longer Spanish one", () => {
    const { container } = renderInSpanish(<p>Cancelar Guardado Cerrado</p>);
    expectNoEnglish(container);
  });
  it("does not find an English word inside a dotted job type or permission", () => {
    // "Retry" is an English catalogue word; a job type or permission code that
    // contains it is an identifier, not prose.
    const { container, unmount } = renderInSpanish(<p title="agent.Retry.read">agent.Retry Guardar</p>);
    expectNoEnglish(container);
    unmount();
    // ...but the same word at the end of a sentence still counts.
    const { container: sentence } = renderInSpanish(<p>Press Retry. Then wait.</p>);
    expect(() => expectNoEnglish(sentence)).toThrow(/Retry/);
  });
  it("ignores a phrase with a dot on only one side, and flags one that ends a sentence", () => {
    for (const text of ["x.Retry", "Retry.x", "job x.Retry Guardar", "Guardar Retry.x job"]) {
      const { container, unmount } = renderInSpanish(<p>{text}</p>);
      expectNoEnglish(container);
      unmount();
    }
    // A full stop followed by a space, or by nothing, is the end of a sentence.
    for (const text of ["Retry. Then wait", "Please Retry."]) {
      const { container, unmount } = renderInSpanish(<p>{text}</p>);
      expect(() => expectNoEnglish(container), text).toThrow(/Retry/);
      unmount();
    }
  });
  it("finds English in aria-label, aria-description, placeholder, title and alt", () => {
    const attrs = [{ "aria-label": "Retry" }, { "aria-description": "Retry" }, { placeholder: "Retry" }, { title: "Retry" }, { alt: "Retry" }];
    for (const props of attrs) {
      const { container, unmount } = renderInSpanish(<input {...props} />);
      expect(() => expectNoEnglish(container)).toThrow(/Retry/);
      unmount();
    }
  });
  it("finds the static part of a rich-text value", () => {
    const { container } = renderInSpanish(<p>See the guide for details.</p>);
    expect(() => expectNoEnglish(container)).toThrow(/for details/);
  });
  it("honours the per-call allow list", () => {
    const { container } = renderInSpanish(<Bad />);
    expect(() => expectNoEnglish(container, ["Save"])).not.toThrow();
  });
  it("resets to English between tests", async () => {
    const { getLanguage } = await import("./core");
    expect(getLanguage()).toBe("en");
  });
});

describe("englishFragments (pure)", () => {
  const english = {
    a: { save: "Save", same: "Brand name", rich: "Please see <link>the guide</link> ok", n: { one: "{count} thing", other: "{count} things" } },
    b: { word: "Cancel" },
  };
  const spanish = {
    a: { save: "Guardar", same: "Brand name", rich: "Consulte <link>la guía</link> ya", n: { one: "{count} cosa", other: "{count} cosas" } },
    b: { word: "Cancelar" },
  };

  it("skips a leaf identical in both languages even when it is not allowlisted", () => {
    expect(englishFragments(english, spanish)).not.toContain("Brand name");
  });
  it("keeps the static parts of differing leaves, plural forms and rich text included", () => {
    const found = englishFragments(english, spanish);
    expect(found).toEqual(expect.arrayContaining(["Save", "Please see", "the guide", "thing", "things", "Cancel"]));
    expect(found).not.toContain("ok"); // under four characters
  });
  it("ignores fragments under four characters", () => {
    expect(englishFragments({ x: "Yes it is", y: "Hi" }, { x: "Sí es", y: "Hola" })).toEqual(["Yes it is"]);
  });
  it("drops a fragment the Spanish catalogue also contains verbatim", () => {
    expect(englishFragments({ x: "Cancel it" }, { x: "Cancel it" })).toEqual([]);
    expect(englishFragments({ x: "Review", y: "Check" }, { x: "Revisión", y: "Review <b>tag</b>" })).toEqual(["Check"]);
  });
  it("drops a single English word that is also a whole Spanish word (a homograph)", () => {
    const found = englishFragments(
      { a: "{name} uses", b: "Save here", c: "Not here" },
      { a: "{name} usa", b: "Guardar aquí", c: "No uses una cuenta aquí" },
    );
    expect(found).not.toContain("uses");
    // A phrase is not a homograph, and a word only inside a longer word is not one either.
    expect(found).toEqual(expect.arrayContaining(["Save here", "Not here"]));
    expect(englishFragments({ a: "Save" }, { a: "Guardar", b: "Saved" })).toEqual(["Save"]);
  });
  it("does not let a code, permission or address whitelist its words as homographs", () => {
    // `<code>devices.read</code>` is a literal in the Spanish text; it must not
    // make the English word "devices" a homograph everywhere else.
    const found = englishFragments(
      { a: "devices", b: "Needs <code>devices.read</code>", c: "Mail ops@example.com" },
      { a: "dispositivos", b: "Requiere <code>devices.read</code>", c: "Escribe a ops@example.com" },
    );
    expect(found).toContain("devices");
    // The address is a literal too: its words are not Spanish prose either.
    expect(englishFragments({ a: "example", b: "Mail ops@example.com" }, { a: "ejemplo", b: "Escribe a ops@example.com" })).toContain("example");
  });
  it("treats a one-word fragment that carries its sentence's full stop as the word", () => {
    // "host." (from "Install queued on {count} host.") is the Spanish word "host"
    // in "la clave de host." as far as a whole-word match can tell.
    const found = englishFragments({ a: "Queued on {count} host.", b: "Retry." }, { a: "En cola en {count} equipo.", b: "Reintentar.", c: "No se confirmó la clave de host." });
    expect(found).not.toContain("host.");
    expect(found).toContain("Retry.");
  });
  it("still treats a word of a Spanish sentence as a homograph, even one ending in a period", () => {
    expect(englishFragments({ a: "{name} uses" }, { a: "{name} usa", b: "No uses una cuenta aquí." })).not.toContain("uses");
    // ...and a token with an interior dot beside it in the same fragment does not hide it.
    expect(englishFragments({ a: "{name} uses" }, { a: "{name} usa", b: "No uses devices.read aquí." })).not.toContain("uses");
    expect(englishFragments({ a: "devices", b: "x" }, { a: "dispositivos", b: "No uses devices.read aquí." })).toContain("devices");
  });
});
