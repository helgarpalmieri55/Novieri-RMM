/**
 * Where a screen session's live stream is. Same origin as the console.
 *
 * The URL carries NO credential: access logs record URLs verbatim, so a token
 * there would be replayable by anyone who reads a proxy log. The token goes in
 * the subprotocol list instead (`screenProtocols`), which the browser sends as
 * the `Sec-WebSocket-Protocol` header -- the same scheme as the `/ws` channel.
 */
export function screenUrl(sessionId: string, location: { protocol: string; host: string }): string {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const id = encodeURIComponent(sessionId);
  return `${scheme}://${location.host}/api/v1/remote/sessions/${id}/screen`;
}

/** The subprotocols that carry the bearer token: `bearer` first, then the token. */
export function screenProtocols(token: string): string[] {
  return ["bearer", token];
}

/**
 * NO TOKEN, NO SOCKET. Without a credential the api closes the handshake
 * without a word and the page would blame the device for the browser's 1006.
 */
export function canOpenTunnel(token: string | null | undefined): token is string {
  return typeof token === "string" && token.trim() !== "";
}
