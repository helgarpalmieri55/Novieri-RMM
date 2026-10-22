import { cleanup, render, type RenderOptions, type RenderResult } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach } from "vitest";
import { setActiveLanguage } from "./core";
import { en } from "./en";
import { es } from "./es";

/** Terms that are legitimately the same in both languages (product and
 *  platform names, loanwords). Shared by every module's Spanish test. */
export const SAME_IN_BOTH: string[] = [
  "Email",
  "OK",
  "Windows",
  "macOS",
  "Linux",
  "ITMS",
  "Toeshee",
  "Keycloak",
  "Microsoft 365",
  "English",
  "Español",
  // The glossary's own Spanish word for a ticket. The shell's search noun is
  // English "ticket" / Spanish "tickets", which would otherwise flag it.
  "ticket",
];

// Reset to English after every test that imported this file. The component
// tree is unmounted FIRST: a language change notifies mounted subscribers,
// and a re-render outside act() is a console.error, which the global gate in
// src/test/setup.ts turns into a failure. cleanup() is idempotent, so the
// setup file's own cleanup() afterwards is a no-op.
afterEach(() => {
  cleanup();
  setActiveLanguage("en");
});

/** Sets Spanish, renders, and (via the afterEach above) resets to English. */
export function renderInSpanish(ui: ReactElement, options?: RenderOptions): RenderResult {
  setActiveLanguage("es");
  return render(ui, options);
}

type Tree = { [key: string]: unknown };

/** Every leaf string in a catalogue, keyed by dot path; a plural form is
 *  keyed `path#form` so each form is compared with its own counterpart. */
export function leaves(node: unknown, path = "", out: Map<string, string> = new Map()): Map<string, string> {
  if (typeof node === "string") {
    out.set(path, node);
  } else if (node !== null && typeof node === "object") {
    const tree = node as Tree;
    const isPlural = typeof tree.other === "string" && typeof tree.one === "string";
    for (const [key, child] of Object.entries(tree)) {
      if (isPlural) {
        if (typeof child === "string") out.set(`${path}#${key}`, child);
      } else {
        leaves(child, path ? `${path}.${key}` : key, out);
      }
    }
  }
  return out;
}

/** The static text of a leaf: placeholders (`{name}`) and rich-text tags
 *  (`<link>`, `</link>`) are separators, never part of the fragment. */
function fragments(value: string): string[] {
  return value.split(/\{\w+\}|<\/?\w+>/).map((f) => f.trim());
}

const MIN_LENGTH = 4;

/** Leaf paths whose Spanish value equals the English one (each plural form on
 *  its own, keyed `path#form`). Pure: takes the two catalogues. */
export function identicalLeaves(english: unknown, spanish: unknown): string[] {
  const es = leaves(spanish);
  return [...leaves(english)].filter(([path, value]) => es.get(path) === value).map(([path]) => path);
}

/** The English fragments (>= 4 chars) a Spanish render must not show: those
 *  of English leaves whose Spanish counterpart differs, minus any fragment
 *  the Spanish catalogue also contains verbatim. A leaf identical in both
 *  languages contributes nothing here; that is audited separately, by
 *  catalogue.identical.test.ts, so a pasted-English Spanish value cannot hide.
 *  Pure, longest first. */
export function englishFragments(english: unknown, spanish: unknown): string[] {
  const enLeaves = leaves(english);
  const esLeaves = leaves(spanish);
  const inSpanish = new Set<string>();
  // HOMOGRAPHS: a single English word that is also a whole word somewhere in
  // the Spanish text ("uses" in "No uses una cuenta") cannot be told apart from
  // that Spanish by a whole-word match, so it is not a usable English signal.
  // Only prose counts: see the `.` / `@` rule below.
  const spanishWords = new Set<string>();
  for (const value of esLeaves.values()) {
    for (const f of fragments(value)) {
      inSpanish.add(f);
      // A token holding an `@` or an interior `.` is a code, a permission or an
      // address (`devices.read`), not Spanish prose: its words must not
      // whitelist the English word "devices" everywhere else. The rest of the
      // fragment still counts, and so does a sentence-final period.
      for (const token of f.split(/\s+/)) {
        if (token.includes("@") || /[\p{L}\p{N}]\.[\p{L}\p{N}]/u.test(token)) continue;
        for (const word of token.split(/[^\p{L}\p{N}]+/u)) if (word) spanishWords.add(word);
      }
    }
  }
  const found = new Set<string>();
  for (const [path, value] of enLeaves) {
    if (esLeaves.get(path) === value) continue;
    for (const f of fragments(value)) {
      if (f.length < MIN_LENGTH || inSpanish.has(f)) continue;
      // A fragment that is one word may carry the punctuation its leaf ended with
      // ("host." from "Install queued on {count} host."); the word is what is compared.
      const word = f.replace(/^[^\p{L}\p{N}]+|[^\p{L}\p{N}]+$/gu, "");
      if (!/\s/.test(f) && word !== "" && spanishWords.has(word)) continue; // a homograph, see above
      found.add(f);
    }
  }
  return [...found].sort((a, b) => b.length - a.length);
}

let cache: string[] | undefined;
function catalogueFragments(): string[] {
  return (cache ??= englishFragments(en, es));
}

/** MATCHING RULE: a fragment matches only as a whole word or phrase, case
 *  sensitive: the characters on either side must not be a letter or digit
 *  (Unicode-aware), so English "Save" is not found inside "Guardado" or
 *  "Saved", and "Cancel" is not found inside "Cancelar". A dotted identifier is
 *  not prose either: "update" is not found inside the job type `agent.update`
 *  or the permission `agent.update.read` (a dot with a letter or digit on its
 *  far side). A full stop that ends a sentence is not such a dot. */
const phraseMatchers = new Map<string, RegExp>();
function containsPhrase(haystack: string, phrase: string): boolean {
  // Most fragments are nowhere in the text: a plain substring test settles that
  // without compiling an expression for each of thousands of fragments.
  if (!haystack.includes(phrase)) return false;
  let matcher = phraseMatchers.get(phrase);
  if (!matcher) {
    const escaped = phrase.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    matcher = new RegExp(`(?<![\\p{L}\\p{N}])${escaped}(?![\\p{L}\\p{N}])`, "gu");
    phraseMatchers.set(phrase, matcher);
  }
  for (const match of haystack.matchAll(matcher)) {
    const end = match.index + match[0].length;
    const dottedBefore = /[\p{L}\p{N}]\.$/u.test(haystack.slice(Math.max(0, match.index - 2), match.index));
    const dottedAfter = /^\.[\p{L}\p{N}]/u.test(haystack.slice(end, end + 2));
    if (!dottedBefore && !dottedAfter) return true;
  }
  return false;
}

/** Text nodes are joined with a newline (not concatenated, as `textContent`
 *  does) so adjacent elements cannot fuse two words into one. */
function visibleText(container: HTMLElement): string[] {
  const texts: string[] = [];
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    if (node.nodeValue?.trim()) texts.push(node.nodeValue);
  }
  const elements = [container, ...Array.from(container.querySelectorAll<HTMLElement>("*"))];
  for (const el of elements) {
    for (const attr of ["aria-label", "aria-description", "placeholder", "title", "alt"]) {
      const value = el.getAttribute(attr);
      if (value) texts.push(value);
    }
  }
  return texts;
}

/** Fails listing every English catalogue value found in the container's text
 *  or its aria-label / aria-description / placeholder / title / alt attributes. `allow` adds
 *  module-specific same-in-both terms to SAME_IN_BOTH. */
export function expectNoEnglish(container: HTMLElement, allow: string[] = []): void {
  const allowed = new Set([...SAME_IN_BOTH, ...allow]);
  const haystack = visibleText(container).join("\n");
  const leaked = catalogueFragments().filter((f) => !allowed.has(f) && containsPhrase(haystack, f));
  if (leaked.length > 0) {
    throw new Error(
      `English text found in a Spanish render (${leaked.length}):\n` +
        leaked.map((f) => `  - ${JSON.stringify(f)}`).join("\n"),
    );
  }
}
