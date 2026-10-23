import { useState } from "react";
import { ConfirmDialog } from "../../components/confirm";
import { Form, FormActions, attemptProblems, describeSubmitError } from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import { Button, Dialog, Field, Table, Td, Th, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import { Btn, Panel, QueryState } from "../../shell/components";
import { Icon } from "../../shell/icons";
import {
  useCannedResponses,
  useCreateCannedResponseMutation,
  useDeleteCannedResponseMutation,
  useUpdateCannedResponseMutation,
} from "./api";
import type { CannedResponseOut } from "./types";

const replyRules = (): readonly FieldRule[] => [
  { name: "title", label: translate("serviceDesk.common.name") },
  { name: "body", label: translate("serviceDesk.replies.replyText") },
];

// Mirrors the server's bounds (schema.py) so the form can say so first.
const MAX_TITLE = 100;
const MAX_BODY = 20_000;

/**
 * The org's saved replies. Everyone who works tickets can insert them; only
 * `ticket.admin` holders see the controls to change them (the server checks
 * that on every write).
 */
export function SavedRepliesPanel({ canAdmin }: { canAdmin: boolean }) {
  const t = useT();
  const replies = useCannedResponses();
  const remove = useDeleteCannedResponseMutation();
  const [editing, setEditing] = useState<CannedResponseOut | "new" | null>(null);
  const [deleting, setDeleting] = useState<CannedResponseOut | null>(null);

  return (
    <Panel
      title={t("serviceDesk.replies.title")}
      subtitle={t("serviceDesk.replies.subtitle")}
      bare
      actions={
        canAdmin ? (
          <Btn label={t("serviceDesk.replies.newReply")} icon="plus" variant="primary" onClick={() => setEditing("new")} />
        ) : undefined
      }
    >
      <QueryState
        loading={replies.isLoading}
        error={replies.error}
        empty={replies.data != null && replies.data.length === 0}
        emptyIcon="mail"
        emptyLabel={t("serviceDesk.replies.empty")}
      >
        <Table>
          <thead>
            <tr>
              <Th>{t("serviceDesk.common.name")}</Th>
              {canAdmin && <Th>{""}</Th>}
            </tr>
          </thead>
          <tbody>
            {replies.data?.map((reply) => (
              <tr key={reply.id}>
                <Td lead={<Icon name="mail" size={18} />} clip={520}>
                  <div className="cell-title">{reply.title}</div>
                  <div className="cell-sub" title={reply.body}>
                    {reply.body}
                  </div>
                </Td>
                {canAdmin && (
                  <Td>
                    <div className="flex gap-2">
                      <Btn small label={t("serviceDesk.common.edit")} ariaLabel={t("serviceDesk.replies.editAria", { title: reply.title })} onClick={() => setEditing(reply)} />
                      <Btn
                        small
                        variant="danger"
                        label={t("serviceDesk.common.delete")}
                        ariaLabel={t("serviceDesk.replies.deleteAria", { title: reply.title })}
                        onClick={() => setDeleting(reply)}
                      />
                    </div>
                  </Td>
                )}
              </tr>
            ))}
          </tbody>
        </Table>
      </QueryState>
      {remove.isError && (
        <p role="alert" className="notice red" style={{ margin: 14 }}>
          {describeSubmitError(remove.error)}
        </p>
      )}

      {editing != null && (
        <SavedReplyDialog
          reply={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
        />
      )}
      <ConfirmDialog
        open={deleting != null}
        title={t("serviceDesk.replies.deleteTitle")}
        body={t("serviceDesk.replies.deleteBody")}
        subject={deleting?.title ?? ""}
        confirmLabel={t("serviceDesk.common.delete")}
        destructive
        onConfirm={() => {
          if (deleting) remove.mutate(deleting.id);
          setDeleting(null);
        }}
        onCancel={() => setDeleting(null)}
      />
    </Panel>
  );
}

function SavedReplyDialog({
  reply,
  onClose,
}: {
  reply: CannedResponseOut | null;
  onClose: () => void;
}) {
  const t = useT();
  const create = useCreateCannedResponseMutation();
  const update = useUpdateCannedResponseMutation();
  const [title, setTitle] = useState(reply?.title ?? "");
  const [body, setBody] = useState(reply?.body ?? "");
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  const mutation = reply ? update : create;

  function submit() {
    const next: Attempt = { rules: replyRules, values: { title, body } };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    const values = { title: title.trim(), body };
    if (reply) update.mutate({ id: reply.id, body: values }, { onSuccess: onClose });
    else create.mutate(values, { onSuccess: onClose });
  }

  return (
    <Dialog open onClose={onClose} title={reply ? t("serviceDesk.replies.editTitle") : t("serviceDesk.replies.newReply")}>
      <Form onSubmit={submit}>
        <div className="form-grid">
          <Field label={t("serviceDesk.common.name")} full>
            <input
              className={inputClass}
              required
              maxLength={MAX_TITLE}
              placeholder={t("serviceDesk.replies.namePlaceholder")}
              value={title}
              onChange={(e) => setTitle(e.target.value)}
            />
          </Field>
          <Field label={t("serviceDesk.replies.replyText")} full>
            <textarea
              className={inputClass}
              required
              rows={8}
              maxLength={MAX_BODY}
              value={body}
              onChange={(e) => setBody(e.target.value)}
            />
          </Field>
        </div>
        <div className="flex flex-wrap items-center justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>
            {t("common.actions.cancel")}
          </Button>
          <FormActions
            label={t("common.actions.save")}
            pending={mutation.isPending}
            error={mutation.error}
            problems={attemptProblems(attempt)}
          />
        </div>
      </Form>
    </Dialog>
  );
}
