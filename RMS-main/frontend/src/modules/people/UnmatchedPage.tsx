import { useState } from "react";
import { Link } from "react-router-dom";
import { describeSubmitError } from "../../components/form";
import { Button, Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import { PageHead, Panel, QueryState } from "../../shell/components";
import { useCan, useLinkSeat, useUnmatched } from "./api";
import { PersonPickerDialog } from "./links";

/** Seats, computers and licences that name someone who is not linked. */
export function UnmatchedPage() {
  const t = useT();
  const unmatched = useUnmatched();
  const canManage = useCan("people.manage");
  const link = useLinkSeat();
  const [choosingFor, setChoosingFor] = useState<string | null>(null);
  const data = unmatched.data;
  const total = data ? data.seats.length + data.devices.length + data.allocations.length : 0;

  return (
    <div>
      <PageHead
        title={t("people.unmatched.title")}
        description={t("people.unmatched.description")}
      />
      {link.isError ? (
        <p role="alert" className="form-error">
          {describeSubmitError(link.error)}
        </p>
      ) : null}
      <QueryState
        loading={unmatched.isLoading}
        error={unmatched.error}
        empty={total === 0}
        emptyLabel={t("people.unmatched.empty")}
        emptyIcon="users"
      >
        {data && data.seats.length > 0 ? (
          <Panel title={t("people.unmatched.seats.title")} bare>
            <Table label={t("people.unmatched.seats.title")}>
              <thead>
                <tr>
                  <Th>{t("people.unmatched.seats.service")}</Th>
                  <Th>{t("people.unmatched.seats.recordedAs")}</Th>
                  <Th>{canManage ? t("people.unmatched.seats.link") : ""}</Th>
                </tr>
              </thead>
              <tbody>
                {data.seats.map((seat) => (
                  <tr key={seat.seat_id}>
                    <Td>
                      <Link to={`/subscriptions/${seat.subscription_id}`}>{seat.service_name}</Link>
                    </Td>
                    <Td>
                      {seat.assignee_name} ({seat.assignee_email})
                    </Td>
                    <Td>
                      {canManage ? (
                        <span className="flex gap-2">
                          <Button variant="outline" onClick={() => setChoosingFor(seat.seat_id)}>
                            {t("people.unmatched.seats.linkTo")}
                          </Button>
                          <Button
                            variant="ghost"
                            disabled={link.isPending}
                            onClick={() => link.mutate({ seatId: seat.seat_id, personId: null })}
                          >
                            {t("people.unmatched.seats.addNew")}
                          </Button>
                        </span>
                      ) : null}
                    </Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          </Panel>
        ) : null}

        {data && data.devices.length > 0 ? (
          <Panel
            title={t("people.unmatched.computers.title")}
            subtitle={t("people.unmatched.computers.subtitle")}
            bare
          >
            <Table label={t("people.unmatched.computers.title")}>
              <thead>
                <tr>
                  <Th>{t("people.unmatched.computers.computer")}</Th>
                  <Th>{t("people.unmatched.computers.signedInAs")}</Th>
                  <Th>{t("people.unmatched.computers.ownerField")}</Th>
                </tr>
              </thead>
              <tbody>
                {data.devices.map((device) => (
                  <tr key={device.device_id}>
                    <Td>
                      <Link to={`/devices/${device.device_id}`}>{device.hostname ?? t("people.unmatched.computers.unnamed")}</Link>
                    </Td>
                    <Td>{device.observed_login ?? t("people.unmatched.computers.nobodySeen")}</Td>
                    <Td>{device.owner ?? t("people.unmatched.computers.empty")}</Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          </Panel>
        ) : null}

        {data && data.allocations.length > 0 ? (
          <Panel
            title={t("people.unmatched.licences.title")}
            subtitle={t("people.unmatched.licences.subtitle")}
            bare
          >
            <Table label={t("people.unmatched.licences.title")}>
              <thead>
                <tr>
                  <Th>{t("people.unmatched.licences.product")}</Th>
                </tr>
              </thead>
              <tbody>
                {data.allocations.map((allocation) => (
                  <tr key={allocation.allocation_id}>
                    <Td>
                      <Link to={`/licenses/${allocation.license_id}`}>{allocation.product_name}</Link>
                    </Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          </Panel>
        ) : null}
      </QueryState>

      <PersonPickerDialog
        open={choosingFor != null}
        title={t("people.unmatched.pickerTitle")}
        confirmLabel={t("people.unmatched.pickerConfirm")}
        allowNobody={false}
        pending={link.isPending}
        error={null}
        onCancel={() => setChoosingFor(null)}
        onConfirm={(personId) => {
          const seatId = choosingFor;
          if (!seatId) return;
          link.mutate({ seatId, personId }, { onSuccess: () => setChoosingFor(null) });
        }}
      />
    </div>
  );
}
