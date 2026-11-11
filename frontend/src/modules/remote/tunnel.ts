/**
 * Where a screen session's live stream is. Same origin as the console.
 *
 * The URL carries NO credential: access logs record URLs verbatim, so a token
 * there would be replayable by anyone who reads a proxy log. Nor does the
 * handshake carry one any more: the session cookie is the credential, and the
 * browser sends it with this same-origin upgrade itself (own-auth design
 * §6.5; the server also checks the handshake's `Origin`). The subprotocol
 * list that carried Keycloak's access token is gone with it.
 */
export function screenUrl(sessionId: string, location: { protocol: string; host: string }): string {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const id = encodeURIComponent(sessionId);
  return `${scheme}://${location.host}/api/v1/remote/sessions/${id}/screen`;
}
