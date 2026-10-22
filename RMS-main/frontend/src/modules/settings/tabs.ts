/**
 * Settings' tab keys, and the link to one.
 *
 * THE ACTIVE TAB LIVES IN `?tab=<key>` (docs/design/cross-module-guidance.md
 * §0.1), so another page can send an operator straight to Settings -> Users,
 * Email or Health. A key the page does not know, or a tab this account is not
 * offered, falls back to Organization: the link is never a way round the tab
 * list, which is itself built from the account's permissions.
 *
 * In its own file so a CTA elsewhere can build the link without importing the
 * whole Settings page.
 */

export const SETTINGS_TABS = [
  "organization",
  "email",
  "roles",
  "users",
  "assistant",
  "ai-connections",
  "notifications",
  "sla",
  "health",
  "budget",
] as const;

export type SettingsTabKey = (typeof SETTINGS_TABS)[number];

export const DEFAULT_SETTINGS_TAB: SettingsTabKey = "organization";

export function isSettingsTab(value: string | null): value is SettingsTabKey {
  return value !== null && (SETTINGS_TABS as readonly string[]).includes(value);
}

/** `/settings?tab=users`, for a CTA's `to`. */
export function settingsHref(tab: SettingsTabKey): string {
  return tab === DEFAULT_SETTINGS_TAB ? "/settings" : `/settings?tab=${tab}`;
}
