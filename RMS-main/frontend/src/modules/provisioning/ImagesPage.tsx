/**
 * OS images, on the v3 kit: the design's `P['provisioning/images']` head
 * and one bare panel of rows led by the photo glyph, the sha256 in mono
 * under the name, and the registration date.
 *
 * The design's rows also print the image's size and how many runs used it.
 * `OsImage` (schema.py) carries `artifact_ref` and `artifact_sha256` but no
 * size, and no endpoint counts runs per image, so both are OMITTED rather
 * than guessed. The tiles are counts of the images actually fetched.
 */

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Form, FormActions, attemptProblems } from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import { Button, Dialog, Field, inputClass, Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import type { Me } from "../../lib/api";
import { useSearchParams } from "react-router-dom";
import { useApi } from "../../lib/ApiContext";
import { formatDateTime } from "../../lib/datetime";
import { Icon } from "../../shell/icons";
import { useCreateOsImageMutation, useOsImages } from "./api";
import { humaniseEnum } from "../../lib/labels";
import { OsIcon } from "../../shell/viz";
import { osFamilyLabel, osImageStateLabel } from "./pickerOptions";
import { ProvisioningPageNav } from "./provisioningNav";
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

/** `sha256 7c1d…e0a2`: the first and last four hex digits, as the design prints them. */
function shortSha(sha: string): string {
  return sha.length > 12 ? `${sha.slice(0, 4)}…${sha.slice(-4)}` : sha;
}

export function ImagesPage() {
  const t = useT();
  const api = useApi();
  const [createOpen, setCreateOpen] = useState(false);
  // In the URL, so the "Active" tile hands over the rows it counted instead
  // of linking back to the page the reader is already on.
  const [params, setParams] = useSearchParams();
  const stateFilter = params.get("state") ?? "";

  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const permissions = me.data?.permissions ?? [];
  const canRead = permissions.includes("provisioning.read");
  const canManage = permissions.includes("provisioning.manage");

  // `me` is already fetched for the permission gates above, so the profile
  // timezone costs nothing extra (cycle 5).
  const zone = me.data?.timezone ?? null;

  const images = useOsImages();
  const items = images.data ?? [];
  const activeCount = items.filter((image) => image.state === "active").length;
  // What the totals are made of, for the tiles' second lines: the platforms
  // the catalog covers, and how much of the active set is a golden image
  // rather than an answer file.
  const platforms = new Set(items.map((image) => image.os_family)).size;
  const visible =
    stateFilter === "active"
      ? items.filter((image) => image.state === "active")
      : stateFilter === "inactive"
        ? items.filter((image) => image.state !== "active")
        : items;

  return (
    <div>
      <PageHead
        title={t("provisioning.images.title")}
        description={t("provisioning.images.description")}
        actions={
          canManage ? (
            <Btn label={t("provisioning.images.newImage")} icon="upload" variant="primary" onClick={() => setCreateOpen(true)} />
          ) : undefined
        }
      />

      <ProvisioningPageNav canRead={canRead} />

      {/* An image that is registered but not active cannot be built from, so
          "Active" is the number that says whether provisioning has anything to
          work with: green, with a zero that says which zero it is. The total
          is the route back to the unfiltered list, and "Not active" is the
          number that says how much of the catalog cannot be built from.

          THREE TILES, NOT TWO (review wave, round 2, harmony): two tiles
          stretched to half the content width each and left a 26px figure
          floating in a mostly empty card, out of rhythm with the 3- and
          4-column rows the rest of the console draws. Each tile lands on a
          slice the filter below it can also reach, so the third is a real
          view and not a spacer. */}
      <StatsGrid columns={3}>
        <StatCard
          label={t("provisioning.images.tileImages")}
          value={String(items.length)}
          icon="photo"
          to="/provisioning/images"
          tone="neutral"
          zeroLabel={t("provisioning.images.noImageRegistered")}
          foot={
            items.length === 0
              ? undefined
              : t("provisioning.images.acrossPlatforms", { count: platforms })
          }
        />
        <StatCard
          label={t("provisioning.images.tileActive")}
          value={String(activeCount)}
          icon="circle-check"
          to="/provisioning/images?state=active"
          tone="green"
          zeroLabel={
            items.length === 0
              ? t("provisioning.images.noImageToActivate")
              : t("provisioning.images.noneActive")
          }
          foot={activeCount === 0 ? undefined : t("provisioning.images.readyToBuild")}
        />
        <StatCard
          label={t("provisioning.images.tileNotActive")}
          value={String(items.length - activeCount)}
          icon="minus"
          to="/provisioning/images?state=inactive"
          tone="neutral"
          zeroLabel={
            items.length === 0 ? t("provisioning.images.noImageIsRegistered") : t("provisioning.images.everyActive")
          }
          foot={
            items.length - activeCount === 0 ? undefined : t("provisioning.images.cannotBuild")
          }
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
            aria-label={t("provisioning.images.filterAria")}
          >
            <option value="">{t("provisioning.images.allImages")}</option>
            <option value="active">{t("provisioning.images.tileActive")}</option>
            <option value="inactive">{t("provisioning.images.tileNotActive")}</option>
          </select>
          <span className="filter-spacer" />
        </FilterBar>
        <QueryState
          loading={images.isLoading}
          error={images.error}
          empty={visible.length === 0}
          emptyLabel={
            items.length === 0 ? t("provisioning.images.emptyNone") : t("provisioning.images.emptyFiltered")
          }
          emptyIcon="photo"
        >
          <Table>
            <thead>
              <tr>
                <Th>{t("provisioning.images.colName")}</Th>
                <Th>{t("provisioning.images.colPlatform")}</Th>
                <Th>{t("provisioning.images.colKind")}</Th>
                <Th>{t("provisioning.images.colState")}</Th>
                <Th>{t("provisioning.images.colAdded")}</Th>
              </tr>
            </thead>
            <tbody>
              {visible.map((image) => (
                <tr key={image.id}>
                  <Td lead={<Icon name="photo" size={18} />}>
                    <div className="cell-title">{image.name}</div>
                    {/* The hash the run verifies, or the fact that there is
                        no artifact yet: a build from a hashless image fails
                        at PXE time rather than at edit time. */}
                    <div className="cell-sub">
                      {image.artifact_sha256 ? (
                        <span className="mono">
                          {t("provisioning.images.sha", { sha: shortSha(image.artifact_sha256) })}
                        </span>
                      ) : (
                        t("provisioning.images.noFile")
                      )}
                    </div>
                  </Td>
                  {/* Platform is a property of the image, not a state of
                      it: the brand glyph and the platform's name, not the
                      info-circle pill this column used to wear. */}
                  <Td lead={<OsIcon os={image.os_family} />}>{osFamilyLabel(image.os_family)}</Td>
                  {/* `kind` reaches a NOT NULL column as free text, so this
                      humanises rather than maps: "answer_file" was printed
                      verbatim, underscore and all, in a column whose
                      neighbours are prose. */}
                  <Td>{humaniseEnum(image.kind, "OsImageKind")}</Td>
                  <Td>
                    <StatusPill
                      value={image.state}
                      kind={image.state === "active" ? "ok" : "mut"}
                      label={osImageStateLabel(image.state)}
                    />
                  </Td>
                  <Td>{formatDateTime(image.created_at, zone)}</Td>
                </tr>
              ))}
            </tbody>
          </Table>
        </QueryState>
      </Panel>

      {/* KEYED so a refusal cannot outlive the dialog that produced it: this
          component is never unmounted (only `Dialog` returns null when
          closed), so a failed create left its alert sitting there for the next
          opening, describing a request the operator had abandoned. */}
      <CreateImageDialog
        key={createOpen ? "open" : "closed"}
        open={createOpen}
        onClose={() => setCreateOpen(false)}
      />
    </div>
  );
}

/**
 * What an image may not be registered without.
 *
 * `OsImageCreateRequest` requires `name`, `os_family` and `kind`. Only the
 * first two can be left empty from this dialog (OS family is a closed select
 * with a default), and `kind` is free text that reaches a NOT NULL column, so
 * both are rules rather than a `disabled` attribute that explains nothing.
 */
const imageRules = (): readonly FieldRule[] => [
  { name: "name", label: translate("provisioning.imageDialog.name") },
  { name: "kind", label: translate("provisioning.imageDialog.kind") },
];

function CreateImageDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const t = useT();
  const create = useCreateOsImageMutation();
  const [osFamily, setOsFamily] = useState("windows");
  const [name, setName] = useState("");
  const [kind, setKind] = useState("install");
  // Populated ON SUBMIT, never while typing. Held as data (rules and values) so
  // a language switch re-words the sentences.
  const [attempt, setAttempt] = useState<Attempt | null>(null);

  function reset() {
    setName("");
    setOsFamily("windows");
    setKind("install");
    setAttempt(null);
  }

  function submit() {
    // The previous outcome must not be readable as this one.
    create.reset();
    const next: Attempt = { rules: imageRules, values: { name, kind } };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    create.mutate(
      { os_family: osFamily, name: name.trim(), kind: kind.trim() },
      {
        onSuccess: () => {
          reset();
          onClose();
        },
      },
    );
  }

  return (
    <Dialog open={open} onClose={onClose} title={t("provisioning.imageDialog.title")}>
      {/* A real <form>, so Enter in Name registers the image. It did nothing
          before: there was no <form> on this dialog at all. */}
      <Form onSubmit={submit}>
        <div className="form-grid">
          <Field label={t("provisioning.imageDialog.name")}>
            <input className={inputClass} value={name} onChange={(e) => setName(e.target.value)} />
          </Field>
          <Field label={t("provisioning.imageDialog.osFamily")}>
            <select
              className={inputClass}
              value={osFamily}
              onChange={(e) => setOsFamily(e.target.value)}
            >
              <option value="windows">Windows</option>
              <option value="linux">Linux</option>
              <option value="macos">macOS</option>
            </select>
          </Field>
          <Field label={t("provisioning.imageDialog.kind")}>
            <input className={inputClass} value={kind} onChange={(e) => setKind(e.target.value)} />
          </Field>
        </div>
        <div className="flex flex-wrap items-center justify-end gap-2">
          {/* `type="button"` is load-bearing: `ui.tsx`'s Button sets no type,
              and an untyped button inside a form submits it, so Cancel would
              register the image. */}
          <Button type="button" variant="ghost" onClick={onClose}>
            {t("common.actions.cancel")}
          </Button>
          {/* `String(create.error)` printed "ApiError: " for a bodyless 503
              and the raw Pydantic payload for a 422, cycle 4's two named
              defects at once. `FormActions` routes both through
              `describeSubmitError` into a role="alert", so a screen reader is
              told the create failed rather than the operator discovering it
              from a list that never grew. Create is no longer disabled on an
              empty Name; the click names the field. */}
          <FormActions
            label={t("provisioning.imageDialog.create")}
            pendingLabel={t("provisioning.imageDialog.creating")}
            pending={create.isPending}
            error={create.error}
            problems={attemptProblems(attempt)}
          />
        </div>
      </Form>
    </Dialog>
  );
}
