import { useLayoutEffect, useState } from "react";
import { ConfirmDialog } from "../../components/confirm";
import { attemptProblems, describeSubmitError, Form, FormActions } from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import { translate } from "../../i18n/core";
import { Button, Dialog, Field, inputClass, Table, Td, Th } from "../../components/ui";
import { RowMenu } from "../../components/rowmenu";
import { NoValue } from "../../components/emptystate";
import { useT } from "../../i18n/context";
import { ApiError } from "../../lib/api";
import { Icon } from "../../shell/icons";
import { Btn, FormSection } from "../../shell/components";
import { SectionLead } from "../service-desk/SectionLead";
import { useCreateAllowlistEntryMutation, useDeleteAllowlistEntryMutation, useNotificationAllowlist } from "./api";
import type { AllowlistEntryOut } from "./types";

// Spec §0.1/§2.6/§11: a webhook URL is a server-side request forgery
// primitive by construction, and the egress guard (app/core/egress.py)
// denies private address space by default. An allowlist entry is a
// deliberate, per-host-AND-PORT hole in that guard -- migration 0026 made
// `port` a required part of the entry (an admin allowlists
// `mattermost.lan:8065`, not a host and every port on it), and this form
// must both collect it and make unmistakable what adding one means.
/** What an allowlist entry needs, named as the form names it (worded when read). */
const allowlistRules = (): readonly FieldRule[] => [
  { name: "host", label: translate("notification.common.host") },
  { name: "port", label: translate("notification.common.port") },
];

export function AllowlistPanel({ canAdmin }: { canAdmin: boolean }) {
  const t = useT();
  const allowlist = useNotificationAllowlist();
  const create = useCreateAllowlistEntryMutation();
  const del = useDeleteAllowlistEntryMutation();

  const [creating, setCreating] = useState(false);
  const [host, setHost] = useState("");
  const [port, setPort] = useState("443");
  const [note, setNote] = useState("");
  // The entry awaiting confirmation, held whole so the dialog can name the
  // host AND port -- the pair IS the identity here (migration 0026), and
  // "mattermost.lan" alone would not distinguish two rows that differ only by
  // port, which is precisely the mis-click this guards against.
  const [pendingDelete, setPendingDelete] = useState<AllowlistEntryOut | null>(null);

  const forbidden = allowlist.error instanceof ApiError && allowlist.error.status === 403;
  const failed = allowlist.error != null && !forbidden;

  // Set ON SUBMIT, so a field being typed is not called missing before it is done.
  const [attempt, setAttempt] = useState<Attempt | null>(null);

  // THE FORM STARTS EMPTY WHENEVER THE DIALOG OPENS (general ruling 4; fix
  // round 2): a failed Save, then Cancel, then Add host again showed the last
  // host and its refusal. `create.reset()` because mutation state outlives
  // the dialog. Before paint, so nothing stale flashes.
  useLayoutEffect(() => {
    if (!creating) return;
    setHost("");
    setPort("443");
    setNote("");
    setAttempt(null);
    create.reset();
    // The opening is what counts.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [creating]);

  function submit() {
    // SAVE STAYS PRESSABLE (general ruling 8; fix round 2): an empty host or
    // port is named on Save, as every other dialog does, instead of a dead
    // button that says nothing.
    const next: Attempt = { rules: allowlistRules, values: { host: host.trim(), port: port.trim() } };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    create.mutate({ host, port: Number(port), note }, { onSuccess: () => setCreating(false) });
  }

  return (
    // The Allowlist entries stat tile links to `/notifications#allowlist`.
    <div id="allowlist">
      <FormSection title={t("notification.allowlist.title")} icon="shield-lock">
        <SectionLead text={t("notification.allowlist.subtitle")} action={canAdmin ? <Btn label={t("notification.allowlist.add")} icon="plus" onClick={() => setCreating(true)} /> : undefined} />
        {/* Requirement #3: a visible warning, not decoration -- this always
            renders on the panel (not only inside the create form) so a reader
            understands what every row already on the list means, too. */}
        <p className="notice gold" style={{ margin: 14 }}>
          <Icon name="alert-triangle" size={17} />
          <span>
            {t("notification.allowlist.warning")}
          </span>
        </p>
        {allowlist.isLoading ? (
          <p className="note">{t("notification.common.loading")}</p>
        ) : forbidden ? (
          <p className="note">{t("notification.allowlist.forbidden")}</p>
        ) : failed ? (
          <p role="alert" className="notice red" style={{ margin: 14 }}>
            {t("notification.allowlist.failed", { reason: describeSubmitError(allowlist.error) })}
          </p>
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>{t("notification.common.host")}</Th>
                <Th>{t("notification.common.note")}</Th>
                {canAdmin && <Th>{""}</Th>}
              </tr>
            </thead>
            <tbody>
              {allowlist.data?.map((entry) => (
                <tr key={entry.id}>
                  {/* The pair IS the identity (migration 0026): host and port
                      together, in mono, as one title. */}
                  <Td lead={<Icon name="world" size={18} />}>
                    <div className="cell-title mono">
                      {entry.host}:{entry.port}
                    </div>
                  </Td>
                  <Td>{entry.note ? entry.note : <NoValue label={t("notification.allowlist.noNote")} />}</Td>
                  {canAdmin && (
                    <Td>
                      {/* A security-control row removed on one unconfirmed
                          click, styled like every other action: the audits'
                          cross-cutting "destructive actions are undifferentiated
                          and unconfirmed" finding. Removing an entry is not
                          merely tidying -- it revokes the exception that lets a
                          live channel reach that host, so anything routed
                          through it stops delivering. */}
                      <RowMenu
                        subject={`${entry.host}:${entry.port}`}
                        items={[
                          {
                            label: t("notification.allowlist.remove"),
                            destructive: true,
                            disabled: del.isPending,
                            onSelect: () => setPendingDelete(entry),
                          },
                        ]}
                      />
                    </Td>
                  )}
                </tr>
              ))}
              {allowlist.data && allowlist.data.length === 0 && (
                <tr>
                  <Td colSpan={canAdmin ? 3 : 2}>
                    <span className="note">{t("notification.allowlist.empty")}</span>
                  </Td>
                </tr>
              )}
            </tbody>
          </Table>
        )}
        {/* REMOVING AN ENTRY IS THE SECURITY-RELEVANT DIRECTION -- an operator
            closing a hole in the egress guard. `String(del.error)` rendered
            "Error: " for a bodyless 503 while the row stayed on screen, which
            reads as "the row is still listed because the page has not
            refreshed yet" rather than "the hole is still open". */}
        {del.isError && (
          <p role="alert" className="notice red" style={{ margin: 14 }}>
            {t("notification.allowlist.notRemoved", { reason: describeSubmitError(del.error) })}
          </p>
        )}

        <ConfirmDialog
          open={pendingDelete !== null}
          title={t("notification.allowlist.removeTitle")}
          subject={pendingDelete ? `${pendingDelete.host}:${pendingDelete.port}` : ""}
          body={t("notification.allowlist.removeBody")}
          confirmLabel={t("notification.allowlist.removeConfirm")}
          destructive
          onCancel={() => setPendingDelete(null)}
          onConfirm={() => {
            if (!pendingDelete) return;
            del.mutate(pendingDelete.id);
            setPendingDelete(null);
          }}
        />

        <Dialog open={creating} onClose={() => setCreating(false)} title={t("notification.allowlist.addTitle")}>
          <p className="notice gold" style={{ marginBottom: 12 }}>
            <Icon name="alert-triangle" size={17} />
            <span>
              {t("notification.allowlist.addWarning")}
            </span>
          </p>
          <Form onSubmit={submit}>
          <div className="form-grid">
            <Field label={t("notification.common.host")}>
              <input
                className={inputClass}
                placeholder={t("notification.allowlist.hostPlaceholder")}
                value={host}
                onChange={(e) => setHost(e.target.value)}
              />
            </Field>
            <Field label={t("notification.common.port")}>
              <input
                className={inputClass}
                type="number"
                min={1}
                max={65535}
                value={port}
                onChange={(e) => setPort(e.target.value)}
              />
            </Field>
            <Field label={t("notification.common.note")} full>
              <input className={inputClass} value={note} onChange={(e) => setNote(e.target.value)} />
            </Field>
          </div>
          <div className="flex flex-wrap items-center justify-end gap-2">
            {/* `type="button"`: inside a <form>, an untyped button submits. */}
            <Button type="button" variant="ghost" onClick={() => setCreating(false)}>
              {t("notification.allowlist.cancel")}
            </Button>
            <FormActions
              label={t("notification.allowlist.save")}
              pendingLabel={t("notification.common.saving")}
              pending={create.isPending}
              error={create.error}
              problems={attemptProblems(attempt)}
            />
          </div>
          </Form>
        </Dialog>
      </FormSection>
    </div>
  );
}
