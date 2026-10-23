# Spanish Language Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The whole console, the self-service portal, the server's messages,
notification emails and the Keycloak sign-in pages in English or Spanish, chosen
from the account menu and saved on the person's account.

**Architecture:**
- **Frontend:** a dependency-free `src/i18n/` module. Typed per-module
  catalogues; English is the source, and a Spanish gap fails `tsc`. A React
  context holds the active language.
- **Server:** a gettext-style `tr(english_template, **params)` driven by a
  request-scoped context variable set from `Accept-Language`. The Spanish
  catalogue is a dict, and AST tests keep it complete.
- **Emails:** rendered in the recipient's language.
- **Keycloak:** follows a synced `locale` user attribute.

**Tech Stack:** React 18 + TypeScript strict + Vite + vitest; FastAPI +
Python 3.12 + pytest; Keycloak 26.2; Playwright (harmony).

**Spec:** `docs/superpowers/specs/2026-10-09-spanish-language-design.md`
(approved 2026-10-09). Read it before any task.

## Global Constraints

- Supported languages: exactly `en` and `es`. The console writes only these
  two values to `users.locale`.
- Language resolution order: saved `me.locale` (primary subtag) → `localStorage["itms.lang"]` → `navigator.language` starting `es` → `en`.
- **English text must stay byte-identical** to today's. The ~7,000 existing
  test assertions on English must pass unedited. If an existing test needs
  editing to pass, the English changed: stop and fix the catalogue instead.
- No new npm or Python dependency.
- Spanish is neutral `es`, with the glossary at the top of
  `src/i18n/es/index.ts`.
- Date formatting: English keeps `en-GB`. Spanish uses `es`.
- Every API request carries `Accept-Language: en|es`.
- Server: `tr()` formats with `str.format` only. The Spanish catalogue lives
  in `backend/app/core/i18n/es.py`.
- Audit entries are never translated or rewritten (hash chain).
- The agent (`agent/`) is not touched.
- Frontend checks: `npx tsc --noEmit`, `npx vitest run`, `npm run build`,
  `node scripts/harmony.mjs --strict-fixtures` (and `--lang es`).
- Backend checks: `.venv/bin/ruff check app tests`, `.venv/bin/mypy app`,
  full pytest.
- Never run prettier. Never run `playwright install`.
- Commit messages end with:
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_019gyzgtDWmPL7HFfYK1eg89
  ```

---

## File map

**Frontend**

`src/i18n/`:

| File | Role |
|---|---|
| `types.ts` | `Language`, `Catalog`, `Messages`, key-path types |
| `core.ts` | `translate()`, plural and interpolation, the module language getter/setter |
| `context.tsx` | `I18nProvider`, `useT()`, `useLanguage()` |
| `Trans.tsx` | rich-text strings |
| `resolve.ts` | language resolution (§1.1 of the spec) |
| `en/index.ts`, `es/index.ts` | assembled catalogues |
| `en/<area>.ts`, `es/<area>.ts` | one per module (plus `common`, `shell`, `components`, `enums`, `validation`, `time`) |
| `test-utils.ts` | `renderInSpanish()`, `expectNoEnglish()` |

Files changed:
- `src/shell/AccountMenu.tsx`: the language row and submenu.
- `src/modules/profile/ProfilePage.tsx`: the Language select.
- `src/lib/api.ts`: `#headers()` builder and `Accept-Language` on every path.
- `src/modules/setup/SetupPage.tsx`: its two raw `fetch` calls.
- `src/lib/datetime.ts`, `src/lib/format.ts`, `src/lib/labels.ts`: localised.
- `src/App.tsx`: the provider, and syncing `me.locale`.
- Every module's `.tsx`/`.ts` with user-visible text: strings move to the
  catalogues.
- `scripts/harmony.mjs`: `--lang`.
- `.github/workflows/ci.yml`: harmony matrix over `en` and `es`.

**Backend**

`app/core/i18n/`:

| File | Role |
|---|---|
| `__init__.py` | `tr`, `locale_var`, `use_locale`, `SUPPORTED` |
| `es.py` | `CATALOG: dict[str, str]` |

Also:
- `app/api/locale_middleware.py`: `Accept-Language` → `locale_var`.
- `app/main.py`: register it.

Tests:
- `tests/test_i18n_core.py`
- `tests/test_i18n_catalog.py`: completeness, placeholders, stale entries, bypass.

Files changed:
- Every module that raises user-facing messages.
- `app/modules/notification/delivery.py`: recipient language.
- `app/api/v1/me.py`: `locale` format check and Keycloak sync.
- `app/modules/user_admin/kc_client.py`: `set_locale_attribute`.

**Deploy:**
- `deploy/keycloak/realm-it-suite.json`: i18n keys.
- `deploy/keycloak/init-clients.py`: converge them.
- `deploy/keycloak/themes/itms/login/messages/messages_es.properties`.

---

### Task 1: Frontend i18n core

**Files:**
- Create: `frontend/src/i18n/types.ts`, `core.ts`, `context.tsx`, `Trans.tsx`, `resolve.ts`, `en/index.ts`, `en/common.ts`, `es/index.ts`, `es/common.ts`
- Test: `frontend/src/i18n/i18n.test.tsx`

**Interfaces:**
- Produces:
  - `type Language = "en" | "es"`;
  - `translate(key: MessageKey, vars?: Record<string, string | number>): string`;
  - `useT(): typeof translate` (subscribes the component to language changes);
  - `useLanguage(): { language: Language; setLanguage(l: Language): void }`;
  - `<I18nProvider initial?: Language>`;
  - `<Trans k={key} vars={…} components={{ name: <el/> }} />`;
  - `resolveLanguage(saved: string | null | undefined, stored: string | null, browser: string | undefined): Language`;
  - `getLanguage(): Language` (module-level, for non-React helpers).
- Catalogue shape: nested objects of `string | { one: string; other: string; many?: string }`. `MessageKey` is the union of dot paths to leaves.

- [ ] **Step 1: Write the failing tests** — `frontend/src/i18n/i18n.test.tsx`:

```tsx
import { act, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { I18nProvider, useLanguage, useT } from "./context";
import { resolveLanguage } from "./resolve";
import { translate, setActiveLanguage } from "./core";
import { en } from "./en";
import { es } from "./es";

function Probe() {
  const t = useT();
  const { setLanguage } = useLanguage();
  return (
    <div>
      <span data-testid="out">{t("common.actions.save")}</span>
      <button onClick={() => setLanguage("es")}>go</button>
    </div>
  );
}

describe("i18n core", () => {
  it("defaults to English without a provider, so existing tests need no wrapper", () => {
    render(<Probe />);
    expect(screen.getByTestId("out")).toHaveTextContent(en.common.actions.save);
  });

  it("switches every consumer at once, without remounting", () => {
    render(<I18nProvider initial="en"><Probe /></I18nProvider>);
    act(() => screen.getByText("go").click());
    expect(screen.getByTestId("out")).toHaveTextContent(es.common.actions.save);
  });

  it("interpolates named placeholders", () => {
    setActiveLanguage("en");
    expect(translate("common.count.selected", { count: 3 })).toBe("3 selected");
  });

  it("chooses the plural form with Intl.PluralRules", () => {
    setActiveLanguage("es");
    expect(translate("common.count.items", { count: 1 })).toBe("1 elemento");
    expect(translate("common.count.items", { count: 2 })).toBe("2 elementos");
    setActiveLanguage("en");
  });

  it("resolves the language in the spec's order", () => {
    expect(resolveLanguage("es", "en", "en-US")).toBe("es");
    expect(resolveLanguage("en-GB", "es", "es-ES")).toBe("en");
    expect(resolveLanguage(null, "es", "en-US")).toBe("es");
    expect(resolveLanguage(null, null, "es-CO")).toBe("es");
    expect(resolveLanguage("fr", null, "fr-FR")).toBe("en");
  });

  it("has the same placeholders in every Spanish string as in its English one", () => {
    const leaves = (o: unknown, path = ""): [string, string][] =>
      typeof o === "string"
        ? [[path, o]]
        : Object.entries(o as Record<string, unknown>).flatMap(([k, v]) =>
            leaves(v, path ? `${path}.${k}` : k),
          );
    const vars = (s: string) => [...s.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort().join(",");
    const esLeaves = new Map(leaves(es));
    for (const [path, text] of leaves(en)) {
      const base = path.replace(/\.(one|other|many)$/, "");
      const spanish = esLeaves.get(path) ?? esLeaves.get(`${base}.other`);
      expect(spanish, path).toBeDefined();
      expect(vars(spanish!), path).toBe(vars(text));
    }
  });
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd frontend && npx vitest run src/i18n/i18n.test.tsx`
Expected: FAIL, "Cannot find module './context'".

- [ ] **Step 3: Implement.**

`frontend/src/i18n/types.ts`:

```ts
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
```

`frontend/src/i18n/en/common.ts`:

```ts
export const common = {
  actions: { save: "Save", cancel: "Cancel", close: "Close", retry: "Retry" },
  count: {
    selected: "{count} selected",
    items: { one: "{count} item", other: "{count} items" },
  },
  language: { label: "Language", english: "English", spanish: "Español" },
} as const;
```

`frontend/src/i18n/es/common.ts`:

```ts
import type { Shape } from "../types";
import type { common as en } from "../en/common";

export const common: Shape<typeof en> = {
  actions: { save: "Guardar", cancel: "Cancelar", close: "Cerrar", retry: "Reintentar" },
  count: {
    selected: "{count} seleccionados",
    items: { one: "{count} elemento", other: "{count} elementos" },
  },
  language: { label: "Idioma", english: "English", spanish: "Español" },
};
```

`frontend/src/i18n/en/index.ts`:

```ts
import { common } from "./common";
export const en = { common } as const;
export type Catalog = typeof en;
```

`frontend/src/i18n/es/index.ts`:

```ts
/**
 * GLOSSARY: one place to change a term (spec §6). Every Spanish catalogue
 * uses these choices:
 *   device → dispositivo     enroll → inscribir     agent → agente
 *   push install → instalación remota     tenant/organization → organización
 *   job → tarea     ticket → ticket     rollout → despliegue
 *   approve → aprobar     review → revisión     subnet → subred
 */
import type { Shape } from "../types";
import type { Catalog } from "../en";
import { common } from "./common";

export const es: Shape<Catalog> = { common };
```

`frontend/src/i18n/core.ts`:

```ts
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
  const text = typeof leaf === "string" ? leaf : pick(leaf, Number(vars.count ?? 0), active);
  return text.replace(/\{(\w+)\}/g, (whole, name: string) =>
    name in vars ? String(vars[name]) : whole,
  );
}
```

`frontend/src/i18n/context.tsx`:

```tsx
import { createContext, useCallback, useContext, useEffect, useMemo, useState, useSyncExternalStore, type ReactNode } from "react";
import { getLanguage, setActiveLanguage, subscribe, translate } from "./core";
import type { Language } from "./types";

const STORAGE_KEY = "itms.lang";

interface Ctx { language: Language; setLanguage: (l: Language) => void; onChange?: (l: Language) => void }
const LanguageContext = createContext<Ctx | null>(null);

/** Re-renders the caller when the language changes, and returns `translate`.
 *  EVERY component that renders translated text calls this, including one
 *  that only calls a helper which uses `translate` internally: the hook is
 *  what subscribes it to the switch. */
export function useT(): typeof translate {
  useSyncExternalStore(subscribe, getLanguage, getLanguage);
  return translate;
}

export function useLanguage(): { language: Language; setLanguage: (l: Language) => void } {
  const language = useSyncExternalStore(subscribe, getLanguage, getLanguage);
  const ctx = useContext(LanguageContext);
  const setLanguage = useCallback(
    (l: Language) => {
      try { localStorage.setItem(STORAGE_KEY, l); } catch { /* private mode */ }
      setActiveLanguage(l);
      ctx?.onChange?.(l);
    },
    [ctx],
  );
  return { language, setLanguage };
}

export function readStoredLanguage(): string | null {
  try { return localStorage.getItem(STORAGE_KEY); } catch { return null; }
}

export function I18nProvider({ initial, onChange, children }: { initial?: Language; onChange?: (l: Language) => void; children: ReactNode }) {
  const [first] = useState(initial);
  useEffect(() => { if (first) setActiveLanguage(first); }, [first]);
  const value = useMemo<Ctx>(() => ({ language: getLanguage(), setLanguage: setActiveLanguage, onChange }), [onChange]);
  return <LanguageContext.Provider value={value}>{children}</LanguageContext.Provider>;
}
```

`frontend/src/i18n/resolve.ts`:

```ts
import type { Language } from "./types";

function primary(tag: string | null | undefined): string {
  return (tag ?? "").toLowerCase().split("-")[0] ?? "";
}

/** Spec §1.1: the account's saved value, then this browser's last choice,
 *  then the browser's language, then English. */
export function resolveLanguage(saved: string | null | undefined, stored: string | null, browser: string | undefined): Language {
  for (const candidate of [saved, stored]) {
    const p = primary(candidate);
    if (p === "en" || p === "es") return p;
    if (candidate) return "en";
  }
  return primary(browser) === "es" ? "es" : "en";
}
```

`frontend/src/i18n/Trans.tsx`:

```tsx
import { cloneElement, Fragment, type ReactElement } from "react";
import type { MessageKey } from "./core";
import { useT } from "./context";

/** `<link>text</link>` markers in the catalogue string become the matching
 *  element from `components`, with the inner text as its child. No HTML is
 *  parsed; an unknown marker renders as plain text. */
export function Trans({ k, vars, components }: { k: MessageKey; vars?: Record<string, string | number>; components: Record<string, ReactElement> }) {
  const t = useT();
  const parts = t(k, vars).split(/(<(\w+)>.*?<\/\2>)/g);
  const out: ReactElement[] = [];
  for (let i = 0; i < parts.length; i += 1) {
    const part = parts[i] ?? "";
    const match = /^<(\w+)>(.*)<\/\1>$/.exec(part);
    if (match && components[match[1]!]) {
      out.push(cloneElement(components[match[1]!]!, { key: i }, match[2]));
      i += 1; // skip the captured tag name
    } else if (part) {
      out.push(<Fragment key={i}>{part}</Fragment>);
    }
  }
  return <>{out}</>;
}
```

- [ ] **Step 4: Run the tests and `tsc`**

Run: `cd frontend && npx vitest run src/i18n && npx tsc --noEmit`
Expected: PASS. Then delete a key from `es/common.ts` and confirm `tsc`
fails with a missing-property error. Restore it.

- [ ] **Step 5: Commit** — `feat(i18n): typed English/Spanish catalogues, useT, provider and language resolution`

---

### Task 2: The switcher, the saved preference and the Accept-Language header

**Files:**
- Modify: `frontend/src/shell/AccountMenu.tsx`, `frontend/src/App.tsx`, `frontend/src/modules/profile/ProfilePage.tsx`, `frontend/src/lib/api.ts`, `frontend/src/modules/setup/SetupPage.tsx`
- Create: `frontend/src/i18n/en/shell.ts`, `frontend/src/i18n/es/shell.ts` (registered in both indexes)
- Test: `frontend/src/shell/AccountMenu.language.test.tsx`, `frontend/src/lib/api.language.test.ts`

**Interfaces:**
- Consumes: Task 1's `useT`, `useLanguage`, `I18nProvider`, `resolveLanguage`, `readStoredLanguage`, `getLanguage`.
- Produces:
  - `AccountMenuProps` unchanged; the menu reads the language itself;
  - `ApiClient` sends `Accept-Language: getLanguage()` on every request;
  - `App.tsx` saves the language with `api.updateMe({ locale })` on change.

- [ ] **Step 1: Write the failing tests.**

`frontend/src/shell/AccountMenu.language.test.tsx`:

```tsx
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { AccountMenu } from "./AccountMenu";
import { I18nProvider } from "../i18n/context";
import { setActiveLanguage } from "../i18n/core";

const user = { name: "Alex Morgan", email: "alex@example.com", role: "org-admin" };

function renderMenu(onChange = vi.fn()) {
  render(
    <MemoryRouter>
      <I18nProvider initial="en" onChange={onChange}>
        <AccountMenu user={user} onSignOut={vi.fn()} />
      </I18nProvider>
    </MemoryRouter>,
  );
  return onChange;
}

describe("account menu: language", () => {
  it("puts the language row between Settings and Sign out", async () => {
    renderMenu();
    await userEvent.click(screen.getByRole("button", { name: /Alex Morgan/ }));
    const items = within(screen.getByRole("menu")).getAllByRole("menuitem").map((el) => el.textContent);
    expect(items).toEqual(["Your profile", "Settings", "Language: English", "Sign out"]);
  });

  it("switches to Spanish, re-labels the menu and reports the choice to be saved", async () => {
    const onChange = renderMenu();
    await userEvent.click(screen.getByRole("button", { name: /Alex Morgan/ }));
    await userEvent.click(screen.getByRole("menuitem", { name: "Language: English" }));
    await userEvent.click(screen.getByRole("menuitemradio", { name: "Español" }));
    expect(onChange).toHaveBeenCalledWith("es");
    await userEvent.click(screen.getByRole("button", { name: /Alex Morgan/ }));
    expect(screen.getByRole("menuitem", { name: "Idioma: Español" })).toBeInTheDocument();
    setActiveLanguage("en");
  });

  it("marks the current language as checked", async () => {
    renderMenu();
    await userEvent.click(screen.getByRole("button", { name: /Alex Morgan/ }));
    await userEvent.click(screen.getByRole("menuitem", { name: "Language: English" }));
    expect(screen.getByRole("menuitemradio", { name: "English" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("menuitemradio", { name: "Español" })).toHaveAttribute("aria-checked", "false");
  });
});
```

`frontend/src/lib/api.language.test.ts`:

```ts
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiClient } from "./api";
import { setActiveLanguage } from "../i18n/core";

function walk(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) return walk(p);
    return /\.(ts|tsx)$/.test(name) && !/\.test\./.test(name) ? [p] : [];
  });
}

describe("Accept-Language", () => {
  afterEach(() => { vi.restoreAllMocks(); setActiveLanguage("en"); });

  it("is sent on every request, in the active language", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("{}", { status: 200 }));
    setActiveLanguage("es");
    await new ApiClient(() => "tok", () => {}).me();
    const headers = new Headers(fetchMock.mock.calls[0]![1]!.headers);
    expect(headers.get("Accept-Language")).toBe("es");
  });

  it("no source file builds request headers without the shared builder", () => {
    // Every `new Headers(` outside the builder is a raw fetch path that would
    // silently send no language.
    const offenders = walk("src").filter((file) => {
      const text = readFileSync(file, "utf8");
      return /new Headers\(/.test(text) && !/requestHeaders\(/.test(text);
    });
    expect(offenders).toEqual([]);
  });
});
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd frontend && npx vitest run src/shell/AccountMenu.language.test.tsx src/lib/api.language.test.ts`
Expected: FAIL (no language row; no Accept-Language).

- [ ] **Step 3: Implement.**

1. `en/shell.ts` adds these keys under `shell.accountMenu`, and `es/shell.ts`
   their Spanish (`Tu perfil`, `Configuración`, `Idioma: {language}`,
   `Cerrar sesión`, `No se pudo guardar el idioma.`):
   - `yourProfile: "Your profile"`
   - `settings: "Settings"`
   - `language: "Language: {language}"`
   - `signOut: "Sign out"`
   - `saveFailed: "Your language changed here, but could not be saved to your account."`

   Register `shell` in both indexes.
2. `AccountMenu.tsx`:
   - call `const t = useT()` and `const { language, setLanguage } = useLanguage()`;
   - replace the three literals with `t("shell.accountMenu.…")`;
   - insert, between Settings and Sign out:

```tsx
<button
  className="menu-item"
  role="menuitem"
  type="button"
  aria-haspopup="menu"
  aria-expanded={languageOpen}
  onClick={() => setLanguageOpen((v) => !v)}
>
  {t("shell.accountMenu.language", { language: language === "es" ? t("common.language.spanish") : t("common.language.english") })}
</button>
{languageOpen ? (
  <div className="account-menu-sub" role="menu" aria-label={t("common.language.label")}>
    {(["en", "es"] as const).map((l) => (
      <button
        key={l}
        className="menu-item"
        role="menuitemradio"
        aria-checked={language === l}
        type="button"
        onClick={() => { setLanguage(l); setLanguageOpen(false); close(true); }}
      >
        {l === "es" ? t("common.language.spanish") : t("common.language.english")}
      </button>
    ))}
  </div>
) : null}
```

   Add `const [languageOpen, setLanguageOpen] = useState(false);`. Extend the
   arrow-key query in `move` to `'[role="menuitem"], [role="menuitemradio"]'`.
   Add `.account-menu-sub { padding-left: var(--space-3); }` to `shell.css`,
   using an existing spacing token: harmony checks tokens.
3. `api.ts`:
   - add the exported builder and use it in `request()` and in every raw-fetch
     method (all 18 `Authorization` sets);
   - replace each `new Headers(...)`/`headers.set("Authorization", …)` pair
     with `requestHeaders(this.getToken(), init.headers)`, keeping any
     `Content-Type` the method sets itself;
   - `request()` keeps its `Content-Type: application/json`.

```ts
import { getLanguage } from "../i18n/core";

/** The one place request headers are built: bearer and language. */
export function requestHeaders(token: string | null | undefined, extra?: HeadersInit): Headers {
  const headers = new Headers(extra);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  headers.set("Accept-Language", getLanguage());
  return headers;
}
```

4. `SetupPage.tsx:64,146`: use `requestHeaders(null, …)` for its unauthenticated calls.
5. `App.tsx`:
   - wrap the router in `<I18nProvider initial={resolveLanguage(undefined, readStoredLanguage(), navigator.language)} onChange={saveLanguage}>`;
   - `saveLanguage = (l) => api.updateMe({ locale: l }).catch(() => toast(translate("shell.accountMenu.saveFailed")))`;
   - when the `["me"]` query resolves with `me.locale`, call
     `setActiveLanguage(resolveLanguage(me.locale, readStoredLanguage(), navigator.language))`
     inside a `useEffect` on `me?.locale`.
6. `ProfilePage.tsx`:
   - replace the free-text "Locale" input (273–283) with a `<select>` labelled
     `t("common.language.label")` holding `en`/`es`;
   - its value is `resolveLanguage(form.locale, null, undefined)`;
   - on save it sends the choice and calls `setLanguage`;
   - update `ProfilePage.test.tsx:81`, which uses `getByLabelText("Locale")`,
     to `getByLabelText("Language")`. This is the one deliberate English
     change in the plan: the spec replaces that field (§1, point 4).

- [ ] **Step 4: Run the tests**

Run: `cd frontend && npx vitest run src/shell src/lib src/modules/profile src/modules/setup && npx tsc --noEmit`
Expected: PASS, including the existing `AppShell.test.tsx` account-menu tests unchanged.

- [ ] **Step 5: Commit** — `feat(i18n): language row in the account menu, saved to the account, Accept-Language on every request`

---

### Task 3: Dates, numbers and enum labels follow the language

**Files:**
- Modify: `frontend/src/lib/datetime.ts`, `frontend/src/lib/format.ts`, `frontend/src/lib/labels.ts`, `frontend/src/modules/budget/money.ts`, `frontend/src/modules/jobs/JobsPage.tsx:122,129`, `frontend/src/modules/change/ChangeCalendar.tsx:18`, `frontend/src/modules/ipam/ipamState.ts:1208`, `frontend/src/modules/agent-push/pushState.ts:126`, `frontend/src/modules/devices/ApproveAllDialog.tsx:168`, `frontend/src/modules/agent-push/PushRunsPage.tsx:51`
- Create: `frontend/src/i18n/en/time.ts`, `es/time.ts`, `en/enums.ts`, `es/enums.ts`
- Test: `frontend/src/lib/datetime.language.test.ts`, `frontend/src/lib/enums.coverage.test.ts`

**Interfaces:**
- Consumes: `getLanguage()`, `translate()`.
- Produces:
  - `intlLocale(): "en-GB" | "es"`, used by every formatter;
  - `humaniseEnum(type: string, value: string): string` now looks up
    `enums.<type>.<value>` first. Keep the existing signature; if today's
    signature is `(value)` only, add an optional `type` parameter and pass it
    at the call sites this task touches.

- [ ] **Step 1: Write the failing tests.**

`datetime.language.test.ts`:

```ts
import { afterEach, describe, expect, it } from "vitest";
import { describeWhen, formatDate, formatDateTime } from "./datetime";
import { setActiveLanguage } from "../i18n/core";

const ISO = "2026-10-09T14:05:00Z";
describe("dates follow the language", () => {
  afterEach(() => setActiveLanguage("en"));
  it("English is unchanged (en-GB)", () => {
    expect(formatDate(ISO, "UTC")).toBe("09 Oct 2026");
  });
  it("Spanish uses es", () => {
    setActiveLanguage("es");
    expect(formatDate(ISO, "UTC")).toBe("09 oct 2026");
    expect(formatDateTime(ISO, "UTC")).toContain("14:05");
  });
  it("relative words are translated", () => {
    setActiveLanguage("es");
    const now = Date.parse(ISO) + 5 * 60_000;
    expect(describeWhen(ISO, now)).toBe("hace 5 minutos");
    expect(describeWhen(ISO, Date.parse(ISO) + 10_000)).toBe("ahora mismo");
  });
});
```

`enums.coverage.test.ts`:
- parse `src/lib/api.ts` for string-literal union types used as enums (`export type X = "a" | "b" | …`);
- for every union the console renders through `humaniseEnum`, assert
  `enums.<typeName>.<value>` exists in `en` and in `es`;
- the list of rendered types is the set of `humaniseEnum("<type>", …)` call
  sites found by scanning `src/`.

```ts
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { en } from "../i18n/en";
import { es } from "../i18n/es";

const walk = (d: string): string[] =>
  readdirSync(d).flatMap((n) => (statSync(join(d, n)).isDirectory() ? walk(join(d, n)) : /\.tsx?$/.test(n) && !/\.test\./.test(n) ? [join(d, n)] : []));

describe("every rendered enum value has a label in both languages", () => {
  const api = readFileSync("src/lib/api.ts", "utf8");
  const unions = new Map<string, string[]>();
  for (const m of api.matchAll(/export type (\w+) =\s*((?:\s*\|?\s*"[^"]+")+);/g)) {
    unions.set(m[1]!, [...m[2]!.matchAll(/"([^"]+)"/g)].map((x) => x[1]!));
  }
  const rendered = new Set<string>();
  for (const f of walk("src")) for (const m of readFileSync(f, "utf8").matchAll(/humaniseEnum\("(\w+)"/g)) rendered.add(m[1]!);
  for (const type of rendered) {
    it(type, () => {
      const values = unions.get(type);
      expect(values, `no union type ${type} in api.ts`).toBeDefined();
      for (const v of values!) {
        expect((en.enums as Record<string, Record<string, string>>)[type]?.[v], `${type}.${v} (en)`).toBeDefined();
        expect((es.enums as Record<string, Record<string, string>>)[type]?.[v], `${type}.${v} (es)`).toBeDefined();
      }
    });
  }
});
```

- [ ] **Step 2: Run to verify failure.** `npx vitest run src/lib/datetime.language.test.ts src/lib/enums.coverage.test.ts`. Expected: FAIL.

- [ ] **Step 3: Implement.**
  - `datetime.ts`: add `export function intlLocale() { return getLanguage() === "es" ? "es" : "en-GB"; }`. Replace both `"en-GB"` literals with `intlLocale()`.
  - Move "just now" and the "ago"/"in" phrases to `time.ts` as plurals:
    - `time.justNow: "just now"`;
    - `time.ago.minute: {one: "{count} minute ago", other: "{count} minutes ago"}`;
    - `time.in.minute: {one: "in {count} minute", other: "in {count} minutes"}`;
    - the same for `hour` and `day`.
    - Spanish: `ahora mismo`, `hace {count} minuto(s)`, `en {count} minuto(s)`.
  - Rewrite `describeWhen`'s `say` to call `translate(\`time.${delta < 0 ? "ago" : "in"}.${unit}\`, { count })`. The English output must stay identical: `5 minutes ago`, `in 3 days`.
  - `format.ts`: `formatCount` uses `toLocaleString(intlLocale())`. Move `describeSeconds` units to `time.units.*`.
  - Route the stray locales listed under **Files** through `intlLocale()`.
    `en-CA` in `ChangeCalendar.tsx:18` produces ISO `YYYY-MM-DD` keys and is
    not display: leave it, with a comment saying so.
  - `budget/money.ts`: `LOCALE` becomes a function returning `intlLocale()`.
  - `labels.ts` `humaniseEnum`: `translate(\`enums.${type}.${value}\`)`, falling back to today's humanise when the key is absent. Seed `en/enums.ts` with today's humanised output for every rendered type, so the English is byte-identical, and translate them in `es/enums.ts`.
  - Every `localeCompare(x)` in `src/` gains `, intlLocale()` as its second argument.

- [ ] **Step 4: Run.** `npx vitest run && npx tsc --noEmit`. Expected: PASS (the full suite, because the formatters are used everywhere).

- [ ] **Step 5: Commit** — `feat(i18n): dates, numbers, relative times and enum labels follow the language`

---

### Task 4: The test helpers and the source-scan guards

**Files:**
- Create: `frontend/src/i18n/test-utils.tsx`
- Modify: `frontend/src/lib/noVendorNames.test.ts`, `frontend/src/lib/noWireNames.test.ts`, `frontend/src/test/setup.ts`
- Test: `frontend/src/i18n/test-utils.test.tsx`

**Interfaces:**
- Produces:
  - `renderInSpanish(ui: ReactElement, options?)`: sets `es`, renders, and resets to `en` after the test;
  - `expectNoEnglish(container: HTMLElement, allow?: string[])`: fails listing every English catalogue value (≥4 chars, not in the shared allowlist or `allow`) found in `container.textContent` or in `aria-label`/`placeholder`/`title` attributes;
  - `SAME_IN_BOTH: string[]`, the shared allowlist: `"Email"`, `"OK"`, `"Windows"`, `"macOS"`, `"Linux"`, `"ITMS"`, `"Toeshee"`, `"Keycloak"`, `"Microsoft 365"`, `"English"`, `"Español"`.

- [ ] **Step 1: Write the failing test** — `test-utils.test.tsx`:

```tsx
import { describe, expect, it } from "vitest";
import { expectNoEnglish, renderInSpanish } from "./test-utils";
import { useT } from "./context";

function Good() { const t = useT(); return <p>{t("common.actions.save")}</p>; }
function Bad() { return <p>Save</p>; }

describe("expectNoEnglish", () => {
  it("passes a translated component", () => {
    const { container } = renderInSpanish(<Good />);
    expectNoEnglish(container);
  });
  it("catches a hard-coded English string", () => {
    const { container } = renderInSpanish(<Bad />);
    expect(() => expectNoEnglish(container)).toThrow(/Save/);
  });
});
```

- [ ] **Step 2: Run to verify it fails.** Expected: module not found.

- [ ] **Step 3: Implement**
  - `test-utils.tsx`:
    - collect the English leaves (strings, and each plural form with
      placeholders stripped to the static fragments of ≥4 chars);
    - search the container's text plus those three attributes;
    - `afterEach(() => setActiveLanguage("en"))` registered on import.
  - `src/test/setup.ts`: add `beforeEach(() => setActiveLanguage("en"))` inside the existing `beforeEach`, so one test's Spanish can never leak into the next.
  - Both scan tests: extend their file walk to include `src/i18n/en/**` and
    read string literals there too. Add a test asserting the walk visited at
    least one catalogue file, so an empty set cannot pass.

- [ ] **Step 4: Run.** `npx vitest run src/i18n src/lib`. Expected: PASS.

- [ ] **Step 5: Commit** — `test(i18n): expectNoEnglish, renderInSpanish, and catalogue-aware source scans`

---

### Tasks 5–11: Translate the console, module by module

Each task translates one group of modules **completely**: every user-visible
string in the group's `.tsx` and `.ts` files (state files build labels too).
The procedure is the same for every group and is spelled out here so each
task is self-contained.

**Procedure (identical for every group):**

1. For each module `m` in the group, create `src/i18n/en/<m>.ts` (camelCase
   export name, e.g. `agentPush`) and `src/i18n/es/<m>.ts` typed
   `Shape<typeof en>`, and register both in the indexes.
2. In every non-test `.tsx`/`.ts` of the module, replace each user-visible
   English literal with `t("<m>.<page>.<name>")`. That includes:
   - JSX text;
   - `title`, `label`, `placeholder`, `aria-label`, `description` and
     `alt` props;
   - toast and error strings;
   - confirm-dialog copy;
   - table column headers;
   - empty states;
   - strings built in state/helper files.

   Keys are grouped by page or component. Use `{name}` placeholders for
   values instead of string concatenation, and `Plural` for counts.
3. The English value **must equal the old literal byte for byte**: the
   module's existing tests must pass **without edits**.
4. Every component that renders translated text calls `useT()`. Non-React
   helpers call `translate()`; the component that calls the helper must still
   call `useT()`, so it re-renders on a switch.
5. Strings that are not UI stay literal:
   - wire values, API paths, query keys, CSS classes, test ids;
   - log text that goes only to `console.*`.
6. Rich text uses `<Trans k=… components={{ link: <Link to=… /> }} />` with
   `<link>…</link>` in the catalogue.
7. Write `src/modules/<m>/<m>.es.test.tsx`. It renders each routed page of
   the module with its existing fixtures (copy the render setup from the
   module's existing page test) via `renderInSpanish`, waits for the data,
   then `expectNoEnglish(container, [/* module-specific same-in-both terms */])`.
   One `it` per page.
8. Run `npx vitest run src/modules/<m> && npx tsc --noEmit` per module, then
   the full `npx vitest run` at the end of the task.
9. Spanish follows the glossary in `es/index.ts`. If a needed term is not
   there, add it to the glossary in the same commit.
10. One commit per group: `feat(i18n): Spanish for <modules>`.

| Task | Group (modules under `src/modules/`, plus shared code) |
|---|---|
| 5 | `src/shell/*`, `src/components/*`, `src/lib/*` (remaining UI strings), `dashboard`, `profile`, `onboarding`, `setup`, `platform-status`, `error-monitor`, `audit` |
| 6 | `devices`, `enrollment`, `agent-push`, `agent-releases`, `jobs`, `credentials`, `site-servers` |
| 7 | `patch`, `deploy`, `inventory`, `health`, `remote` |
| 8 | `ipam`, `backup`, `bcp`, `provisioning` |
| 9 | `service-desk`, `portal`, `sla`, `email-intake`, `notification` |
| 10 | `settings`, `people`, `security`, `docs`, `reporting` |
| 11 | `licenses`, `subscriptions`, `budget`, `change`, `ai-admin`, `ai-automation` |

**Task 5 also:** `components/form.tsx` `describeValidation` maps Pydantic 422
`type` values to `validation.*` keys. The mapping and Spanish are:

| `type` | English | Spanish |
|---|---|---|
| `missing` | "This field is required." | "Este campo es obligatorio." |
| `string_too_short` | "Too short (at least {min_length} characters)." | "Demasiado corto (mínimo {min_length} caracteres)." |
| `string_too_long` | "Too long (at most {max_length} characters)." | "Demasiado largo (máximo {max_length} caracteres)." |
| `int_parsing` / `float_parsing` | "Must be a number." | "Debe ser un número." |
| `greater_than_equal` | "Must be at least {ge}." | "Debe ser al menos {ge}." |
| `less_than_equal` | "Must be at most {le}." | "Debe ser como máximo {le}." |
| `enum` / `literal_error` | "Not one of the allowed values." | "No es uno de los valores permitidos." |
| `value_error` | the server's `msg` minus the `"Value error, "` prefix (the server translates it) | the same |

Placeholders read `ctx`. An unknown `type` shows the server's `msg`.
Existing `form.test.tsx` assertions must pass unedited. If one asserts the
server's English `msg` for a type in this table, keep the table's English
equal to that `msg`.

**Task 11 also:** after it, add `src/i18n/coverage.test.ts`. It walks every
`.tsx` under `src/modules`, `src/shell` and `src/components` (not tests) and
fails on any JSX text node or the listed props holding a literal of ≥2
letters that is not inside `t(`/`translate(`/`<Trans`. An explicit
`// i18n-exempt: <reason>` comment on the line exempts it. This is the guard
that keeps the console fully Spanish after this PR.

---

### Task 12: Harmony in both languages

**Files:**
- Modify: `frontend/scripts/harmony.mjs`, `.github/workflows/ci.yml` (the `harmony` job)

- [ ] **Step 1:** `harmony.mjs` accepts `--lang en|es` (default `en`):
  - before each page load it sets `localStorage["itms.lang"]` through
    `page.addInitScript`;
  - with no `/me` locale in the fixtures, §1.1 then resolves to that language;
  - screenshots go to `.harmony/<lang>/…`;
  - the report names the language.
- [ ] **Step 2:** Run `node scripts/harmony.mjs --strict-fixtures --lang es` locally.
  - Fix every overflow at 390 px in the component's CSS. Use wrapping
    (`overflow-wrap: anywhere`, `min-width: 0`, flex-wrap), never smaller
    type, and only existing tokens.
  - Re-run `--lang en` to confirm English is unaffected.
- [ ] **Step 3:** In `ci.yml`, give the `harmony` job `strategy.matrix.lang: [en, es]`:
  - run `node scripts/harmony.mjs --strict-fixtures --lang ${{ matrix.lang }}`;
  - name the artifact `harmony-${{ matrix.lang }}`.
- [ ] **Step 4: Commit** — `ci(i18n): harmony gate runs in English and Spanish`

---

### Task 13: Server `tr()`, the request language, and the catalogue guards

**Files:**
- Create: `backend/app/core/i18n/__init__.py`, `backend/app/core/i18n/es.py`, `backend/app/api/locale_middleware.py`
- Modify: `backend/app/main.py:211-215`
- Test: `backend/tests/test_i18n_core.py`, `backend/tests/test_i18n_catalog.py`

**Interfaces:**
- Produces:
  - `tr(template: str, /, **params: object) -> str`;
  - `locale_var: ContextVar[str]` (default `"en"`);
  - `use_locale(lang: str) -> ContextManager[None]`;
  - `SUPPORTED = ("en", "es")`;
  - `parse_accept_language(header: str | None) -> str`.

- [ ] **Step 1: Write the failing tests.**

`tests/test_i18n_core.py`:

```python
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.locale_middleware import locale_middleware
from app.core.i18n import CATALOG_ES, locale_var, parse_accept_language, tr, use_locale


def test_english_is_the_template_formatted():
    assert tr("Run {run_id} is not paused.", run_id="r1") == "Run r1 is not paused."


def test_spanish_comes_from_the_catalogue(monkeypatch):
    monkeypatch.setitem(CATALOG_ES, "Run {run_id} is not paused.", "La ejecución {run_id} no está en pausa.")
    with use_locale("es"):
        assert tr("Run {run_id} is not paused.", run_id="r1") == "La ejecución r1 no está en pausa."


def test_missing_spanish_falls_back_to_english():
    with use_locale("es"):
        assert tr("No such message {x}.", x=1) == "No such message 1."


def test_accept_language_keeps_only_supported():
    assert parse_accept_language("es-CO,es;q=0.9,en;q=0.8") == "es"
    assert parse_accept_language("fr-FR") == "en"
    assert parse_accept_language(None) == "en"
    assert parse_accept_language("en-GB") == "en"


def test_middleware_sets_the_language_for_the_request(monkeypatch):
    monkeypatch.setitem(CATALOG_ES, "Nope.", "No.")
    app = FastAPI()
    app.middleware("http")(locale_middleware)

    @app.get("/x")
    async def x() -> None:
        raise HTTPException(409, tr("Nope."))

    client = TestClient(app)
    assert client.get("/x", headers={"Accept-Language": "es"}).json() == {"detail": "No."}
    assert client.get("/x").json() == {"detail": "Nope."}
    assert locale_var.get() == "en"
```

`tests/test_i18n_catalog.py`. This is the completeness, placeholder,
stale-entry and bypass guard. The bypass check carries a shrinking
`PENDING` set of path prefixes not yet swept; Tasks 14–17 remove theirs, and
Task 17 asserts it is empty.

```python
"""The server catalogue is complete, consistent, and nothing bypasses tr().

PENDING shrinks as Tasks 14-17 sweep each area; the last task empties it and
`test_nothing_is_pending` keeps it empty.
"""
import ast
import re
import string
from pathlib import Path

from app.core.i18n import CATALOG_ES

APP = Path(__file__).resolve().parents[1] / "app"
PENDING: set[str] = {
    "core", "api", "services", "jobs",
    "modules/ipam", "modules/health", "modules/agent_push", "modules/agent_distribution", "modules/agent_update",
    "modules/email_intake", "modules/service_desk", "modules/sla", "modules/notification", "modules/portal", "modules/people",
    "modules/budget", "modules/change_mgmt", "modules/documentation", "modules/backup", "modules/bcp", "modules/deploy",
    "modules/remote", "modules/site_server", "modules/licenses", "modules/subscriptions", "modules/user_admin",
    "modules/provisioning", "modules/ai_admin", "modules/ai_copilot", "modules/ai_portal", "modules/patch",
    "modules/inventory", "modules/reporting", "modules/security_logs", "modules/keycloak_events",
}
# Callables whose message argument is shown to a person.
RAISERS = re.compile(r"^(HTTPException|_reject\w*|_refuse\w*|\w+Error|\w+Refused|MsiRefused|ValueError)$")


def _files():
    return [p for p in APP.rglob("*.py") if "i18n" not in p.parts]


def _rel(p: Path) -> str:
    return p.relative_to(APP).as_posix()


def _pending(p: Path) -> bool:
    return any(_rel(p).startswith(prefix + "/") or _rel(p) == prefix + ".py" for prefix in PENDING)


def _tr_literals():
    for p in _files():
        for node in ast.walk(ast.parse(p.read_text())):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "tr" and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    yield p, arg.value


def _fields(s: str) -> list[str]:
    return sorted(f for _, f, _, _ in string.Formatter().parse(s) if f)


def test_every_tr_literal_is_translated_with_the_same_placeholders():
    missing, mismatched = [], []
    for p, text in _tr_literals():
        if text not in CATALOG_ES:
            missing.append(f"{_rel(p)}: {text!r}")
        elif _fields(CATALOG_ES[text]) != _fields(text):
            mismatched.append(f"{_rel(p)}: {text!r}")
    assert not missing, "untranslated:\n" + "\n".join(missing)
    assert not mismatched, "placeholder mismatch:\n" + "\n".join(mismatched)


def test_no_stale_catalogue_entries():
    used = {text for _, text in _tr_literals()}
    assert sorted(set(CATALOG_ES) - used) == []


def test_tr_is_only_called_with_a_literal():
    bad = []
    for p in _files():
        for node in ast.walk(ast.parse(p.read_text())):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "tr":
                if not node.args or not (isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                    bad.append(f"{_rel(p)}:{node.lineno}")
    assert bad == [], "tr() needs a literal template (the catalogue is keyed by it)"


def test_nothing_bypasses_tr_outside_pending_areas():
    bad = []
    for p in _files():
        if _pending(p):
            continue
        source = p.read_text()
        lines = source.splitlines()
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None) or ""
            if not RAISERS.match(name):
                continue
            # An internal error a person never sees (caught and logged, or
            # re-raised as something else) is exempted on its own line, with
            # the reason: `# i18n-exempt: caught by _http_error, logged only`.
            if "# i18n-exempt:" in lines[node.lineno - 1]:
                continue
            for arg in [*node.args, *(k.value for k in node.keywords if k.arg in ("detail", "message"))]:
                if isinstance(arg, ast.JoinedStr) or (isinstance(arg, ast.Constant) and isinstance(arg.value, str) and re.search(r"[A-Za-z]{3}", arg.value)):
                    bad.append(f"{_rel(p)}:{node.lineno}")
    assert bad == [], "user-facing message not wrapped in tr():\n" + "\n".join(bad)
```

- [ ] **Step 2: Run to verify failure.** `cd backend && .venv/bin/pytest tests/test_i18n_core.py tests/test_i18n_catalog.py -q`. Expected: ImportError.

- [ ] **Step 3: Implement.**

`backend/app/core/i18n/__init__.py`:

```python
"""Server-side translation (docs/superpowers/specs/2026-10-09-spanish-language-design.md §3).

The message ID is the English template itself. `tr` formats with str.format
and nothing else: the catalogue is data, never code.
"""
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from app.core.i18n.es import CATALOG as CATALOG_ES

SUPPORTED = ("en", "es")
locale_var: ContextVar[str] = ContextVar("locale", default="en")

__all__ = ["CATALOG_ES", "SUPPORTED", "locale_var", "parse_accept_language", "tr", "use_locale"]


def tr(template: str, /, **params: object) -> str:
    text = CATALOG_ES.get(template, template) if locale_var.get() == "es" else template
    return text.format(**params) if params else text


@contextmanager
def use_locale(lang: str) -> Iterator[None]:
    token = locale_var.set(lang if lang in SUPPORTED else "en")
    try:
        yield
    finally:
        locale_var.reset(token)


def parse_accept_language(header: str | None) -> str:
    """The highest-weighted supported language, or English."""
    best, best_q = "en", -1.0
    for part in (header or "").split(","):
        tag, _, q = part.strip().partition(";q=")
        lang = tag.split("-")[0].strip().lower()
        try:
            weight = float(q) if q else 1.0
        except ValueError:
            continue
        if lang in SUPPORTED and weight > best_q:
            best, best_q = lang, weight
    return best
```

`backend/app/core/i18n/es.py`:

```python
"""English template → Spanish template. Keys must match tr() literals exactly;
tests/test_i18n_catalog.py fails on a missing, mismatched or stale entry."""
CATALOG: dict[str, str] = {}
```

`backend/app/api/locale_middleware.py`:

```python
from collections.abc import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import Response

from app.core.i18n import parse_accept_language, use_locale


async def locale_middleware(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    with use_locale(parse_accept_language(request.headers.get("accept-language"))):
        response = await call_next(request)
    response.headers.setdefault("Content-Language", parse_accept_language(request.headers.get("accept-language")))
    return response
```

`app/main.py`: register `app.middleware("http")(locale_middleware)` after
`request_context_middleware`. Starlette runs the last-registered middleware
outermost, so the language is set before everything else runs.

- [ ] **Step 4: Run.** The two test files plus `ruff`/`mypy`. Expected: PASS.
The bypass test passes because every area is still `PENDING`.

- [ ] **Step 5: Commit** — `feat(i18n): server tr(), request language from Accept-Language, catalogue guards`

---

### Tasks 14–17: Translate the server's messages, area by area

**Procedure (identical for every group):**

1. In every file of the group, wrap every user-facing message in `tr()`:
   - each literal or f-string passed to `HTTPException`, `_reject*`,
     `_refuse*`, a module error class, `MsiRefused`, or a `ValueError` raised
     inside a Pydantic validator;
   - f-strings become templates with named fields, e.g.
     `f"run {run.id} is {run.state}"` → `tr("run {run_id} is {state}", run_id=run.id, state=run.state)`.

   Field names must be valid Python identifiers. Keep the English text
   **identical**: existing tests assert on it.
2. **Formatting specs inside f-strings** (`{x!r}`, `{n:,}`) move into the
   parameter: `tr("... {name} ...", name=repr(x))`.
3. **Messages built in one place and raised in another** (an error class
   whose `__init__` composes text) are wrapped where the English is composed.
4. **Internal errors no person sees** (caught and logged, or converted
   to another error before reaching a response) get a trailing
   `# i18n-exempt: <reason>` comment on the raise line instead of `tr()`.
   The reason must name where the error goes.
5. **Log messages stay English and unwrapped.** They are not shown to a
   person, and the logging kernel's static-template rule still applies.
6. Add each new template with its Spanish to `app/core/i18n/es.py`, grouped
   under a `# --- <area> ---` comment, following the glossary in the
   frontend's `es/index.ts`.
7. Remove the group's prefixes from `PENDING` in `tests/test_i18n_catalog.py`.
8. Run:
   - `.venv/bin/pytest tests/test_i18n_catalog.py -q`;
   - the group's own tests (`tests/test_<module>*.py`);
   - `ruff` and `mypy`.

   Then run the full suite at the end of the task.
9. Add one test per group, `tests/test_i18n_<group>.py`. It calls one
   representative failing route of each module in the group with
   `Accept-Language: es` and asserts the `detail` is the catalogue's Spanish.
10. One commit per group: `feat(i18n): Spanish server messages for <areas>`.

| Task | `PENDING` prefixes removed |
|---|---|
| 14 | `core`, `api`, `services`, `jobs` |
| 15 | `modules/ipam`, `modules/health`, `modules/agent_push`, `modules/agent_distribution`, `modules/agent_update` |
| 16 | `modules/email_intake`, `modules/service_desk`, `modules/sla`, `modules/notification`, `modules/portal`, `modules/people` |
| 17 | every remaining prefix; then add `def test_nothing_is_pending(): assert PENDING == set()` |

---

### Task 18: Emails in the recipient's language

**Files:**
- Modify: `backend/app/modules/notification/delivery.py` (renderers 258–760, `_send_email` 997, call at ~1074)
- Test: `backend/tests/test_notification_email_language.py`

**Interfaces:**
- Consumes: `tr`, `use_locale`; `User.locale`, `User.email`, `User.org_id`.
- Produces: `async def _recipient_language(session, org_id, address) -> str`.

- [ ] **Step 1: Write the failing test.** Reuse the fixtures of the existing
  email-delivery tests (`tests/test_notification_delivery*.py`):
  - create two users in one org, one with `locale="es"` and one with `None`;
  - send a `ticket.status_changed` email to each;
  - assert the Spanish user's captured subject and body equal the catalogue's
    Spanish, and the other's are the English;
  - assert a channel-default address that is no user's gets English.

- [ ] **Step 2: Run it to see it fail.**
- [ ] **Step 3: Implement.**
  - Wrap every subject and body template in the 21 `_render_*` functions in
    `tr()` (Task 16 may already have done so; finish any it did not), with
    their Spanish in `es.py`.
  - Add `_recipient_language`:

```python
async def _recipient_language(session: AsyncSession, org_id: UUID, address: str) -> str:
    """The recipient's own language (spec §3.4); English for an address that is no user's."""
    locale = await session.scalar(
        select(User.locale).where(User.org_id == org_id, func.lower(User.email) == address.lower())
    )
    return "es" if (locale or "").lower().startswith("es") else "en"
```

  - In `_send_email`, replace `subject, body = _render_email(event)` with:

```python
with use_locale(await _recipient_language(session, delivery.org_id, delivery.recipient)):
    subject, body = _render_email(event)
```

- [ ] **Step 4: Run** the new test and the notification suite. Expected: PASS.
- [ ] **Step 5: Commit** — `feat(i18n): notification emails in the recipient's language`

---

### Task 19: `locale` validation and the Keycloak `locale` attribute

**Files:**
- Modify: `backend/app/api/v1/me.py` (`ProfileUpdate` 69–83, PATCH 140–165), `backend/app/modules/user_admin/kc_client.py`
- Test: `backend/tests/test_me_locale.py`

**Interfaces:**
- Produces: `async def set_locale_attribute(config: KeycloakAdminConfig, *, user_id: str, locale: str | None) -> None`.

- [ ] **Step 1: Write the failing tests:**
  - `PATCH /me {"locale": "es"}` → 200 and stored;
  - `{"locale": "en-GB"}` → 200 (existing values stay valid);
  - `{"locale": "<script>"}` and `{"locale": "e"}` → 422;
  - with a fake Keycloak admin, the attribute update is a GET then a PUT
    whose `attributes` keeps the existing `org` attribute and sets
    `locale: ["es"]`;
  - when the Keycloak call raises, the PATCH still returns 200, with
    `"identity_locale_synced": false` in the response.
- [ ] **Step 2: Run to see it fail.**
- [ ] **Step 3: Implement**
  - `ProfileUpdate.locale` gains `pattern=r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$"` (null still allowed).
  - `kc_client.set_locale_attribute`: GET `/users/{id}`, copy its
    `attributes` dict, set or delete `locale`, then PUT
    `{"attributes": merged}`. Keycloak replaces the whole attributes map on
    PUT, so a partial map would erase `org`; that is why it merges.
  - In PATCH, after the commit, when `"locale" in supplied` and
    `KeycloakAdminConfig.from_settings(settings)` is not `None`, call it with
    `user.external_subject`.
  - Catch `KeycloakAdminError` and `httpx.HTTPError`, log a static template,
    and set `identity_locale_synced=False` on the response. `MeResponse`
    gains `identity_locale_synced: bool | None = None`.
- [ ] **Step 4: Run** the new test and `tests/test_me*.py`. Expected: PASS.
- [ ] **Step 5: Commit** — `feat(i18n): validate locale and mirror it to the Keycloak locale attribute`

---

### Task 20: Keycloak sign-in pages in Spanish

**Files:**
- Modify: `deploy/keycloak/realm-it-suite.json`, `deploy/keycloak/init-clients.py` (`CONVERGED_REALM_KEYS`)
- Create: `deploy/keycloak/themes/itms/login/messages/messages_es.properties`
- Test: the existing init-clients tests (`backend/tests/test_keycloak_init*.py` or `deploy/keycloak/test_*.py`; find them with `grep -rl CONVERGED_REALM_KEYS`)

- [ ] **Step 1: Write the failing test**, beside the existing convergence tests:
  - `internationalizationEnabled`, `supportedLocales`, `defaultLocale` are in
    `CONVERGED_REALM_KEYS`;
  - the realm file sets them to `true`, `["en","es"]`, `"en"`;
  - every key in `messages_en.properties` exists in `messages_es.properties`.
- [ ] **Step 2: Run to see it fail.**
- [ ] **Step 3: Implement:**
  - add the three keys to the realm file and to `CONVERGED_REALM_KEYS`, each
    with a comment in the file's style saying why;
  - translate all 50 lines of `messages_en.properties` into
    `messages_es.properties` (UTF-8);
  - Keycloak's base theme supplies Spanish for everything else.
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** — `feat(i18n): Keycloak sign-in pages and emails follow the person's language`

---

### Task 21: Final verification and docs

- [ ] **Step 1:** Frontend:
  - `npx tsc --noEmit`, `npx vitest run`, `npm run build`;
  - `node scripts/harmony.mjs --strict-fixtures --lang en`;
  - `node scripts/harmony.mjs --strict-fixtures --lang es`.

  All pass.
- [ ] **Step 2:** Backend:
  - `.venv/bin/ruff check app tests`, `.venv/bin/mypy app`;
  - the full pytest suite;
  - the migration round trip exactly as `ci.yml`'s migrations job runs it
    (no migration is expected; confirm `alembic heads` is unchanged at `0200`).
- [ ] **Step 3:** `deploy/README.md`:
  - a short "Language" paragraph saying where the switcher is, that the
    choice is per account, and that Keycloak follows it after the next
    `keycloak-init`;
  - "what stays English" (spec §3.6).
- [ ] **Step 4: Commit** — `docs(i18n): language switcher and what stays English`
