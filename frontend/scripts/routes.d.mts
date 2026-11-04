/** Types for `routes.mjs`, so `src/test/routes-covered.test.ts` imports it under `strict`. */
export declare const PARAMS: Readonly<Record<string, string>>;
export declare const EXCLUDED: Set<string>;
export declare function fillPath(pattern: string): string;
export declare function patternOf(route: string): string;
export declare const ROUTES: string[];
/** The client harmony chooses in the top bar (a UUID, `src/fixtures/core.ts`'s `CUSTOMER_ID`). */
export declare const CHOSEN_CLIENT: string;
