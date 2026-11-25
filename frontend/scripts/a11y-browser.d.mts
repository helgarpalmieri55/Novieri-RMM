/** Types for what `a11y-browser.mjs` exports, so a vitest guard can import it under `strict`. */
/** The few Playwright calls a state makes, so a test can stand in for a page. */
export interface StatePage {
  getByRole(role: string, options: { name: RegExp | string }): { first(): { click(): Promise<void> } };
  locator(selector: string): {
    fill(value: string): Promise<void>;
    nth(index: number): { isDisabled(): Promise<boolean> };
  };
  keyboard: { press(key: string): Promise<void> };
  waitForTimeout(ms: number): Promise<void>;
}
/** The states the pass enters after the default render: `dialog`, `armed`, `palette`. */
export const STATES: Record<string, (page: StatePage) => Promise<void>>;
