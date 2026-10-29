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
import { Button, Dialog, Field, inputClass, Table, Td, Th } from "../../components/ui";
import { Form, FormActions, attemptProblems } from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import { useSearchParams } from "react-router-dom";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import { formatDateTime } from "../../lib/datetime";
import type { Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { internalClientId, requireKnown } from "../../lib/internalClient";
import { Icon } from "../../shell/icons";
import { useBackupRepositories, useCreateBackupRepositoryMutation } from "./api";
import { repositoryStateKind, repositoryStateLabel } from "./backupState";
import { BackupPageNav } from "./backupNav";
import { OffsiteReadiness } from "./BackupReadiness";
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
  // The state filter lives in the URL so the "Active"/"Disabled" tiles can
  // hand over the exact rows they counted; the select below is the same
  // vocabulary, so a reader who arrives by tile can see and clear it.
  const [params, setParams] = useSearchParams();
  const stateFilter = params.get("state") ?? "";

  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const permissions = me.data?.permissions ?? [];
  const canRead = permissions.includes("backup.read");
  const canManage = permissions.includes("backup.policy.manage");

  // Cycle 5: the operator's profile zone, from the `/me` this page already
  // fetches for permissions. Null falls back to the browser zone.
  const zone = me.data?.timezone;

  const repositories = useBackupRepositories();
  const items = repositories.data ?? [];
  const activeCount = items.filter((repo) => repo.state === "active").length;
  const visible =
    stateFilter === "active"
      ? items.filter((repo) => repo.state === "active")
      : stateFilter === "disabled"
        ? items.filter((repo) => repo.state !== "active")
        : items;

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
          `?state=disabled` that is what clears the filter, which is the one
          job a total tile can honestly do. The other two open the rows they
          counted. Nothing here is `urgent`: a repository that is registered
          but not active is a configuration state somebody chose, not an
          incident with a deadline, and gold without weight says exactly
          that. */}
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
          label={t("backup.repositories.tileDisabled")}
          value={String(items.length - activeCount)}
          icon="minus"
          to="/backup/repositories?state=disabled"
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
            <option value="disabled">{t("backup.repositories.notActive")}</option>
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
                <Th>{t("backup.repositories.colState")}</Th>
                <Th>{t("backup.repositories.colRegistered")}</Th>
              </tr>
            </thead>
            <tbody>
              {visible.map((repo) => (
                <tr key={repo.id}>
                  <Td lead={<Icon name="database" size={18} />}>
                    <div className="cell-title">{repo.name}</div>
                    <div className="cell-sub mono">{repo.storage_ref}</div>
                  </Td>
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
                </tr>
              ))}
            </tbody>
          </Table>
        </QueryState>
      </Panel>

      {/* KEYED so a refusal cannot outlive the dialog that produced it: this
          component is never unmounted (only `Dialog` returns null when
          closed), so a failed create left its alert sitting there for the
          next opening, describing a request the operator had abandoned. */}
      <CreateRepositoryDialog
        key={createOpen ? "open" : "closed"}
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        // A repository is ONE client's: its key travels to every device that
        // backs up into it (MSP clients phase 2, Task 13). Nothing on this
        // page is a row of a client to take it from.
        // PHASE 3: the client/site selector. Until it exists that is the
        // organisation's internal client, whose id is the organisation's --
        // `/me`'s `org_id` (`lib/internalClient.ts`).
        clientId={internalClientId(me.data)}
      />
    </div>
  );
}

const repositoryRules = (): readonly FieldRule[] => [
  { name: "name", label: translate("backup.repositories.name") },
];

function CreateRepositoryDialog({
  open,
  onClose,
  clientId,
}: {
  open: boolean;
  onClose: () => void;
  /** The client the repository is filed under; `null` until it is known. */
  clientId: string | null;
}) {
  const t = useT();
  const create = useCreateBackupRepositoryMutation();
  const [name, setName] = useState("");
  // Populated ON SUBMIT, never while typing. Held as data (rules and values)
  // so a language switch re-words the sentences.
  const [attempt, setAttempt] = useState<Attempt | null>(null);

  function submit() {
    // The previous outcome must not be readable as this one.
    create.reset();
    const next: Attempt = { rules: repositoryRules, values: { name } };
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
          <FormActions
            label={t("backup.repositories.create")}
            pendingLabel={t("backup.repositories.creating")}
            pending={create.isPending}
            error={create.error}
            problems={attemptProblems(attempt)}
          />
        </div>
      </Form>
    </Dialog>
  );
}
