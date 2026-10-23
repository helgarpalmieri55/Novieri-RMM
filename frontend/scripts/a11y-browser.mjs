/**
 * The browser pass `docs/product.md` §11 says has not been done.
 *
 * WHAT THIS IS FOR. Four guards already hold WCAG 2.2 AA in this repo --
 * `contrast.test.ts`, `tokens.test.ts`, `responsive.test.ts`,
 * `no-dark-variants.test.ts` -- and §11 states plainly what none of them
 * covers: "These read tokens and stylesheets as text. jsdom implements no
 * cascade and evaluates no media query, so no test here can see a colour
 * composited by opacity, painted by a gradient, or the actual rendered result
 * at a real viewport." This script is the complement. It drives a real
 * Chromium over the dev-only `/__preview` harness and measures the pixels.
 *
 * IT IS NOT WIRED INTO CI, deliberately -- that is the operator's call, and
 * the cost is a browser download. It runs headless and exits non-zero on a
 * violation so it COULD be, unchanged.
 *
 * THE METHOD, and why it is not just axe. axe resolves a background by walking
 * the DOM for an opaque ancestor, so it returns "incomplete" for every node
 * over a gradient or a translucent wash -- 52 of them on this page at 1440px,
 * which is most of the rail and every filled chip. Those are exactly the
 * colours no other guard can see either. So the sweep below:
 *
 *   1. records each text run's computed paint (`color` for HTML, `fill` for
 *      SVG -- an axis label is painted by neither the same property nor the
 *      same cascade as a paragraph) and its accumulated ancestor `opacity`;
 *   2. blanks every glyph and screenshots, leaving the exact BACKPLATE the
 *      text sits on -- gradients resolved, washes composited, shadows painted;
 *   3. samples that backplate inside each run's own line boxes, hit-testing
 *      every point with `elementFromPoint` and clipping to the element's
 *      rounded border box, so a pixel from a neighbour, a clipping scroll
 *      container or the antialiased fringe of a pill is never mistaken for
 *      the ground behind the text;
 *   4. composites the foreground through that opacity over the worst-case
 *      backplate pixel and computes the ratio.
 *
 * Both halves are falsifiable, and `--falsify` proves it on every run. There
 * are TWO self-checks and the second one is newer than the first for a reason:
 *
 *   - the sweep repaints the table headers at the `#878590` auditor A measured
 *     from the live DOM and must report that defect at 3.49:1;
 *   - the overflow probe is handed a planted 200vw scroll container and must
 *     both DETECT it and NAME it, because for its whole life it could do
 *     neither on this product -- `clipped()` walked into `body`'s
 *     `overflow-x: auto` and filtered the entire page away, so a real 437px
 *     document at a 390px viewport was reported with an empty culprit list.
 *
 * A measurement harness that cannot see a known failure is decoration, and a
 * check that detects a defect and will not say where it is is barely better.
 *
 * THE VIEWPORT LIST INCLUDES 320. SC 1.4.10 is written against 320 CSS pixels
 * and the narrowest width here used to be 390, so the criterion's own number
 * was the one number never measured. See `VIEWPORTS`.
 *
 * WHAT IT STILL CANNOT SEE. One browser, one engine, no assistive technology.
 * It does not read a screen reader's output, judge whether a label says
 * anything useful, or notice that a control is in the wrong place. Findings
 * that need a person are in `docs/superpowers/audits/`.
 *
 * USAGE
 *   node scripts/a11y-browser.mjs                 # starts its own dev server
 *   A11Y_URL=http://127.0.0.1:5173/__preview node scripts/a11y-browser.mjs
 *   node scripts/a11y-browser.mjs --falsify       # self-check only
 *
 * REQUIREMENTS. `axe-core` is a devDependency. Playwright is NOT -- adding it
 * would make every `npm ci` in this repo download a browser, which is a cost
 * the operator should choose rather than inherit. The script resolves it from
 * `node_modules` or from a global install; if CI ever wants this, the step is
 * `npx playwright install --with-deps chromium`.
 */

import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const FRONTEND = resolve(HERE, "..");
const require = createRequire(import.meta.url);

const FALSIFY_ONLY = process.argv.includes("--falsify");

/* ------------------------------------------------------------------ colour */

/* The WCAG arithmetic itself lives INSIDE the page (see SAMPLE_IN_PAGE): every
 * ratio is computed next to the pixel it is about, because shipping a
 * full-page ImageData array out per scroll position turns a 40-second run into
 * a several-minute one. It is the same formula `contrast.test.ts` uses, and
 * the self-check at the top of `main()` is what proves the two agree -- it
 * reproduces auditor A's 3.49:1 to the second decimal. */

const hex = (c) => "#" + c.map((v) => Math.round(v).toString(16).padStart(2, "0")).join("");

/* ------------------------------------------------------- browser + server */

/* WHERE `npm i -g` ACTUALLY PUT IT, derived rather than guessed.
 *
 * npm's default global prefix is the directory two levels above the running
 * `node` binary, so `/x/bin/node` -> `/x/lib/node_modules`. The list below used
 * to be three hardcoded prefixes, and it worked everywhere the author tried and
 * NOWHERE ELSE: on a GitHub-hosted runner `actions/setup-node` installs Node
 * under `/opt/hostedtoolcache/node/<version>/x64`, which is none of them. The
 * a11y job therefore downloaded 184MB of Chromium successfully and then failed
 * to import the library that drives it — a failure that looks like "playwright
 * is not installed" while playwright is, in fact, installed.
 *
 * Deriving it needs no subprocess and is correct on any prefix, which is the
 * whole point: the next runner image to move Node would break a fourth
 * hardcoded path the same way. The three literals stay as a backstop for a
 * prefix that is genuinely elsewhere (a distro package, a custom `npm config
 * set prefix`). */
const GLOBAL_MODULES = join(dirname(dirname(process.execPath)), "lib/node_modules");

async function loadPlaywright() {
  for (const spec of [
    "playwright",
    join(GLOBAL_MODULES, "playwright/index.mjs"),
    "/opt/node22/lib/node_modules/playwright/index.mjs",
    "/usr/lib/node_modules/playwright/index.mjs",
    "/usr/local/lib/node_modules/playwright/index.mjs",
  ]) {
    try {
      const mod = await import(spec.startsWith("/") ? spec : require.resolve(spec, { paths: [FRONTEND] }));
      // A CommonJS entry (index.js, as a bare-name resolve may find) imports
      // as { default }; the named exports live there.
      return mod.chromium ? mod : mod.default;
    } catch {
      /* try the next one */
    }
  }
  throw new Error(
    "playwright is not installed, or is installed somewhere none of these resolve it:\n  " +
      [`require.resolve from ${FRONTEND}`, GLOBAL_MODULES, "/opt/node22/lib/node_modules", "/usr/lib/node_modules", "/usr/local/lib/node_modules"].join("\n  ") +
      "\nIt is deliberately not a devDependency of this package (see the header). Install it " +
      "where you are running this: `npm i -D playwright && npx playwright install chromium`.",
  );
}

/** A dev server of our own, unless the caller points us at one. */
async function startServer() {
  if (process.env.A11Y_URL) return { url: process.env.A11Y_URL, stop: async () => {} };
  // A RANDOM PORT CAN BE TAKEN on a shared CI host (main, 2026-09-29: `vite
  // exited with 1` a second into the run, with nothing else to say). With
  // --strictPort vite exits rather than drift to a port this script is not
  // pointing the browser at, so the answer is a different random port, and
  // vite's own words in the error when every attempt fails.
  const attempts = 5;
  let last;
  for (let i = 0; i < attempts; i++) {
    const port = 5199 + Math.floor(Math.random() * 300);
    try {
      return await startServerOn(port);
    } catch (err) {
      last = err;
    }
  }
  throw last;
}

function startServerOn(port) {
  const child = spawn("npx", ["vite", "--port", String(port), "--host", "127.0.0.1", "--strictPort"], {
    cwd: FRONTEND,
    stdio: ["ignore", "pipe", "pipe"],
  });
  const url = `http://127.0.0.1:${port}/__preview`;
  let said = "";
  const keep = (d) => {
    said = (said + String(d)).slice(-2000);
  };
  child.stderr.on("data", keep);
  return new Promise((ok, fail) => {
    const timer = setTimeout(() => {
      child.kill("SIGTERM");
      fail(new Error(`vite did not start on ${port} within 60s\n${said}`));
    }, 60_000);
    child.stdout.on("data", (d) => {
      keep(d);
      if (String(d).includes("ready in")) {
        clearTimeout(timer);
        ok({ url, stop: async () => child.kill("SIGTERM") });
      }
    });
    child.on("exit", (code) => {
      clearTimeout(timer);
      fail(new Error(`vite exited with ${code} on port ${port}\n${said}`));
    });
  });
}

/* ---------------------------------------------------------- the axe half */

const AXE_SOURCE = readFileSync(require.resolve("axe-core/axe.min.js", { paths: [FRONTEND] }), "utf8");

/** Rules whose absence from this page is a property of the harness, not the product. */
const AXE_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"];

async function runAxe(page) {
  await page.addScriptTag({ content: AXE_SOURCE });
  return page.evaluate(
    async (tags) =>
      await window.axe.run(document, {
        runOnly: { type: "tag", values: tags },
        resultTypes: ["violations"],
      }),
    AXE_TAGS,
  );
}

/* ------------------------------------------------- the composited-pixel half */

/**
 * Everything below runs INSIDE the page, because shipping a full-page
 * ImageData array across the CDP boundary for every scroll position takes
 * minutes; sampling in-page takes seconds.
 */
const SAMPLE_IN_PAGE = async ({ b64, meta }) => {
  const img = new Image();
  img.src = "data:image/png;base64," + b64;
  await img.decode();
  const canvas = document.createElement("canvas");
  canvas.width = img.naturalWidth;
  canvas.height = img.naturalHeight;
  const ctx = canvas.getContext("2d");
  ctx.drawImage(img, 0, 0);
  const W = canvas.width;
  const H = canvas.height;
  const data = ctx.getImageData(0, 0, W, H).data;
  const px = (x, y) => {
    const i = (y * W + x) * 4;
    return [data[i], data[i + 1], data[i + 2]];
  };
  const lum = ([r, g, b]) => {
    const ch = (v) => {
      const c = v / 255;
      return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
    };
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b);
  };
  const cr = (a, b) => {
    const x = lum(a);
    const y = lum(b);
    return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
  };
  const over = (fg, alpha, bg) => fg.map((v, i) => v * alpha + bg[i] * (1 - alpha));

  const out = [];
  for (const el of document.querySelectorAll("[data-a11y-idx]")) {
    const idx = Number(el.getAttribute("data-a11y-idx"));
    const m = meta[idx];
    if (!m) continue;

    // THE LINE BOXES OF THIS RUN'S OWN TEXT, not the element's layout box. A
    // tall flex parent's box is mostly not behind any glyph, and grading a
    // label against a colour no glyph touches is how a naive sweep invents
    // failures.
    const rects = [];
    for (const node of el.childNodes) {
      if (node.nodeType !== 3 || !node.nodeValue.trim()) continue;
      const range = document.createRange();
      range.selectNodeContents(node);
      for (const r of range.getClientRects()) if (r.width > 1 && r.height > 1) rects.push(r);
    }
    if (!rects.length) continue;

    // A ROUNDED BORDER BELONGS TO THE ELEMENT'S OWN BOX, so a hit test cannot
    // exclude it: at a 999px radius the painted edge cuts several pixels into
    // the corners, and those are a blend of the fill and whatever is behind.
    // Sampling them reported the 21px notification dot -- white on --primary,
    // a true 5.35:1 -- as 3.79:1 against a colour painted nowhere on the page.
    const cs = getComputedStyle(el);
    const box = el.getBoundingClientRect();
    const border = Math.max(
      parseFloat(cs.borderTopWidth) || 0,
      parseFloat(cs.borderLeftWidth) || 0,
    );
    const guard = border + 1.5;
    // CSS clamps a declared radius to half the box -- that is what makes a
    // 999px pill a pill rather than an error.
    const radius = Math.min(parseFloat(cs.borderTopLeftRadius) || 0, box.width / 2, box.height / 2);
    const ir = Math.max(0, radius - guard);
    const ix = box.x + guard;
    const iy = box.y + guard;
    const iw = box.width - 2 * guard;
    const ih = box.height - 2 * guard;
    //
    // A SAMPLE IS A PIXEL, NOT A POINT, and the half-pixel that distinction is
    // worth put the same false positive back on the board the day a 320px
    // viewport was added. `px(x, y)` reads the pixel occupying
    // [x, x+1) x [y, y+1), whose colour is the average over that square — so
    // the thing to ask is whether its CENTRE is inside the guard, not whether
    // its top-left corner is. On the 21px dot at 320px exactly one sample
    // cleared the rounded test by 0.16px on its corner and read `#4f82da`, a
    // 22% blend of the fill and its own white border, reporting 3.79:1 for a
    // 5.83:1 badge. Its centre misses by 0.52px.
    const inside = (px_, py) => {
      const x = px_ + 0.5;
      const y = py + 0.5;
      if (x < ix || y < iy || x > ix + iw || y > iy + ih) return false;
      if (ir <= 0) return true;
      const cx = Math.min(Math.max(x, ix + ir), ix + iw - ir);
      const cy = Math.min(Math.max(y, iy + ir), iy + ih - ir);
      return Math.hypot(x - cx, y - cy) <= ir;
    };

    let worst = Infinity;
    let worstBg = null;
    let samples = 0;
    for (const r of rects) {
      const x0 = Math.ceil(r.x);
      const x1 = Math.floor(r.x + r.width);
      const y0 = Math.ceil(r.y);
      const y1 = Math.floor(r.y + r.height);
      const sx = Math.max(1, Math.floor((x1 - x0) / 14));
      const sy = Math.max(1, Math.floor((y1 - y0) / 5));
      for (let y = y0; y <= y1; y += sy) {
        for (let x = x0; x <= x1; x += sx) {
          if (x < 0 || y < 0 || x >= W || y >= H) continue;
          if (!inside(x, y)) continue;
          // HIT TEST: is this pixel really this run's own painted ground, or
          // a neighbour's, or something clipped out of a scroll container?
          const hit = document.elementFromPoint(x, y);
          if (!hit) continue;
          if (hit !== el && !el.contains(hit)) continue;
          const bg = px(x, y);
          const fg = m.a >= 0.999 ? m.c : over(m.c, m.a, bg);
          const ratio = cr(fg, bg);
          samples += 1;
          if (ratio < worst) {
            worst = ratio;
            worstBg = bg;
          }
        }
      }
    }
    if (worstBg === null || samples < 2) continue;
    out.push({
      i: idx,
      ratio: worst,
      bg: worstBg,
      fg: m.a >= 0.999 ? m.c : over(m.c, m.a, worstBg),
    });
  }
  return out;
};

async function sweep(page, viewport) {
  // 1. Describe every text run BEFORE anything is blanked -- the computed
  //    paint is what we are about to hide.
  const items = await page.evaluate(() => {
    const out = [];
    let i = 0;
    for (const el of document.querySelectorAll("body *")) {
      const texts = [...el.childNodes].filter((n) => n.nodeType === 3 && n.nodeValue.trim());
      if (!texts.length) continue;
      const cs = getComputedStyle(el);
      if (cs.visibility === "hidden" || cs.display === "none") continue;
      const isSvg = el.namespaceURI === "http://www.w3.org/2000/svg";
      const paint = isSvg ? cs.fill : cs.color;
      const m = /rgba?\(([\d.]+),\s*([\d.]+),\s*([\d.]+)(?:,\s*([\d.]+))?\)/.exec(paint);
      if (!m) continue;
      let opacity = 1;
      let node = el;
      while (node && node !== document.documentElement) {
        opacity *= parseFloat(getComputedStyle(node).opacity || "1");
        node = node.parentElement;
      }
      const path = ((e) => {
        const parts = [];
        while (e && e.tagName !== "BODY") {
          const cls = (e.getAttribute && e.getAttribute("class")) || "";
          parts.unshift(e.tagName.toLowerCase() + (cls ? "." + cls.trim().split(/\s+/).join(".") : ""));
          e = e.parentElement;
        }
        return parts.slice(-3).join(" > ");
      })(el);
      el.setAttribute("data-a11y-idx", String(i));
      out.push({
        i,
        path,
        isSvg,
        // Content BEHIND a modal scrim is obscured, and WCAG does not ask
        // obscured content to meet 1.4.3 while it is. Without this flag every
        // open dialog reports the entire page as failing -- 72 runs at 1440px,
        // none of them a defect.
        inOverlay: !!el.closest(".modal, .palette, .drawer, .account-menu"),
        text: texts.map((t) => t.nodeValue).join("").trim().slice(0, 44),
        color: [Number(m[1]), Number(m[2]), Number(m[3])],
        colorAlpha: m[4] === undefined ? 1 : Number(m[4]),
        opacity,
        fontSize: parseFloat(cs.fontSize),
        weight: cs.fontWeight,
      });
      i += 1;
    }
    return out;
  });
  const meta = Object.fromEntries(items.map((it) => [it.i, { c: it.color, a: it.colorAlpha * it.opacity }]));

  // 2. Blank every glyph -- `color` for HTML, `fill` for SVG -- leaving the
  //    exact backplate. Nothing here changes layout, so the line boxes
  //    measured in step 3 are the ones the glyphs really occupy.
  await page.addStyleTag({
    content: `*,*::before,*::after{color:transparent!important;-webkit-text-fill-color:transparent!important;text-shadow:none!important;}
      svg text,svg tspan{fill:transparent!important;}
      /* HIT-TESTING ONLY, and it changes no pixel: a full-viewport scrim
       * returns itself from elementFromPoint for every point on the page,
       * which rejected all 211 text runs behind and inside an open dialog.
       * It still PAINTS, so backplates are measured exactly as rendered. */
      .overlay{pointer-events:none!important;}`,
  });
  await page.waitForTimeout(300);

  const results = new Map();
  const collect = async () => {
    const png = await page.screenshot({ type: "png" }); // VIEWPORT only: rect coords match 1:1
    const found = await page.evaluate(SAMPLE_IN_PAGE, { b64: png.toString("base64"), meta });
    for (const f of found) {
      const prev = results.get(f.i);
      if (!prev || f.ratio < prev.ratio) {
        results.set(f.i, { ...items[f.i], ratio: f.ratio, bg: hex(f.bg), fg: hex(f.fg) });
      }
    }
  };

  const docHeight = await page.evaluate(() => document.documentElement.scrollHeight);
  for (let y = 0; y < docHeight; y += Math.floor(viewport.height * 0.75)) {
    await page.evaluate((v) => window.scrollTo(0, v), y);
    await page.waitForTimeout(180);
    await collect();
  }
  // The rail scrolls independently of the document, so its lower half is
  // never in a document-scroll screenshot.
  if (await page.locator(".nav-scroll").count()) {
    const navHeight = await page.evaluate(() => document.querySelector(".nav-scroll")?.scrollHeight ?? 0);
    await page.evaluate(() => window.scrollTo(0, 0));
    for (let y = 0; y < navHeight; y += 280) {
      await page.evaluate((v) => {
        const nav = document.querySelector(".nav-scroll");
        if (nav) nav.scrollTop = v;
      }, y);
      await page.waitForTimeout(150);
      await collect();
    }
  }

  const measured = [...results.values()].map((f) => {
    // WCAG 1.4.3 relaxes to 3:1 only for 18.66px bold or 24px regular.
    const large = f.fontSize >= 24 || (f.fontSize >= 18.66 && Number(f.weight) >= 700);
    return { ...f, large, floor: large ? 3 : 4.5, pass: f.ratio >= (large ? 3 : 4.5) };
  });
  measured.sort((a, b) => a.ratio - b.ratio);
  return { measured, unsampled: items.filter((it) => !results.has(it.i)) };
}


/* ------------------------------------------------- the third wall, in a browser */

/**
 * DID AN UPLOADED DOCUMENT EXECUTE?
 *
 * `docs/modules/documentation/spec-attachments.md` §0.4 builds the document
 * module's XSS containment out of three walls instead of a second hostname,
 * and the console holds the third: a render map with one entry per
 * server-declared viewer kind, where HTML, SVG and XML are shown as SOURCE
 * TEXT and never as markup. The spec states the cost of being wrong -- "a
 * `Blob` of type `text/html` opened from the console origin executes in it" --
 * and `deploy/docker-compose.yml` puts MinIO on that same origin, so the blast
 * radius is every operator in the tenant.
 *
 * WHY THIS LIVES HERE AND NOT IN VITEST. `DocumentViewer.test.tsx` asks jsdom
 * whether the characters landed in `textContent` rather than in `innerHTML`,
 * and jsdom answers that exactly. What jsdom CANNOT answer is whether a real
 * parser, handed these bytes through this component, runs anything -- it has
 * no script execution to observe. That question needs a browser, and this is
 * the browser.
 *
 * `/__preview`'s document fixture is a live three-vector payload -- a
 * `<script>` element, an `<svg onload>` and an `<image onerror>` -- each
 * setting the same global. One assertion therefore covers all three.
 *
 * NON-VACUITY IS HALF THE CHECK. "Nothing executed" is also what an empty page
 * reports, so the probe additionally requires the payload's characters to be
 * ON the page as text. If the fixture is ever removed from the harness this
 * goes RED rather than quietly green, which is the failure mode every other
 * guard in this file was written to avoid.
 */
const XSS_IN_PAGE = () => {
  const pre = document.querySelector('[data-testid="document-source"]');
  const scope = pre ?? document.body;
  return {
    executed: Boolean(window.__ITMS_XSS_EXECUTED),
    // The characters, as the console rendered them.
    text: pre ? (pre.textContent ?? "") : null,
    // ELEMENTS, in the real parsed DOM. A `<pre>` holding a text node has
    // none of these; a `<pre>` that was handed `innerHTML` has all of them.
    scripts: scope.querySelectorAll("script").length,
    svgs: scope.querySelectorAll("svg").length,
    images: scope.querySelectorAll("img,image").length,
  };
};

/* ------------------------------------------------------ page overflow */

/**
 * SC 1.4.10 in one measurement: does the DOCUMENT scroll sideways, and if it
 * does, which box made it. Lifted out of the main loop so `--falsify` can
 * run it against a planted defect -- see the second self-check in `main()`.
 */
const OVERFLOW_IN_PAGE = () => {
  const de = document.documentElement;
  const client = de.clientWidth;

  // NAMING THE CULPRIT IS THE POINT. The first version of this check reported
  // the two numbers and nothing else, and finding which element was 314px too
  // wide took a bisection by hand -- `sr-only` tables losing a cascade-layer
  // fight, in the end. The widest few boxes crossing the right edge, deepest
  // first, is the answer that check should have printed.
  //
  // AND IT PRINTED NOTHING AT ALL, on a genuine 437px-wide document at a 390px
  // viewport -- the exact shape of overflow it exists to catch. TWO bugs,
  // either of which alone empties the list:
  //
  //   1. `clipped()` walked into `<body>`, which carries `overflow-x: auto`,
  //      so EVERY element on the page counted as "inside a scroll container".
  //      See the note on that function. This one was fatal on its own, and had
  //      been since the check was written: on this product the culprit list
  //      could never name anything, and never had.
  //   2. The parent-suppression line dropped any box that had ANY overflowing
  //      child, whether or not that child was itself reportable. A
  //      `.table-wrap` is `overflow-x: auto`, so its children are `clipped()`
  //      and skipped -- and the wrap was then suppressed BY those same skipped
  //      children. Every candidate cancelled a candidate.
  //
  // Suppression is now computed against the REPORTABLE set, not against every
  // child: a box is dropped only when something that would itself have been
  // printed sits inside it. That is the deepest-first answer the comment above
  // always claimed. `--falsify` plants that exact shape and requires this to
  // name it, because a check nobody can falsify is how a silent one survives a
  // release.

  // THE WALK STOPS AT <body>, AND THAT STOP IS BUG 1'S FIX. `index.css` sets
  // `body { overflow-x: auto }`. Overflow on the body PROPAGATES TO THE
  // VIEWPORT when the root element's own overflow is `visible` (CSS Overflow 3
  // §3.3): the body does not become a scroll container, the viewport does --
  // which is precisely the sideways page scroll being measured here. Treating
  // it as a clip meant the check filtered out the very thing it was looking
  // for.
  const clipped = (el) => {
    for (
      let p = el.parentElement;
      p && p !== document.body && p !== document.documentElement;
      p = p.parentElement
    ) {
      const ox = getComputedStyle(p).overflowX;
      if (ox === "auto" || ox === "scroll" || ox === "hidden") return true;
    }
    return false;
  };
  const describe = (el, r) =>
    `${el.tagName.toLowerCase()}.${(el.getAttribute("class") || "").trim().split(/\s+/)[0] || "-"}` +
    ` right=${Math.round(r.right)}`;

  const over = [];
  for (const el of document.querySelectorAll("body *")) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.right <= client + 1) continue;
    if (getComputedStyle(el).position === "fixed") continue;
    over.push({ el, r });
  }
  // A box inside a scroll container scrolls that container, not the page, so
  // it is not what widened the document.
  const candidates = over.filter(({ el }) => !clipped(el));
  const deepest = candidates.filter(
    ({ el }) => !candidates.some((other) => other.el !== el && el.contains(other.el)),
  );
  const rank = (list) =>
    [...list].sort((a, b) => b.r.right - a.r.right).map(({ el, r }) => describe(el, r));

  // THE FALLBACK IS NOT DECORATION. If the filters above eliminate everything
  // while the document really is too wide, the honest report is the widest
  // overflowing boxes with the filters OFF, labelled as such -- an unexplained
  // pair of numbers is what sent the last reader bisecting by hand. Silence is
  // never the answer to a defect that has already been detected.
  const guilty = deepest.length ? rank(deepest) : candidates.length ? rank(candidates) : [];
  return {
    scroll: de.scrollWidth,
    client,
    inner: window.innerWidth,
    guilty: guilty.slice(0, 6),
    unfiltered: guilty.length ? [] : rank(over).slice(0, 6),
  };
};
/* ------------------------------------------------------------------- run */

/**
 * 320 IS THE CRITERION'S OWN NUMBER, and until it was added this script never
 * exercised it. SC 1.4.10 Reflow is written against "a width equivalent to
 * 320 CSS pixels"; the narrowest viewport here was 390, so the width the
 * success criterion actually specifies was the one width never measured. A
 * hand-written probe found a real overflow at 320 that four passes of this
 * script had reported clean. 390 stays -- it is the common phone, and a
 * defect that appears only below it is a different defect from one that
 * appears at both.
 *
 * 320x568 is the iPhone SE's CSS viewport, which is where the number comes
 * from in the first place.
 */
const VIEWPORTS = [
  { width: 1440, height: 900, coarse: false },
  { width: 1024, height: 768, coarse: false },
  { width: 768, height: 1024, coarse: true },
  { width: 390, height: 844, coarse: true },
  { width: 320, height: 568, coarse: true },
];

/** States the default render never reaches, and where the interesting bugs live. */
const STATES = {
  default: async () => {},
  dialog: async (page) => {
    await page.getByRole("button", { name: /Delete device/ }).first().click();
    await page.waitForTimeout(400);
  },
  palette: async (page) => {
    await page.keyboard.press("/");
    await page.waitForTimeout(400);
  },
};

async function main() {
  const { chromium } = await loadPlaywright();
  const server = await startServer();
  const failures = [];
  const note = (line) => {
    failures.push(line);
    console.log(`FAIL  ${line}`);
  };

  const launch = {
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
    ...(process.env.PLAYWRIGHT_BROWSERS_PATH
      ? { executablePath: `${process.env.PLAYWRIGHT_BROWSERS_PATH}/chromium` }
      : {}),
  };
  let browser;
  try {
    browser = await chromium.launch(launch);
  } catch {
    // A pinned playwright disagreeing with the installed build is a launch
    // failure, not a finding -- fall back to whatever it ships with.
    browser = await chromium.launch({ args: launch.args });
  }

  try {
    // THE SELF-CHECK, FIRST AND ALWAYS. `#878590` on the table headers is the
    // exact defect auditor A measured from the live DOM at 3.49:1. If the
    // sweep cannot see it, nothing else this script reports means anything.
    {
      const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });
      const page = await ctx.newPage();
      await page.goto(server.url, { waitUntil: "networkidle" });
      await page.waitForTimeout(400);
      await page.addStyleTag({ content: "thead th{color:#878590 !important;}" });
      await page.waitForTimeout(200);
      const { measured } = await sweep(page, { width: 1440, height: 900 });
      const caught = measured.filter((f) => !f.pass && f.path.includes("th") && f.ratio < 3.6);
      console.log(
        `self-check: repainting the table headers at #878590 produced ${caught.length} failures ` +
          `at ${caught[0]?.ratio.toFixed(2) ?? "n/a"}:1`,
      );
      if (caught.length === 0) note("SELF-CHECK: the sweep could not see a known 3.49:1 defect");
      await ctx.close();
    }

    // THE SECOND SELF-CHECK, and it exists because the overflow half spent a
    // release reporting a genuine 437px-wide document with an EMPTY culprit
    // list. A check that detects a defect and then declines to say where it is
    // has failed at the only job it has; a check nobody can falsify is how
    // that survives. So: plant the exact shape that broke it -- a box wider
    // than the viewport whose only overflowing child is clipped by the box's
    // own `overflow-x: auto`, which is what a `.table-wrap` is -- and require
    // both halves of the answer, the detection AND the name.
    {
      const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 1 });
      const page = await ctx.newPage();
      await page.goto(server.url, { waitUntil: "networkidle" });
      await page.waitForTimeout(400);
      await page.evaluate(() => {
        const wrap = document.createElement("div");
        wrap.className = "a11y-falsify-overflow";
        wrap.style.cssText = "overflow-x:auto;width:200vw;height:6px";
        const child = document.createElement("div");
        child.style.cssText = "width:400vw;height:4px";
        wrap.append(child);
        document.body.append(wrap);
      });
      await page.waitForTimeout(200);
      const planted = await page.evaluate(OVERFLOW_IN_PAGE);
      const detected = planted.scroll > planted.client + 1;
      const named = [...planted.guilty, ...planted.unfiltered].join("; ");
      console.log(
        `self-check: a planted 200vw scroll container was ${detected ? "detected" : "MISSED"} ` +
          `(scrollWidth ${planted.scroll} vs clientWidth ${planted.client}) and named [${named}]`,
      );
      if (!detected) note("SELF-CHECK: the overflow probe could not see a 200vw box");
      if (!named.includes("a11y-falsify-overflow")) {
        note(`SELF-CHECK: the overflow probe saw the defect and did not name it -- got [${named}]`);
      }
      await ctx.close();
    }
    // THE THIRD SELF-CHECK. Take the characters the document viewer actually
    // rendered and hand those same characters to the HTML parser. If the probe
    // above cannot see the difference between "shown as text" and "parsed as
    // markup", then the check that follows it means nothing -- it would report
    // a clean page for a viewer that had been rewritten to use `innerHTML`.
    {
      const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });
      const page = await ctx.newPage();
      await page.goto(server.url, { waitUntil: "networkidle" });
      await page.waitForTimeout(600);
      const shown = await page.evaluate(XSS_IN_PAGE);
      if (shown.text === null) {
        note("SELF-CHECK: /__preview renders no document viewer, so the XSS probe has nothing to measure");
      } else {
        // PLANTED INTO THE `<pre>` ITSELF, which is precisely the defect
        // being falsified: someone rewrites the text branch to use
        // `innerHTML` (or `dangerouslySetInnerHTML`) and the same bytes stop
        // being characters. Planting into a fresh div elsewhere would prove
        // the payload is live and prove nothing about the probe's scope --
        // the first version of this did exactly that and reported "DID
        // execute" beside "0 svg elements".
        await page.evaluate(() => {
          const pre = document.querySelector('[data-testid="document-source"]');
          if (pre) pre.innerHTML = pre.textContent ?? "";
        });
        await page.waitForTimeout(600);
        const planted = await page.evaluate(XSS_IN_PAGE);
        console.log(
          `self-check: the viewer's own characters, parsed as markup, produced ` +
            `${planted.svgs} svg / ${planted.scripts} script element(s) and ` +
            `${planted.executed ? "DID" : "did NOT"} execute`,
        );
        // The ELEMENT count is the falsifier proper: it is what separates
        // "rendered as text" from "rendered as markup", and unlike script
        // execution it does not depend on which vectors a given browser
        // build happens to run.
        if (planted.svgs === 0) {
          note("SELF-CHECK: the XSS probe could not see markup built from the viewer's own bytes");
        }
      }
      await ctx.close();
    }

    if (FALSIFY_ONLY) {
      await browser.close();
      await server.stop();
      process.exit(failures.length ? 1 : 0);
    }

    // THE CHECK ITSELF. One page load, no planting: does the document viewer,
    // handed a live three-vector payload through the real component, execute
    // anything or build any element from it?
    {
      const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });
      const page = await ctx.newPage();
      await page.goto(server.url, { waitUntil: "networkidle" });
      await page.waitForTimeout(800);
      const seen = await page.evaluate(XSS_IN_PAGE);
      if (seen.text === null) {
        note("xss  /__preview renders no document viewer, so nothing was measured");
      } else if (!seen.text.includes("<script>") || !seen.text.includes("onerror")) {
        // NON-VACUITY. "Nothing executed" is also what a blank page reports.
        note(
          "xss  the document fixture no longer carries a payload, so a clean " +
            `result would mean nothing -- got ${JSON.stringify(seen.text.slice(0, 80))}`,
        );
      } else {
        console.log(
          `ok    xss  ${seen.text.length} characters of live payload rendered as text; ` +
            `${seen.scripts} script / ${seen.svgs} svg / ${seen.images} image element(s) built; ` +
            `executed=${seen.executed}`,
        );
        if (seen.executed) {
          note("xss  AN UPLOADED DOCUMENT EXECUTED IN THE CONSOLE ORIGIN -- wall 3 is breached");
        }
        if (seen.scripts || seen.svgs || seen.images) {
          note(
            `xss  the viewer built markup from an uploaded document ` +
              `(${seen.scripts} script / ${seen.svgs} svg / ${seen.images} image)`,
          );
        }
      }
      await ctx.close();
    }

    for (const vp of VIEWPORTS) {
      for (const [state, enter] of Object.entries(STATES)) {
        // The overlays are the same markup at every width; measure them once.
        if (state !== "default" && vp.width !== 1440) continue;
        const ctx = await browser.newContext({
          viewport: { width: vp.width, height: vp.height },
          deviceScaleFactor: 1,
          hasTouch: vp.coarse,
          isMobile: vp.coarse,
        });
        const page = await ctx.newPage();
        await page.goto(server.url, { waitUntil: "networkidle" });
        await page.waitForTimeout(500);
        await enter(page);
        const label = `${vp.width}x${vp.height}/${state}`;

        // --- axe ---
        const axe = await runAxe(page);
        for (const v of axe.violations) {
          for (const n of v.nodes) {
            note(`${label}  axe ${v.id} [${v.impact}]  ${n.target.join(" ")}  ${v.help}`);
          }
        }

        // --- composited pixels ---
        // Content behind a modal scrim is obscured, and WCAG does not ask it
        // to meet 1.4.3 while it is. Grading it would report the whole page
        // as failing every time a dialog opens.
        //
        // THE PALETTE STATE NO LONGER HAS A SCRIM -- the search is an inline
        // panel hanging off the top bar's field, not a dialog -- so nothing
        // behind it is obscured. The narrowing stays anyway, and deliberately:
        // the page underneath is graded in full by the `default` state one
        // pass earlier, and grading it twice would only mean reading the same
        // finding twice. What this line buys in every non-default state is
        // that the state's OWN surface is the thing being measured.
        const { measured, unsampled } = await sweep(page, vp);
        const scrimmed = state !== "default";
        for (const f of measured) {
          if (f.pass) continue;
          if (scrimmed && !f.inOverlay) continue;
          note(
            `${label}  contrast ${f.ratio.toFixed(2)}:1 (floor ${f.floor})  fg=${f.fg} bg=${f.bg} ` +
              `${f.fontSize}px/${f.weight}  ${f.path}  "${f.text}"`,
          );
        }
        console.log(
          `ok    ${label}  axe ${axe.violations.length} violations; ` +
            `${measured.length} text runs measured, ${unsampled.length} not visible`,
        );

        // --- a modal must RENDER as a modal ---
        // The class name was there and the rules were not: `.modal`,
        // `.modal.open` and `.modal-body` never came across from the design,
        // so the destructive confirmation laid out as a static full-width
        // block at y=613 with its own 38%-black blurred scrim painted on top
        // of it. Twelve jsdom tests passed on it throughout. Geometry is the
        // only thing that can tell those two apart, so geometry is asserted.
        if (state === "dialog") {
          const geo = await page.evaluate(() => {
            const modal = document.querySelector("section.modal.open");
            const scrim = document.querySelector(".overlay.open");
            if (!modal || !scrim) return null;
            const cs = getComputedStyle(modal);
            const ss = getComputedStyle(scrim);
            const r = modal.getBoundingClientRect();
            const hit = document.elementFromPoint(
              Math.round(r.left + r.width / 2),
              Math.round(r.top + 6),
            );
            return {
              position: cs.position,
              z: cs.zIndex,
              scrimZ: ss.zIndex,
              rect: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
              onScreen: r.top >= 0 && r.bottom <= window.innerHeight && r.width > 0,
              centred:
                Math.abs((r.left + r.right) / 2 - window.innerWidth / 2) <= 2 &&
                Math.abs((r.top + r.bottom) / 2 - window.innerHeight / 2) <= 2,
              aboveScrim: !!hit && modal.contains(hit),
            };
          });
          if (!geo) {
            note(`${label}  no .modal.open/.overlay.open pair to measure`);
          } else {
            const where = `position=${geo.position} z=${geo.z} scrim-z=${geo.scrimZ} rect=${geo.rect.join(",")}`;
            if (geo.position !== "fixed") note(`${label}  the dialog is not fixed: ${where}`);
            if (!geo.onScreen) note(`${label}  the dialog is not fully on screen: ${where}`);
            if (!geo.centred) note(`${label}  the dialog is not centred: ${where}`);
            if (!geo.aboveScrim) note(`${label}  the scrim paints OVER the dialog: ${where}`);
            console.log(`ok    ${label}  dialog geometry: ${where}`);
          }
        }

        // --- the rail must LEAVE the tab order when it is a drawer ---
        if (vp.width < 1024 && state === "default") {
          const reachable = await page.evaluate(() => {
            const rail = document.querySelector(".sidebar");
            if (!rail) return -1;
            if (getComputedStyle(rail).visibility === "hidden") return 0;
            return rail.querySelectorAll('a[href],button,[tabindex]:not([tabindex="-1"])').length;
          });
          if (reachable > 0) {
            note(`${label}  the closed drawer leaves ${reachable} controls in the tab order`);
          }
        }

        // --- nothing may scroll the PAGE sideways ---
        if (state === "default") {
          const overflow = await page.evaluate(OVERFLOW_IN_PAGE);
          // `innerWidth` IS REPORTED TOO, and it is not redundant. Under
          // Chromium's mobile emulation a document whose minimum content
          // width exceeds the device width is laid out WIDER and scaled to
          // fit rather than given a scrollbar, so at 390 the symptom was a
          // 469px layout viewport rather than a visible scrollbar. Same
          // defect, and the number that names it.
          if (overflow.scroll > overflow.client + 1) {
            const named = overflow.guilty.length
              ? `  [${overflow.guilty.join("; ")}]`
              : overflow.unfiltered.length
                ? `  [inside scroll containers, widest first: ${overflow.unfiltered.join("; ")}]`
                : "  [no element crosses the right edge -- look at a margin, a `width`" +
                  " on <html>/<body>, or a transform]";
            note(
              `${label}  the page scrolls horizontally: documentElement.scrollWidth ` +
                `${overflow.scroll} > clientWidth ${overflow.client} (innerWidth ${overflow.inner})` +
                named,
            );
          }
        }

        // --- every focusable must show something ---
        if (state === "default" && vp.width === 1440) {
          await page.evaluate(() => window.scrollTo(0, 0));
          for (let i = 0; i < 60; i += 1) {
            await page.keyboard.press("Tab");
            const stop = await page.evaluate(() => {
              const a = document.activeElement;
              if (!a || a === document.body) return null;
              const cs = getComputedStyle(a);
              return {
                what: `${a.tagName}.${(a.getAttribute("class") || "").split(" ")[0]}`,
                // An outline OR a ring drawn as a box-shadow. `.search-field`
                // takes the second path deliberately (`outline: none` plus a
                // 3px azure glow and an azure border, components.css), so
                // both count -- but neither present is nothing to see.
                ring:
                  (cs.outlineStyle !== "none" && parseFloat(cs.outlineWidth) > 0) ||
                  (cs.boxShadow !== "none" && /rgba?\(/.test(cs.boxShadow)),
              };
            });
            if (!stop) break;
            if (!stop.ring) note(`${label}  focusable with no visible indicator: ${stop.what}`);
          }
        }
        await ctx.close();
      }
    }

    // --- THE OVERLAYS MUST HOLD THE KEYBOARD AND HAND IT BACK ---
    //
    // Neither of these can be asked of jsdom in any useful way and both were
    // measured broken: four Tabs out of the open delete confirmation reached
    // the rail's "Dashboard" link with `aria-modal="true"` still asserted, and
    // Escape on either surface dropped focus on <body>, so the next Tab
    // restarted at the skip link thirty stops away.
    {
      const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });
      const page = await ctx.newPage();
      await page.addInitScript(() => {
        window.__where = () => {
          const a = document.activeElement;
          if (!a || a === document.body) return "BODY";
          return `${a.tagName}.${(a.getAttribute("class") || "").split(" ")[0]} "${(a.textContent || a.getAttribute("aria-label") || "").trim().slice(0, 28)}"`;
        };
      });
      await page.goto(server.url, { waitUntil: "networkidle" });
      await page.waitForTimeout(400);

      const surfaces = [
        {
          what: "the confirm dialog",
          selector: "section.modal.open",
          reach: async () => page.getByRole("button", { name: /Delete device/ }).first().focus(),
          key: "Enter",
        },
      ];

      for (const s of surfaces) {
        // The trigger is read AFTER focusing it and BEFORE the keypress that
        // opens the surface: that is the element focus has to come back to.
        await s.reach();
        const trigger = await page.evaluate(() => window.__where());
        await page.keyboard.press(s.key);
        await page.waitForTimeout(350);
        const opened = await page.evaluate((sel) => !!document.querySelector(sel), s.selector);
        if (!opened) {
          note(`keyboard  ${s.what} did not open from the keyboard`);
          continue;
        }

        let escaped = null;
        for (let i = 0; i < 14 && escaped === null; i += 1) {
          await page.keyboard.press("Tab");
          escaped = await page.evaluate(
            (sel) => {
              const panel = document.querySelector(sel);
              if (!panel) return "the surface closed while tabbing";
              return panel.contains(document.activeElement) ? null : window.__where();
            },
            s.selector,
          );
        }
        if (escaped) note(`keyboard  Tab walks out of ${s.what} onto ${escaped}`);
        else console.log(`ok    keyboard  14 Tabs stay inside ${s.what}`);

        // Escape has to work from wherever the trap left the focus, not only
        // from the one element a handler happened to be bound to.
        await page.keyboard.press("Escape");
        await page.waitForTimeout(350);
        const stillOpen = await page.evaluate((sel) => !!document.querySelector(sel), s.selector);
        if (stillOpen) note(`keyboard  Escape does not dismiss ${s.what}`);

        const returned = await page.evaluate(() => window.__where());
        if (returned !== trigger) {
          note(`keyboard  ${s.what} did not give focus back: opened from ${trigger}, closed onto ${returned}`);
        } else {
          console.log(`ok    keyboard  ${s.what} returns focus to ${returned}`);
        }
      }

      // --- THE INLINE SEARCH HOLDS NOTHING, AND STILL HANDS FOCUS BACK ---
      //
      // "The search bar is a pop-up, it shouldn't be like that." It no longer
      // is: `.palette` hangs off the top bar's field with no scrim and no
      // `aria-modal`, so the trap this used to assert on it would now be a
      // defect -- a popup the operator cannot tab out of. The assertion is
      // INVERTED rather than dropped, because "Tab leaves, and leaving closes
      // it" is a claim of exactly the same weight about the control that
      // exists, and jsdom can no more see it than it could see the trap.
      //
      // Focus return survives intact and is measured the same way. `/` pressed
      // from a page control is an interruption the operator did not ask for,
      // so Escape owes that control its focus back.
      {
        const field = "input.topbar-search";
        const panel = ".palette.open";
        const state = async () =>
          page.evaluate(
            ([f, pn]) => ({
              open: !!document.querySelector(pn),
              inPanel: !!document.querySelector(pn)?.contains(document.activeElement),
              onField: document.activeElement === document.querySelector(f),
              where: window.__where(),
            }),
            [field, panel],
          );

        await page.getByRole("button", { name: /Delete device/ }).first().focus();
        const trigger = await page.evaluate(() => window.__where());
        await page.keyboard.press("/");
        await page.waitForTimeout(350);

        let now = await state();
        if (!now.open) note("keyboard  the inline search did not open from /");
        else if (!now.onField) note(`keyboard  / opened the search but left focus on ${now.where}`);
        else console.log("ok    keyboard  / opens the inline search and lands in its field");

        if (now.open) {
          // Tab out. Six is more stops than the panel has; if the panel is
          // still holding focus after that, it is holding it on purpose.
          let left = false;
          for (let i = 0; i < 6 && !left; i += 1) {
            await page.keyboard.press("Tab");
            await page.waitForTimeout(120);
            now = await state();
            left = !now.open && !now.inPanel;
            if (now.open && !now.inPanel && !now.onField) {
              note(`keyboard  Tab left the inline search but the panel stayed open, on ${now.where}`);
              break;
            }
          }
          if (!left) note("keyboard  Tab cannot leave the inline search -- it is trapping focus");
          else console.log("ok    keyboard  Tab leaves the inline search and closes its panel");
        }

        // And again, this time backing out rather than tabbing through.
        await page.getByRole("button", { name: /Delete device/ }).first().focus();
        await page.keyboard.press("/");
        await page.waitForTimeout(350);
        await page.keyboard.press("Escape");
        await page.waitForTimeout(350);
        now = await state();
        if (now.open) note("keyboard  Escape does not dismiss the inline search");
        if (now.where !== trigger) {
          note(`keyboard  the inline search did not give focus back: opened from ${trigger}, closed onto ${now.where}`);
        } else {
          console.log(`ok    keyboard  the inline search returns focus to ${now.where}`);
        }
      }
      await ctx.close();
    }
  } finally {
    await browser?.close();
    await server.stop();
  }

  console.log(`\n${failures.length} finding(s).`);
  process.exit(failures.length ? 1 : 0);
}

await main();
