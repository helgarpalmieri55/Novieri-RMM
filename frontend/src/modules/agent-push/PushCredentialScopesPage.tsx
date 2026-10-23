import { useState } from "react";
import { NoValue } from "../../components/emptystate";
import { useT } from "../../i18n/context";
import { Trans } from "../../i18n/Trans";
import { describeSubmitError } from "../../components/form";
import { Button, Field, Td, Th, inputClass } from "../../components/ui";
import { FilterBar, PageHead, Panel, QueryState, StatusPill, TableWrap } from "../../shell/components";
import { formatDate } from "../../lib/datetime";
import { useOrgZone } from "../../lib/zone";
import { Icon } from "../../shell/icons";
import {
  useBindPushCredentialScopeMutation,
  usePushCredentials,
  usePushCredentialScopes,
  usePushPermissions,
  useUnbindPushCredentialScopeMutation,
} from "./api";
import { PushHazardNotice, PushNav } from "./PushNav";
import { PushReadiness } from "./PushReadiness";
import { credentialTypeLabel } from "../credentials/credentialTypes";
import { describeReach, formatCount, parsePrefix } from "./pushState";
import { PUSH_CREDENTIAL_TYPE, type PushCredentialScopeOut } from "./types";

// **THE MODULE'S SECOND CONTROL SURFACE, AND THE ONE THAT ANSWERS "HOW DOES
// ONE CREDENTIAL NOT GET SPRAYED AT THE WHOLE ESTATE".**
//
// The answer is that the credential itself carries where it may be presented.
// `credentials.device_group_id` cannot express that: it scopes a credential to
// DEVICES, and a push target is not yet a managed device and may be in no
// group at all.
//
// TWO THINGS THIS PAGE SAYS THAT NO OTHER SCREEN CAN:
//
//   1. THE CREDENTIAL IS DEDICATED. Only a credential of type "Push install"
//      may be bound here, and one that is bound here may not be used by remote
//      control. The picker offers only those, so the rule is visible before it
//      is enforced rather than arriving as an error.
//   2. IT SHOULD NOT BE A DOMAIN ADMIN. This platform cannot check what rights
//      an account holds in somebody else's directory, and it can refuse to
//      pretend the choice does not matter. SCCM's client-push account is a
//      documented lateral-movement enabler precisely because everybody made it
//      a domain admin and pointed it at every subnet.
//
// This page renders `agent.push.scope.manage` controls only when the caller
// appears to hold the code. That is cosmetic: the server refuses regardless.
//
// v3 draws this as one list: the key glyph, the credential's name, the prefix
// it may be presented in, its state. The bind form the design opens from its
// "New scope" button is inline below the list here.
export function PushCredentialScopesPage() {
  const t = useT();
  const permissions = usePushPermissions();
  const [includeUnbound, setIncludeUnbound] = useState(false);
  const scopes = usePushCredentialScopes(includeUnbound);
  const unbind = useUnbindPushCredentialScopeMutation();

  return (
    <div>
      {/* UNDER THE HEAD, not above it: see the note in
          `PushAuthorizationsPage`. Crumb, title, description, then the strip
          that switches what follows, as every other tabbed page does. */}
      {/* THE PERMISSION IS NAMED IN WORDS FIRST, code in parentheses after it
          -- the console's own convention (see /devices' "the fleet read
          (devices.read)"). A bare `agent.push.scope.manage` dropped into a
          sentence reads as prose to nobody and is a role name to an admin;
          both readers need the code, neither needs it unannounced. */}
      <PageHead
        title={t("agentPush.scopes.title")}
        description={t("agentPush.scopes.description")}
      />

      <PushNav />

      <PushHazardNotice />

      <PushReadiness />

      <Panel bare>
        <FilterBar>
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={includeUnbound}
              onChange={(e) => setIncludeUnbound(e.target.checked)}
            />
            <span>{t("agentPush.scopes.showUnbound")}</span>
          </label>
          <span className="filter-spacer" />
          <span className="note mono">
            {scopes.data ? t("agentPush.scopes.count", { count: scopes.data.length }) : "…"}
          </span>
        </FilterBar>
        <QueryState
          loading={scopes.isLoading}
          error={scopes.error}
          empty={(scopes.data ?? []).length === 0}
          emptyLabel={t("agentPush.scopes.empty")}
          emptyIcon="lock"
        >
          <TableWrap>
            <table>
              <thead>
                <tr>
                  <th scope="col">{t("agentPush.shared.credential")}</th>
                  <th scope="col">{t("agentPush.scopes.mayBePresentedIn")}</th>
                  <th scope="col">{t("agentPush.shared.status")}</th>
                  <th scope="col">{t("agentPush.scopes.bound")}</th>
                  <th scope="col">{t("agentPush.shared.note")}</th>
                  {/* `Th` with no children renders an `sr-only` name --
                      "Actions" by default -- rather than an empty header cell
                      a screen reader announces as nothing. */}
                  {permissions.scopeManage && <Th />}
                </tr>
              </thead>
              <tbody>
                {scopes.data?.map((scope) => (
                  <ScopeRow
                    key={scope.id}
                    scope={scope}
                    canManage={permissions.scopeManage}
                    onUnbind={() => unbind.mutate(scope.id)}
                  />
                ))}
              </tbody>
            </table>
          </TableWrap>
        </QueryState>
        {unbind.isError && (
          <p role="alert" className="form-error" style={{ margin: "12px 18px" }}>
            {describeSubmitError(unbind.error)}
          </p>
        )}
      </Panel>

      {permissions.scopeManage && <BindForm />}
    </div>
  );
}

function ScopeRow({
  scope,
  canManage,
  onUnbind,
}: {
  scope: PushCredentialScopeOut;
  canManage: boolean;
  onUnbind: () => void;
}) {
  const t = useT();
  const bound = scope.unbound_at === null;
  const zone = useOrgZone();
  return (
    <tr>
      {/* THE NAME, NEVER THE SECRET. Nothing in this row's contract can hold
          one -- the server's response model has no such field. */}
      {/* NEITHER HALF OF THIS CELL IS BROKEN ACROSS LINES. At 390px this
          table is six columns inside the design's scrolling `.table-wrap`, so
          the first one lands at about ninety pixels: the name wrapped to four
          lines and the reference to three ("cred-", "push-", "b4"). An
          identifier a person carries to the vault to compare, rendered as
          three fragments, is no longer an identifier. The table scrolls -- it
          already does at this width, which is the design's own answer -- and
          the page does not. */}
      <Td lead={<Icon name="key" size={18} />}>
        <div className="cell-title whitespace-nowrap">
          {scope.credential_name || t("agentPush.scopes.deleted")}
        </div>
        <div className="cell-sub mono whitespace-nowrap">{scope.credential_id}</div>
      </Td>
      <Td>
        <div className="cell-title mono">{scope.prefix}</div>
        <div className="cell-sub">
          {t("agentPush.shared.addresses", { count: scope.address_count, formatted: formatCount(scope.address_count) })}
        </div>
      </Td>
      <Td>
        {bound ? (
          <span title={t("agentPush.scopes.boundMeaning")}>
            <StatusPill value="bound" kind="ok" label={t("agentPush.scopes.boundPill")} />
          </span>
        ) : (
          <span title={t("agentPush.scopes.unboundMeaning")}>
            <StatusPill value="unbound" kind="mut" label={t("agentPush.scopes.unboundPill")} />
          </span>
        )}
      </Td>
      <Td>
        <div>{formatDate(scope.bound_at, zone)}</div>
        <div className="cell-sub">
          <Trans k="agentPush.scopes.boundBy" vars={{ by: scope.bound_by }} components={{ by: <span className="mono" /> }} />
        </div>
      </Td>
      <Td>{scope.note || <NoValue label={t("agentPush.shared.noNote")} />}</Td>
      {canManage && (
        <Td>
          {bound ? (
            <Button variant="outline" onClick={onUnbind}>
              {t("agentPush.scopes.unbind")}
            </Button>
          ) : (
            <NoValue label={t("agentPush.scopes.alreadyUnbound")} />
          )}
        </Td>
      )}
    </tr>
  );
}

function BindForm() {
  const t = useT();
  const bind = useBindPushCredentialScopeMutation();
  const credentials = usePushCredentials();
  const [credentialId, setCredentialId] = useState("");
  const [prefix, setPrefix] = useState("");
  const [note, setNote] = useState("");

  const parsed = parsePrefix(prefix);
  const available = credentials.data ?? [];

  return (
    <Panel
      title={t("agentPush.scopes.form.title")}
      subtitle={t("agentPush.scopes.form.subtitle")}
    >
      <div className="notice gold">
        <Icon name="warning" size={17} />
        <div>
          <p>
            <strong>{t("agentPush.scopes.form.dedicatedHeadline")}</strong>
          </p>
          <p className="mt-1">{t("agentPush.scopes.form.dedicatedBody")}</p>
        </div>
      </div>

      <div className="form-grid mt-3">
        <Field label={t("agentPush.shared.credential")}>
          {/* `.select-field`, WHICH IS THE KIT'S CLASS FOR A SELECT.
              `inputClass` is `.input-field`, the class ~300 text inputs
              carry; the two take the same box, but a select is not a text
              input and the console's other pickers say so. The design draws a
              select as `.inp.sel` with a Tabler chevron; the kit draws that
              chevron on every `<select>` now (`select.input-field,
              select.select-field` in `components.css`), so the control keeps
              the platform's picker and wears the design's glyph. */}
          <select
            className="select-field"
            value={credentialId}
            onChange={(e) => setCredentialId(e.target.value)}
          >
            <option value="">{t("agentPush.scopes.form.choose")}</option>
            {/* A Windows domain account is named as one, so an operator binding
                a range can tell which transport the secret goes out on. The
                type is a wire value and reaches the screen only through
                `credentialTypeLabel`. */}
            {available.map((credential) => (
              <option key={credential.id} value={credential.id}>
                {credential.type === PUSH_CREDENTIAL_TYPE
                  ? credential.name
                  : `${credential.name} · ${credentialTypeLabel(credential.type)}`}
              </option>
            ))}
          </select>
        </Field>
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
      </div>

      {/* AN EMPTY PICKER IS EXPLAINED, NOT LEFT BLANK. "There are no push
          credentials yet" and "you cannot read credentials" are different
          facts with different remedies, and a bare empty dropdown says
          neither. */}
      {!credentials.isLoading && available.length === 0 && (
        <p className="note mt-2">
          {credentials.isError
            ? t("agentPush.scopes.form.cannotView")
            : t("agentPush.scopes.form.noneYet")}
        </p>
      )}

      {parsed !== null && (
        <div className="notice gold mt-3">
          <Icon name="warning" size={17} />
          <div>
            <p>
              <strong>{describeReach(parsed)}</strong>
            </p>
            <p className="mt-1">{t("agentPush.scopes.form.doesNotAuthorize")}</p>
          </div>
        </div>
      )}
      {prefix.trim() !== "" && parsed === null && (
        <p className="form-error mt-2">{t("agentPush.shared.notAPrefix")}</p>
      )}

      <div className="mt-3">
        <Button
          onClick={() =>
            bind.mutate(
              { credential_id: credentialId, prefix, note },
              {
                onSuccess: () => {
                  setPrefix("");
                  setNote("");
                },
              },
            )
          }
          disabled={bind.isPending || parsed === null || credentialId === ""}
        >
          {t("agentPush.scopes.form.submit")}
        </Button>
      </div>

      {/* SEPARATION OF DUTIES, AT THE CONTROL, as on the authorizations page.
          It was the tail of a 391-char page description. */}
      <p className="note mt-2">{t("agentPush.scopes.form.separation")}</p>

      {bind.isError && (
        <p role="alert" className="form-error mt-2">
          {describeSubmitError(bind.error)}
        </p>
      )}
    </Panel>
  );
}
