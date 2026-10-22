import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { en } from "../i18n/en";
import { es } from "../i18n/es";

const walk = (d: string): string[] =>
  readdirSync(d).flatMap((n) =>
    statSync(join(d, n)).isDirectory()
      ? walk(join(d, n))
      : /\.tsx?$/.test(n) && !/\.test\./.test(n)
        ? [join(d, n)]
        : [],
  );

// humaniseEnum takes the type as a trailing string-literal argument:
//   humaniseEnum(session.state, "RemoteSessionState")
// The scan below matches exactly that call form (the value expression may
// contain one level of nested parentheses).
const TYPED_CALL = /humaniseEnum\((?:[^()]|\([^()]*\))*?,\s*"(\w+)"\s*\)/g;

describe("every rendered enum value has a label in both languages", () => {
  // The union a typed call names is read from the file that declares it:
  // lib/api.ts for the wire enums, or the owning module's own types.ts when the
  // union is that module's (platform-status's `ReasonCode`, say). Every
  // declaration of the name is checked, so two files that happen to share a
  // name cannot hide one another's values.
  const unions = new Map<string, string[][]>();
  const rendered = new Set<string>();
  for (const f of walk("src")) {
    const source = readFileSync(f, "utf8");
    // Comments are removed before the unions are read: a union whose members
    // carry a doc comment (`JobStatus`) is otherwise not matched at all.
    const code = source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
    for (const m of code.matchAll(/export type (\w+) =\s*((?:\s*\|?\s*"[^"]+")+);/g)) {
      const values = [...m[2]!.matchAll(/"([^"]+)"/g)].map((x) => x[1]!);
      unions.set(m[1]!, [...(unions.get(m[1]!) ?? []), values]);
    }
    for (const m of source.matchAll(TYPED_CALL)) rendered.add(m[1]!);
  }

  it("the scan finds the typed call sites", () => {
    expect(rendered.size).toBeGreaterThan(0);
  });

  for (const type of rendered) {
    it(type, () => {
      const declarations = unions.get(type);
      expect(declarations, `no union type ${type} declared under src/`).toBeDefined();
      for (const v of declarations!.flat()) {
        expect((en.enums as Record<string, Record<string, string>>)[type]?.[v], `${type}.${v} (en)`).toBeDefined();
        expect((es.enums as Record<string, Record<string, string>>)[type]?.[v], `${type}.${v} (es)`).toBeDefined();
      }
    });
  }
});
