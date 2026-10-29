import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { NoValue } from "../../components/emptystate";
import { useT } from "../../i18n/context";
import { Trans } from "../../i18n/Trans";
import { describeSubmitError } from "../../components/form";
import { Button, Field, Td, Th, inputClass } from "../../components/ui";
import type { Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { formatDate, toInstant } from "../../lib/datetime";
import { internalClientId, requireKnown } from "../../lib/internalClient";
import {
  FilterBar,
  PageHead,
  Panel,
  QueryState,
  StatusPill,
  TableWrap,
  type Kind,
} from "../../shell/components";
import { Icon } from "../../shell/icons";
import { useOrgZone } from "../../lib/zone";
import type { BadgeVariant } from "../jobs/jobState";
import { useUserTimezone } from "../ipam/api";
import {
  useCreatePushAuthorizationMutation,
  usePushAuthorizations,
  usePushPermissions,
  useRevokePushAuthorizationMutation,
} from "./api";
import { PushHazardNotice, PushNav } from "./PushNav";
import { PushReadiness } from "./PushReadiness";
import {
  describeReach,
  formatCount,
  GRANT_STATUS_PRESENTATION,
  grantStatus,
  parsePrefix,
  prefixSize,
} from "./pushState";
import type { PushAuthorizationOut } from "./types";

/** The presentation table's badge vocabulary, as v3 kinds. */
const KIND_OF_VARIANT: Record<BadgeVariant, Kind> = {
  muted: "mut",
  info: "info",
  success: "ok",
  destructive: "crit",
  warning: "warn",
};

// **THE MODULE'S FIRST CONTROL SURFACE.**
//
// A row in this table is an operator's statement that this platform may
// present an administrative credential to machines inside a prefix -- machines
// nobody has enrolled, over a transport that cannot authenticate the target on
// first contact. `ScanAuthorizationsPage.tsx` is the same screen one privilege
// level down, and this page follows its discipline for the same reasons and
// with more at stake:
//
//   * EVERY ROW STATES ITS REACH IN ADDRESSES. "10.0.0.0/8" is four characters
//     and 16,777,216 machines, and nobody converts mask lengths in their head.
//   * THE GRANT FORM PREVIEWS WHAT IT WOULD OPEN BEFORE IT IS MADE.
//     Deny-by-default only helps if the person granting knows what they are
//     opening.
//   * REVOCATION STAMPS, IT DOES NOT DELETE. Revoked and expired rows stay
//     visible on request so "who authorized presenting credentials on that
//     segment in March" stays answerable.
//   * AN AUTHORIZED PREFIX IS NOT A PUSH. This grant is one of four
//     independent bounds and satisfies none of the others, and the page says
//     so rather than leaving an operator to infer that granting it finished
//     the job.
//   * AN IPAM SCAN AUTHORIZATION OVER THE SAME PREFIX GRANTS NOTHING HERE, and
//     the page says that too. It is the assumption most likely to be made and
//     it is exactly wrong.
//
// This page renders `agent.push.scope.manage` controls only when the caller
// appears to hold the code. That is cosmetic: the server refuses regardless,
// and it is the only thing that refuses.
//
// v3 draws this as one list -- prefix, who authorized it and when, state,
// expiry -- and one note under it. The grant form the design opens from its
// "New authorization" button is inline below the list here, so the head
// carries no button that would only scroll to it.
export function PushAuthorizationsPage() {
  const t = useT();
  const permissions = usePushPermissions();
  const [includeInactive, setIncludeInactive] = useState(false);
  const grants = usePushAuthorizations(includeInactive);
  const revoke = useRevokePushAuthorizationMutation();

  return (
    <div>
      {/* THE TABS SIT UNDER THE HEAD, as they do on /audit, /credentials,
          /patch and /remote. Above it they read as an eyebrow band between the
          breadcrumb and the h1, and the page loses the vertical rhythm every
          other tabbed screen in the console keeps: crumb, title, description,
          then the strip that switches what follows. */}
      <PageHead
        title={t("agentPush.authorizations.title")}
        description={t("agentPush.authorizations.description")}
      />

      <PushNav />

      <PushHazardNotice />

      <PushReadiness />

      <Panel
        title={t("agentPush.authorizations.panelTitle")}
        subtitle={t("agentPush.authorizations.panelSubtitle")}
        bare
      >
        <FilterBar>
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={includeInactive}
              onChange={(e) => setIncludeInactive(e.target.checked)}
            />
            <span>{t("agentPush.authorizations.showRevoked")}</span>
          </label>
          <span className="filter-spacer" />
          <span className="note mono">
            {grants.data ? t("agentPush.authorizations.count", { count: grants.data.length }) : "…"}
          </span>
        </FilterBar>
        <QueryState
          loading={grants.isLoading}
          error={grants.error}
          empty={(grants.data ?? []).length === 0}
          emptyLabel={t("agentPush.authorizations.empty")}
          emptyIcon="shield"
        >
          <TableWrap>
            <table>
              <thead>
                <tr>
                  <th scope="col">{t("agentPush.shared.prefix")}</th>
                  <th scope="col">{t("agentPush.shared.status")}</th>
                  <th scope="col">{t("agentPush.authorizations.expires")}</th>
                  <th scope="col">{t("agentPush.shared.note")}</th>
                  {/* `Th` with no children renders an `sr-only` name --
                      "Actions" by default -- rather than an empty header cell
                      a screen reader announces as nothing. */}
                  {permissions.scopeManage && <Th />}
                </tr>
              </thead>
              <tbody>
                {grants.data?.map((grant) => (
                  <GrantRow
                    key={grant.id}
                    grant={grant}
                    canManage={permissions.scopeManage}
                    onRevoke={() => revoke.mutate(grant.id)}
                  />
                ))}
              </tbody>
            </table>
          </TableWrap>
        </QueryState>
        {revoke.isError && (
          <p role="alert" className="form-error" style={{ margin: "12px 18px" }}>
            {describeSubmitError(revoke.error)}
          </p>
        )}
      </Panel>

      {/* THE FOUR BOUNDS, IN ONE PLACE. This note carried three of them while
          the panel subtitle above carried all four in 448 characters; the
          fourth (the site server) moves here and the subtitle goes. A bound
          this page does not name is one an operator will assume away. */}
      <p className="note mb-4">{t("agentPush.authorizations.fourBounds")}</p>

      {permissions.scopeManage && <GrantForm />}
    </div>
  );
}

function GrantRow({
  grant,
  canManage,
  onRevoke,
}: {
  grant: PushAuthorizationOut;
  canManage: boolean;
  onRevoke: () => void;
}) {
  const t = useT();
  const status = grantStatus(grant);
  const zone = useOrgZone();
  const presentation = GRANT_STATUS_PRESENTATION[status];

  return (
    <tr>
      {/* v3's row: the shield glyph leads, the prefix is the title, and the
          sub-line says who authorized it and when. THE SERVER'S COUNT of
          addresses, not one derived here: one answer to "how big is this
          grant", computed in `agent_push/net.py`. */}
      {/* The gutter and the pill are the same carrier read twice, so they
          come from the same table: `Td` draws nothing for ok, mut and info,
          so an active or a revoked grant stays plain and only an expired one
          warns, exactly as its pill does. */}
      <Td lead={<Icon name="shield-check" size={18} />} gutter={KIND_OF_VARIANT[presentation.variant]}>
        <div className="cell-title mono">{grant.prefix}</div>
        <div className="cell-sub">
          <span>
            {t("agentPush.shared.addresses", {
              count: grant.address_count,
              formatted: formatCount(grant.address_count),
            })}
          </span>{" "}
          ·{" "}
          <Trans
            k="agentPush.authorizations.authorizedBy"
            vars={{ date: formatDate(grant.authorized_at, zone), by: grant.authorized_by }}
            components={{ by: <span className="mono" /> }}
          />
        </div>
      </Td>
      <Td>
        <span title={presentation.meaning}>
          <StatusPill
            value={status}
            kind={KIND_OF_VARIANT[presentation.variant]}
            label={presentation.label}
          />
        </span>
      </Td>
      <Td className="mono">
        {grant.expires_at === null ? (
          status === "revoked" ? (
            <NoValue label={t("agentPush.authorizations.revokedNoExpiry")} />
          ) : (
            t("agentPush.authorizations.never")
          )
        ) : status === "expired" ? (
          t("agentPush.authorizations.lapsed", { date: formatDate(grant.expires_at, zone) })
        ) : (
          formatDate(grant.expires_at, zone)
        )}
      </Td>
      <Td>{grant.note || <NoValue label={t("agentPush.shared.noNote")} />}</Td>
      {canManage && (
        <Td>
          {grant.revoked_at === null ? (
            <Button variant="outline" onClick={onRevoke}>
              {t("agentPush.authorizations.revoke")}
            </Button>
          ) : (
            <NoValue label={t("agentPush.authorizations.alreadyRevoked")} />
          )}
        </Td>
      )}
    </tr>
  );
}

function GrantForm() {
  const t = useT();
  const api = useApi();
  const create = useCreatePushAuthorizationMutation();
  // Same `["me"]` key the shell already holds, so this is a cache read.
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const zone = useUserTimezone();
  const [prefix, setPrefix] = useState("");
  const [note, setNote] = useState("");
  const [expiresAt, setExpiresAt] = useState("");

  const parsed = parsePrefix(prefix);

  return (
    // `id` is the target of the readiness panel's "Authorize a range".
    <div id="authorize">
    <Panel
      title={t("agentPush.authorizations.form.title")}
      subtitle={t("agentPush.authorizations.form.subtitle")}
    >
      <div className="form-grid">
        <Field label={t("agentPush.shared.prefix")}>
          <input
            className={inputClass}
            placeholder="10.4.7.0/24"
            value={prefix}
            onChange={(e) => setPrefix(e.target.value)}
          />
        </Field>
        <Field label={t("agentPush.shared.noteWhy")}>
          <input className={inputClass} value={note} onChange={(e) => setNote(e.target.value)} />
        </Field>
        <Field label={t("agentPush.authorizations.form.expiresOptional")}>
          <input
            className={inputClass}
            type="date"
            value={expiresAt}
            onChange={(e) => setExpiresAt(e.target.value)}
          />
          {/* The date this field holds is spelled out UNDER the group, below
              -- not here inside the `<Field>`. See the note there. */}
        </Field>
      </div>
      {/* THE DATE THE FORM IS ABOUT TO SEND, SPELLED THE WAY THIS CONSOLE
          SPELLS EVERY OTHER DATE.

          WHY THE ECHO EXISTS. The control is the platform's own date input on
          purpose -- it is the one field widget every keyboard, screen reader
          and locale already agrees on, and a hand-drawn calendar would be a
          worse control wearing a better skin. What it cannot do is say WHICH
          date it holds: the mask is drawn by the USER AGENT from ITS locale
          and no CSS reorders it, so a US-locale operator reads "11/27/2026"
          on a screen that writes "27 Nov 2026" everywhere else, and 11
          December and 27 November are both plausible readings of it. This is
          the console's answer to that, and design review M1 is every other
          date input in the product not having it yet.

          UNDER THE GROUP, NOT INSIDE THE FIELD, and that placement was
          bought with a failing test. `Field` wraps its control in the
          `<label>` (`components/ui.tsx`), so every text node inside one
          becomes part of the field's LABEL: with this sentence in the field,
          /changes/calendar's "From" input was labelled "FromFrom 01 Sept
          2026." and `change.test.tsx`'s `getByLabelText("From")` stopped
          finding it. That guard was right -- a description is not a name --
          and `Field` offers no slot for a description. The console's
          `aria-describedby` precedent (`health/HealthSettingsPanel`) puts the
          text outside the `<Field>` too, which inside a `.form-grid` means a
          cell of its own; under the whole group there is no cell to take and
          the line reads as one statement about the form. `.field .help`
          remains in the kit's CSS and remains the trap. */}
      {expiresAt !== "" ? (
        <p className="note" style={{ marginTop: 14 }}>
          {t("agentPush.authorizations.form.lapses", { date: formatDate(toInstant(`${expiresAt}T00:00`, zone), zone) })}
        </p>
      ) : null}

      {parsed !== null && (
        <div className="notice gold mt-3">
          <Icon name="warning" size={17} />
          {/* ONE CHILD: `.notice` is a flex row shaped icon-then-body, and
              stacked paragraphs handed to it directly lay out as columns. */}
          <div>
            <p>
              <strong>{describeReach(parsed)}</strong>
            </p>
            <p className="mt-1">{t("agentPush.authorizations.form.stillRequires")}</p>
            {expiresAt === "" && (
              <p className="mt-1">{t("agentPush.authorizations.form.noExpiry")}</p>
            )}
            {prefixSize(parsed) > 1024n && (
              <p className="mt-1">{t("agentPush.authorizations.form.wide")}</p>
            )}
          </div>
        </div>
      )}
      {prefix.trim() !== "" && parsed === null && (
        <p className="form-error mt-2">{t("agentPush.shared.notAPrefix")}</p>
      )}

      <div className="mt-3">
        <Button
          onClick={() =>
            create.mutate(
              () => ({
                // A prefix is one client's network (MSP clients phase 2,
                // Task 12): the grant authorizes pushes to that client's
                // machines only.
                // PHASE 3: the client/site selector. Until it exists that is
                // the organisation's internal client, whose id is the
                // organisation's -- `/me`'s `org_id` (`lib/internalClient.ts`).
                client_id: requireKnown(internalClientId(me.data)),
                prefix,
                note,
                // THE FIELD IS A WALL CLOCK; the API stores an instant.
                // `new Date("2026-08-25").toISOString()` reads it as UTC
                // midnight, so an operator at UTC-5 gets a grant that expires
                // five hours before the date they typed begins -- and east of
                // UTC the same arithmetic EXTENDS the window past the date on
                // the screen. On a grant that authorizes credentialed logins,
                // an unintended extension is the dangerous direction.
                expires_at: expiresAt === "" ? null : toInstant(`${expiresAt}T00:00`, zone),
              }),
              {
                onSuccess: () => {
                  setPrefix("");
                  setNote("");
                  setExpiresAt("");
                },
              },
            )
          }
          disabled={create.isPending || parsed === null}
        >
          {t("agentPush.authorizations.form.submit")}
        </Button>
      </div>

      {/* SEPARATION OF DUTIES, AT THE CONTROL. It was the tail of a 465-char
          page description; the fact is about who may press this button, so it
          is stated beside it. */}
      <p className="note mt-2">{t("agentPush.authorizations.form.separation")}</p>

      {create.isError && (
        <p role="alert" className="form-error mt-2">
          {describeSubmitError(create.error)}
        </p>
      )}
    </Panel>
    </div>
  );
}
