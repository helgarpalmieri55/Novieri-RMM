/**
 * Enrollment & certificates — the design's "Kernel identity" screen
 * (`renderEnrollment` in `docs/design/itms-prototype.html`), built only out of
 * what the kernel actually serves.
 *
 * TWO ENDPOINTS BACK THIS PAGE. `GET /enrollment-tokens` (gated
 * `enrollment.manage`) and `GET /devices` (gated `devices.read`, and the only
 * place certificate state is served: `cert_state` and `cert_not_after` per
 * device). The two tabs are gated separately for that reason — a technician
 * with `devices.read` and no `enrollment.manage` sees certificates and is never
 * offered a token table that would 403.
 *
 * THE DESIGN'S "Agent packages" PANEL IS BUILT, ON ITS OWN SCREEN. Platform →
 * Agent Packages (`modules/agent-releases/AgentReleasesPage.tsx`), backed by
 * `GET /agent/releases`, lists the published artefacts and hands out a
 * one-machine install command. It is not folded into this page because that
 * command mints an enrollment token — the route is gated `enrollment.manage`,
 * while this page's certificate tab deliberately serves holders of only
 * `devices.read`.
 *
 * WHAT THE DESIGN ASKS FOR THAT NOTHING SERVES, omitted rather than faked:
 *
 * | Design element                          | Why it is not here |
 * |-----------------------------------------|--------------------|
 * | "Agent release rings" and adoption %    | No ring, no version, no adoption is served — `GET /devices` carries no agent version at all |
 * | "Current agent 94%" metric              | Same: there is no agent version to compare against a current one |
 * | Idle-budget panel ("Measured on Linux") | A claim about the Go agent's runtime. CLAUDE.md is explicit that this number is owed a benchmark that runs; restating it in the console would be asserting it a second time from a screen that measured nothing |
 * | Certificate Serial / Thumbprint / Issuer columns | `GET /devices` serves neither. Only `cert_state` and `cert_not_after` |
 * | Token Organization / Site / Created-by columns | `TokenListItem` has six fields and none of them is these |
 * | Masked token value ("ENR-••••-91KQ")    | The plaintext is served exactly ONCE, at creation, and is never stored or re-served. There is nothing to mask |
 *
 * WHAT CHANGED 2026-08-24, because one row of that table is now wrong.
 * "Revoked-devices count — revocation is a write; no endpoint lists revoked
 * agents" was true and was the reason this page could not say a certificate had
 * been revoked: `cert_state` is derived from `cert_not_after` alone, so a
 * revoked certificate read `ok` until its natural expiry and kept its Revoke
 * button. `GET /devices` now serves `cert_revoked`, and both tabs hide revoked
 * entries behind a counted toggle rather than listing them forever — an
 * operator reported that they pile up with nothing to clear them. NOTHING IS
 * DELETED, and the reasons are at the two sites that would have had to do it:
 * `backend/app/services/enrollment.py::list_tokens` for tokens, and the
 * comment on the certificate toggle below for the CRL.
 *
 * The enrollment-flow timeline IS kept: it describes this platform's invariants
 * (agent-side keypair, outbound-only mTLS, no endpoint listener), which are
 * properties of the design, not data about a tenant.
 *
 * ---------------------------------------------------------------------------
 * UX REMEDIATION CYCLES 7 AND 8 — WHAT THE TOKEN TAB GOT WRONG.
 *
 * Auditor B, [HIGH]: *"I clicked 'Create token' expecting it to open a form. It
 * immediately created an active enrollment token using default values, and only
 * then revealed the input fields beneath."* The panel rendered its fields
 * permanently and its submit button was labelled with the intent ("Create
 * token") rather than the effect, so the one control that looked like it opened
 * a form was the control that minted a live bootstrap credential. Nielsen #5.
 *
 * Auditor B, [HIGH]: *"Every token in the list displays as `(unnamed)` … and
 * every one shows uses as `0 / ∞`."* The name was optional and the use limit
 * defaulted to blank, which the API reads as unlimited. One leaked token then
 * enrolls an unbounded number of machines until it expires, and the table
 * cannot tell an operator which token that was.
 *
 * So the flow here is three deliberate steps and no fewer:
 *
 *   1. "Create token" REVEALS the form. It writes nothing.
 *   2. "Issue token" validates it client-side — a name is required, and a use
 *      limit between 1 and `MAX_TOKEN_USES` is required, always (see below:
 *      there is no longer an "unlimited" to opt into).
 *   3. A `ConfirmDialog` names the token and states its lifetime, its
 *      redemption limit and the fact that the plaintext is shown once. Only its
 *      confirm button reaches `POST /enrollment-tokens`.
 *
 * The default is ONE use, per the audit's fix, because the safe default for a
 * bootstrap credential is the smallest one that still works: enrolling a single
 * machine.
 *
 * ---------------------------------------------------------------------------
 * AND THE UNLIMITED OPTION IS GONE, BECAUSE THE SERVER NO LONGER HAS ONE.
 *
 * The audit fix above changed the DEFAULT and left the capability: an operator
 * could still tick "Allow unlimited uses" and receive a credential that enrols
 * an unbounded number of machines into this organisation until it expires —
 * each with a device certificate, each in the fleet list, each submitting
 * inventory this console believes, and each a legitimate target for a signed
 * job whose params may carry a secret.
 * `docs/modules/agent-distribution/spec-managed-distribution.md` §3.2 named
 * that "a long-lived shared secret wearing a shorter name".
 *
 * `POST /enrollment-tokens` now REQUIRES `max_uses` and caps it at
 * `MAX_TOKEN_USES`, so this form has a number field and no escape hatch. A
 * checkbox whose only outcome would be a 422 is worse than no checkbox.
 *
 * WHAT IS ALREADY IN THE FIELD IS NOT REWRITTEN, AND THIS SCREEN IS WHERE
 * THAT LANDS. Rows minted while the field defaulted to null are still
 * unlimited and the server still honours them — changing that meaning would
 * fail at the ENDPOINT, as a detail-free 403 an operator reads as a revocation
 * they did not perform. So the platform's answer is a human with a Revoke
 * button, and the page's job is to put them in front of one:
 * `unlimited_count` is a callout above the table and the filter beside it
 * lists exactly those rows.
 *
 * REVOCATION MOVED FROM THE INLINE TWO-STEP TO `ConfirmDialog`. Auditor B liked
 * the inline "Confirm revoke | Cancel" pair and proposed standardising on it.
 * The product standardised on `components/confirm.tsx` instead, and the reason
 * is the one thing the inline pair could not do: NAME THE SUBJECT. The mistake
 * a confirmation exists to catch is having the wrong row — and "Confirm revoke"
 * sitting in a cell says nothing about which device's certificate is about to
 * be destroyed. The two-click property is preserved; the dialog adds the name
 * and the consequence.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { ConfirmDialog } from "../../components/confirm";
import { Form, FormActions, attemptProblems, describeSubmitError } from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import { EmptyState, NoValue } from "../../components/emptystate";
import { Field, Td, inputClass } from "../../components/ui";
import type { Device, Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { internalSiteId, requireKnown } from "../../lib/internalClient";
import { formatDate, formatDateTime } from "../../lib/datetime";
import { humaniseEnum } from "../../lib/labels";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import { Btn, FilterBar, PageHead, Pager, Panel, Progress, QueryState, SearchField, StatCard, StatsGrid, StatusPill, TableWrap, Tabs } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { OsIcon } from "../../shell/viz";
import {
  ENROLLMENT_TOKENS_KEY,
  ENROLLMENT_TOKENS_PAGE_SIZE,
  MAX_TOKEN_USES,
  createEnrollmentToken,
  listEnrollmentTokens,
  revokeAgent,
  revokeEnrollmentToken,
} from "./api";
import type {
  EnrollmentTokenCreateRequest,
  EnrollmentTokenCreated,
  EnrollmentTokenListItem,
  EnrollmentTokenPage,
  EnrollmentTokenState,
} from "./types";
import { EnrollmentPrerequisites } from "./EnrollmentPrerequisites";

/** The codes `shell/nav.ts` uses, and the codes the routes enforce server-side. */
const DEVICES_READ = "devices.read";
const ENROLLMENT_MANAGE = "enrollment.manage";

/** Certificates a human should act on. `renewing` is the healthy automatic
 * path and is excluded, matching `modules/devices/DevicesPage.tsx`. */
const CERT_ATTENTION = new Set(["expiring", "expired"]);

function tokenState(token: EnrollmentTokenListItem, now: number): EnrollmentTokenState {
  if (token.revoked) return "revoked";
  if (new Date(token.expires_at).getTime() <= now) return "expired";
  if (token.max_uses != null && token.use_count >= token.max_uses) return "spent";
  return "active";
}

/** Maps a state onto one of the design's defined `.status` slugs. The slug
 * picks the colour; the word an operator reads is `enums.EnrollmentTokenState`,
 * looked up when the pill is drawn. */
const TOKEN_PILL_SLUG: Record<EnrollmentTokenState, string> = {
  active: "success",
  revoked: "critical",
  expired: "warning",
  spent: "neutral",
};

/** The certificate states `GET /devices` serves, each on a slug; the word is
 * `enums.CertificateState` (sentence case, never the raw slug). */
const CERT_PILL_SLUG: Record<string, string> = {
  ok: "success",
  renewing: "info",
  expiring: "warning",
  expired: "critical",
};

/**
 * The form's starting values.
 *
 * `maxUses: "1"` IS THE AUDIT FIX, not a placeholder. The previous default was
 * an empty field, which `createEnrollmentToken` sent as `max_uses: null`, and
 * `null` meant unlimited. Every token in the audited deployment was therefore
 * unlimited without anybody having chosen that. The route now refuses that
 * body outright, so this value is the smallest working credential rather than
 * the safest of several.
 */
const BLANK_FORM = { name: "", hours: "72", maxUses: "1" };

/** The server bounds `expires_in_hours` at 1..24*90; saying so here means the
 *  operator learns it before the round trip rather than from a 422. */
const MAX_LIFETIME_HOURS = 24 * 90;

// Built when the form is submitted, not held as a constant: the labels and
// messages are translated text.
const nameAndLifetime = (): FieldRule[] => [
  // "Token name", not "name": this is what the table will show instead of
  // "(unnamed)", and it is the only thing that will ever say what the
  // credential was for.
  { name: "name", label: translate("enrollment.form.name") },
  {
    name: "hours",
    label: translate("enrollment.form.hours"),
    validate: (value) => {
      const hours = Number(value);
      if (!Number.isInteger(hours) || hours < 1 || hours > MAX_LIFETIME_HOURS) {
        return translate("enrollment.form.hoursInvalid", { max: MAX_LIFETIME_HOURS });
      }
      return null;
    },
  },
];

/**
 * REQUIRED AND CAPPED, in that order, and the ceiling is the server's.
 *
 * `>= 1` alone was never a bound — the escape hatch beside this field was the
 * unbounded case, and without a ceiling `2147483647` is the same token spelled
 * differently. `MAX_TOKEN_USES` mirrors `services/enrollment.py`, which is
 * where the 422 comes from; saying it here means the operator learns it before
 * the round trip.
 */
const maxUsesRule = (): FieldRule => ({
  name: "maxUses",
  label: translate("enrollment.form.maxUses"),
  validate: (value) => {
    const uses = Number(value);
    if (!Number.isInteger(uses) || uses < 1 || uses > MAX_TOKEN_USES) {
      return translate("enrollment.form.maxUsesInvalid", { max: MAX_TOKEN_USES });
    }
    return null;
  },
});

/** What the confirmation says about the redemption limit, in words. */
function describeUses(maxUses: number): string {
  if (maxUses === 1) return translate("enrollment.confirm.usesOnce");
  return translate("enrollment.confirm.usesMany", { count: maxUses });
}

/**
 * Copying without swallowing the failure.
 *
 * `navigator.clipboard` is undefined outside a secure context and rejects when
 * permission is denied, and the previous reveal panel had no copy control at
 * all — Auditor B, [MEDIUM]: *"There is no copy-to-clipboard button — the user
 * must manually select a long high-entropy string."* A copy button that fails
 * silently would be worse than none, because the operator would then dismiss
 * the only copy of the token believing it was on the clipboard.
 */
async function copyToClipboard(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

type PendingRevoke =
  | { kind: "token"; id: string; subject: string }
  | { kind: "agent"; id: string; subject: string };

/** What the form asks for: the request, less the site the mutation adds. */
type TokenDraft = Omit<EnrollmentTokenCreateRequest, "site_id">;

export function EnrollmentPage() {
  const t = useT();
  const api = useApi();
  const queryClient = useQueryClient();

  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  // The signed-in user's zone (cycle 5). A token expiry read five hours early
  // is a credential a technician believes is dead while it is still live, and
  // a certificate expiry off by a whole day is the same mistake with a longer
  // fuse -- which is why the expiry column below goes through `formatDate`.
  const zone = me.data?.timezone;
  const held = me.data?.permissions ?? [];
  const canReadDevices = held.includes(DEVICES_READ);
  const canManage = held.includes(ENROLLMENT_MANAGE);

  // The keys are memoised; the labels are translated at render, so a language
  // switch re-words the strip.
  const tabKeys = useMemo(
    () => [...(canManage ? ["tokens" as const] : []), ...(canReadDevices ? ["certificates" as const] : []), "flow" as const],
    [canManage, canReadDevices],
  );
  const tabs = tabKeys.map((key) => ({ key, label: t(`enrollment.tabs.${key}`) }));
  /**
   * THE TAB LIVES IN THE URL, so a stat tile can hand one over.
   *
   * "Active enrollment tokens" is the one number on this page whose rows exist
   * nowhere else in the console — `GET /enrollment-tokens` is served here and
   * only here — so its tile has to point back into this page's Tokens tab. A
   * tile that pushed a URL the page did not read would be exactly the dead
   * tile this cycle removes, so the tab reads `?tab=` rather than component
   * state. `/devices` already does this with `?state=` for the same reason.
   *
   * An unknown or unpermitted `?tab=` falls back to the first tab the caller
   * may actually see, exactly as the previous component state did.
   */
  const [params, setParams] = useSearchParams();
  const requestedTab = params.get("tab");
  const tab = tabs.some((item) => item.key === requestedTab)
    ? (requestedTab as string)
    : (tabs[0]?.key ?? "flow");
  function setRequestedTab(key: string) {
    const next = new URLSearchParams(params);
    next.set("tab", key);
    // Pushed, not replaced: Back should undo a tab change, as it does on
    // /devices.
    setParams(next);
  }

  /**
   * REVOKED ENTRIES ARE HIDDEN, NOT DELETED, and there are two of them because
   * this page lists two different things that can be revoked.
   *
   * The operator's report was that revoked entries stay on this screen with no
   * way to clear them, and the obvious answer — a delete button — is the one
   * this platform cannot give. A revocation is among the most security-relevant
   * acts here: it is written to the hash-chained audit log (invariant 4), and
   * for an agent certificate the `agents` row is the only thing that maps a
   * serial in the CA's CRL back to a device and an organization. So the row
   * stays and the default view stops showing it, which is what `GET
   * /device-groups?include_archived` already does for an archived group.
   *
   * BOTH TOGGLES STATE THE COUNT THEY ARE HIDING. A filter that silently
   * removes security records is worse than the clutter it fixes.
   */
  const [showRevokedTokens, setShowRevokedTokens] = useState(false);
  const [showRevokedCerts, setShowRevokedCerts] = useState(false);
  /**
   * NARROW THE TABLE TO THE CREDENTIALS NOBODY BOUNDED.
   *
   * `max_uses` is required and capped on `POST /enrollment-tokens`, so nothing
   * can mint another unlimited token — but rows issued while the field
   * defaulted to null are still redeemable, and the server still honours them
   * deliberately (`backend/app/services/enrollment.py::enroll`: re-reading a
   * stored null as a number fails at the endpoint as a 403 indistinguishable
   * from a revocation). The only way that number falls is a human revoking
   * them, and an operator cannot revoke what they cannot find — on a fleet
   * with three hundred tokens the ∞ rows are not on the page they are looking
   * at. The server filters on the same predicate it counts with.
   */
  const [unlimitedOnly, setUnlimitedOnly] = useState(false);
  /**
   * WHICH PAGE OF TOKENS. `GET /enrollment-tokens` was unbounded and tokens
   * only accumulate — one per device onboarded, one per provisioning run, and
   * none ever deleted — so this screen, the one an operator sits on while
   * onboarding a fleet, grew its response forever. The route is paged now, and
   * a console that asked for a bounded page and offered no way to reach the
   * second one would have swapped a slow complete list for a fast truncated
   * one, which is worse: the first is visibly bad.
   */
  const [tokenOffset, setTokenOffset] = useState(0);

  const devices = useQuery({
    queryKey: ["devices"],
    queryFn: () => api.listDevices(),
    enabled: canReadDevices,
  });
  const tokens = useQuery<EnrollmentTokenPage>({
    // The flag AND the offset are IN THE KEY: the server decides what comes
    // back, so two answers to two different requests must not share one cache
    // entry. `ENROLLMENT_TOKENS_KEY` alone stays the invalidation prefix, so
    // issuing or revoking still refreshes whichever page is on screen.
    queryKey: [ENROLLMENT_TOKENS_KEY, showRevokedTokens, unlimitedOnly, tokenOffset],
    queryFn: () =>
      listEnrollmentTokens(api, {
        include_revoked: showRevokedTokens,
        unlimited_only: unlimitedOnly,
        limit: ENROLLMENT_TOKENS_PAGE_SIZE,
        offset: tokenOffset,
      }),
    enabled: canManage,
  });

  const [search, setSearch] = useState("");
  const [pendingRevoke, setPendingRevoke] = useState<PendingRevoke | null>(null);
  /**
   * The one-time plaintext bootstrap token, held in COMPONENT STATE for the
   * render that shows it and dropped on dismiss. It is never put in query
   * state: a credential parked in the query cache outlives the screen that
   * needed it.
   */
  const [issued, setIssued] = useState<EnrollmentTokenCreated | null>(null);
  const [copied, setCopied] = useState<"idle" | "done" | "failed">("idle");
  const [confirmingDismiss, setConfirmingDismiss] = useState(false);
  const [formOpen, setFormOpen] = useState(false);
  const [form, setForm] = useState(BLANK_FORM);
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  /** The validated request, waiting on the confirmation. Nothing is sent while
   *  this is set — it is the state of "asked, not yet answered". */
  const [pendingIssue, setPendingIssue] = useState<TokenDraft | null>(null);

  const create = useMutation({
    mutationFn: (body: TokenDraft) =>
      createEnrollmentToken(api, {
        ...body,
        // PHASE 3: the client/site selector. Until it exists the console
        // issues tokens for the organisation's internal client, whose
        // "Principal" site has the organisation's id -- `/me`'s `org_id`
        // (`lib/internalClient.ts`). The route requires the site.
        site_id: requireKnown(internalSiteId(me.data)),
      }),
    onSuccess: (created: EnrollmentTokenCreated) => {
      setIssued(created);
      setCopied("idle");
      setForm(BLANK_FORM);
      setAttempt(null);
      // The form closes on success: leaving it open with the fields cleared
      // invites a second click that would issue a second live credential.
      setFormOpen(false);
      void queryClient.invalidateQueries({ queryKey: [ENROLLMENT_TOKENS_KEY] });
    },
  });

  const revokeToken = useMutation({
    mutationFn: (tokenId: string) => revokeEnrollmentToken(api, tokenId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: [ENROLLMENT_TOKENS_KEY] });
    },
  });

  const revokeDevice = useMutation({
    mutationFn: (deviceId: string) => revokeAgent(api, deviceId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["devices"] });
    },
  });

  /** Backing out of a confirmation also clears the failure it may have left:
   *  a "was NOT revoked" banner that outlives the row it was about is a claim
   *  about the wrong thing. */
  function dismissConfirm() {
    setPendingRevoke(null);
    revokeToken.reset();
    revokeDevice.reset();
  }

  function askRevoke(target: PendingRevoke) {
    // Opening a fresh confirmation clears the previous attempt's banner for the
    // same reason cancelling does.
    revokeToken.reset();
    revokeDevice.reset();
    setPendingRevoke(target);
  }

  /**
   * Step 2 of three. Validates, and on success opens the confirmation — it
   * does NOT call the API. The audit's whole finding is that the control which
   * looked like it opened a form was the control that minted the credential.
   */
  function submitIssue() {
    create.reset();
    // `MAX_USES_RULE` IS UNCONDITIONAL NOW. It used to be dropped from the
    // rule set when "unlimited" was ticked, which is what made an unbounded
    // credential expressible from this form at all.
    // Held as rules + values, not as sentences, so a language switch rewords them.
    const next: Attempt = { rules: () => [...nameAndLifetime(), maxUsesRule()], values: form };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    setPendingIssue({
      name: form.name.trim(),
      expires_in_hours: Number(form.hours),
      max_uses: Number(form.maxUses),
    });
  }

  function toggleForm() {
    setAttempt(null);
    create.reset();
    setFormOpen((open) => !open);
  }

  /**
   * v3 puts "New token" in the page head, primary. From any other tab it
   * first opens the Tokens tab, so the form the click promises is the form
   * that appears; on the Tokens tab it is the same toggle the panel had.
   */
  function createFromHead() {
    if (tab !== "tokens") {
      setRequestedTab("tokens");
      if (!formOpen) toggleForm();
      return;
    }
    toggleForm();
  }

  const deviceItems: Device[] = useMemo(() => devices.data?.items ?? [], [devices.data]);
  const tokenItems: EnrollmentTokenListItem[] = useMemo(
    () => tokens.data?.items ?? [],
    [tokens.data],
  );
  /** Served whether or not they are in `items`, so the toggle can name what it
   *  is offering before the operator clicks it. */
  const revokedTokenCount = tokens.data?.revoked_count ?? 0;
  /** Still-redeemable tokens with no use limit. Org-wide and independent of
   *  the page, like `revoked_count` — a number taken from the page would
   *  understate the standing risk exactly on the deployment that has most of
   *  it. */
  const unlimitedTokenCount = tokens.data?.unlimited_count ?? 0;
  /** Tokens matching the current filter across the WHOLE org, before the page
   *  was cut — the number that tells a short page from the end of the list. */
  const tokenTotal = tokens.data?.total ?? 0;
  const tokenPage = Math.floor(tokenOffset / ENROLLMENT_TOKENS_PAGE_SIZE);
  const tokenLastPage = Math.max(
    0,
    Math.ceil(tokenTotal / ENROLLMENT_TOKENS_PAGE_SIZE) - 1,
  );

  /**
   * A REVOKE CAN SHRINK THE LIST UNDER THE OPERATOR'S FEET.
   *
   * They were on the last page of live tokens and withdrew the only row on it.
   * Without this the table renders empty beneath an empty-state reading "No
   * enrollment tokens have been issued" — a claim about the whole organization,
   * made from an offset that simply ran off the end. `total > 0` is the guard:
   * an org that genuinely has none must keep its real empty state.
   */
  useEffect(() => {
    if (tokenTotal > 0 && tokenOffset >= tokenTotal) {
      setTokenOffset(tokenLastPage * ENROLLMENT_TOKENS_PAGE_SIZE);
    }
  }, [tokenTotal, tokenOffset, tokenLastPage]);
  const now = Date.now();

  const certAttention = deviceItems.filter((device) =>
    CERT_ATTENTION.has(device.cert_state ?? ""),
  ).length;
  const connected = deviceItems.filter((device) => device.agent_status === "online").length;
  const activeTokens = tokenItems.filter((token) => tokenState(token, now) === "active").length;

  /** Devices whose agent certificate has been revoked. The device itself is
   *  untouched and still on `/devices`; it is the CERTIFICATE that is dead, and
   *  this tab is the one about certificates. */
  const revokedCertCount = deviceItems.filter((device) => device.cert_revoked).length;

  const filteredDevices = useMemo(() => {
    const needle = search.trim().toLowerCase();
    // THE REVOCATION FILTER RUNS FIRST, and the search runs over what it left.
    // Searching "revoked" must not resurrect the rows the toggle is hiding —
    // the toggle is the only control that decides that.
    const visible = showRevokedCerts
      ? deviceItems
      : deviceItems.filter((device) => !device.cert_revoked);
    if (needle === "") return visible;
    return visible.filter((device) =>
      [device.hostname, device.fqdn ?? "", device.cert_state ?? ""]
        .join(" ")
        .toLowerCase()
        .includes(needle),
    );
  }, [deviceItems, search, showRevokedCerts]);

  return (
    <>
      <PageHead
        title={t("enrollment.page.title")}
        description={t("enrollment.page.description")}
        actions={
          canManage ? (
            <Btn
              label={formOpen && tab === "tokens" ? t("common.actions.cancel") : t("enrollment.page.createToken")}
              icon={formOpen && tab === "tokens" ? undefined : "plus"}
              variant={formOpen && tab === "tokens" ? "secondary" : "primary"}
              onClick={createFromHead}
            />
          ) : undefined
        }
      />

      {/* Tokens are what depend on a published package, so only the people
          who issue them are shown what else that needs. */}
      {canManage ? <EnrollmentPrerequisites /> : null}

      {/* A `/me` FAILURE USED TO READ AS "YOU ARE NOT ALLOWED". Permissions
          default to `[]` when the request fails, which silently removes both
          gated tabs -- and the page then looks exactly like the page a
          read-only technician is supposed to see. Saying which of the two it is
          costs one line and is the difference between "ask for access" and
          "retry". (The same defaulting is in `App.tsx`'s shell, where it hides
          nav items the same way; that file belongs to no module and is left to
          the controller.) */}
      {me.isError ? (
        <div className="notice red mb-3.5" role="alert">
          <Icon name="warning" size={17} />
          <div>
            {describeSubmitError(me.error)}
            <br />
            {t("enrollment.page.permissionsUnread")}
          </div>
        </div>
      ) : null}

      {/* EVERY TILE OPENS THE ROWS IT COUNTED.

          Three of these four are computed from `GET /devices`, and /devices is
          where those rows are readable AND filterable — it reads `?state=` from
          the URL, so "Connected" and "Certificate attention" arrive with the
          exact tab their number was counted from. Neither points at this page's
          own Certificates tab, and the reason is worth stating: that tab lists
          every device with a search box, so two rows needing attention would
          arrive buried in three hundred that do not. `?state=certs` is the
          filtered list; this page is not.

          The fourth is the exception. Enrollment tokens are served by
          `GET /enrollment-tokens` and rendered by nothing else in the console,
          so it points back into this page's Tokens tab — which is addressable
          precisely so that link is not a no-op (see `?tab=` above). It lands on
          the whole token table rather than a filtered one because no
          active-only filter exists there, and inventing one so a tile could
          have a prettier href would be inventing UI.

          COLOUR IS CATEGORY: neutral for the inert estate total, green for
          healthy connectivity, gold for a deadline, primary for the live
          credential surface. WEIGHT IS URGENCY, and only one number here has a
          clock on it — an expiring agent certificate is a machine about to fall
          off the fleet. The other three are facts, not tasks.

          The "—" is a permission or a pending read, never a zero, so every
          `zeroLabel` below describes a zero that was actually measured. */}
      <StatsGrid columns={4}>
        <StatCard
          label={t("enrollment.tiles.enrolled")}
          value={canReadDevices && devices.data ? String(devices.data.total) : "—"}
          icon="devices"
          to="/devices"
          tone="neutral"
          zeroLabel={t("enrollment.tiles.enrolledZero")}
        />
        <StatCard
          label={t("enrollment.tiles.connected")}
          value={canReadDevices && devices.data ? String(connected) : "—"}
          icon="activity"
          to="/devices?state=online"
          tone="green"
          zeroLabel={t("enrollment.tiles.connectedZero")}
        />
        <StatCard
          label={t("enrollment.tiles.certAttention")}
          value={canReadDevices && devices.data ? String(certAttention) : "—"}
          icon="certificate"
          to="/devices?state=certs"
          tone="gold"
          urgent
          /* "0 expiring" and "no certificate state was recorded" are the same
             pixel otherwise. */
          zeroLabel={t("enrollment.tiles.certAttentionZero")}
        />
        <StatCard
          label={t("enrollment.tiles.activeTokens")}
          value={canManage && tokens.data ? String(activeTokens) : "—"}
          icon="certificate"
          to="/enrollment?tab=tokens"
          tone="primary"
          /* NOT an alarm at zero: no live bootstrap credential outstanding is
             the resting state of a fleet that has finished enrolling. */
          zeroLabel={t("enrollment.tiles.activeTokensZero")}
        />
      </StatsGrid>

      <Tabs items={tabs} active={tab} onSelect={setRequestedTab} />

      {tab === "tokens" ? (
        <>
          {issued ? (
            <>
              <div className="notice gold mb-3.5" role="alert">
                <Icon name="warning" size={17} />
                {/* A STACK OF BLOCKS, NOT A `<br />`-SEPARATED INLINE RUN, and
                    that is the whole fix. `.code` is `padding: 12px`, and
                    vertical padding on an INLINE box does not grow the line box
                    it sits in (CSS 2.1 §10.6.1) — which is also why that rule's
                    `overflow-x: auto` was inert here. So the 12px-padded token
                    chip on a 20px line bled 12px upward and 12px downward and
                    painted over the two lines around it: the one instruction
                    the operator needs ("it is shown once", "cannot show it
                    again") was underneath the value it is about.

                    Each line is its own block now, so every box contains its
                    own padding and the chip is a `<pre>` — the element for a
                    value whose exact characters matter, and the one `.code`'s
                    `overflow-x` was written for. It wraps rather than scrolls
                    because the fallback path here is transcribing it by hand.
                    `min-w-0` lets it shrink inside the flex notice instead of
                    pushing the row wider than the card. */}
                <div className="flex min-w-0 flex-col items-start gap-2">
                  <strong>{t("enrollment.issued.title")}</strong>
                  <p>{t("enrollment.issued.body")}</p>
                  <pre className="code w-full whitespace-pre-wrap break-all">{issued.token}</pre>
                  <div className="flex flex-wrap gap-2">
                    {/* THE COPY BUTTON IS THE POINT. A ~43-character
                        high-entropy string selected by hand is where a
                        character gets dropped, and the next thing the operator
                        does is destroy the only copy. */}
                    <Btn
                      label={copied === "done" ? t("enrollment.issued.copied") : t("enrollment.issued.copy")}
                      icon={copied === "done" ? "check" : undefined}
                      small
                      onClick={() => {
                        void copyToClipboard(issued.token).then((ok) =>
                          setCopied(ok ? "done" : "failed"),
                        );
                      }}
                    />
                    <Btn
                      label={t("enrollment.issued.done")}
                      small
                      onClick={() => {
                        // Confirm ONLY when the copy button was never used. A
                        // confirmation on the path where we know they have the
                        // token is friction that teaches people to click
                        // through the one that matters.
                        if (copied === "done") {
                          setIssued(null);
                          setCopied("idle");
                          create.reset();
                        } else {
                          setConfirmingDismiss(true);
                        }
                      }}
                    />
                  </div>
                </div>
              </div>
              {copied === "done" ? (
                <p role="status" className="form-saved">
                  {t("enrollment.issued.copiedStatus")}
                </p>
              ) : null}
              {copied === "failed" ? (
                <p role="alert" className="form-error">
                  {t("enrollment.issued.copyFailed")}
                </p>
              ) : null}
            </>
          ) : null}

          <Panel
            title={t("enrollment.form.title")}
            subtitle={t("enrollment.form.subtitle")}
          >
            {formOpen ? (
              <Form onSubmit={submitIssue}>
                <Field label={t("enrollment.form.name")}>
                  <input
                    className={inputClass}
                    placeholder={t("enrollment.form.namePlaceholder")}
                    value={form.name}
                    onChange={(changeEvent) =>
                      setForm({ ...form, name: changeEvent.target.value })
                    }
                  />
                </Field>
                <Field label={t("enrollment.form.hours")}>
                  <input
                    className={inputClass}
                    type="number"
                    min={1}
                    max={MAX_LIFETIME_HOURS}
                    value={form.hours}
                    onChange={(changeEvent) =>
                      setForm({ ...form, hours: changeEvent.target.value })
                    }
                  />
                </Field>
                {/* THE "Allow unlimited uses" CHECKBOX WAS HERE AND IS GONE
                    WITH THE SERVER'S ABILITY TO HONOUR IT. `max_uses` is
                    required and capped at `MAX_TOKEN_USES`, so ticking it
                    could only ever have produced a 422. `max` on the input is
                    a courtesy; `MAX_USES_RULE` is what refuses, and the route
                    is what decides (invariant 6). */}
                <Field label={t("enrollment.form.maxUses")}>
                  <input
                    className={inputClass}
                    type="number"
                    min={1}
                    max={MAX_TOKEN_USES}
                    value={form.maxUses}
                    onChange={(changeEvent) =>
                      setForm({ ...form, maxUses: changeEvent.target.value })
                    }
                  />
                </Field>
                <p className="mini-sub">
                  {t("enrollment.form.maxUsesHelp", { max: MAX_TOKEN_USES })}
                </p>
                {/* THE ELLIPSIS IS LOAD-BEARING, and it is the convention
                    every desktop menu uses: a control that opens a further
                    step, rather than doing the thing. The audit's finding was
                    a button whose label described the intent while the click
                    performed the effect; this label promises a confirmation
                    and the confirmation is what writes. */}
                <FormActions
                  label={t("enrollment.form.issue")}
                  pendingLabel={t("enrollment.form.issuing")}
                  pending={create.isPending}
                  problems={attemptProblems(attempt)}
                />
              </Form>
            ) : (
              /* THE WAY OUT IS IN THE PANEL, NOT A SENTENCE POINTING AT THE
                 PAGE HEAD. Closed, this panel was a heading, a subtitle and
                 one line of prose whose only content was "use the button
                 above" -- a card with nothing in it and an instruction to
                 look somewhere else (design review M6). `EmptyState` is the
                 console's one shape for "there is nothing here": the glyph,
                 what is missing, WHY, and the way out as a control the reader
                 can press where they are reading.

                 NO `action`, AND THE TESTS ARE WHY. A second "Create token"
                 in this panel is a second control with the same accessible
                 name as the page head's, sixty pixels below it, calling the
                 same toggle -- thirteen of this module's tests found two
                 buttons where they ask for one, which is exactly what a
                 screen reader finds. The head keeps the single primary action
                 and this state names it; `/bcp/report`'s "No period chosen"
                 sits under its controls in the same shape for the same
                 reason. What changed is that the resting state is now the
                 console's recognised one -- glyph, what is missing, why --
                 instead of a paragraph in an otherwise blank card.

                 `headingLevel` STAYS 3: this panel HAS a title, so its `h2` is
                 the rung above and `h3` is the next one down. */
              <EmptyState
                icon="key"
                title={t("enrollment.form.emptyTitle")}
                body={t("enrollment.form.emptyBody")}
              />
            )}
            {/* THE SERVER'S REASON, not a fixed sentence (cycle 4). This
                previously said only "The token was not created", which is true
                of a 403, a 409 on a duplicate name and a 422 on `max_uses: 0`
                alike -- three different corrections, one indistinguishable
                message. `describeSubmitError` translates the refusal; the
                second line keeps the guarantee that nothing was issued. */}
            {create.error ? (
              <div className="notice red" role="alert">
                <Icon name="warning" size={17} />
                <div>
                  {describeSubmitError(create.error)}
                  <br />
                  {t("enrollment.form.notIssued")}
                </div>
              </div>
            ) : null}
          </Panel>

          {/* A REVOKE THAT FAILED USED TO SAY NOTHING AT ALL. Neither revoke
              mutation had an `onError`, so a 503 from the proxy left the
              confirm buttons sitting there and the row still listed as active,
              with no way for the operator to tell the request from a slow one.
              Revocation is the destructive half of this screen: the outcome a
              user must never be wrong about is "this credential is dead". */}
          {revokeToken.error ? (
            <div className="notice red mb-3.5" role="alert">
              <Icon name="warning" size={17} />
              <div>
                {describeSubmitError(revokeToken.error)}
                <br />
                {t("enrollment.tokens.notRevoked")}
              </div>
            </div>
          ) : null}

          <Panel bare>
            {/* THE COUNT IS PART OF THE CONTROL, not a caption beside it. A
                revoked bootstrap credential is a security record; a filter that
                removed it from the screen without saying so would trade one
                defect for a worse one. When there are none to hide the label
                still says so, because "no revoked tokens" and "revoked tokens
                you cannot see" must never be the same pixels. */}
            {/* ONLY ONCE THE LIST HAS ACTUALLY BEEN READ. "None have been
                revoked" is a claim about data, and rendering it beside a
                spinner or a failed request would be asserting it from a screen
                that has not been told. */}
            {/* THE CREDENTIALS NOBODY CHOSE THE BOUNDS OF, SAID OUT LOUD.
                `max_uses` used to default to null on `POST /enrollment-tokens`
                and null means unlimited, so a deployment can be holding live
                tokens that enrol machines without limit — issued by a form
                that never asked. Nothing rewrites those rows (a bound this
                platform invented after the fact would either be a number no
                human chose or, below the row's `use_count`, an instant
                rejection at the endpoint indistinguishable from a revocation),
                so the fix is to put a human in front of the Revoke button.

                RENDERED ONLY ONCE THE LIST HAS BEEN READ, and only when the
                count is non-zero: "you hold 0 unlimited tokens" is a sentence
                that trains an operator to skip the box on the day it says 3. */}
            {tokens.data && unlimitedTokenCount > 0 ? (
              <div role="alert" className="notice gold mb-3.5">
                {t("enrollment.tokens.unlimited", { count: unlimitedTokenCount })}{" "}
                {t("enrollment.tokens.unlimitedBody")}{" "}
                {unlimitedOnly ? null : (
                  <button
                    type="button"
                    className="btn small"
                    onClick={() => {
                      // Back to page one: the list this offset indexes into is
                      // a different list once it is filtered.
                      setTokenOffset(0);
                      setUnlimitedOnly(true);
                    }}
                  >
                    {t("enrollment.tokens.showThem")}
                  </button>
                )}
              </div>
            ) : null}
            {tokens.data ? (
              <FilterBar>
                <label className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    checked={showRevokedTokens}
                    onChange={(changeEvent) => {
                      // BACK TO PAGE ONE. The list the offset indexes into is
                      // a different list once revoked rows join it, so page
                      // three of the live tokens is not page three of all of
                      // them -- keeping the offset lands the operator
                      // somewhere neither they nor this page chose.
                      setTokenOffset(0);
                      setShowRevokedTokens(changeEvent.target.checked);
                    }}
                  />
                  {/* "hidden" IS ONLY TRUE WHILE THEY ARE HIDDEN. A label
                      that still says "3 hidden" under a ticked box describes
                      the screen the operator is not looking at. */}
                  <span>
                    {revokedTokenCount === 0
                      ? t("enrollment.tokens.showRevokedNone")
                      : showRevokedTokens
                        ? t("enrollment.tokens.showRevokedShown", { count: revokedTokenCount })
                        : t("enrollment.tokens.showRevokedHidden", { count: revokedTokenCount })}
                  </span>
                </label>
                {/* OFFERED ONLY WHERE IT WOULD FIND SOMETHING, and left
                    visible while it is on so the operator can turn it off --
                    a filter with no control to clear it is a table that has
                    silently stopped being the table. */}
                {unlimitedTokenCount > 0 || unlimitedOnly ? (
                  <label className="flex items-center gap-2">
                    <input
                      type="checkbox"
                      checked={unlimitedOnly}
                      onChange={(changeEvent) => {
                        setTokenOffset(0);
                        setUnlimitedOnly(changeEvent.target.checked);
                      }}
                    />
                    <span>{t("enrollment.tokens.onlyUnlimited", { count: unlimitedTokenCount })}</span>
                  </label>
                ) : null}
                <span className="filter-spacer" />
              </FilterBar>
            ) : null}
            <QueryState
              loading={tokens.isLoading}
              error={tokens.error}
              empty={tokenItems.length === 0}
              emptyIcon="certificate"
              emptyLabel={
                unlimitedOnly
                  ? t("enrollment.tokens.emptyUnlimited")
                  : revokedTokenCount > 0 && !showRevokedTokens
                    ? t("enrollment.tokens.emptyAllRevoked")
                    : t("enrollment.tokens.emptyNone")
              }
            >
              <TableWrap>
                <table>
                  <thead>
                    <tr>
                      <th scope="col">{t("enrollment.tokens.token")}</th>
                      <th scope="col">{t("enrollment.tokens.expires")}</th>
                      <th scope="col">{t("enrollment.tokens.uses")}</th>
                      <th scope="col">{t("enrollment.tokens.state")}</th>
                      <th scope="col">{t("enrollment.tokens.actions")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {tokenItems.map((token) => {
                      const state = tokenState(token, now);
                      return (
                        <tr key={token.id}>
                          {/* v3's row: the key glyph leads, the name is the
                              title, the id the sub-line (it is what an
                              operator greps the audit log for). */}
                          <Td lead={<Icon name="key" size={18} />}>
                            <div className="cell-title">{token.name || t("enrollment.tokens.unnamed")}</div>
                            <div className="cell-sub mono">{token.id}</div>
                          </Td>
                          <td className="mono">{formatDateTime(token.expires_at, zone)}</td>
                          <td>
                            {/* v3's "Used" cell: the mono fraction over the
                                bar. A bar needs a denominator, so an
                                unlimited token draws the words and no bar. */}
                            <span className="mono">
                              {token.use_count} / {token.max_uses ?? "∞"}
                            </span>
                            {token.max_uses == null ? (
                              // `∞` ALONE IS A GLYPH SOMEBODY SKIMS PAST. The
                              // standing risk in this table is the unlimited
                              // token, so it is said in words.
                              <div className="cell-sub">{t("enrollment.tokens.unlimitedWord")}</div>
                            ) : (
                              <Progress
                                value={(token.use_count / Math.max(1, token.max_uses)) * 100}
                                tone={state === "spent" ? "gold" : "green"}
                                label={t("enrollment.tokens.usesBar", { name: token.name || token.id })}
                              />
                            )}
                          </td>
                          <td>
                            <StatusPill value={TOKEN_PILL_SLUG[state]} label={humaniseEnum(state, "EnrollmentTokenState")} />
                          </td>
                          <td>
                            {token.revoked ? (
                              <NoValue label={t("enrollment.tokens.alreadyRevoked")} />
                            ) : (
                              <Btn
                                label={t("enrollment.tokens.revoke")}
                                icon="lock"
                                small
                                onClick={() =>
                                  askRevoke({
                                    kind: "token",
                                    id: token.id,
                                    subject: token.name || token.id,
                                  })
                                }
                              />
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </TableWrap>
            </QueryState>
            {/* THE PAGER, AND THE COUNT BESIDE IT. The count is the org's, not
                the page's: "Page 1 of 3 — 61 tokens" is the only thing that
                lets an operator tell a short page from the end of the list,
                and it is the same reason `revoked_count` sits above the table.
                Absent entirely when everything fits, so a single-page org is
                not given controls that do nothing. Mirrors the patch rollups'
                footer (`modules/patch/DevicesPanel.tsx`). */}
            <Pager
              page={tokenPage}
              pageCount={tokenLastPage + 1}
              previousLabel={t("enrollment.tokens.previous")}
              nextLabel={t("enrollment.tokens.next")}
              onPrevious={() =>
                setTokenOffset(Math.max(0, tokenOffset - ENROLLMENT_TOKENS_PAGE_SIZE))
              }
              onNext={() => setTokenOffset(tokenOffset + ENROLLMENT_TOKENS_PAGE_SIZE)}
            >
              {t("enrollment.tokens.page", { page: tokenPage + 1, pages: tokenLastPage + 1, count: tokenTotal })}
            </Pager>
          </Panel>
        </>
      ) : tab === "certificates" ? (
        <Panel bare>
          {revokeDevice.error ? (
            <div className="notice red mb-3.5" role="alert">
              <Icon name="warning" size={17} />
              <div>
                {describeSubmitError(revokeDevice.error)}
                <br />
                {t("enrollment.certs.notRevoked")}
              </div>
            </div>
          ) : null}
          <FilterBar>
            <SearchField
              placeholder={t("enrollment.certs.searchPlaceholder")}
              aria-label={t("enrollment.certs.searchAria")}
              value={search}
              onChange={(changeEvent) => setSearch(changeEvent.target.value)}
            />
            {/* A REVOKED CERTIFICATE IS NOT DELETED, HERE OR ANYWHERE. The
                device stays on `/devices` and the `agents` row stays in the
                database — it is the only mapping from a serial in the CA's CRL
                back to a machine, and revocation deliberately commits the
                database flag BEFORE the CRL write so that write can be retried.
                What this control does is take a dead certificate off the list
                of certificates a human is meant to act on, and say how many it
                took. */}
            {/* And, as with the token toggle, only once the read has landed:
                the count is a fact about `GET /devices`. */}
            {devices.data ? (
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={showRevokedCerts}
                  onChange={(changeEvent) => setShowRevokedCerts(changeEvent.target.checked)}
                />
                <span>
                  {revokedCertCount === 0
                    ? t("enrollment.certs.showRevokedNone")
                    : showRevokedCerts
                      ? t("enrollment.certs.showRevokedShown", { count: revokedCertCount })
                      : t("enrollment.certs.showRevokedHidden", { count: revokedCertCount })}
                </span>
              </label>
            ) : null}
            <span className="filter-spacer" />
          </FilterBar>
          <QueryState
            loading={devices.isLoading}
            error={devices.error}
            empty={filteredDevices.length === 0}
            emptyIcon="certificate"
            emptyLabel={
              deviceItems.length === 0
                ? t("enrollment.certs.emptyNone")
                : // "No certificate matches that filter" over an empty search
                  // box would send an operator hunting for a filter they never
                  // set. When the revocation toggle is what emptied the table,
                  // it says so and names the control that undoes it.
                  search.trim() === "" && !showRevokedCerts && revokedCertCount > 0
                  ? t("enrollment.certs.emptyAllRevoked")
                  : t("enrollment.certs.emptyFiltered")
            }
          >
            <TableWrap>
              <table>
                <thead>
                  <tr>
                    <th scope="col">{t("enrollment.certs.device")}</th>
                    <th scope="col">{t("enrollment.certs.certificate")}</th>
                    <th scope="col">{t("enrollment.certs.expires")}</th>
                    <th scope="col">{t("enrollment.certs.lastSeen")}</th>
                    {canManage ? <th scope="col">{t("enrollment.certs.actions")}</th> : null}
                  </tr>
                </thead>
                <tbody>
                  {filteredDevices.map((device) => (
                    <tr key={device.id}>
                      {/* A row that names a device leads with its OS glyph. */}
                      <Td lead={<OsIcon os={device.os_family ?? ""} />}>
                        <div className="cell-title">{device.hostname}</div>
                        {device.fqdn ? <div className="cell-sub">{device.fqdn}</div> : null}
                      </Td>
                      <td>
                        {/* REVOCATION OUTRANKS THE LIFECYCLE STATE, and until
                            this cycle it was not shown at all: `cert_state` is
                            derived from `cert_not_after` alone, so a
                            certificate revoked this morning still read `ok`.
                            An operator who had just revoked a machine saw
                            nothing change on this screen. */}
                        {device.cert_revoked ? (
                          <StatusPill value="critical" label={t("enrollment.certs.revoked")} />
                        ) : device.cert_state ? (
                          <StatusPill
                            value={CERT_PILL_SLUG[device.cert_state] ?? "neutral"}
                            label={humaniseEnum(device.cert_state, "CertificateState")}
                          />
                        ) : (
                          <NoValue label={t("enrollment.certs.noAgent")} />
                        )}
                      </td>
                      <td className="mono">{formatDate(device.cert_not_after, zone)}</td>
                      <td className="mono">{formatDateTime(device.last_seen_at, zone)}</td>
                      {canManage ? (
                        <td>
                          {/* Nothing left to revoke, exactly as a revoked token
                              offers nothing. Leaving the button live invited a
                              confirmation dialog promising to drop a connection
                              that was already dropped. */}
                          {device.cert_state == null || device.cert_revoked ? (
                            <NoValue label={t("enrollment.certs.nothingToRevoke")} />
                          ) : (
                            <Btn
                              label={t("enrollment.certs.revokeAgent")}
                              icon="shield"
                              small
                              onClick={() =>
                                askRevoke({
                                  kind: "agent",
                                  // `hostname` is nullable on the wire, and a
                                  // confirmation whose subject is blank is the
                                  // one thing this dialog must never be. The
                                  // FQDN, then the id, rather than nothing.
                                  id: device.id,
                                  subject: device.hostname ?? device.fqdn ?? device.id,
                                })
                              }
                            />
                          )}
                        </td>
                      ) : null}
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableWrap>
          </QueryState>
        </Panel>
      ) : (
        <Panel
          title={t("enrollment.flow.title")}
          subtitle={t("enrollment.flow.subtitle")}
        >
          {/* Prose about how the platform works, not data about a tenant. It is
              reproduced from the design because these four steps are the
              security invariants an operator installing an agent needs to know
              (invariants 1 and 2), and no request could tell them. */}
          <div className="mini-list">
            <div className="mini-row">
              <span className="mini-icon">
                <Icon name="certificate" size={14} />
              </span>
              <span className="mini-copy">
                <div className="mini-title">{t("enrollment.flow.step1")}</div>
                <div className="mini-sub">{t("enrollment.flow.step1Body")}</div>
              </span>
            </div>
            <div className="mini-row">
              <span className="mini-icon">
                <Icon name="lock" size={14} />
              </span>
              <span className="mini-copy">
                <div className="mini-title">{t("enrollment.flow.step2")}</div>
                <div className="mini-sub">{t("enrollment.flow.step2Body")}</div>
              </span>
            </div>
            <div className="mini-row">
              <span className="mini-icon">
                <Icon name="shield" size={14} />
              </span>
              <span className="mini-copy">
                <div className="mini-title">{t("enrollment.flow.step3")}</div>
                <div className="mini-sub">{t("enrollment.flow.step3Body")}</div>
              </span>
            </div>
            <div className="mini-row">
              <span className="mini-icon">
                <Icon name="activity" size={14} />
              </span>
              <span className="mini-copy">
                <div className="mini-title">{t("enrollment.flow.step4")}</div>
                <div className="mini-sub">{t("enrollment.flow.step4Body")}</div>
              </span>
            </div>
          </div>
        </Panel>
      )}

      {/* STEP 3 OF THREE. Names the token, states its lifetime and its
          redemption limit, and says the plaintext is shown once — the three
          facts an operator needs in order to notice they set the wrong one.
          NOT `destructive`, AND THE CONDITION THAT MADE IT SO IS GONE. It
          used to read `destructive={pendingIssue?.max_uses == null}` — red for
          the unlimited case, which this form can no longer express. Minting a
          bounded credential is privileged, not destructive, and a dialog that
          is always red is a dialog nobody reads. */}
      <ConfirmDialog
        open={pendingIssue != null}
        title={t("enrollment.confirm.issueTitle")}
        subject={pendingIssue?.name ?? ""}
        body={
          pendingIssue
            ? t("enrollment.confirm.issueBody", {
                count: pendingIssue.expires_in_hours,
                uses: describeUses(pendingIssue.max_uses),
              })
            : ""
        }
        confirmLabel={t("enrollment.confirm.issueLabel")}
        onCancel={() => setPendingIssue(null)}
        onConfirm={() => {
          const body = pendingIssue;
          setPendingIssue(null);
          if (body) create.mutate(body);
        }}
      />

      {/* Dismissing the reveal destroys the only copy of the plaintext, so the
          confirmation appears exactly where the risk is: the operator who never
          pressed Copy. */}
      <ConfirmDialog
        open={confirmingDismiss}
        title={t("enrollment.confirm.dismissTitle")}
        subject={issued?.name || issued?.id || ""}
        body={t("enrollment.confirm.dismissBody")}
        confirmLabel={t("enrollment.confirm.dismissLabel")}
        destructive
        onCancel={() => setConfirmingDismiss(false)}
        onConfirm={() => {
          setConfirmingDismiss(false);
          setIssued(null);
          setCopied("idle");
          create.reset();
        }}
      />

      <ConfirmDialog
        open={pendingRevoke?.kind === "token"}
        title={t("enrollment.confirm.revokeTokenTitle")}
        subject={pendingRevoke?.subject ?? ""}
        body={t("enrollment.confirm.revokeTokenBody")}
        confirmLabel={t("enrollment.confirm.revokeTokenLabel")}
        destructive
        onCancel={dismissConfirm}
        onConfirm={() => {
          const target = pendingRevoke;
          setPendingRevoke(null);
          if (target) revokeToken.mutate(target.id);
        }}
      />

      {/* No `requireTyping`: the blast radius is one device, which can be
          re-enrolled with a new token. Typing a hostname here would teach the
          copy-the-word-without-reading habit that makes the confirmations which
          DO need it worthless. */}
      <ConfirmDialog
        open={pendingRevoke?.kind === "agent"}
        title={t("enrollment.confirm.revokeAgentTitle")}
        subject={pendingRevoke?.subject ?? ""}
        body={t("enrollment.confirm.revokeAgentBody")}
        confirmLabel={t("enrollment.confirm.revokeAgentLabel")}
        destructive
        onCancel={dismissConfirm}
        onConfirm={() => {
          const target = pendingRevoke;
          setPendingRevoke(null);
          if (target) revokeDevice.mutate(target.id);
        }}
      />
    </>
  );
}
