// Markdown rendering with an allowlist sanitizer (spec §7): marked produces
// HTML, then we rebuild the DOM keeping only known-safe tags/attributes.
// Raw HTML in Markdown never reaches the page unsanitized.

import { marked } from "marked";

import { translate } from "../../i18n/core";

const ALLOWED_TAGS = new Set([
  "p", "a", "strong", "em", "code", "pre", "ul", "ol", "li",
  "h1", "h2", "h3", "h4", "blockquote", "table", "thead", "tbody",
  "tr", "th", "td", "hr", "br",
]);

// --------------------------------------------------------------------------
// What rendered documentation looks like
// --------------------------------------------------------------------------
//
// AUDIT (HIGH): "Markdown article headings, lists and links are rendered with
// no typographic styling ... The article `<h1>` computes to `font-size: 14px;
// font-weight: 400; margin: 0px`, byte-identical to body paragraph text. The
// `<ul>` renders with no bullets and no indentation. The result is a flat
// stack of same-size lines with no visual hierarchy at all ... a runbook that
// renders as an undifferentiated wall of text cannot be skimmed under incident
// pressure, which is exactly when runbooks are read."
//
// AUDIT (MEDIUM), the same defect from the other direction: "The global table
// style force-uppercases markdown table headers, altering the author's
// content ... Documentation frequently contains case-sensitive headers --
// `pid`, `iPhone`, `kubectl`, `DNSName`. Silently uppercasing them makes the
// documentation wrong."
//
// WHY INLINE STYLE AND NOT A CLASS. The container this HTML is injected into
// carried `class="prose prose-sm"` and always had -- but nothing in this
// application defines `.prose`. Tailwind's typography plugin is not a
// dependency (`package.json`), so those two classes named a stylesheet that
// does not exist, which is precisely how the `<h1>` ended up at 14px/400 while
// the markup looked correct in review. A second class here would be the same
// bet again: a silent dependency on a rule living in a file this module does
// not own. An inline declaration travels with the element, and -- the reason
// it is the fix for the `<th>` finding rather than merely for the headings --
// it outranks the product-wide bare `th { text-transform: uppercase }` rule in
// `shell/base.css` without editing a global stylesheet that every other table
// in the product depends on.
//
// These are the audit's own numbers ("h1 24px/600, h2 19px/600, h3 16px/600
// with top margins, `ul{list-style:disc;padding-left:1.25rem}`,
// `p{margin-block:0.75rem}`, styled `blockquote`, `pre` and `code`"), not
// fresh opinions about type.
const TAG_STYLES: Readonly<Record<string, string>> = {
  h1: "font-size: 24px; font-weight: 600; line-height: 1.25; margin: 1.2em 0 0.5em;",
  h2: "font-size: 19px; font-weight: 600; line-height: 1.3; margin: 1.2em 0 0.5em;",
  h3: "font-size: 16px; font-weight: 600; line-height: 1.35; margin: 1.1em 0 0.4em;",
  h4: "font-size: 14px; font-weight: 600; margin: 1em 0 0.4em;",
  p: "margin-block: 0.75rem;",
  // `list-style` AND `padding-left`: the preflight reset zeroes both, so a
  // list restored with only one of them is either unmarked or unindented.
  ul: "list-style: disc; padding-left: 1.25rem; margin-block: 0.75rem;",
  ol: "list-style: decimal; padding-left: 1.25rem; margin-block: 0.75rem;",
  blockquote:
    "border-left: 3px solid var(--line-strong); padding-left: 0.75rem; margin-block: 0.75rem; color: var(--muted);",
  pre: "background: var(--canvas); border: 1px solid var(--line); border-radius: 8px; padding: 0.75rem; margin-block: 0.75rem; overflow-x: auto; font-size: 12px; line-height: 1.5;",
  code: "font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.9em;",
  table: "border-collapse: collapse; margin-block: 0.75rem; width: auto;",
  // `text-transform: none` is the whole of the MEDIUM finding: the author's
  // `| pid |` stays `pid`. The rest restores a readable header from the 9px
  // grey chrome micro-type the global rule imposes on body copy.
  th: "text-transform: none; font-size: 13px; font-weight: 600; letter-spacing: normal; color: var(--ink); background: var(--canvas); border: 1px solid var(--line); padding: 6px 10px; text-align: left; white-space: normal;",
  td: "font-size: 13px; border: 1px solid var(--line); padding: 6px 10px;",
  hr: "border: 0; border-top: 1px solid var(--line); margin-block: 1.25rem;",
};

function sanitizeNode(node: Node, output: Node): void {
  for (const child of Array.from(node.childNodes)) {
    if (child.nodeType === Node.TEXT_NODE) {
      output.appendChild(child.cloneNode());
      continue;
    }
    if (child.nodeType !== Node.ELEMENT_NODE) continue;
    const element = child as Element;
    const tag = element.tagName.toLowerCase();
    if (tag === "script" || tag === "style" || tag === "iframe") {
      continue; // dropped entirely, children included
    }
    if (!ALLOWED_TAGS.has(tag)) {
      // unknown wrapper: keep its (sanitized) children, drop the tag
      sanitizeNode(element, output);
      continue;
    }
    const clean = document.createElement(tag);
    // Structure the author wrote, made visible. See TAG_STYLES above for why
    // this is an attribute rather than a class.
    const style = TAG_STYLES[tag];
    if (style) clean.setAttribute("style", style);
    if (tag === "a") {
      const href = element.getAttribute("href") ?? "";
      if (/^(https?:\/\/|\/|#)/i.test(href)) {
        clean.setAttribute("href", href);
        clean.setAttribute("rel", "noopener noreferrer");
      } else {
        // AUDIT (LOW): "Blocked `javascript:` links render as indistinguishable
        // plain text with no indication they were stripped ... the author gets
        // no feedback that their link was rejected; they see a link in the
        // source and a word in the output and will assume a rendering bug."
        //
        // The refusal itself is correct and stays exactly as it was -- no href
        // is written for a scheme that is not http(s), a site-relative path or
        // a fragment. What is added is SAYING SO. Marked with an attribute and
        // an inline rule rather than a class, because the stylesheet this
        // renders into is shared across the product and a class here would be
        // a silent dependency on a rule somewhere else.
        clean.setAttribute("data-link-removed", "true");
        clean.setAttribute("title", translate("docs.markdown.linkRemoved"));
        clean.setAttribute(
          "style",
          "text-decoration: line-through; text-decoration-style: wavy; cursor: help;",
        );
      }
    }
    sanitizeNode(element, clean);
    output.appendChild(clean);
  }
}

export function renderMarkdown(md: string): string {
  const rawHtml = marked.parse(md, { async: false });
  const parsed = new DOMParser().parseFromString(rawHtml, "text/html");
  const container = document.createElement("div");
  sanitizeNode(parsed.body, container);
  return container.innerHTML;
}
