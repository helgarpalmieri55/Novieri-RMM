import { translate, type MessageKey } from "./core";

/**
 * A record of labels that reads the catalogue WHEN A FIELD IS READ, not when
 * the module loads. A module-level `Record<string, string>` built from
 * `translate()` would freeze the language the page started in; an accessor per
 * field keeps the exported shape (`LABELS[code]`, `Object.keys(LABELS)`) while
 * a language switch shows on the next render. Never copy the values into
 * state or a memo.
 */
export function lazyLabels<K extends string>(keys: Readonly<Record<K, MessageKey>>): Readonly<Record<K, string>> {
  const out = {} as Record<K, string>;
  for (const [field, key] of Object.entries<MessageKey>(keys)) {
    Object.defineProperty(out, field, { enumerable: true, get: () => translate(key) });
  }
  return out;
}

/**
 * An object that keeps its plain fields (a badge variant, a flag) and reads
 * its WORDED fields from the catalogue when they are read. Same reason as
 * `lazyLabels`: an exported table of presentations built at module load would
 * freeze the language the page started in, and the worded fields are accessors
 * so a language switch shows on the next render. Never copy the worded values
 * into state or a memo.
 */
export function lazyFields<T extends object, F extends string>(
  base: T,
  fields: Readonly<Record<F, MessageKey>>,
): T & Readonly<Record<F, string>> {
  const out = { ...base } as T & Record<F, string>;
  for (const [field, key] of Object.entries<MessageKey>(fields)) {
    Object.defineProperty(out, field, { enumerable: true, get: () => translate(key) });
  }
  return out;
}
