// The report catalogue (spec §11, §6.2).
//
// **NOTHING IS HIDDEN.** Every report the deployment can run is listed,
// including the ones this caller may not run -- those are greyed and NAME THE
// MISSING PERMISSION CODES. A catalogue that filtered them out would give an
// under-permissioned operator exactly the same page as a smaller deployment,
// with no way to tell the two apart, and would turn a permissions conversation
// into a support ticket. The codes leak nothing: `requires` is derived from a
// definition compiled into the server image, and the server tells the same
// caller the same thing in the 403 body.
//
// The greying is COSMETIC. `entitled` comes from the server and the server
// refuses regardless; the frontend gates nothing (invariant #6).

import { Link } from "react-router-dom";
import { Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import { Trans } from "../../i18n/Trans";
import { permissionsSentence } from "../../lib/permissions";
import { useReportCatalogue } from "./api";
import { ReportReadiness } from "./ReportReadiness";
import { reportName, reportProse } from "./reportText";
import type { CatalogueEntry } from "./types";
import type { MessageKey } from "../../i18n/core";
import { humaniseKey } from "../../lib/labels";
import {
  FilterChips,
  ListEmpty,
  ListToolbar,
  PageHead,
  Panel,
  QueryState,
  SearchField,
  ShowingCount,
  StatCard,
  StatsGrid,
  useUrlFilters,
} from "../../shell/components";
import { Icon } from "../../shell/icons";
import { KIND_ICON } from "../../shell/viz";

// **EVERY ROW IS AN `<h3>` CARRYING THE REPORT'S NAME**, audit [HIGH]: "Report
// cards in the catalogue have no titles -- the link text is a 200-word
// paragraph ... a screen-reader user listing links hears four paragraph-length
// names and cannot distinguish them ... The actual report names exist only in
// the URLs." Enumerating headings returned `["H1:Reports"]` and nothing else.
//
// The name comes from `entry.key` (`reportText.reportName`), which is the same
// string the URL carries, so the link, the heading and the address agree. The
// SUMMARY -- one line, "what question this report answers" -- becomes the
// supporting text, and the long description follows it.
//
// v3 draws the catalogue as one list: the chart glyph as the lead, the name
// over its summary, and an Open button on the row. The row a caller cannot
// run keeps its heading (a report you cannot run is still a report you have
// to identify to ask for access) and carries the missing codes where the
// runnable row carries the ones it needs.
function EntryRow({ entry }: { entry: CatalogueEntry }) {
  const t = useT();
  const name = reportName(entry.key);
  if (!entry.entitled) {
    return (
      <tr data-testid={`report-card-${entry.key}`} aria-disabled="true">
        <Td lead={<Icon name="chart-bar" size={18} />}>
          <h3 className="cell-title" style={{ color: "var(--muted)" }}>
            {name}
          </h3>
          <div className="cell-sub">{reportProse(entry.summary)}</div>
          <div className="cell-sub">{reportProse(entry.description)}</div>
        </Td>
        <Td>
          {/* The kit's warn notice, not a coloured sentence: the glyph carries
              the state where colour alone would not. */}
          <div className="notice gold">
            <Icon name={KIND_ICON.warn} size={17} />
            <div>
              {t("reporting.catalogue.cannotRun", {
                permissions: permissionsSentence(entry.missing_permissions),
              })}
            </div>
          </div>
        </Td>
        <Td>{null}</Td>
      </tr>
    );
  }
  return (
    <tr data-testid={`report-card-${entry.key}`}>
      <Td lead={<Icon name="chart-bar" size={18} />}>
        <h3 className="cell-title">
          <Link to={`/reports/${entry.key}`}>{name}</Link>
        </h3>
        <div className="cell-sub">{reportProse(entry.summary)}</div>
        <div className="cell-sub">{reportProse(entry.description)}</div>
      </Td>
      <Td>
        <span className="note">
          {t("reporting.catalogue.needs", { permissions: permissionsSentence(entry.requires) })}
        </span>
      </Td>
      <Td>
        <Link className="btn small" to={`/reports/${entry.key}`} aria-label={t("reporting.catalogue.openAria", { name })}>
          <Icon name="arrow-right" size={14} />
          {t("reporting.catalogue.open")}
        </Link>
      </Td>
    </tr>
  );
}

// WHAT THE DESIGN ASKS FOR THAT DOES NOT EXIST (UI port spec §7, and this
// task's own reporting-specific instruction). The prototype's "BI datasets"
// tab lists four datasets -- inventory, patch, service, financial -- each
// marked "Ready" with a sample `GET /api/v1/reporting/datasets/inventory`
// call. NONE OF THAT IS REAL. Spec §0.11 states plainly that scheduled/bulk
// delivery ships nothing in v1 "because the only delivery substrate would
// leak the conjunction" (§0.4), and §6.2's route table has exactly six
// routes: the catalogue, the four reports, and one CSV export route PER
// report -- no `/datasets` prefix, no bulk export, at all. Claiming those
// four datasets are "Ready" would be exactly the mock-content-as-fact defect
// the UI port spec calls out. What is real: each report below exports its
// own bounded CSV once it has been run, gated by the same conjunction as the
// report itself -- so this panel says that, and nothing more.
function BiExportNotice() {
  const t = useT();
  return (
    <Panel title={t("reporting.catalogue.bulkTitle")}>
      <p className="note">{t("reporting.catalogue.bulkNote")}</p>
    </Panel>
  );
}

// THE THREE TILES ABOVE THE LIST EACH HAVE A DESTINATION, and that is what
// this parameter is for. A tile is a promise that a list exists behind the
// number; before this, "Missing a permission: 3" was a number with nowhere to
// go, and the operator had to read forty cards to find the three it meant.
//
// **THE DEFAULT STILL HIDES NOTHING** — the header's first rule. `access` is
// absent unless a person asked for it, and when it is present the page SAYS
// how many entries it is holding back and offers the way out. A filter the
// operator chose and can see is not the silent omission that rule forbids.
const ACCESS_PARAM = "access";

type Access = "" | "runnable" | "blocked";

/** The URL is untrusted input: anything that is not one of the two known
 *  values is the unfiltered catalogue, never an empty page. */
function readAccess(raw: string): Access {
  return raw === "runnable" || raw === "blocked" ? raw : "";
}

/**
 * A REPORT'S FAMILIES ARE THE MODULES IT READS (redesign Task 11, "ReportCatalogue").
 * The catalogue publishes no family of its own; what it publishes is
 * `requires`, every module read permission a report needs, derived on the
 * server from the tables it reads. So a family is a module, named as the rail
 * names it, and a report belongs to each module it reads: "Devices" holds
 * every report that reads devices. Nothing here is a second table the server
 * does not have.
 */
const FAMILY_LABEL: Readonly<Record<string, MessageKey>> = {
  devices: "shell.nav.item.devices",
  patch: "shell.nav.item.patching",
  backup: "shell.nav.item.backup",
  health: "shell.nav.item.alerts",
  sla: "shell.nav.item.serviceLevels",
  ticket: "shell.nav.item.tickets",
};

function familiesOf(entry: CatalogueEntry): string[] {
  return entry.requires.map((code) => code.split(".").slice(0, -1).join("."));
}

function familyLabel(family: string): string {
  const key = FAMILY_LABEL[family];
  return key ? translate(key) : humaniseKey(family);
}

/** For the search: case and accents do not count, the Admin panel's rule. */
function fold(text: string): string {
  return text.normalize("NFD").replace(/\p{M}/gu, "").toLowerCase().trim();
}

/**
 * ON THE LIST KIT (redesign Task 11, "ReportCatalogue"; Receta L): a search
 * (`?q=`, over the report's name, summary and description) and the family
 * chips (`?family=`, each counting the reports of the whole catalogue that read
 * that module), beside the `?access=` the tiles open, which stays. The h1 is
 * the rail's label.
 */
export function ReportCataloguePage() {
  const t = useT();
  const filters = useUrlFilters(["q", "family", ACCESS_PARAM] as const);
  const access = readAccess(filters.values[ACCESS_PARAM]);
  const catalogue = useReportCatalogue();
  const reports = catalogue.data?.reports ?? [];
  const entitledCount = reports.filter((entry) => entry.entitled).length;
  const blockedCount = reports.length - entitledCount;
  const byAccess =
    access === ""
      ? reports
      : reports.filter((entry) => (access === "runnable" ? entry.entitled : !entry.entitled));
  const wanted = fold(filters.values.q);
  const family = filters.values.family;
  const shown = byAccess.filter(
    (entry) =>
      (family === "" || familiesOf(entry).includes(family)) &&
      (wanted === "" ||
        [reportName(entry.key), reportProse(entry.summary), reportProse(entry.description)].some((text) =>
          fold(text).includes(wanted),
        )),
  );
  // Each family once, the most-read first; counted over the whole catalogue.
  const familyCounts = new Map<string, number>();
  for (const entry of reports) {
    for (const name of new Set(familiesOf(entry))) familyCounts.set(name, (familyCounts.get(name) ?? 0) + 1);
  }
  const families = [...familyCounts.entries()].sort(
    (a, b) => b[1] - a[1] || familyLabel(a[0]).localeCompare(familyLabel(b[0])),
  );
  const searched = filters.values.q !== "" || family !== "";

  return (
    <div>
      <PageHead
        title={t("reporting.catalogue.title")}
        description={t("reporting.catalogue.description")}
      />

      <ReportReadiness keys={catalogue.data?.reports.map((entry) => entry.key)} />

      {/* The published total is an inert count; what you can run is a
          capability you have; what you cannot is the one number on this page a
          person can act on (ask for the codes), so it is the only one carrying
          weight. */}
      <StatsGrid columns={3}>
        <StatCard
          label={t("reporting.catalogue.tiles.templates")}
          value={String(reports.length)}
          icon="chart-bar"
          to="/reports"
          tone="neutral"
          zeroLabel={t("reporting.catalogue.tiles.templatesZero")}
        />
        <StatCard
          label={t("reporting.catalogue.tiles.runnable")}
          value={String(entitledCount)}
          icon="circle-check"
          to={`/reports?${ACCESS_PARAM}=runnable`}
          tone="green"
          zeroLabel={t("reporting.catalogue.tiles.runnableZero")}
        />
        <StatCard
          label={t("reporting.catalogue.tiles.blocked")}
          value={String(blockedCount)}
          icon="lock"
          to={`/reports?${ACCESS_PARAM}=blocked`}
          tone="gold"
          urgent
          zeroLabel={t("reporting.catalogue.tiles.blockedZero")}
        />
      </StatsGrid>

      {access === "" ? null : (
        <p className="note mb-3" role="status">
          <Trans
            k={access === "runnable" ? "reporting.catalogue.showingRunnable" : "reporting.catalogue.showingBlocked"}
            vars={{ hidden: reports.length - byAccess.length, count: reports.length }}
            components={{ link: <Link to="/reports" /> }}
          />
        </p>
      )}

      {/* The rung between the page's h1 and the h3 each row carries: the
          design gives this list no visible title, so the heading is for the
          outline only. */}
      <h2 className="sr-only">{t("reporting.catalogue.heading")}</h2>
      <Panel bare>
        <ListToolbar
          search={
            <SearchField
              type="search"
              aria-label={t("reporting.catalogue.search")}
              placeholder={t("reporting.catalogue.search")}
              value={filters.values.q}
              onChange={(event) => filters.set("q", event.target.value)}
            />
          }
          chips={
            families.length > 0 ? (
              <FilterChips
                label={t("reporting.catalogue.familyLabel")}
                options={[
                  { value: "", label: t("reporting.catalogue.allFamilies") },
                  ...families.map(([name, count]) => ({ value: name, label: familyLabel(name), count })),
                ]}
                value={family}
                onChange={(value) => filters.set("family", value)}
              />
            ) : null
          }
          end={catalogue.data ? <ShowingCount shown={shown.length} total={reports.length} /> : null}
        />
        <QueryState loading={catalogue.isLoading} error={catalogue.error} empty={false} emptyLabel="">
          {shown.length === 0 ? (
            searched ? (
              <ListEmpty filtered onClear={filters.clear} nothingYet="" icon="chart-bar" />
            ) : (
              // WHICH EMPTY IT IS. "No reports exist", "you can run none of
              // them" and "you are blocked from none of them" are three
              // different facts, and the third is good news; one sentence for
              // all three is the zero-that-means-nothing defect in prose form.
              <ListEmpty
                filtered={false}
                onClear={filters.clear}
                nothingYet={
                  access === "runnable"
                    ? t("reporting.catalogue.emptyRunnable")
                    : access === "blocked"
                      ? t("reporting.catalogue.emptyBlocked")
                      : t("reporting.catalogue.empty")
                }
                icon="chart-bar"
              />
            )
          ) : (
            <Table label={t("reporting.catalogue.heading")}>
              <thead>
                <tr>
                  <Th>{t("reporting.catalogue.name")}</Th>
                  <Th>{t("reporting.catalogue.access")}</Th>
                  <Th>{""}</Th>
                </tr>
              </thead>
              <tbody>
                {shown.map((entry) => (
                  <EntryRow key={entry.key} entry={entry} />
                ))}
              </tbody>
            </Table>
          )}
        </QueryState>
      </Panel>

      <BiExportNotice />
    </div>
  );
}
