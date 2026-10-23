/** Types for the pure helpers `harmony.mjs` exports, so a vitest guard can import them under `strict`. */
export type Rgba = [number, number, number, number];
export interface Allowlist {
  tokens: Map<string, string>;
  fontSizes: Set<string>;
  radii: Set<string>;
  colors: Rgba[];
  colorNames: Map<string, string>;
}
export declare function parseColor(text: string): Rgba | null;
export declare function readTokens(css: string): Map<string, string>;
export declare function buildAllowlist(css: string): Allowlist;
