/**
 * Provisioning profiles, on the v3 kit: the design's
 * `P['provisioning/profiles']` head and one bare panel of rows led by the
 * file glyph, the image the profile lays down under its name, the two
 * credential facts as pills, and the profile's state.
 *
 * Stands in for the design's "Task sequences" tab (see provisioningNav.tsx):
 * a `ProvisionProfile` binds an image to post-config and a target scope, the
 * closest entity this app has to the design's task-sequence card. The
 * design's rows also print how many runs used each profile; no endpoint
 * counts that, so the column is OMITTED rather than invented.
 */

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import type { Me } from "../../lib/api";
import { useSearchParams } from "react-router-dom";
import { useApi } from "../../lib/ApiContext";
import { Icon } from "../../shell/icons";
import { useOsImages, useProvisionProfiles } from "./api";
import { imagePickerOptions, nameForOption } from "./pickerOptions";
import { ProfileEditor } from "./ProfileEditor";
import type { ProvisionProfile } from "./types";
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
import { useEverywherePermissions } from "../../lib/mePermissions";

export function ProfilesPage() {
  const t = useT();
  const api = useApi();
  const [editing, setEditing] = useState<ProvisionProfile | "new" | null>(null);
  // In the URL, so the "Enabled" tile opens the rows it counted.
  const [params, setParams] = useSearchParams();
  const enabledFilter = params.get("enabled") ?? "";

  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const permissions = me.data?.permissions ?? [];
  const canRead = permissions.includes("provisioning.read");
  // Configuration every client shares (GLOBAL_WRITES): the code on EVERY client (phase 3 design §2).
  const canManage = useEverywherePermissions().has("provisioning.manage");

  const profiles = useProvisionProfiles();
  // The image a profile lays down is the fact that tells two profiles apart,
  // and the table named it by UUID. The list is cached under the same key the
  // editor's picker reads, so this costs nothing extra once either has run;
  // `nameForOption` falls back to the id, never to blank.
  const images = useOsImages();
  const imageOptions = imagePickerOptions(images.data ?? []);
  const items = profiles.data ?? [];
  const enabledCount = items.filter((profile) => profile.enabled).length;
  // What the total is made of, for the first tile's second line: how many
  // distinct images these profiles lay down.
  const imagesInUse = new Set(items.map((profile) => profile.image_id)).size;
  const visible =
    enabledFilter === "true"
      ? items.filter((profile) => profile.enabled)
      : enabledFilter === "false"
        ? items.filter((profile) => !profile.enabled)
        : items;

  return (
    <div>
      <PageHead
        title={t("provisioning.profiles.title")}
        description={t("provisioning.profiles.description")}
        actions={
          canManage ? (
            <Btn label={t("provisioning.profiles.newProfile")} icon="plus" variant="primary" onClick={() => setEditing("new")} />
          ) : undefined
        }
      />

      <ProvisioningPageNav canRead={canRead} />

      {/* A disabled profile provisions nothing, so "Enabled" is the number
          that says whether a machine could actually be built today, and
          "Disabled" is what is configured but inert.

          THREE TILES, NOT TWO: the same two-up row the review filed on
          /provisioning/images and /backup/policies -- a 26px figure floating
          in half a card. Not filed on this page, and fixed here too because
          it is the identical row in the same group. */}
      <StatsGrid columns={3}>
        <StatCard
          label={t("provisioning.profiles.tileProfiles")}
          value={String(items.length)}
          icon="file-text"
          to="/provisioning/profiles"
          tone="neutral"
          zeroLabel={t("provisioning.profiles.noneConfigured")}
          foot={
            items.length === 0
              ? undefined
              : t("provisioning.profiles.layingDown", { count: imagesInUse })
          }
        />
        <StatCard
          label={t("provisioning.profiles.tileEnabled")}
          value={String(enabledCount)}
          icon="circle-check"
          to="/provisioning/profiles?enabled=true"
          tone="green"
          zeroLabel={
            items.length === 0
              ? t("provisioning.profiles.noProfileToEnable")
              : t("provisioning.profiles.everyDisabled")
          }
          foot={enabledCount === 0 ? undefined : t("provisioning.profiles.readyToProvision")}
        />
        <StatCard
          label={t("provisioning.profiles.tileDisabled")}
          value={String(items.length - enabledCount)}
          icon="minus"
          to="/provisioning/profiles?enabled=false"
          tone="neutral"
          zeroLabel={
            items.length === 0 ? t("provisioning.profiles.noProfileToDisable") : t("provisioning.profiles.everyEnabled")
          }
          foot={
            items.length - enabledCount === 0 ? undefined : t("provisioning.profiles.provisioningNothing")
          }
        />
      </StatsGrid>

      <Panel bare>
        <FilterBar>
          <select
            className="select-field"
            value={enabledFilter}
            onChange={(event) => {
              const next = new URLSearchParams(params);
              if (event.target.value === "") next.delete("enabled");
              else next.set("enabled", event.target.value);
              setParams(next);
            }}
            aria-label={t("provisioning.profiles.filterAria")}
          >
            <option value="">{t("provisioning.profiles.allProfiles")}</option>
            <option value="true">{t("provisioning.profiles.tileEnabled")}</option>
            <option value="false">{t("provisioning.profiles.tileDisabled")}</option>
          </select>
          <span className="filter-spacer" />
        </FilterBar>
        <QueryState
          loading={profiles.isLoading}
          error={profiles.error}
          empty={visible.length === 0}
          emptyLabel={
            items.length === 0
              ? t("provisioning.profiles.emptyNone")
              : t("provisioning.profiles.emptyFiltered")
          }
          emptyIcon="file-text"
        >
          <Table>
            <thead>
              <tr>
                <Th>{t("provisioning.profiles.colName")}</Th>
                <Th>{t("provisioning.profiles.colCredentials")}</Th>
                <Th>{t("provisioning.profiles.colState")}</Th>
                <Th>{""}</Th>
              </tr>
            </thead>
            <tbody>
              {visible.map((profile) => (
                <tr key={profile.id}>
                  <Td lead={<Icon name="file-text" size={18} />}>
                    <div className="cell-title">{profile.name}</div>
                    <div className="cell-sub">{nameForOption(imageOptions, profile.image_id)}</div>
                  </Td>
                  {/* Whether a credential is SET, never its value: the API
                      returns only these two booleans (invariant 3). */}
                  <Td>
                    <div className="flex flex-wrap gap-1">
                      <StatusPill
                        value={profile.has_domain_join_credential ? "set" : "unset"}
                        kind={profile.has_domain_join_credential ? "ok" : "mut"}
                        label={
                          profile.has_domain_join_credential
                            ? t("provisioning.profiles.domainJoinSet")
                            : t("provisioning.profiles.domainJoinNotSet")
                        }
                      />
                      <StatusPill
                        value={profile.has_local_admin_credential ? "set" : "unset"}
                        kind={profile.has_local_admin_credential ? "ok" : "mut"}
                        label={
                          profile.has_local_admin_credential
                            ? t("provisioning.profiles.localAdminSet")
                            : t("provisioning.profiles.localAdminNotSet")
                        }
                      />
                    </div>
                  </Td>
                  <Td>
                    <StatusPill
                      value={profile.enabled ? "enabled" : "disabled"}
                      kind={profile.enabled ? "ok" : "mut"}
                    />
                  </Td>
                  <Td>
                    {canManage && <Btn label={t("provisioning.profiles.edit")} icon="edit" small onClick={() => setEditing(profile)} />}
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        </QueryState>
      </Panel>

      {/* `key` IS LOAD-BEARING, and it fixes a silent destruction of the
          operator's data.

          `ProfileEditor` seeds all nine of its fields with `useState(existing?
          .… ?? default)`. This page mounts it ONCE, on first render, when
          `editing` is still null, so every initializer ran against `null`,
          and React never re-runs an initializer for a component that stays
          mounted. Pressing Edit therefore opened a BLANK editor for a
          populated profile.

          That is not merely cosmetic, because `PUT /provisioning/profiles/
          {id}` is a full-body upsert. The editor's submit deletes
          `domain_join.domain` and `.username` when its (empty) fields are
          empty, and posts `enabled` from state, which defaulted to `true`.
          So retyping the name over a blank form and saving silently dropped
          the domain-join configuration and re-enabled a profile the operator
          had disabled, and the dialog closed reporting success. Same family
          as the swallowed failures this cycle is closing: the interface said
          the operation went well while the record lost data.

          Keying on the profile's identity gives each profile its own editor
          instance, so the initializers run against the profile actually being
          edited. "new" is its own key so New profile always opens empty. */}
      <ProfileEditor
        key={editing === "new" ? "new" : (editing?.id ?? "none")}
        profile={editing}
        onClose={() => setEditing(null)}
      />
    </div>
  );
}
