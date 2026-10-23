import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * NO USER-VISIBLE STRING MAY NAME THE IDENTITY VENDOR.
 *
 * A buyer does not know what Keycloak is and must never need to. The word
 * leaking into a rendered string is not cosmetic: it tells the reader there is
 * a second system they are expected to understand, which is the exact
 * impression this whole cycle exists to remove.
 *
 * SCOPED TO WHAT A USER CAN SEE, DELIBERATELY. String literals (quoted and
 * template) and JSX text are checked. Import paths, module names, type names
 * and COMMENTS are exempt -- the platform does integrate with Keycloak, and
 * source that pretended otherwise would be lying about its own dependency. A
 * guard broader than its own claim gets deleted the first time it is
 * inconvenient; this one is exactly as wide as "what a user can see".
 *
 * WHY A SCANNER AND NOT A REGEX. The obvious implementation -- run
 * /(["'`])(...)\1/g over the raw file -- was written first and did not survive
 * its own first run. It reported seven offenders in this repository and ALL
 * SEVEN were comments: an apostrophe in "Keycloak's account console" opens a
 * string that runs to the next apostrophe several lines away, and a
 * markdown-style `keycloak_events` inside a `//` comment is a perfectly good
 * template literal as far as a regex is concerned. A guard whose failures are
 * all false has one outcome -- somebody rewrites accurate comments to appease
 * it, or deletes it. It also missed the case that matters most: JSX text.
 * `<p>Keycloak asks for your password</p>` contains no string literal at all
 * and sailed straight through.
 *
 * So the file is lexed instead: comments removed, string and template bodies
 * collected, and JSX text read off what is left. The lexer is small and its
 * three judgement calls (regex-vs-division, template interpolation, JSX text)
 * are pinned by the unit tests below, which exist so that a guard reporting
 * "no offenders" is a fact about the source and not about a broken scanner.
 */
const SRC = join(import.meta.dirname, "..");
const FORBIDDEN = /keycloak/i;

/**
 * Comment-stripped, string-blanked source plus every literal body found.
 *
 * `code` keeps the shape of the file (a removed string leaves its empty
 * delimiters behind) so that a `<` or `>` inside a string cannot be mistaken
 * for a JSX tag boundary later.
 */
interface Lexed {
  strings: string[];
  code: string;
}

/** Chars after which a `/` opens a regex rather than dividing. */
const REGEX_PRECEDERS = new Set("=(,:[!&|?{};+-*%~^<>\n");
const REGEX_KEYWORDS =
  /(?:^|[^\w$])(?:return|typeof|instanceof|in|of|new|delete|void|case|do|else|yield|await)\s*$/;

function opensRegex(code: string, next: string): boolean {
  // `/>` closes a JSX element; no regex in this codebase starts with `>`.
  if (next === ">") return false;
  const trimmed = code.replace(/\s+$/, "");
  if (trimmed === "") return true;
  return REGEX_PRECEDERS.has(trimmed[trimmed.length - 1]!) || REGEX_KEYWORDS.test(trimmed);
}

function lex(source: string): Lexed {
  const strings: string[] = [];
  let code = "";
  // A stack so a template inside `${...}` inside a template still works.
  const stack: { kind: "code" | "template"; depth: number; buf: string }[] = [
    { kind: "code", depth: 0, buf: "" },
  ];
  let i = 0;

  while (i < source.length) {
    const top = stack[stack.length - 1]!;
    const c = source[i]!;
    const next = source[i + 1] ?? "";

    if (top.kind === "template") {
      if (c === "\\") {
        top.buf += source.slice(i, i + 2);
        i += 2;
      } else if (c === "`") {
        strings.push(top.buf);
        stack.pop();
        code += "``";
        i += 1;
      } else if (c === "$" && next === "{") {
        strings.push(top.buf);
        top.buf = "";
        stack.push({ kind: "code", depth: 0, buf: "" });
        i += 2;
      } else {
        top.buf += c;
        i += 1;
      }
      continue;
    }

    if (c === "/" && next === "/") {
      while (i < source.length && source[i] !== "\n") i += 1;
      continue;
    }
    if (c === "/" && next === "*") {
      i += 2;
      while (i < source.length && !(source[i] === "*" && source[i + 1] === "/")) i += 1;
      i += 2;
      continue;
    }
    if (c === '"' || c === "'") {
      let body = "";
      i += 1;
      while (i < source.length && source[i] !== c) {
        if (source[i] === "\\") {
          body += source.slice(i, i + 2);
          i += 2;
        } else {
          body += source[i];
          i += 1;
        }
      }
      i += 1;
      strings.push(body);
      code += c + c;
      continue;
    }
    if (c === "`") {
      stack.push({ kind: "template", depth: 0, buf: "" });
      i += 1;
      continue;
    }
    if (c === "/" && opensRegex(code, next)) {
      i += 1;
      let inClass = false;
      while (i < source.length) {
        const r = source[i]!;
        if (r === "\\") {
          i += 2;
          continue;
        }
        if (r === "[") inClass = true;
        else if (r === "]") inClass = false;
        else if (r === "/" && !inClass) break;
        else if (r === "\n") break;
        i += 1;
      }
      i += 1;
      code += " ";
      continue;
    }
    if (c === "{") {
      top.depth += 1;
      code += c;
      i += 1;
      continue;
    }
    if (c === "}") {
      if (top.depth === 0 && stack.length > 1) {
        stack.pop();
        i += 1;
        continue;
      }
      top.depth -= 1;
      code += c;
      i += 1;
      continue;
    }
    code += c;
    i += 1;
  }

  // An unterminated template at EOF still yields what it held.
  for (const frame of stack) if (frame.kind === "template") strings.push(frame.buf);
  return { strings, code };
}

/**
 * JSX text: what sits between a `>` and the next `<` with no bracket or brace
 * in between. Read off the lexed `code`, where strings are already blanked, so
 * `"a > b"` cannot fake a text node. `=> <Foo/>` and `count > 0 && <Foo/>`
 * yield fragments with no letters in them, which is harmless for a word check.
 */
function jsxText(code: string): string[] {
  return [...code.matchAll(/>([^<>{}]+)</g)].map((m) => m[1]!);
}

/** Copy a user could read: literal bodies plus JSX text, minus specifiers. */
function userVisibleText(source: string): string[] {
  const { strings, code } = lex(source);
  return [
    ...strings.filter((body) => !/^[./@]/.test(body) && !/^https?:/.test(body)),
    ...jsxText(code),
  ];
}

function* sourceFiles(dir: string): Generator<string> {
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) {
      yield* sourceFiles(path);
    } else if (
      /\.(ts|tsx)$/.test(entry) &&
      !/\.test\.tsx?$/.test(entry) &&
      // The i18n test helper is never shipped, and its same-in-both list
      // names the vendor on purpose (a Spanish render may legitimately show it).
      relative(SRC, path).replaceAll("\\", "/") !== "i18n/test-utils.tsx"
    ) {
      yield path;
    }
  }
}

/** A language catalogue file (src/i18n/en/**, src/i18n/es/**). */
function isCatalogue(file: string): boolean {
  return /[\\/]i18n[\\/](?:en|es)[\\/]/.test(file);
}

describe("the scanner the guard is built on", () => {
  // Without these, "no offenders" could equally mean "the lexer returned
  // nothing", and the guard below would be decoration.
  it("reads a quoted string", () => {
    expect(userVisibleText(`const a = "Keycloak asks first";`)).toContain("Keycloak asks first");
  });

  it("reads JSX text, which carries no quotes at all", () => {
    expect(userVisibleText(`const a = <p>Keycloak asks first</p>;`)).toContain(
      "Keycloak asks first",
    );
  });

  it("reads a template literal, around its interpolations", () => {
    expect(userVisibleText("const a = `sent by ${who} via Keycloak`;")).toContain(" via Keycloak");
  });

  it("ignores a line comment, apostrophe and backticks included", () => {
    const source = "// Keycloak's `keycloak_events` probe stays named accurately\nconst a = 1;";
    expect(userVisibleText(source).join("")).not.toMatch(FORBIDDEN);
  });

  it("ignores a block comment", () => {
    expect(userVisibleText("/** Keycloak owns the password. */\nconst a = 1;").join("")).not.toMatch(
      FORBIDDEN,
    );
  });

  it("ignores an import specifier and a URL", () => {
    const source = `import x from "./keycloak/client";\nconst u = "https://kc/keycloak";`;
    expect(userVisibleText(source).join("")).not.toMatch(FORBIDDEN);
  });

  it("is not derailed by a regex literal or a self-closing tag", () => {
    const source = `const a = <b className={x} />;\nconst c = "x".replace(/['"\`]/g, "");\nconst d = "Keycloak";`;
    expect(userVisibleText(source)).toContain("Keycloak");
  });

  it("keeps identifiers and type names out of the copy it checks", () => {
    const source = `import type { KeycloakProbe } from "../types";\nconst keycloakEvents = probe.keycloak_events;`;
    expect(userVisibleText(source).join("")).not.toMatch(FORBIDDEN);
  });
});

describe("user-visible copy", () => {
  it("never names the identity vendor", () => {
    const offenders: string[] = [];
    let scanned = 0;
    let catalogues = 0;
    for (const file of sourceFiles(SRC)) {
      scanned += 1;
      if (isCatalogue(file)) catalogues += 1;
      for (const text of userVisibleText(readFileSync(file, "utf8"))) {
        if (FORBIDDEN.test(text)) {
          offenders.push(`${file.replace(SRC, "src")}: ${JSON.stringify(text).slice(0, 80)}`);
        }
      }
    }
    // A walk that found no files would also find no offenders.
    expect(scanned).toBeGreaterThan(50);
    // Copy moves into the catalogues, so the walk must reach them: every
    // string literal there is checked (this scanner reads all literals). An
    // empty catalogue set must not pass.
    expect(catalogues).toBeGreaterThan(0);
    expect(offenders).toEqual([]);
  });
});
