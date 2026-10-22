# IT Documentation Module Specification (pulled earlier per roadmap flexibility)

Status: approved (autonomous-build delegation, 2026-07-18)
Source brief: `docs/modules/documentation.md`. Kernel: `docs/kernel/spec.md`.

> **A stored FILE is specified next door, not here.** This document describes
> Markdown articles, typed records, links, versions and FTS, and every word of
> it still stands. The one thing this module never had — a file you can upload,
> version and view — is
> `docs/modules/documentation/spec-attachments.md`, which is a sibling rather
> than an edit so that it stays possible to tell which sentences described
> running code and which described an intention. §1–§8 below are unchanged by
> it.

## 1. Scope

Asset-linked knowledge base: Markdown articles/runbooks with categories,
versioning, and audit; typed structured records (network/service/vendor/
location); links from documentation to devices, device groups, credential
*metadata*, and records; Postgres full-text search; RBAC-scoped access; a
Docs console section plus a Docs tab on the device detail page.

No agent work (control-plane data only). Deferred per brief: inventory
auto-population, relationship graph, templates/required-field policies.

## 2. Resolutions of the brief's open questions

- **Format:** Markdown, stored as text, rendered client-side (`marked`, MIT).
  No WYSIWYG in v1 — light, diffable, versionable.
- **Search:** PostgreSQL full-text (`to_tsvector('english', title || body)`
  with a GIN index; `websearch_to_tsquery` for queries) over articles, plus
  name matching over records. No new infrastructure.
- **Typed records v1:** all four from the brief — `network`, `service`,
  `vendor`, `location` — one table with `record_type` + `fields` jsonb.
  Per-type Pydantic schemas validate *known* fields and allow extras
  (`extra="allow"`), so records are structured without being rigid.
- **Inventory auto-population:** deferred (keeps the phase light).

## 3. Permissions (catalog extension via defined path)

Two new permission codes seeded by data migration and added to
`app/core/rbac.py`: `docs.read` (view documentation) and `docs.manage`
(create/edit/archive). Grants: org-admin + technician get both; auditor and
read-only get `docs.read`. The RBAC registry test updates accordingly.

## 4. Data model (migration 0007: tables + FTS index + permission seed; RLS standard)

- `doc_articles`: `id`, `org_id`, `title` (≤300), `body` (text, Markdown),
  `category` (≤100, free-form), `archived` bool default false,
  `current_version` int default 1, `created_by`, `updated_by` (user FKs,
  nullable), `created_at`/`updated_at`. GIN FTS index on
  `to_tsvector('english', title || ' ' || body)`.
- `doc_records`: `id`, `org_id`, `record_type`
  (`network|service|vendor|location`), `name` (≤300), `fields` jsonb,
  `archived`, `current_version`, `created_by`, `updated_by`, timestamps.
  Index `(org_id, record_type)`.
- `doc_links`: `id`, `org_id`, `article_id` FK (cascade), `target_type`
  (`device|device_group|credential|record`), `target_id` uuid; unique
  `(article_id, target_type, target_id)`. Link targets are validated to
  exist in-org at creation; a linked credential exposes only its metadata
  (name/type) — secrets stay behind the vault, always.
- `doc_versions`: `id`, `org_id`, `article_id` FK nullable, `record_id` FK
  nullable (exactly one set — CHECK constraint), `version` int, `snapshot`
  jsonb (article: `{title, body, category}`; record: `{name, record_type,
  fields}`), `changed_by`, `ts`. Written on every create and update; archive
  does not delete versions (archival is soft — history and audit survive).

## 5. Services & module layout

```
backend/app/modules/documentation/
├── __init__.py     # register(app): mounts routes (no job types)
├── schemas.py      # per-record-type field models + request/response models
├── service.py      # create/update/archive with versioning + audit + link mgmt
└── api.py          # routes below
```

`service.py` behavior: every mutation runs in the tenant session, bumps
`current_version`, writes the `doc_versions` snapshot, and records an audit
event (`docs.article_created|article_updated|article_archived|
record_created|record_updated|record_archived|link_added|link_removed`,
target = the article/record id). Updates with no actual change are no-ops
(no version bump, no audit).

## 6. API (`/api/v1/docs/...`; read = `docs.read`, mutations = `docs.manage`)

- `POST/GET /docs/articles` (list: `?category=`, `?target_type=&target_id=`
  to find docs linked to an asset, `?include_archived=false` default,
  paged) — list items exclude `body` (id, title, category, updated_at,
  updated_by, link_count).
- `GET/PUT /docs/articles/{id}` (full body; PUT = edit title/body/category),
  `POST /docs/articles/{id}/archive`.
- `GET /docs/articles/{id}/versions` — newest first, snapshots included.
- `POST /docs/articles/{id}/links` `{target_type, target_id}` (404 if the
  target doesn't exist in-org), `DELETE /docs/articles/{id}/links/{link_id}`.
- `POST/GET /docs/records` (`?record_type=`), `GET/PUT /docs/records/{id}`,
  `POST /docs/records/{id}/archive`, `GET /docs/records/{id}/versions`.
- `GET /docs/search?q=` — FTS over articles (ranked, `ts_rank`) + ilike over
  record names; returns typed hits `{kind: article|record, id, title,
  category|record_type, rank}`.
- `GET /docs/suggest?q=&limit=` (added 2026-09-23, `docs.read`) — live
  articles matching ANY word of `q` (search requires all of them), best match
  first, `{id, title, category}`. The ticket reply box uses it with the
  ticket subject. Staff-only; requesters use the portal's own help routes.
- `PUT /docs/articles/{id}/portal-visibility` `{portal_visible}` (added
  2026-09-23, `docs.manage`, audited `docs.article_portal_visibility_changed`,
  no version bump). Articles are internal by default; a visible, unarchived
  article is readable in the portal through `/portal/help` (portal spec §5),
  never through a `/docs` route.

## 7. Frontend

- **Docs page** (`/docs`, nav gated on `docs.read`): left column — search box
  (hits grouped by kind) and category-grouped article list + records section
  filterable by type; main pane — article view (rendered Markdown via
  `marked` + sanitized with DOMPurify-lite approach: `marked` output injected
  through a sanitizer allowlist — no raw HTML from Markdown), edit mode
  (textarea + live preview), version history drawer, links editor (pickers
  for devices/groups/records/credentials by name), archive button. Records
  render as field tables with type-appropriate forms.
- **Device detail**: new **Docs** tab listing articles linked to the device
  (via the `target_type=device&target_id=` filter) with links to `/docs`.
- Mutation UI hidden without `docs.manage` (server enforces regardless).

## 8. Testing

- Service: create/update bumps versions with correct snapshots; no-op update
  doesn't bump; archive hides from default list but keeps versions; audit
  events per mutation; link add validates in-org target and rejects
  foreign-org/nonexistent targets (404), unique constraint on duplicate links.
- Search: FTS finds by title and body words (`websearch_to_tsquery`), rank
  orders title hits first for a title match; record name hits included;
  archived excluded.
- RBAC: read-only can GET but not POST/PUT (403); technician can mutate;
  org isolation via RLS on every table (completeness test covers).
- Records: each of the four types validates its known fields (e.g. network
  record `cidr` must parse) while allowing extras.
- Frontend: vitest for the search-grouping helper and the sanitizer
  allowlist; tsc strict + build.
