/**
 * Handing the browser a file it can save, from a response only an
 * AUTHENTICATED request could have obtained.
 *
 * WHY THIS EXISTS, AND IT IS NOT A STYLE PREFERENCE. This console
 * authenticates with a bearer token that `ApiClient` puts on every request
 * (`lib/api.ts`). A `<a href="/api/v1/…" download>` is not one of those
 * requests: the browser navigates to the URL itself and sends no
 * `Authorization` header, so the route answers 401 and the browser
 * cheerfully saves the refusal — a JSON error body under whatever name the
 * URL ended in. That is exactly what the operator reported: "export
 * inventory exports a .json instead of an .xlsx". The export had therefore
 * never worked on a deployed console, and could not have; nothing about it
 * was intermittent.
 *
 * Two anchors of that shape shipped — Devices' "Export inventory" and
 * Software's export — and the reporting module had already learned this the
 * hard way and written the fetch-and-save dance privately inside
 * `ReportView.tsx`. A helper only one page can reach is a helper the next
 * page rebuilds or, as here, does without. So it lives in `lib/`.
 *
 * WHAT THE BROWSER ACTUALLY NEEDS. There is no API for "save these bytes",
 * only for "follow this link" — so the bytes become an object URL and a
 * detached anchor is clicked to follow it. The URL is revoked immediately
 * afterwards: it pins the blob in memory for the life of the document
 * otherwise, and an operator exporting an estate several times over a long
 * session would accumulate every copy.
 */
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  // APPENDED BEFORE CLICKING. A detached anchor's click is a no-op in
  // Firefox; it has to be in the document to be followed.
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}
