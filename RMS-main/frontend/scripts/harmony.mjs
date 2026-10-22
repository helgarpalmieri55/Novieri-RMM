/**
 * THE HARMONY GATE: every routed page, in a real browser, measured against
 * the design tokens (v3 port spec, `docs/superpowers/specs/2026-09-07-redesign-v3-port-design.md` §5).
 *
 * WHAT IT DOES. Starts Vite, opens every route in `scripts/routes.mjs` with
 * `?__fixtures` (so each page renders over `src/fixtures/` and no backend is
 * needed), and at 1440x900 and 390x844:
 *
 *   1. reads the computed `font-size`, `border-radius`, `color`,
 *      `background-color`, `border-color` (and `fill`/`stroke` on SVG) of
 *      every painted element and diffs them against the token allowlist;
 *   2. checks nothing scrolls the PAGE sideways (`documentElement.scrollWidth`),
 *      naming the box that widened it;
 *   3. runs axe (WCAG 2.x A/AA + best-practice);
 *   4. reads `window.__fixtureMisses` -- the `ApiClient` methods the page
 *      called that no fixture answered, so the page drew its error state;
 *   5. screenshots the full page to `frontend/.harmony/<lang>/<viewport>/<route>.png`,
 *      which is what the review wave reads against the prototype.
 *
 * THE ALLOWLIST IS READ FROM `src/index.css` WHEN THIS RUNS, never hardcoded
 * here: the tokens are transcribed from the design file and guarded by
 * `tokens.test.ts`, and a second copy in this script would be a second thing
 * to drift. Every `--*` custom property in that file is parsed (with `var()`
 * references resolved); the `--t-*`/`--text-*` values are the legal font
 * sizes, `--r-*`/`--radius-*` the legal radii, and every colour literal
 * anywhere in the file a legal colour. Three structural additions come from
 * the port brief rather than the CSS: `26px` (the KPI figure, the one size
 * outside the five-step scale), `2px`/`3px` (heat cells) and `50%` (avatars
 * and pips), plus `0` and full transparency, which are not choices.
 *
 * EXIT 1 on any stray value, any horizontal overflow, any serious or critical
 * axe violation, any route that fails to load -- and, with
 * `--strict-fixtures`, any fixture miss. Without that flag a miss is
 * reported and tolerated, which is the Phase A state: the gate runs and
 * names what each module still owes.
 *
 * USAGE
 *   node scripts/harmony.mjs                          # every route, both viewports
 *   node scripts/harmony.mjs --routes /devices,/dashboard
 *   node scripts/harmony.mjs --strict-fixtures        # CI: a miss is a failure
 *   node scripts/harmony.mjs --proto                  # screenshot the v3 prototype instead
 *   node scripts/harmony.mjs --no-shots               # measure only
 *   node scripts/harmony.mjs --lang es                # the console in Spanish (default en)
 *   HARMONY_URL=http://127.0.0.1:5173 node scripts/harmony.mjs   # your own dev server
 *
 * REQUIREMENTS. `axe-core` is a devDependency. Playwright is not (see
 * `a11y-browser.mjs`'s header for why); it is resolved from `node_modules` or
 * a global install, the same way that script does it.
 */

import { spawn } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { createServer as createHttpServer } from "node:http";
import { createRequire } from "node:module";
import { createServer as createNetServer } from "node:net";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { ROUTES, patternOf } from "./routes.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const FRONTEND = resolve(HERE, "..");
const REPO = resolve(FRONTEND, "..");
const HARMONY_ROOT = resolve(FRONTEND, ".harmony");
// Each language writes its own report and screenshots (set in main()).
let OUT = HARMONY_ROOT;
const LANGS = ["en", "es"];
const INDEX_CSS = resolve(FRONTEND, "src/index.css");
const PROTOTYPE = resolve(REPO, "docs/design/itms-prototype-v3.html");
const require = createRequire(import.meta.url);

/* ------------------------------------------------------------------ args */

function parseArgs(argv) {
  const args = { routes: null, strictFixtures: false, proto: false, shots: true, lang: "en" };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--strict-fixtures") args.strictFixtures = true;
    else if (arg === "--proto") args.proto = true;
    else if (arg === "--no-shots") args.shots = false;
    else if (arg === "--lang" || arg.startsWith("--lang=")) {
      args.lang = arg === "--lang" ? String(argv[i + 1] ?? "") : arg.slice("--lang=".length);
      if (arg === "--lang") i += 1;
      if (!LANGS.includes(args.lang)) throw new Error(`harmony: --lang must be one of ${LANGS.join(", ")}`);
    }
    else if (arg === "--routes") {
      args.routes = String(argv[i + 1] ?? "")
        .split(",")
        .map((r) => r.trim())
        .filter(Boolean);
      i += 1;
    } else if (arg.startsWith("--routes=")) {
      args.routes = arg
        .slice("--routes=".length)
        .split(",")
        .map((r) => r.trim())
        .filter(Boolean);
    } else {
      throw new Error(`harmony: unknown argument ${arg}`);
    }
  }
  return args;
}

/* ------------------------------------------------------------- viewports */

/** The two the spec names: the desktop the design was drawn at, and the phone. */
const VIEWPORTS = [
  { width: 1440, height: 900, coarse: false },
  { width: 390, height: 844, coarse: true },
];

/* ------------------------------------------------------------- allowlist */

/** `#rgb`, `#rrggbb`, `#rrggbbaa`, `rgb()`, `rgba()`, `color(srgb ...)` -> [r, g, b, a] or null. */
export function parseColor(text) {
  const s = String(text).trim().toLowerCase();
  if (s === "transparent") return [0, 0, 0, 0];
  let m = s.match(/^#([0-9a-f]{3,8})$/);
  if (m) {
    const h = m[1];
    if (h.length === 3 || h.length === 4) {
      const [r, g, b, a] = h.split("").map((c) => parseInt(c + c, 16));
      return [r, g, b, h.length === 4 ? a / 255 : 1];
    }
    if (h.length === 6 || h.length === 8) {
      const r = parseInt(h.slice(0, 2), 16);
      const g = parseInt(h.slice(2, 4), 16);
      const b = parseInt(h.slice(4, 6), 16);
      const a = h.length === 8 ? parseInt(h.slice(6, 8), 16) / 255 : 1;
      return [r, g, b, a];
    }
    return null;
  }
  m = s.match(/^rgba?\(([^)]*)\)$/);
  if (m) {
    const parts = m[1].replace(/\s*\/\s*/, " ").split(/[\s,]+/).filter(Boolean);
    if (parts.length < 3) return null;
    const channel = (p) => (p.endsWith("%") ? (parseFloat(p) / 100) * 255 : parseFloat(p));
    const alpha = (p) => (p === undefined ? 1 : p.endsWith("%") ? parseFloat(p) / 100 : parseFloat(p));
    return [channel(parts[0]), channel(parts[1]), channel(parts[2]), alpha(parts[3])];
  }
  m = s.match(/^color\(srgb\s+([^)]*)\)$/);
  if (m) {
    const parts = m[1].replace(/\s*\/\s*/, " ").split(/\s+/).filter(Boolean);
    if (parts.length < 3) return null;
    const a = parts[3] === undefined ? 1 : parseFloat(parts[3]);
    return [parseFloat(parts[0]) * 255, parseFloat(parts[1]) * 255, parseFloat(parts[2]) * 255, a];
  }
  return null;
}

const sameColor = (a, b) =>
  Math.abs(a[0] - b[0]) <= 1.5 &&
  Math.abs(a[1] - b[1]) <= 1.5 &&
  Math.abs(a[2] - b[2]) <= 1.5 &&
  Math.abs(a[3] - b[3]) <= 0.011;

/**
 * Every custom property declared in `index.css`, `var()` references resolved.
 * Comments are stripped first so a token mentioned in prose is not a token.
 */
export function readTokens(css) {
  const stripped = css.replace(/\/\*[\s\S]*?\*\//g, "");
  const raw = new Map();
  for (const m of stripped.matchAll(/(--[a-zA-Z0-9-]+)\s*:\s*([^;{}]+);/g)) {
    raw.set(m[1], m[2].trim());
  }
  const resolve1 = (value, depth = 0) => {
    if (depth > 10) return value;
    return value.replace(/var\((--[a-zA-Z0-9-]+)(?:\s*,\s*([^)]*))?\)/g, (_, name, fallback) => {
      const found = raw.get(name);
      return found !== undefined ? resolve1(found, depth + 1) : (fallback ?? "").trim();
    });
  };
  const tokens = new Map();
  for (const [name, value] of raw) tokens.set(name, resolve1(value));
  return tokens;
}

const COLOR_LITERAL = /#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)|color\(srgb[^)]*\)/g;

/** The allowlist, from the tokens: font sizes, radii, colours. */
export function buildAllowlist(css) {
  const tokens = readTokens(css);
  const px = (v) => /^\d+(\.\d+)?px$/.test(v);
  const fontSizes = new Set(["26px"]);
  const radii = new Set(["0px", "2px", "3px", "50%"]);
  const colors = [[0, 0, 0, 0]];
  const colorNames = new Map();
  for (const [name, value] of tokens) {
    if ((name.startsWith("--t-") || name.startsWith("--text-")) && px(value)) fontSizes.add(value);
    if ((name.startsWith("--r-") || name.startsWith("--radius")) && px(value)) radii.add(value);
    for (const literal of value.match(COLOR_LITERAL) ?? []) {
      const parsed = parseColor(literal);
      if (!parsed) continue;
      if (!colors.some((c) => sameColor(c, parsed))) colors.push(parsed);
      if (!colorNames.has(literal.toLowerCase())) colorNames.set(literal.toLowerCase(), name);
    }
  }
  return { tokens, fontSizes, radii, colors, colorNames };
}

/** Is `value` (a computed style string) on the allowlist for `prop`? */
function allowed(allow, prop, value) {
  if (prop === "font-size") return allow.fontSizes.has(value);
  if (prop === "border-radius") {
    return value
      .split(/\s+|\s*\/\s*/)
      .filter(Boolean)
      .every((part) => allow.radii.has(part));
  }
  const parsed = parseColor(value);
  if (!parsed) return false;
  return allow.colors.some((c) => sameColor(c, parsed));
}

/* ------------------------------------------------------------ in-page: styles */

/**
 * Runs INSIDE the page. Returns one record per (element, property) that
 * paints: font-size and color on text runs and form controls, background
 * where it is not transparent, border colour per side that has a border,
 * radius where the box paints something, fill/stroke on SVG where set.
 */
const COLLECT_IN_PAGE = () => {
  const out = [];
  const describe = (el) => {
    const bits = [];
    let node = el;
    for (let depth = 0; node && node !== document.body && depth < 3; depth += 1) {
      const cls = (node.getAttribute("class") || "").trim().split(/\s+/).filter(Boolean).slice(0, 2).join(".");
      const id = node.id ? `#${node.id}` : "";
      bits.unshift(`${node.tagName.toLowerCase()}${id}${cls ? "." + cls : ""}`);
      node = node.parentElement;
    }
    return bits.join(" > ");
  };
  const hasText = (el) => {
    for (const child of el.childNodes) {
      if (child.nodeType === Node.TEXT_NODE && child.textContent.trim() !== "") return true;
    }
    const tag = el.tagName.toLowerCase();
    if (tag === "input" || tag === "textarea" || tag === "select" || tag === "option") return true;
    return false;
  };
  const push = (el, prop, value) => out.push({ prop, value, path: describe(el) });

  for (const el of document.body.querySelectorAll("*")) {
    const tag = el.tagName.toLowerCase();
    if (tag === "script" || tag === "style" || tag === "template" || tag === "noscript") continue;
    if (el.closest("[data-harmony-ignore]")) continue;
    const rects = el.getClientRects();
    if (rects.length === 0) continue;
    const r = el.getBoundingClientRect();
    if (r.width <= 1 && r.height <= 1) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === "hidden" || cs.display === "none" || cs.opacity === "0") continue;
    const isSvg = el instanceof SVGElement;

    if (!isSvg && hasText(el)) {
      push(el, "font-size", cs.fontSize);
      push(el, "color", cs.color);
    }

    const bg = cs.backgroundColor;
    if (bg && bg !== "rgba(0, 0, 0, 0)" && bg !== "transparent") push(el, "background-color", bg);

    let bordered = false;
    for (const side of ["top", "right", "bottom", "left"]) {
      const width = parseFloat(cs.getPropertyValue(`border-${side}-width`));
      const style = cs.getPropertyValue(`border-${side}-style`);
      if (width > 0 && style !== "none" && style !== "hidden") {
        bordered = true;
        push(el, "border-color", cs.getPropertyValue(`border-${side}-color`));
      }
    }

    const radius = ["top-left", "top-right", "bottom-right", "bottom-left"]
      .map((corner) => cs.getPropertyValue(`border-${corner}-radius`))
      .join(" ");
    const paints =
      bordered ||
      (bg && bg !== "rgba(0, 0, 0, 0)") ||
      cs.boxShadow !== "none" ||
      cs.overflow !== "visible" ||
      cs.outlineStyle !== "none" ||
      tag === "img" ||
      tag === "video" ||
      tag === "canvas";
    if (paints && radius !== "0px 0px 0px 0px") push(el, "border-radius", radius);

    if (isSvg) {
      for (const prop of ["fill", "stroke"]) {
        const value = cs.getPropertyValue(prop);
        if (!value || value === "none") continue;
        // A PAINT SERVER IS NOT A COLOUR, AND IS NOT A REASON TO STOP
        // MEASURING. `fill: url("#hatch-1")` names a <pattern> or a gradient;
        // comparing that string against a palette can only ever report a
        // stray, and the colours that actually paint are the ones DECLARED
        // INSIDE the referenced element -- which lives in <defs>, is never
        // laid out, and so was never reached by the walk above. Follow the
        // reference and measure what is in there instead: the charts' hatch
        // stroke was a hardcoded `rgba(255,255,255,.55)` hiding behind a
        // `url()` for exactly this reason, and is now the `--hatch-ink`
        // token. Skipping `url()` outright would have kept that hidden.
        const ref = value.match(/^url\(["']?#([^"')]+)["']?\)/);
        if (ref) {
          const server = document.getElementById(ref[1]);
          if (!server) {
            push(el, prop, value);
            continue;
          }
          for (const inner of [server, ...server.querySelectorAll("*")]) {
            const ics = getComputedStyle(inner);
            // `stop-color` ONLY ON A `<stop>`. Its initial value is BLACK and
            // it is computed on every element whether or not it means
            // anything there, so reading it off a `<pattern>` and its
            // `<path>` reported 25 black stops that do not exist. `fill` and
            // `stroke` are different: they PAINT wherever they are set.
            const props =
              inner.tagName.toLowerCase() === "stop" ? ["stop-color"] : ["fill", "stroke"];
            for (const inked of props) {
              const paint = ics.getPropertyValue(inked);
              if (paint && paint !== "none" && !paint.startsWith("url(")) push(inner, inked, paint);
            }
          }
          continue;
        }
        push(el, prop, value);
      }
    }
  }
  return out;
};

/* ---------------------------------------------------------- in-page: overflow */

/**
 * The overflow probe from `a11y-browser.mjs`, verbatim in method: the widest
 * boxes past the right edge that are NOT inside a scroll container of their
 * own, deepest first, with the unfiltered list as a fallback so a detected
 * overflow is never reported without a culprit.
 */
const OVERFLOW_IN_PAGE = () => {
  const de = document.documentElement;
  const client = de.clientWidth;
  const clipped = (el) => {
    for (let p = el.parentElement; p && p !== document.body && p !== de; p = p.parentElement) {
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
  const candidates = over.filter(({ el }) => !clipped(el));
  const deepest = candidates.filter(
    ({ el }) => !candidates.some((other) => other.el !== el && el.contains(other.el)),
  );
  const rank = (list) => [...list].sort((a, b) => b.r.right - a.r.right).map(({ el, r }) => describe(el, r));
  const guilty = deepest.length ? rank(deepest) : candidates.length ? rank(candidates) : [];
  return {
    scroll: de.scrollWidth,
    client,
    inner: window.innerWidth,
    guilty: guilty.slice(0, 6),
    unfiltered: guilty.length ? [] : rank(over).slice(0, 6),
  };
};

/* --------------------------------------------------------------------- axe */

const AXE_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"];

function axeSource() {
  return readFileSync(require.resolve("axe-core/axe.min.js", { paths: [FRONTEND] }), "utf8");
}

async function runAxe(page, source) {
  await page.addScriptTag({ content: source });
  return page.evaluate(
    async (tags) =>
      await window.axe.run(document, {
        runOnly: { type: "tag", values: tags },
        resultTypes: ["violations"],
      }),
    AXE_TAGS,
  );
}

/* ------------------------------------------------------- browser + servers */

/* Same derivation as `a11y-browser.mjs`: npm's global prefix is two levels
 * above the running `node`, with the literal prefixes as a backstop. */
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
      "\nIt is deliberately not a devDependency (see scripts/a11y-browser.mjs). Install it where you " +
      "are running this: `npm i -g playwright && npx playwright install chromium`.",
  );
}

/** A port nothing is listening on, from the kernel rather than a guess. */
function freePort() {
  return new Promise((ok, fail) => {
    const server = createNetServer();
    server.unref();
    server.on("error", fail);
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address();
      server.close(() => ok(port));
    });
  });
}

/** A dev server of our own, unless `HARMONY_URL` points at one. */
async function startVite() {
  if (process.env.HARMONY_URL) {
    return { url: process.env.HARMONY_URL.replace(/\/$/, ""), stop: async () => {} };
  }
  const port = await freePort();
  const child = spawn("npx", ["vite", "--port", String(port), "--host", "127.0.0.1", "--strictPort"], {
    cwd: FRONTEND,
    stdio: ["ignore", "pipe", "pipe"],
  });
  let log = "";
  await new Promise((ok, fail) => {
    const timer = setTimeout(() => fail(new Error(`vite did not start on ${port} within 60s\n${log}`)), 60_000);
    child.stdout.on("data", (d) => {
      log += String(d);
      if (String(d).includes("ready in")) {
        clearTimeout(timer);
        ok();
      }
    });
    child.stderr.on("data", (d) => {
      log += String(d);
    });
    child.on("exit", (code) => {
      clearTimeout(timer);
      fail(new Error(`vite exited with ${code}\n${log}`));
    });
  });
  return { url: `http://127.0.0.1:${port}`, stop: async () => child.kill("SIGTERM") };
}

/** The prototype, served from disk so the browser sees a real origin. */
async function startPrototypeServer() {
  const html = readFileSync(PROTOTYPE);
  const port = await freePort();
  const server = createHttpServer((_req, res) => {
    res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
    res.end(html);
  });
  await new Promise((ok) => server.listen(port, "127.0.0.1", ok));
  return { url: `http://127.0.0.1:${port}/itms-prototype-v3.html`, stop: async () => server.close() };
}

async function launchBrowser(chromium) {
  const args = ["--no-sandbox", "--disable-dev-shm-usage"];
  try {
    return await chromium.launch({
      args,
      ...(process.env.PLAYWRIGHT_BROWSERS_PATH
        ? { executablePath: `${process.env.PLAYWRIGHT_BROWSERS_PATH}/chromium` }
        : {}),
    });
  } catch {
    return chromium.launch({ args });
  }
}

/* --------------------------------------------------------------- helpers */

// A query variant (`/settings?tab=ai-connections`) gets a file name of its own:
// `?`, `=` and `&` become `--`, `-` and `--`, so it never overwrites the plain route's shot.
const slug = (route) =>
  route.replace(/^\//, "").replace(/\//g, "__").replace(/\?/, "--").replace(/=/g, "-").replace(/&/g, "--") || "root";
/** The route with harmony's fixture flag added, after `?` or, on a query variant, `&`. */
const withFixtures = (route) => `${route}${route.includes("?") ? "&" : "?"}__fixtures`;
const vpLabel = (vp) => `${vp.width}x${vp.height}`;

function selectRoutes(requested) {
  if (!requested) return ROUTES;
  const unknown = requested.filter((r) => !ROUTES.includes(r));
  if (unknown.length) {
    throw new Error(`harmony: --routes names routes not in scripts/routes.mjs: ${unknown.join(", ")}`);
  }
  return requested;
}

function pad(text, width) {
  const s = String(text);
  return s.length >= width ? s : s + " ".repeat(width - s.length);
}

/* ------------------------------------------------------------- the walk */

async function walkApp(args, browser, server, allow) {
  const axe = axeSource();
  const routes = selectRoutes(args.routes);
  const results = [];

  for (const route of routes) {
    for (const vp of VIEWPORTS) {
      const result = {
        route,
        viewport: vpLabel(vp),
        loaded: false,
        loadError: null,
        strays: [],
        overflow: null,
        axe: [],
        misses: [],
        errors: [],
        shot: null,
      };
      results.push(result);

      const ctx = await browser.newContext({
        viewport: { width: vp.width, height: vp.height },
        deviceScaleFactor: 1,
        hasTouch: vp.coarse,
        isMobile: vp.coarse,
      });
      // The fixture /me carries no saved language, so the console takes this
      // browser's remembered choice -- the same path a person's first paint
      // takes (spec §1.1, step 2).
      await ctx.addInitScript((lang) => {
        try {
          window.localStorage.setItem("itms.lang", lang);
        } catch {
          /* storage blocked: the page falls back to English and the run says so below */
        }
      }, args.lang);
      const page = await ctx.newPage();
      page.on("pageerror", (err) => result.errors.push(`pageerror: ${err.message}`));
      page.on("console", (msg) => {
        if (msg.type() === "error") result.errors.push(`console: ${msg.text().slice(0, 200)}`);
      });

      try {
        await page.goto(`${server.url}${withFixtures(route)}`, { waitUntil: "networkidle", timeout: 60_000 });
        // React Query settles synchronously against the fixture proxy, but the
        // render behind it is a frame away; give the page a beat.
        await page.waitForTimeout(600);
        await page.evaluate(() => window.scrollTo(0, 0));
        // NON-VACUITY: a Spanish walk that rendered English would measure
        // nothing it claims to. <html lang> follows the active language.
        const lang = await page.evaluate(() => document.documentElement.lang);
        if (lang !== args.lang) throw new Error(`rendered in "${lang}", not "${args.lang}"`);
        result.loaded = true;
      } catch (err) {
        result.loadError = err.message.split("\n")[0];
        await ctx.close();
        continue;
      }

      // 1. token diff
      const records = await page.evaluate(COLLECT_IN_PAGE);
      const seen = new Map();
      for (const rec of records) {
        if (allowed(allow, rec.prop, rec.value)) continue;
        const key = `${rec.prop}|${rec.value}|${rec.path}`;
        const entry = seen.get(key);
        if (entry) entry.count += 1;
        else seen.set(key, { ...rec, count: 1 });
      }
      result.strays = [...seen.values()];

      // 2. overflow
      const overflow = await page.evaluate(OVERFLOW_IN_PAGE);
      if (overflow.scroll > overflow.client + 1) result.overflow = overflow;

      // 3. axe
      const report = await runAxe(page, axe);
      for (const v of report.violations) {
        for (const n of v.nodes) {
          result.axe.push({ id: v.id, impact: v.impact, target: n.target.join(" "), help: v.help });
        }
      }

      // 4. fixture misses
      result.misses = await page.evaluate(() => window.__fixtureMisses ?? []);

      // 5. screenshot
      if (args.shots) {
        const dir = resolve(OUT, vpLabel(vp));
        mkdirSync(dir, { recursive: true });
        const file = resolve(dir, `${slug(route)}.png`);
        await page.screenshot({ path: file, fullPage: true });
        result.shot = file;
      }

      await ctx.close();
    }
  }
  return results;
}

/**
 * `--proto`: the same routes, from `docs/design/itms-prototype-v3.html`, so a
 * reviewer has the design and the port side by side under the same name.
 * The prototype keys its pages by pattern (`devices/:deviceId`), so each
 * route is mapped back to its pattern; a page the prototype never drew falls
 * back to its dashboard and is reported as absent rather than screenshotted
 * under a false name.
 */
async function walkPrototype(args, browser, server) {
  const routes = selectRoutes(args.routes);
  const results = [];
  for (const route of routes) {
    // The prototype has one page per path: a query variant is its plain route there.
    const key = patternOf(route.split("?")[0]).replace(/^\//, "");
    for (const vp of VIEWPORTS) {
      const result = { route, viewport: vpLabel(vp), key, present: false, shot: null, loadError: null };
      results.push(result);
      const ctx = await browser.newContext({
        viewport: { width: vp.width, height: vp.height },
        deviceScaleFactor: 1,
        hasTouch: vp.coarse,
        isMobile: vp.coarse,
      });
      const page = await ctx.newPage();
      try {
        await page.goto(`${server.url}#/${key}`, { waitUntil: "load", timeout: 30_000 });
        await page.waitForFunction(() => Boolean(document.body.dataset.route), null, { timeout: 10_000 });
        await page.waitForTimeout(300);
        const drawn = await page.evaluate(() => document.body.dataset.route);
        result.present = drawn === key;
        if (result.present && args.shots) {
          const dir = resolve(OUT, "proto", vpLabel(vp));
          mkdirSync(dir, { recursive: true });
          result.shot = resolve(dir, `${slug(route)}.png`);
          await page.screenshot({ path: result.shot, fullPage: true });
        }
      } catch (err) {
        result.loadError = err.message.split("\n")[0];
      }
      await ctx.close();
    }
  }
  return results;
}

/* --------------------------------------------------------------- report */

const serious = (v) => v.impact === "serious" || v.impact === "critical";

function printAppTable(results, args) {
  console.log("");
  console.log(
    `${pad("route", 36)} ${pad("viewport", 9)} ${pad("strays", 7)} ${pad("overflow", 9)} ${pad("axe s/c", 8)} ${pad("misses", 7)} errors`,
  );
  console.log("-".repeat(96));
  for (const r of results) {
    if (!r.loaded) {
      console.log(`${pad(r.route, 36)} ${pad(r.viewport, 9)} FAILED TO LOAD  ${r.loadError}`);
      continue;
    }
    const strayCount = r.strays.reduce((n, s) => n + s.count, 0);
    const over = r.overflow ? `${r.overflow.scroll}>${r.overflow.client}` : "ok";
    const axeSerious = r.axe.filter(serious).length;
    const axeAll = r.axe.length;
    console.log(
      `${pad(r.route, 36)} ${pad(r.viewport, 9)} ${pad(strayCount, 7)} ${pad(over, 9)} ${pad(`${axeSerious}/${axeAll}`, 8)} ${pad(r.misses.length, 7)} ${r.errors.length}`,
    );
  }
  console.log("");

  for (const r of results) {
    if (!r.loaded) continue;
    const lines = [];
    const byProp = new Map();
    for (const s of r.strays) {
      const list = byProp.get(s.prop) ?? [];
      list.push(s);
      byProp.set(s.prop, list);
    }
    for (const [prop, list] of byProp) {
      const values = new Map();
      for (const s of list) values.set(s.value, (values.get(s.value) ?? 0) + s.count);
      const top = [...values.entries()].sort((a, b) => b[1] - a[1]).slice(0, 6);
      lines.push(
        `stray ${prop}: ${top.map(([v, n]) => `${v} x${n}`).join(", ")}${values.size > 6 ? ` (+${values.size - 6} more values)` : ""}`,
      );
      for (const s of list.slice(0, 3)) lines.push(`      e.g. ${s.value}  ${s.path}`);
    }
    if (r.overflow) {
      const named = r.overflow.guilty.length
        ? r.overflow.guilty.join("; ")
        : r.overflow.unfiltered.length
          ? `inside scroll containers, widest first: ${r.overflow.unfiltered.join("; ")}`
          : "no element crosses the right edge; look at a margin, a width on <html>/<body>, or a transform";
      lines.push(
        `overflow: scrollWidth ${r.overflow.scroll} > clientWidth ${r.overflow.client} (innerWidth ${r.overflow.inner})  [${named}]`,
      );
    }
    for (const v of r.axe) lines.push(`axe ${v.id} [${v.impact}]  ${v.target}  ${v.help}`);
    if (r.misses.length) lines.push(`misses: ${r.misses.join(", ")}`);
    for (const e of r.errors.slice(0, 5)) lines.push(`error: ${e}`);
    if (r.errors.length > 5) lines.push(`      (+${r.errors.length - 5} more errors)`);
    if (lines.length) {
      console.log(`${r.route} @ ${r.viewport}`);
      for (const line of lines) console.log(`   ${line}`);
    }
  }
  if (args.strictFixtures) console.log("--strict-fixtures: a fixture miss is a failure");
}

function writeAppReport(results, allow, args) {
  mkdirSync(OUT, { recursive: true });
  const json = {
    generated_at: new Date().toISOString(),
    lang: args.lang,
    strict_fixtures: args.strictFixtures,
    allowlist: {
      font_sizes: [...allow.fontSizes],
      radii: [...allow.radii],
      colors: allow.colors.map((c) => `rgba(${Math.round(c[0])}, ${Math.round(c[1])}, ${Math.round(c[2])}, ${c[3]})`),
    },
    results,
  };
  writeFileSync(resolve(OUT, "report.json"), JSON.stringify(json, null, 2));

  const md = [];
  md.push(`# Harmony report (${args.lang})`, "", `Generated ${json.generated_at}.`, "");
  md.push("| route | viewport | strays | overflow | axe serious/all | misses | errors | screenshot |");
  md.push("|---|---|---|---|---|---|---|---|");
  for (const r of results) {
    if (!r.loaded) {
      md.push(`| ${r.route} | ${r.viewport} | failed to load: ${r.loadError} | | | | | |`);
      continue;
    }
    const strayCount = r.strays.reduce((n, s) => n + s.count, 0);
    const over = r.overflow ? `${r.overflow.scroll} > ${r.overflow.client}` : "ok";
    const shot = r.shot ? r.shot.slice(OUT.length + 1) : "";
    md.push(
      `| ${r.route} | ${r.viewport} | ${strayCount} | ${over} | ${r.axe.filter(serious).length}/${r.axe.length} | ${r.misses.length} | ${r.errors.length} | ${shot} |`,
    );
  }
  md.push("");
  for (const r of results) {
    if (!r.loaded) continue;
    if (!r.strays.length && !r.overflow && !r.axe.length && !r.misses.length && !r.errors.length) continue;
    md.push(`## ${r.route} @ ${r.viewport}`, "");
    for (const s of r.strays) md.push(`- stray ${s.prop} \`${s.value}\` x${s.count}: \`${s.path}\``);
    if (r.overflow) md.push(`- overflow: scrollWidth ${r.overflow.scroll} > clientWidth ${r.overflow.client}; ${[...r.overflow.guilty, ...r.overflow.unfiltered].join("; ")}`);
    for (const v of r.axe) md.push(`- axe ${v.id} [${v.impact}] \`${v.target}\`: ${v.help}`);
    if (r.misses.length) md.push(`- fixture misses: ${r.misses.map((m) => `\`${m}\``).join(", ")}`);
    for (const e of r.errors) md.push(`- error: ${e}`);
    md.push("");
  }
  writeFileSync(resolve(OUT, "report.md"), md.join("\n"));
}

/* ------------------------------------------------------------------ main */

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const { chromium } = await loadPlaywright();

  if (args.proto) {
    if (!existsSync(PROTOTYPE)) throw new Error(`harmony: ${PROTOTYPE} does not exist`);
    const server = await startPrototypeServer();
    const browser = await launchBrowser(chromium);
    let results;
    try {
      results = await walkPrototype(args, browser, server);
    } finally {
      await browser.close();
      await server.stop();
    }
    console.log("");
    console.log(`${pad("route", 36)} ${pad("viewport", 9)} ${pad("prototype page", 30)} screenshot`);
    console.log("-".repeat(96));
    let failures = 0;
    for (const r of results) {
      const state = r.loadError ? `error: ${r.loadError}` : r.present ? r.key : `absent (no P['${r.key}'])`;
      if (r.loadError) failures += 1;
      console.log(`${pad(r.route, 36)} ${pad(r.viewport, 9)} ${pad(state, 30)} ${r.shot ? r.shot.slice(OUT.length + 1) : ""}`);
    }
    mkdirSync(OUT, { recursive: true });
    writeFileSync(resolve(OUT, "proto-report.json"), JSON.stringify({ generated_at: new Date().toISOString(), results }, null, 2));
    console.log("");
    console.log(`${results.filter((r) => r.present).length}/${results.length} prototype pages screenshotted to ${resolve(OUT, "proto")}`);
    process.exit(failures ? 1 : 0);
  }

  OUT = resolve(HARMONY_ROOT, args.lang);
  console.log(`language: ${args.lang}`);
  const allow = buildAllowlist(readFileSync(INDEX_CSS, "utf8"));
  console.log(
    `allowlist from src/index.css: ${allow.fontSizes.size} font sizes [${[...allow.fontSizes].join(" ")}], ` +
      `${allow.radii.size} radii [${[...allow.radii].join(" ")}], ${allow.colors.length} colours`,
  );
  if (allow.fontSizes.size < 3 || allow.colors.length < 5) {
    // NON-VACUITY. An allowlist read from a stylesheet with no tokens in it
    // would flag every element on every page, which is noise rather than a
    // measurement; an empty one would pass everything.
    throw new Error("harmony: src/index.css yielded almost no tokens; is the :root block still there?");
  }

  const server = await startVite();
  const browser = await launchBrowser(chromium);
  let results;
  try {
    results = await walkApp(args, browser, server, allow);
  } finally {
    await browser.close();
    await server.stop();
  }

  printAppTable(results, args);
  writeAppReport(results, allow, args);

  const failures = [];
  for (const r of results) {
    const where = `${r.route} @ ${r.viewport}`;
    if (!r.loaded) failures.push(`${where}: failed to load (${r.loadError})`);
    if (r.strays.length) failures.push(`${where}: ${r.strays.reduce((n, s) => n + s.count, 0)} stray value(s)`);
    if (r.overflow) failures.push(`${where}: horizontal overflow`);
    const bad = r.axe.filter(serious).length;
    if (bad) failures.push(`${where}: ${bad} serious/critical axe violation(s)`);
    if (args.strictFixtures && r.misses.length) failures.push(`${where}: ${r.misses.length} fixture miss(es)`);
  }
  const routesWalked = new Set(results.map((r) => r.route)).size;
  console.log(
    `${routesWalked} route(s) x ${VIEWPORTS.length} viewport(s); report in ${resolve(OUT, "report.md")}` +
      (args.shots ? `; screenshots under ${OUT}` : ""),
  );
  if (failures.length) {
    console.log(`FAIL  ${failures.length} finding(s):`);
    for (const f of failures) console.log(`  ${f}`);
    process.exit(1);
  }
  console.log("ok    every route on the tokens, no overflow, no serious axe violation" + (args.strictFixtures ? ", no fixture miss" : ""));
  process.exit(0);
}

// Only run when executed directly, so the pure helpers above can be imported by a test.
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch((err) => {
    console.error(err instanceof Error ? err.stack || err.message : String(err));
    process.exit(1);
  });
}
