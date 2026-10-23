export type Language = "en" | "es";
export const LANGUAGES: readonly Language[] = ["en", "es"] as const;

export type Plural = { one: string; other: string; many?: string };
export type Leaf = string | Plural;
export interface Messages { [key: string]: Leaf | Messages }

type Join<K, P> = K extends string ? (P extends string ? `${K}.${P}` : never) : never;
/** Dot paths to every leaf; a Plural is one leaf. */
export type Paths<T> = T extends string
  ? never
  : T extends Plural
    ? never
    : { [K in keyof T & string]: T[K] extends string | Plural ? K : Join<K, Paths<T[K]>> }[keyof T & string];

/** The Spanish catalogue must have exactly the English one's shape: same keys,
 *  string where English has a string, a Plural where English has a Plural. */
export type Shape<T> = { [K in keyof T]: T[K] extends string ? string : T[K] extends Plural ? Plural : Shape<T[K]> };
