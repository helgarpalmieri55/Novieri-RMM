// Small pieces other modules' pages use to link to a person. Each renders
// nothing when the viewer cannot read people, or when there is nobody to show.
//
// THERE IS NO PEOPLE PAGE TO SEND ANYONE TO (operator decision 2026-09-23).
// People left the menu; a device's holder is set from the device, so the
// picker below can add someone new itself rather than pointing at a list that
// no longer exists.

import { useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import {
  attemptProblems,
  describeSubmitError,
  Form,
  FormActions,
  type Attempt,
  type FieldRule,
} from "../../components/form";
import { EntityPicker } from "../../components/picker";
import { Button, Dialog, Field, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import {
  useCan,
  useClearDeviceOverride,
  useCreatePerson,
  useDevicePerson,
  usePeople,
  usePersonForUser,
  useSetDevicePerson,
} from "./api";
import { assignedSourceNote } from "./labels";
import type { PersonOut } from "./types";

/** "View person" for a console or portal account, e.g. a ticket requester. */
export function PersonLinkForUser({ userId }: { userId: string | null | undefined }) {
  const t = useT();
  const canRead = useCan("people.read");
  const person = usePersonForUser(userId, canRead);
  if (!canRead || !person.data) return null;
  return (
    <Link to={`/people/${person.data.id}`} className="note">
      {t("people.links.viewPerson")}
    </Link>
  );
}

/** A person id on another module's row, as a link. */
export function PersonLink({ personId, children }: { personId: string; children: ReactNode }) {
  const canRead = useCan("people.read");
  if (!canRead) return <>{children}</>;
  return <Link to={`/people/${personId}`}>{children}</Link>;
}

/** Someone to add, from the picker's "Someone new…" form. */
export interface NewPerson {
  display_name: string;
  email: string | null;
}

const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

/** The "Someone new" form's rules, worded when read. The email is checked only when one was typed. */
function newPersonRules(withEmail: boolean): readonly FieldRule[] {
  return [
    { name: "display_name", label: translate("people.links.picker.name") },
    ...(withEmail
      ? [
          {
            name: "email",
            label: translate("people.links.picker.email"),
            validate: (value: string) => (EMAIL.test(value) ? null : translate("people.links.picker.notAnEmail")),
          },
        ]
      : []),
  ];
}

/**
 * Choose a person, or nobody -- or, when `onConfirmNew` is given, add someone
 * new. `pending` and `error` cover whichever of the two the caller is running.
 */
export function PersonPickerDialog({
  open,
  title,
  confirmLabel,
  allowNobody,
  pending,
  error,
  onCancel,
  onConfirm,
  onConfirmNew,
}: {
  open: boolean;
  title: string;
  confirmLabel: string;
  allowNobody: boolean;
  pending: boolean;
  error: unknown;
  onCancel: () => void;
  onConfirm: (personId: string | null) => void;
  /** Offer "Someone new…": a name and an optional email, handed back here. */
  onConfirmNew?: (person: NewPerson) => void;
}) {
  const t = useT();
  const people = usePeople({}, open);
  const [value, setValue] = useState("");
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  // The refused submission as data (the rules and what they were checked against); worded at render.
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  const options = [
    ...(allowNobody ? [{ id: "none", label: t("people.links.picker.nobody") }] : []),
    ...(people.data ?? []).map((p) => ({
      id: p.id,
      label: p.display_name,
      sublabel: p.email ?? undefined,
    })),
  ];

  function cancel() {
    setAdding(false);
    setAttempt(null);
    onCancel();
  }

  function submitNew() {
    const withEmail = email.trim() !== "";
    const next: Attempt = { rules: () => newPersonRules(withEmail), values: { display_name: name, email } };
    setAttempt(next);
    if (attemptProblems(next).length > 0 || !onConfirmNew) return;
    onConfirmNew({ display_name: name.trim(), email: email.trim() || null });
  }

  if (adding && onConfirmNew) {
    return (
      <Dialog open={open} onClose={cancel} title={title}>
        <Form onSubmit={submitNew}>
          <div className="form-grid">
            <Field label={t("people.links.picker.name")}>
              <input className={inputClass} value={name} onChange={(e) => setName(e.target.value)} />
            </Field>
            <Field label={t("people.links.picker.email")} help={t("people.links.picker.emailHelp")}>
              <input
                className={inputClass}
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            </Field>
          </div>
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={() => setAdding(false)}>
              {t("people.links.picker.chooseExisting")}
            </Button>
            <FormActions label={confirmLabel} pending={pending} error={error} problems={attemptProblems(attempt)} />
          </div>
        </Form>
      </Dialog>
    );
  }

  return (
    <Dialog open={open} onClose={cancel} title={title}>
      <EntityPicker
        label={t("people.links.picker.person")}
        options={options}
        value={value}
        onChange={setValue}
        loading={people.isLoading}
        emptyHint={
          onConfirmNew ? t("people.links.picker.emptyWithNew") : t("people.links.picker.empty")
        }
      />
      {error ? (
        <p role="alert" className="form-error">
          {describeSubmitError(error)}
        </p>
      ) : null}
      <div className="flex justify-end gap-2" style={{ marginTop: 12 }}>
        {onConfirmNew ? (
          <Button
            variant="outline"
            onClick={() => setAdding(true)}
            style={{ marginRight: "auto" }}
          >
            {t("people.links.picker.someoneNew")}
          </Button>
        ) : null}
        <Button variant="ghost" onClick={cancel}>
          {t("people.links.picker.cancel")}
        </Button>
        <Button
          disabled={!value || pending}
          onClick={() => onConfirm(value === "none" ? null : value)}
        >
          {confirmLabel}
        </Button>
      </div>
    </Dialog>
  );
}

function sameDraft(person: PersonOut, draft: NewPerson): boolean {
  return person.display_name === draft.display_name && (person.email ?? null) === draft.email;
}

/**
 * "Assigned to" on a device's page: who holds it, read as a field of the
 * device, with a way to change it (operator decision 2026-09-23).
 *
 * The holder is a person underneath -- set by hand, or taken from who signs in
 * at the machine -- so reading needs `people.read` and changing `people.manage`,
 * both enforced by the server; hiding here is a courtesy.
 */
export function DeviceAssignedTo({ deviceId }: { deviceId: string }) {
  const t = useT();
  const canRead = useCan("people.read");
  const canManage = useCan("people.manage");
  const link = useDevicePerson(deviceId, canRead);
  const set = useSetDevicePerson(deviceId);
  const clear = useClearDeviceOverride(deviceId);
  const create = useCreatePerson();
  const [choosing, setChoosing] = useState(false);
  // KEPT ACROSS A FAILED ASSIGNMENT. Create-then-assign is two requests; if
  // the second fails, pressing Save again must assign the person the first
  // one made, not add the same someone twice.
  const [created, setCreated] = useState<PersonOut | null>(null);
  if (!canRead || !link.data) return null;
  const { person, source, observed_login: login } = link.data;

  function close() {
    create.reset();
    set.reset();
    setCreated(null);
    setChoosing(false);
  }

  function assign(personId: string | null) {
    set.mutate(personId, { onSuccess: close });
  }

  function assignNew(draft: NewPerson) {
    if (created && sameDraft(created, draft)) {
      assign(created.id);
      return;
    }
    set.reset();
    // A FAILED CREATE ASSIGNS NOTHING: the assignment is only reached from
    // the create's own success.
    create.mutate(draft, {
      onSuccess: (added) => {
        setCreated(added);
        assign(added.id);
      },
    });
  }

  const notes = [
    assignedSourceNote(source, person != null),
    !person && login ? t("people.links.lastSignedInAs", { login }) : null,
  ].filter((note): note is string => note != null);

  return (
    <span className="flex flex-wrap items-center gap-2" data-testid="device-assigned-to">
      <span>
        <span className="note">{t("people.links.assignedTo")}</span>{" "}
        {person ? (
          <Link to={`/people/${person.id}`}>{person.display_name}</Link>
        ) : (
          <span>{source === "operator" ? t("people.links.nobody") : t("people.links.nobodyYet")}</span>
        )}
        {notes.map((note) => (
          <span key={note} className="note">
            {" · "}
            {note}
          </span>
        ))}
      </span>
      {canManage ? (
        <>
          <Button variant="ghost" onClick={() => setChoosing(true)}>
            {t("people.links.change")}
          </Button>
          {source === "operator" ? (
            <Button variant="ghost" disabled={clear.isPending} onClick={() => clear.mutate()}>
              {t("people.links.backToAutomatic")}
            </Button>
          ) : null}
        </>
      ) : null}
      <PersonPickerDialog
        open={choosing}
        title={t("people.links.assignTitle")}
        confirmLabel={t("people.links.save")}
        allowNobody
        pending={create.isPending || set.isPending}
        error={create.error ?? set.error}
        onCancel={close}
        onConfirm={assign}
        onConfirmNew={assignNew}
      />
    </span>
  );
}
