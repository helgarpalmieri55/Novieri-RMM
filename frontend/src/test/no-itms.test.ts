/**
 * "ITMS" WAS A PLACEHOLDER; THE PRODUCT IS NOVIERI RMM (operator, 2026-11-23,
 * decision A3 of the console redesign). This keeps the old name out of the
 * console by reading its source, the technique of
 * `backend/tests/test_no_vendor_names_backend.py` and `lib/noVendorNames.test.ts`.
 *
 * WIDER THAN THOSE TWO, ON PURPOSE. They read only what a user can see,
 * because the vendor they ban is a real dependency whose name is accurate in
 * code. "ITMS" names nothing real any more, so every line of `src/`,
 * `scripts/` and `index.html` is read, comments and tests included. A line
 * may still carry the letters for one of two reasons, and only these:
 *   - a wire contract with the server, which project 4 keeps (its design
 *     §1.2): the `X-ITMS-*` request headers (`X-ITMS-Request` and
 *     `X-ITMS-Background` today, and any other P4 keeps by that prefix) and
 *     the recording format's magic `ITMSRSP1`;
 *   - dated history IN A COMMENT: the line is a comment (`//`, `*`, `/*`,
 *     `{/*`, `<!--`) and carries a `YYYY-MM-DD` date, so it says what was true
 *     then, and rewriting it would falsify the record. A date does not excuse
 *     a string a person can read.
 * Lower-case `itms.` storage keys are identifiers nobody sees and stay
 * (project 4's rule for internal identifiers), so this first check ignores
 * only all-lower-case `itms` (`Itms` and `iTMS` are caught); the second, at the end of the file, reads lower case in
 * the copy a person reads.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, resolve } from "node:path";
import { describe, expect, it } from "vitest";

const FRONTEND = resolve(import.meta.dirname, "../..");
const ROOTS = [resolve(FRONTEND, "src"), resolve(FRONTEND, "scripts"), resolve(FRONTEND, "index.html")];
const SELF = resolve(import.meta.dirname, "no-itms.test.ts");
const WIRE = /X-ITMS-[A-Za-z-]+|ITMSRSP1/g;
const DATED = /\b20\d\d-\d\d-\d\d\b/;
const COMMENT = /^\s*(\/\/|\*|\/\*|\{\/\*|<!--)/;

function files(path: string): string[] {
  if (statSync(path).isDirectory()) return readdirSync(path).flatMap((name) => files(join(path, name)));
  return /\.(tsx?|css|html|mjs|json)$/.test(path) ? [path] : [];
}

/** The 1-based numbers of the lines that name ITMS for a reason this guard refuses. */
export function offences(source: string): number[] {
  return source.split("\n").flatMap((line, at) => {
    const rest = line.replace(WIRE, "");
    // Any case but all lower: lower-case `itms.` identifiers stay (see the second check).
    if (![...rest.matchAll(/itms/gi)].some((hit) => hit[0] !== "itms")) return [];
    if (COMMENT.test(line) && DATED.test(line)) return [];
    return [at + 1];
  });
}

describe("the old product name", () => {
  it("is caught where it appears, so a clean result means something", () => {
    expect(offences('<span className="brand-name">ITMS</span>')).toEqual([1]);
    expect(offences("document.title = `${title} · ITMS`;")).toEqual([1]);
    expect(offences('export const CSRF_HEADER = "X-ITMS-Request";')).toEqual([]);
    expect(offences('const MAGIC = "ITMSRSP1";')).toEqual([]);
    expect(offences("// ITMS was the placeholder name until 2026-11-23")).toEqual([]);
    expect(offences('headers.set("X-ITMS-Request", "1"); // the ITMS header')).toEqual([1]);
    expect(offences('const AGENT = "X-ITMS-Agent-Version";')).toEqual([]);
    expect(offences(" * the ITMS tab title, as it read on 2026-09-08")).toEqual([]);
    expect(offences('title: "ITMS (2026-11-23)",')).toEqual([1]);
    expect(offences("ok\nstill ITMS\nok")).toEqual([2]);
    expect(offences('<h1>Itms console</h1>')).toEqual([1]);
    expect(offences('const KEY = "itms.client";')).toEqual([]);
  });

  it("reads a real tree", () => {
    const all = ROOTS.flatMap(files);
    expect(all.length).toBeGreaterThan(500);
    expect(all).toContain(resolve(FRONTEND, "index.html"));
  });

  it("appears nowhere in the console's source, tests, scripts or page", () => {
    const found = ROOTS.flatMap(files)
      .filter((path) => path !== SELF)
      .flatMap((path) => offences(readFileSync(path, "utf8")).map((line) => `${relative(FRONTEND, path)}:${line}`));
    expect(found, found.join("\n")).toEqual([]);
  });
});

/**
 * LOWER-CASE "itms" IN WHAT A PERSON READS (coordinator, 2026-11-24). The
 * check above ignores only all-lower-case `itms`, on purpose: that is how project 4
 * spells the identifiers it keeps. So it misses an example address or a
 * download's file name that a person reads in lower case. This one reads the
 * copy: the two catalogues (`src/i18n/en`, `src/i18n/es`), the console's JSX
 * (every `.tsx` outside the tests), the fixtures the harmony gate photographs
 * (every `fixtures.ts` and the non-test `.ts` under `src/fixtures/`) and `lib/api.ts` (the MSI fallback name), each line
 * that is not a comment. KNOWN BLIND SPOT: a line is a comment if it starts
 * with `//`, `*`, `/*`, `{/*` or `<!--`, so a line of JSX *text* that starts
 * with `*` is skipped; whether a `*` line is a block comment's continuation or
 * JSX text cannot be told line by line without parsing, so it is left. The
 * letters may stay only inside an identifier project 4 keeps, each named here
 * (its design §1.2 and the coordinator's resolution of 2026-11-24); anything
 * else is a name a person reads, and it says Novieri.
 */
const KEPT_LOWER: readonly RegExp[] = [
  /itms\.(client|lang|fixtures|session|session-ended|sidebar\.collapsed|microsoft\.return)\b/g, // browser storage keys (decision D15)
  /__Host-itms_[a-z]+/g, // the session and Entra cookies
  /itms-recovery-public:/g, // the off-site recovery key's format
  /itms_recover\.py/g, // the recovery tool, `scripts/itms_recover.py`
  /itms-rsp\.1/g, // the remote-session recording format
  /x-itms-[a-z-]+/gi, // the request headers, in any case
  /\.itms-probe\/|itms-platform/g, // the bucket prefixes project 4 keeps
  /itms-prototype(-v3)?\.html/g, // the dated design prototypes, by file name
];
/** One file keeps a stored value: the off-site target's default bucket prefix. */
const KEPT_IN_FILE: Readonly<Record<string, RegExp>> = {
  "src/modules/backup/OffsitePage.tsx": /prefix: target\?\.prefix \?\? "itms",/g,
  // the server's MCP client-id format (`"itms_" + token`, core/auth/oauth/clients.py): a wire value
  "src/modules/settings/fixtures.ts": /client_id: "itms_/g,
};
const CATALOGUES = [resolve(FRONTEND, "src/i18n/en"), resolve(FRONTEND, "src/i18n/es")];
const IS_TEST = /(\.test\.tsx?$|\/__tests__\/|\/src\/test\/)/;

/** The 1-based numbers of the lines whose copy says `itms` outside a kept identifier. */
export function lowerOffences(source: string, kept: RegExp | null = null): number[] {
  return source.split("\n").flatMap((line, at) => {
    if (COMMENT.test(line)) return [];
    let rest = kept ? line.replace(kept, "") : line;
    for (const pattern of KEPT_LOWER) rest = rest.replace(pattern, "");
    return rest.includes("itms") ? [at + 1] : [];
  });
}

describe("the old product name in lower case, where a person reads it", () => {
  it("is caught in copy and let through only in what project 4 keeps", () => {
    expect(lowerOffences('    addressPlaceholder: "itms@example.com",')).toEqual([1]);
    expect(lowerOffences("It looks like https://itms.example.com/i/ followed by 32 letters.")).toEqual([1]);
    expect(lowerOffences('saveBlob(blob, "itms-sign-in.cer")')).toEqual([1]);
    expect(lowerOffences("    publicKeyHelp: \"The line that starts with itms-recovery-public:\",")).toEqual([]);
    expect(lowerOffences("<pre>python3 itms_recover.py keygen --out recovery.key</pre>")).toEqual([]);
    expect(lowerOffences('const KEY = "itms.client";')).toEqual([]);
    expect(lowerOffences('const KEY = "itms.example.com";')).toEqual([1]);
    expect(lowerOffences("// the itms.example.com host, as it read on 2026-09-08")).toEqual([]);
    expect(lowerOffences('prefix: target?.prefix ?? "itms",')).toEqual([1]);
    expect(lowerOffences('prefix: target?.prefix ?? "itms",', KEPT_IN_FILE["src/modules/backup/OffsitePage.tsx"])).toEqual([]);
    // the settings fixtures keep the server's `client_id` format and nothing wider
    const settings = KEPT_IN_FILE["src/modules/settings/fixtures.ts"];
    expect(lowerOffences('app_id: "itms_x",', settings)).toEqual([1]);
    expect(lowerOffences('client_id: "itms_x",', settings)).toEqual([]);
  });

  it("reads the catalogues and the console's JSX", () => {
    const src = files(resolve(FRONTEND, "src"));
    const copy = [
      ...CATALOGUES.flatMap(files),
      ...src.filter((path) => path.endsWith(".tsx") && !IS_TEST.test(path)),
      ...src.filter((path) => /\/fixtures\.ts$/.test(path) || (/\/src\/fixtures\/[^/]+\.ts$/.test(path) && !IS_TEST.test(path))),
      resolve(FRONTEND, "src/lib/api.ts"),
    ];
    expect(copy.length).toBeGreaterThan(300);
    expect(copy).toContain(resolve(FRONTEND, "src/i18n/en/settings.ts"));
    expect(copy).toContain(resolve(FRONTEND, "src/modules/settings/MicrosoftTab.tsx"));
    const found = [...new Set(copy)].flatMap((path) => {
      const name = relative(FRONTEND, path);
      return lowerOffences(readFileSync(path, "utf8"), KEPT_IN_FILE[name] ?? null).map((line) => `${name}:${line}`);
    });
    expect(found, found.join("\n")).toEqual([]);
  });
});
