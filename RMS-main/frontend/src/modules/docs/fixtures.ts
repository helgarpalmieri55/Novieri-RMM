/**
 * Fixtures for the documentation module: the article library, the structured
 * records, the stored documents and the search over all three.
 *
 * ONE LIBRARY, EVERY NUMBER DERIVED FROM IT. The four tiles on `/docs` read
 * four totals, and each is the length of the list the panel under it shows,
 * so the page cannot disagree with itself. The articles are the ones the v3
 * design shows (`Restoring sentinel-db-02 from backup`, `Rack B4 network
 * layout`) plus enough more to make the category grouping and the link
 * counts visible. The BCP module's plan editor reads `listArticles` too, so
 * the procedure a plan references (`a-1`) is a real row here.
 *
 * DEV-ONLY BY CONSTRUCTION: reached through `fixtures/client.ts` only, which
 * nothing imports outside `FIXTURE_MODE`.
 */

import type { FixtureMap } from "../../fixtures/client";
import type {
  DocArticle,
  DocArticleListItem,
  DocDocument,
  DocDocumentListItem,
  DocDocumentVersion,
  DocRecordOut,
  DocSearchHit,
  DocVersion,
} from "../../lib/api";
import { ApiError } from "../../lib/api";

const MINUTE = 60_000;
const DAY = 24 * 60;
/** An ISO timestamp `minutes` before the call. */
const ago = (minutes: number): string => new Date(Date.now() - minutes * MINUTE).toISOString();

const notFound = (what: string) => new ApiError(404, JSON.stringify({ detail: `${what} not found` }));

/* ---------------------------------------------------------------- articles */

interface ArticleSeed {
  id: string;
  title: string;
  category: string;
  updatedDays: number;
  links: { target_type: string; target_id: string }[];
  body: string;
}

const ARTICLES: ArticleSeed[] = [
  {
    id: "a-1",
    title: "Restoring sentinel-db-02 from backup",
    category: "Runbook",
    updatedDays: 4,
    links: [{ target_type: "device", target_id: "dev-6" }],
    body: [
      "# Restoring sentinel-db-02 from backup",
      "",
      "Use this runbook when the PostgreSQL primary is lost and the replica cannot be promoted.",
      "",
      "## Before you start",
      "",
      "- Confirm the last verified restore point in **Backup & Restore**.",
      "- Announce the maintenance window in the service desk.",
      "- Freeze application writes on `lis-app-02`.",
      "",
      "## Steps",
      "",
      "1. Provision the replacement host from the `pg-16` image.",
      "2. Restore the latest verified repository snapshot.",
      "3. Replay WAL to the chosen point in time.",
      "4. Run the smoke checks below and hand back to the application team.",
      "",
      "```",
      "psql -h lis-db-01 -c 'select now(), pg_is_in_recovery();'",
      "```",
    ].join("\n"),
  },
  {
    id: "a-2",
    title: "Rack B4 network layout",
    category: "Network",
    updatedDays: 26,
    links: [
      { target_type: "ipam_subnet", target_id: "subnet-1" },
      { target_type: "ipam_subnet", target_id: "subnet-2" },
    ],
    body: [
      "# Rack B4 network layout",
      "",
      "Top of rack: `lis-sw-core-1`. Two uplinks to the core, one per PSU feed.",
      "",
      "| Unit | Device | Subnet |",
      "| --- | --- | --- |",
      "| U40 | lis-sw-core-1 | 10.20.0.0/24 |",
      "| U32 | lis-db-01 | 10.20.10.0/24 |",
      "| U30 | lis-app-02 | 10.20.10.0/24 |",
    ].join("\n"),
  },
  {
    id: "a-3",
    title: "Onboarding a new workstation",
    category: "Runbook",
    updatedDays: 11,
    links: [{ target_type: "device_group", target_id: "grp-workstations" }],
    body: "# Onboarding a new workstation\n\nEnrol the device, join it to the workstation group and let the baseline policy apply.",
  },
  {
    id: "a-4",
    title: "VPN certificate rotation",
    category: "Runbook",
    updatedDays: 8,
    links: [
      { target_type: "device", target_id: "dev-7" },
      { target_type: "credential", target_id: "cred-3" },
      { target_type: "record", target_id: "rec-1" },
    ],
    body: "# VPN certificate rotation\n\nRotate the gateway certificate a week before expiry. Change CR-117 carried the last rotation.",
  },
  {
    id: "a-5",
    title: "Veeam support escalation",
    category: "Vendors",
    updatedDays: 61,
    links: [],
    body: "# Veeam support escalation\n\nOpen a case on the vendor portal with the contract number from the vendor record before calling the hotline.",
  },
  {
    id: "a-6",
    title: "Porto office WAN failover",
    category: "Network",
    updatedDays: 40,
    links: [{ target_type: "device", target_id: "dev-8" }],
    body: "# Porto office WAN failover\n\nThe LTE backup path activates automatically. Verify the file server is reachable over it before closing the incident.",
  },
];

// Articles published to the portal, by id.
const inPortal = new Set<string>();

const listItem = (seed: ArticleSeed): DocArticleListItem => ({
  id: seed.id,
  title: seed.title,
  category: seed.category,
  archived: false,
  portal_visible: inPortal.has(seed.id),
  updated_at: ago(seed.updatedDays * DAY),
  link_count: seed.links.length,
});

const listArticles: FixtureMap["listArticles"] = (params = {}) => {
  const items = ARTICLES.filter((seed) => !params.category || seed.category === params.category)
    .filter(
      (seed) =>
        !params.target_id ||
        seed.links.some(
          (link) =>
            link.target_id === params.target_id &&
            (!params.target_type || link.target_type === params.target_type),
        ),
    )
    .map(listItem);
  return { items, total: items.length };
};

function findArticle(id: string): ArticleSeed {
  const found = ARTICLES.find((seed) => seed.id === id);
  if (!found) throw notFound("article");
  return found;
}

const getArticle: FixtureMap["getArticle"] = (id) => {
  const seed = findArticle(id);
  const article: DocArticle = {
    id: seed.id,
    title: seed.title,
    body: seed.body,
    category: seed.category,
    archived: false,
    portal_visible: inPortal.has(seed.id),
    current_version: 3,
    updated_at: ago(seed.updatedDays * DAY),
    links: seed.links.map((link, at) => ({ id: `${seed.id}-link-${at + 1}`, ...link })),
  };
  return article;
};

const articleVersions: FixtureMap["articleVersions"] = (id) => {
  const seed = findArticle(id);
  const versions: DocVersion[] = [
    { version: 3, snapshot: { title: seed.title }, changed_by: "usr-ines", ts: ago(seed.updatedDays * DAY) },
    { version: 2, snapshot: { title: seed.title }, changed_by: "usr-mateus", ts: ago((seed.updatedDays + 30) * DAY) },
    { version: 1, snapshot: { title: seed.title }, changed_by: "usr-mateus", ts: ago((seed.updatedDays + 90) * DAY) },
  ];
  return versions;
};

const createArticle: FixtureMap["createArticle"] = (body) => ({
  id: `a-${Date.now()}`,
  title: body.title,
  body: body.body,
  category: body.category,
  archived: false,
  current_version: 1,
  updated_at: ago(0),
  links: [],
});

const updateArticle: FixtureMap["updateArticle"] = (id, body) => ({
  ...getArticle(id),
  title: body.title,
  body: body.body,
  category: body.category,
  current_version: 4,
  updated_at: ago(0),
});

const archiveArticle: FixtureMap["archiveArticle"] = () => ({ archived: true });

const setArticlePortalVisibility: FixtureMap["setArticlePortalVisibility"] = (id, visible) => {
  findArticle(id);
  if (visible) inPortal.add(id);
  else inPortal.delete(id);
  return { portal_visible: visible };
};

/* ----------------------------------------------------------------- records */

const RECORDS: DocRecordOut[] = [
  {
    id: "rec-1",
    record_type: "service",
    name: "PostgreSQL primary (lis-db-01)",
    fields: { port: 5432, owner: "Platform team", runbook: "a-1" },
    archived: false,
    current_version: 2,
  },
  {
    id: "rec-2",
    record_type: "vendor",
    name: "Veeam Software",
    fields: { contract: "VS-2026-0142", support: "+351 21 000 0100" },
    archived: false,
    current_version: 1,
  },
  {
    id: "rec-3",
    record_type: "location",
    name: "Lisbon, rack B4",
    fields: { floor: -1, feeds: 2 },
    archived: false,
    current_version: 1,
  },
  {
    id: "rec-4",
    record_type: "network",
    name: "VLAN 20 workstations",
    fields: { vlan: 20, cidr: "10.20.4.0/23" },
    archived: false,
    current_version: 3,
  },
];

const listDocRecords: FixtureMap["listDocRecords"] = (recordType) => {
  const items = RECORDS.filter((record) => !recordType || record.record_type === recordType);
  return { items, total: items.length };
};

const createDocRecord: FixtureMap["createDocRecord"] = (body) => ({
  id: `rec-${Date.now()}`,
  record_type: body.record_type,
  name: body.name,
  fields: body.fields,
  archived: false,
  current_version: 1,
});

/* --------------------------------------------------------------- documents */

interface DocumentSeed {
  id: string;
  title: string;
  description: string;
  category: string;
  updatedDays: number;
  links: { target_type: string; target_id: string }[];
  versions: DocDocumentVersion[];
}

const DOCUMENTS: DocumentSeed[] = [
  {
    id: "doc-1",
    title: "Veeam support contract 2026",
    description: "Signed support contract, renewed in January.",
    category: "Vendors",
    updatedDays: 15,
    links: [{ target_type: "record", target_id: "rec-2" }],
    versions: [
      {
        version: 2,
        filename: "veeam-support-2026-signed.pdf",
        byte_size: 412_338,
        content_type: "application/pdf",
        viewer: "pdf",
        sha256: "9c1f7e2a4b8d0e6f5a3c2b1d9e8f7a6b5c4d3e2f1a0b9c8d7e6f5a4b3c2d1e0f",
        created_at: ago(15 * DAY),
        uploaded_by: "usr-ines",
        purged_at: null,
      },
      {
        version: 1,
        filename: "veeam-support-2026-draft.pdf",
        byte_size: 398_120,
        content_type: "application/pdf",
        viewer: "pdf",
        sha256: "1a0b9c8d7e6f5a4b3c2d1e0f9c1f7e2a4b8d0e6f5a3c2b1d9e8f7a6b5c4d3e2f",
        created_at: ago(44 * DAY),
        uploaded_by: "usr-ines",
        purged_at: null,
      },
    ],
  },
  {
    id: "doc-2",
    title: "Rack B4 elevation diagram",
    description: "Front elevation with unit numbers, exported from the CAD file.",
    category: "Network",
    updatedDays: 26,
    links: [{ target_type: "record", target_id: "rec-3" }],
    versions: [
      {
        version: 1,
        filename: "rack-b4-elevation.png",
        byte_size: 188_402,
        content_type: "image/png",
        viewer: "image",
        sha256: "5c4d3e2f1a0b9c8d7e6f5a4b3c2d1e0f9c1f7e2a4b8d0e6f5a3c2b1d9e8f7a6b",
        created_at: ago(26 * DAY),
        uploaded_by: "usr-mateus",
        purged_at: null,
      },
    ],
  },
  {
    id: "doc-3",
    title: "DR test evidence pack, August",
    description: "Exported test report for the database failover drill.",
    category: "Runbook",
    updatedDays: 2,
    links: [],
    versions: [],
  },
];

const documentItem = (seed: DocumentSeed): DocDocumentListItem => ({
  id: seed.id,
  title: seed.title,
  description: seed.description,
  category: seed.category,
  archived: false,
  current_version: seed.versions[0]?.version ?? 0,
  updated_at: ago(seed.updatedDays * DAY),
  link_count: seed.links.length,
});

const listDocuments: FixtureMap["listDocuments"] = (params = {}) => {
  const all = DOCUMENTS.filter((seed) => !params.category || seed.category === params.category).map(
    documentItem,
  );
  const offset = params.offset ?? 0;
  const limit = params.limit ?? 50;
  return { items: all.slice(offset, offset + limit), total: all.length };
};

function findDocument(id: string): DocumentSeed {
  const found = DOCUMENTS.find((seed) => seed.id === id);
  if (!found) throw notFound("document");
  return found;
}

const getDocument: FixtureMap["getDocument"] = (id) => {
  const seed = findDocument(id);
  const doc: DocDocument = {
    id: seed.id,
    title: seed.title,
    description: seed.description,
    category: seed.category,
    archived: false,
    current_version: seed.versions[0]?.version ?? 0,
    updated_at: ago(seed.updatedDays * DAY),
    current: seed.versions[0] ?? null,
    links: seed.links.map((link, at) => ({ id: `${seed.id}-link-${at + 1}`, ...link })),
  };
  return doc;
};

const documentVersions: FixtureMap["documentVersions"] = (id) => findDocument(id).versions;

const createDocument: FixtureMap["createDocument"] = (body) => ({
  id: `doc-${Date.now()}`,
  title: body.title,
  description: body.description,
  category: body.category,
  archived: false,
  current_version: 0,
  updated_at: ago(0),
  current: null,
  links: [],
});

const updateDocument: FixtureMap["updateDocument"] = (id, body) => ({
  ...getDocument(id),
  title: body.title,
  description: body.description,
  category: body.category,
});

/* ------------------------------------------------------------------ search */

/** Title and category, case-insensitively, across the three kinds. */
const searchDocs: FixtureMap["searchDocs"] = (q) => {
  const needle = q.trim().toLowerCase();
  const matches = (text: string) => text.toLowerCase().includes(needle);
  const hits: DocSearchHit[] = [];
  for (const seed of ARTICLES) {
    if (matches(seed.title) || matches(seed.category)) {
      hits.push({ kind: "article", id: seed.id, title: seed.title, category: seed.category, rank: hits.length });
    }
  }
  for (const record of RECORDS) {
    if (matches(record.name) || matches(record.record_type)) {
      hits.push({ kind: "record", id: record.id, title: record.name, category: record.record_type, rank: hits.length });
    }
  }
  for (const seed of DOCUMENTS) {
    if (matches(seed.title) || matches(seed.category)) {
      hits.push({ kind: "document", id: seed.id, title: seed.title, category: seed.category, rank: hits.length });
    }
  }
  return hits;
};

/** `/docs/suggest`: articles sharing any word of three letters or more. */
const suggestDocArticles: FixtureMap["suggestDocArticles"] = (q, limit = 5) => {
  const words = q.toLowerCase().match(/[a-z0-9]{3,}/g) ?? [];
  return ARTICLES.map((seed) => {
    const text = `${seed.title} ${seed.body}`.toLowerCase();
    return { seed, score: words.filter((w) => text.includes(w)).length };
  })
    .filter((hit) => hit.score > 0)
    .sort((a, b) => b.score - a.score || a.seed.title.localeCompare(b.seed.title))
    .slice(0, limit)
    .map(({ seed }) => ({ id: seed.id, title: seed.title, category: seed.category }));
};

export const fixtures: FixtureMap = {
  suggestDocArticles,
  listArticles,
  getArticle,
  articleVersions,
  createArticle,
  updateArticle,
  archiveArticle,
  setArticlePortalVisibility,
  listDocRecords,
  createDocRecord,
  listDocuments,
  getDocument,
  documentVersions,
  createDocument,
  updateDocument,
  searchDocs,
};

export default fixtures;
