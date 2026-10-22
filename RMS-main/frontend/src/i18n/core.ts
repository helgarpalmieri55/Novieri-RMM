import { en, type Catalog } from "./en";
import { es } from "./es";
import type { Language, Leaf, Paths, Plural } from "./types";

export type MessageKey = Paths<Catalog>;
const CATALOGS: Record<Language, unknown> = { en, es };
let active: Language = "en";
const listeners = new Set<() => void>();

export function getLanguage(): Language {
  return active;
}

export function setActiveLanguage(language: Language): void {
  if (language === active) return;
  active = language;
  if (typeof document !== "undefined") document.documentElement.lang = language;
  listeners.forEach((fn) => fn());
}

export function subscribe(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

function lookup(language: Language, key: string): Leaf | undefined {
  let node: unknown = CATALOGS[language];
  for (const part of key.split(".")) {
    if (node === null || typeof node !== "object") return undefined;
    node = (node as Record<string, unknown>)[part];
  }
  return node as Leaf | undefined;
}

function pick(leaf: Plural, count: number, language: Language): string {
  const rule = new Intl.PluralRules(language).select(count);
  if (rule === "one") return leaf.one;
  if (rule === "many" && leaf.many) return leaf.many;
  return leaf.other;
}

export function translate(key: MessageKey, vars: Record<string, string | number> = {}): string {
  const leaf = lookup(active, key) ?? lookup("en", key);
  if (leaf === undefined) return key;
  // A truncated key (reachable only through a cast) lands on a branch, not a leaf: fail soft.
  if (typeof leaf !== "string" && typeof (leaf as Plural).other !== "string") return key;
  const text = typeof leaf === "string" ? leaf : pick(leaf, Number(vars.count ?? 0), active);
  return text.replace(/\{(\w+)\}/g, (whole, name: string) =>
    Object.hasOwn(vars, name) ? String(vars[name]) : whole,
  );
}
