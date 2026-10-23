# Spanish, and a language switcher — design

**Status:** approved by the operator on 2026-10-09: the design section by
section, then this written spec as a whole.

**Operator decisions (2026-10-09):**

| # | Question | Answer |
|---|---|---|
| Q1 | How much of the console, and when? | The whole console, in one PR. |
| Q2 | Server-written text too? | Yes: messages the server writes are translated too, and emails go out in the recipient's language. |
| Q3 | Where is the switcher? | In the profile menu (the account chip, top right), between **Settings** and **Sign out**. |
| Q4 | Where is the preference kept? | On the user's account, so it follows them to any browser. |

This is independent of the MCP server design written the same day
(`2026-10-09-mcp-server-design.md`). Neither depends on the other.

---

## 1. What a person sees

1. The account menu (`frontend/src/shell/AccountMenu.tsx`) gains one row,
   **Language: English** (or **Idioma: Español**), between **Settings** and
   **Sign out**. It is a `role="menuitem"` like its neighbours, so the
   existing arrow-key wrap and Escape handling cover it unchanged.
2. Activating it opens a two-item submenu, **English** and **Español**, with
   the current one marked (`aria-checked`, `role="menuitemradio"`).
3. Choosing one switches every visible string at once, with no reload, and
   saves the choice to the person's account (`PATCH /api/v1/me` with
   `{"locale": "es"}`). If the save fails, the language still changes for this
   tab and a toast says it could not be remembered.
4. **Your profile** replaces today's free-text **Locale** box (`placeholder
   "e.g. en-GB"`) with the same two-choice **Language** select.
5. The choice follows the person: another browser, another computer, the
   self-service portal (same app, same menu — `App.tsx:1191`).

### 1.1 Which language is used, in order

1. The account's saved `locale`, if it is one the console supports.
2. Otherwise, the last choice made in this browser (`localStorage`,
   `itms.lang`), so the first paint before `/me` loads is not English for a
   Spanish speaker.
3. Otherwise, the browser's language if it starts with `es`.
4. Otherwise, English.

A saved value is matched on its primary subtag, so the `en-GB` values already
stored by today's free-text field read as English. The console only ever
writes `en` or `es`.

`<html lang>` follows the active language, for screen readers.

## 2. Frontend mechanism

### 2.1 No new dependency

A small in-house module, `frontend/src/i18n/`, rather than i18next or
FormatJS:

- **Catalogues are TypeScript, split by module:**
  `src/i18n/en/<module>.ts` and `src/i18n/es/<module>.ts`, assembled in
  `src/i18n/en/index.ts` and `src/i18n/es/index.ts`. **English is the source
  of truth.** The Spanish catalogue is typed as `Catalog`, the type derived
  from the English one, so **a missing or extra Spanish key fails `tsc`**,
  and therefore CI. This is the check that stops a new screen from shipping
  English-only.
- **`useT()`** returns `t(key, vars?)`. Keys are typed (dot paths into the
  catalogue), so a typo is a compile error, not a blank label.
- **Interpolation:** `{name}` placeholders. A test asserts that every Spanish
  string has exactly the placeholders of its English counterpart.
- **Plurals:** an entry may be `{one, other}` (and `many` where Spanish needs
  it), chosen with `Intl.PluralRules` for the active language.
- **Rich text:** strings with embedded links or emphasis use
  `<Trans k="…" components={{ link: <Link …/> }} />`, which splits on
  `<link>…</link>` markers. No HTML is ever injected.
- **`I18nProvider`** sits above the router in `App.tsx`. It holds the active
  language, resolves it per §1.1, and exposes `setLanguage(lang)`.

### 2.2 Dates, numbers and enum labels

- `lib/datetime.ts` (`formatDateTime`, `formatDate`, `describeWhen`) and
  `lib/format.ts` (`formatCount`, `formatBytes`, `describeSeconds`) read the
  active language from one module-level getter set by the provider. English
  keeps today's `en-GB` formatting exactly, so existing tests are unchanged.
  Spanish uses `es`, e.g. `9 oct 2026, 14:05`.
- The English words inside those helpers ("just now", "ago", "in", unit
  names) move into the catalogue.
- The stray hard-coded locales are routed through the helpers:
  - `JobsPage.tsx:122,129` (`en-GB`);
  - `ChangeCalendar.tsx:18` (`en-CA`);
  - `ipamState.ts:1208` and `pushState.ts:126` (`en-US`);
  - `ApproveAllDialog.tsx:168` (`en`);
  - `PushRunsPage.tsx:51` (bare);
  - `budget/money.ts:28` (`LOCALE`).
- `lib/labels.ts` `humaniseEnum` / `humaniseKey` turn wire values like
  `stopped_credential` into words. They gain a catalogue lookup
  (`enums.<type>.<value>`). A test enumerates every enum the console renders,
  read from the API types in `lib/api.ts`, and asserts each value has an
  entry in both languages, so the English fallback is never what a Spanish
  speaker sees.
- `localeCompare` sorts pass the active language.

### 2.3 The API learns the language

- `ApiClient` gets one header builder, used by the central `request()` and by
  every one of the ~17 raw `fetch` paths (uploads, downloads; `api.ts:1852`,
  `2041`, `2292`, `3113`, `4907`, …) and `SetupPage.tsx:64,146`. It adds
  `Accept-Language: en` or `es` to every call. A test asserts that no `fetch`
  in `src/` builds its own `Headers` without it.
- 422 validation details rendered by `components/form.tsx`
  (`describeValidation`) are mapped by Pydantic error `type` (`missing`,
  `string_too_long`, …) to catalogue sentences. The server's `msg` is shown
  only for custom validator messages, which the server translates (§3).

### 2.4 The strings themselves

About 3,600 strings across 424 screen files and 40 modules move into the
catalogues. This is mechanical, but it is most of the diff.

- **The English text must stay byte-identical**, so the ~7,000 existing test
  assertions on English text keep passing untouched. `src/test/setup.ts`
  pins tests to English.
- **New tests per module** render each module's main page in Spanish, with
  the existing fixtures, and assert that no catalogue English value appears
  on the page. A short allowlist covers words that are the same in both
  languages ("Email", "Windows", product names, "OK").
- **The two source-scanning tests read the catalogues too.**
  `noVendorNames.test.ts` and `noWireNames.test.ts` today read JSX literals;
  once strings move, they must also scan `src/i18n/**`, or they would pass
  over an empty set.

### 2.5 Layout

Spanish runs about 20–30% longer. `scripts/harmony.mjs` gains `--lang es`
and CI runs the harmony job in both languages, `--strict-fixtures`, at 1440
and 390 px. Overflow at 390 px in Spanish fails the build exactly as it does
in English today. This roughly doubles the harmony job's time (≈8 → ≈16
minutes on the shared runners).

## 3. Server messages

### 3.1 The request's language

- A middleware beside `api/request_context.py` reads `Accept-Language`,
  keeps only `en` or `es` (anything else → `en`), and sets a context variable
  `locale_var`, following the `request_id_var` pattern
  (`core/logging.py:192`).
- Background work (Celery tasks, the pacer, sweeps) has no request, so it runs
  in English unless it sets the variable itself (§3.4).

### 3.2 One translation function

`app/core/i18n/` provides `tr(template, **params)`:

- **The message ID is the English template itself** (gettext style), e.g.
  `tr("Push run {run_id} is not paused.", run_id=run.id)`. That keeps every
  raise site readable and the change mechanical.
- The Spanish catalogue `app/core/i18n/es.py` is a dict from English template
  to Spanish template.
- `tr` formats with `str.format`, never `%`, and never evaluates anything
  from the catalogue.

About 1,270 distinct messages are raised today, 724 of them as f-strings.
Each becomes a `tr(...)` call with named parameters. The ~43 `_reject` /
`_refuse` helpers and the per-module `_http_error(exc)` mappers are unchanged:
the translated text is already in the exception by the time `str(exc)` reads
it, because the exception is built inside the request's context.

### 3.3 Tests that keep it complete

- **Completeness:** an AST scan of `backend/app` collects every literal
  passed to `tr()` and asserts each has a Spanish entry with exactly the same
  `{placeholders}`. A missing or mismatched translation is a red build.
- **Nothing bypasses `tr`:** the same scan fails on a string literal or
  f-string passed directly to `HTTPException(...)`, to `_reject*` /
  `_refuse*`, or to a module error class (the classes deriving from each
  module's error base, e.g. `ipam/errors.py:18`). Without this, the next
  raise added in English goes unnoticed.
- **Stale entries:** catalogue entries no `tr()` call uses fail the build too,
  so the catalogue does not rot.

### 3.4 Emails go out in the recipient's language

- `notification/delivery.py`'s renderers (`_render_*`, 21 event types,
  `delivery.py:258–760`) wrap their subject and body text in `tr()`.
- `_send_email` (`delivery.py:997`) renders each message inside a context
  where `locale_var` is the **recipient's** language. The recipient is an
  address (`NotificationDelivery.recipient`), so the language is looked up
  from the `users` row with that address in the delivery's organization.
  An address that is not a user (a channel's default mailbox) gets English.
- The worker never uses the language of whoever triggered the event.

### 3.5 Keycloak: sign-in pages and its own emails

The sign-in page and Keycloak's invitation and password-reset emails are
Keycloak's, not ours. For them to follow the same choice:

- The realm enables internationalization with `en` and `es`
  (`internationalizationEnabled`, `supportedLocales`, `defaultLocale: en`).
  Because a realm file is imported only once, `keycloak-init` converges these
  settings on every deploy, alongside its existing realm allowlist.
- The `itms` login theme gains `messages_es.properties` beside its 50-line
  `messages_en.properties`.
- When a person changes their language, `PATCH /me` also sets the Keycloak
  user attribute `locale`, through the existing admin client
  (`user_admin/kc_client.py`). Keycloak's own emails then use it. If the
  Keycloak update fails, the console setting is still saved, and the response
  says the sign-in pages were not updated.

### 3.6 What stays in English, and why

- **Audit log entries stay as recorded.** The log is hash-chained, so a stored
  entry cannot be rewritten. The audit screens translate their own labels and
  the action names (`enums`), but not text stored inside an entry.
- **Text stored by background work stays as stored.** A few modules persist a
  sentence when something happens, for example a job's failure reason.
  Whatever language that background task ran in (English) is what is shown
  later. Where the console already maps a stored *code* to words, as push-run
  readiness reasons do, that mapping is translated.
- **The agent and its logs** (`msi.log`, service messages) stay English. The
  agent has no user.

## 4. Validation of `locale`

`api/v1/me.py` `ProfileUpdate.locale` keeps `max_length=16` and gains a
format check: a language tag of letters and hyphens (`^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$`),
or `null`. Existing stored values such as `en-GB` remain valid. The console
writes only `en` or `es`.

## 5. Out of scope

- Languages beyond English and Spanish. The mechanism takes a third language
  as one more catalogue pair, with no code change.
- Translating API field names, wire enums on the wire, or log lines.
- Regional Spanish variants. The catalogue is neutral Spanish, `es`.

## 6. Review

The translation is written by Claude. A native Spanish speaker should review
the catalogues before Spanish-speaking users rely on them. The PR will say so,
and list every place a term was chosen between alternatives (e.g.
*dispositivo* vs *equipo*, *inscribir* vs *registrar* for "enroll") in a
glossary at the top of `src/i18n/es/index.ts`, so the reviewer can change a
term in one place.

## 7. Deploy

- **No migration**: `users.locale` already exists.
- **No new `.env` keys.**
- **Image rebuild**: yes (backend and web).
- `keycloak-init` (run by `deploy.sh` on every deploy) converges the realm's
  language settings; the login theme is part of the Keycloak image mount.
