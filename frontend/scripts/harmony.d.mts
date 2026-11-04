/** Types for the pure helpers `harmony.mjs` exports, so a vitest guard can import them under `strict`. */
export type Rgba = [number, number, number, number];
export interface Allowlist {
  tokens: Map<string, string>;
  fontSizes: Set<string>;
  radii: Set<string>;
  colors: Rgba[];
  colorNames: Map<string, string>;
}
export interface Viewport {
  width: number;
  height: number;
  coarse: boolean;
  /** Walked with a client chosen in the top bar (`CHOSEN_CLIENT` in `itms.client`). */
  client: boolean;
}
/** What `CHROME_IN_PAGE` reads of the top bar. */
export interface ChromeProbe {
  toggle: { width: number; height: number } | null;
  profileRight: number | null;
  innerWidth: number;
  topbar?: boolean;
  selected?: string | null;
}
export declare const VIEWPORTS: Viewport[];
export declare function viewportLabel(vp: Viewport): string;
/** The client mark is drawn in the drawer toggle: a client chosen, on a handset (640 px). */
export declare function markDrawn(vp: Viewport): boolean;
/** `vp` adds the 60 px floor where `markDrawn(vp)`; without it only the 44-70 band is asked. */
export declare function chromeProblems(probe: ChromeProbe, vp?: Viewport): string[];
export declare function parseColor(text: string): Rgba | null;
export declare function readTokens(css: string): Map<string, string>;
export declare function buildAllowlist(css: string): Allowlist;
