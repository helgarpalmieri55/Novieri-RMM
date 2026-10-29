# Module Spec — Documentation: stored documents (files)

> **A sibling spec, not a replacement.** `docs/modules/documentation/spec.md`
> describes what shipped: Markdown articles, typed records, links, versions,
> FTS. It stays authoritative for all of that, and nothing in it changes. This
> file specifies the one thing that module has never had — **a file** — and
> where the two meet it says so by section number.
>
> Written as a sibling rather than as an edit to the shipped spec because that
> document is a record of built behaviour and this one is a proposal. Merging
> them would make it impossible to tell, six months from now, which sentences
> described running code and which described an intention.
>
> **Control plane only.** No agent capability, no job type, no wire-protocol
> change, no signed-job path, no change to `docs/kernel/wire-protocol/`. §15.13
> states what that means for the agent idle budget.

Status: **proposed** — awaiting the operator's answers to §17.
Source: the operator, 2026-08-25 — *"the it documentation page needs a lot of
improvement, there's no way to upload a document or view a document here, i'm
looking for a functionality similar to ITGlue"*.

---

## 0. Resolved design decisions

Ten decisions settled before the design, because each rules out a family of
designs. Every one that is a judgement call rather than a consequence of an
invariant is repeated in §17 so the operator can overturn it as a single list.

### 0.1 The gap is a file. Almost everything else ITGlue sells, this platform already has

ITGlue is documents, passwords, configurations, contacts, locations, domains,
SSL certificates, runbooks, a relationship graph, and a browser extension.
Specifying that whole surface here would be re-specifying the platform. What
this platform already owns, verified against shipped code:

| ITGlue concept | Already here | Where |
|---|---|---|
| Runbooks / procedures | Markdown articles, versioned, linked, FTS-searchable | `doc_articles`, spec.md §4 |
| Configurations (hardware) | Devices, including agentless assets — a switch or a printer is a `devices` row | `devices`, migration 0061 |
| Networks / IP records | The address plan of record, with reconciliation | `ipam_*`, IPAM spec — and it **superseded** `doc_records.record_type='network'` |
| Passwords | The credential vault: envelope-encrypted, decrypt-at-use, audited checkout | `credentials`, invariant 3 |
| Vendors, services, locations | Typed `doc_records` with per-type field schemas | `doc_records`, spec.md §2 |
| Licences and keys | Licence catalogue with audited key reveal | `licenses.*` |
| Subscriptions / contracts | Subscription registry with seats and renewal | `subscriptions.*` |
| Tickets and their files | Service Desk, with its own attachment path | `ticket_attachment` |
| Contracts / spend | Budget, with expense invoice attachments | `budget.*` |

The column that is empty everywhere except tickets and expenses is **a stored
file that documents something**. A signed vendor contract PDF. A rack
photograph. A site network diagram somebody drew in Visio. A firewall
configuration export. Today the only way to put one of those into this platform
is to attach it to a ticket, where it lives inside a thread that freezes when
the ticket closes.

**So this spec builds documents, versions, links, a viewer, and search over
their metadata — and nothing else.** §15 lists what is deliberately left out and
why, item by item.

### 0.2 The bytes go in the object store, addressed by a row the database protects

Rejected: **bytes in PostgreSQL** (`bytea` or large objects). It is simpler by
exactly one component and worse in four ways: it puts 25 MiB blobs in the
WAL and therefore in every base backup and every replication stream; it makes
`pg_dump` unusable as an operational tool; it defeats the architecture's own
statement that "object storage absorbs the large artifacts, keeping the database
lean" (`docs/01-architecture.md`, Scaling notes); and it would be the only
attachment path in this codebase that does it differently — Budget, Service
Desk, Deploy, Backup, Remote recordings and Provisioning all use
`app.core.storage.ObjectStore`.

**An object store has no Row-Level Security, so the isolation has to be
designed rather than assumed.** Invariant 5 says tenant scoping is enforced at
the database layer. The object store is not a database layer. Four mechanisms
replace it, and all four are required — §3 specifies each:

1. **The key is entirely server-derived** from ids the caller was already
   authorized for: `{org_id}/docs/documents/{document_id}/{version_id}`. No
   client-supplied segment reaches it, ever. The client-supplied filename is a
   sanitized *display* name stored in a column.
2. **The only way to learn a key is through an RLS-protected row.** Keys are
   never returned by any API response. A caller asks for a document by id;
   `doc_document_versions` is under the standard `tenant_isolation` policy, so a
   cross-org id is indistinguishable from a nonexistent one.
3. **Both directions of the storage API assert the prefix.**
   `get_doc_document(ref, org_id)` and `delete_doc_document(org_id, ref)` each
   refuse a ref that does not start with `{org_id}/docs/` — the same
   defence-in-depth `get_budget_attachment` and `get_ticket_attachment` already
   carry, in both directions, because a check present on only one direction is
   worth little.
4. **No credential and no URL for the bucket ever leaves the control plane** —
   see §0.3.

### 0.3 No presigned URL is ever minted for a document. Not one, not ever

`ObjectStore` has a careful, well-reasoned presigning facility, and this module
must not use it. Three reasons, and the third is decisive.

- A presigned URL is a **bearer credential**: anyone holding it performs the
  operation it was signed for, with no token, no RBAC check, and no audit
  event. Invariants 4 and 6 both fall.
- The presign machinery exists to hand a grant to **an endpoint, inside a signed
  job envelope**. There is no job here and no endpoint. A console user already
  has an authenticated channel to the API.
- **In this deployment the bucket is on the console's own origin.**
  `deploy/docker-compose.yml` routes `PathPrefix('/${ITS_S3_BUCKET}/')` with
  `GET`/`HEAD` to MinIO on the same hostname and the same Traefik entrypoint
  that serves the console at `PathPrefix('/')`. A presigned GET for an uploaded
  HTML file would therefore be an unauthenticated, same-origin, script-executing
  URL for the whole tenant. That is the exact stored-XSS this spec is required
  to prevent, delivered by the mitigation.

**Document bytes flow through the API process and nowhere else.** That is what
makes the size cap in §4.2 load-bearing rather than arbitrary.

### 0.4 The transport is always download-shaped; the viewer never gets a document context

The task named three safe answers — a separate origin, a download-only path, or
a sandboxed viewer. **We take the sandboxed viewer, and we build the sandbox out
of three walls instead of one hostname.** §5 specifies each wall; the summary:

- **Wall 1 — the transport.** The content route always responds
  `Content-Type: application/octet-stream`,
  `Content-Disposition: attachment; filename="<sanitized>"`,
  `X-Content-Type-Options: nosniff` (already applied globally by
  `app/api/security_headers.py`), and `Cache-Control: no-store`. Direct
  navigation to a document URL downloads a file. It never renders a page,
  whatever the bytes are. This is the same posture Budget and Service Desk
  already ship.
- **Wall 2 — the type is decided by the server from the bytes** (§0.5), not by
  anything the client said, and it is stored in a column.
- **Wall 3 — the console's render map has exactly one entry per server-declared
  viewer kind**, and no entry produces a document context for a type that can
  carry script. Bytes are fetched into memory with the caller's own token and
  rendered from a `Blob` whose MIME type comes from that map — never from the
  filename, never from a response header.

**Why not the separate origin, which is the strictly stronger answer.** It costs
a second hostname, a certificate covering it, a Traefik router, a CORS and
auth-forwarding story for a cross-origin authenticated `fetch`, and a change to
`deploy/GO-LIVE.md`. On a self-hosted single-node stack that an operator brings
up by hand, a second origin that is *silently* misconfigured — resolving to the
same host, or falling back to the console origin — is worse than not having one,
because the whole containment is then a belief. **The upgrade path is named and
narrow: if the console ever needs to render uploaded HTML, SVG, or XML as a
document, the second origin gets built first, and it is not optional then.**
Until that day the answer is that those types are never rendered at all.

**What this trades away, stated plainly.** With a separate origin, wall 3 could
be wrong and nothing would happen. Here, wall 3 is a client-side constant, and
if someone later adds `text/html` to the render map, walls 1 and 2 do not save
us — a `Blob` of type `text/html` opened from the console origin executes in it.
That constant is therefore pinned by a test (§13, F3c) rather than by care.

Overturnable: §17 D3.

### 0.5 The declared content type is never believed, because the client never gets to declare one

There is no multipart parser in this project (Budget, Service Desk, Deploy and
the Portal all note this). Uploads take the raw request body as bytes with the
filename in a query parameter. That accident is an opportunity: **this module
accepts no client-supplied content type at all**, so "a file whose content-type
lies" is not a case to handle — it is a sentence that cannot be formed.

The stored `content_type` and `viewer` are computed server-side from the leading
bytes (§4.3), with the filename extension used **only to disambiguate within a
sniff class** — which OOXML flavour a ZIP container is, whether decodable text
is Markdown or CSV or plain. A file named `diagram.pdf` whose bytes are a ZIP is
stored as a ZIP, is download-only, and the console labels it as a ZIP.

A sniff/extension mismatch is **labelled, not refused**. Refusing would break
the legitimate case (a `.dat` that really is a PDF) and buys nothing, because
the viewer keys off the sniff and never off the extension. Overturnable: §17 D4.

### 0.6 Versioning ships in v1, because immutable-per-version is *cheaper* than overwrite

ITGlue versions documents, and the obvious v1 shortcut is one blob per document,
overwritten on re-upload. That shortcut is not actually cheaper here:

- The object key already needs a unique trailing segment (Service Desk's
  `{attachment_id}` exists for exactly this reason). Using a version id costs
  nothing extra.
- An overwrite is the one operation in this design that can **destroy the only
  copy of a file** on a mis-click, with no undo. Every other destructive act in
  this module is soft.
- The module's whole existing posture is snapshot-on-write (`doc_versions`,
  spec.md §4). A document that silently loses its predecessor would be the odd
  one out on its own page.

So: a `doc_documents` row plus append-only `doc_document_versions` rows, each
with its own immutable object. Uploading a new version never touches the old
object. Old versions stay downloadable. Nothing is pruned in v1; retention is
deferred (§15.9) and the quota in §4.2 is what bounds the growth in the
meantime.

### 0.7 Documents use the *same* target vocabulary articles already use

`doc_links.target_type` is `device | device_group | credential | record |
ipam_subnet | ipam_address`. Documents get their own link table (§2.3) over the
identical vocabulary, resolved by the identical dictionary.

Rejected: **a second, different vocabulary tuned to documents.** The shipped
code carries a comment about precisely this hazard — the `_LINK_TARGET_MODELS`
dict and `LinkCreate`'s regex must move together or you get a 404 on one side
and a 422 on the other. Two vocabularies would be that trap, permanently, in
two modules that share a page.

**`ticket` is deliberately not a target.** Tickets already have attachments,
with their own terminal-status freeze and their own portal exposure rules.
A second file-attachment path onto the same object would need to answer both of
those questions again, differently. §15.5.

**There is no `org` target type.** Every document already carries `org_id`; a
document with no links *is* the organisation-level document, and that is what
the console's unfiltered list shows.

### 0.8 Search is metadata in v1. Extraction is named, costed, and deferred

Searchable in v1: **title, description and category** through one more GIN FTS
index in the same shape `doc_articles` already uses, plus **filename** through an
`ilike` against the current version — the same two-mechanism split the shipped
search already uses for articles (FTS) and record names (`ilike`). One more
`kind` in the existing `GET /docs/search` response.

Deferred: **content extraction**. It is three dependencies with three licence
reviews (a PDF text extractor, an OOXML reader, and OCR for the scanned
contracts that are most of what an IT team actually files), plus a background
worker, plus a re-index pass over everything already uploaded, plus a decision
about what a failed extraction means. The forward path is additive and stated
here so it is not re-derived: a nullable `extracted_text` column on
`doc_document_versions`, a Celery task that fills it, and a new migration that
widens the FTS index expression. Nothing in the v1 schema blocks it.

**Say the limitation out loud in the console** (§11.6). A search box that
silently only searches filenames teaches people the library is empty when it is
not.

### 0.9 The object store is a *soft* dependency, and the page has to behave like it

`app/core/health/probes.py::probe_objectstore` is explicitly a soft probe —
object storage being down never gates readiness. That is a deliberate platform
decision and this module inherits it: with MinIO down, the documentation page
must still list documents, articles and records from PostgreSQL, and must fail
**loudly and specifically** on the byte paths. A metadata list rendered as an
empty list, or a viewer that shows a spinner forever, would tell an operator
their library is gone. §11.7 and F8.

### 0.10 A `docs.manage` holder can fill the shared bucket, so v1 has a quota

The bucket is shared: agent binaries, deployment packages, OS images, **backup
repositories**, job artefacts and session recordings all live in it under their
own prefixes. An unbounded document upload path is therefore not a documentation
problem — a `docs.manage` holder (technician tier) filling the volume stops
backups from writing.

v1 ships a **per-organisation total-bytes cap**, a module constant, checked with
one `SUM` before the write (§4.2). Rejected for v1: a per-org override in
`organizations.settings`. It is the right eventual shape and matches the
`console_email_domain` precedent, but it needs a settings-route change and a UI
this spec is not otherwise touching. The constant is named in one place so the
override is a small later change. Overturnable: §17 D6.

> **Amended 2026-10-29 (MSP clients phase 2, operator decision after the final
> review): the cap is per client.** Each client has its own quota of
> `CLIENT_DOCUMENT_QUOTA_BYTES` (5 GiB), and the documents for every client
> (no `client_id`) are one bucket more with the same cap. Usage is counted
> explicitly for the document's owner — versions joined to `doc_documents`
> and kept to that document's `client_id` (or to the documents with none) —
> never as "whatever the caller's scope can see", so an every-client admin and
> a technician narrowed to X get the same number for the same upload. The 507
> names the owner's usage: the caller's own client's, or the every-client
> bucket's, which only an every-client person can write to. **The documents'
> share of the shared volume is therefore bounded by the cap times the number
> of clients plus one, and grows as clients are added**: size the volume, which
> backups share, for that. (Before this, the `SUM` ran under the caller's
> client policy: each narrowed scope could fill its own 5 GiB, while an
> every-client admin was measured against the organisation's total.)

---

## 1. Scope

**In scope:** the document and version model (§2), object storage and its tenant
isolation (§3), upload (§4), download and the in-console viewer (§5),
versioning (§6), relationships (§7), search (§8), permissions (§9), API (§10),
the console screen (§11), tenancy/RBAC/audit (§12), tests and falsifiers (§13),
required cross-module changes (§14).

**Out of scope and not by accident:** §15, item by item.

**Dependencies:** the shipped documentation module (`doc_links`' target
resolution and the `/docs` page), `app.core.storage.ObjectStore`,
`app.core.attachments.sanitize_attachment_filename`, `app.core.http`'s bounded
body helpers, `app.core.audit`. All exist. Nothing new joins the deploy stack.

---

## 2. Data model

Three tables. All three carry `org_id` and get the standard `tenant_isolation`
RLS policy with `FORCE ROW LEVEL SECURITY` (`docs/04-data-model.md`
Conventions — a tenant-scoped table without an RLS policy is a security bug).

### 2.1 `doc_documents`

`id` (uuid7 pk), `org_id` (FK organizations), `title` (str 300),
`description` (Text, default ""), `category` (str 100, default "General"),
`archived` (bool, default false), `current_version` (int, default 0),
`created_by` / `updated_by` (FK users, nullable), `created_at` / `updated_at`.

- `category` is free-form, exactly as `doc_articles.category` is, so the two
  group under the same headings on the same page. No folder tree — §15.8.
- `current_version` starts at **0**, not 1: a document exists before its first
  file does (§4.1), and a zero is the honest way to say so. This differs
  deliberately from `doc_articles.current_version`, which starts at 1 because an
  article is its own content.
- Index `(org_id, category)`, matching `ix_doc_articles_org_category`.

### 2.2 `doc_document_versions`

`id` (uuid7 pk), `org_id` (FK organizations),
`document_id` (FK doc_documents, ondelete CASCADE), `version` (int),
`object_key` (Text — the `s3://bucket/key` ref, or `""` once purged),
`filename` (str 255 — the sanitized **display** name),
`byte_size` (int), `content_type` (str 100 — server-sniffed, §4.3),
`viewer` (str 16 — `pdf | image | markdown | text | none`),
`sha256` (str 64, lowercase hex), `uploaded_by` (FK users, nullable),
`created_at`, `purged_at` (datetime, nullable),
`purged_by` (FK users, nullable).

- Unique `(document_id, version)`. Index `(org_id, document_id, version)`.
- **Append-only.** No route updates a version row except purge (§6.3), which
  sets `purged_at`, `purged_by`, and clears `object_key` to `""`.
- `sha256` is computed over the bytes already in memory. It is the handle for
  the storage-integrity falsifier (F7b) and lets the console say "this is
  byte-identical to version 3" without a second download.
- `viewer` is stored rather than derived at read time so the console's render
  map has a single, server-controlled key (§0.4 wall 3) instead of a client-side
  re-derivation from `content_type`.

### 2.3 `doc_document_links`

`id` (uuid7 pk), `org_id`, `document_id` (FK doc_documents, ondelete CASCADE),
`target_type` (str 20), `target_id` (uuid), `created_at`.
Unique `(document_id, target_type, target_id)`. Index `(org_id, target_type,
target_id)` — the device-detail tab queries by target, not by document.

`target_type` ∈ `device | device_group | credential | record | ipam_subnet |
ipam_address` — §0.7. Resolved through the **same** `_LINK_TARGET_MODELS`
dictionary `documentation/service.py` already defines, imported, not copied. A
target is resolved inside the tenant session, so a foreign-org target is
indistinguishable from a nonexistent one (404 either way) — the shipped
module's own reasoning, unchanged.

A `credential` link carries **metadata only**, never a secret, exactly as
article links do. Invariant 3 is untouched: nothing in this module reads the
vault.

### 2.4 Migration

One migration, the next free revision (`0075` at the time of writing — take
whatever `alembic heads` reports, other work is in flight). It creates the three
tables, enables and forces RLS with the `tenant_isolation` policy on each,
creates the FTS index of §8, and seeds the one new permission code of §9 with
its role grants. Forward-only; no shipped migration is edited.

---

## 3. Object storage

### 3.1 Key layout

```
s3://<bucket>/{org_id}/docs/documents/{document_id}/{version_id}
```

Added to `docs/04-data-model.md`'s "Object storage layout" block and to
`deploy/README.md` (§14). Every segment is server-derived from ids the caller
was already authorized for. No client-supplied bytes reach the key.

### 3.2 New `ObjectStore` methods

Four, mirroring the ticket-attachment set exactly — including its comments'
reasoning, which is not this module's to re-derive:

- `_doc_document_key(org_id, document_id, version_id) -> str`
- `async put_doc_document(org_id, document_id, version_id, data) -> str`
- `async get_doc_document(ref, org_id) -> bytes`
- `async delete_doc_document(org_id, ref) -> None`

`get_` and `delete_` **both** refuse a ref that does not start with
`s3://{bucket}/` and then **both** refuse a key that does not start with
`{org_id}/docs/`. The key is always server-derived, so neither assertion should
ever trip; they exist because a defence-in-depth check present in only one
direction is worth very little, and the codebase already says so twice.

**No `presign_doc_*` method is added.** §0.3. F2 makes the absence a test rather
than a habit.

### 3.3 Tenant isolation on the stored object — the whole argument in one place

The object store has no RLS. What replaces it:

| Attack | What stops it |
|---|---|
| Guess another org's key and ask for it | No route takes a key. Routes take a document id, resolved under RLS. |
| Read a key out of an API response | No response contains an object key. Not in list, detail, version, or search shapes. |
| Craft a filename that escapes the prefix | The filename never reaches the key. `sanitize_attachment_filename` rejects any path segment before that, and `os.path.basename` runs anyway. |
| Get the server to fetch a foreign ref | `get_doc_document` asserts `{org_id}/docs/`. |
| Get a URL to the bucket | None is ever minted (§0.3). |
| Reach the bucket directly | MinIO denies anonymous access — a bucket created by `_ensure_bucket` carries no policy, and MinIO's default for a policy-less bucket is deny. `deploy/docker-compose.yml` states this and warns against `mc anonymous set download`. |

Falsifiers: F1a–F1e.

---

## 4. Upload

### 4.1 Two steps, following Deploy's package/artifact precedent

`POST /docs/documents` creates the row from JSON metadata. `POST
/docs/documents/{id}/versions?filename=…` sends the raw bytes. There is no
multipart parser in this project, so metadata and bytes cannot share a request
without adding one.

A document with zero versions is a legal, visible state: the console renders it
as "No file uploaded yet" with an Upload control, not as an error. This mirrors
`deploy`'s package-before-artifact shape.

### 4.2 Limits, and what enforces each

- **Per file: `MAX_DOCUMENT_BYTES = 25 * 1024 * 1024` (25 MiB).** Both guards
  from `app/core/http.py`, in this order, exactly as Budget and Service Desk
  call them: `reject_oversized_content_length(request, MAX_DOCUMENT_BYTES)`
  first (cheap, before any DB or object-store work), then
  `read_bounded_body(request, MAX_DOCUMENT_BYTES)` (the guard a chunked or
  lying client cannot route around). A third check on `len(data)` inside the
  service is defence in depth on a body already read, not a substitute.
- **Why 25 MiB and not 100.** `read_bounded_body` buffers the whole body in
  API-process memory, and §0.3 means these bytes cannot be streamed straight to
  the bucket by the client. Peak memory is therefore
  `concurrent_uploads × 25 MiB`. 25 MiB comfortably holds a scanned contract or
  a diagram export and keeps ten simultaneous uploads inside a quarter of a
  gigabyte. Overturnable: §17 D5 — and note that raising it raises that product
  linearly, which is the thing to argue about, not the file size.
- **Per client: `CLIENT_DOCUMENT_QUOTA_BYTES = 5 * 1024 * 1024 * 1024`
  (5 GiB)**, §0.10 (per organisation until the amendment of 2026-10-29 there).
  Checked with one `SELECT COALESCE(SUM(v.byte_size), 0) FROM
  doc_document_versions v JOIN doc_documents d ON d.id = v.document_id WHERE
  d.client_id IS NOT DISTINCT FROM <the document's client> AND v.purged_at IS
  NULL` inside the tenant session, before the object write, in the two passes
  §4.4 sets out. Over quota → **507**, no row, no object.
  **Two concurrent uploads can both pass this check.** The overshoot is bounded
  by `concurrency × MAX_DOCUMENT_BYTES` and is not worth an advisory lock on a
  soft, operator-visible limit. Stated rather than fixed.
- **Filename:** `app.core.attachments.sanitize_attachment_filename`, imported,
  not re-derived. Its ASCII-only `fullmatch` allow-list and its
  reject-don't-truncate posture are both load-bearing and both have their
  reasons recorded at the shared implementation; do not "simplify" either. A
  rejected filename is a 400 before the body is read.

### 4.3 Type detection

`app/modules/documentation/filetypes.py`, one pure function
`sniff(data: bytes, filename: str) -> tuple[content_type, viewer]`, unit-tested
directly.

| Leading bytes | `content_type` | `viewer` |
|---|---|---|
| `%PDF-` | `application/pdf` | `pdf` |
| `\x89PNG\r\n\x1a\n` | `image/png` | `image` |
| `\xff\xd8\xff` | `image/jpeg` | `image` |
| `GIF87a` / `GIF89a` | `image/gif` | `image` |
| `RIFF` with `WEBP` at offset 8 | `image/webp` | `image` |
| `PK\x03\x04` | OOXML type by extension (`.docx`, `.xlsx`, `.pptx`, `.vsdx`), else `application/zip` | `none` |
| `\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1` | legacy Office type by extension, else `application/x-ole-storage` | `none` |
| no magic match, decodes strict UTF-8 (or UTF-8 BOM), contains no `\x00`, and ≤ 2 MiB | `.md`/`.markdown` → `text/markdown` `markdown`; `.csv` → `text/csv` `text`; **everything else** → `text/plain` `text` | as shown |
| as above but **> 2 MiB** | `text/plain` | `none` |
| anything else | — | **refused, 415** |

**HTML, SVG, XML and anything else that is really text.** These decode as UTF-8,
so they land in the text branch and are **accepted** — stored as `text/plain`
with `viewer="text"`, and displayed as *source*, character by character, in a
`<pre>`. That is deliberate and it is useful: viewing a firewall configuration
export or a `.drawio` file as text is a real thing an operator does.

The rule that makes it safe is that the text branch emits **exactly three**
content types — `text/markdown`, `text/csv`, `text/plain` — and never a type
that names a rendering engine. `text/html` and `image/svg+xml` are values this
function cannot produce, for any input, at all. So the stored metadata itself
can never become a rendering instruction, which is the property F3b pins. An
SVG is never rendered as an image; if the operator wants that, it is the second
origin (§0.4), not an exception here.

- The 2 MiB text ceiling is a *rendering* limit: pushing 25 MiB of text into the
  DOM hangs the tab. Over it the file is stored and downloadable, just not
  previewed.
- **UTF-16 text is refused.** Re-save as UTF-8, or zip it. Honest and cheap;
  guessing encodings is a different project.
- The refusal message names the accepted list, so the wall is discoverable
  rather than mysterious.

**What the allow-list is and is not.** It bounds *what the viewer can be asked
to render* and makes the library's contents predictable. It is **not** a malware
control, and must not be described as one: ZIP is on the list, so arbitrary
bytes can be stored by zipping them, deliberately — it is the escape hatch for
what the table genuinely refuses, which is binary with no recognised magic
(`.exe`, `.msi`, `.iso`, `.dmg`, `.pcap`, `.mp4`, a vendor's proprietary
export). What makes an
uploaded binary inert is elsewhere: no agent can resolve a docs key, no job type
references one, and no presigned URL exists (§0.3), so this platform will never
hand an uploaded file to an endpoint. §15.10 states the missing antivirus
honestly.

### 4.4 Ordering inside the route

Cheapest first, and the reasoning is Service Desk's, carried over because it was
earned there:

1. `reject_oversized_content_length` — no DB, no wire read.
2. `sanitize_attachment_filename` — no DB, no wire read.
3. Load the document under `org_session` — 404 for missing or cross-org
   **before** a byte of body is read. Reading first would let any `docs.manage`
   holder buffer 25 MiB against a nonexistent id.
4. Reject if archived (a 409; an archived document takes no new versions, the
   same freeze Service Desk applies to terminal tickets).
5. **Quota, first pass** — `SUM(byte_size)` for the document's owner (its
   client, or the documents for every client; §4.2). If it is already at
   or above the cap, 507 now, before reading anything. This pass cannot see the
   incoming size, which is why there is a second one.
6. `read_bounded_body`.
7. `sniff`; 415 on refusal.
8. **Quota, second pass** — `usage + len(data)` against the cap; 507 before the
   object write. Both passes are required and neither is redundant: the first
   avoids buffering 25 MiB for an org that is already full, the second is the
   one that actually enforces the limit.
9. sha256, `put_doc_document`, insert the version row, bump
   `current_version`, audit, flush.

**Known limitation, carried not fixed:** a failure between the object write
succeeding and the transaction committing leaves an orphaned object with no row
pointing at it. This is the identical, already-accepted limitation in
`service_desk.threads.add_attachment`. It leaks storage; it leaks nothing else,
because an unreferenced key is unreachable by every path in §3.3.

---

## 5. Download and the viewer

### 5.1 One byte-serving route, and it is always a download

`GET /docs/documents/{id}/versions/{n}/content` — `docs.read`, audited on every
call (§12.3). Response:

```
Content-Type: application/octet-stream
Content-Disposition: attachment; filename="<doc_document_versions.filename>"
Cache-Control: no-store
X-Content-Type-Options: nosniff        (global middleware)
```

- `application/octet-stream` **regardless of the sniffed type**, including for
  PDFs and images. The stored `content_type` is metadata for the console's
  render map, never a response header. Wall 1 of §0.4.
- `Cache-Control: no-store` is set on this route explicitly rather than by
  widening `security_headers.py`'s `/checkout` suffix rule — a suffix match that
  grows is how one of these eventually catches a route nobody meant. A
  documentation library holds signed contracts; those should not sit in a shared
  workstation's disk cache.
- A purged version (§6.3) is a **410 Gone**, not a 404: the version existed and
  was deliberately destroyed, and the audit log says by whom. Conflating that
  with "never existed" loses the only fact worth keeping.

### 5.2 The viewer, per `viewer` kind

The console fetches the bytes with the caller's own token (the shipped
`downloadTicketAttachment` pattern: `fetch` with the Authorization header →
`Blob`), then renders from a **map with exactly one entry per kind**:

| `viewer` | Blob MIME constructed | Rendered as |
|---|---|---|
| `pdf` | `application/pdf` | `<embed type="application/pdf" src={blobUrl}>` in a fixed-height panel, with a "if the preview does not load, download the file" line and a Download button beneath it |
| `image` | looked up in a **four-entry client-side map** keyed on the stored `content_type` (`image/png`, `image/jpeg`, `image/gif`, `image/webp`); anything else falls through to the `none` row | `<img src={blobUrl}>`, bounded by `max-width:100%` and a max height |
| `markdown` | not used — bytes decoded as UTF-8 | the **existing** `renderMarkdown()` and its allow-list sanitizer, unchanged and un-forked |
| `text` | not used — bytes decoded as UTF-8 | a `<pre>` whose content is set as **text**, never `innerHTML` |
| `none` | `application/octet-stream` | no preview: a panel naming the type and offering Download |
| anything else | `application/octet-stream` | falls through to `none` |

The last row is the point: an unknown `viewer` value degrades to download, it
does not degrade to guessing.

**Why `<embed>` for PDF and not a sandboxed iframe.** A blob URL carries the
creating origin; an `<iframe sandbox>` *without* `allow-same-origin` gets an
opaque origin and Chrome then refuses to load the blob at all, while *with*
`allow-same-origin` the sandbox attribute is no longer isolating anything. The
sandboxed-iframe-over-blob combination does not do what its name suggests. What
does the isolating here is the browser's own PDF viewer, which runs in a
separate process and cannot touch the embedder's DOM. The residual risk is a
PDF-reader exploit, which is a different threat from stored XSS and is not one
this application can mitigate.

**Why images are safe.** Scripts and external references are disabled for
content loaded through `<img>`. The image types above cannot execute in that
context.

**SVG is accepted, and shown as source text, never as an image.** It is XML
that can carry script, and it is exactly the file a network diagram arrives as —
which is why it needs saying out loud rather than being forgotten. §4.3 stores
it as `text/plain`, so the viewer shows its markup in a `<pre>` and the browser
is never asked to parse it. Export a PNG to get a picture. If the operator wants
SVG *rendered* in the console, that is the trigger for the second origin (§0.4),
not for an exception here. Overturnable: §17 D3.

**Nothing in the document viewer assigns `innerHTML`** except the single
`dangerouslySetInnerHTML` that consumes `renderMarkdown()`'s already-sanitized
output — the same call the article view has shipped with. No second sanitizer,
no second allow-list.

---

## 6. Versioning

### 6.1 Upload

Each `POST .../versions` writes a new immutable object and a new row at
`version = current_version + 1`, then bumps `doc_documents.current_version`.
The previous object is never touched.

### 6.2 Reading an old version

`GET /docs/documents/{id}/versions` lists every version newest-first with
filename, size, type, sha256, uploader and timestamp. The content route takes
an explicit version number, so any version is downloadable and any *viewable*
version is viewable. There is no rollback verb in v1: "make version 2 current
again" is `download version 2, upload it` — one more append, no new concept, and
the history stays true. Overturnable: §17 D7.

### 6.3 Purge — the only irreversible act in this module

Archiving a document is soft and keeps the bytes. But a file uploaded by mistake
may contain a password or personal data, and "we archived it" is not an answer
to that. So one destructive path exists, and it is gated on its own permission:

`POST /docs/documents/{id}/versions/{n}/purge` — `docs.document.purge` (§9).
It deletes the object, sets `purged_at`/`purged_by`, and clears `object_key` to
`""` so no code path can even attempt a fetch. The row survives as a
**tombstone**: filename, size, sha256, uploader and timestamps remain, and the
audit event records them too. A destroyed file leaves a mark; that is the point.

Purging the current version does not decrement `current_version`. The document's
current version is then a tombstone, and the console says so.

There is no hard delete of a document. Archive plus purge covers every real
need without a route whose failure mode is losing an audit trail.

---

## 7. Relationships

Per §0.7. Two routes (`POST`/`DELETE` on `/docs/documents/{id}/links`), the
shared target resolver, the shared 404-on-foreign-org behaviour, the same unique
constraint, the same `link_added`/`link_removed` audit shape.

Where the links pay off:

- **Document detail** lists the objects the document describes, each a link into
  that object's own page.
- **The list filter** `?target_type=device&target_id=…` answers "what documents
  cover this device".
- **Device detail's existing Docs tab** (shipped, `DeviceDetailPage.tsx`) gains
  a Documents list beside its Articles list, from that filter. One tab, two
  lists — not a second tab (§11.5).

---

## 8. Search

`GET /docs/search?q=` gains a third `kind`, `document`, beside `article` and
`record`. Same route, same permission, same response shape
(`{kind, id, title, category, rank}`), so the console's existing `groupHits`
helper needs one more group and no new query.

Index: `GIN (to_tsvector('english', title || ' ' || description || ' ' ||
category))` on `doc_documents` — three columns, all NOT NULL with defaults, so
the expression is never NULL and never silently indexes nothing. `filename`
lives on `doc_document_versions`, not on the indexed row, so it is matched
separately with `ilike` against the **current** version only; older versions'
filenames are not searched, and §11.6 says so on screen. Ranked with `ts_rank`;
archived documents excluded, matching articles and records.

The console states, next to the search box, that document *contents* are not
searched (§11.6). §0.8 for why, and for the additive path when that changes.

---

## 9. Permissions

**One new code.** The other two already exist and already carry the right
grants.

| Code | Meaning | Roles |
|---|---|---|
| `docs.read` *(existing)* | List documents, read metadata and version history, **download and view bytes** (audited) | super-admin, org-admin, technician, auditor, read-only |
| `docs.manage` *(existing)* | Create a document, upload a version, edit metadata, link/unlink, archive | super-admin, org-admin, technician |
| `docs.document.purge` **(new)** | Irreversibly destroy a stored version's bytes | **super-admin, org-admin only** |

- **Download reuses `docs.read` rather than getting its own code.** A separate
  download permission would, on the existing grants, leave an auditor able to
  see that a signed contract exists and unable to read it — which is the
  opposite of an auditor. The control on reading a document is the audit event
  (§12.3), not a second gate.
- **`docs.document.purge` is not folded into `docs.manage`.** Editing the
  library and destroying evidence are different acts with different blast
  radii — the same reasoning `health.remediate` records for not folding into
  `health.admin`. A technician can archive; only an admin can destroy.
- Three dot-segments is established, not novel: `remote.recordings.read`,
  `licenses.keys.read`, `health.action.admin` and `ipam.sweep.manage` all use it.
  Seeded as the tuple `("docs.document.purge", "docs", "document.purge")`,
  matching how `0010` and `0018` seed theirs.
- Added to `app/core/rbac.py::PERMISSIONS` with a description that says it is
  irreversible. The RBAC registry test updates accordingly.

---

## 10. API

All under `/api/v1/docs/`, all inside `org_session`.

```
POST   /docs/documents                                  docs.manage   201
GET    /docs/documents                                  docs.read
         ?category= &target_type=&target_id= &include_archived=false
         &limit=100 &offset=0   -> {items, total}
GET    /docs/documents/{id}                             docs.read
PUT    /docs/documents/{id}                             docs.manage   (title/description/category)
POST   /docs/documents/{id}/archive                     docs.manage
GET    /docs/documents/{id}/versions                    docs.read
POST   /docs/documents/{id}/versions?filename=…         docs.manage   201, raw body
GET    /docs/documents/{id}/versions/{n}/content        docs.read     audited, octet-stream
POST   /docs/documents/{id}/versions/{n}/purge          docs.document.purge
POST   /docs/documents/{id}/links                       docs.manage   201
DELETE /docs/documents/{id}/links/{link_id}             docs.manage
GET    /docs/search?q=                                  docs.read     (+ kind="document")
```

Response shapes carry `id, title, description, category, archived,
current_version, updated_at, link_count` on list items, and add
`current` (the current version's `{version, filename, byte_size, content_type,
viewer, sha256, created_at, uploaded_by, purged_at}` or `null`) plus `links` on
detail. **No shape, anywhere, contains `object_key`.** F1b.

Status codes: 400 bad filename; 404 missing or cross-org document, version, or
link target; **409** either an archived document being sent a new version or a
duplicate link; 410 purged version; 413 over per-file cap; 415 unsupported type;
507 over org quota.

Route ordering: every path above is under the literal prefix `/docs/documents`,
so none of them shadows the shipped `/docs/articles`, `/docs/records` or
`/docs/search`, and none shadows another — the `{id}` segment is always preceded
by a literal. This is stated because `service_desk/api.py` had to fix a real
shadowing bug of exactly this kind; it is checked here, not assumed.

---

## 11. The console

The screen is `/docs` — the shipped `DocsPage.tsx`, extended. No new route.

### 11.1 The stat row

Four tiles today: Articles, Records, Linked references, Categories.
**Replace Categories with Documents.** Categories is the weakest of the four —
it links to the same list Articles links to and decomposes into headings rather
than rows — and a fifth tile would break the `columns={4}` grid. Tone `blue`
(a thing you can open), `zeroLabel` "no files uploaded yet". Overturnable:
§17 D8.

The shipped `count()` helper's rule holds: a pending or failed query renders an
em dash, never a zero. A dead backend and an empty library must not read alike.

### 11.2 The left column

A third section beneath Articles and Records: **Documents**, grouped by category
like Articles, each row showing the title, the filename, a size, and a small
type badge. `docs.manage` holders get an **Upload document** button in the
section header, matching the shipped "New record" placement.

Search results gain a Documents group, rendered only when non-empty — the
shipped rule (an empty group heading reads as a bug) applies to the new group
too.

### 11.3 The main pane — document view

Header: title, category badge, `v{current_version}` badge, filename, size,
type. Actions: **Download**, **Upload new version** (`docs.manage`), **Edit
details** (`docs.manage`), **History**, **Links**, **Archive** (`docs.manage`,
behind the shipped `ConfirmDialog`), **Purge** (`docs.document.purge`, behind a
separate `ConfirmDialog` whose body says the bytes are destroyed and that the
record of the destruction is kept).

**Purge is per version, and the control follows the version.** The header's
Purge acts on the version currently displayed and names it in the dialog
("Destroy version 4, `contract-2026.pdf`, 2.1 MB"); every other version is
purged from its own row in the History drawer. There is no "purge this
document" control, because there is no operation behind it — a document is a
row and a name, and destroying every version one at a time is the honest shape
of destroying its contents.

Body: the viewer of §5.2 for the current version, or the "no preview" panel, or
"No file uploaded yet" for a zero-version document, or the tombstone notice for
a purged current version.

History drawer: the version list, each row downloadable and (for the viewable
kinds) selectable into the viewer. Timestamps through `formatDateTime(ts, zone)`
with the operator's profile timezone — the shipped rule, and version history is
precisely where a wrong zone is most expensive.

### 11.4 Linkability

Selection lives in the query string as `?document=<id>`, pushed not replaced,
exactly as the shipped `?article=<id>` does. A runbook PDF that cannot be pasted
into a ticket is half a runbook, and this page has already had that audit
finding fixed once; the new object must not re-introduce it.

### 11.5 Device detail

The existing **Docs** tab gains a Documents list beside its Articles list, from
`?target_type=device&target_id=`. One tab, two lists. A second tab would split
"what is written about this machine" across two clicks for no gain.

### 11.6 Saying what search does

One line of muted text under the search box: document *contents* are not
searched — titles, filenames, descriptions and categories are. §0.8.

### 11.7 Error and empty states

Distinct, always — the shipped page's own hard-won rule (a failed version query
must not render as "no history"):

- Metadata query fails → the error, never an empty list.
- **Object store down** → the list still renders from PostgreSQL; the viewer
  panel and Download show the dependency error and name it. §0.9, F8.
- Upload refused → the reason, on the field: filename rejected, too large, type
  not accepted (with the accepted list), quota reached, document archived.
- Download refused → surfaced on screen. The shipped `downloadError` state on
  the ticket attachment panel exists because `void download(...)` once had no
  rejection path and a failed download looked exactly like no click at all.
  Same shape here.

---

## 12. Tenancy, RBAC, audit

### 12.1 Tenancy (invariant 5)

Every route runs inside `org_session`. All three tables are RLS-protected and
`FORCE`d. The object store's isolation is §3.3, which is the part RLS cannot do
and which is therefore designed rather than assumed.

### 12.2 RBAC (invariant 6)

Server-side on every route via `require_permission`. The console hides controls
the caller cannot use; the server refuses regardless. Purge is admin-tier.

### 12.3 Audit (invariant 4)

Every mutation, and one read. Actions, all `target_type="doc_document"`:

| Action | When | Metadata |
|---|---|---|
| `docs.document_created` | POST /documents | title, category |
| `docs.document_updated` | PUT, when something actually changed | changed fields |
| `docs.document_version_uploaded` | POST /versions | version, filename, byte_size, content_type, sha256 |
| `docs.document_downloaded` | **every** `/content` response | version, filename |
| `docs.document_archived` | POST /archive | — |
| `docs.document_version_purged` | POST /purge | version, filename, byte_size, sha256 |
| `docs.document_link_added` / `_removed` | link routes | target_type, target_id |

**Opening the viewer writes a download event, and that is correct.** The server
cannot distinguish "viewed" from "saved" — the bytes left the control plane
either way, and the client could always keep them. An audit log that claimed a
distinction it cannot verify would be lying in the one place lying is most
expensive. Consequence, stated so it is not a surprise: browsing a document
library generates audit volume proportional to reading, not to writing.

No-op updates write nothing and bump nothing, matching `update_article`.

---

## 13. Tests, and the falsifier for every guarantee

CLAUDE.md: a claim about running code is owed a test and a stated falsifier.
Each guarantee below names the observation that would show it broken.

### F1 — Tenant isolation on the stored object (§3.3)

*Broken if:* a caller authenticated to org B obtains org A's bytes, key, or
metadata.

- **F1a** `GET /docs/documents/{id}` for an org-A document under an org-B token
  → 404. Same for versions, content, links.
- **F1b** No API response body — list, detail, versions, search, link — contains
  the substring `s3://` or the key format. Asserted over every documents route,
  not spot-checked.
- **F1c** `get_doc_document(ref, org_id)` raises for a ref outside
  `{org_id}/docs/`; `delete_doc_document` raises for the same ref. Both
  directions, one test each.
- **F1d** Every key `_doc_document_key` returns starts with `f"{org_id}/docs/"`.
- **F1e** The repo's RLS-completeness test covers all three new tables (it fails
  on a tenant-scoped table with no policy — that is why it exists).

### F2 — No presigned URL for a document (§0.3)

*Broken if:* any code path signs a URL for a `{org}/docs/` key.

- **F2a** `ObjectStore` exposes no attribute matching `presign_doc*`.
- **F2b** No documents-route response contains `X-Amz-Signature` or
  `X-Amz-Credential`.

### F3 — XSS containment (§0.4, §5)

*Broken if:* attacker-supplied bytes are ever rendered in a document context on
the console's origin.

- **F3a** For every accepted type — including a `.txt` whose content is
  `<script>alert(1)</script>`, a `.md` containing raw HTML, and a `.pdf`-named
  ZIP — the `/content` response has `Content-Type: application/octet-stream`,
  `Content-Disposition: attachment; …`, and `Cache-Control: no-store`.
- **F3b** An uploaded `.html`, `.svg` and `.xml` each store
  `content_type="text/plain"` and `viewer="text"` (§4.3) — never `text/html`,
  never `image/svg+xml` — and render as literal characters. Asserted as a
  property of `sniff()`: over the whole accepted corpus it emits no content type
  outside the enumerated set of §4.3's table, so a future edit cannot introduce
  one quietly. The two values that must never appear — `text/html` and
  `image/svg+xml` — are asserted absent by name as well.
- **F3c** **The render-map test.** The client's `viewer → Blob MIME` map
  contains no entry whose value is `text/html`, `image/svg+xml`,
  `application/xhtml+xml`, or `text/xml`; every key is one of the five known
  kinds; and an unknown key resolves to the download-only branch. This is the
  test that stands where a second origin would (§0.4), so it is written as an
  assertion over the map object, not over rendered output.
- **F3d** A `text`-viewer document containing `<script>` renders those
  characters literally (`textContent`, not `innerHTML`).
- **F3e** The markdown viewer path calls the shipped `renderMarkdown`; its
  existing sanitizer tests are the coverage, and this module adds no second
  sanitizer to drift from it.
- **Not covered, and stated:** direct browser navigation to `/content` cannot be
  exercised in the in-process ASGI harness — the same structural gap that makes
  `sanitize_attachment_filename` a direct unit test rather than an API test.
  The two headers are pinned instead; they are what the browser acts on.

### F4 — The client never declares a type (§0.5)

*Broken if:* a stored `content_type` traces to anything the client sent.

- **F4a** A ZIP named `report.pdf` stores `application/zip`, `viewer="none"`.
- **F4b** A PDF named `notes.txt` stores `application/pdf`, `viewer="pdf"`.
- **F4c** `sniff()` unit tests over every row of the §4.3 table, including the
  2 MiB text boundary, the UTF-16 refusal, and a NUL-containing "text" file.

### F5 — Size and quota limits (§4.2)

*Broken if:* a version is stored above the cap, or an over-cap body is fully
buffered before rejection.

- **F5a** Declared `Content-Length` over cap → 413, and **no** document version
  row and **no** object write (assert both absences, not just the status).
- **F5b** A chunked request with no `Content-Length` and an over-cap body → 413
  from `read_bounded_body`. This is the case F5a cannot catch.
- **F5c** An org at quota → 507, no row, no object.
- **F5d** No stored `byte_size` exceeds `MAX_DOCUMENT_BYTES`.
- **Not measured, and stated:** peak API-process memory under N concurrent
  maximum-size uploads. `read_bounded_body` buffers whole bodies, so the bound
  is `N × 25 MiB` by construction, and nothing here measures it. Per CLAUDE.md
  that makes it a design target, not a verified number, and §17 D5 is where the
  cap gets argued.

### F6 — Audit completeness (invariant 4)

*Broken if:* bytes are served, or a mutation lands, with no event.

- **F6a** Every route in §10 writes its §12.3 event in the same transaction.
- **F6b** Two downloads write two `docs.document_downloaded` events — the count
  increments, so a view is never silently deduplicated into nothing.
- **F6c** A no-op `PUT` writes nothing and bumps nothing.

### F7 — Purge destroys, and integrity is checkable (§6.3)

- **F7a** After purge: `/content` → 410; `object_key == ""`; `purged_at` and
  `purged_by` set; the object is gone from the store; the tombstone still
  carries filename, size and sha256; the audit event exists.
- **F7b** A round trip (`put` then `get`) returns bytes whose sha256 equals the
  stored digest. A mismatch means storage corruption or the wrong object, and
  this is the only test that can tell you which.

### F8 — The object store is soft (§0.9)

*Broken if:* object storage being down makes the library look empty or returns
a 500.

- **F8a** With a store that raises, `GET /docs/documents` returns 200 with
  metadata.
- **F8b** With the same store, `/content` returns 503 and an error naming the
  dependency — not a 500, not an empty 200.

### Other coverage

RBAC: read-only can GET and download but not POST/PUT (403); technician can
upload and archive but **not** purge (403); org-admin can purge. Links: in-org
target resolves, foreign-org and nonexistent both 404, duplicate 409. Search:
a document is found by title, by filename, and by description; archived
documents are excluded. Frontend: vitest over the render map (F3c), the
grouping helper's new group, the upload-refusal messages, and the four distinct
states of §11.7; `tsc --strict` and a build.

---

## 14. Required changes outside this module

1. `app/core/rbac.py` — add `docs.document.purge` to `PERMISSIONS`.
2. `app/core/storage.py` — the four methods of §3.2, and **no** presign method.
3. Migration (next free revision) — three tables, RLS ×3, the FTS index, the
   permission seed and role grants.
4. `app/models/documentation.py` — three model classes.
5. `frontend/src/lib/api.ts` — typed client methods following the existing
   binary upload/download pattern (`uploadTicketAttachment`,
   `downloadTicketAttachment`), including the new list/detail/version types.
6. `frontend/src/modules/inventory/DeviceDetailPage.tsx` — the Documents list
   inside the existing Docs tab.
7. `docs/04-data-model.md` — add `/{org_id}/docs/documents/…` to the object
   storage layout block.
8. `deploy/README.md` — a documents subsection beside the Budget and Service
   Desk attachment ones, stating the key layout, the 25 MiB cap and the 5 GiB
   org quota.
9. `docs/modules/documentation/spec.md` — a one-line pointer at the top to this
   file. Its own §1–§8 are unchanged.

**No changes to:** the job engine, the gateway, the wire protocol, the agent,
the signed-job path, tenancy core, the audit core, or the credential vault.

---

## 15. Deliberately out of scope

Each of these is a thing ITGlue does that this spec does not, with the reason.

1. **Passwords as documents.** The vault already owns secrets, with envelope
   encryption, decrypt-at-use and audited checkout (invariant 3). A document is
   never the place for one. **The honest limitation:** nothing stops an operator
   uploading a PDF that contains a password, and no check could reliably detect
   it. The remedy is `docs.document.purge` plus the audit trail — response, not
   prevention. Say this in the operator-facing docs; do not imply a control that
   does not exist.
2. **Contacts and people.** There is no contact entity in this platform and
   inventing one here would be this module defining shared structure. A vendor
   contact is a field on the existing `vendor` `doc_record` today.
3. **Domains and SSL certificates.** Genuinely missing, genuinely useful, and
   genuinely a different module — expiry tracking needs a scheduled checker, a
   notification rule and an alert lifecycle, none of which is a document.
4. **The browser extension.** No.
5. **Documents on tickets.** Service Desk has attachments with their own freeze
   and portal-exposure rules. §0.7.
6. **Portal / end-user access.** The portal is requester-scoped
   (`portal_session` pins both org and requester). A document library there is a
   new authorization model, not a new route.
7. **Content extraction, full-text search of file contents, OCR.** §0.8, with
   the additive forward path stated.
8. **Folders and hierarchy.** `category` is flat, deliberately: a tree is a
   second navigation model to build, maintain, migrate and explain, on a page
   that already groups by category.
9. **Retention and pruning of old versions.** Nothing is pruned in v1; the org
   quota (§4.2) bounds growth and is operator-visible. A retention policy needs
   a scheduled job and a "which versions are safe to destroy" rule, and destroying
   things on a timer is not a v1 behaviour for a document library.
10. **Antivirus scanning of uploads.** Not done, and not pretended. There is no
    scanner in this stack; ClamAV would be a new container, a new dependency and
    a new failure mode on the upload path. What limits the exposure is
    structural rather than detective: no agent can resolve a docs key, no job
    type references one, and no presigned URL exists — so this platform never
    hands an uploaded file to an endpoint. The residual risk is a console user
    downloading a malicious file they themselves went and fetched. If the
    operator wants scanning, it is its own cycle. §17 D9.
11. **Templates, required-field policies, the relationship graph, and
    auto-population from inventory.** Already deferred by the module brief;
    unchanged by this spec.
12. **Editing Office documents in the console.** No.

### 15.13 The agent, and the idle budget

Continuing the numbering above, because it is the same list: this is another
thing this module does not do.

This module adds **no** agent capability, no job type, no capability handler,
and no wire-protocol field. The correct statement about the agent idle budget is
therefore not "unchanged, measured" but **"not applicable — there is no agent
code in this module to measure."** Per CLAUDE.md, a budget claim without a
benchmark is a design target and must be described as one; here there is no
claim to make at all.

---

## 16. Implementation shape

Two cycles, and the seam between them is the API surface of §10 — the frontend
cycle needs nothing from the backend cycle except those routes and those
response shapes.

**Cycle A — backend.** Migration (three tables, RLS ×3, FTS index, permission
seed); the three model classes; the four `ObjectStore` methods; `filetypes.py`
and its direct unit tests; the service layer (create, upload version, update,
archive, purge, links); the eleven routes; F1, F2, F4, F5, F6, F7, F8 and the
RBAC/link/search coverage. Written test-first per CLAUDE.md; the security-
relevant paths — key derivation, the prefix assertions, the sniffer, the
bounded body, the audit events — get explicit tests, not incidental ones.

**Cycle B — console.** The typed client methods; the Documents section, the
document view and its five-kind viewer, the upload dialog, the version drawer,
the links editor, the purge dialog; the stat-tile swap; the device-detail
list; F3c and F3d plus the four distinct states of §11.7; `tsc --strict` and a
build.

Attempting both in one plan is the failure mode CLAUDE.md's build discipline
warns about: the viewer is the part most likely to need a second pass against a
real browser, and it should not be able to hold the schema hostage.

### 16.1 Cycle A as built (2026-08-26)

Landed: migration `0075_doc_documents` (three tables, RLS forced on each, the
FTS index, `docs.document.purge` seeded to super-admin and org-admin); the
three model classes; the four `ObjectStore` methods and no fifth;
`filetypes.py` with 41 direct unit tests; `documents.py` (the service) and
`documents_api.py` (the eleven routes); the `document` kind on `/docs/search`.
Falsifiers covered: F1a-F1e, F2a/F2b, F3a/F3b (F3c-F3e are the console),
F4a-F4c, F5a-F5d, F6a-F6c, F7a/F7b, F8a/F8b, plus the RBAC, link and search
coverage. The migration's `upgrade head -> downgrade base -> upgrade head`
round trip leaves nothing behind and reproduces the reference schema exactly.

**Four places the build is more specific than the text above, recorded so the
code and the spec do not quietly disagree:**

1. **§4.4's quota passes are two different PREDICATES, not one called twice.**
   Pass one is `usage >= cap` ("already full?"); pass two is
   `usage + incoming > cap` ("would this write exceed it?"). Neither implies
   the other, so they are `assert_not_already_full` and `assert_room_for`
   rather than one `assert_quota(incoming=...)`. A consequence worth stating:
   an upload landing EXACTLY on the cap is allowed, and the next one is
   refused. The test that separates the two passes sends an over-cap chunked
   body to a full org -- 507 with pass one, 413 without it.
2. **`GET /documents/{id}/versions` takes `limit` and `offset`** (defaults 100
   and 0), which §10 does not list. Versions are append-only and v1 prunes
   nothing (§15.9), so "a document's history is naturally short" is a claim
   about how people behave, not about the data. It also keeps the route from
   adding to this codebase's counted "unbounded list route" bill.
3. **A purge that cannot reach the object store answers 503 and changes
   nothing** -- no tombstone, no audit event. §6.3 does not say what happens
   when the store is down; recording a destruction that did not happen is the
   worse of the two failures.
4. **The three GET list routes join `PENDING_TOOL_REVIEW`**, not the AI tool
   registry. They pass all six of the AI surface's route properties, so
   something had to be decided; a library's titles, categories and filenames
   are tenant content, and whether a persona holds them is an autonomy
   question. The content route fails two properties structurally and can never
   be registered.

**Still a design target, not a measurement:** peak API-process memory under N
concurrent maximum-size uploads. `read_bounded_body` buffers whole bodies, so
the bound is `N x 25 MiB` by construction and nothing in this repo measures it.
Per CLAUDE.md that keeps it described as a target, which is what §4.2, §13 F5
and the `MAX_DOCUMENT_BYTES` docstring all say. It was not converted into a
claim by this cycle.

**Found, not fixed:** `ix_doc_articles_fts` is not in the schema. 0007 created
it in raw SQL without declaring it on the model, and 0008 -- autogenerated one
migration later -- emitted a bare `op.drop_index` for an index its metadata did
not recognise. Article FTS has been on a sequential scan since 2026-07 and
nothing went red. The documents index is declared on the model as well as in
0075 so autogenerate produces a remove/add pair rather than a silent delete;
repairing the articles one is a forward migration of its own.

### 16.2 Cycle B as built (2026-08-26)

Landed: the eleven typed client methods and their four shapes; `viewer.ts` (the
render map) and `DocumentViewer.tsx` (the five branches); the Documents section
on `/docs`; the document view with its header, upload/edit/archive/purge
controls, version drawer and links editor; the stat-tile swap (D8); the
device-detail Documents list (§11.5); and the Documents screens on
`/__preview` so the CI browser pass covers them. Falsifiers covered: F3c, F3d
and F3e, plus the four distinct states of §11.7 and the five upload refusals.
`tsc --noEmit` clean over every file this cycle touched, `vite build` clean,
and the browser pass exits 0 with 0 findings.

**Five places the build is more specific than the text above, recorded so the
code and the spec do not quietly disagree:**

1. **F3d IS ASSERTED IN A BROWSER AS WELL AS IN JSDOM, AND THE TWO ARE
   DIFFERENT CLAIMS.** jsdom answers "did these characters land in
   `textContent` or in `innerHTML`" exactly, and `DocumentViewer.test.tsx`
   asks it over `.html`, `.svg`, `.xml` and a `.txt` full of markup. What
   jsdom cannot answer is whether a REAL parser, handed these bytes through
   this component, runs anything — it has no script execution to observe.
   `/__preview`'s document fixture is therefore a live three-vector payload
   (a `<script>` element, an `<svg onload>`, an `<image onerror>`, all setting
   one global) and `scripts/a11y-browser.mjs` measures the answer:

   ```
   ok    xss  298 characters of live payload rendered as text;
               0 script / 0 svg / 0 image element(s) built; executed=false
   ```

   Per that script's own discipline it also carries a self-check that can
   falsify it — the viewer's own `<pre>` is set to `innerHTML = textContent`,
   which is exactly the defect (someone rewrites the text branch), and the
   probe is required to see `1 svg / 1 script` and `DID execute`. It does. A
   non-vacuity clause fails the check if the fixture ever stops carrying a
   payload, because "nothing executed" is also what a blank page reports.

2. **The render map is checked as a CLOSED SET, not only against four named
   types.** §13 F3c asks for four values to be absent by name. The test also
   enumerates every Blob MIME the module can construct — six — and asserts the
   whole set, so a future edit introducing a rendering type nobody thought to
   name still fails. `resolveViewer` matches on the kind LIST rather than on
   property presence, because a plain object literal answers property access
   for `__proto__`, `constructor` and `toString`; the test plants all three.

3. **The image branch refuses a type the SERVER can never emit.** An SVG
   cannot arrive with `viewer="image"` — `filetypes.py` stores it
   `text/plain`/`text`, and F3b pins that. The client does not lean on it:
   `resolveViewer("image", "image/svg+xml")` falls through to download-only
   and a test says so. Leaning on the server's guarantee is what would make
   two independent walls into one.

4. **The console's 25 MiB check is documented as a COURTESY, not a control.**
   §4.2's enforcement is `reject_oversized_content_length` then
   `read_bounded_body`, and neither can be routed around by a client that
   lies about `Content-Length`. What the client-side check buys is that an
   operator on a slow link learns the answer before spending four minutes on
   the upload. The constant is named in the console as a mirror of the
   module's, with that distinction written at the declaration.

5. **The upload dialog performs §4.1's two steps and SHOWS the seam when the
   second fails.** A create that lands followed by an upload that is refused
   leaves a document with no file — a legal state — and the dialog says so
   rather than reporting a plain failure. Swallowing it would leave a row the
   operator did not know they had created.

**What is browser-verified and what is not, stated rather than blurred.**
Browser-verified in Chromium at 1440/1024/768/390/320 across the default,
dialog and palette states: axe (0 violations), the composited-pixel contrast
sweep, the 320px reflow check, the keyboard contract, and the XSS result in
(1). Unit-tested only: that the `pdf` branch constructs a Blob of
`application/pdf` and the `image` branch one of the four raster types — the
assertion is on the constructed `Blob.type`, not on whether Chromium's PDF
plugin paints, which headless Chromium is not a fair witness for and which is
not a claim this module makes. The `<embed>`-over-`<iframe sandbox>` reasoning
in §5.2 is a statement about how blob origins work, not a measurement, and it
is not reported as one.

**Found and fixed, and it was not this module's bug.** Checking that the new
harness fixture did not reach production turned up that `/__preview`'s fixtures
*already* did: `KB5041585`, `openssl=3.0.13-0ubuntu3.4`, `srv-dc01` and
`Alex Morgan` were all in `dist/assets/*.js`, and `preview.test.ts` was green
throughout. Rollup drops the route and the component, but not a module-scope
CALL that might have a side effect — `const X = xPreviewClient()`,
`[...].join("\n")` and `"c".repeat(64)` are all calls, and each anchored every
fixture it touched. The guard greps two strings that live in the one fixture
array nothing calls, so it was sampling the family that was already fine. The
harness now builds its clients in a `useMemo` and writes its strings literally,
and the guard spans both families. This mattered here specifically because the
new fixture is a live XSS payload, which has no business in a production
artifact even inert.

**Found, not fixed.** `PUT /docs/documents/{id}` takes the same body model as
`POST`, so it is a full replace: a caller sending only `title` silently resets
`description` to `""` and `category` to `"General"`. That is ordinary PUT
semantics and the console sends all three fields, so nothing is wrong today —
it is recorded because the next client to touch this route is where it bites.
Also: the `/content` route writes `docs.document_downloaded` BEFORE fetching
the bytes, so a 503 from a down object store records a download that did not
deliver anything. That is the conservative direction (an attempted read of a
signed contract is worth logging) and it matches §12.3's "every `/content`
response", but the route's docstring does not say so and a reader of the audit
log cannot tell the two apart.

---

---

## 17. Open decisions

Every judgement call above, collected. The recommendation is what the spec
currently says; overturning any of them is an edit to the named section, not a
redesign.

**D1 — Scope.** *Recommend:* build documents, versions, links, a viewer and
metadata search, and nothing else from the ITGlue surface (§1, §15).
*Rejected:* also building contacts, domains, SSL-certificate expiry and a
password manager, on the grounds that the vault already covers passwords and
the other three are a different module with a scheduler in it.

**D2 — Where the bytes live.** *Recommend:* the object store, keyed
`{org_id}/docs/documents/{document_id}/{version_id}`, with the four isolation
mechanisms of §3.3 replacing the RLS an object store cannot have. *Rejected:*
`bytea` in PostgreSQL — it would put 25 MiB blobs in the WAL, in every base
backup and in every replication stream, and would be the only attachment path
in the codebase that works differently.

**D3 — XSS containment.** *Recommend:* download-shaped transport + server-side
type sniffing + a one-entry-per-kind client render map (§0.4), with SVG, HTML
and XML stored and shown as **source text**, never rendered.
*Rejected for now:* a separate file-serving origin — strictly stronger, but it costs a hostname, a certificate, a Traefik router and
a cross-origin auth story on a hand-operated single-node stack, where a silently
misconfigured second origin is worse than none. **This is the decision to
revisit first if the operator wants SVG diagrams or HTML exports previewed in
the console** — those need the second origin, not an exception.

**D4 — Extension/content mismatch.** *Recommend:* label it, don't refuse it —
the viewer keys off the sniff, so a mismatch has no security consequence and
refusing breaks the legitimate `.dat`-that-is-a-PDF case. *Alternative:* refuse
any upload whose extension contradicts its bytes.

**D5 — Per-file cap: 25 MiB.** *Recommend:* 25 MiB. It holds a scanned contract
or a diagram export, and because §0.3 forbids client-to-bucket uploads, peak API
memory is `concurrent_uploads × cap`. *Alternative:* raise it — but the thing to
argue about is that product, not the file size, and above roughly 100 MiB the
right answer stops being a bigger number and becomes a streaming upload path,
which is its own cycle.

**D6 — Per-org quota: 5 GiB, as a constant.** *Recommend:* a module constant, no
UI. The bucket is shared with **backup repositories**, so an unbounded
technician-tier upload path can stop backups from writing — that is why a quota
exists at all rather than being deferred. *Alternative:* a per-org override in
`organizations.settings` (the `console_email_domain` precedent), which needs a
settings-route change this spec does not otherwise touch.

**D7 — No rollback verb.** *Recommend:* "restore version 2" is
download-then-upload — one more append, and the history stays literally true.
*Alternative:* a `POST .../versions/{n}/restore` that copies an old object to a
new version. Cheap to add later; adds a second way to create a version.

**D8 — The stat row.** *Recommend:* replace the Categories tile with Documents,
keeping four tiles. Categories links to the same list Articles links to and
decomposes into headings rather than rows. *Alternative:* five tiles, which
breaks the `columns={4}` grid.

**D9 — No antivirus scanning.** *Recommend:* don't, and say so plainly (§15.10)
rather than implying a control that does not exist. The structural limit is that
no uploaded file can ever reach an endpoint through this platform.
*Alternative:* a ClamAV container and a scan step on upload — its own cycle, its
own failure mode on the upload path.

**D10 — Every view is an audited download.** *Recommend:* yes; the server cannot
tell viewing from saving, and an audit log should not claim a distinction it
cannot verify. *Consequence the operator should agree to knowingly:* audit
volume grows with reading, not just with writing.
