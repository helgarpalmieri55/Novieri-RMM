/**
 * Handing the browser a file it can save, from a response only an
 * AUTHENTICATED request could have obtained.
 *
 * WHY THIS EXISTS, AND IT IS NOT A STYLE PREFERENCE. When the console
 * authenticated with a bearer token, a `<a href="/api/v1/…" download>` sent
 * none: the browser navigates to the URL itself, the route answered 401, and
 * the browser cheerfully saved the refusal — a JSON error body under whatever
 * name the URL ended in. That is exactly what the operator reported: "export
 * inventory exports a .json instead of an .xlsx".
 *
 * THE SESSION IS A COOKIE NOW (own-auth phase 1), and a link would carry it.
 * It is still a fetch, because what a link cannot carry still matters: the
 * client chosen in the top bar (`X-Client-Id`, `lib/api.ts::requestHeaders`),
 * without which the export would be of every client the person can reach,
 * and the language (`Accept-Language`). And a 401 through a link would still
 * be saved as a file instead of ending the session (`ApiClient`'s
 * `onUnauthorized`).
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
