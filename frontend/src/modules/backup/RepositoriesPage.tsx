/**
 * Backup repositories, on the v3 kit: the design's `P['backup/repositories']`
 * head and one bare panel of rows led by the database glyph, the storage ref
 * in mono under the name, a state pill and the registration date.
 *
 * The design has no dedicated "Repositories" tab (its Backup screen's tabs
 * are Overview / Backup jobs / Restore points / Policies); this route is a
 * real, separately-permissioned surface the app already had
 * (`backup.policy.manage` gates creation), so it keeps its own place in
 * `BackupPageNav` rather than folding into Overview.
 *
 * The design's rows also draw "Used" as a bar and "Capacity" as a figure;
 * `GET /backup/repositories` (RepositoryOut) returns neither a quota nor a
 * used-bytes count, so both are OMITTED rather than asserted. The tiles are
 * counts of the repositories actually fetched.
 */

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { ClientField } from "../../components/ClientField";
import { ConfirmDialog } from "../../components/confirm";
import { Button, Dialog, Field, inputClass, Table, Td, Th } from "../../components/ui";
import { Form, FormActions, attemptProblems, describeSubmitError } from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import { useSearchParams } from "react-router-dom";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import { formatDateTime } from "../../lib/datetime";
import type { Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { useClientScope } from "../../lib/clientScope";
import { requireKnown, useNewRowClientField } from "../../lib/clientSelection";
import { useClientName } from "../settings/api";
import { Icon } from "../../shell/icons";
import {
  useBackupRepositories,
  useCreateBackupRepositoryMutation,
  useRetireBackupRepositoryMutation,
} from "./api";
import { repositoryStateKind, repositoryStateLabel } from "./backupState";
import { BackupPageNav } from "./backupNav";
import { OffsiteReadiness } from "./BackupReadiness";
import type { Repository } from "./types";
import {
  Btn,
  FilterBar,
  PageHead,
  Panel,
  QueryState,
  StatCard,
  StatsGrid,
  StatusPill,
} from "../../shell/components";

export function RepositoriesPage() {
  const t = useT();
  const api = useApi();
  const [createOpen, setCreateOpen] = useState(false);
  // The state filter lives in the URL so the "Active"/"Retired" tiles can
  // hand over the exact rows they counted; the select below is the same
  // vocabulary, so a reader who arrives by tile can see and clear it.
  const [params, setParams] = useSearchParams();
  const stateFilter = params.get("state") ?? "";

  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const permissions = me.data?.permissions ?? [];
  // With every client chosen, each repository says whose it is.
  const { selected } = useClientScope();
  const clientName = useClientName();
  const showClient = selected === null;
  const canRead = permissions.includes("backup.read");
  const canManage = permissions.includes("backup.policy.manage");

  // Cycle 5: the operator's profile zone, from the `/me` this page already
  // fetches for permissions. Null falls back to the browser zone.
  const zone = me.data?.timezone;

  const repositories = useBackupRepositories();
  const items = repositories.data ?? [];
  const activeCount = items.filter((repo) => repo.state === "active").length;
  const retiredCount = items.filter((repo) => repo.state === "retired").length;
  const visible =
    stateFilter === "active"
      ? items.filter((repo) => repo.state === "active")
      : stateFilter === "retired"
        ? items.filter((repo) => repo.state === "retired")
        : items;

  // RETIRE (design §5.5): offered on an active repository of a client where
  // the person holds `backup.policy.manage`, from `/me.clients`. A courtesy
  // (invariant 6): the route checks the code, and refuses with a 409 while an
  // active policy names the repository -- explained below the table.
  const retire = useRetireBackupRepositoryMutation();
  const [retiring, setRetiring] = useState<Repository | null>(null);
  const mayRetire = (repo: Repository) =>
    repo.state === "active" &&
    (me.data?.clients ?? []).some(
      (client) => client.id === repo.client_id && client.permissions.includes("backup.policy.manage"),
    );

  return (
    <div>
      <PageHead
        title={t("backup.repositories.title")}
        description={t("backup.repositories.description")}
        actions={
          canManage ? (
            <Btn label={t("backup.repositories.newRepository")} icon="plus" variant="primary" onClick={() => setCreateOpen(true)} />
          ) : undefined
        }
      />

      <BackupPageNav canRead={canRead} />
      <OffsiteReadiness />

      {/* The total is the way back to the unfiltered list: arriving from
          `?state=retired` that is what clears the filter, which is the one
          job a total tile can honestly do. The other two open the rows they
          counted. Nothing here is `urgent`: a retired repository is a
          decision somebody made, not an incident with a deadline, and gold
          without weight says exactly that. */}
      <StatsGrid columns={3}>
        <StatCard
          label={t("backup.repositories.tileRepositories")}
          value={String(items.length)}
          icon="database"
          to="/backup/repositories"
          tone="neutral"
          zeroLabel={t("backup.repositories.noneRegistered")}
        />
        <StatCard
          label={t("backup.repositories.tileActive")}
          value={String(activeCount)}
          icon="circle-check"
          to="/backup/repositories?state=active"
          tone="green"
          zeroLabel={t("backup.repositories.noneAccept")}
        />
        <StatCard
          label={t("backup.repositories.tileRetired")}
          value={String(retiredCount)}
          icon="minus"
          to="/backup/repositories?state=retired"
          tone="gold"
          zeroLabel={t("backup.repositories.everyActive")}
        />
      </StatsGrid>

      <Panel bare>
        <FilterBar>
          <select
            className="select-field"
            value={stateFilter}
            onChange={(event) => {
              const next = new URLSearchParams(params);
              if (event.target.value === "") next.delete("state");
              else next.set("state", event.target.value);
              setParams(next);
            }}
            aria-label={t("backup.repositories.filterAria")}
          >
            <option value="">{t("backup.repositories.allRepositories")}</option>
            <option value="active">{t("backup.repositories.tileActive")}</option>
            <option value="retired">{t("backup.repositories.tileRetired")}</option>
          </select>
          <span className="filter-spacer" />
        </FilterBar>
        <QueryState
          loading={repositories.isLoading}
          error={repositories.error}
          empty={visible.length === 0}
          emptyLabel={
            items.length === 0
              ? t("backup.repositories.emptyNone")
              : t("backup.repositories.emptyFiltered")
          }
          emptyIcon="database"
        >
          <Table>
            <thead>
              <tr>
                <Th>{t("backup.repositories.colName")}</Th>
                {showClient ? <Th>{t("backup.repositories.colClient")}</Th> : null}
                <Th>{t("backup.repositories.colState")}</Th>
                <Th>{t("backup.repositories.colRegistered")}</Th>
                <Th />
              </tr>
            </thead>
            <tbody>
              {visible.map((repo) => (
                <tr key={repo.id}>
                  <Td lead={<Icon name="database" size={18} />}>
                    <div className="cell-title">{repo.name}</div>
                    <div className="cell-sub mono">{repo.storage_ref}</div>
                  </Td>
                  {showClient ? <Td>{clientName(repo.client_id)}</Td> : null}
                  <Td>
                    <StatusPill
                      value={repo.state}
                      kind={repositoryStateKind(repo.state)}
                      label={repositoryStateLabel(repo.state)}
                    />
                  </Td>
                  {/* CYCLE 5: was `new Date(...).toLocaleString()`. */}
                  <Td>
                    {formatDateTime(repo.created_at, zone)}
                  </Td>
                  <Td>
                    {mayRetire(repo) ? (
                      <Btn
                        small
                        variant="danger"
                        label={t("backup.repositories.retire")}
                        ariaLabel={t("backup.repositories.retireAria", { name: repo.name })}
                        onClick={() => {
                          retire.reset();
                          setRetiring(repo);
                        }}
                      />
                    ) : null}
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        </QueryState>
        {retire.isError ? (
          <p role="alert" className="form-error">
            {t("backup.repositories.retireFailed")} {describeSubmitError(retire.error)}
          </p>
        ) : null}
      </Panel>

      <ConfirmDialog
        open={retiring !== null}
        title={t("backup.repositories.retireTitle")}
        subject={retiring?.name ?? ""}
        body={t("backup.repositories.retireBody")}
        confirmLabel={t("backup.repositories.retireConfirm")}
        destructive
        onCancel={() => setRetiring(null)}
        onConfirm={() => {
          const repo = retiring;
          setRetiring(null);
          if (repo) retire.mutate(repo.id);
        }}
      />

      {/* KEYED so a refusal cannot outlive the dialog that produced it: this
          component is never unmounted (only `Dialog` returns null when
          closed), so a failed create left its alert sitting there for the
          next opening, describing a request the operator had abandoned. */}
      <CreateRepositoryDialog
        key={createOpen ? "open" : "closed"}
        open={createOpen}
        onClose={() => setCreateOpen(false)}
      />
    </div>
  );
}

const repositoryRules = (): readonly FieldRule[] => [
  { name: "name", label: translate("backup.repositories.name") },
];

function CreateRepositoryDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const t = useT();
  const create = useCreateBackupRepositoryMutation();
  const [name, setName] = useState("");
  // A repository is ONE client's: its key travels to every device that backs
  // up into it (MSP clients phase 2, Task 13). Nothing on this page is a row
  // to take it from, so it is the client chosen at the top, or with every
  // client the dialog's Client field (design §3.1).
  const client = useNewRowClientField("backup.policy.manage");
  const clientId = client.clientId;
  // Populated ON SUBMIT, never while typing. Held as data (rules and values)
  // so a language switch re-words the sentences.
  const [attempt, setAttempt] = useState<Attempt | null>(null);

  function submit() {
    // The previous outcome must not be readable as this one.
    create.reset();
    const next: Attempt = {
      rules: () => [
        ...repositoryRules(),
        ...(client.needsField ? [{ name: "client", label: translate("backup.repositories.client") }] : []),
      ],
      values: { name, client: clientId },
    };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    create.mutate(
      () => ({ client_id: requireKnown(clientId), name: name.trim() }),
      {
        onSuccess: () => {
          setName("");
          onClose();
        },
      },
    );
  }

  return (
    <Dialog open={open} onClose={onClose} title={t("backup.repositories.dialogTitle")}>
      {/* A real <form>, so Enter in Name creates the repository. */}
      <Form onSubmit={submit}>
        {client.needsField ? <ClientField field={client} /> : null}
        <Field label={t("backup.repositories.name")}>
          <input className={inputClass} value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
        <div className="flex flex-wrap items-center justify-end gap-2">
          {/* `type="button"` is load-bearing: `ui.tsx`'s Button sets no type,
              and an untyped button inside a form submits it: Cancel would
              create the repository. */}
          <Button type="button" variant="ghost" onClick={onClose}>
            {t("common.actions.cancel")}
          </Button>
          {/* CYCLE 4. Was `{String(create.error)}` in an unannounced <p>: a
              bodyless 503 carries `message === ""`, so `String(error)`
              rendered "Error: " and nothing announced it. `FormActions` routes
              every failure through `describeSubmitError` into a role="alert".
              The control is also no longer disabled on an empty Name; that
              disabled state was the silent no-op, and the click names the
              field now. */}
          {/* With Unassigned chosen at the top nothing new can be filed: the
              dialog says so instead of offering a Create that can only fail. */}
          {client.refused === "unassigned" ? (
            <div className="notice" role="note">
              <Icon name="info" size={17} />
              <p>{t("backup.unassignedRefused")}</p>
            </div>
          ) : (
            <FormActions
              label={t("backup.repositories.create")}
              pendingLabel={t("backup.repositories.creating")}
              pending={create.isPending}
              error={create.error}
              problems={attemptProblems(attempt)}
            />
          )}
        </div>
      </Form>
    </Dialog>
  );
}
