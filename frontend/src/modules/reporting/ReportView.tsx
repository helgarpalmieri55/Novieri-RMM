// One report: a params bar, the table, the totals strip, the provenance line
// and the CSV download (spec §11).
//
// **"AS OF <generated_at>" IS MANDATORY, NOT OPTIONAL**, and so is the
// staleness note when `meta.reproducible` is false. Reports are computed live
// (§0.3), so a figure on this page is a statement about the record at one
// instant; rendered without that instant it is a number somebody will quote in
// a month. `sla-attainment` is the v1 report whose sources are mutable by
// design -- a human re-triage recomputes a deadline from ticket start, so a
// ticket that read "met" last month can read "breached" today -- and the note
// is the only thing on the page that says so.
//
// **NO CLIENT-SIDE AGGREGATION OF ANY KIND.** `totals` and every percentage
// come from the response, untouched. If the browser computed a percentage there
// would be two implementations of it and the browser's is the one someone
// screenshots -- the same reasoning `sla/views.py` gives for owning breach
// state server-side. `__tests__/reporting.test.tsx` enforces this
// STRUCTURALLY, over this folder's source, because a reviewer cannot see the
// absence of something.
//
// **THE PARAMS BAR IS BUILT FROM THE SERVER'S PUBLISHED SCHEMA (§17.11).** It
// used to hardcode `limit`, `offset` and an optional window, because the
// catalogue published no params schema and the browser had nowhere else to get
// one. That was a second copy of a contract the server already held, and the
// copies had already drifted: EIGHT real parameters were unreachable from this
// page, and `from`/`to` were offered to the three reports that answer 422 for
// them.
//
// `useReportCatalogue()` is where the schema comes from, and it is the SAME
// query the catalogue page uses, cached under the same key -- arriving here by
// a link means it is usually already in hand. A report key with no catalogue
// entry renders no params bar and runs nothing, rather than guessing.
//
// **THE FRONTEND STILL GATES NOTHING** (invariant #6). `required` on a field
// and `min`/`max` on an input come from the schema and save a round trip; the
// server validates every parameter against the model that schema was derived
// from, and a 403 or a 422 is rendered verbatim when it comes.

// **THE PARAMS BAR EXPLAINS A REFUSED RUN INSTEAD OF GOING DEAD.** Auditor
// (MEDIUM): "Report parameters form has the same submit gaps." Run was
// `disabled={unfilled.length > 0}`, which is the silent no-op this cycle
// exists to delete -- and the only thing that named the missing parameters was
// a hint using the WIRE names (`from`, `to`), not the titles printed above the
// boxes. Run now stays clickable and the submit says what is wrong, in the
// words on the screen.

import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { Link, useParams } from "react-router-dom";
import { ApiError } from "../../lib/api";
import { attemptProblems, describeSubmitError, Form, FormActions } from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import { useLanguage, useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import { Trans } from "../../i18n/Trans";
import { formatDate, formatDateTime, toInstant } from "../../lib/datetime";
import { humaniseStatus } from "../../lib/labels";
import { formatCount } from "../../lib/format";
import { BarChart } from "../../components/chart";
import { Button, inputClass } from "../../components/ui";
import { EmptyState, NoValue } from "../../components/emptystate";
import { EntityPicker } from "../../components/picker";
import { osFamilyLabel } from "../devices/osLabel";
import type { PickerOption } from "../../components/picker";
import { useExportReport, useProfileZone, useReport, useReportCatalogue, useTicketQueues } from "./api";
import { figureFor } from "./reportFigures";
import { initialForm, missingRequired, paramControls, toQuery } from "./paramsForm";
import type { ParamControl } from "./paramsForm";
import { fieldLabel, reportName, reportProse, sourceLine, vocabularyCell } from "./reportText";
import { ticketPriorityLabel } from "../service-desk/serviceDeskState";
import type { ParamsSchema, ReportCell, ReportParams } from "./types";
import { Btn, PageHead, Panel, StatsGrid, TableWrap } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { KIND_ICON, Tile } from "../../shell/viz";

const EMPTY_SCHEMA: ParamsSchema = {};

/** The one published parameter this page renders as navigation, not as a field. */
const OFFSET_PARAM = "offset";

// The shape checks, DERIVED FROM THE SCHEMA like everything else on this bar.
// `min`/`max` are already on the input, but the form is `noValidate` (the
// kernel's, deliberately: the browser's own bubble is not announced and not
// styleable), so these are what actually catch a value out of bounds. Still
// not a gate -- invariant #6, the server validates every parameter against the
// model this schema came from and a 422 is rendered verbatim when it comes.
// This only saves the round trip and names the field while the operator is
// still looking at it.
function validatorFor(control: ParamControl, zone: string | null): FieldRule["validate"] {
  if (control.kind === "number") {
    return (value) => {
      const parsed = Number(value);
      if (!Number.isFinite(parsed)) {
        return translate("reporting.validation.notNumber", { label: control.label, value });
      }
      if (control.min !== undefined && parsed < control.min) {
        return translate("reporting.validation.atLeast", { label: control.label, min: String(control.min) });
      }
      if (control.max !== undefined && parsed > control.max) {
        return translate("reporting.validation.atMost", { label: control.label, max: String(control.max) });
      }
      return null;
    };
  }
  if (control.kind === "datetime") {
    // `toQuery` sends the raw string through when the value cannot be read as
    // an instant, so an unparseable window reached the server as a 422 about a
    // field the operator had, as far as they could tell, filled in.
    //
    // ASKED OF `toInstant` -- the SAME function that will convert it on
    // submit -- rather than of a second `new Date()` here. Two readers of one
    // string is how a form comes to accept a value its own submit path then
    // silently mangles.
    return (value) =>
      toInstant(value, zone) === ""
        ? translate("reporting.validation.notDateTime", { label: control.label })
        : null;
  }
  return undefined;
}

// A rule per parameter that is REQUIRED, plus one per optional parameter the
// operator actually typed into. `requiredProblems` reports an empty value as
// missing, and an untouched optional parameter is not missing -- it is the
// filter nobody asked for, which `toQuery` already drops.
function paramRules(
  controls: readonly ParamControl[],
  form: Record<string, string>,
  zone: string | null,
): FieldRule[] {
  return controls
    .filter((control) => control.required || (form[control.name] ?? "").trim() !== "")
    .map((control) => ({
      name: control.name,
      // The TITLE, which is what is printed above the box. The wire name is
      // what the old hint used, and `stale_after_days` is not what an operator
      // is looking at.
      label: control.label,
      validate: validatorFor(control, zone),
    }));
}

/**
 * The report parameters that name another module's row by its UUID, and where
 * that module's list comes from.
 *
 * AUDIT [MEDIUM]: "Queue Id ... the user must find a UUID in another module and
 * transcribe it, when a queue picker already exists elsewhere in the product."
 * A mistyped UUID here is not a validation message; it is a report about the
 * wrong queue, presented as compliance evidence.
 *
 * A NAME, NOT A SHAPE. Keying this on `format: "uuid"` would put a picker in
 * front of every identifier any report ever publishes, including ones this
 * console has no list for. A parameter not named here keeps its text box, which
 * is what the schema-driven bar does with anything it cannot classify.
 */
const ENTITY_PARAMS: readonly string[] = ["queue_id"];

export function entityParamNames(controls: readonly ParamControl[]): string[] {
  return controls.map((control) => control.name).filter((name) => ENTITY_PARAMS.includes(name));
}

function ParamField({
  control,
  value,
  onChange,
  entity,
}: {
  control: ParamControl;
  value: string;
  onChange: (next: string) => void;
  /** Supplied only for a parameter in `ENTITY_PARAMS`. */
  entity?: { options: PickerOption[]; loading: boolean; emptyHint: string };
}) {
  const t = useT();
  const { language } = useLanguage();
  if (entity != null) {
    return (
      <div>
        <EntityPicker
          label={control.label}
          options={entity.options}
          loading={entity.loading}
          emptyHint={entity.emptyHint}
          placeholder={t("reporting.view.searchQueues")}
          value={value}
          onChange={onChange}
        />
      </div>
    );
  }
  // In English `aria-label` is the WIRE name, so what a test (and a screen reader) reads is
  // the parameter actually sent; the visible label is the schema's title. In any other
  // language the wire name is English, so the wrapping label's visible title names the input.
  const shared = {
    "aria-label": language === "en" ? control.name : undefined,
    title: control.description,
    required: control.required,
    value,
    className: inputClass,
  };
  return (
    /* The kit's `.field`: the caption in the span, the control under it. The
       bar lays its fields out in a row, so the field's own bottom margin and
       width cap are lifted. */
    <label className="field" style={{ marginBottom: 0, maxWidth: "none" }}>
      <span>
        {control.label}
        {control.required && <span aria-hidden="true"> *</span>}
      </span>
      {control.kind === "select" ? (
        <select {...shared} className="select-field" onChange={(e) => onChange(e.target.value)}>
          {/* An optional enum can be left unset; a required one cannot. */}
          {!control.required && <option value="">{t("reporting.view.any")}</option>}
          {/* THE VALUE IS THE WIRE'S, THE LABEL IS THE READER'S. The option
              text was the raw enum — `windows`, `macos` — one panel above the
              result table that now writes Windows and macOS. `value` is
              untouched, so the query this bar sends is unchanged. */}
          {control.options.map((option) => (
            <option key={option} value={option}>
              {/^(os_)?family$/.test(control.name)
                ? osFamilyLabel(option)
                : control.name === "priority"
                  ? ticketPriorityLabel(option)
                  : humaniseStatus(option)}
            </option>
          ))}
        </select>
      ) : (
        <input
          {...shared}
          type={
            control.kind === "datetime"
              ? "datetime-local"
              : control.kind === "number"
                ? "number"
                : "text"
          }
          min={control.min}
          max={control.max}
          onChange={(e) => onChange(e.target.value)}
        />
      )}
    </label>
  );
}

function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

/**
 * ONE CELL, AS A PERSON READS IT -- and the column is an argument because a
 * generic table cannot ask a value what it means.
 *
 * WHAT WAS WRONG. This was `String(value)`. The port humanised the column
 * HEADERS (`humaniseKey`) and left the cells alone, so the result table wrote
 * `2026-09-08T06:57:26.394Z` on every row four lines under the panel's own
 * "As of 08 Sept 2026, 13:57", printed the wire enums `windows` / `macos` /
 * `linux` where the rest of the console writes Windows and macOS, and said
 * `yes`/`no` for a boolean. Three vocabularies on one screen, and the
 * ambiguous one is the evidence somebody screenshots.
 *
 * THE ENUM COLUMNS ARE AN ALLOWLIST, and the direction of that choice is
 * deliberate. A single lowercase token is not proof of an enum: `alpha` is a
 * hostname in this module's own tests, and capitalising a hostname is a
 * statement about a machine that is not true. So a value is only humanised
 * when it is snake_case (the kit's own rule in `components/attributes.tsx`,
 * where a space, a slash or a colon means prose) or when its COLUMN names one
 * of the wire vocabularies below. A column this list has not heard of renders
 * verbatim, which is the correct direction for this particular list to be
 * wrong in: an unhumanised enum is ugly, a mangled identifier is wrong.
 *
 * `os_family` has its own table rather than a case rule, because no case rule
 * produces "macOS" -- `devices/osLabel.ts` owns that spelling, and a report
 * row's `os_family` is the same observation about the same machine.
 */
const ENUM_COLUMN =
  /(^|_)(family|status|state|type|kind|result|outcome|decision|severity|verdict|priority|risk|mode|channel|disposition)$/;

/** An instant, as the wire writes it. Anything else is left to the string branch. */
const ISO_INSTANT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})$/;
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
/** `attributes.tsx`'s rule: identifier-shaped, and it needs the underscore. */
const WIRE_ENUM = /^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$/;
/** A single lowercase token: an enum ONLY when its column says so. */
const BARE_TOKEN = /^[a-z][a-z0-9]*$/;

function renderCell(column: string, value: ReportCell | undefined, zone: string | null): ReactNode {
  // THE LABELLED ABSENCE, not a bare dash: `NoValue` exists because a screen
  // reader announces an em dash as "em dash" or skips it entirely.
  if (value === undefined || value === null) return <NoValue label={translate("reporting.view.notRecorded")} />;
  if (typeof value === "boolean") return translate(value ? "reporting.view.yes" : "reporting.view.no");
  if (typeof value !== "string") return String(value);
  if (ISO_INSTANT.test(value)) return formatDateTime(value, zone);
  if (ISO_DATE.test(value)) return formatDate(value, zone);
  if (/(^|_)os_family$/.test(column)) return osFamilyLabel(value);
  // The columns whose vocabulary the console words itself (a value it has not met falls through).
  const known = vocabularyCell(column, value);
  if (known !== null) return known;
  if (WIRE_ENUM.test(value)) return humaniseStatus(value);
  if (ENUM_COLUMN.test(column) && BARE_TOKEN.test(value)) return humaniseStatus(value);
  return value;
}

export function ReportViewPage() {
  const t = useT();
  const { reportKey = "" } = useParams<{ reportKey: string }>();
  // ONE ZONE FOR THE WHOLE PAGE, read once and threaded through both halves:
  // the window the operator types (`toQuery`) and the instant the run is
  // stamped with ("As of"). Splitting them is how a page comes to display a
  // time it did not send.
  const zone = useProfileZone();
  const catalogue = useReportCatalogue();
  const entry = catalogue.data?.reports.find((candidate) => candidate.key === reportKey);
  const schema = entry?.params_schema ?? EMPTY_SCHEMA;
  const controls = paramControls(schema);

  // Only fetched when this report actually publishes an entity parameter.
  const entityParams = entityParamNames(controls);
  const queues = useTicketQueues(entityParams.includes("queue_id"));
  const queueOptions: PickerOption[] = (queues.data ?? []).map((queue) => ({
    id: queue.id,
    label: queue.name,
    sublabel: queue.active ? queue.key : t("reporting.view.queueInactive", { key: queue.key }),
  }));

  // **`offset` IS NOT A QUESTION AN OPERATOR ANSWERS**, audit [MEDIUM]:
  // "Limit and Offset are pagination internals leaking into a business-user
  // form; nobody running an SLA report thinks in offsets." It stays a real
  // parameter — it is what the server pages on, and the run still sends the
  // schema's default — but it is driven by Previous/Next under the table
  // instead of a number box. `limit` stays on the bar: "how many rows do I
  // want" is a question, and the schema publishes its bounds.
  const paged = controls.some((control) => control.name === OFFSET_PARAM);
  const visibleControls = controls.filter((control) => control.name !== OFFSET_PARAM);

  const [form, setForm] = useState<Record<string, string>>({});
  const [applied, setApplied] = useState<ReportParams | null>(null);
  // Filled ON SUBMIT, never while typing: "From is required" appearing before
  // the operator has reached the box is its own kind of unhelpful.
  // The refused run as data (the rules and the snapshot they were checked
  // against), worded at render so a language switch re-words it.
  const [attempt, setAttempt] = useState<Attempt | null>(null);

  // The form is seeded from the SCHEMA'S OWN DEFAULTS once it arrives, and the
  // first run uses them -- so the query this page runs on open is the query the
  // server would have run for a bare request, not a guess that happens to match.
  useEffect(() => {
    if (entry == null) return;
    const seeded = initialForm(entry.params_schema);
    setForm(seeded);
    // A complaint about the previous report's parameters must not outlive the
    // parameters it was about.
    setAttempt(null);
    // A report with a required parameter and no default for it (`sla-attainment`'s
    // window, §5.4) runs NOTHING until an operator fills it in. The old page ran
    // immediately and rendered the 422.
    setApplied(
      missingRequired(entry.params_schema, seeded).length === 0
        ? toQuery(entry.params_schema, seeded, zone)
        : null,
    );
    // `zone` IS DELIBERATELY NOT A DEPENDENCY. `/me` can land after this
    // effect has seeded the form, and re-running it would discard whatever the
    // operator had already typed. It is safe here and only here: the seeded
    // values are the SCHEMA'S defaults, and a `format: "date-time"` default is
    // published as a qualified instant, which `toInstant` passes through
    // unchanged in any zone. Every value a person types goes through `run()`
    // below, which reads the zone at submit time.
  }, [entry]);

  const report = useReport(reportKey, applied ?? {}, reportKey !== "" && applied != null);
  const exporter = useExportReport(reportKey);

  /**
   * Move the window of rows, without touching any other parameter.
   *
   * Clamped at zero rather than trusted: the buttons are disabled at the ends,
   * but a disabled button is a courtesy and a negative `offset` is a 422.
   */
  function showRowsFrom(next: number): void {
    const at = Math.max(0, next);
    const moved = { ...form, [OFFSET_PARAM]: String(at) };
    setForm(moved);
    setApplied(toQuery(schema, moved, zone));
  }

  function run(): void {
    // The rules are built from what is on screen NOW, and the values are a copy
    // of the form, so nothing the rules read changes under a held refusal.
    const rules = paramRules(visibleControls, form, zone);
    const next: Attempt = { rules: () => rules, values: { ...form } };
    setAttempt(next);
    // Nothing is applied while something is wrong: running anyway produces a
    // 422 rendered as a server error, which reads as "the report is broken"
    // rather than "this box is empty".
    if (attemptProblems(next).length > 0) return;
    // BACK TO THE FIRST PAGE. Keeping page 4's offset across a parameter change
    // is how a run reports "no rows" for a query that has plenty.
    const first = paged ? { ...form, [OFFSET_PARAM]: "0" } : form;
    setForm(first);
    setApplied(toQuery(schema, first, zone));
  }

  const page = report.data;
  const columns = page != null && page.rows[0] != null ? Object.keys(page.rows[0]) : [];
  const totalKeys = page != null ? Object.keys(page.totals) : [];
  const figure = page != null ? figureFor(reportKey, page.totals) : null;
  // The metric's window, named in the chart title as both audits require. A
  // report with no window is stamped with the instant it ran instead, which is
  // the only period statement that report can honestly make.
  const windowLabel =
    page == null
      ? ""
      : page.meta.window_from != null && page.meta.window_to != null
        ? t("reporting.view.windowRange", {
            from: formatDateTime(page.meta.window_from, zone),
            to: formatDateTime(page.meta.window_to, zone),
          })
        : t("reporting.view.windowAsOf", { date: formatDateTime(page.meta.generated_at, zone) });

  return (
    <div>
      {/* THE H1 IS THE REPORT'S NAME, audit [HIGH]: the catalogue and this page
          both used `summary` -- a whole sentence -- where the name belongs, and
          "the actual report names exist only in the URLs". The name is derived
          from that same URL key, so the address, the catalogue card and this
          heading cannot disagree. */}
      <PageHead
        title={reportName(entry?.key ?? reportKey)}
        description={entry ? reportProse(entry.summary) : undefined}
        actions={
          <>
            {entry != null ? (
              /* Export sends the APPLIED parameters (so the file and the screen
                 agree), and before a run there are none: a "you may not do this
                 yet" with the reason beside the bar below, not a dead button. */
              <Btn
                label={t("reporting.view.download")}
                icon="file-download"
                disabled={applied == null}
                onClick={() => {
                  if (applied == null) return;
                  exporter.mutate(applied, {
                    onSuccess: (blob) => downloadBlob(blob, `${reportKey}.csv`),
                  });
                }}
              />
            ) : null}
            {/* THE WAY BACK LIVES IN THE HEAD'S ACTION SLOT. It was a bare
                `<Link>` with an inline flex style above the head — the eyebrow
                slot v3 forbids, a second navigation line under a crumb that
                already returns to the section. Every sibling detail page in
                this wave (the DR test report, the change record) puts the
                identical link here. */}
            <Link className="btn" to="/reports">
              <Icon name="chart-bar" size={14} />
              {t("reporting.view.allReports")}
            </Link>
          </>
        }
      />
      {entry != null && (
        /* THE GAP IS AN INLINE STYLE BECAUSE `mb-4` IS INERT HERE.
           `shell/base.css` resets `p { margin: 0 }` unlayered, and Tailwind's
           utilities live in the `utilities` layer -- unlayered wins, so this
           paragraph computed `margin-bottom: 0` and its descenders sat against
           the Parameters panel. 18px is `.ph`'s own bottom margin, so the head
           block ends the same distance above the first panel here as on every
           page whose head is one paragraph. */
        <p className="note" style={{ maxWidth: "70ch", marginBottom: 18 }}>
          {reportProse(entry.description)}
        </p>
      )}

      {catalogue.isLoading && <p className="note mt-4">{t("reporting.view.loadingParams")}</p>}
      {/* Without this the page is blank when the catalogue itself is refused --
          which is the 403 case, since the catalogue is gated on `reporting.read`. */}
      {/* ANNOUNCED, AND IT SAYS WHICH FAILURE. This read "could not be loaded"
          for a 403 and for a 503 alike, so the operator could not tell "you
          lack reporting.read" from "come back in a minute" -- and it was a
          bare <p>, which a screen reader never mentions. */}
      {catalogue.error != null && (
        <p role="alert" className="form-error mt-4">
          {t("reporting.view.catalogueFailed", { reason: describeSubmitError(catalogue.error) })}
        </p>
      )}
      {catalogue.data != null && entry == null && (
        <p className="form-error mt-4">
          <Trans
            k="reporting.view.unknownReport"
            vars={{ key: reportKey }}
            components={{ code: <code /> }}
          />
        </p>
      )}

      {entry != null && (
        <Panel title={t("reporting.view.parameters")}>
          <Form className="flex flex-wrap items-end gap-3" onSubmit={run}>
            {/* ONE CONTROL PER PUBLISHED PARAMETER. Nothing is listed here. */}
            {visibleControls.map((control) => (
              <ParamField
                key={control.name}
                control={control}
                value={form[control.name] ?? ""}
                onChange={(next) => setForm({ ...form, [control.name]: next })}
                entity={
                  control.name === "queue_id"
                    ? {
                        options: queueOptions,
                        loading: queues.isLoading,
                        // NOT "no queues": the list can also be empty because the
                        // queue list itself was refused, and telling an operator
                        // there are no queues when there are is worse than saying
                        // nothing.
                        emptyHint:
                          queues.error != null
                            ? t("reporting.view.queuesFailed", { reason: describeSubmitError(queues.error) })
                            : t("reporting.view.noQueues"),
                      }
                    : undefined
                }
              />
            ))}
            {/* `pending` is FALSE on purpose. Running a report is a QUERY, not a
                mutation: its progress is the "Running…" line below, and a second
                submit of the same parameters resolves to the same query key
                rather than a second request — so there is nothing here for a
                disabled control to protect. The kernel's pending label is also
                built for "Save" → "Saving…" and would render this one as
                "Runing…". */}
            <FormActions label={t("reporting.view.run")} pending={false} problems={attemptProblems(attempt)} />
            {/* The head's Export CSV is the one disabled control on this page,
                and this is where it says why. */}
            {applied == null && (
              <span className="note">{t("reporting.view.runFirst")}</span>
            )}
          </Form>
        </Panel>
      )}

      {/* WHY THE PAGE IS EMPTY, which is not the same message as what is wrong
          with the form. A report whose required parameters have no default runs
          nothing on open (§17.11) and used to leave a blank page under a params
          bar. This names no field — naming them is the submit's job, and two
          places saying it in two vocabularies is how a product ends up with
          four error styles. */}
      {entry != null && applied == null && (
        <Panel>
          <EmptyState
            icon="chart-bar"
            title={t("reporting.view.notRunTitle")}
            body={t("reporting.view.notRunBody")}
          />
        </Panel>
      )}

      {exporter.error != null && (
        <p role="alert" className="form-error mt-3">
          {/* 413 KEEPS ITS OWN SENTENCE: §0.8's decision is that the server
              refuses rather than truncating, and "narrow the parameters" is
              advice no generic handler can give. Everything else goes through
              the kernel -- "The export failed." named neither the reason nor
              whether anything happened, and for a bodyless 503 it was the only
              thing the operator got. */}
          {exporter.error instanceof ApiError && exporter.error.status === 413
            ? t("reporting.view.exportTooMany")
            : t("reporting.view.exportFailed", { reason: describeSubmitError(exporter.error) })}
        </p>
      )}

      {report.isLoading && <p className="note mt-6">{t("reporting.view.running")}</p>}
      {/* THE REFUSAL AS A SENTENCE. This rendered
          `HTTP ${status}. ${error.message}` -- a status code the operator
          cannot act on, followed by a body that is EMPTY for a bodyless
          refusal (the BLOCKER `describeSubmitError` exists for) and raw
          Pydantic JSON for a 422. */}
      {report.error != null && (
        <p role="alert" className="form-error mt-6">
          {t("reporting.view.runFailed", { reason: describeSubmitError(report.error) })}
        </p>
      )}

      {page != null && (
        <>
          <Panel
            title={t("reporting.view.result")}
            /* MANDATORY. See the module header. */
            subtitle={t("reporting.view.resultSubtitle", {
              date: formatDateTime(page.meta.generated_at, zone),
              count: page.meta.total,
            })}
          >
            {!page.meta.reproducible && (
              <div role="status" className="notice gold mb-3">
                <Icon name={KIND_ICON.warn} size={17} />
                <div>{t("reporting.view.mutable")}</div>
              </div>
            )}

            {/* **NOTHING MATCHED IS NOT A MEASUREMENT OF ZERO**, audit [HIGH]:
                "Running the report for 1 Jul - 19 Aug 2026 returned `0 matching
                rows.` Below that, nine tiles render ... Nine zeros are
                indistinguishable from nine genuine measurements of zero. '0
                tickets breached' reads as a perfect score when in fact nothing
                was measured. This is a data-integrity risk in a module
                explicitly built as compliance evidence -- someone will
                screenshot this as proof of 100% attainment."

                So on an empty match the tiles and the figure are SUPPRESSED
                rather than zeroed, and one empty state says which of the two
                this is. `meta.total` is the count over the whole match, not
                over the page, so this is "nothing matched" and never "you
                have paged past the end". */}
            {page.meta.total === 0 ? (
              <EmptyState
                icon="chart"
                title={t("reporting.view.emptyTitle")}
                body={t("reporting.view.emptyBody")}
              />
            ) : (
              <>
                {/* v3's headline numbers: a row of tiles over the figure, one
                    per total the server sent. Straight from the response,
                    never computed here. A total the server did not send has
                    no tile, so an unmeasured figure is absent rather than 0. */}
                <StatsGrid columns={Math.min(4, totalKeys.length)}>
                  {totalKeys.map((name) => (
                    <Tile key={name} label={fieldLabel(name)} value={page.totals[name]} />
                  ))}
                </StatsGrid>

                {/* THE FIGURE, audit [HIGH]: "There is no data visualisation
                    anywhere in the reporting module ... This is the one module
                    where a chart is the deliverable." Every bar is a value from
                    `totals`, selected by name; see `reportFigures.ts` for why
                    nothing here computes. */}
                {/* NO WIDTH CAP HERE ANY MORE. It read `maxWidth: 640`,
                    against the kit drawing `width: 100%` over a fixed 640-unit
                    viewBox -- which scaled the SVG's own text with the box, a
                    12px tick painted near 20px in a 1100px panel. The comment
                    recorded the other end of that as `chart.tsx`'s to fix and
                    reported to the kit, and the kit fixed it: the drawing is
                    measured, its viewBox is its rendered width, and one user
                    unit is one CSS pixel at every size. What the cap left
                    behind was a plot stopping ~500px short of this card's
                    right edge, so it goes. */}
                {/* THE WINDOW IN PARENTHESES, NOT BEHIND AN EM DASH. New UI copy
                    carries no em dash (the plan's global constraints), and this
                    caption is the one string on the page that built one by
                    concatenation: "Devices by scan and compliance state (as of
                    08 Sept 2026, 15:23)". */}
                {figure != null && (
                  <BarChart
                    title={`${figure.caption} (${windowLabel})`}
                    categories={figure.categories}
                    series={figure.series}
                    unit={figure.unit}
                  />
                )}
              </>
            )}

            {/* The provenance lines, as the panel's note under the figures,
                under the caption rung that says what they are. Written for a
                reader by `sourceLine` -- the wire enum and the bracketed
                permission code used to be the whole sentence. */}
            {page.meta.sources.length > 0 && (
              <>
                <p className="k" style={{ marginTop: 16 }}>
                  {t("reporting.view.sources")}
                </p>
                <ul className="note" style={{ listStyle: "none", margin: "4px 0 0", padding: 0 }}>
                  {page.meta.sources.map((source) => (
                    <li key={source.module}>{sourceLine(source)}</li>
                  ))}
                </ul>
              </>
            )}
          </Panel>

          {page.meta.total > 0 && (
            <Panel bare>
              <TableWrap>
                <table>
                  <thead>
                    <tr>
                      {/* The wire key as a person reads it (`lib/labels.ts`), not
                          `snake_case` with the underscores swapped for spaces --
                          the audit's "raw API concepts" complaint about this module
                          applies to a column header as much as to a parameter. */}
                      {columns.map((column) => (
                        <th key={column} scope="col">
                          {fieldLabel(column)}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {page.rows.map((row, index) => (
                      <tr key={String(row["device_id"] ?? row["queue_id"] ?? index)}>
                        {columns.map((column) => (
                          <td key={column}>{renderCell(column, row[column], zone)}</td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </TableWrap>

              {/* THE PAGINATION THE `Offset` BOX USED TO BE. `meta.limit` and
                  `meta.offset` are the server's echo of what it actually paged on,
                  so the position shown is the position run — not what is typed in
                  the form. */}
              {paged && (
                <nav
                  className="filterbar"
                  style={{ borderBottom: 0, borderTop: "1px solid var(--line)" }}
                  aria-label={t("reporting.view.rowsNav")}
                >
                  <Button
                    variant="outline"
                    className="small"
                    disabled={page.meta.offset <= 0}
                    onClick={() => showRowsFrom(page.meta.offset - page.meta.limit)}
                  >
                    {t("reporting.view.previous")}
                  </Button>
                  <Button
                    variant="outline"
                    className="small"
                    disabled={page.meta.offset + page.meta.limit >= page.meta.total}
                    onClick={() => showRowsFrom(page.meta.offset + page.meta.limit)}
                  >
                    {t("reporting.view.next")}
                  </Button>
                  {/* `role="status"`, so moving through the pages is announced
                      rather than only redrawn. */}
                  <span role="status" className="note">
                    {t("reporting.view.rowsRange", {
                      from: formatCount(page.meta.offset + 1),
                      to: formatCount(Math.min(page.meta.offset + page.meta.limit, page.meta.total)),
                      total: formatCount(page.meta.total),
                      // Picks the form; the formatted figure above is what is printed.
                      count: page.meta.total,
                    })}
                  </span>
                </nav>
              )}
            </Panel>
          )}
        </>
      )}
    </div>
  );
}
