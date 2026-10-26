import { useMutation, useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useLocation } from "react-router-dom";

import { ConfirmDialog } from "../../components/confirm";
import { useT } from "../../i18n/context";
import { Trans } from "../../i18n/Trans";
import { describeSubmitError } from "../../components/form";
import { NoValue } from "../../components/emptystate";
import { Button, Dialog, Table, Td, Th } from "../../components/ui";
import type { AgentInstallCommand, AgentRelease, Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { formatDateTime, intlLocale } from "../../lib/datetime";
import { internalSiteId, requireKnown } from "../../lib/internalClient";
import { PageHead, Panel, QueryState, StatCard, StatsGrid, StatusPill } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { OsIcon } from "../../shell/viz";
import { AgentUpdatePrerequisites } from "./AgentUpdatePrerequisites";
import { AgentUpdatesSection } from "./AgentUpdatesSection";
import { InstallLinkPanel, LINK_MANAGE } from "./InstallLinkPanel";
import { ManagedDeploymentDialog } from "./ManagedDeploymentDialog";
import { platformLabel, untestedCaveat } from "./platform";
import { PublishReleaseDialog } from "./PublishReleaseDialog";
import { ChannelPill, ReleaseChannels, compareVersions } from "./ReleaseChannels";

/** The lifetime this page asks for, in hours. Stated in the confirmation
 *  because "how long is this credential alive" is the thing the operator is
 *  actually agreeing to. */
const TOKEN_TTL_HOURS = 4;

/**
 * Copying without swallowing the failure.
 *
 * `navigator.clipboard` is undefined outside a secure context and rejects when
 * permission is denied. The command below carries a live enrollment token as a
 * literal, so an operator who believes it is on the clipboard and pastes
 * nothing into a remote shell has lost the token to the dialog they then
 * closed. (The same helper exists in `modules/enrollment`; it is duplicated
 * rather than shared because `components/` is not this module's to extend --
 * see the note to the controller.)
 */
async function copyToClipboard(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

function formatBytes(bytes: number): string {
  if (bytes <= 0) return "—";
  const units = ["B", "KiB", "MiB", "GiB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

/**
 * Scroll the catalogue a stat tile points at into view.
 *
 * React Router pushes a fragment and stops — it does not scroll, and `App.tsx`
 * wires no scroll restoration. Without this the three tiles would change the
 * URL and nothing on screen, which is the dead tile this cycle removes.
 */
function useScrollToCatalogue(rendered: boolean) {
  const { hash } = useLocation();
  useEffect(() => {
    // `rendered` matters: the table is behind `QueryState`, so a cold arrival
    // at /agent-packages#releases runs this while the page still says
    // "Loading…" and `#releases` does not exist yet.
    if (!hash || !rendered) return;
    const target = document.getElementById(decodeURIComponent(hash.slice(1)));
    // jsdom does no layout and ships no `scrollIntoView`; a TypeError here
    // would take the whole page down.
    if (target && typeof target.scrollIntoView === "function") {
      target.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }, [hash, rendered]);
}

/**
 * WHAT AN EMPTY CATALOGUE MEANS, AND WHO IT SPEAKS TO.
 *
 * "No agent packages" was filed as a fault on the deployed console. It is not
 * one, and a bare "No agent packages published yet." was still the wrong
 * answer: a zero that explains nothing leads nowhere, which is the dead-tile
 * rule one level up. But the first fix over-corrected — it printed the whole
 * PUBLISHING procedure (a Go build, two `curl` calls, a local `sha256sum`) on
 * the screen an operator reaches to INSTALL. "the steps to build an agent
 * shouldn't be there, just the installation packages" — the operator, on the
 * deployed console.
 *
 * SO THIS SCREEN NOW SPLITS BY AUDIENCE. The catalogue is empty because the
 * platform never builds, fetches or pushes the agent (spec §1): a publisher
 * uploads what the `release-agent` workflow built. Publishing needs
 * `agent.release.manage`, which since migration 0070 belongs to the
 * platform-operator role and is not grantable from any org-scoped console.
 *
 * The installing operator is told that, plainly, and pointed at nobody's build
 * recipe. Only a holder of `agent.release.manage` — the one person who could
 * act on it — sees the "Publish a release…" button in the page head, and the
 * runbook pointer: the workflow run and the download of its draft release
 * happen outside this console, and `deploy/README.md` is where those steps
 * are written down.
 */
function EmptyCatalogue({ canPublish }: { canPublish: boolean }) {
  const t = useT();
  return (
    <div className="empty">
      <div>
        <span className="empty-icon">
          <Icon name="package" size={20} />
        </span>
        {/* The exact sentence the page has always shown, kept verbatim: it is
            the honest headline. */}
        <h3>{t("agentReleases.empty.headline")}</h3>
        <p>
          <Trans k="agentReleases.empty.body" components={{ strong: <strong /> }} />
        </p>

        {/* AUDIENCE SPLIT. An operator who only installs is pointed at no
            publish mechanics at all -- not the recipe, not even the runbook
            link. The publisher, and only the publisher, gets the pointer.

            NOT `<p>` FOR THE LINE BELOW: `components.css` sets
            `.empty p { max-width: 370px; margin: … auto }` at a specificity
            Tailwind's utilities cannot override, so a paragraph here would
            render as a 370px column centred inside the 640px left-aligned
            block. The one `<p>` above keeps that treatment deliberately -- it
            is the house empty-state sentence and 370px is the measure it is
            designed for. */}
        {canPublish ? (
          <div className="note mx-auto mt-4 max-w-[640px] text-left">
            <Trans k="agentReleases.empty.canPublish" components={{ strong: <strong />, code: <code /> }} />
          </div>
        ) : (
          /* NOT A GUESS ABOUT SECURITY, AND NOT A GATE EITHER. The frontend
             never gates security (invariant 6) and this hides nothing the
             routes do not already refuse server-side. It exists so an operator
             is not sent to do a publisher's work that will end in a 403. */
          <div className="note mx-auto mt-4 max-w-[640px] text-left">{t("agentReleases.empty.cannotPublish")}</div>
        )}
      </div>
    </div>
  );
}

export function AgentReleasesPage() {
  const t = useT();
  const api = useApi();
  const [command, setCommand] = useState<AgentInstallCommand | null>(null);
  /**
   * The release whose install command has been ASKED for and not yet minted.
   *
   * CYCLE 8. "Install command" reads like a disclosure — show me the command —
   * and it is not: it calls `POST /agent/releases/{id}/install-command`, which
   * mints a live single-use enrollment token and embeds it in the text. That is
   * the same shape as the audit's [HIGH] on /enrollment ("Create token mints a
   * live enrollment credential instantly"), one screen over, and it is
   * confirmed here for the same reason.
   */
  const [pendingRelease, setPendingRelease] = useState<AgentRelease | null>(null);
  /**
   * The release whose MANAGED deployment is open. A separate piece of state
   * from the one above, and a separate dialog, because they are different
   * acts: "Install command" mints a credential on the spot, and "Managed
   * deployment" opens an inert package whose credential is a second, confirmed
   * step inside it.
   */
  const [managedRelease, setManagedRelease] = useState<AgentRelease | null>(null);
  const [copied, setCopied] = useState<"idle" | "done" | "failed">("idle");
  const [publishing, setPublishing] = useState(false);
  const [showOlder, setShowOlder] = useState(false);

  // THE SAME `["me"]` QUERY THE SHELL ALREADY HOLDS, read here only for the
  // profile timezone (cycle 5). It is a cache hit in the running app; it is
  // fetched at all because the token expiry below is the one figure on this
  // screen a person acts on, and "expires 14:05" read five hours early is an
  // operator who abandons a token that is still good.
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const zone = me.data?.timezone;
  // NOT A PERMISSION GATE: the publish and promote routes refuse an
  // under-privileged caller server-side whatever this says (invariant 6). It
  // decides whether the page offers "Publish a release…" and the channel
  // controls, and what the empty state tells the reader.
  const canPublish = me.data?.permissions?.includes("agent.release.manage") ?? false;
  const canReadUpdates = me.data?.permissions?.includes("agent.update.read") ?? false;
  // NOT A PERMISSION GATE (invariant 6): every install-link route checks
  // `enrollment.link.manage` itself. This decides whether the panel renders.
  const canManageLink = me.data?.permissions?.includes(LINK_MANAGE) ?? false;
  // PHASE 3: the client/site selector. The install command's token, the
  // managed deployment's token and the install link each belong to ONE SITE
  // now (MSP clients spec §1.5, §3.2). Until the selector exists this page
  // works on the organisation's internal client, whose "Principal" site has
  // the organisation's id -- `/me`'s `org_id`, from the query above
  // (`lib/internalClient.ts`). `null` until `/me` has answered.
  const internalSite = internalSiteId(me.data);

  const releases = useQuery({
    queryKey: ["agent-releases"],
    queryFn: () => api.listAgentReleases(),
  });

  const generate = useMutation({
    mutationFn: (release: AgentRelease) =>
      // PHASE 3: the client/site selector -- the internal site, above.
      api.generateAgentInstallCommand(release.id, TOKEN_TTL_HOURS, requireKnown(internalSite)),
    onSuccess: (result) => {
      setCopied("idle");
      setCommand(result);
    },
  });

  // NEWEST VERSION FIRST, THEN PLATFORM, AND OLDER VERSIONS FOLDED AWAY.
  // Until 2026-09-24 the table sorted by platform, so publishing 1.5.0 beside
  // 1.4.0 interleaved them (1.4.0 Mac, 1.5.0 Mac, 1.4.0 Mac…) and the operator
  // called it "not organized". The newest version is the one anybody installs;
  // older ones stay one click away for a machine that needs a specific build,
  // and "Release channels" below removes them. Sorting a COPY leaves the query
  // cache untouched (`Array.sort` mutates in place).
  const allItems = [...(releases.data ?? [])].sort(
    (a, b) =>
      compareVersions(a.version, b.version) ||
      a.os.localeCompare(b.os, intlLocale()) ||
      a.arch.localeCompare(b.arch, intlLocale()),
  );
  const newest = allItems[0]?.version;
  const olderVersions = [...new Set(allItems.map((r) => r.version))].filter((v) => v !== newest);
  const items = showOlder ? allItems : allItems.filter((r) => r.version === newest);
  // Counted from the rows on screen. The design's mock shows fixed figures;
  // a console that kept them would be wrong on every deployment.
  const withArtifact = allItems.filter((release) => release.has_artifact).length;
  const platforms = new Set(allItems.map((release) => release.os)).size;

  useScrollToCatalogue(releases.data != null);

  return (
    <>
      {/* THE PAGE'S ONE PRIMARY, and only for the one person who can use it.
          v3 keeps a page's primary in the head; the rows below stay plain. */}
      <PageHead
        title={t("agentReleases.page.title")}
        description={t("agentReleases.page.description")}
        actions={
          canPublish ? (
            <Button onClick={() => setPublishing(true)}>
              <Icon name="upload" size={14} />
              <span>{t("agentReleases.page.publish")}</span>
            </Button>
          ) : undefined
        }
      />

      {/* What a rollout needs from elsewhere, for the people the updates
          section below is shown to. */}
      {canReadUpdates && <AgentUpdatePrerequisites />}

      {/* WHERE THESE NUMBERS LIVE, AND WHY ALL THREE LAND IN THE SAME PLACE.

          `GET /agent/releases` is served here and rendered by nothing else in
          the console, and this screen is one unfiltered table — there is no
          `?os=` and no artefact filter for a tile to set. So all three tiles
          open the catalogue, and that is stated rather than disguised: giving
          each tile a distinct href would have meant inventing two filters
          nobody asked for so a row of numbers could look busier. The fragment
          is what keeps the click from being a no-op (see
          `useScrollToCatalogue`).

          COLOUR IS CATEGORY, and the categories here are mild on purpose:
          neutral for the inert catalogue total, green for the subset that can
          actually be installed, blue for the informational platform count.
          NOTHING HERE IS URGENT. A published-package catalogue is a shelf, not
          a queue — no number on this screen means a person has to act now, and
          marking one would spend the only signal the row has on something that
          does not need it. */}
      <StatsGrid columns={3}>
        <StatCard
          label={t("agentReleases.page.releases")}
          value={releases.data ? String(allItems.length) : "—"}
          icon="package"
          to="/agent-packages#releases"
          tone="neutral"
          zeroLabel={t("agentReleases.page.releasesZero")}
        />
        <StatCard
          label={t("agentReleases.page.ready")}
          value={releases.data ? String(withArtifact) : "—"}
          icon="check"
          to="/agent-packages#releases"
          tone="green"
          /* A release with no uploaded artefact cannot produce an install
             command at all, so this zero is not "none counted" — it is a
             catalogue nobody can install from. */
          zeroLabel={t("agentReleases.page.readyZero")}
        />
        <StatCard
          label={t("agentReleases.page.platforms")}
          value={releases.data ? String(platforms) : "—"}
          icon="devices"
          to="/agent-packages#releases"
          tone="blue"
          zeroLabel={t("agentReleases.page.platformsZero")}
        />
      </StatsGrid>

      {/* The install link (spec 2026-09-28 §1.1): one shared link, off by
          default, for technicians setting up machines by hand. */}
      {/* PHASE 3: the client/site selector. One link per site; this is the
          internal client's "Principal" site's, and it renders once `/me` has
          said which that is -- the same answer `canManageLink` waits on. */}
      {canManageLink && internalSite !== null && (
        <InstallLinkPanel zone={zone} siteId={internalSite} />
      )}

      {/* THE FRAGMENT TARGET IS A REAL ELEMENT. `Panel` takes no id, so the
          anchor is the wrapper; `#releases` pointing at nothing scrolls
          nowhere, which is a dead tile wearing an href. */}
      <div id="releases">
        <Panel
          title={t("agentReleases.page.catalogue")}
          subtitle={
            releases.data
              ? newest && olderVersions.length > 0 && !showOlder
                ? t("agentReleases.page.newestSubtitle", { version: newest, count: items.length })
                : t("agentReleases.page.allSubtitle", {
                    count: allItems.length,
                    platforms: t("agentReleases.page.platformCount", { count: platforms }),
                  })
              : undefined
          }
          bare
        >
          {/* `empty` IS FALSE ON PURPOSE, and the emptiness is handled below.
              `QueryState`'s empty state is one sentence and this page needs a
              procedure — see `EmptyCatalogue`. Loading and error stay with
              `QueryState`, because "did not load" and "nobody has published"
              are the two states an operator must never confuse, and only the
              shared component distinguishes them consistently. */}
          <QueryState
            loading={releases.isLoading}
            error={releases.error}
            empty={false}
            emptyLabel={t("agentReleases.empty.headline")}
            emptyIcon="package"
          >
            {allItems.length === 0 ? (
              <EmptyCatalogue canPublish={canPublish} />
            ) : (
            <Table>
              <thead>
                <tr>
                  <Th>{t("agentReleases.table.platform")}</Th>
                  <Th>{t("agentReleases.table.version")}</Th>
                  <Th>{t("agentReleases.table.channel")}</Th>
                  <Th>{t("agentReleases.table.size")}</Th>
                  <Th>SHA-256</Th>
                  {/* TWO DIFFERENT SIGNATURES, named apart. "Code signing" is
                      Authenticode / Developer ID -- the publisher's claim,
                      `signed`. "Release signature" is the offline Ed25519 key
                      the release workflow holds, which agents check before
                      they self-update -- `release_signed`. */}
                  <Th>{t("agentReleases.table.codeSigning")}</Th>
                  <Th>{t("agentReleases.table.releaseSignature")}</Th>
                  <Th>{""}</Th>
                </tr>
              </thead>
              <tbody>
                {items.map((release) => {
                  const caveat = untestedCaveat(release.os);
                  return (
                    <tr key={release.id}>
                      {/* v3's row: the platform's brand glyph leads, the
                          platform is the title, the never-executed caveat
                          the sub-line. The design's sub-line also names the
                          artefact file; the API carries no filename, so none
                          is invented here. */}
                      <Td lead={<OsIcon os={release.os} />}>
                        <div className="cell-title">{platformLabel(release.os, release.arch)}</div>
                        <div className="cell-sub mono">
                          {release.os}/{release.arch}
                        </div>
                        {caveat != null ? (
                          <div className="cell-sub">
                            <StatusPill value="untested" kind="warn" label={t("agentReleases.table.notTested")} /> {caveat}
                          </div>
                        ) : null}
                      </Td>
                      {/* Mono values stay on one line, as v3 draws them: a
                          version or a size that breaks across two lines is
                          two tokens an operator has to reassemble. The
                          platform column is the one that gives. (Kit gap,
                          reported: `Td` takes no `style`, and `td.mono` does
                          not keep its line, so this is the utility class.) */}
                      <Td className="mono whitespace-nowrap">{release.version}</Td>
                      <Td>
                        <ChannelPill channel={release.channel ?? "pilot"} />
                      </Td>
                      <Td className="mono whitespace-nowrap">
                        {release.size_bytes > 0 ? (
                          formatBytes(release.size_bytes)
                        ) : (
                          <NoValue label={t("agentReleases.table.noFileSize")} />
                        )}
                      </Td>
                      <Td className="mono">
                        {/* Truncated for width. The dialog shows it in full,
                            because comparing it against a local `sha256sum` is
                            the point of having it at all. */}
                        {release.sha256 ? (
                          `${release.sha256.slice(0, 12)}…`
                        ) : (
                          <NoValue label={t("agentReleases.table.noFileChecksum")} />
                        )}
                      </Td>
                      <Td>
                        {/* COLOUR IS NEVER THE ONLY CARRIER: the pill says
                            "Signed" or "Unsigned" and draws its kind's glyph. */}
                        {release.signed ? (
                          <StatusPill value="verified" kind="ok" label={t("agentReleases.table.signed")} />
                        ) : (
                          <StatusPill value="unsigned" kind="warn" label={t("agentReleases.table.unsigned")} />
                        )}
                      </Td>
                      <Td>
                        {release.release_signed ? (
                          <StatusPill value="recorded" kind="ok" label={t("agentReleases.table.recorded")} />
                        ) : (
                          <StatusPill value="missing" kind="warn" label={t("agentReleases.table.notRecorded")} />
                        )}
                      </Td>
                      <Td>
                        {!release.has_artifact && (
                          <div className="cell-sub">{t("agentReleases.table.noFile")}</div>
                        )}
                        {/* The ellipsis is the promise that a further step
                            follows -- this click asks, the confirmation mints.

                            NO PRIMARY IN THE ROW. v3 draws both per-row
                            actions ("Download", "Copy install command") as
                            plain buttons and keeps a page's one primary in
                            the head -- "Publish a release…", for a publisher
                            only -- so the body carries no primary at all
                            rather than painting one per row. The one-machine
                            install still comes first, and still mints on
                            confirm.

                            ONE ROW, NOT WRAPPED: v3's row is a single line
                            with the buttons inline; letting them stack made
                            every row twice its height. */}
                        <div className="flex flex-nowrap justify-end gap-2">
                          <Button
                            variant="outline"
                            disabled={!release.has_artifact || generate.isPending}
                            onClick={() => {
                              generate.reset();
                              setPendingRelease(release);
                            }}
                          >
                            <Icon name="copy" size={14} />
                            <span>{t("agentReleases.table.installCommand")}</span>
                          </Button>
                          {/* THE FLEET ANSWER, BESIDE THE ONE-MACHINE ANSWER.
                              The install command is deliberately for one
                              machine at a time -- its token is single-use and
                              lands in a shell history -- and until now the page
                              said so with nothing to offer instead. This is the
                              instead: an artefact and a procedure the estate's
                              own distribution mechanism consumes, carrying no
                              credential at all. */}
                          <Button
                            variant="outline"
                            disabled={!release.has_artifact}
                            onClick={() => setManagedRelease(release)}
                          >
                            <Icon name="download" size={14} />
                            <span>{t("agentReleases.table.managed")}</span>
                          </Button>
                        </div>
                      </Td>
                    </tr>
                  );
                })}
              </tbody>
            </Table>
            )}
            {olderVersions.length > 0 && (
              <div className="panel-b" style={{ borderTop: "1px solid var(--line)" }}>
                <Button variant="ghost" onClick={() => setShowOlder((open) => !open)}>
                  {showOlder
                    ? t("agentReleases.page.hideOlder")
                    : t("agentReleases.page.showOlder", { versions: olderVersions.join(", ") })}
                </Button>
              </div>
            )}
          </QueryState>
        </Panel>
      </div>

      {/* Promotion, for a publisher: per version, because the route is. */}
      {/* `#channels` is where "Mark a release as stable" lands. */}
      {canPublish && releases.data != null && (
        <div id="channels">
          <ReleaseChannels releases={releases.data} />
        </div>
      )}

      {/* Self-update (spec-self-update.md §8.6), for holders of the read code;
          the server enforces every route. */}
      {canReadUpdates && <AgentUpdatesSection me={me.data} />}

      {/* `String(generate.error)` PUT "Error: {\"detail\":[…]}" ON THE SCREEN
          -- and, for the bodyless 503 an ApiError carries as `message: ""`,
          the string "Error: ". Neither is a sentence. `role="alert"` because a
          failure that only changes colour is not announced to a screen
          reader. */}
      {generate.isError && (
        <p role="alert" className="form-error mt-3">
          {describeSubmitError(generate.error)}
        </p>
      )}

      <Dialog
        open={command != null}
        onClose={() => setCommand(null)}
        title={t("agentReleases.command.title")}
      >
        {command != null && (
          <>
            <p>
              <Trans
                k="agentReleases.command.forOne"
                vars={{ date: formatDateTime(command.token_expires_at, zone) }}
                components={{ strong: <strong /> }}
              />
            </p>
            <p className="note mt-2">
              <Trans k="agentReleases.command.shellHistory" components={{ strong: <strong /> }} />
            </p>
            {/* `.code` IS THE DESIGN'S BLOCK FOR THIS, and `JobsPage`, `AuditPage`
                and `Attributes` already render it: 10px radius, 12px mono at 1.7,
                its own dark ground. This was a hand-built approximation beside
                three real ones. */}
            <pre className="code mt-3">
              {command.command}
            </pre>
            {/* THE DIGEST, AND WHAT IT IS FOR. "The hash shown is the one the
                installer verifies" was the middle of a 265-char page
                description, three clicks from the only place the whole digest
                is on screen. */}
            <p className="note mt-3">
              <Trans
                k="agentReleases.command.digest"
                vars={{ sha: command.sha256 }}
                components={{ mono: <span className="mono" /> }}
              />
            </p>
            {/* A DIRECT DOWNLOAD OF THE ARTEFACT, for this release's OS/arch.
                The command above curls the same file, but an operator staging
                the binary by hand should not have to lift it out of a shell
                one-liner. The URL is a presigned GET -- it carries the SHA
                above, not the enrollment token -- so it is safe as a plain
                link. Azure, not magenta: a download informs, it does not commit
                (DESIGN.md, the Two Voices Rule). */}
            <p className="mt-2">
              <a href={command.download_url}>{t("agentReleases.command.download")}</a>{" "}
              <span className="note">
                {t("agentReleases.command.downloadNote", { count: Math.round(command.download_expires_in_s / 60) })}
              </span>
            </p>
            {/* THE COMMAND IS THE ONLY COPY OF A LIVE TOKEN. Selecting it by
                hand out of a scrolling <pre> is where a character goes
                missing, and the failure mode is an enrollment that dies at the
                endpoint with no explanation. */}
            <div className="mt-3 flex items-center gap-2">
              <Button
                variant="outline"
                onClick={() => {
                  void copyToClipboard(command.command).then((ok) =>
                    setCopied(ok ? "done" : "failed"),
                  );
                }}
              >
                {copied === "done" ? t("agentReleases.command.copied") : t("agentReleases.command.copy")}
              </Button>
              {copied === "done" ? (
                <span role="status" className="form-saved">
                  {t("agentReleases.command.copiedStatus")}
                </span>
              ) : null}
              {copied === "failed" ? (
                <span role="alert" className="form-error">
                  {t("agentReleases.command.copyFailed")}
                </span>
              ) : null}
            </div>
          </>
        )}
      </Dialog>

      {/* Names the release, and says what the click actually does: mint a
          credential, not reveal one. Not `destructive` -- nothing is destroyed
          and the token is single-use and four hours long, so spending the red
          here would spend it on the mildest privileged action in the product
          and leave nothing for revoke and erase. */}
      {publishing && <PublishReleaseDialog onClose={() => setPublishing(false)} />}

      <ManagedDeploymentDialog
        release={managedRelease}
        onClose={() => setManagedRelease(null)}
        timezone={zone}
        // PHASE 3: the client/site selector -- the internal site, above.
        siteId={internalSite}
      />

      <ConfirmDialog
        open={pendingRelease != null}
        title={t("agentReleases.confirm.title")}
        subject={
          pendingRelease
            ? `${pendingRelease.version} · ${platformLabel(pendingRelease.os, pendingRelease.arch)}`
            : ""
        }
        body={t("agentReleases.confirm.body", { hours: TOKEN_TTL_HOURS })}
        confirmLabel={t("agentReleases.confirm.confirm")}
        onCancel={() => setPendingRelease(null)}
        onConfirm={() => {
          const release = pendingRelease;
          setPendingRelease(null);
          if (release) generate.mutate(release);
        }}
      />
    </>
  );
}
