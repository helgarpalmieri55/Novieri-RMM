import { useState } from "react";
import { RowMenu } from "../../components/rowmenu";
import { ConfirmDialog } from "../../components/confirm";
import { attemptProblems, describeSubmitError, Form, FormActions } from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import { Button, Dialog, Field, inputClass, Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import { ApiError } from "../../lib/api";
import { Icon } from "../../shell/icons";
import { Btn, Panel, StatusPill, Toggle } from "../../shell/components";
import {
  useCreateNotificationChannelMutation,
  useDeleteNotificationChannelMutation,
  useNotificationChannels,
  useNotificationRules,
  useTestNotificationChannelMutation,
  useUpdateNotificationChannelMutation,
} from "./api";
import {
  EMPTY_SIGN_IN,
  EmailSignInFields,
  MICROSOFT_SMTP,
  emailAuthOf,
  signInProblemKind,
  signInProblemText,
  signInRequest,
} from "./EmailSignInFields";
import type { EmailSignIn, SignInProblemKind } from "./EmailSignInFields";
import { channelKindLabel, deliveryErrorMessage } from "./notificationState";
import type { ChannelKind, ChannelOut } from "./types";

// Spec §11: "Channels -- list, create/edit (webhook or email), test-send
// with an inline result, active toggle." Reads are `notification.read`-gated
// server-side; every write (including test-send, spec §5) is
// `notification.admin`-gated. `canAdmin` here is presentation only -- the
// server rejects an unauthorized call regardless (invariant #6).

// Auditor (HIGH): "Webhook channels accept any string as a URL -- no
// validation, and the field is not even type=url". NOTHING on the server
// catches this either: `_validate_config` only checks that `url` is non-empty
// (schema.py), and the scheme/host/allowlist decisions all happen at DELIVERY
// time (delivery.py's `scheme_not_allowed`/`host_not_allowed`). So "chat.lan"
// saved cleanly and then dropped every notification routed through it, hours
// later, in a deliveries table nobody was watching. The check belongs where
// the person typing it can still fix it.
//
// `new URL` rather than a regex: it is the same parser the browser and the
// server's `urlparse` agree on, and a regex approximating it is how "valid
// here, rejected there" gets shipped.
function invalidWebhookUrl(value: string): string | null {
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    return translate("notification.channels.urlShape", { value });
  }
  if (parsed.protocol !== "https:" && parsed.protocol !== "http:") {
    return translate("notification.channels.urlScheme", { scheme: parsed.protocol });
  }
  if (!parsed.hostname) {
    return translate("notification.channels.urlHost", { value });
  }
  return null;
}

// The same class of defect one field down, and the reason it is fixed in the
// same pass: an SMTP `from_address` of "ops" is accepted by this dialog, then
// rejected by the relay at send time with the same invisible outcome.
const EMAIL_SHAPE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

function invalidFromAddress(value: string): string | null {
  if (!EMAIL_SHAPE.test(value)) {
    return translate("notification.channels.fromShape", { value });
  }
  return null;
}

// `Number(port)` below turns anything non-numeric into `NaN`, which
// `JSON.stringify` writes as `null` -- so a typo here reached the API as a
// missing port and came back as a raw 422. Bounds match `_validate_config`'s
// (1..65535) so the sentence the user reads is the rule the server enforces.
function invalidSmtpPort(value: string): string | null {
  if (!/^\d+$/.test(value)) {
    return translate("notification.channels.portShape", { value });
  }
  const port = Number(value);
  if (port < 1 || port > 65535) {
    return translate("notification.channels.portRange", { port });
  }
  return null;
}

// Auditor (MEDIUM): the Add channel modal is "the same shared component -- same
// four gaps as /deploy (not a <form>, no required on any control)". The four
// answers are one list per kind, plus `<Form>` and `<FormActions>` below.
// `label` is the words ON SCREEN, so "URL is required" reads back to the field
// the user is looking at.
//
// Factories of rule lists, so the words are looked up when a refusal is
// rendered, not when the module loads. What they close over is a value (the
// kind chosen at the time of the press), not live state. The sign-in check
// travels as the CODE of what is missing, never the secrets it was worked out
// from: "ok" when nothing is missing.
const webhookRules = (): readonly FieldRule[] => [
  { name: "name", label: translate("notification.channels.name") },
  { name: "url", label: translate("notification.channels.url"), validate: invalidWebhookUrl },
];

const emailRules = (): readonly FieldRule[] => [
  { name: "name", label: translate("notification.channels.name") },
  { name: "host", label: translate("notification.channels.smtpHost") },
  { name: "port", label: translate("notification.channels.smtpPort"), validate: invalidSmtpPort },
  { name: "fromAddress", label: translate("notification.channels.fromAddress"), validate: invalidFromAddress },
];

// Microsoft 365 fixes the server and port, so neither is asked for.
const microsoftRules = (): readonly FieldRule[] => [
  { name: "name", label: translate("notification.channels.name") },
  { name: "fromAddress", label: translate("notification.channels.fromAddress"), validate: invalidFromAddress },
];

const signInRule = (): FieldRule => ({
  name: "signIn",
  label: translate("notification.signIn.caption"),
  validate: (value) => (value === "ok" ? null : signInProblemText(value as SignInProblemKind)),
});

export function ChannelsPanel({ canAdmin }: { canAdmin: boolean }) {
  const t = useT();
  const channels = useNotificationChannels();
  const create = useCreateNotificationChannelMutation();
  const update = useUpdateNotificationChannelMutation();
  const test = useTestNotificationChannelMutation();
  // Auditor A (HIGH), "Notification channels cannot be deleted -- only
  // disabled": "The only row actions are Disable and Test. There is no
  // Delete ... A channel created by mistake -- such as the invalid
  // audit-a-test one this audit just created -- is permanent." The cleanup
  // note is the sharpest part of that finding: the auditor could not remove
  // their own test channel and had to ask for it to be deleted server-side.
  //
  // `DELETE /notifications/channels/{id}` HAS EXISTED THE WHOLE TIME
  // (api.py's `delete_channel_route`, `notification.admin`-gated, audited as
  // `notification.channel_deleted`) and `useDeleteNotificationChannelMutation`
  // was already defined and wired to nothing. Only the control was missing.
  const del = useDeleteNotificationChannelMutation();
  // Read-only, and only to WARN: the audit's fix asks for "a warning if rules
  // reference the channel". This is the same query key RulesPanel uses, so it
  // is served from cache rather than fetched twice.
  const rules = useNotificationRules();

  // `null` closed, `{channel: null}` create, `{channel}` edit. Auditor A's
  // HIGH finding asked for BOTH row actions -- "Add Edit and Delete row
  // actions" -- and named the cost of the missing one exactly: "Because the
  // URL cannot be edited either, an operator who mistypes a webhook URL must
  // abandon a permanently-listed dead channel and create a second one."
  // `PATCH /notifications/channels/{id}` has existed the whole time.
  const [dialog, setDialog] = useState<{ channel: ChannelOut | null } | null>(null);
  const creating = dialog !== null;
  const editingChannel = dialog?.channel ?? null;
  const [kind, setKind] = useState<ChannelKind>("webhook");
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [host, setHost] = useState("");
  const [port, setPort] = useState("587");
  const [fromAddress, setFromAddress] = useState("");
  const [useTls, setUseTls] = useState(true);
  const [secret, setSecret] = useState("");
  const [signIn, setSignIn] = useState<EmailSignIn>(EMPTY_SIGN_IN);
  // Filled ON SUBMIT, never while typing: "Name is required" appearing before
  // the first keystroke lands is its own kind of unhelpful. Held as the rules and
  // the values they were checked against (secrets only as the code of what is
  // missing), worded at render.
  const [attempt, setAttempt] = useState<Attempt | null>(null);

  const [testedChannelId, setTestedChannelId] = useState<string | null>(null);
  // The row awaiting confirmation. Holding the CHANNEL, not its id, is what
  // lets the dialog name the thing being deleted (components/confirm.tsx: the
  // subject "is what lets somebody notice they have the wrong row selected").
  const [pendingDelete, setPendingDelete] = useState<ChannelOut | null>(null);

  // Degrade quietly on 403 -- a notification.read holder still gets the
  // panel, just without the list (mirrors TicketDetailPage's
  // DeviceSummaryPanel precedent: never an error banner, never a broken
  // page for a permission the caller simply lacks).
  const forbidden = channels.error instanceof ApiError && channels.error.status === 403;
  // Distinguishes a genuine failure (a 500, a network error) from BOTH
  // "still loading" and "insufficient permissions" -- without this, any
  // non-403 error rendered nothing at all, reading identically to a still-
  // loading table (UI port spec §6/§7).
  const failed = channels.error != null && !forbidden;

  function resetForm() {
    setKind("webhook");
    setName("");
    setUrl("");
    setHost("");
    setPort("587");
    setFromAddress("");
    setUseTls(true);
    setSecret("");
    setSignIn(EMPTY_SIGN_IN);
    setAttempt(null);
  }

  // Mutation state OUTLIVES the dialog: without `create.reset()` a failed Save
  // leaves `create.error` set, and the next open announces the refusal of a
  // request nobody has made yet.
  function openCreate() {
    resetForm();
    create.reset();
    update.reset();
    setDialog({ channel: null });
  }

  // `kind` IS NOT EDITABLE and the dialog says so rather than offering a
  // control the server ignores: `ChannelUpdate` has no `kind` field and
  // `_CHANNEL_EDITABLE_FIELDS` omits it (channels.py).
  //
  // THE SECRET IS NEVER SEEDED, and cannot be: `ChannelOut` has no field that
  // could carry one (invariant #3). Blank means "leave the stored one alone"
  // -- the key is omitted from the PATCH entirely rather than sent as "" --
  // which is the same convention email-intake's mailbox form already uses.
  function openEdit(channel: ChannelOut) {
    resetForm();
    create.reset();
    update.reset();
    setKind(channel.kind);
    setName(channel.name);
    if (channel.kind === "webhook") {
      setUrl(String(channel.config.url ?? ""));
    } else {
      setHost(String(channel.config.host ?? ""));
      setPort(String(channel.config.port ?? 587));
      setFromAddress(String(channel.config.from_address ?? ""));
      setUseTls(channel.config.use_tls !== false);
      setSignIn({ ...EMPTY_SIGN_IN, auth: emailAuthOf(channel.config) });
    }
    setDialog({ channel });
  }

  const microsoft = kind === "email" && signIn.auth === "microsoft";
  // A saved sign-in of the kind now chosen: blank then means "keep it".
  const signInStored =
    editingChannel !== null &&
    editingChannel.has_credential &&
    emailAuthOf(editingChannel.config) === signIn.auth;

  function submit() {
    const base = kind === "webhook" ? webhookRules : microsoft ? microsoftRules : emailRules;
    const withSignIn = kind === "email";
    const next: Attempt = {
      rules: () => (withSignIn ? [...base(), signInRule()] : base()),
      values: {
        name,
        url,
        host,
        port,
        fromAddress,
        signIn: withSignIn ? (signInProblemKind(signIn, signInStored) ?? "ok") : "ok",
      },
    };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;

    // Spec §2.2: a webhook channel's config is `{url}` only -- `method` and
    // `headers` were dropped from the spec (whole-branch review finding M3):
    // the backend always POSTs and builds its own headers, and a
    // caller-configurable `headers` dict is an invariant-3 hazard (a secret
    // pasted into it would be returned by the API).
    //
    // Trimmed to match what was validated -- `requiredProblems` trims before
    // testing, so an untrimmed send could carry a URL the checks never saw.
    const config =
      kind === "webhook"
        ? { url: url.trim() }
        : microsoft
          ? {
              auth: "microsoft",
              host: MICROSOFT_SMTP.host,
              port: MICROSOFT_SMTP.port,
              from_address: fromAddress.trim(),
              use_tls: true,
            }
          : {
              auth: "password",
              host: host.trim(),
              port: Number(port.trim()),
              from_address: fromAddress.trim(),
              use_tls: useTls,
            };
    // An email channel's sign-in travels as its own fields, never as `secret`.
    const credential =
      kind === "webhook" ? (secret ? { secret } : {}) : signInRequest(signIn);
    if (editingChannel) {
      update.mutate(
        {
          id: editingChannel.id,
          body: { name: name.trim(), config, ...credential },
        },
        { onSuccess: () => { resetForm(); setDialog(null); } },
      );
      return;
    }
    create.mutate(
      kind === "webhook"
        ? { kind, name: name.trim(), config, secret: secret || undefined }
        : { kind, name: name.trim(), config, ...credential },
      { onSuccess: () => { resetForm(); setDialog(null); } },
    );
  }

  // SWALLOWED ENTIRELY BEFORE THIS CYCLE: `update` had no `onError` and no
  // call site rendered `update.error`, so a refused Enable/Disable left the
  // badge showing the OLD state with no explanation -- indistinguishable
  // from a toggle nobody clicked. Disabling a channel is how an operator
  // stops a noisy or compromised webhook; believing it worked when it did
  // not is the failure mode that matters here.
  function toggleActive(channel: ChannelOut) {
    update.mutate({ id: channel.id, body: { active: !channel.active } });
  }

  function runTest(channelId: string) {
    setTestedChannelId(channelId);
    test.mutate(channelId);
  }

  // How many rules would stop routing anywhere if this channel went away.
  // Counted rather than merely mentioned: "3 rules currently route through
  // this channel" is a fact the operator can weigh; "some rules may break" is
  // not. A rules list that 403'd or has not arrived yields `undefined`, and
  // the dialog then says nothing about rules rather than claiming zero.
  function rulesUsing(channelId: string): number | undefined {
    if (!rules.data) return undefined;
    return rules.data.filter((r) => r.channel_id === channelId).length;
  }

  function confirmDeleteBody(channel: ChannelOut): string {
    const used = rulesUsing(channel.id);
    const consequence =
      used === undefined
        ? ""
        : used === 0
          ? t("notification.channels.deleteNoRule")
          : t("notification.channels.deleteRules", { count: used });
    const base = t("notification.channels.deleteBody");
    return consequence === "" ? base : `${base} ${consequence}`;
  }

  return (
    // `id` is a stat tile's destination: the Channels tile on this page
    // links to `/notifications#channels`.
    <div id="channels">
      <Panel
        title={t("notification.channels.title")}
        subtitle={t("notification.channels.subtitle")}
        bare
        actions={canAdmin ? <Btn label={t("notification.channels.add")} icon="plus" onClick={openCreate} /> : undefined}
      >
        {channels.isLoading ? (
          <p className="note" style={{ padding: "16px 18px" }}>{t("notification.common.loading")}</p>
        ) : forbidden ? (
          <p className="note" style={{ padding: "16px 18px" }}>{t("notification.channels.forbidden")}</p>
        ) : failed ? (
          // `role="alert"` and the reason, through the one translation
          // (components/form.tsx): red text with no live region is not
          // announced, and "did not load" alone tells the reader nothing about
          // whether to retry or call someone.
          <p role="alert" className="notice red" style={{ margin: 14 }}>
            {t("notification.channels.failed", { reason: describeSubmitError(channels.error) })}
          </p>
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>{t("notification.common.name")}</Th>
                <Th>{t("notification.channels.colCredential")}</Th>
                {/* v3's "On" column: the switch is the state AND the control
                    (`P['notifications']`), instead of a pill here and a
                    "Disable" text link in the action cluster. */}
                <Th>{t("notification.channels.colOn")}</Th>
                {canAdmin && <Th>{""}</Th>}
              </tr>
            </thead>
            <tbody>
              {channels.data?.map((c) => (
                <tr key={c.id}>
                  {/* The kind as the row's glyph and, in words, under the name
                      with the destination beside it: `config` is the API's own
                      non-secret half of the channel (invariant #3), so the URL
                      or the relay host is safe to show and is what tells two
                      channels of the same kind apart. */}
                  <Td lead={<Icon name={c.kind === "email" ? "mail" : "link"} size={18} />}>
                    <div className="cell-title">{c.name}</div>
                    <div className="cell-sub">
                      {channelKindLabel(c.kind)}
                      {" · "}
                      <span className="mono">
                        {c.kind === "email"
                          ? `${String(c.config.host ?? "")}:${String(c.config.port ?? "")}`
                          : String(c.config.url ?? "")}
                      </span>
                      {c.kind === "email" && emailAuthOf(c.config) === "microsoft"
                        ? ` · ${t("notification.channels.microsoftSignIn")}`
                        : ""}
                    </div>
                  </Td>
                  <Td>{c.has_credential ? t("notification.channels.credentialSet") : t("notification.channels.credentialNone")}</Td>
                  <Td>
                    {/* A reader without `notification.admin` still needs to see
                        whether a channel is live, so the state is a pill for
                        them -- "Active"/"Inactive" through `StatusPill`, which
                        humanises; this column printed the wire's own lower-case
                        "active" one panel above a DELIVERY column reading
                        "Pending", "Sent" and "Dead-lettered". */}
                    {canAdmin ? (
                      <Toggle
                        checked={c.active}
                        onChange={() => toggleActive(c)}
                        label={c.active ? t("notification.channels.on") : t("notification.channels.off")}
                        disabled={update.isPending}
                      />
                    ) : (
                      <StatusPill value={c.active ? "active" : "inactive"} />
                    )}
                  </Td>
                  {canAdmin && (
                    <Td>
                      {/* ONE WEIGHT FOR THE WHOLE CLUSTER: Edit and Test were
                          borderless ghosts beside a bordered danger Delete, so
                          the row's least reversible action was the only one
                          that looked like a control. All are `Btn small` now
                          and Delete differs only in the ink and edge its
                          `danger` variant paints -- which is the distinction
                          Auditor B asked for ("no destructive colour, so a
                          mis-click is one pixel of travel away from
                          irreversible data loss") and the only one that should
                          be visible here. Enable/Disable has left the cluster
                          entirely: it is the switch in the On column. */}
                      <div className="flex flex-wrap gap-2">
                        <Btn
                          small
                          label={t("notification.channels.edit")}
                          ariaLabel={t("notification.channels.editAria", { name: c.name })}
                          onClick={() => openEdit(c)}
                        />
                        {/* NO `ariaLabel` HERE, unlike Edit and Delete: the
                            channels panel is found in the tests -- and by a
                            reader scanning the page -- as "the row with a Test
                            button", and the label already changes to "Testing…"
                            while one is in flight, which is state a fixed
                            accessible name would hide. */}
                        <Btn
                          small
                          label={test.isPending && testedChannelId === c.id ? t("notification.channels.testing") : t("notification.channels.test")}
                          onClick={() => runTest(c.id)}
                          disabled={test.isPending}
                        />
                        <RowMenu
                          subject={c.name}
                          items={[
                            {
                              label: t("notification.channels.delete"),
                              destructive: true,
                              disabled: del.isPending,
                              onSelect: () => setPendingDelete(c),
                            },
                          ]}
                        />
                      </div>
                      {/* `role="status"` because the test's OUTCOME is the whole
                          point of pressing the button, and Auditor A measured
                          `live: 0` here: "It is rendered inline in the row, not
                          in a live region, so assistive technology is never told
                          the test failed." */}
                      {testedChannelId === c.id && test.isSuccess && (
                        <p
                          role="status"
                          className={test.data.delivered ? "form-saved" : "form-error"}
                          style={{ marginTop: 4 }}
                        >
                          {test.data.delivered
                            ? t("notification.channels.delivered", {
                                code: test.data.status_code ?? t("notification.channels.deliveredOk"),
                              })
                            : t("notification.channels.notDelivered", { reason: deliveryErrorMessage(test.data.error) })}
                        </p>
                      )}
                      {/* `String(test.error)` rendered "Error: " for a bodyless
                          503 -- a label with no sentence after it, beside a
                          "Test" button that had just stopped saying "Testing…".
                          That reads as a test which finished, which is Auditor
                          A's BLOCKER shape exactly. */}
                      {testedChannelId === c.id && test.isError && (
                        <p role="alert" className="form-error" style={{ marginTop: 4 }}>
                          {describeSubmitError(test.error)}
                        </p>
                      )}
                    </Td>
                  )}
                </tr>
              ))}
              {channels.data && channels.data.length === 0 && (
                <tr>
                  <Td colSpan={canAdmin ? 4 : 3}>
                    <span className="note">{t("notification.channels.empty")}</span>
                  </Td>
                </tr>
              )}
            </tbody>
          </Table>
        )}

        {update.isError && (
          <p role="alert" className="notice red" style={{ margin: 14 }}>
            {t("notification.channels.notChanged", { reason: describeSubmitError(update.error) })}
          </p>
        )}

        {/* A refused DELETE leaves the row on screen, which reads as "the list
            has not refreshed yet" rather than "it is still there". Same reason
            the Enable/Disable failure gets a sentence. */}
        {del.isError && (
          <p role="alert" className="notice red" style={{ margin: 14 }}>
            {t("notification.channels.notDeleted", { reason: describeSubmitError(del.error) })}
          </p>
        )}

        <ConfirmDialog
          open={pendingDelete !== null}
          title={t("notification.channels.deleteTitle")}
          subject={pendingDelete?.name ?? ""}
          body={pendingDelete ? confirmDeleteBody(pendingDelete) : ""}
          confirmLabel={t("notification.channels.deleteConfirm")}
          destructive
          onCancel={() => setPendingDelete(null)}
          onConfirm={() => {
            if (!pendingDelete) return;
            del.mutate(pendingDelete.id);
            setPendingDelete(null);
          }}
        />

        {/* A real <form>, so Enter in the URL field saves instead of doing
            nothing -- the other half of the same finding. */}
        <Dialog
          open={creating}
          onClose={() => setDialog(null)}
          title={editingChannel ? t("notification.channels.editTitle") : t("notification.channels.addTitle")}
        >
          <Form onSubmit={submit}>
            <div className="form-grid">
              <Field label={t("notification.channels.kind")}>
                <select
                  className={inputClass}
                  value={kind}
                  disabled={editingChannel !== null}
                  onChange={(e) => setKind(e.target.value as ChannelKind)}
                >
                  {/* Auditor A (LOW): the select offered `webhook` while the
                      table rendered `Webhook` -- one value, two spellings, one
                      screen. Both now go through `channelKindLabel`. */}
                  <option value="webhook">{channelKindLabel("webhook")}</option>
                  <option value="email">{channelKindLabel("email")}</option>
                </select>
              </Field>
              {editingChannel && (
                <p className="form-hint">
                  {t("notification.channels.kindFixed")}
                  {editingChannel.has_credential ? ` ${t("notification.channels.credentialStored")}` : ""}
                </p>
              )}
              <Field label={t("notification.channels.name")}>
                <input className={inputClass} value={name} onChange={(e) => setName(e.target.value)} />
              </Field>
              {kind === "webhook" ? (
                <>
                  <Field label={t("notification.channels.url")} full>
                    {/* `type="url"` is the half of the finding the browser can
                        answer on its own -- it gets the right keyboard on mobile
                        and marks the value invalid for assistive tech. It does NOT
                        block submission: `<Form>` is `noValidate`, because a
                        native bubble that vanishes on the next click is the silent
                        no-op wearing a hat. `invalidWebhookUrl` is what the user
                        actually reads. */}
                    <input
                      className={inputClass}
                      type="url"
                      inputMode="url"
                      placeholder={t("notification.channels.urlPlaceholder")}
                      value={url}
                      onChange={(e) => setUrl(e.target.value)}
                    />
                  </Field>
                  <Field label={t("notification.channels.bearer")} full>
                    {/* Auditor A (MEDIUM): `type="password"` with
                        `autocomplete: null`, so "browser password managers may
                        autofill the operator's own password here and will offer to
                        save the webhook bearer token into the operator's personal
                        credential store". `new-password` is what /email-intake's
                        IMAP password field already sets -- the audit's point was
                        the inconsistency, not the absence of knowledge. */}
                    <input
                      className={inputClass}
                      type="password"
                      autoComplete="new-password"
                      value={secret}
                      onChange={(e) => setSecret(e.target.value)}
                    />
                  </Field>
                </>
              ) : (
                <>
                  {!microsoft && (
                    <>
                      <Field label={t("notification.channels.smtpHost")}>
                        <input className={inputClass} value={host} onChange={(e) => setHost(e.target.value)} />
                      </Field>
                      <Field label={t("notification.channels.smtpPort")}>
                        <input
                          className={inputClass}
                          type="number"
                          value={port}
                          onChange={(e) => setPort(e.target.value)}
                        />
                      </Field>
                    </>
                  )}
                  <Field label={t("notification.channels.fromAddress")} full>
                    <input
                      className={inputClass}
                      type="email"
                      placeholder={t("notification.channels.fromPlaceholder")}
                      value={fromAddress}
                      onChange={(e) => setFromAddress(e.target.value)}
                    />
                  </Field>
                  {!microsoft && (
                    <Field label={t("notification.channels.useTls")}>
                      <label className="flex items-center gap-2">
                        <input type="checkbox" checked={useTls} onChange={(e) => setUseTls(e.target.checked)} />
                        {t("notification.channels.useTls")}
                      </label>
                    </Field>
                  )}
                  <EmailSignInFields value={signIn} onChange={setSignIn} stored={signInStored} />
                </>
              )}
            </div>
            <div className="flex items-center justify-end gap-2">
              {/* `type="button"` is load-bearing now that there is a form around
                  it: a button with no type inside a <form> IS a submit button,
                  so Cancel would save. */}
              <Button type="button" variant="ghost" onClick={() => setDialog(null)}>
                {t("notification.channels.cancel")}
              </Button>
              {/* Enabled with the fields empty ON PURPOSE. The refusal is what
                  the user gets to read; a control that will not move is not.
                  The failure also comes from here rather than the old
                  `String(create.error)` line, which rendered "Error: " for a
                  bodyless 503 and nothing at all for the guard around it. */}
              <FormActions
                label={t("notification.channels.save")}
                pending={create.isPending || update.isPending}
                error={editingChannel ? update.error : create.error}
                problems={attemptProblems(attempt)}
              />
            </div>
          </Form>
        </Dialog>
      </Panel>
    </div>
  );
}
