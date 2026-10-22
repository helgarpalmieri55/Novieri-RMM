import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { parseAst } from "vite";
import { describe, expect, it } from "vitest";

/**
 * NO WIRE NAME IN PRIMARY COPY (docs/design/settings-language.md).
 *
 * The console names the decision the operator is making; the system's own
 * vocabulary -- job types, check keys, permission codes, enum values, cron
 * strings, Go build targets -- never reaches the screen as the thing a person
 * reads. A raw key may stay as secondary text an operator copies into a log
 * search or an API call, and then it sits in a monospace span (`mono`,
 * `<code>`, `<kbd>`, `<pre>`, `<samp>`), which is the explicit allowlist here.
 *
 * WHAT COUNTS AS COPY. The file is parsed (vite's own parser), not
 * regex-scanned, because JSX text is full of apostrophes that a hand lexer
 * reads as string openers. Checked: JSX text; string literals rendered as a
 * JSX child (through `?:`, `&&`, `??`, `+` and template literals, never
 * through a call, whose arguments are data); the same in copy attributes
 * (`title`, `subtitle`, `label`, `body`, ...) and in object keys that carry
 * copy (`label`, `description`, `hint`, ...). A string handed to a function
 * is not copy: `jobTypeLabel("patch.apply")` is the fix, not the leak.
 */
const SRC = join(import.meta.dirname, "..");

const COPY_ATTRS = new Set([
  "title",
  "subtitle",
  "label",
  "body",
  "description",
  "hint",
  "help",
  "placeholder",
  "emptyLabel",
  "confirmLabel",
  "aria-label",
  "ariaLabel",
  "caption",
  "note",
  "legend",
]);
const COPY_KEYS = new Set(["label", "description", "hint", "help", "title", "subtitle", "body", "placeholder", "caption"]);
const MONO_TAGS = new Set(["code", "kbd", "pre", "samp"]);

/** Job types and health check keys the backend registers. */
const WIRE_NAMES = [
  "patch.apply",
  "patch.scan",
  "patch.reboot",
  "app.deploy",
  "script.run",
  "backup.run",
  "backup.restore",
  "provision.enroll",
  "remote.session.open",
  "remote.screen.open",
  "net.sweep",
  "site.wake",
  "health.check.now",
  "health.policy.apply",
  "agent.cert.renew",
  "agent.cert.install",
  "agent.update",
  "agent.push_install",
  "agent.push_probe",
  // Windows push over WinRM (plan 2026-09-28, Task 10): the capability a site
  // server advertises, the probe's readiness fields and the push credential
  // types. They reach the screen only as "WinRM answers on the target", "A
  // domain controller is reachable", "Push install", "Windows domain account".
  "agent.push_install.winrm",
  "winrm_https",
  "winrm_http",
  "kdc_reachable",
  "dns_name",
  "push_install",
  "windows_domain",
  "agent.uninstall",
  // The notification events (install-link spec §1.5); they reach the screen
  // only through `EVENT_TYPE_LABELS`.
  "device.joined_via_link",
  "device.removal_incomplete",
  "push.run.completed",
  "push.run.stopped",
  // How a rejection ended (`Device.rejection_outcome`); `manualRemovalStatus`
  // is what reaches the screen. `timeout` is left out: it is also a word.
  "agent_confirmed",
  "agent_did_not_confirm",
  "agent_cannot_uninstall",
  "inventory.collect",
  "logs.ship",
  "job.cancel",
  "fs.clean_temp",
  "service.running",
  "cpu.busy_pct",
  "disk.free_pct",
  "mem.used_pct",
  "agent.offline",
  // Enum values.
  "require_approval",
  "auto_execute",
  "end_user",
  "unavailable_timeout",
];

const escape = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/** Each rule: a name for the failure message and a test over one string. */
const RULES: { name: string; test: (text: string) => boolean }[] = [
  ...WIRE_NAMES.map((name) => {
    const re = new RegExp(`(?<![\\w.])${escape(name)}(?![\\w])`);
    return { name, test: (text: string) => re.test(text) };
  }),
  {
    name: "Go build target",
    test: (text) => /\bdarwin\/|\b(?:linux|windows|freebsd)\/(?:amd64|arm64|386|arm)\b/.test(text),
  },
  {
    name: "permission code",
    test: (text) => /(?<![\w.])[a-z]+(?:\.[a-z_]+)*\.(?:read|manage)(?![\w])/.test(text),
  },
  {
    name: "cron expression",
    test: (text) =>
      [...text.matchAll(/(?:^|\s)([\d*/,-]+(?:\s+[\d*/,-]+){4})(?=\s|$)/g)].some((m) => m[1]!.includes("*")),
  },
  { name: "dead-lettered", test: (text) => /\bdead[- ]letter/i.test(text) },
  { name: "egress guard", test: (text) => /\begress guard/i.test(text) },
  { name: "flapping", test: (text) => /\bflap(?:ping|s)?\b/i.test(text) },
];

/**
 * Offenders in modules another change owns at the moment (service desk, SLA
 * and portal were out of bounds for the pass that added this guard). Each is
 * the file and a fragment of the string. An entry that no longer matches fails
 * the guard, so it is removed the day the module is fixed.
 */
const PENDING: readonly (readonly [string, RegExp])[] = [
  // The revoke confirmation names the autonomy value `require_approval` in
  // English prose. It lived in a function's return value until the copy moved
  // into the catalogue, which the scanner reads; the English is held
  // byte-identical by the translation work, so the wording is a follow-up (the
  // Spanish already says "Necesita aprobación", the label the table shows).
  ["i18n/en/health.ts", /^require_approval: "The exemption is withdrawn/],
  // The two budget picker hints name the permission `budget.manage` in English
  // prose. They lived in constants the scanner does not read until the copy
  // moved into the catalogue; the English must stay byte-identical, which is
  // what blocks rewording it here (a follow-up for whoever may change the
  // English copy). The Spanish already says
  // "alguien que pueda administrar el presupuesto".
  ["i18n/en/budget.ts", /^permission code: "No budget categories yet\./],
  ["i18n/en/budget.ts", /^permission code: "No cost centers yet\./],
];

type Node = { type: string; [key: string]: unknown };

function isNode(value: unknown): value is Node {
  return typeof value === "object" && value !== null && typeof (value as Node).type === "string";
}

/** Strings an expression renders, without descending into calls. */
function rendered(expr: unknown): string[] {
  if (!isNode(expr)) return [];
  switch (expr.type) {
    case "Literal":
      return typeof expr.value === "string" ? [expr.value] : [];
    case "TemplateLiteral": {
      const quasis = (expr.quasis as Node[]).map((q) => String((q.value as { cooked?: string }).cooked ?? ""));
      return [quasis.join(" "), ...(expr.expressions as unknown[]).flatMap(rendered)];
    }
    case "ConditionalExpression":
      return [...rendered(expr.consequent), ...rendered(expr.alternate)];
    case "LogicalExpression":
      return [...rendered(expr.left), ...rendered(expr.right)];
    case "BinaryExpression":
      return expr.operator === "+" ? [...rendered(expr.left), ...rendered(expr.right)] : [];
    case "ParenthesizedExpression":
    case "TSAsExpression":
    case "TSSatisfiesExpression":
      return rendered(expr.expression);
    default:
      return [];
  }
}

function elementName(el: Node): string {
  const name = (el.openingElement as Node | undefined)?.name as Node | undefined;
  return name?.type === "JSXIdentifier" ? String(name.name) : "";
}

/** A `code`/`kbd`/`pre`/`samp` element, or one whose className says `mono`. */
function isMono(el: Node): boolean {
  if (MONO_TAGS.has(elementName(el))) return true;
  const attrs = ((el.openingElement as Node).attributes as Node[]) ?? [];
  return attrs.some((attr) => {
    if (attr.type !== "JSXAttribute" || (attr.name as Node).name !== "className") return false;
    const value = attr.value as Node | null;
    const texts =
      value?.type === "JSXExpressionContainer" ? rendered(value.expression) : value ? rendered(value) : [];
    return texts.some((t) => /(^|\s)mono(\s|$)/.test(t));
  });
}

/** Every piece of primary copy in a source file, with its offset. */
function primaryCopy(source: string, lang: "ts" | "tsx"): { text: string; at: number }[] {
  const found: { text: string; at: number }[] = [];
  const program = parseAst(source, { lang }) as unknown as Node;

  function add(texts: string[], at: number) {
    for (const text of texts) if (text.trim()) found.push({ text, at });
  }

  function walk(node: Node, monoDepth: number) {
    let mono = monoDepth;
    if (node.type === "JSXElement" && isMono(node)) mono += 1;
    const inMono = mono > 0;
    const at = Number(node.start ?? 0);

    if (!inMono) {
      if (node.type === "JSXText") add([String(node.value)], at);
      if (node.type === "JSXElement" || node.type === "JSXFragment") {
        for (const child of (node.children as Node[]) ?? []) {
          if (child.type === "JSXExpressionContainer") add(rendered(child.expression), Number(child.start));
        }
      }
      if (node.type === "JSXAttribute") {
        const name = (node.name as Node).name;
        const value = node.value as Node | null;
        if (typeof name === "string" && COPY_ATTRS.has(name) && value) {
          add(value.type === "JSXExpressionContainer" ? rendered(value.expression) : rendered(value), at);
        }
      }
    }
    if (node.type === "Property" && !node.computed) {
      const key = node.key as Node;
      const name = key.type === "Identifier" ? key.name : key.type === "Literal" ? key.value : null;
      if (typeof name === "string" && COPY_KEYS.has(name)) add(rendered(node.value), at);
    }

    for (const [key, value] of Object.entries(node)) {
      if (key === "parent") continue;
      if (Array.isArray(value)) {
        for (const item of value) if (isNode(item)) walk(item, mono);
      } else if (isNode(value)) {
        walk(value, mono);
      }
    }
  }

  walk(program, 0);
  return found;
}

/** A language catalogue file (src/i18n/en/**, src/i18n/es/**). */
function isCatalogue(file: string): boolean {
  return /[\\/]i18n[\\/](?:en|es)[\\/]/.test(file);
}

/**
 * Every string value in a catalogue file is primary copy, whatever its key:
 * `save: "..."` is not a `COPY_KEYS` name, but it is exactly what the screen
 * shows. Object keys are not copy and are skipped. A raw key may stay as
 * secondary text when the catalogue marks it as monospace, so the content of
 * `<code>`, `<kbd>`, `<pre>` and `<samp>` in a value is dropped, as the JSX
 * scan does for those elements.
 */
function catalogueCopy(source: string): { text: string; at: number }[] {
  const found: { text: string; at: number }[] = [];
  const program = parseAst(source, { lang: "ts" }) as unknown as Node;
  const mono = /<(code|kbd|pre|samp)>[\s\S]*?<\/\1>/g;

  function walk(node: Node) {
    for (const [key, value] of Object.entries(node)) {
      if (key === "parent" || (key === "key" && !node.computed)) continue;
      const children = Array.isArray(value) ? value : [value];
      for (const child of children) {
        if (!isNode(child)) continue;
        for (const text of rendered(child).filter(() => child.type === "Literal" || child.type === "TemplateLiteral")) {
          const stripped = text.replace(mono, " ");
          if (stripped.trim()) found.push({ text: stripped, at: Number(child.start ?? 0) });
        }
        walk(child);
      }
    }
  }

  walk(program);
  return found;
}

function offenders(source: string, lang: "ts" | "tsx", catalogue = false): string[] {
  const out: string[] = [];
  for (const { text } of catalogue ? catalogueCopy(source) : primaryCopy(source, lang)) {
    for (const rule of RULES) if (rule.test(text)) out.push(`${rule.name}: ${JSON.stringify(text.trim()).slice(0, 90)}`);
  }
  return out;
}

function* sourceFiles(dir: string): Generator<string> {
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) {
      yield* sourceFiles(path);
    } else if (/\.(ts|tsx)$/.test(entry) && !/\.test\.tsx?$/.test(entry) && !entry.endsWith(".d.ts")) {
      yield path;
    }
  }
}

describe("the copy scanner", () => {
  // Without these, "no offenders" could mean "the scanner found no copy".
  it("reads JSX text, apostrophes and all", () => {
    expect(offenders(`const a = <p>Don't run patch.apply here</p>;`, "tsx")).toEqual([
      'patch.apply: "Don\'t run patch.apply here"',
    ]);
  });

  it("reads a rendered string, through a conditional and a template", () => {
    const hits = offenders("const a = <p>{ok ? `Needs devices.read` : \"darwin/arm64\"}</p>;", "tsx");
    expect(hits.map((h) => h.split(":")[0])).toEqual(["permission code", "Go build target"]);
  });

  it("reads copy attributes and copy keys", () => {
    expect(offenders(`const a = <Panel subtitle="Dead-lettered deliveries" />;`, "tsx")).toHaveLength(1);
    expect(offenders(`const o = { label: "require_approval" };`, "ts")).toHaveLength(1);
    expect(offenders(`const o = { label: "Maintenance window", cron: "0 2-5 * * 6" };`, "ts")).toEqual([]);
    expect(offenders(`const a = <p>Runs at 0 2-5 * * 6</p>;`, "tsx")).toHaveLength(1);
  });

  it("does not read a string handed to a function, or a non-copy attribute", () => {
    expect(offenders(`const a = <p>{jobTypeLabel("patch.apply")}</p>;`, "tsx")).toEqual([]);
    expect(offenders(`const a = <Gate permission="patch.read" />;`, "tsx")).toEqual([]);
    expect(offenders(`if (can("devices.read")) run("patch.scan");`, "ts")).toEqual([]);
  });

  it("allows a raw key in a monospace secondary span, and only there", () => {
    expect(offenders(`const a = <span className="mono muted">patch.apply</span>;`, "tsx")).toEqual([]);
    expect(offenders(`const a = <code>{"health.check.now"}</code>;`, "tsx")).toEqual([]);
    expect(offenders(`const a = <span className="muted">patch.apply</span>;`, "tsx")).toHaveLength(1);
  });

  it("does not mistake English for a permission code", () => {
    expect(offenders(`const a = <p>Read the file. Manage it.</p>;`, "tsx")).toEqual([]);
  });
});

describe("the catalogue scan", () => {
  it("reads every string value, whatever its key", () => {
    expect(offenders(`export const a = { save: "Run patch.apply now" };`, "ts", true)).toHaveLength(1);
    expect(offenders("export const a = { n: { one: `Needs devices.read`, other: \"x\" } };", "ts", true)).toHaveLength(1);
  });

  it("does not read object keys, or a raw key inside monospace markup", () => {
    expect(offenders(`export const a = { "patch.apply": "Patch" };`, "ts", true)).toEqual([]);
    expect(offenders(`export const a = { hint: "Search <code>patch.apply</code> in the log" };`, "ts", true)).toEqual([]);
    expect(offenders(`export const a = { hint: "Search patch.apply in the log" };`, "ts", true)).toHaveLength(1);
  });
});

describe("user-visible copy", () => {
  it("names no wire value outside a monospace span", () => {
    const found: string[] = [];
    const pendingSeen = new Set<number>();
    let scanned = 0;
    let catalogues = 0;
    for (const file of sourceFiles(SRC)) {
      scanned += 1;
      const lang = file.endsWith(".tsx") ? "tsx" : "ts";
      const path = relative(SRC, file);
      const catalogue = isCatalogue(file);
      if (catalogue) catalogues += 1;
      for (const hit of offenders(readFileSync(file, "utf8"), lang, catalogue)) {
        const pending = PENDING.findIndex(([where, what]) => where === path && what.test(hit));
        if (pending >= 0) pendingSeen.add(pending);
        else found.push(`${path}: ${hit}`);
      }
    }
    // A walk that found no files would also find no offenders.
    expect(scanned).toBeGreaterThan(300);
    // Copy moves into the catalogues, so the walk must reach them. An empty
    // catalogue set must not pass.
    expect(catalogues).toBeGreaterThan(0);
    expect(found).toEqual([]);
    // A pending entry that matches nothing has been fixed: remove it.
    expect(PENDING.filter((_, at) => !pendingSeen.has(at))).toEqual([]);
  });
});
