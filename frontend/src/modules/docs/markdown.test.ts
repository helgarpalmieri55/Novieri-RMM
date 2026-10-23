import { describe, expect, it } from "vitest";
import { renderMarkdown } from "./markdown";

describe("renderMarkdown", () => {
  it("renders basic markdown", () => {
    const html = renderMarkdown("# Title\n\nSome **bold** text and `code`.");
    expect(html).toContain("<h1");
    expect(html).toContain("<strong>bold</strong>");
    // `<code>` now carries a typographic declaration (see below), so this
    // asserts the element and its text rather than an exact byte sequence.
    expect(html).toMatch(/<code[^>]*>code<\/code>/);
  });

  it("strips script tags entirely", () => {
    const html = renderMarkdown('hello <script>alert("xss")</script> world');
    expect(html).not.toContain("<script");
    expect(html).not.toContain("alert(");
  });

  it("strips event handler attributes", () => {
    const html = renderMarkdown('<img src="x" onerror="alert(1)">');
    expect(html).not.toContain("onerror");
  });

  it("drops javascript: hrefs but keeps http links", () => {
    const bad = renderMarkdown("[click](javascript:alert(1))");
    expect(bad).not.toContain("javascript:");
    const good = renderMarkdown("[docs](https://example.com/a)");
    expect(good).toContain('href="https://example.com/a"');
  });

  it("says a link was removed rather than emitting silent plain text", () => {
    // AUDIT (LOW): "Blocked `javascript:` links render as indistinguishable
    // plain text with no indication they were stripped ... the author gets no
    // feedback that their link was rejected; they see a link in the source and
    // a word in the output and will assume a rendering bug." The REFUSAL is
    // unchanged and still asserted above; this is about saying so.
    const html = renderMarkdown("[click](javascript:alert(1))");
    expect(html).toContain('data-link-removed="true"');
    expect(html).toContain("Link removed");
    expect(html).not.toContain("javascript:");

    // And a legitimate link carries no such marker.
    expect(renderMarkdown("[docs](https://example.com/a)")).not.toContain("data-link-removed");
  });

  it("keeps tables and lists", () => {
    const html = renderMarkdown("| a | b |\n|---|---|\n| 1 | 2 |\n\n- item");
    expect(html).toContain("<table");
    expect(html).toContain("<li>item</li>");
  });

  // AUDIT (HIGH): "Markdown article headings, lists and links are rendered
  // with no typographic styling ... the article `<h1>` computes to
  // `font-size: 14px; font-weight: 400; margin: 0px`, byte-identical to body
  // paragraph text. The `<ul>` renders with no bullets and no indentation."
  //
  // The container has always carried `class="prose prose-sm"`, and nothing in
  // this application defines `.prose` — so a test asserting the CLASS would
  // have passed against the broken product for as long as the defect existed.
  // These assert the declarations that actually reach the element.
  it("gives a heading a size and a weight of its own, not body copy's", () => {
    const html = renderMarkdown("# Heading\n\nbody text");

    const heading = /<h1 style="([^"]*)"/.exec(html);
    expect(heading).not.toBeNull();
    expect(heading?.[1]).toContain("font-size: 24px");
    expect(heading?.[1]).toContain("font-weight: 600");
    // And it is not the same as the paragraph beside it, which was the
    // measured symptom: "byte-identical to body paragraph text".
    const paragraph = /<p style="([^"]*)"/.exec(html);
    expect(paragraph?.[1]).not.toEqual(heading?.[1]);
  });

  it("descends h1 → h2 → h3 rather than rendering three identical lines", () => {
    // `NaN` when no size reaches the element, which fails both comparisons —
    // the unstyled case must not slip through as "equal and therefore fine".
    const fontSize = (md: string): number => {
      const found = /style="font-size: (\d+)px/.exec(renderMarkdown(md));
      return Number(found?.[1] ?? NaN);
    };
    expect(fontSize("# a")).toBeGreaterThan(fontSize("## b"));
    expect(fontSize("## b")).toBeGreaterThan(fontSize("### c"));
  });

  it("gives a list its bullets and its indent back", () => {
    const html = renderMarkdown("- item one\n- item two");
    const list = /<ul style="([^"]*)"/.exec(html);
    expect(list?.[1]).toContain("list-style: disc");
    expect(list?.[1]).toContain("padding-left");
  });

  // AUDIT (MEDIUM): "The global table style force-uppercases markdown table
  // headers, altering the author's content ... `th.textContent === 'a'` while
  // `getComputedStyle(th).textTransform === 'uppercase'`; rendered pixel shows
  // 'A'." The bare `th { text-transform: uppercase }` rule in
  // `shell/base.css` is product-wide chrome and stays; this element opts out
  // of it, which an inline declaration can do and a class cannot rely on.
  it("does not uppercase a markdown table header the author wrote in lower case", () => {
    const html = renderMarkdown("| pid | kubectl |\n|---|---|\n| 1 | get |");

    const header = /<th style="([^"]*)"/.exec(html);
    expect(header?.[1]).toContain("text-transform: none");
    // The 9px grey chrome micro-type is overridden too: "9px grey is
    // unreadable inside body copy".
    expect(header?.[1]).toContain("font-size: 13px");
    // The author's own casing is still the text, which is what makes the
    // override observable rather than cosmetic.
    expect(html).toContain(">pid</th>");
    expect(html).toContain(">kubectl</th>");
  });

  it("styles code and quotes rather than leaving them as plain lines", () => {
    expect(renderMarkdown("`x`")).toMatch(/<code style="[^"]*monospace/);
    expect(renderMarkdown("```\nx\n```")).toMatch(/<pre style="[^"]*overflow-x: auto/);
    expect(renderMarkdown("> quoted")).toMatch(/<blockquote style="[^"]*border-left/);
  });
});
