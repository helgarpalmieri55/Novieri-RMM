/**
 * THE BLIND SPOT THIS CLOSES, and it was hiding a live WCAG failure.
 *
 * `contrast.test.ts` carries a guard whose entire job is to fail if a token
 * that cannot carry small text is ever moved onto the small-text list. That
 * guard reads STYLESHEETS. A consistency sweep once found five places painting
 * `text-[var(--gold)]` in JSX -- inside a Tailwind arbitrary value, where no
 * stylesheet scan could ever see them -- at 4.28:1 against a 4.5:1 floor. The
 * guard was green the whole time and the product was failing SC 1.4.3 in five
 * places. The v3 palette has no `--gold` (it aliases to `--sev-warn`, which is
 * legible), so the list below is v3's own: the two decorative hairlines, the
 * legacy alias that resolves to one of them, and the rail's two text tones,
 * which are legible on the rail and nowhere a module can draw.
 *
 * A floor that only holds in the files you remembered to check is not a floor.
 * This is the same lesson the 12px sweep learned when a `fontSize: "9px"`
 * shipped inline in JSX past a stylesheet-reading check.
 */
import { readFileSync, readdirSync, statSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

const REPO = resolve(import.meta.dirname, "../../..");
const SRC = resolve(REPO, "frontend/src");

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = resolve(dir, name);
    if (statSync(path).isDirectory()) return sources(path);
    return /\.tsx?$/.test(name) && !/\.test\./.test(name) ? [path] : [];
  });
}

const FILES = sources(SRC);
const rel = (path: string) => path.slice(REPO.length + 1);

/**
 * Tokens that do NOT clear 4.5:1 on the console's light surfaces, so they may
 * never paint text there. Each is measured in `contrast.test.ts` (`--line`
 * 1.23:1 and `--line-2` 1.49:1 on `--surface`; `--line-strong` is the legacy
 * alias of `--line-2`); this list is the JSX half of the same claim.
 */
const NOT_FOR_TEXT = ["--line", "--line-2", "--line-strong"];

/**
 * The rail's text tones, and their legacy aliases. Legible on `--rail`
 * (5.51:1 and 7.85:1) and illegible on every light surface (`--rail-dim` is
 * 3.20:1 on white). A module never draws the rail, so a module painting text
 * in one of these is painting it where it cannot be read.
 */
const RAIL_ONLY = ["--rail-ink", "--rail-dim", "--rail-label", "--rail-eyebrow"];

/**
 * The retired violet, in every form it can be written.
 *
 * `#6f36d9` is `rgb(111, 54, 217)`. A sweep verified its removal by grepping
 * for `111,54,217` — and the console's own sidebar carried
 * `rgba(111, 54, 217, .22)`, WITH SPACES, so the grep matched nothing and the
 * whole left rail stayed violet over a near-black base while every audit
 * reported the palette migrated. The operator saw it immediately; four
 * automated sweeps did not.
 *
 * Whitespace is stripped before matching, so no formatting hides a value again.
 */
describe("retired palette values", () => {
  const RETIRED: Record<string, string[]> = {
    "the violet primary": ["#6f36d9", "rgb(111,54,217", "rgba(111,54,217"],
    "its near-black grounds": ["#0b0b0f", "#111016", "#191821", "#0d0c11"],
    "the violet ramp": ["#7e45e5", "#8b5cf6", "#5d25c7", "#a875ff", "#6d4aff"],
  };

  const STYLES = FILES.filter((path) => /\.(css|tsx?)$/.test(path));

  it.each(Object.entries(RETIRED))("no live declaration paints %s", (_name, values) => {
    const offenders: string[] = [];
    for (const path of STYLES) {
      // Comments name retired values on purpose, to explain why they are gone.
      const source = readFileSync(path, "utf8")
        .replace(/\/\*[\s\S]*?\*\//g, "")
        .replace(/\s+/g, "");
      for (const value of values) {
        if (source.toLowerCase().includes(value.toLowerCase())) offenders.push(`${rel(path)} -> ${value}`);
      }
    }
    expect(offenders, "a retired palette value is still being painted").toEqual([]);
  });
});

describe("tokens used through Tailwind arbitrary values", () => {
  it.each(NOT_FOR_TEXT)("never paints text in %s, which fails the small-text floor", (token) => {
    const pattern = new RegExp(`text-\\[var\\(${token}\\)\\]`);
    const offenders = FILES.filter((path) => pattern.test(readFileSync(path, "utf8"))).map(rel);
    expect(
      offenders,
      `${token} is under 4.5:1 on this console's surfaces; use its -ink form for text`,
    ).toEqual([]);
  });

  it.each(RAIL_ONLY)("never paints text in %s outside the shell, where there is no rail to read it on", (token) => {
    const pattern = new RegExp(`text-\\[var\\(${token}\\)\\]`);
    const offenders = FILES.filter((path) => !path.startsWith(resolve(SRC, "shell")))
      .filter((path) => pattern.test(readFileSync(path, "utf8")))
      .map(rel);
    expect(offenders, `${token} is a rail tone; on a light surface it is under 4.5:1`).toEqual([]);
  });

  it("dresses every <button> in the modules, or gives it the design's focus ring", () => {
    // WHY THIS IS A TEST AND NOT A GREP. The sweep that found these used
    // `grep '<button' | grep -v 'className="...btn..."'`, which is
    // line-oriented: a multi-line opening tag carries its `className` on the
    // NEXT line, so the filter matched nothing and every multi-line button was
    // reported as undressed. It said 24. The answer was 3. A check that
    // overstates by 8x is worse than no check, because the real three drown.
    //
    // This parses the opening tag instead. A button is acceptable if it wears
    // `.btn` (the design's height, radius, weight, hover and disabled) or, when
    // it is deliberately not a Btn -- a list option, a button inside a
    // sentence, a segmented radio -- carries the design's focus ring so a
    // keyboard user can still find it.
    const offenders: string[] = [];
    for (const path of FILES.filter((p) => p.includes("/modules/"))) {
      // COMMENTS ARE STRIPPED FIRST, and the first version of this test did not
      // do it. This codebase explains its decisions in prose, so five comments
      // discussing why a control is or is not a `<button>` were reported as
      // undressed buttons -- the same false-positive shape as the grep this
      // replaced, one layer up. Blanked rather than removed, so the line
      // numbers in a failure still point at the real source line.
      const source = readFileSync(path, "utf8").replace(
        /\/\*[\s\S]*?\*\//g,
        (match) => match.replace(/[^\n]/g, " "),
      );
      for (const match of source.matchAll(/<button\b[^>]*>/gs)) {
        const tag = match[0];
        if (/\bbtn\b/.test(tag)) continue;
        if (/focus-visible:outline/.test(tag)) continue;
        const line = source.slice(0, match.index).split("\n").length;
        offenders.push(`${rel(path)}:${line}`);
      }
    }
    expect(
      offenders,
      "a <button> with neither .btn nor a focus ring is invisible to a keyboard user",
    ).toEqual([]);
  });

  it("uses no bare `rounded`, which Tailwind fixes at 4px outside the ladder", () => {
    // `@theme` remaps `rounded-sm`…`rounded-2xl` onto the ladder (6 / 10 /
    // 999), but the UNSUFFIXED utility is static in Tailwind v4 and keeps
    // 0.25rem. It is the one radius the remap cannot reach, which is why boxes
    // were still landing at 4px beside panels after the scale was mapped.
    const offenders = FILES.filter((path) =>
      /className="[^"]*\brounded\b(?!-)[^"]*"/.test(readFileSync(path, "utf8")),
    ).map(rel);
    expect(
      offenders,
      "bare `rounded` is 4px and off the ladder; name a rung (rounded-sm/md for a control, rounded-lg..2xl for a panel, rounded-full for a pill)",
    ).toEqual([]);
  });
});
