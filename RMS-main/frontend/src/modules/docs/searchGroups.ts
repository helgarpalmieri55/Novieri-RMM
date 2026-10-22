import type { DocSearchHit } from "../../lib/api";

export interface GroupedHits {
  articles: DocSearchHit[];
  records: DocSearchHit[];
  /**
   * `document` is the third kind on the SAME `/docs/search` route with the
   * SAME response shape (spec-attachments.md §8), so this helper gains a group
   * and the console gains no second query.
   *
   * WHAT A DOCUMENT HIT COVERS, and it is narrower than a reader will assume:
   * title, description and category through the FTS index, plus the filename
   * of the CURRENT version through an `ilike`. Not the file's CONTENTS, and
   * not older versions' filenames. §11.6 puts that sentence on the screen
   * beside the search box, because a search box that silently only searches
   * metadata teaches people the library is empty when it is not.
   */
  documents: DocSearchHit[];
}

export function groupHits(hits: DocSearchHit[]): GroupedHits {
  return {
    articles: hits.filter((hit) => hit.kind === "article"),
    records: hits.filter((hit) => hit.kind === "record"),
    documents: hits.filter((hit) => hit.kind === "document"),
  };
}
