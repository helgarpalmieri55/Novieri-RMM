import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation, useSearchParams } from "react-router-dom";
import { Badge, Button, Dialog, Field, Table, Td, Th, inputClass } from "../../components/ui";
import { ConfirmDialog } from "../../components/confirm";
import { ForEveryClientToggle } from "../../components/forEveryClient";
import { attemptProblems, describeSubmitError, Form, FormActions } from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import type { MessageKey } from "../../i18n/core";
import { Trans } from "../../i18n/Trans";
import type { DocArticleInput, Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { formatDate, formatDateTime } from "../../lib/datetime";
import { newRowClient } from "../../lib/internalClient";
import { humaniseEnum, humaniseStatus } from "../../lib/labels";
import { DocumentsSection, DocumentView, tooLong, UploadDocumentDialog } from "./documents";
import { renderMarkdown } from "./markdown";
import { RowButton } from "./RowButton";
import { groupHits } from "./searchGroups";
import { PortalVisibility } from "./PortalVisibility";
import { FilterBar, PageHead, Panel, QueryState, SearchField, StatCard, StatsGrid } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { Chips } from "../../shell/viz";

/** The kinds of documentation record the server knows (`record_type`). */
export type DocRecordType = "network" | "service" | "vendor" | "location";

const RECORD_TYPES: readonly DocRecordType[] = ["network", "service", "vendor", "location"];

// AUDIT (BLOCKER): "The default value of the New record Type dropdown can
// never be saved, and the error tells the user to call the REST API by hand."
// `network` is in `_SUPERSEDED_RECORD_TYPES` (backend
// `modules/documentation/service.py`) and is refused unconditionally on
// create -- so the FIRST option, selected by default, was the one value the
// server always rejects, and the refusal it produced told a console user to
// `POST /api/v1/ipam/subnets`.
//
// It stays in the list rather than being deleted, and stays DISABLED: existing
// network records remain readable, editable and archivable, so the vocabulary
// is still real -- what is gone is the ability to create a new one here. The
// label says where it went; the note under the select links there.
const SUPERSEDED_RECORD_TYPES: ReadonlySet<string> = new Set(["network"]);

// The first type the server will actually accept. Not `RECORD_TYPES[0]`.
const DEFAULT_RECORD_TYPE: DocRecordType = "service";

// THE VOCABULARY IS SPELLED FOR A READER, NOT PRINTED AS A WIRE VALUE. The
// four record types reached the screen as the raw `record_type` -- "service",
// "vendor" -- in the picker and on every row of the Records panel, which is
// the defect `humaniseEnum` exists to close (lib/labels.ts). The parenthesis
// replaces an em dash, which new UI copy does not use.
function recordTypeLabel(type: string): string {
  return humaniseEnum(type, "DocRecordType");
}

function recordTypeOptionLabel(type: string): string {
  return SUPERSEDED_RECORD_TYPES.has(type)
    ? translate("docs.record.movedOption", { type: recordTypeLabel(type) })
    : recordTypeLabel(type);
}

/** "2 services": the count of one record type, agreeing with the number. */
const RECORD_COUNT_KEYS: Readonly<Record<DocRecordType, MessageKey>> = {
  network: "docs.page.foot.recordType.network",
  service: "docs.page.foot.recordType.service",
  vendor: "docs.page.foot.recordType.vendor",
  location: "docs.page.foot.recordType.location",
};

function recordTypeCount(type: string, count: number): string {
  return Object.hasOwn(RECORD_COUNT_KEYS, type)
    ? translate(RECORD_COUNT_KEYS[type as DocRecordType], { count })
    : // A type the console has not met reads through the shared status words, lower case mid-sentence.
      translate("docs.page.foot.recordType.other", { count, type: humaniseStatus(type).toLowerCase() });
}

// The lead glyph for a record, so a type reads as a kind of thing rather than
// as a severity. The Records panel and the search group draw the same one.
const RECORD_TYPE_ICONS: Record<string, string> = {
  network: "network",
  service: "server-2",
  vendor: "building",
  location: "map-pin",
};

function recordTypeIcon(type: string): string {
  return RECORD_TYPE_ICONS[type] ?? "database";
}

// The server's own bounds, from `ArticleCreate`/`RecordCreate` in
// `backend/app/modules/documentation/api.py`. Stated here so the limit is
// enforced by the input and named in a sentence, rather than discovered by
// submitting and reading a Pydantic 422 body.
const TITLE_MAX = 300;
const CATEGORY_MAX = 100;
const NAME_MAX = 300;

// The rules are built when they are read, so their words follow the language.
function articleRules(): readonly FieldRule[] {
  return [
    {
      name: "title",
      label: translate("docs.editor.title"),
      validate: tooLong(translate("docs.editor.title"), TITLE_MAX),
    },
    {
      name: "category",
      label: translate("docs.editor.category"),
      validate: tooLong(translate("docs.editor.category"), CATEGORY_MAX),
    },
  ];
}

function recordRules(): readonly FieldRule[] {
  return [
    {
      name: "name",
      label: translate("docs.record.name"),
      validate: tooLong(translate("docs.record.name"), NAME_MAX),
    },
    {
      name: "fields",
      label: translate("docs.record.fields"),
      // AUDIT: "Invalid JSON in the Fields (JSON) editor produces no error at
      // all." The parse used to live inside the mutation function, where a
      // `SyntaxError` is not the failure this form knows how to talk about. It
      // is a shape problem with the field the user typed into, so it travels the
      // same channel as a missing one.
      validate: (value) => {
        let parsed: unknown;
        try {
          parsed = JSON.parse(value);
        } catch {
          return translate("docs.validation.notJson");
        }
        if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
          return translate("docs.validation.notObject");
        }
        return null;
      },
    },
  ];
}

/**
 * The stat row's two halves: where a tile points, and making it land.
 *
 * WHERE IT POINTS. `/docs` is this module's only route in App.tsx, so every
 * tile above links to a view OF this page — which is honest, because the rows
 * behind each number are on this screen. `link()` PRESERVES whatever is
 * already in the query string and only overwrites the keys it is given: a tile
 * is a place to look, not a reset button, and a tile that rebuilt the URL from
 * scratch would silently discard the reader's current filter or selection on
 * the way past.
 *
 * MAKING IT LAND. React Router pushes a fragment and stops there — it does not
 * scroll, and `App.tsx` wires no scroll restoration. Without the effect below,
 * a fragment tile navigates and moves nothing on screen, which is the dead
 * tile the operator asked us to remove, wearing an href.
 */
function useStatLinks(route: string) {
  const { hash } = useLocation();
  const [searchParams] = useSearchParams();

  useEffect(() => {
    if (!hash) return;
    const target = document.getElementById(decodeURIComponent(hash.slice(1)));
    // Guarded: jsdom does no layout and defines no `scrollIntoView`, and a
    // TypeError here would take the whole page down.
    if (target && typeof target.scrollIntoView === "function") {
      target.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }, [hash]);

  return useCallback(
    (fragment: string, params: Readonly<Record<string, string>> = {}) => {
      const next = new URLSearchParams(searchParams);
      for (const [key, value] of Object.entries(params)) next.set(key, value);
      const query = next.toString();
      return `${route}${query ? `?${query}` : ""}#${fragment}`;
    },
    [route, searchParams],
  );
}

export function DocsPage() {
  const t = useT();
  const api = useApi();
  const queryClient = useQueryClient();
  const [search, setSearch] = useState("");
  // v3's category chips over the list. Local, not in the URL: it narrows what
  // is on screen and nothing links to a category. `null` is the "All" chip.
  const [category, setCategory] = useState<string | null>(null);
  // AUDIT (MEDIUM): "Selecting an article does not change the URL -- articles
  // are not linkable ... A documentation system whose articles cannot be
  // linked to is severely reduced in value -- you cannot paste a runbook link
  // into a ticket, an incident channel, or another article."
  //
  // The selection LIVES IN THE QUERY STRING rather than in component state, so
  // there is one source of truth: a pasted `?article=<id>` opens that article
  // on load for the same reason clicking one puts it in the address bar. Push,
  // not replace, so Back returns to the previously-viewed article -- the other
  // half of the finding.
  const [searchParams, setSearchParams] = useSearchParams();
  const selectedId = searchParams.get("article");
  // THE SAME LINKABILITY, FOR THE NEW OBJECT (§11.4). A runbook PDF that
  // cannot be pasted into a ticket is half a runbook, and this page has
  // already had that audit finding fixed once for articles; the new selection
  // must not re-introduce it. Pushed, not replaced, so Back returns to what
  // was open before.
  //
  // THE TWO KEYS ARE MUTUALLY EXCLUSIVE and each setter clears the other,
  // because the main pane shows one thing. A URL carrying both would render
  // whichever branch happened to be checked first, and the address bar would
  // disagree with the screen.
  const selectedDocumentId = searchParams.get("document");
  const setSelectedId = useCallback(
    (id: string | null) => {
      setSearchParams((current) => {
        const next = new URLSearchParams(current);
        if (id) next.set("article", id);
        else next.delete("article");
        next.delete("document");
        return next;
      });
    },
    [setSearchParams],
  );
  const setSelectedDocumentId = useCallback(
    (id: string | null) => {
      setSearchParams((current) => {
        const next = new URLSearchParams(current);
        if (id) next.set("document", id);
        else next.delete("document");
        next.delete("article");
        return next;
      });
    },
    [setSearchParams],
  );
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<DocArticleInput>({ title: "", body: "", category: "General" });
  // A NEW article is the internal client's unless the person asks for it to
  // be every client's (MSP clients plan 2026-10-26 Task 14). Set at creation
  // only: an article's client never changes.
  const [forEveryClient, setForEveryClient] = useState(false);
  const [showVersions, setShowVersions] = useState(false);
  const [newRecordOpen, setNewRecordOpen] = useState(false);
  const [uploadOpen, setUploadOpen] = useState(false);
  // A refused article save, held as data (the rules and what they were checked
  // against) and worded at render, so a language switch re-words it.
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  const [confirmingArchive, setConfirmingArchive] = useState(false);
  // The editor is an inline panel, not a modal, so focus TRAPPING is not owed
  // -- but initial focus and Escape-to-cancel are (audit, MEDIUM).
  const titleInput = useRef<HTMLInputElement>(null);
  const searchInput = useRef<HTMLInputElement>(null);

  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const canManage = me.data?.permissions.includes("docs.manage") ?? false;
  // ITS OWN CODE, NOT `docs.manage` (§9). Editing the library and destroying
  // evidence are different acts with different blast radii, which is why the
  // server seeds `docs.document.purge` to super-admin and org-admin only. The
  // console hides what the caller cannot use; the server refuses regardless.
  const canPurge = me.data?.permissions.includes("docs.document.purge") ?? false;
  // The zone the operator set on their profile (cycle 5). A version history is
  // the place a wrong zone is least visible and most expensive: "who changed
  // this, and was it before or after the outage" is answered by these lines.
  const zone = me.data?.timezone ?? null;

  const articles = useQuery({
    queryKey: ["doc-articles"],
    queryFn: () => api.listArticles(),
  });
  const records = useQuery({ queryKey: ["doc-records"], queryFn: () => api.listDocRecords() });
  // §0.9/F8a: this query never touches the object store, so the library still
  // lists when MinIO is down. Only the byte paths fail, and they fail loudly.
  const documents = useQuery({
    queryKey: ["doc-documents"],
    queryFn: () => api.listDocuments(),
  });
  const hits = useQuery({
    queryKey: ["doc-search", search],
    queryFn: () => api.searchDocs(search),
    enabled: search.length > 0,
  });
  const article = useQuery({
    queryKey: ["doc-article", selectedId],
    queryFn: () => api.getArticle(selectedId as string),
    enabled: selectedId != null,
  });
  const versions = useQuery({
    queryKey: ["doc-versions", selectedId],
    queryFn: () => api.articleVersions(selectedId as string),
    enabled: selectedId != null && showVersions,
  });

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ["doc-articles"] });
    void queryClient.invalidateQueries({ queryKey: ["doc-article", selectedId] });
    void queryClient.invalidateQueries({ queryKey: ["doc-versions", selectedId] });
  };

  const save = useMutation({
    mutationFn: async () => {
      if (selectedId && editing && article.data) {
        return api.updateArticle(selectedId, draft);
      }
      // PHASE 3: the client/site selector. Until it exists a new article is
      // the organisation's internal client's -- `/me`'s `org_id`
      // (`lib/internalClient.ts`) -- or, when asked, every client's.
      return api.createArticle({ ...draft, ...newRowClient(me.data, forEveryClient) });
    },
    onSuccess: (saved) => {
      invalidate();
      setSelectedId(saved.id);
      setEditing(false);
    },
  });
  // AUDIT CLASS (cycle 4/5): this mutation had NO error channel at all. A
  // refused archive — a 403 for an operator whose `docs.manage` was revoked
  // between page load and click, a 503 from a proxy — left the article sitting
  // on screen with nothing said, which reads as "the button does not work"
  // rather than "the server refused". `archive.error` is rendered beside the
  // control below; there is no `onError` swallowing it and no optimistic
  // removal, so the article disappears only when the server said it did.
  const archive = useMutation({
    mutationFn: () => api.archiveArticle(selectedId as string),
    onSuccess: () => {
      invalidate();
      setSelectedId(null);
    },
  });

  // A refusal describes ONE article. Moving to another must not carry it over,
  // for the same reason the editor clears the server's complaint on edit.
  const selectArticle = (id: string) => {
    setSelectedId(id);
    setEditing(false);
    archive.reset();
  };

  // Same rule for the new object: a refusal describes ONE thing, and the
  // editor must not be left open over a document it cannot save.
  const selectDocument = (id: string) => {
    setSelectedDocumentId(id);
    setEditing(false);
    archive.reset();
  };

  const grouped = useMemo(() => groupHits(hits.data ?? []), [hits.data]);
  const items = useMemo(() => articles.data?.items ?? [], [articles.data]);
  // The chips: "All", then every category the library has, in first-seen
  // order. A category with no article has no chip, so a chip never opens an
  // empty list. The categories are data; the "All" chip is worded at render.
  const categories = useMemo(() => [...new Set(items.map((item) => item.category))], [items]);
  const chipItems = [t("docs.page.list.all"), ...categories];
  const activeChip = category === null ? 0 : Math.max(0, categories.indexOf(category) + 1);
  const listed = useMemo(
    () => (category === null ? items : items.filter((item) => item.category === category)),
    [items, category],
  );

  const startNew = () => {
    setSelectedId(null);
    setDraft({ title: "", body: "", category: "General" });
    setForEveryClient(false);
    setAttempt(null);
    save.reset();
    setEditing(true);
  };
  const startEdit = () => {
    if (!article.data) return;
    setDraft({
      title: article.data.title,
      body: article.data.body,
      category: article.data.category,
    });
    setAttempt(null);
    save.reset();
    setEditing(true);
  };

  // AUDIT (MEDIUM): "Article editor is not a dialog: Escape does not close it
  // and focus is never moved into it ... Keyboard users must Tab forward
  // blindly to reach the first field, and the universally-expected
  // Escape-to-dismiss does nothing."
  useEffect(() => {
    if (!editing) return;
    titleInput.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setEditing(false);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [editing]);

  // AUDIT: "Server error text persists after the user corrects the input and
  // is never cleared." A refusal describes the request that was sent; the
  // moment the draft differs from it, the sentence on screen is about a
  // submission that no longer exists. Every edit goes through here so no field
  // can be added later that forgets to clear it.
  const editDraft = (patch: Partial<DocArticleInput>) => {
    setDraft((current) => ({ ...current, ...patch }));
    if (save.isError) save.reset();
    if (attempt) setAttempt(null);
  };

  const submitArticle = () => {
    const next: Attempt = {
      rules: articleRules,
      values: { title: draft.title, category: draft.category },
    };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    save.mutate();
  };

  // `undefined` until the articles query has answered — see `count` below.
  const linkedCount = articles.data?.items.reduce((total, item) => total + item.link_count, 0);

  const link = useStatLinks("/docs");

  /** Whether the detail column has anything to show. */
  const paneOpen = editing || selectedDocumentId != null || selectedId != null;

  // A PENDING OR FAILED QUERY IS NOT A ZERO. `?? 0` printed "0 articles"
  // while the library was still loading and again when the request failed,
  // which is the very confusion `zeroLabel` exists to end one level down: an
  // empty knowledge base and a dead backend read identically. An em dash for
  // absent data means every zero on this row is a measured zero.
  const count = (n: number | undefined): string => (n === undefined ? "—" : String(n));

  // EACH TILE SAYS ONE THING ITS FIGURE DOES NOT. A caption over a bare number
  // leaves the sub-line band empty on all four tiles, which is a row of
  // vertical space spent on four counts a reader cannot act on. The sub-line
  // decomposes the figure instead: what the articles are filed under, which
  // record types are actually in use, how much of the library is reachable
  // from an object, and whether every document has its bytes.
  //
  // DERIVED ONLY FROM ROWS IN HAND, AND ONLY WHEN THAT IS THE WHOLE SET. Each
  // list route answers with `{ items, total }`; if the server ever caps a page
  // the breakdown would describe that page while the figure beside it counts
  // the library, so `whole` refuses the sub-line rather than printing a claim
  // one query short of true. A pending or failed query has no items and so no
  // sub-line either -- the same rule `count` above applies to the figure.
  const whole = <T,>(page: { items: T[]; total: number } | undefined): T[] | null =>
    page != null && page.total > 0 && page.items.length === page.total ? page.items : null;

  const articleItems = whole(articles.data);
  const articlesFoot =
    articleItems == null
      ? undefined
      : t("docs.page.foot.categories", { count: new Set(articleItems.map((item) => item.category)).size });

  const recordItems = whole(records.data);
  const recordsFoot = (() => {
    if (recordItems == null) return undefined;
    const byType = new Map<string, number>();
    for (const item of recordItems) {
      byType.set(item.record_type, (byType.get(item.record_type) ?? 0) + 1);
    }
    // "2 services, 1 vendor" -- the type spelled for a reader, each count
    // agreeing with its own number (`recordTypeCount`).
    return [...byType].map(([type, n]) => recordTypeCount(type, n)).join(", ");
  })();

  const linkedFoot =
    articleItems == null
      ? undefined
      : t("docs.page.foot.linkedFrom", {
          linked: articleItems.filter((item) => item.link_count > 0).length,
          count: articleItems.length,
        });

  const documentItems = whole(documents.data);
  const documentsFoot = (() => {
    if (documentItems == null) return undefined;
    // `current_version === 0` is a document row with no bytes behind it yet --
    // a legal state (§4.1), and the one fact about this count worth acting on.
    const awaiting = documentItems.filter((item) => item.current_version === 0).length;
    return awaiting === 0
      ? t("docs.page.foot.everyDocumentHasFile")
      : t("docs.page.foot.documentsAwaiting", { count: awaiting });
  })();

  /** "1 link" / "3 links", or nothing for an unlinked article. */
  const linksLine = (n: number): string | null => (n === 0 ? null : t("docs.page.linksLine", { count: n }));

  return (
    <div>
      <PageHead
        title={t("docs.page.title")}
        description={t("docs.page.description")}
        actions={
          canManage ? (
            <Button onClick={startNew}>
              <Icon name="plus" size={14} />
              {t("docs.page.newArticle")}
            </Button>
          ) : undefined
        }
      />

      {/* NOTHING ON THIS PAGE IS URGENT. A documentation library has no
          failing state: an article count is not an alarm however low it goes,
          and giving one a wash would spend the console's only "look here"
          signal on a number nobody has to act on today. Four plain tiles,
          each a link to the list its number decomposes into. */}
      <StatsGrid columns={4}>
        <StatCard
          label={t("docs.page.tiles.articles")}
          value={count(articles.data?.total)}
          icon="book"
          to={link("articles")}
          tone="blue"
          zeroLabel={t("docs.page.tiles.articlesZero")}
          foot={articlesFoot}
        />
        <StatCard
          label={t("docs.page.tiles.records")}
          value={count(records.data?.total)}
          icon="database"
          to={link("records")}
          tone="blue"
          zeroLabel={t("docs.page.tiles.recordsZero")}
          foot={recordsFoot}
        />
        <StatCard
          label={t("docs.page.tiles.linked")}
          value={count(linkedCount)}
          icon="link"
          /* The link counts are rendered per article in the library list, so
             that list is where this total decomposes. */
          to={link("articles")}
          tone="neutral"
          /* Worth naming: an unlinked library is documentation nobody reaches
             from the object it describes, which is a different problem from
             an empty one. */
          zeroLabel={t("docs.page.tiles.linkedZero")}
          foot={linkedFoot}
        />
        {/* D8 — CATEGORIES OUT, DOCUMENTS IN, and the row stays at four.
            Categories was the weakest of the four: it linked to the same list
            Articles links to and decomposed into headings rather than rows, so
            it was a number an operator could not act on. A fifth tile was the
            alternative and it breaks `columns={4}`. */}
        <StatCard
          label={t("docs.page.tiles.documents")}
          value={count(documents.data?.total)}
          icon="file-text"
          to={link("documents")}
          tone="blue"
          zeroLabel={t("docs.page.tiles.documentsZero")}
          foot={documentsFoot}
        />
      </StatsGrid>

      {/* `min-w-0` ON BOTH COLUMNS IS LOAD-BEARING: a grid item's default
          `min-width: auto` lets the 560px table floor inside the list panel
          widen the column past a 390px viewport instead of scrolling inside
          it, which the gate measured as a 576px document. */}
      {/* v3 draws ONE FULL-WIDTH LIST under the toolbar. The detail column
          opens only once something is selected or the editor is up; with
          nothing open the list spans the grid rather than sharing it with an
          empty pane, which had the toolbar, the chips and the count wrapping
          inside a third of the width. */}
      <div className="grid gap-4 lg:grid-cols-3">
        <div className={paneOpen ? "min-w-0" : "min-w-0 lg:col-span-3"}>
          {/* The Articles and Linked references tiles land here: this list is
              where each of those numbers decomposes. v3 draws it as the
              toolbar (search, chips, count) over one list of rows. */}
          <div id="articles">
            <Panel bare>
              <FilterBar>
                {/* AUDIT (LOW): "Once a query is typed the only way back to
                    the full list is to select the text and delete it
                    manually. There is no clear button and the input is not
                    type=search." Both: the native type, and an explicit
                    button for the browsers that render none. */}
                <SearchField
                  inputRef={searchInput}
                  type="search"
                  aria-label={t("docs.page.list.searchLabel")}
                  placeholder={t("docs.page.list.searchPlaceholder")}
                  maxLength={128}
                  value={search}
                  onChange={(event) => setSearch(event.target.value)}
                />
                {search && (
                  <Button
                    variant="ghost"
                    aria-label={t("docs.page.list.clearSearch")}
                    onClick={() => {
                      setSearch("");
                      searchInput.current?.focus();
                    }}
                  >
                    {t("docs.page.list.clear")}
                  </Button>
                )}
                {!search && chipItems.length > 1 && (
                  <Chips
                    items={chipItems}
                    active={activeChip}
                    onSelect={(at) => setCategory(at === 0 ? null : (categories[at - 1] ?? null))}
                    label={t("docs.page.list.filterByCategory")}
                  />
                )}
                <span className="filter-spacer" />
                {articles.data ? (
                  <span className="mono cell-sub">{t("docs.page.list.count", { count: listed.length })}</span>
                ) : null}
                {/* §11.6 — SAY WHAT SEARCH DOES, because it does less than a
                    reader assumes. A search box that silently only searches
                    metadata teaches people the library is empty when it is
                    not, and this one never opens a stored file. */}
                <p className="note" style={{ flexBasis: "100%" }}>
                  {t("docs.page.list.searchNote")}
                </p>
              </FilterBar>
              <QueryState
                loading={articles.isLoading}
                error={articles.error}
                empty={
                  search
                    ? grouped.articles.length === 0 &&
                      grouped.records.length === 0 &&
                      grouped.documents.length === 0
                    : listed.length === 0
                }
                emptyLabel={
                  search
                    ? t("docs.page.list.noMatch")
                    : items.length === 0
                      ? t("docs.page.list.noArticles")
                      : t("docs.page.list.noneInCategory", { category: category ?? "" })
                }
                emptyIcon="book"
              >
                {search ? (
                  <div
                    role="region"
                    aria-label={t("docs.page.list.searchResults")}
                    className="mini-list"
                    style={{ padding: "6px 18px" }}
                  >
                    {/* AUDIT (MEDIUM): "Search renders an empty Articles group
                        header when a query matches only records ... An empty
                        group heading reads as a loading state or a bug." A
                        header now appears only when it has something under it. */}
                    {grouped.articles.length > 0 && <p className="note mt-2">{t("docs.page.list.groupArticles")}</p>}
                    {grouped.articles.map((hit) => (
                      <div key={hit.id} className="mini-row">
                        <span className="mini-icon">
                          <Icon name="book" size={18} />
                        </span>
                        <RowButton onClick={() => selectArticle(hit.id)}>
                          {hit.title}
                          <span className="cell-sub"> {hit.category}</span>
                        </RowButton>
                      </div>
                    ))}
                    {grouped.records.length > 0 && <p className="note mt-2">{t("docs.page.list.groupRecords")}</p>}
                    {/* AUDIT (LOW): "Record type appears twice on every record
                        chip ... Same two pieces of information, opposite order
                        in two lists 100px apart." ONE ORDER, and it is now the
                        one the article rows above already use: the lead glyph,
                        the name, the type as plain sub-text. The muted pill it
                        replaces carried the `mut` minus glyph, so a taxonomy
                        label read as a severity. The Records panel below draws
                        the identical row. */}
                    {grouped.records.map((hit) => (
                      <div key={hit.id} className="mini-row">
                        <span className="mini-icon">
                          <Icon name={recordTypeIcon(hit.category)} size={18} />
                        </span>
                        <span className="mini-copy">
                          <div className="mini-title">{hit.title}</div>
                          <div className="mini-sub">{recordTypeLabel(hit.category)}</div>
                        </span>
                      </div>
                    ))}
                    {/* The new group, rendered only when non-empty — the
                        shipped rule (an empty group heading reads as a loading
                        state or a bug) applies to it too. */}
                    {grouped.documents.length > 0 && <p className="note mt-2">{t("docs.page.list.groupDocuments")}</p>}
                    {grouped.documents.map((hit) => (
                      <div key={hit.id} className="mini-row">
                        <span className="mini-icon">
                          <Icon name="file-text" size={18} />
                        </span>
                        <RowButton onClick={() => selectDocument(hit.id)}>
                          {hit.title}
                          <span className="cell-sub"> {hit.category}</span>
                        </RowButton>
                      </div>
                    ))}
                  </div>
                ) : (
                  <Table>
                    <thead>
                      <tr>
                        <Th>{t("docs.page.list.name")}</Th>
                        <Th>{t("docs.page.list.updated")}</Th>
                      </tr>
                    </thead>
                    <tbody>
                      {listed.map((item) => (
                        <tr
                          key={item.id}
                          style={item.id === selectedId ? { background: "var(--surface-2)" } : undefined}
                        >
                          {/* v3's row: the book as the lead, the title over
                              "Runbook · linked to 1 device · updated 3 Sep". */}
                          <Td lead={<Icon name="book" size={18} />}>
                            <RowButton
                              selected={item.id === selectedId}
                              onClick={() => selectArticle(item.id)}
                            >
                              {item.title}
                            </RowButton>
                            <div className="cell-sub">
                              {[item.category, linksLine(item.link_count)].filter(Boolean).join(" · ")}
                            </div>
                          </Td>
                          <Td>
                            <span className="mono" style={{ whiteSpace: "nowrap" }}>
                              {formatDate(item.updated_at, zone)}
                            </span>
                          </Td>
                        </tr>
                      ))}
                    </tbody>
                  </Table>
                )}
              </QueryState>
            </Panel>
          </div>

          <div id="records">
            <Panel
              title={t("docs.page.records.title")}
              actions={
                canManage && (
                  <Button variant="outline" onClick={() => setNewRecordOpen(true)}>
                    <Icon name="plus" size={14} />
                    {t("docs.page.records.newRecord")}
                  </Button>
                )
              }
            >
              <QueryState
                loading={records.isLoading}
                error={records.error}
                empty={(records.data?.items ?? []).length === 0}
                emptyLabel={t("docs.page.records.empty")}
                emptyIcon="database"
              >
                <div className="mini-list">
                  {(records.data?.items ?? []).map((record) => (
                    <div key={record.id} className="mini-row">
                      <span className="mini-icon">
                        <Icon name={recordTypeIcon(record.record_type)} size={18} />
                      </span>
                      <span className="mini-copy">
                        <div className="mini-title">{record.name}</div>
                        <div className="mini-sub">{recordTypeLabel(record.record_type)}</div>
                      </span>
                    </div>
                  ))}
                </div>
              </QueryState>
            </Panel>
          </div>

          <DocumentsSection
            documents={documents}
            selectedId={selectedDocumentId}
            onSelect={selectDocument}
            canManage={canManage}
            onUpload={() => setUploadOpen(true)}
          />
        </div>

        {paneOpen && (
        <div className="min-w-0 lg:col-span-2">
          {/* THE DOCUMENT BRANCH COMES FIRST, and the two selections are
              mutually exclusive by construction (each setter clears the other),
              so this is an ordering for readers rather than a tie-break. */}
          {selectedDocumentId && !editing ? (
            <DocumentView
              documentId={selectedDocumentId}
              canManage={canManage}
              canPurge={canPurge}
              zone={zone}
              onCleared={() => setSelectedDocumentId(null)}
            />
          ) : editing ? (
            <Panel title={selectedId && article.data ? t("docs.editor.editTitle") : t("docs.editor.newTitle")}>
              <Form onSubmit={submitArticle}>
                <div className="form-grid">
                  <Field label={t("docs.editor.title")}>
                    <input
                      ref={titleInput}
                      className={inputClass}
                      // AUDIT: "Title has no maxlength, so the 300-character
                      // limit is only discoverable by failing." The server's
                      // bound, made reachable before the round trip rather
                      // than after it.
                      maxLength={TITLE_MAX}
                      value={draft.title}
                      onChange={(event) => editDraft({ title: event.target.value })}
                    />
                  </Field>
                  <Field label={t("docs.editor.category")}>
                    <input
                      className={inputClass}
                      maxLength={CATEGORY_MAX}
                      value={draft.category}
                      onChange={(event) => editDraft({ category: event.target.value })}
                    />
                  </Field>
                </div>
                {/* AUDIT (MEDIUM) twice over, on one row of two panes.
                    (1) "Markdown editor textarea is 190px wide inside a ~650px
                        card ... a markdown table wraps after ~9 characters."
                        190px is a `<textarea>`'s intrinsic `cols="20"`: nothing
                        here ever gave it a width, so it never had one. `w-full`
                        is the audit's own fix ("make the textarea fill its
                        column"), and the 50/50 split is the `md:grid-cols-2`.
                    (2) "'Markdown' label sits below and to the left of its
                        textarea while 'Preview' sits above its pane ... two
                        labels for two side-by-side panes placed by two different
                        rules." A column here places both labels above their
                        panes by construction. `htmlFor` keeps the control
                        labelled. */}
                <div className="mt-3 grid gap-3 md:grid-cols-2">
                  <div className="flex flex-col gap-1">
                    <label className="note" htmlFor="doc-body-markdown" style={{ fontWeight: 500 }}>
                      {t("docs.editor.markdown")}
                    </label>
                    <textarea
                      id="doc-body-markdown"
                      className={`${inputClass} w-full min-h-64 mono`}
                      value={draft.body}
                      onChange={(event) => editDraft({ body: event.target.value })}
                    />
                  </div>
                  <div className="flex flex-col gap-1">
                    <span className="note" id="doc-body-preview-label" style={{ fontWeight: 500 }}>
                      {t("docs.editor.preview")}
                    </span>
                    {/* The type comes from `markdown.ts`, with the element.
                        The well is the kit's control edge and radius; there
                        is no kit piece for a rendered-content box yet. */}
                    <div
                      role="region"
                      aria-labelledby="doc-body-preview-label"
                      className="min-h-64 w-full overflow-x-auto"
                      style={{
                        border: "1px solid var(--line)",
                        borderRadius: "var(--r-ctl)",
                        padding: 12,
                      }}
                      dangerouslySetInnerHTML={{ __html: renderMarkdown(draft.body) }}
                    />
                  </div>
                </div>
                {selectedId == null ? (
                  <div className="mt-3">
                    <ForEveryClientToggle
                      code="docs.manage"
                      checked={forEveryClient}
                      onChange={(checked) => {
                        setForEveryClient(checked);
                        if (save.isError) save.reset();
                      }}
                    />
                  </div>
                ) : null}
                <div className="mt-3 flex justify-end gap-2">
                  {/* `type="button"` because `ui.tsx`'s Button sets no type, and
                   * inside a real <form> the default is "submit" — Cancel would
                   * save the article it is meant to abandon. */}
                  <Button type="button" variant="ghost" onClick={() => setEditing(false)}>
                    {t("docs.editor.cancel")}
                  </Button>
                  {/* Save STAYS ENABLED with an empty title: the old
                   * `disabled={save.isPending || !draft.title}` was the audit's
                   * silent no-op — a click that moved nothing and named no
                   * field. Submitting now says what is wrong, in one alert,
                   * whether the reason is a missing field or the server's. */}
                  <FormActions
                    label={t("docs.editor.save")}
                    pending={save.isPending}
                    error={save.isError ? save.error : undefined}
                    problems={attemptProblems(attempt)}
                  />
                </div>
              </Form>
            </Panel>
          ) : article.data ? (
            <section className="panel">
              <div className="panel-h">
                <div>
                  <h2 className="panel-title">{article.data.title}</h2>
                  <p>
                    <Badge variant="muted">{article.data.category}</Badge>{" "}
                    <span className="mono">v{article.data.current_version}</span>
                    {" · "}
                    {t("docs.article.updated", { date: formatDateTime(article.data.updated_at, zone) })}
                  </p>
                </div>
                <div className="panel-a">
                  <Button variant="ghost" onClick={() => setShowVersions(!showVersions)}>
                    {t("docs.article.history")}
                  </Button>
                  {canManage && (
                    <>
                      <Button variant="outline" onClick={startEdit}>
                        <Icon name="edit" size={14} />
                        {t("docs.article.edit")}
                      </Button>
                      {/* AUDIT (MEDIUM), on the same shape elsewhere: "Archive
                          sits beside Edit plan with no confirmation ... The three
                          header buttons are visually identical." Archiving takes
                          a runbook out of the library that an incident may be
                          about to need, and nothing in this page puts it back. */}
                      <Button
                        variant="outline"
                        className="danger"
                        onClick={() => setConfirmingArchive(true)}
                        disabled={archive.isPending}
                      >
                        {t("docs.article.archive")}
                      </Button>
                    </>
                  )}
                </div>
              </div>
              <div className="panel-b">
                <ConfirmDialog
                  open={confirmingArchive}
                  title={t("docs.article.archiveTitle")}
                  subject={article.data.title}
                  body={t("docs.article.archiveBody")}
                  confirmLabel={t("docs.article.archiveConfirm")}
                  destructive
                  onCancel={() => setConfirmingArchive(false)}
                  onConfirm={() => {
                    setConfirmingArchive(false);
                    archive.mutate();
                  }}
                />
                {archive.isError && (
                  <p role="alert" className="form-error mb-3">
                    {t("docs.article.archiveFailed", { reason: describeSubmitError(archive.error) })}
                  </p>
                )}
                <PortalVisibility article={article.data} canManage={canManage} />
                {article.data.links.length > 0 && (
                  <p className="note mb-3">
                    {t("docs.article.linkedTo", {
                      types: article.data.links
                        .map((l) => humaniseEnum(l.target_type, "DocLinkTarget"))
                        .join(", "),
                    })}
                  </p>
                )}
                {/* The type comes from `markdown.ts`, with the element.
                    `overflow-x` so a wide code block or table scrolls inside
                    the panel instead of pushing the layout. */}
                <div
                  className="overflow-x-auto"
                  dangerouslySetInnerHTML={{ __html: renderMarkdown(article.data.body) }}
                />
                {showVersions && (
                  <section className="mt-4">
                    <h3 className="panel-title mb-2">{t("docs.article.history")}</h3>
                    {/* A FAILED HISTORY FETCH USED TO RENDER AS NO HISTORY.
                        `(versions.data ?? []).map` over an errored query
                        produces an empty list, which on an audit-adjacent
                        surface says "this article has never been edited" — a
                        claim the page has no evidence for. The three outcomes
                        are now distinct. */}
                    {versions.isError ? (
                      <p role="alert" className="form-error">
                        {t("docs.article.historyFailed", { reason: describeSubmitError(versions.error) })}
                      </p>
                    ) : versions.isPending ? (
                      <p className="note">{t("docs.article.loading")}</p>
                    ) : versions.data.length === 0 ? (
                      <p className="note">{t("docs.article.noEarlier")}</p>
                    ) : (
                      <ul className="mini-list" style={{ listStyle: "none", margin: 0, padding: 0 }}>
                        {versions.data.map((version) => (
                          <li key={version.version} className="mini-row">
                            <span className="mini-icon">
                              <Icon name="history" size={16} />
                            </span>
                            <span className="cell-sub">
                              {t("docs.article.version", {
                                version: version.version,
                                date: formatDateTime(version.ts, zone),
                              })}
                            </span>
                          </li>
                        ))}
                      </ul>
                    )}
                  </section>
                )}
              </div>
            </section>
          ) : (
            /* Reached only with an article selected and not yet in hand: the
               pane does not open otherwise. A refused fetch is said, not shown
               as an empty pane. */
            <Panel>
              {article.isError ? (
                <p role="alert" className="form-error">
                  {t("docs.article.loadFailed", { reason: describeSubmitError(article.error) })}
                </p>
              ) : (
                <p className="note">{t("docs.article.loading")}</p>
              )}
            </Panel>
          )}
        </div>
        )}
      </div>
      <NewRecordDialog open={newRecordOpen} onClose={() => setNewRecordOpen(false)} />
      <UploadDocumentDialog
        open={uploadOpen}
        onClose={() => setUploadOpen(false)}
        onCreated={(id) => {
          void queryClient.invalidateQueries({ queryKey: ["doc-documents"] });
          selectDocument(id);
        }}
      />
    </div>
  );
}

function NewRecordDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const t = useT();
  const api = useApi();
  const queryClient = useQueryClient();
  const [recordType, setRecordType] =
    useState<DocRecordType>(DEFAULT_RECORD_TYPE);
  const [name, setName] = useState("");
  const [fieldsJson, setFieldsJson] = useState("{}");
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  const [forEveryClient, setForEveryClient] = useState(false);
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const create = useMutation({
    // The parsed object is the mutation's input, so no `JSON.parse` runs on
    // the dispatch path: a throw there surfaced as a raw `SyntaxError` (or as
    // nothing at all) instead of as a message about the field it came from.
    mutationFn: (fields: Record<string, unknown>) =>
      api.createDocRecord({
        record_type: recordType,
        name,
        fields,
        // PHASE 3: the client/site selector. Until it exists a new record is
        // the organisation's internal client's (`lib/internalClient.ts`), or,
        // when asked, every client's.
        ...newRowClient(me.data, forEveryClient),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["doc-records"] });
      onClose();
    },
  });

  // Same reason as the article editor: a server refusal is about the request
  // that was sent, so it must not outlive the input it described.
  const clearStaleFeedback = () => {
    if (create.isError) create.reset();
    if (attempt) setAttempt(null);
  };

  const submit = () => {
    const next: Attempt = { rules: recordRules, values: { name, fields: fieldsJson } };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    // Safe: the rule above already proved this parses to an object.
    create.mutate(JSON.parse(fieldsJson) as Record<string, unknown>);
  };

  return (
    <Dialog open={open} onClose={onClose} title={t("docs.record.title")}>
      <Form onSubmit={submit}>
        <div className="form-grid">
          <Field label={t("docs.record.type")}>
            <select
              className={inputClass}
              value={recordType}
              onChange={(event) => {
                setRecordType(event.target.value as DocRecordType);
                clearStaleFeedback();
              }}
            >
              {RECORD_TYPES.map((type) => (
                <option key={type} value={type} disabled={SUPERSEDED_RECORD_TYPES.has(type)}>
                  {recordTypeOptionLabel(type)}
                </option>
              ))}
            </select>
          </Field>
          {/* The remedy, as a place in this console rather than as an HTTP verb.
              The audit's words: "the remediation offered to a UI user is to issue
              raw HTTP calls -- a console user has no way to act on that." */}
          <p className="note">
            <Trans k="docs.record.networkNote" components={{ link: <Link className="underline" to="/ipam" /> }} />
          </p>
          <Field label={t("docs.record.name")} full>
            <input
              className={inputClass}
              maxLength={NAME_MAX}
              value={name}
              onChange={(event) => {
                setName(event.target.value);
                clearStaleFeedback();
              }}
            />
          </Field>
          <Field label={t("docs.record.fields")} full>
            <textarea
              className={`${inputClass} min-h-24 mono`}
              value={fieldsJson}
              onChange={(event) => {
                setFieldsJson(event.target.value);
                clearStaleFeedback();
              }}
            />
          </Field>
        </div>
        <ForEveryClientToggle
          code="docs.manage"
          checked={forEveryClient}
          onChange={(checked) => {
            setForEveryClient(checked);
            clearStaleFeedback();
          }}
        />
        <div className="flex justify-end gap-2">
          {/* See the article editor: an untyped Button inside a <form>
           * submits, and Cancel must not create the record. */}
          <Button type="button" variant="ghost" onClick={onClose}>
            {t("docs.record.cancel")}
          </Button>
          <FormActions
            label={t("docs.record.create")}
            pending={create.isPending}
            error={create.isError ? create.error : undefined}
            problems={attemptProblems(attempt)}
          />
        </div>
      </Form>
    </Dialog>
  );
}
