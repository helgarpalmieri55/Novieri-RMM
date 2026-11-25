/**
 * Needs a person: the computers, licences and subscription seats that name
 * someone we could not match to a person by email.
 *
 * ON THE LIST KIT (redesign Task 11, 6.8; Receta L). It was three tables one
 * under another; it is now one list of the type chosen in `?kind=`, so a type
 * is a link and a reload keeps it. Computers are the default (`""`, which
 * writes nothing). Each chip's count is the size of that type's list in the
 * ONE answer `GET /people/unmatched` gives -- a total, not a page -- so the
 * three counts add up to what the three tables showed.
 *
 * A type with nothing in it says so for that type ("Every licence has a
 * person."): the chip is the kind of thing listed, not a filter over it, so
 * "Nothing matches these filters" would not be its cause. With nothing in any
 * of them, the page says everything is linked.
 *
 * Linking a seat is `people.manage`, as before, and the server checks it
 * (invariant 6): the buttons are a courtesy.
 */
import { useState } from "react";
import { Link } from "react-router-dom";
import { describeSubmitError } from "../../components/form";
import { Button, Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import type { MessageKey } from "../../i18n/core";
import {
  FilterChips,
  ListEmpty,
  ListToolbar,
  PageHead,
  Panel,
  QueryState,
  ShowingCount,
  useUrlFilters,
} from "../../shell/components";
import { useCan, useLinkSeat, useUnmatched } from "./api";
import { PersonPickerDialog } from "./links";
import type { UnmatchedOut } from "./types";

/** The three types, computers first: the default writes nothing to the URL. */
const KINDS = ["", "licenses", "seats"] as const;
type Kind = (typeof KINDS)[number];

const KIND_LABEL: Record<Kind, MessageKey> = {
  "": "people.unmatched.kind.devices",
  licenses: "people.unmatched.kind.licences",
  seats: "people.unmatched.kind.seats",
};

const KIND_EMPTY: Record<Kind, MessageKey> = {
  "": "people.unmatched.emptyDevices",
  licenses: "people.unmatched.emptyLicences",
  seats: "people.unmatched.emptySeats",
};

function sizeOf(data: UnmatchedOut, kind: Kind): number {
  if (kind === "licenses") return data.allocations.length;
  if (kind === "seats") return data.seats.length;
  return data.devices.length;
}

/** The line under the chips that says what to do with the type shown, flush with the table. */
const NOTE_STYLE = { padding: "12px 16px 4px" } as const;

/** Seats, computers and licences that name someone who is not linked. */
export function UnmatchedPage() {
  const t = useT();
  const filters = useUrlFilters(["kind"] as const);
  const kind: Kind = (KINDS as readonly string[]).includes(filters.values.kind) ? (filters.values.kind as Kind) : "";
  const unmatched = useUnmatched();
  const canManage = useCan("people.manage");
  const link = useLinkSeat();
  const [choosingFor, setChoosingFor] = useState<string | null>(null);
  const data = unmatched.data;
  const total = data ? data.seats.length + data.devices.length + data.allocations.length : 0;
  const shown = data ? sizeOf(data, kind) : 0;

  return (
    <div>
      <PageHead title={t("people.unmatched.title")} description={t("people.unmatched.description")} />
      {link.isError ? (
        <p role="alert" className="form-error">
          {describeSubmitError(link.error)}
        </p>
      ) : null}
      <Panel bare>
        <ListToolbar
          chips={
            <FilterChips
              label={t("people.unmatched.kindLabel")}
              // The counts are the server's whole answer; before it, no number at all.
              options={KINDS.map((value) => ({
                value,
                label: t(KIND_LABEL[value]),
                count: data ? sizeOf(data, value) : undefined,
              }))}
              value={kind}
              onChange={(value) => filters.set("kind", value)}
            />
          }
          end={data ? <ShowingCount shown={shown} total={shown} /> : null}
        />
        <QueryState loading={unmatched.isLoading} error={unmatched.error} empty={false} emptyLabel="">
          {!data ? null : total === 0 ? (
            <ListEmpty filtered={false} onClear={filters.clear} nothingYet={t("people.unmatched.empty")} icon="users" />
          ) : shown === 0 ? (
            <ListEmpty filtered={false} onClear={filters.clear} nothingYet={t(KIND_EMPTY[kind])} icon="users" />
          ) : kind === "seats" ? (
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
          ) : kind === "licenses" ? (
            <>
              <p className="note" style={NOTE_STYLE}>
                {t("people.unmatched.licences.subtitle")}
              </p>
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
            </>
          ) : (
            <>
              <p className="note" style={NOTE_STYLE}>
                {t("people.unmatched.computers.subtitle")}
              </p>
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
                        <Link to={`/devices/${device.device_id}`}>
                          {device.hostname ?? t("people.unmatched.computers.unnamed")}
                        </Link>
                      </Td>
                      <Td>{device.observed_login ?? t("people.unmatched.computers.nobodySeen")}</Td>
                      <Td>{device.owner ?? t("people.unmatched.computers.empty")}</Td>
                    </tr>
                  ))}
                </tbody>
              </Table>
            </>
          )}
        </QueryState>
      </Panel>

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
