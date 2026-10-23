/**
 * Mirrors `backend/app/api/v1/enrollment.py`'s Pydantic models exactly.
 *
 * The list row is SIX fields. The design's token table has seven columns —
 * Token, Organization, Site / group, Created by, Expires, Uses, State — and
 * four of them (the masked token value, organization, site/group, created by)
 * have nothing behind them: `TokenListItem` carries `id`, `name`, `expires_at`,
 * `max_uses`, `use_count`, `revoked`, and the plaintext exists exactly once, in
 * the creation response, and is never stored or re-served.
 */

export interface EnrollmentTokenListItem {
  id: string;
  name: string;
  expires_at: string;
  /**
   * `null` means UNLIMITED USES, and only a GRANDFATHERED row can say it.
   *
   * `POST /enrollment-tokens` used to default this to null, so the field an
   * operator left alone minted a credential that enrols machines into the
   * organisation without bound. It is now required and capped at
   * `MAX_TOKEN_USES`, and nothing can mint another one — but rows issued
   * before that are still redeemable and the server still honours them,
   * because re-reading a stored null as a number would fail at the endpoint
   * as an indistinguishable 403. `EnrollmentTokenPage.unlimited_count` is how
   * this screen finds them.
   */
  max_uses: number | null;
  use_count: number;
  revoked: boolean;
}

/**
 * `GET /enrollment-tokens`, which is an ENVELOPE rather than a bare list.
 *
 * Revoked tokens are excluded from `items` unless `include_revoked` was asked
 * for, and `revoked_count` says how many exist either way. The count is the
 * reason the envelope exists: hiding a revocation is only honest while the
 * screen can say that something was hidden. Nothing is ever deleted — the row
 * is the record of a bootstrap credential that once existed and of who
 * withdrew it, and `backend/app/services/enrollment.py::list_tokens` carries
 * the full reasoning.
 */
export interface EnrollmentTokenPage {
  items: EnrollmentTokenListItem[];
  /**
   * Tokens matching the request — the live ones, unless revoked were asked for
   * — BEFORE `limit`/`offset`. The route is paged, so this is the only thing
   * that tells a short page from the end of the list.
   */
  total: number;
  /** Revoked tokens in this organization, counted whether or not they are in `items`. */
  revoked_count: number;
  /**
   * Tokens that are STILL REDEEMABLE and carry NO USE LIMIT — not revoked, not
   * expired. Nothing can mint one any more, so this number only falls; until
   * it is zero the organisation holds credentials that enrol an unbounded
   * number of machines, and the screen that let them be created is the screen
   * that has to say so. `?unlimited_only=true` lists exactly these.
   */
  unlimited_count: number;
}

export interface EnrollmentTokenCreateRequest {
  name: string;
  /** Server bounds: 1 .. 24*90. */
  expires_in_hours: number;
  /**
   * REQUIRED, and server-bounded to `1 .. MAX_TOKEN_USES`. There is no
   * unlimited enrollment token: omitting this, or sending `null`, is a 422.
   */
  max_uses: number;
}

/**
 * `POST /enrollment-tokens`.
 *
 * `token` IS THE PLAINTEXT BOOTSTRAP CREDENTIAL AND IS SERVED ONCE. It is
 * never written to query cache in this module (see `EnrollmentPage.tsx`): a
 * cached mutation result is a credential sitting in memory for the rest of the
 * session, which is the shape of security invariant #3's concern even though
 * this value is not vault-stored.
 */
export interface EnrollmentTokenCreated {
  id: string;
  token: string;
  name: string;
  expires_at: string;
  /** Never null: this route cannot mint an unbounded credential. */
  max_uses: number;
}

/**
 * The lifecycle a token row is drawn in. Derived by the page from `revoked`,
 * `expires_at` and the use count; the union is what `enums.EnrollmentTokenState`
 * is held to cover in both languages.
 */
export type EnrollmentTokenState = "active" | "revoked" | "expired" | "spent";

/** The agent certificate states `GET /devices` serves in `cert_state`. */
export type CertificateState = "ok" | "renewing" | "expiring" | "expired";
