// @vitest-environment node
/**
 * The guard that keeps the console fully Spanish.
 *
 * It walks the syntax tree of every non-test `.tsx` under `src/modules`,
 * `src/shell` and `src/components` (plus the two app-level files named in
 * EXTRA_FILES) and fails on any user-visible English literal that is not
 * routed through `t(`, `translate(` or `<Trans`:
 *
 *  - a JSX text node, or a string literal used directly as a JSX child
 *    (`{"Hello"}`, `{ok ? "Yes" : "No"}`);
 *  - a literal in one of the TEXT_PROPS below (a string, a no-substitution
 *    template, a template with literal parts, or either arm of a ternary /
 *    `||` / `??` / `&&` / `+`, which is how a literal usually hides in a prop).
 *
 * "Literal" means at least two letters. A literal passed to `t()` /
 * `translate()` is a call argument, never a prop value, so it is never seen;
 * the `k=` prop of `<Trans>` is a catalogue key and is not a TEXT_PROP.
 *
 * Exemptions, each reviewed:
 *  - `// i18n-exempt: <reason>` on the same line or the line before (inside
 *    JSX, `{/* i18n-exempt: <reason> *\/}`). An exemption that exempts nothing
 *    fails the test, so a stale one cannot linger;
 *  - ALLOWED_LITERALS, for text that is not language (a brand, a protocol).
 *
 * PARSER. TypeScript 7 (the native port) no longer ships the classic
 * `ts.createSourceFile` API; its programmatic API is `typescript/unstable/*`,
 * which drives the bundled native compiler over a pipe and hands back the same
 * syntax tree. That is what is used here -- no regular expression is used to
 * find structure; the only text matching is on already-extracted literal text
 * and on comment lines.
 *
 * FALSIFIER. This test is wrong if it can pass while the console shows English.
 * The self-test at the bottom pins what it must flag and must not flag, and the
 * non-vacuity floors fail it if the walk silently covers nothing.
 */
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createVirtualFileSystem } from "typescript/unstable/fs";
import { API } from "typescript/unstable/sync";
import { SyntaxKind } from "typescript/unstable/ast";
import type { Node, SourceFile } from "typescript/unstable/ast";
import { afterAll, describe, expect, it } from "vitest";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FRONTEND = path.resolve(HERE, "../..");

const ROOTS = ["src/modules", "src/shell", "src/components"].map((r) => `${FRONTEND}/${r}/`);

/** App-level files that render text but sit outside the three roots. */
const EXTRA_FILES = ["src/App.tsx", "src/lib/ApiContext.tsx"].map((f) => `${FRONTEND}/${f}`);

/**
 * JSX props whose string value a person reads (or a screen reader speaks).
 * The first seventeen are the brief's; the rest are the text-carrying props of
 * the shared components, read from `components/ui.tsx`, `shell/components.tsx`,
 * `components/form.tsx`, `components/picker.tsx` and their neighbours
 * (`confirm`, `emptystate`, `prerequisites`, `weekpainter`, `duration`,
 * `chart`, `shell/viz`, `shell/AreaPage`).
 */
export const TEXT_PROPS: ReadonlySet<string> = new Set([
  // the brief's list
  "title",
  "label",
  "placeholder",
  "aria-label",
  "description",
  "alt",
  "subtitle",
  "emptyLabel",
  "zeroLabel",
  "body",
  "confirmLabel",
  "hint",
  "emptyHint",
  "heading",
  "cta",
  "tooltip",
  "caption",
  // shared-component text props
  "ariaLabel", // shell/components Tabs, Pager, Drawer ...
  "pendingLabel", // form.tsx SubmitButton
  "disabledReason", // form.tsx SubmitButton
  "savedMessage", // form.tsx SaveStatus
  "failedMessage", // form.tsx SaveStatus
  "message", // form.tsx Notice / Banner
  "sublabel", // picker.tsx Option
  "previousLabel", // shell/components Pager
  "nextLabel", // shell/components Pager
  "sub", // shell/components + viz: a secondary line
  "subject", // confirm.tsx: names the thing being confirmed
  "unit", // chart.tsx + viz: a unit of measure shown to the reader
  "unitLabel", // duration.tsx
  "empty", // attributes.tsx: what to show for a missing value
  "help", // ui.tsx Field + viz: help text
  "detail", // prerequisites + viz: a detail line
  "lead", // ui.tsx: lead text
  "foot", // shell/components Panel: footer text
  "waitingOn", // prerequisites.tsx
  "summary", // shell/viz: a text summary of a chart
  // native ARIA attributes that carry spoken text
  "aria-description",
  "aria-roledescription",
  "aria-valuetext",
  "aria-placeholder",
]);

/**
 * Text that is not language, written the same in every locale. Each entry is
 * the whole trimmed literal; `IDENTICAL_OK` is the model. Keep it as small as
 * it can be: when the reason is local to one site, an `i18n-exempt` comment
 * at the site is the better tool.
 */
export const ALLOWED_LITERALS: Readonly<Record<string, string>> = {
  ITMS: "The product's own name, shown in the breadcrumb root and the sidebar wordmark.",
  Windows: "A platform brand, listed as an OS family option; Microsoft's name for it is the same in Spanish.",
  Linux: "A platform brand, listed as an OS family option.",
  macOS: "A platform brand, listed as an OS family option.",
  "SHA-256": "The name of a hash algorithm, a column header for the digest; written the same in Spanish.",
  sha256: "The name of a hash algorithm, the mono prefix of a short digest; written the same in Spanish.",
  VLAN: "An acronym (virtual LAN), the mono prefix of a VLAN id; written the same in Spanish.",
  ms: "The SI symbol for milliseconds, after a measured duration; the same in Spanish.",
  Esc: "A key-cap legend; Spanish keyboards are also printed Esc.",
};

/** The brief's non-vacuity floors, with the one the repository cannot meet. */
const MIN_JSX_NODES = 5_000;
/**
 * The brief asks for "more than 300" files. Under the three roots there are
 * 263 non-test, non-fixture `.tsx` files (the 300+ figure counted tests).
 * Padding the walk to reach 300 would make the floor meaningless, so the floor
 * is set just under the real count: a walk that loses a directory fails.
 */
const MIN_FILES = 250;

interface Finding {
  file: string;
  line: number;
  kind: "jsx-text" | "jsx-child" | "prop";
  prop?: string;
  text: string;
}

interface ScanResult {
  findings: Finding[];
  /** Exemption comments that exempted something. */
  usedExemptions: Set<string>;
  /** Every exemption comment present, as `file:line`. */
  allExemptions: Set<string>;
  /** ALLOWED_LITERALS entries that excused a literal. */
  usedAllowed: Set<string>;
  nodes: number;
  jsxNodes: number;
}

const LETTERS = /\p{L}[\s\S]*\p{L}/u;
const ENTITY = /&(?:#\d+|#x[0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]*);/g;

/** True when `text` holds at least two letters, ignoring HTML entities. */
function hasLanguage(text: string): boolean {
  return LETTERS.test(text.replace(ENTITY, " "));
}

function lineStarts(text: string): number[] {
  const starts = [0];
  for (let i = 0; i < text.length; i++) if (text.charCodeAt(i) === 10) starts.push(i + 1);
  return starts;
}

function lineOf(starts: number[], offset: number): number {
  let lo = 0;
  let hi = starts.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if ((starts[mid] ?? 0) <= offset) lo = mid;
    else hi = mid - 1;
  }
  return lo + 1;
}

/** The `i18n-exempt:` marker on a line, if it is in a comment and has a reason. */
function exemptionOn(lineText: string): boolean {
  const at = lineText.indexOf("i18n-exempt:");
  if (at < 0) return false;
  const before = lineText.slice(0, at);
  if (!before.includes("//") && !before.includes("/*")) return false;
  return lineText.slice(at + "i18n-exempt:".length).replace(/\*\/\}?/g, "").trim().length > 0;
}

interface Literal {
  text: string;
  start: number;
}

/**
 * The literal text an expression can evaluate to directly: itself if it is a
 * literal, otherwise the literal arms of a ternary / logical / `+` / cast.
 * Calls (including `t(...)`), identifiers and property reads yield nothing.
 */
function literalsOf(node: Node | undefined, sf: SourceFile): Literal[] {
  if (!node) return [];
  switch (node.kind) {
    case SyntaxKind.StringLiteral:
    case SyntaxKind.NoSubstitutionTemplateLiteral:
      return [{ text: (node as unknown as { text: string }).text, start: node.getStart(sf) }];
    case SyntaxKind.TemplateExpression: {
      const t = node as unknown as {
        head: { text: string };
        templateSpans: { expression: Node; literal: { text: string } }[];
      };
      const parts = [t.head.text, ...t.templateSpans.map((s) => s.literal.text)];
      const out: Literal[] = [];
      if (hasLanguage(parts.join(" "))) out.push({ text: parts.join("${}"), start: node.getStart(sf) });
      for (const s of t.templateSpans) out.push(...literalsOf(s.expression, sf));
      return out;
    }
    case SyntaxKind.JsxExpression:
    case SyntaxKind.ParenthesizedExpression:
    case SyntaxKind.AsExpression:
    case SyntaxKind.SatisfiesExpression:
    case SyntaxKind.NonNullExpression:
      return literalsOf((node as unknown as { expression?: Node }).expression, sf);
    case SyntaxKind.ConditionalExpression: {
      const c = node as unknown as { whenTrue: Node; whenFalse: Node };
      return [...literalsOf(c.whenTrue, sf), ...literalsOf(c.whenFalse, sf)];
    }
    case SyntaxKind.BinaryExpression: {
      const b = node as unknown as { left: Node; right: Node; operatorToken: Node };
      const op = b.operatorToken.kind;
      if (
        op === SyntaxKind.BarBarToken ||
        op === SyntaxKind.QuestionQuestionToken ||
        op === SyntaxKind.AmpersandAmpersandToken ||
        op === SyntaxKind.PlusToken
      ) {
        return [...literalsOf(b.left, sf), ...literalsOf(b.right, sf)];
      }
      return [];
    }
    default:
      return [];
  }
}

function attributeName(node: Node): string | undefined {
  const name = (node as unknown as { name: Node & { text?: string } }).name;
  return name.kind === SyntaxKind.Identifier ? name.text : undefined;
}

/** Scan one parsed file. `shownAs` is the path findings are reported under. */
function scanFile(sf: SourceFile, shownAs: string, result: ScanResult): void {
  const text = sf.text;
  const starts = lineStarts(text);
  const lines = text.split("\n");
  const exemptLines = new Set<number>();
  lines.forEach((l, i) => {
    if (exemptionOn(l)) {
      exemptLines.add(i + 1);
      result.allExemptions.add(`${shownAs}:${i + 1}`);
    }
  });

  function report(kind: Finding["kind"], offset: number, literal: string, prop?: string): void {
    const trimmed = literal.trim();
    if (!hasLanguage(trimmed)) return;
    if (Object.hasOwn(ALLOWED_LITERALS, trimmed)) {
      result.usedAllowed.add(trimmed);
      return;
    }
    const line = lineOf(starts, offset);
    for (const candidate of [line, line - 1]) {
      if (exemptLines.has(candidate)) {
        result.usedExemptions.add(`${shownAs}:${candidate}`);
        return;
      }
    }
    result.findings.push({ file: shownAs, line, kind, prop, text: trimmed.replace(/\s+/g, " ") });
  }

  function walk(node: Node): void {
    result.nodes++;
    switch (node.kind) {
      case SyntaxKind.JsxText: {
        result.jsxNodes++;
        const raw = text.slice(node.pos, node.end);
        const lead = raw.length - raw.trimStart().length;
        report("jsx-text", node.pos + lead, raw);
        break;
      }
      case SyntaxKind.JsxElement:
      case SyntaxKind.JsxFragment: {
        result.jsxNodes++;
        const children = (node as unknown as { children: Node[] }).children;
        for (const child of children) {
          if (child.kind !== SyntaxKind.JsxExpression) continue;
          for (const lit of literalsOf(child, sf)) report("jsx-child", lit.start, lit.text);
        }
        break;
      }
      case SyntaxKind.JsxSelfClosingElement:
      case SyntaxKind.JsxOpeningElement:
        result.jsxNodes++;
        break;
      case SyntaxKind.JsxAttribute: {
        result.jsxNodes++;
        const name = attributeName(node);
        if (name === undefined || !TEXT_PROPS.has(name)) break;
        const init = (node as unknown as { initializer?: Node }).initializer;
        for (const lit of literalsOf(init, sf)) report("prop", lit.start, lit.text, name);
        break;
      }
      default:
        break;
    }
    node.forEachChild(walk);
  }
  walk(sf);
}

function newResult(): ScanResult {
  return { findings: [], usedExemptions: new Set(), allExemptions: new Set(), usedAllowed: new Set(), nodes: 0, jsxNodes: 0 };
}

function describeFinding(f: Finding): string {
  const where = f.kind === "prop" ? `${f.prop}=` : f.kind;
  return `${f.file}:${f.line}  ${where}  ${JSON.stringify(f.text)}`;
}

const apis: API[] = [];
afterAll(() => {
  for (const a of apis) a.close();
});

function isGuarded(file: string): boolean {
  if (!file.endsWith(".tsx")) return false;
  if (/\.test\.tsx$/.test(file) || file.includes("/fixtures/") || file.includes("/__fixtures__/")) return false;
  return ROOTS.some((r) => file.startsWith(r)) || EXTRA_FILES.includes(file);
}

describe("i18n coverage: no user-visible English literal in the TSX", () => {
  const real = newResult();
  let fileCount = 0;

  it("walks the console and finds every literal routed through the catalogue", { timeout: 180_000 }, () => {
    const api = new API({ cwd: FRONTEND });
    apis.push(api);
    const snapshot = api.updateSnapshot({ openProject: `${FRONTEND}/tsconfig.json` });
    const project = snapshot.getProjects()[0];
    expect(project, "tsconfig.json opened as a project").toBeDefined();
    if (!project) return;
    const files = project.program
      .getSourceFileNames()
      .filter(isGuarded)
      .sort();
    for (const file of files) {
      const sf = project.program.getSourceFile(file);
      expect(sf, `${file} is in the program`).toBeDefined();
      if (!sf) continue;
      scanFile(sf, path.relative(FRONTEND, file), real);
      fileCount++;
    }

    // Non-vacuity: a walk that covers nothing proves nothing.
    expect(fileCount, "files walked").toBeGreaterThan(MIN_FILES);
    expect(real.jsxNodes, "JSX nodes walked").toBeGreaterThan(MIN_JSX_NODES);
    expect(real.nodes, "syntax nodes walked").toBeGreaterThan(MIN_JSX_NODES);

    const report = real.findings.map(describeFinding);
    expect(
      report,
      `User-visible English not routed through t()/translate()/<Trans>.\n` +
        `Translate it, or add "// i18n-exempt: <reason>" on the line or the line before.`,
    ).toEqual([]);

    // A stale exemption is a lie waiting for a real literal to hide behind.
    const unused = [...real.allExemptions].filter((e) => !real.usedExemptions.has(e)).sort();
    expect(unused, "i18n-exempt comments that exempt nothing").toEqual([]);

    // The allow-list stays as small as it can be: an entry nothing uses goes.
    const idle = Object.keys(ALLOWED_LITERALS).filter((k) => !real.usedAllowed.has(k));
    expect(idle, "ALLOWED_LITERALS entries nothing uses").toEqual([]);
    for (const [literal, reason] of Object.entries(ALLOWED_LITERALS)) {
      expect(reason.length, `ALLOWED_LITERALS["${literal}"] needs a real reason`).toBeGreaterThan(10);
    }
  });

  it("the listed props cover the shared components' text props", () => {
    // The shared components declare their text props; every one of them that
    // a caller can fill with a string must be in TEXT_PROPS or be a reviewed
    // non-text prop. This pins the names the brief calls out.
    for (const name of [
      "title", "label", "placeholder", "aria-label", "description", "alt", "subtitle",
      "emptyLabel", "zeroLabel", "body", "confirmLabel", "hint", "emptyHint", "heading",
      "cta", "tooltip", "caption",
    ]) {
      expect(TEXT_PROPS.has(name), name).toBe(true);
    }
    for (const notText of ["className", "key", "id", "to", "href", "type", "name", "role", "value", "k"]) {
      expect(TEXT_PROPS.has(notText), notText).toBe(false);
    }
  });
});

describe("i18n coverage: self-test on an in-memory snippet", () => {
  function scanSnippets(snippets: Record<string, string>): ScanResult {
    const files: Record<string, string> = {
      "/virtual/tsconfig.json": JSON.stringify({
        compilerOptions: { jsx: "react-jsx", noLib: true, types: [], skipLibCheck: true },
        files: Object.keys(snippets).map((n) => `./${n}`),
      }),
    };
    for (const [name, source] of Object.entries(snippets)) files[`/virtual/${name}`] = source;
    const api = new API({ cwd: "/virtual", fs: createVirtualFileSystem(files) });
    apis.push(api);
    const snapshot = api.updateSnapshot({ openProject: "/virtual/tsconfig.json" });
    const project = snapshot.getProjects()[0];
    if (!project) throw new Error("virtual project did not open");
    const result = newResult();
    for (const name of Object.keys(snippets)) {
      const sf = project.program.getSourceFile(`/virtual/${name}`);
      if (!sf) throw new Error(`${name} missing from the virtual program`);
      scanFile(sf, name, result);
    }
    return result;
  }

  const SNIPPET = `
export function A() {
  const t = (k: string) => k;
  return (
    <div className="plain-class" data-testid="some-test-id">
      <p>Hello</p>
      <p>{t("x")}</p>
      <p>OK</p>
      <p>{"Shown"}</p>
      <p>{ready ? "Ready now" : t("y")}</p>
      <p>{"→"} 12 &nbsp; ·</p>
      <Panel title="Settings" label={t("z")} placeholder={\`Type here\`} aria-label={on ? "Close it" : t("c")} />
      <Panel title={\`\${n} items\`} description={t("d") || "Fallback text"} />
      <Panel hint="x" alt="">{t("w")}</Panel>
      <Trans k="components.confirm.typeToConfirm" />
      {/* i18n-exempt: a wire value, not language */}
      <p>Exempt by line before</p>
      <p>Exempt same line</p> {/* i18n-exempt: a unit symbol */}
      <br />
      <p>Multi
        line</p>
      <Panel type="submit" name="search" role="menu" value="archived" to="/profile" href="#main" id="main-region" />
    </div>
  );
}
`;

  it("flags exactly the literals a person would read, and nothing else", () => {
    const result = scanSnippets({ "a.tsx": SNIPPET });
    const flagged = result.findings.map((f) => `${f.kind}:${f.prop ?? ""}:${f.text}`);
    expect(flagged).toEqual([
      "jsx-text::Hello",
      "jsx-text::OK",
      "jsx-child::Shown",
      "jsx-child::Ready now",
      "prop:title:Settings",
      "prop:placeholder:Type here",
      "prop:aria-label:Close it",
      "prop:title:${} items",
      "prop:description:Fallback text",
      "jsx-text::Multi line",
    ]);
    // The two exemptions both exempted a real literal, so neither is stale.
    expect([...result.usedExemptions].sort()).toEqual([...result.allExemptions].sort());
    expect(result.allExemptions.size).toBe(2);
  });

  it("reports an exemption that exempts nothing", () => {
    const result = scanSnippets({
      "b.tsx": `export const B = () => (\n  <div>\n    {/* i18n-exempt: nothing below needs it */}\n    <p>{value}</p>\n  </div>\n);\n`,
    });
    expect(result.findings).toEqual([]);
    expect([...result.allExemptions].filter((e) => !result.usedExemptions.has(e))).toEqual(["b.tsx:3"]);
  });

  it("an exemption needs a reason", () => {
    const result = scanSnippets({
      "c.tsx": `export const C = () => (\n  <div>\n    {/* i18n-exempt: */}\n    <p>Bare</p>\n  </div>\n);\n`,
    });
    expect(result.findings.map((f) => f.text)).toEqual(["Bare"]);
  });

  it("keeps the catalogue-routed idioms clean", () => {
    // Guards the guard against false positives on the shapes the console uses.
    const result = scanSnippets({
      "d.tsx": `
export const D = () => (
  <div>
    <Trans k="a.b.c" components={{ link: <Link to="/x" /> }} />
    <Panel title={t("a.b")} subtitle={translate("c.d")} />
    <Input placeholder={t("e.f", { n: 1 })} />
    {rows.map((r) => <Row key={r.id} label={r.name} />)}
    <p>{count}</p>
    <p>{t("g.h")} · {t("i.j")}</p>
  </div>
);
`,
    });
    expect(result.findings).toEqual([]);
  });
});
