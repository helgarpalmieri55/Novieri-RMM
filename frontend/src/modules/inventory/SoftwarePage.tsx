import { useMutation, useQuery } from "@tanstack/react-query";
import { Link, useLocation, useSearchParams } from "react-router-dom";
import { describeSubmitError } from "../../components/form";
import { useT } from "../../i18n/context";
import { Td } from "../../components/ui";
import { useApi } from "../../lib/ApiContext";
import { saveBlob } from "../../lib/download";
import {
  Btn,
  ListEmpty,
  ListToolbar,
  PageHead,
  Panel,
  QueryState,
  SearchField,
  ShowingCount,
  TableWrap,
  useUrlFilters,
} from "../../shell/components";
import { Icon } from "../../shell/icons";
import { OsIcon } from "../../shell/viz";

// Software discovered across the fleet by the inventory sweep
// (docs/modules/inventory.md "Software inventory"). v3 draws this page as
// one toolbar (search, count) over one list whose rows lead with the package
// glyph and carry installs and a currency pill. The pill -- "128 behind on 41"
// -- is OMITTED here, not faked: `GET /api/v1/software` (SoftwareAggregate)
// carries name, version, publisher and device_count, and "behind" needs a
// notion of the current version no endpoint serves. The design's Licenses
// correlate also shows "Mapped license" and "Last seen"; those belong to the
// licenses module and are not this endpoint's job either.
//
// ON THE LIST KIT (redesign plan Task 6, 1.3; Receta L, a list that only
// searches): the search in `?q=`, "Showing x of y", an empty list that says
// its cause, and the chosen title's computers in a panel under the list. NO
// CLIENT CHIP: the software inventory is the fleet's, one catalogue across
// every client.
export function SoftwarePage() {
  const t = useT();
  const api = useApi();
  /**
   * THE SEARCH AND THE SELECTION LIVE IN THE URL, for the same reason the
   * fleet list's filters do: "the machines with the old Chrome" is a thing one
   * technician sends another, and a selection held in component state cannot
   * be sent. The selection is a LINK on the title, so it is keyboard
   * reachable and middle-clickable; the previous version was a click handler
   * on the row that a keyboard could not reach at all.
   */
  const filters = useUrlFilters(["q"] as const);
  const [params] = useSearchParams();
  const search = filters.values.q;
  const selected = params.get("title");
  const hrefFor = (title: string) => {
    const next = new URLSearchParams(params);
    next.set("title", title);
    return `?${next.toString()}`;
  };
  // Closing the chosen title's panel takes `?title=` away and leaves the search.
  const { pathname } = useLocation();
  const withoutTitle = (() => {
    const next = new URLSearchParams(params);
    next.delete("title");
    const query = next.toString();
    return { pathname, search: query === "" ? "" : `?${query}` };
  })();

  const software = useQuery({
    queryKey: ["software", search],
    queryFn: () => api.searchSoftware(search),
  });
  const devices = useQuery({
    queryKey: ["software-devices", selected],
    queryFn: () => api.softwareDevices(selected as string),
    enabled: selected != null,
  });

  const items = software.data?.items ?? [];

  /**
   * A REQUEST, NOT A LINK -- the same defect Devices carried, in a second
   * place, written the same way and therefore broken the same way. A browser
   * following an `<a href="/api/v1/…" download>` of its own accord sends no
   * `Authorization` header, so the route answered 401 and the browser saved
   * the refusal body. Only the Devices one was reported; both had never
   * worked. `lib/download.ts` carries the reasoning in full.
   */
  const exporter = useMutation({
    mutationFn: () => api.exportInventory("software"),
    onSuccess: (blob) => saveBlob(blob, "software.xlsx"),
  });

  return (
    <>
      <PageHead
        // The page's name in the navigation (coordinator ruling, fix round 1: a page's h1 is its rail label).
        title={t("shell.nav.page.installedSoftware")}
        description={t("inventory.software.description")}
        actions={
          /* "Export CSV" WAS THE LABEL, AND IT WOULD NOW BE FALSE. The route
             behind it serves a workbook, and a button that names a format it
             does not hand you is the same class of lie as the one that made
             this report -- so the label names the CONTENT, which is what the
             operator is after either way. */
          <Btn
            label={t("inventory.software.export")}
            icon="download"
            onClick={() => exporter.mutate()}
            disabled={exporter.isPending}
          />
        }
      />

      {/* A REFUSED EXPORT HAS TO BE SAID OUT LOUD -- the anchor this replaces
          failed silently by construction, because the browser saved the 401
          body and the console never knew. */}
      {exporter.isError && (
        <p role="alert" className="form-error">
          {t("inventory.software.exportFailed")} {describeSubmitError(exporter.error)}
        </p>
      )}

      <Panel bare>
        <ListToolbar
          search={
            <SearchField
              placeholder={t("inventory.software.searchPlaceholder")}
              maxLength={128}
              value={search}
              onChange={(event) => filters.set("q", event.target.value)}
              aria-label={t("inventory.software.searchAria")}
            />
          }
          // No count before the list has answered: "Showing 0 of 0" would be an invented zero.
          end={software.data ? <ShowingCount shown={items.length} total={software.data.total} /> : null}
        />
        <QueryState loading={software.isLoading} error={software.error} empty={false} emptyLabel="">
          {items.length === 0 ? (
            <ListEmpty
              filtered={filters.active}
              onClear={filters.clear}
              nothingYet={t("inventory.software.emptyNone")}
              icon="package"
            />
          ) : (
            <TableWrap>
              <table aria-label={t("shell.nav.page.installedSoftware")}>
                <thead>
                  <tr>
                    <th scope="col">{t("inventory.software.thName")}</th>
                    <th scope="col">{t("inventory.software.thVersion")}</th>
                    <th scope="col">{t("inventory.software.thInstalls")}</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((item) => (
                    <tr
                      key={`${item.name}-${item.version}`}
                      aria-current={selected === item.name ? "true" : undefined}
                    >
                      {/* v3's row: the package glyph leads, the title is the
                          product, the sub-line the publisher. The title is
                          the selection, as a link: keyboard reachable,
                          middle-clickable, and a URL one technician sends
                          another ("the machines with the old Chrome"). */}
                      <Td lead={<Icon name="package" size={18} />}>
                        <div className="cell-title">
                          <Link to={hrefFor(item.name)}>{item.name}</Link>
                        </div>
                        <div className="cell-sub">{item.publisher}</div>
                      </Td>
                      <td className="mono">{item.version}</td>
                      <td className="mono">{item.device_count}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableWrap>
          )}
        </QueryState>
      </Panel>

      {/* THE CHOSEN TITLE'S COMPUTERS, under the list (redesign Task 6, 1.3),
          from `?title=`, so the view is a link; closing it takes `?title=`
          away and keeps the search. Its name says which list it closes: a
          bare "Remove" beside a software title could read as uninstalling it. */}
      {selected ? (
        <div className="mt-4">
          <Panel
            title={selected}
            subtitle={t("inventory.software.devicesReporting")}
            actions={
              <Link to={withoutTitle} aria-label={t("inventory.software.closeTitleAria", { title: selected })}>
                {t("inventory.software.closeTitle")}
              </Link>
            }
            bare
          >
            <QueryState
              loading={devices.isLoading}
              error={devices.error}
              empty={(devices.data ?? []).length === 0}
              emptyLabel={t("inventory.software.noDevices")}
              emptyIcon="devices"
            >
              <TableWrap>
                <table>
                  <thead>
                    <tr>
                      <th scope="col">{t("inventory.software.thDevice")}</th>
                      <th scope="col">{t("inventory.software.thVersion")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(devices.data ?? []).map((device) => (
                      <tr key={device.device_id}>
                        {/* The endpoint carries no OS family, so the lead is
                            the generic device glyph rather than a guessed
                            brand mark. */}
                        <Td lead={<OsIcon os="" />}>
                          <div className="cell-title">
                            <Link to={`/devices/${device.device_id}`}>{device.hostname}</Link>
                          </div>
                        </Td>
                        <td className="mono">{device.version}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </TableWrap>
            </QueryState>
          </Panel>
        </div>
      ) : null}
    </>
  );
}
