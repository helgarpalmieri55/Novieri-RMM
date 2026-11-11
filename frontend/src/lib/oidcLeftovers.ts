/**
 * WHAT THE KEYCLOAK CONSOLE LEFT IN THIS BROWSER, REMOVED AT BOOT (final
 * review D1, Minor 10).
 *
 * `oidc-client-ts` kept the whole OIDC user -- access token, ID token and
 * possibly a refresh token -- in `sessionStorage["oidc.user:<authority>:
 * it-suite-web"]` (the deleted `lib/auth.ts`, its line 74), and its sign-in
 * state under other `oidc.*` keys. A tab left open across the deploy reloads
 * into this console with all of it still there. Nothing reads it, and the
 * tokens open no door any more (the console bearer is refused; the MCP door
 * checks the audience), but the ID token's personal data would stay readable
 * to any script on the page for the life of the tab.
 *
 * ONLY `oidc.*`, in both storages. The console's own keys (`itms.*`) are
 * untouched. Storage that throws -- Safari private browsing, a webview with
 * site data blocked -- held nothing to remove.
 */
export function forgetOidcLeftovers(): void {
  // EACH STORAGE IS READ INSIDE ITS OWN `try` (final re-review, M3): with site
  // data blocked, Chromium throws from the `window.localStorage` GETTER, and
  // thrown here, before `createRoot`, that was a blank page.
  for (const pick of [() => window.localStorage, () => window.sessionStorage]) {
    try {
      const storage = pick();
      const leftovers: string[] = [];
      for (let index = 0; index < storage.length; index += 1) {
        const key = storage.key(index);
        if (key !== null && key.startsWith("oidc.")) leftovers.push(key);
      }
      for (const key of leftovers) storage.removeItem(key);
    } catch {
      // Storage blocked: nothing was kept in it.
    }
  }
}
