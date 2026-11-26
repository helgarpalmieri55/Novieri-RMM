import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import { Trans } from "../../i18n/Trans";
import { ConfirmDialog } from "../../components/confirm";
import { useStepUp } from "../../components/stepUp";
import { UnassignedRefusal } from "../../components/UnassignedRefusal";
import { attemptProblems, describeSubmitError, Form, FormActions } from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import { NoValue } from "../../components/emptystate";
import { Button, Dialog, Field, Table, Td, Th, inputClass } from "../../components/ui";
import type { Credential, Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { RelTime } from "../../components/reltime";
import { RowMenu } from "../../components/rowmenu";
import { useOrgZone } from "../../lib/zone";
import { ClientField } from "../../components/ClientField";
import { ForEveryClientToggle } from "../../components/forEveryClient";
import { useClientScope } from "../../lib/clientScope";
import { requireKnown, useNewRowClientField } from "../../lib/clientSelection";
import { useClientName } from "../settings/api";
import { PUSHABLE_CREDENTIAL_TYPES } from "../agent-push/types";
import { CredentialPrerequisites } from "./CredentialPrerequisites";
import { Icon } from "../../shell/icons";
import {
  CREDENTIAL_TYPES,
  credentialTypeDescription,
  credentialTypeLabel,
  credentialTypeNoun,
  windowsDomainRealm,
} from "./credentialTypes";
import {
  Btn,
  ClientFilter,
  FilterChips,
  ListEmpty,
  ListToolbar,
  PageHead,
  Panel,
  QueryState,
  ShowingCount,
  StatCard,
  StatsGrid,
  StatusPill,
  useNarrowedApi,
  useNewParam,
  useUnsavedGuard,
  useUrlFilters,
  withClient,
  writeQuery,
} from "../../shell/components";

// The design's "Credentials" screen (`P['credentials']` in
// docs/design/itms-prototype-v3.html): one bare panel of rows led by the key
// glyph, each carrying a vault-state pill, its usage and its rotation. The
// console keeps the tiles and the tab strip it already had over that list
// (the tiles are links into the tabs). "Rotation" is OMITTED rather than
// faked: `Credential` carries no rotation date and the API serves none.
//
// SECURITY INVARIANT #3, RESTATED FOR THIS FILE (docs/03-security-model.md
// "Credential vault"): a stored secret is decrypted only at the point of use,
// is never written to a log, and is never returned by an ordinary API
// response. The checkout secret below is the one deliberate exception -- the
// operator asked for it, gave a reason that is audited server-side, and sees
// it exactly once. It is held ONLY in local component state (`reveal`), and
// is cleared the moment the "Done" dialog closes. It does not stay in the
// react-query cache (which would keep it inspectable in devtools): the
// checkout mutation that brought it is reset as soon as it is in `reveal`,
// and its `gcTime: 0` drops it from the mutation cache then -- `reset()`
// alone only detaches it, and TanStack would keep it five more minutes. Nothing here calls `console.*` with it, and no
// list/table column renders `credential.secret` -- there is no such field on
// `Credential` (see `lib/api.ts`) because the API never sends the value back
// on a read.
//
// UX AUDIT, cycle 3: both forms on this route were the product-wide silent
// no-op -- `<Button disabled={mutation.isPending || !field}>` with no <form>,
// no reason rendered, and no live region. They now submit whatever is typed
// and explain what stops the request, via `components/form.tsx`.

// A reason is written verbatim into the immutable audit log (invariant #4), so
// "  " is not a reason -- `requiredProblems` trims before it decides.
// Six characters, the same bar /remote's start-session reason carries: enough
// to exclude the "x" typed to get past a required field, short enough to admit
// a real ticket reference ("INC-42"). Nothing here can verify the text; what
// it can refuse is a reason that says nothing to whoever reads the log later.
const MIN_REASON = 6;

// The rules are built when a form is submitted, not held as constants: their
// labels and messages are translated text, and a constant would keep the
// language it was first built in.
const checkoutRules = (): readonly FieldRule[] => [
  {
    name: "reason",
    label: translate("credentials.checkout.reason"),
    validate: (value) => (value.length < MIN_REASON ? translate("credentials.checkout.tooShort") : null),
  },
];

const createRules = (): readonly FieldRule[] => [
  { name: "name", label: translate("credentials.create.name") },
  { name: "secret", label: translate("credentials.create.secret") },
];

// A user principal name whose domain has a dot (review I1). The domain IS the
// Kerberos realm the push signs in to, and the KDC that sees the account's
// password proof is found for it. `DOMAIN\user` names a NetBIOS domain, not a
// realm; taking the realm from each target's DNS name instead let whoever
// wrote that name choose the KDC. The server refuses the same at save time.
const WINDOWS_ACCOUNT = /^[^\\@\s]+@[^\\@\s.]+(?:\.[^\\@\s.]+)+$/;

const windowsDomainRules = (): readonly FieldRule[] => [
  { name: "name", label: translate("credentials.create.name") },
  {
    name: "account",
    label: translate("credentials.create.account"),
    validate: (value) => (WINDOWS_ACCOUNT.test(value) ? null : translate("credentials.create.accountInvalid")),
  },
  { name: "secret", label: translate("credentials.create.password") },
];

/**
 * THE TILES NEEDED SOMEWHERE TO POINT, so the view they count became a URL.
 *
 * "Never checked out" is a real subset of this table — a stored privileged
 * credential nobody has ever needed — and until now it was a number with no
 * way to see the rows behind it. `state` is the param name /devices and
 * /remote already filter on; the two pages read the same key so a filtered
 * list is pasteable everywhere in the console.
 *
 * CHIPS NOW, ON THE SAME KEY (redesign Task 11, 6.9; Receta L, 5): the tab
 * strip that filtered the list is the kit's `FilterChips` over `?state=`, so
 * every link that opened a tab opens the same chip. "All" is the default and
 * writes nothing.
 */
const STATES = [
  { key: "", label: "credentials.tabs.all" },
  { key: "never-used", label: "credentials.tabs.neverUsed" },
  { key: "used", label: "credentials.tabs.used" },
] as const;

export function CredentialsPage() {
  const t = useT();
  const api = useApi();
  const queryClient = useQueryClient();
  // A CHECKOUT MAY NEED THE SECOND FACTOR AGAIN (own authentication design
  // §2.5): the server answers 403 `step_up_required` when it was not confirmed
  // in the last 10 minutes, and `run` asks for it and sends the checkout once
  // more. The secret still arrives only in the accepted answer.
  const stepUp = useStepUp();
  const [reveal, setReveal] = useState<{ name: string; secret: string } | null>(null);
  const [reasonFor, setReasonFor] = useState<Credential | null>(null);
  const [reason, setReason] = useState("");
  // Populated ON SUBMIT, never while typing -- a complaint about a field the
  // operator has not finished filling in is its own kind of unhelpful.
  const [reasonAttempt, setReasonAttempt] = useState<Attempt | null>(null);
  // The credential the operator has ASKED to delete, not the one being
  // deleted: nothing is sent until the confirmation below is answered.
  const [pendingDelete, setPendingDelete] = useState<Credential | null>(null);

  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const canManage = me.data?.permissions.includes("credentials.manage") ?? false;
  const zone = useOrgZone();
  // With every client chosen, each credential says whose it is.
  const { selected } = useClientScope();
  const clientName = useClientName();
  const showClient = selected === null;

  // THE CLIENT CHIP NARROWS THE READ, AND ONLY THE READ (decisions D6, D17):
  // the list reads through the narrowed API and is keyed by the client it was
  // narrowed to; checkout, delete and create keep `useApi()` and name the
  // row's own client, as before. A credential for every client still says so
  // in its row.
  const filters = useUrlFilters(["state", "client"] as const);
  const narrowed = useNarrowedApi(filters.values.client);
  const credentials = useQuery({
    queryKey: ["credentials", narrowed.client],
    queryFn: () => narrowed.api.listCredentials(),
    enabled: narrowed.ready,
  });

  const checkout = useMutation({
    // The row's client decides `X-Client-Id`: every client's goes without it.
    mutationFn: (payload: { credential: Credential; reason: string }) =>
      stepUp.run(() =>
        api.checkoutCredential(payload.credential.id, payload.reason, payload.credential.client_id),
      ),
    onSuccess: (data, payload) => {
      setReveal({ name: payload.credential.name, secret: data.secret });
      setReasonFor(null);
      setReason("");
      setReasonAttempt(null);
    },
    // Its answer is the secret: dropped from the mutation cache once reset.
    gcTime: 0,
  });

  function openCheckout(credential: Credential) {
    setReasonFor(credential);
    setReason("");
    // The dialog is keyed on `reasonFor`, so last attempt's complaint (or last
    // attempt's server failure) would otherwise greet the next credential.
    setReasonAttempt(null);
    checkout.reset();
  }

  function submitCheckout() {
    const attempt: Attempt = { rules: checkoutRules, values: { reason } };
    setReasonAttempt(attempt);
    if (attemptProblems(attempt).length > 0) {
      checkout.reset();
      return;
    }
    // Reset once the secret is in `reveal` (the hook's `onSuccess` above runs
    // first): the page's state is the one place it is held.
    if (reasonFor) {
      checkout.mutate({ credential: reasonFor, reason: reason.trim() }, { onSuccess: () => checkout.reset() });
    }
  }
  // AUDIT CLASS, /credentials → Delete: the mutation had NO `onError` and no
  // call site rendered `remove.error`. A refused delete -- a 403 from RBAC, a
  // bodyless 503 from a proxy -- therefore did nothing visible at all: the row
  // stayed exactly where it was, which is what a page that has not refetched
  // yet also looks like. Deleting a vault credential is destructive and
  // audited; "it is still there because the request failed" and "it is still
  // there because the table is stale" must not look the same.
  const remove = useMutation({
    // The row's client decides `X-Client-Id`: every client's goes without it.
    mutationFn: (credential: Credential) => api.deleteCredential(credential.id, credential.client_id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["credentials"] }),
  });

  // No data, pending or failed: the tiles read n/a and say nothing under them (ruling 10a).
  const noData = !credentials.data;
  const items = credentials.data ?? [];
  const neverUsed = items.filter((credential) => credential.last_used_at == null).length;
  const distinctTypes = new Set(items.map((credential) => credential.type)).size;

  const [params, setParams] = useSearchParams();
  const tab = filters.values.state;
  // `?new=<type>` opens "New credential" with that type chosen: the push
  // install screens link here when no push install credential exists
  // (`/credentials?new=push_install`, decision D23). An unknown type is
  // ignored rather than trusted into the form. The page's own button opens
  // it from `?new=credential`, with no type chosen.
  const created = useNewParam("credential");
  const requestedType = params.get("new");
  const newType = CREDENTIAL_TYPES.some((type) => type.value === requestedType) ? requestedType : null;
  // `?new=<type>` opens as on the base, whoever follows a push page's link:
  // the server checks `credentials.manage` on the save (invariant 6).
  // `?new=credential` is this wave's own link and gets the button's courtesy
  // check, as every other `?new=` dialog does (fix round 1, M1).
  const createOpen = (created.open && canManage) || newType != null;
  const closeCreate = () => {
    if (created.open) {
      created.close();
      return;
    }
    // A type the link chose: the key goes in place, as before.
    const next = new URLSearchParams(params);
    next.delete("new");
    writeQuery(params, next, setParams, { replace: true });
  };

  const visible = useMemo(
    () =>
      items.filter((credential) =>
        tab === "never-used"
          ? credential.last_used_at == null
          : tab === "used"
            ? credential.last_used_at != null
            : true,
      ),
    [items, tab],
  );
  // The counts of the whole answer (`GET /credentials` is not paged), never of the rows on screen.
  const stateCounts: Record<string, number | undefined> = credentials.data
    ? { "": undefined, "never-used": neverUsed, used: items.length - neverUsed }
    : {};
  // UNDER THE CLIENT CHIP A TILE COUNTS ONE CLIENT'S ROWS, AND OPENS THEM
  // (coordinator ruling 2): its link carries the chip.
  const chip = filters.values.client;
  const listOf = (state: string) => withClient(state ? `/credentials?state=${state}` : "/credentials", chip);
  const newCredential = canManage ? (
    <Btn label={t("credentials.page.newCredential")} icon="plus" variant="primary" onClick={created.start} />
  ) : undefined;

  return (
    <>
      <PageHead title={t("credentials.page.title")} description={t("credentials.page.description")} actions={newCredential} />

      <CredentialPrerequisites credentials={credentials.data} />

      {/* EVERY TILE OPENS THE ROWS IT COUNTED, via the state chips below — the
          filter is in the URL, so the tile hands over the exact view its
          number was computed from, the Client chip included.

          NOTHING HERE IS `urgent`. A vault at rest needs nobody: these are
          inventory figures, not a queue, and a saturated tile on a page with
          no work to do is the wall of numbers this row replaces. */}
      <StatsGrid columns={4}>
        {/* EVERY TILE CARRIES ITS SECOND LINE, as v3 draws them: each line is
            counted from the same rows as the figure above it. Before the list
            answers a tile is unmeasured ("n/a"), never a zero. */}
        <StatCard
          label={t("credentials.tiles.stored")}
          value={String(items.length)}
          pending={noData}
          icon="lock"
          to={listOf("")}
          tone="neutral"
          zeroLabel={t("credentials.tiles.storedZero")}
          foot={items.length > 0 ? t("credentials.tiles.storedFoot", { count: items.length - neverUsed }) : undefined}
        />
        <StatCard
          label={t("credentials.tiles.neverUsed")}
          value={String(neverUsed)}
          pending={noData}
          icon="clock"
          to={listOf("never-used")}
          tone="blue"
          /* "0 never checked out" on an empty vault and on a vault where every
             credential has been used are the same pixel otherwise. */
          zeroLabel={items.length === 0 ? t("credentials.tiles.neverUsedZeroEmpty") : t("credentials.tiles.neverUsedZeroUsed")}
          /* Not a second count: WHAT the figure means. A credential that has
             never been checked out has never been decrypted. */
          foot={neverUsed > 0 ? t("credentials.tiles.neverUsedFoot") : undefined}
        />
        {/* The fourth tile (redesign Task 11, 6.9): the credentials someone
            has needed, the other half of the one beside it, with its rows. */}
        <StatCard
          label={t("credentials.tiles.used")}
          value={String(items.length - neverUsed)}
          pending={noData}
          icon="eye"
          to={listOf("used")}
          tone="neutral"
          zeroLabel={t("credentials.tiles.usedZero")}
          foot={items.length - neverUsed > 0 ? t("credentials.tiles.usedFoot") : undefined}
        />
        {/* AUDIT [LOW]: "'Credential types 0' is misleading given the Type
            dropdown offers three". It counts what is stored, so it says so. */}
        <StatCard
          label={t("credentials.tiles.typesInUse")}
          value={String(distinctTypes)}
          pending={noData}
          icon="shield-lock"
          to={listOf("")}
          tone="blue"
          // Nothing under a tile without data (ruling 10a), not even this.
          foot={noData ? undefined : t("credentials.tiles.typesAvailable", { count: CREDENTIAL_TYPES.length })}
          zeroLabel={t("credentials.tiles.typesInUseZero")}
        />
      </StatsGrid>

      <Panel title={t("credentials.list.title")} subtitle={t("credentials.list.subtitle")} bare>
        <ListToolbar
          chips={
            <>
              <FilterChips
                label={t("credentials.list.stateLabel")}
                options={STATES.map((item) => ({ value: item.key, label: t(item.label), count: stateCounts[item.key] }))}
                value={tab}
                onChange={(value) => filters.set("state", value)}
              />
              <ClientFilter filters={filters} />
            </>
          }
          end={credentials.data ? <ShowingCount shown={visible.length} total={items.length} /> : null}
        />
        {/* `isPending`: under the chip the read waits for `/me`, and a
            waiting read is not an empty vault (general ruling 10). */}
        <QueryState loading={credentials.isPending} error={credentials.error} empty={false} emptyLabel="">
          {visible.length === 0 ? (
            <ListEmpty
              filtered={filters.active}
              onClear={filters.clear}
              nothingYet={t("credentials.list.emptyNone")}
              icon="lock"
              action={newCredential}
            />
          ) : (
            <Table label={t("credentials.list.title")}>
              <thead>
                <tr>
                  <Th>{t("credentials.list.name")}</Th>
                  {showClient ? <Th>{t("credentials.list.client")}</Th> : null}
                  <Th>{t("credentials.list.state")}</Th>
                  <Th>{t("credentials.list.lastUsed")}</Th>
                  <Th>{t("credentials.list.actions")}</Th>
                </tr>
              </thead>
              <tbody>
                {visible.map((credential) => (
                  <tr key={credential.id}>
                    {/* v3's row: the key glyph as the lead, the name over its
                        type. AUDIT [MEDIUM]: "windows_admin", "ssh_key",
                        "domain_join" -- unmapped database enum values with
                        underscores, rendered straight into the console. */}
                    <Td lead={<Icon name="key" size={18} />}>
                      <div className="cell-title">{credential.name}</div>
                      <div className="cell-sub">{credentialTypeLabel(credential.type)}</div>
                    </Td>
                    {showClient ? (
                      <Td>
                        {credential.client_id === null
                          ? t("lib.clients.everyClient")
                          : clientName(credential.client_id)}
                      </Td>
                    ) : null}
                    {/* The one vault fact the API states about a stored
                        credential: whether a secret is behind the reference. */}
                    <Td>
                      {credential.has_secret ? (
                        <StatusPill value="in-vault" kind="ok" label={t("credentials.list.inVault")} />
                      ) : (
                        <StatusPill value="no-secret" kind="warn" label={t("credentials.list.noSecret")} />
                      )}
                    </Td>
                    {/* `new Date(x).toLocaleString()` was one of the ~70 ad-hoc
                        call sites behind "the same record reports two
                        different times in two different formats". The zone is
                        the operator's own (`/me`.timezone), and a credential
                        never checked out says so rather than printing a word no
                        other absent date in the product uses. */}
                    <Td>
                      {credential.last_used_at ? (
                        <RelTime iso={credential.last_used_at} zone={zone} />
                      ) : (
                          <NoValue label={t("credentials.list.neverCheckedOut")} />
                      )}
                    </Td>
                    <Td>
                      <div style={{ display: "flex", gap: 6 }}>
                        <Btn
                          label={t("credentials.list.checkOut")}
                          icon="eye"
                          small
                          onClick={() => openCheckout(credential)}
                        />
                        {canManage && (
                          <RowMenu
                            subject={credential.name}
                            items={[
                              {
                                label: t("credentials.list.delete"),
                                destructive: true,
                                // Opens the confirmation; the vault secret is not recoverable.
                                onSelect: () => {
                                  remove.reset();
                                  setPendingDelete(credential);
                                },
                              },
                            ]}
                          />
                        )}
                      </div>
                    </Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          )}
        </QueryState>
      </Panel>

      {/* Names the credential, and the consequence -- what stops working when
          it is gone -- rather than asking "are you sure?". No `requireTyping`:
          the blast radius is one credential that can be stored again, and
          typing a name here would teach the copy-without-reading habit that
          makes the confirmations which DO need it worthless. */}
      <ConfirmDialog
        open={pendingDelete != null}
        title={t("credentials.delete.title")}
        subject={pendingDelete?.name ?? ""}
        body={
          pendingDelete
            ? t("credentials.delete.body", { type: credentialTypeNoun(pendingDelete.type) })
            : ""
        }
        confirmLabel={t("credentials.delete.confirm")}
        destructive
        onCancel={() => setPendingDelete(null)}
        onConfirm={() => {
          const credential = pendingDelete;
          setPendingDelete(null);
          if (credential) remove.mutate(credential);
        }}
      />

      {remove.isError && (
        <p role="alert" className="form-error" style={{ marginTop: 8 }}>
          {t("credentials.delete.failed")}{" "}
          {describeSubmitError(remove.error)}
        </p>
      )}

      <Panel title={t("credentials.protected.title")} subtitle={t("credentials.protected.subtitle")}>
        <div className="notice">
          <Icon name="lock" size={17} />
          <div>
            <strong>{t("credentials.protected.heading")}</strong>
            <br />
            {t("credentials.protected.reason")}
            {/* AUDIT [LOW], "Strong security copy, no way to act on it": "The
                excellent policy copy is also disconnected from any action the
                user can take." The action lives on each row, because that is
                where the subject is -- so this says which row, rather than
                growing a second checkout entry point that would have to ask
                WHICH credential all over again. */}
            <br />
            {items.length > 0
              ? t("credentials.protected.hintStored")
              : t("credentials.protected.hintEmpty")}
          </div>
        </div>
      </Panel>

      {/* KEYED on what opened it, so it starts empty however it opens (the
          button, a link with a type, Back or Forward), with that type chosen. */}
      <CreateCredentialDialog
        key={createOpen ? `open:${newType ?? ""}` : "closed"}
        open={createOpen}
        initialType={newType ?? undefined}
        onClose={closeCreate}
      />

      <Dialog
        open={reasonFor != null}
        onClose={() => setReasonFor(null)}
        title={t("credentials.checkout.title", { name: reasonFor?.name ?? "" })}
        dismissible={!stepUp.open}
      >
        <p className="cell-sub" style={{ marginBottom: 12 }}>
          {t("credentials.checkout.intro")}
        </p>
        <Form onSubmit={submitCheckout}>
          <Field label={t("credentials.checkout.reason")}>
            <input
              className={inputClass}
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              placeholder={t("credentials.checkout.reasonPlaceholder")}
            />
          </Field>
          <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", justifyContent: "flex-end", gap: 8 }}>
            {/* `type="button"` is load-bearing inside a real `<form>`: `ui.tsx`'s
                Button sets no type, and an untyped button submits — Cancel
                would check the credential out. */}
            <Button type="button" variant="ghost" onClick={() => setReasonFor(null)}>
              {t("credentials.checkout.cancel")}
            </Button>
            {/* "Reveal", not "Reveal secret": the kernel builds its in-flight
                label from this one ("Revealing…"), and the dialog title already
                names the credential being checked out.
                `String(checkout.error)` used to render here — it printed
                "Error: " prefixes and rendered NOTHING at all for the bodyless
                503 that `describeSubmitError` exists to catch. */}
            <FormActions
              label={t("credentials.checkout.reveal")}
              pending={checkout.isPending}
              error={checkout.error}
              problems={attemptProblems(reasonAttempt)}
            />
          </div>
        </Form>
      </Dialog>

      <Dialog open={reveal != null} onClose={() => setReveal(null)} title={reveal?.name ?? ""}>
        {/* THE DESIGN HAS A COMPONENT FOR THIS, and enrollment already uses it for
            the same sentence about the same kind of secret: the warn wash with
            the warn ink, which clears 4.5:1 where the bare warn colour did not. */}
        <div className="notice gold" role="status" style={{ marginBottom: 8 }}>
          <Icon name="alert-triangle" size={17} />
          <div>{t("credentials.reveal.shownOnce")}</div>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <code className="code" style={{ flex: 1 }}>
            {reveal?.secret}
          </code>
          <Btn
            label={t("credentials.reveal.copy")}
            icon="copy"
            onClick={() => {
              if (reveal) void navigator.clipboard?.writeText(reveal.secret);
            }}
          />
        </div>
        <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 12 }}>
          <Btn variant="primary" label={t("credentials.reveal.done")} onClick={() => setReveal(null)} />
        </div>
      </Dialog>
      {/* Outside every form: the recent confirmation holds forms of its own. */}
      {stepUp.dialog}
    </>
  );
}

function CreateCredentialDialog({
  open,
  onClose,
  initialType,
}: {
  open: boolean;
  onClose: () => void;
  initialType?: string;
}) {
  const t = useT();
  const api = useApi();
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [type, setType] = useState(initialType ?? "windows_admin");
  const [secret, setSecret] = useState("");
  // A Windows domain account's other parts (design §2.5). The vault holds one
  // secret per credential, so they travel inside it as the JSON blob the push
  // dispatch parses; none of them is ever read back by this console.
  const [account, setAccount] = useState("");
  const [kdc, setKdc] = useState("");
  const [caPem, setCaPem] = useState("");
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  const isWindowsDomain = type === "windows_domain";
  // A CREDENTIAL GOES TO A CLIENT (design §3.1): the one chosen at the top,
  // or with every client the Client field's. A PUSH CREDENTIAL HAS EXACTLY
  // ONE (MSP clients phase 2, Task 12): every push made with it is made in
  // that client. Any other type MAY instead be every client's, ticked "For
  // every client" by someone holding the code on every client; it is then
  // sent with no client, and without `X-Client-Id` (`api.createCredential`).
  // Before the final review those types were always every client's, which a
  // chosen client's header made a 403 (phase 3 final review C, Important 2).
  const pushable = PUSHABLE_CREDENTIAL_TYPES.includes(type);
  const client = useNewRowClientField("credentials.manage");
  const [forEveryClient, setForEveryClient] = useState(false);
  const everyClient = !pushable && forEveryClient;
  const credentialClient = everyClient ? null : client.clientId;
  const asksClient = !everyClient && client.needsField;

  const create = useMutation({
    // `name` is trimmed on the way out; `secret` DELIBERATELY IS NOT. Trailing
    // whitespace in a vault secret is a character of the credential, and eating
    // it silently stores something that will not authenticate — an
    // unreproducible remote-control failure weeks later. `requiredProblems`
    // trims only to decide emptiness; it never rewrites the value it passes.
    //
    // A `windows_domain` secret is the JSON `{"username", "secret", "kdc",
    // "ca_bundle"}`; `JSON.stringify` drops the optional parts left blank, so
    // an absent KDC means SRV discovery rather than a KDC named "".
    mutationFn: () =>
      api.createCredential({
        name: name.trim(),
        type,
        ...(everyClient ? {} : { client_id: requireKnown(credentialClient) }),
        secret: isWindowsDomain
          ? JSON.stringify({
              username: account.trim(),
              secret,
              kdc: kdc.trim() || undefined,
              ca_bundle: caPem.trim() ? caPem : undefined,
            })
          : secret,
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["credentials"] });
      setName("");
      setSecret("");
      setAccount("");
      setKdc("");
      setCaPem("");
      setAttempt(null);
      // Closing on success is NOT the vanishing-dialog defect the audit filed
      // against job dispatch: the new profile appears in the table this dialog
      // sits over, so the success is visible where the operator lands. A failed
      // store keeps the dialog open, with the reason.
      // Stored: closing goes Back from the dialog's entry, and must not ask.
      release();
      onClose();
    },
  });

  // THE PARENT REMOUNTS THIS DIALOG EACH TIME IT OPENS (it is keyed on what
  // opened it), so the last attempt's "Name is required", its server failure
  // and its "For every client" never greet the next opening (coordinator
  // ruling 4).
  //
  // THE PAGE'S ONE GUARD (Receta FO, 4; ruling 3): the form lives in its
  // `?new=` dialog, so Back or Close with something typed asks first.
  const dirty = open && (name !== "" || secret !== "" || account !== "" || kdc !== "" || caPem !== "");
  const { guard, release } = useUnsavedGuard(dirty, { search: ["new"] });

  function storeCredential() {
    // AUDIT [HIGH], /credentials → New credential → Store: "Store with every
    // field empty is a silent no-op". The control read
    // `disabled={create.isPending || !name || !secret}`, so the click moved
    // nothing and named no field. It is enabled now and the click produces the
    // reason instead.
    //
    // HELD AS RULES + VALUES, not as finished sentences, so a language switch
    // rewords them. The secret is reduced to whether it is blank: the rules
    // only test for presence, and the text itself has no business in a second
    // piece of state.
    const values = { name, account, secret: secret.trim() === "" ? "" : "x", client: credentialClient };
    const next: Attempt = {
      rules: () => [
        ...(isWindowsDomain ? windowsDomainRules : createRules)(),
        ...(asksClient ? [{ name: "client", label: translate("credentials.create.client") }] : []),
      ],
      values,
    };
    setAttempt(next);
    if (attemptProblems(next).length > 0) {
      // A stale server failure sitting beside a fresh complaint reads as though
      // this attempt had already been sent and refused.
      create.reset();
      return;
    }
    create.mutate();
  }

  const typeDescription = credentialTypeDescription(type);
  const realm = windowsDomainRealm(account);

  return (
    <>
      <Dialog open={open} onClose={onClose} title={t("credentials.create.title")}>
        <Form onSubmit={storeCredential}>
          {/* AUDIT [HIGH], /credentials → New credential: "The Secret input is
              `type="password"` with `autocomplete: null` ... they may autofill
              the operator's *own* saved password into the vault's Secret box,
              and -- more damaging -- offer to SAVE the privileged credential
              into the operator's personal password store on submit. That
              silently copies a domain-admin or SSH key out of the audited vault
              into an unmanaged browser profile."

              Auditor A sharpened it from a third route: /email-intake's IMAP
              password already sets `autocomplete="new-password"`, so "the team
              knows and applied it inconsistently" -- the LEAST sensitive of the
              three secrets was protected and the most sensitive was not.

              `new-password` on the secret (the value that must never be offered
              for saving), `off` on the name beside it (a username-shaped field
              is half of what makes a manager treat this as a login form), and
              the two manager-specific opt-outs, which is the rest of the fix
              line. Invariant #3 is about logs and API responses; a password
              manager writing the value into an unmanaged profile is the same
              disclosure through a third door. */}
          <div className="form-grid">
            <Field label={t("credentials.create.name")}>
              <input
                className={inputClass}
                value={name}
                onChange={(event) => setName(event.target.value)}
                autoComplete="off"
                data-1p-ignore
                data-lpignore="true"
              />
            </Field>
            <Field label={t("credentials.create.type")}>
              <select
                className={inputClass}
                value={type}
                onChange={(event) => setType(event.target.value)}
              >
                {CREDENTIAL_TYPES.map((option) => (
                  <option key={option.value} value={option.value}>
                    {credentialTypeLabel(option.value)}
                  </option>
                ))}
              </select>
            </Field>
            {!pushable ? (
              <ForEveryClientToggle
                code="credentials.manage"
                hint={t("credentials.create.forEveryClientHint")}
                checked={forEveryClient}
                onChange={setForEveryClient}
              />
            ) : null}
            {asksClient ? <ClientField field={client} /> : null}
            {/* The half of the enum finding the auditor called the one that
                matters: "an operator choosing between 'ssh_key' and 'domain_join'
                gets no explanation of what each type implies for how the secret is
                used or where it can be checked out." */}
            {typeDescription ? (
              <p className="form-hint" role="status">
                {typeDescription}
              </p>
            ) : null}
            {isWindowsDomain ? (
              <>
                {/* THE STANDING WARNING (design §2.3 "What remains"). The site
                    server holds this password in memory for the length of a
                    push; nothing here can check what the account may do in the
                    directory, so the console says it where the choice is made. */}
                <div className="notice gold" role="note">
                  <Icon name="alert-triangle" size={17} />
                  <div>
                    <Trans k="credentials.create.domainAdminWarning" components={{ strong: <strong /> }} />
                  </div>
                </div>
                <Field
                  label={t("credentials.create.accountField")}
                  full
                  help={
                    realm
                      ? t("credentials.create.realmHelp", { realm })
                      : t("credentials.create.accountHelp")
                  }
                >
                  <input
                    className={inputClass}
                    value={account}
                    onChange={(event) => setAccount(event.target.value)}
                    autoComplete="off"
                    spellCheck={false}
                    data-1p-ignore
                    data-lpignore="true"
                  />
                </Field>
                <Field label={t("credentials.create.password")} full>
                  <input
                    className={inputClass}
                    type="password"
                    value={secret}
                    onChange={(event) => setSecret(event.target.value)}
                    autoComplete="new-password"
                    spellCheck={false}
                    data-1p-ignore
                    data-lpignore="true"
                  />
                </Field>
                <Field label={t("credentials.create.controller")} full>
                  <input
                    className={inputClass}
                    value={kdc}
                    onChange={(event) => setKdc(event.target.value)}
                    autoComplete="off"
                    spellCheck={false}
                    placeholder={t("credentials.create.controllerPlaceholder")}
                  />
                </Field>
                {/* Public material, so a textarea and never a password box; it
                    travels inside the vault blob only so it stays with the
                    credential it belongs to (§I3/W9). */}
                <Field label={t("credentials.create.caCertificate")} full>
                  <textarea
                    className={inputClass}
                    value={caPem}
                    onChange={(event) => setCaPem(event.target.value)}
                    autoComplete="off"
                    spellCheck={false}
                    rows={4}
                  />
                </Field>
              </>
            ) : (
              <Field label={t("credentials.create.secret")} full>
                <input
                  className={inputClass}
                  type="password"
                  value={secret}
                  onChange={(event) => setSecret(event.target.value)}
                  autoComplete="new-password"
                  spellCheck={false}
                  data-1p-ignore
                  data-lpignore="true"
                />
              </Field>
            )}
          </div>
          <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", justifyContent: "flex-end", gap: 8 }}>
            <Button type="button" variant="ghost" onClick={onClose}>
              {t("credentials.create.cancel")}
            </Button>
            {/* No `saved` — this dialog closes on the server's 201 and the row
                appears behind it. The alert here is the failure path, which
                `String(create.error)` rendered as an empty string for the
                bodyless 503. */}
            {/* With Unassigned chosen at the top, nothing can be filed for a
                client there: the dialog says so instead of offering a Store
                that can only fail. Every client's is no client's, so ticking
                "For every client" (where offered) still stores one. */}
            {client.refused === "unassigned" && !everyClient ? (
              <UnassignedRefusal />
            ) : (
              <FormActions
                label={t("credentials.create.store")}
                pending={create.isPending}
                error={create.error}
                problems={attemptProblems(attempt)}
              />
            )}
          </div>
        </Form>
      </Dialog>
      {/* After the dialogs: the leave question shares their `.modal` layer, so it must come later in the DOM to be drawn on top (ruling 3a). */}
      {guard}
    </>
  );
}
