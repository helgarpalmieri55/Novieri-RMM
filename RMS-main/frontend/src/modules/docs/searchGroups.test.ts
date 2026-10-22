import { describe, expect, it } from "vitest";
import type { DocSearchHit } from "../../lib/api";
import { groupHits } from "./searchGroups";

const hit = (kind: DocSearchHit["kind"], title: string, rank: number): DocSearchHit => ({
  kind,
  id: title,
  title,
  category: "G",
  rank,
});

describe("groupHits", () => {
  it("splits by kind preserving order", () => {
    const groups = groupHits([
      hit("article", "a1", 0.9),
      hit("record", "r1", 0),
      hit("document", "d1", 0.7),
      hit("article", "a2", 0.5),
      hit("document", "d2", 0.1),
    ]);
    expect(groups.articles.map((h) => h.title)).toEqual(["a1", "a2"]);
    expect(groups.records.map((h) => h.title)).toEqual(["r1"]);
    expect(groups.documents.map((h) => h.title)).toEqual(["d1", "d2"]);
  });

  it("gives every group an empty array rather than undefined", () => {
    // The page renders a group heading only when its array is non-empty, so
    // `undefined` here would be a `.length` on nothing rather than a hidden
    // heading — and an empty heading reads as a loading state or a bug, which
    // is the finding the shipped page already fixed once for articles.
    const groups = groupHits([]);
    expect(groups).toEqual({ articles: [], records: [], documents: [] });
  });

  it("drops a kind it does not know rather than filing it under one it does", () => {
    // A kind this console has never heard of is a server ahead of it. Putting
    // it in the Articles group would render it as an article and select it as
    // one; leaving it out is visibly incomplete, which is the better failure.
    const groups = groupHits([{ ...hit("article", "x", 0), kind: "runbook" as never }]);
    expect(groups).toEqual({ articles: [], records: [], documents: [] });
  });
});
