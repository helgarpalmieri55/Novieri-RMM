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

import { Link, useSearchParams } from "react-router-dom";
import { Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import { Trans } from "../../i18n/Trans";
import { permissionsSentence } from "../../lib/permissions";
import { useReportCatalogue } from "./api";
import { ReportReadiness } from "./ReportReadiness";
import { reportName, reportProse } from "./reportText";
import type { CatalogueEntry } from "./types";
import { PageHead, Panel, QueryState, StatCard, StatsGrid } from "../../shell/components";
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
function readAccess(raw: string | null): Access {
  return raw === "runnable" || raw === "blocked" ? raw : "";
}

export function ReportCataloguePage() {
  const t = useT();
  const [params] = useSearchParams();
  const access = readAccess(params.get(ACCESS_PARAM));
  const catalogue = useReportCatalogue();
  const reports = catalogue.data?.reports ?? [];
  const entitledCount = reports.filter((entry) => entry.entitled).length;
  const blockedCount = reports.length - entitledCount;
  const shown =
    access === ""
      ? reports
      : reports.filter((entry) => (access === "runnable" ? entry.entitled : !entry.entitled));

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
            vars={{ hidden: reports.length - shown.length, count: reports.length }}
            components={{ link: <Link to="/reports" /> }}
          />
        </p>
      )}

      {/* The rung between the page's h1 and the h3 each row carries: the
          design gives this list no visible title, so the heading is for the
          outline only. */}
      <h2 className="sr-only">{t("reporting.catalogue.heading")}</h2>
      <Panel bare>
        <QueryState
          loading={catalogue.isLoading}
          error={catalogue.error}
          empty={catalogue.data != null && shown.length === 0}
          // WHICH EMPTY IT IS. "No reports exist", "you can run none of them"
          // and "you are blocked from none of them" are three different facts,
          // and the third is good news; one sentence for all three is the
          // zero-that-means-nothing defect in prose form.
          emptyLabel={
            access === "runnable"
              ? t("reporting.catalogue.emptyRunnable")
              : access === "blocked"
                ? t("reporting.catalogue.emptyBlocked")
                : t("reporting.catalogue.empty")
          }
          emptyIcon="chart-bar"
        >
          <Table>
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
        </QueryState>
      </Panel>

      <BiExportNotice />
    </div>
  );
}
