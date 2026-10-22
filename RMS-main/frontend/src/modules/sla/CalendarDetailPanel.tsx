import { describeSubmitError } from "../../components/form";
import { Badge } from "../../components/ui";
import { useT } from "../../i18n/context";
import { ApiError } from "../../lib/api";
import { useSlaCalendar } from "./api";
import { HolidaysPanel } from "./HolidaysPanel";
import {
  calendarHealthExplanation,
  calendarHealthLabel,
  calendarHealthVariant,
} from "./slaState";
import { WorkingWindowsEditor } from "./WorkingWindowsEditor";

// One calendar's week and holidays. The windows come from the by-id read
// (`CalendarDetail.windows`) -- the only route that returns them -- and the
// timezone is repeated at the top of both halves, because a working window
// and a holiday date are each read in the calendar's zone, not the reader's.
export function CalendarDetailPanel({
  calendarId,
  canAdmin,
}: {
  calendarId: string;
  canAdmin: boolean;
}) {
  const t = useT();
  const calendar = useSlaCalendar(calendarId);
  const forbidden = calendar.error instanceof ApiError && calendar.error.status === 403;

  if (forbidden) {
    return <p className="note">{t("sla.detail.forbidden")}</p>;
  }
  if (calendar.isError) {
    // This return REPLACES the whole panel, so if it says nothing the user
    // sees an expanded row with no content in it and no reason given --
    // which is what `describeError` produced on a bodyless refusal.
    return (
      <p role="alert" className="notice red">
        {t("sla.detail.failed", { reason: describeSubmitError(calendar.error) })}
      </p>
    );
  }
  if (!calendar.data) {
    return <p className="note">{t("sla.detail.loading")}</p>;
  }

  const explanation = calendarHealthExplanation(calendar.data.health);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="cell-title">{calendar.data.name}</h3>
        <Badge variant="info">{calendar.data.timezone}</Badge>
        <Badge variant={calendarHealthVariant(calendar.data.health)}>
          {calendarHealthLabel(calendar.data.health)}
        </Badge>
      </div>
      {explanation && <p className="notice red">{explanation}</p>}

      <WorkingWindowsEditor
        calendarId={calendarId}
        timezone={calendar.data.timezone}
        windows={calendar.data.windows}
        canAdmin={canAdmin}
      />
      <HolidaysPanel calendarId={calendarId} canAdmin={canAdmin} />
    </div>
  );
}
