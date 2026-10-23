# IT Documentation Module Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Asset-linked knowledge base: versioned Markdown articles + typed records (network/service/vendor/location), links to devices/groups/credentials/records, Postgres full-text search, `docs.read`/`docs.manage` RBAC, and a Docs console — per `docs/modules/documentation/spec.md`.

**Architecture:** Pure control-plane module (`app/modules/documentation/`): service layer owns versioning + audit; migration 0007 adds four RLS-forced tables, an FTS GIN index, and the two new permission codes with role grants. Frontend adds `/docs` and a Docs tab on device detail.

**Tech Stack:** unchanged backend; frontend adds `marked` (MIT) with an allowlist sanitizer written in-repo.

## Global Constraints

- All prior global constraints (mypy strict, ruff, real-Postgres tests, forced RLS, forward-only migrations).
- New permissions exactly `docs.read`, `docs.manage`; grants: org-admin + technician both, auditor + read-only `docs.read` only.
- Markdown renders client-side through the allowlist sanitizer — raw HTML in Markdown never reaches the DOM.
- Mutations bump `current_version` + write a `doc_versions` snapshot + an audit event; no-op updates do none of that.
- Archive is soft: hidden from default lists, history and audit retained.

---

### Task 1: Models + migration 0007 (tables, FTS, permissions)

**Files:** Create `backend/app/models/documentation.py`, `backend/migrations/versions/0007_documentation.py`; Modify `backend/app/models/__init__.py`, `backend/app/core/rbac.py` (add the two codes), `backend/tests/test_migrations.py` (4 tables), `backend/tests/test_rbac.py` (registry set + grants)

**Interfaces:**
- Produces models per spec §4: `DocArticle` (`title`, `body`, `category`, `archived`, `current_version`, `created_by`, `updated_by`), `DocRecord` (`record_type`, `name`, `fields`, same bookkeeping), `DocLink` (unique `(article_id, target_type, target_id)`), `DocVersion` (`article_id` XOR `record_id` CHECK, `version`, `snapshot`, `changed_by`, `ts`).
- Migration: autogen tables + hand-added RLS blocks + `CREATE INDEX ix_doc_articles_fts ON doc_articles USING gin(to_tsvector('english', title || ' ' || body))` + permission INSERTs (`docs.read`, `docs.manage`) + role_permission grants per spec §3.

**Steps:**
- [ ] Extend EXPECTED_TABLES (+4) and the RBAC registry test (`docs.read` in auditor/read-only/technician/org-admin sets, `docs.manage` in technician/org-admin) → FAIL.
- [ ] Implement models, PERMISSIONS dict entries, autogen+edit migration; suite green (RLS completeness auto-covers). Commit `feat(docs): tables, FTS index, and docs permissions`.

### Task 2: Schemas + service (versioning, audit, links)

**Files:** Create `backend/app/modules/documentation/__init__.py`, `backend/app/modules/documentation/schemas.py`, `backend/app/modules/documentation/service.py`; Test `backend/tests/test_docs_service.py`

**Interfaces:**
- Produces `schemas.py`: `RECORD_FIELD_MODELS: dict[str, type[BaseModel]]` for `network` (`cidr` validated by `ipaddress.ip_network`, `vlan: int|None`, `gateway`, `dns`), `service` (`url`, `owner`, `environment`), `vendor` (`contact_email`, `phone`, `contract_end: date|None`), `location` (`address`, `timezone`) — all `extra="allow"`; `validate_record_fields(record_type, fields) -> dict`.
- Produces `service.py`: `create_article(session, *, org_id, actor_id, title, body, category) -> DocArticle`; `update_article(session, article, *, actor_id, title, body, category) -> bool` (False = no-op); `archive_article(session, article, *, actor_id)`; same trio for records (`create_record(..., record_type, name, fields)`); `add_link(session, article, *, actor_id, target_type, target_id) -> DocLink` raising `LinkTargetNotFound` after in-org existence check per type; `remove_link(session, article, link_id, *, actor_id) -> bool`; `list_versions(session, article_id=None, record_id=None) -> list[DocVersion]`.

**Steps:**
- [ ] Failing tests: create writes version 1 snapshot + `docs.article_created` audit; update bumps to 2 with new snapshot; identical update returns False, no bump/no audit; archive hides but keeps versions; record field validation (bad `cidr` raises, extra fields kept); link to in-org device OK, to foreign-org device/nonexistent target raises `LinkTargetNotFound`, duplicate link raises IntegrityError; credential link stores metadata reference only.
- [ ] Implement; suite green. Commit `feat(docs): versioned article/record service with audited links`.

### Task 3: API routes

**Files:** Create `backend/app/modules/documentation/api.py`; Modify `backend/app/modules/documentation/__init__.py` (mount), `backend/app/modules/__init__.py` (register); Test `backend/tests/test_docs_api.py`

**Interfaces:**
- Produces spec §6 routes exactly; list models `ArticleListItem {id, title, category, updated_at, archived, link_count}`, `ArticleOut` (+`body`, `links: [{id, target_type, target_id}]`, `current_version`), `RecordOut {id, record_type, name, fields, current_version}`, `VersionOut {version, snapshot, changed_by, ts}`, `SearchHit {kind, id, title, category, rank}`.

**Steps:**
- [ ] Failing tests: read-only GETs pass but POST/PUT 403; technician full CRUD; asset filter (`?target_type=device&target_id=`) returns linked articles only; search finds body words and ranks title match first, excludes archived; version history endpoint newest first; org isolation (foreign article 404).
- [ ] Implement; suite green. Commit `feat(docs): documentation API`.

### Task 4: Search behavior hardening

**Files:** Modify `backend/app/modules/documentation/api.py` (only if Task 3 left gaps); Test extend `backend/tests/test_docs_api.py`

**Steps:**
- [ ] Failing tests: `websearch_to_tsquery` syntax (`"exact phrase"`, `word1 word2`) behaves; record-name ilike hits appear with `kind="record"`; empty `q` → 422.
- [ ] Implement/adjust; suite green. Commit `feat(docs): search phrase + record coverage`.

### Task 5: Frontend — Docs page + device Docs tab

**Files:** Create `frontend/src/modules/docs/DocsPage.tsx`, `frontend/src/modules/docs/markdown.ts` (marked + sanitizer), `frontend/src/modules/docs/searchGroups.ts`; Modify `frontend/src/lib/api.ts` (docs types + methods), `frontend/src/App.tsx` (route `/docs`, nav gated on `docs.read`), `frontend/src/modules/inventory/DeviceDetailPage.tsx` (Docs tab); Test `frontend/src/modules/docs/markdown.test.ts`, `frontend/src/modules/docs/searchGroups.test.ts`; `cd frontend && npm install marked`

**Interfaces:**
- Produces `renderMarkdown(md: string): string` — marked → sanitizer allowlist (`p,a[href^=http|/],strong,em,code,pre,ul,ol,li,h1..h4,blockquote,table,thead,tbody,tr,th,td,hr,br`; strips `script/style/iframe/on*` attributes entirely); `groupHits(hits: SearchHit[]): {articles: SearchHit[]; records: SearchHit[]}`.

**Steps:**
- [ ] Failing vitest: `renderMarkdown` renders basic Markdown, strips `<script>`, strips `onerror` attrs, drops `javascript:` hrefs; `groupHits` splits by kind preserving rank order.
- [ ] Implement page (list/search/category groups, article view+edit with live preview, versions drawer, links editor, records forms per type, archive) + device Docs tab; `npm test`, `tsc --noEmit`, `npm run build` green.
- [ ] Commit `feat(docs): docs console and device docs tab`.

### Task 6: Verification gate + README

**Files:** Modify `README.md` (status)

**Steps:**
- [ ] Full gate: backend pytest, `mypy app`, `ruff check app tests`; frontend tsc/vitest/build; `docker compose config -q`.
- [ ] README status entry for the documentation module. Commit `feat(docs): finish documentation module cycle`.

---

## Self-review

- **Coverage:** spec §3→T1, §4→T1, §5→T2, §6→T3+T4, §7→T5, §8→T2/T3/T4/T5, gate→T6. No gaps; no placeholders; names consistent (`LinkTargetNotFound`, `RECORD_FIELD_MODELS`, `SearchHit.kind` values `article|record` used in T3/T5).
