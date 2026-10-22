import { useMutation, useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { EmptyState } from "../../components/emptystate";
import { describeSubmitError } from "../../components/form";
import { useT } from "../../i18n/context";
import { Td } from "../../components/ui";
import { useApi } from "../../lib/ApiContext";
import { saveBlob } from "../../lib/download";
import { Btn, FilterBar, PageHead, Panel, QueryState, SearchField, TableWrap } from "../../shell/components";
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
  const [params, setParams] = useSearchParams();
  const search = params.get("q") ?? "";
  const selected = params.get("title");
  const setSearch = (value: string) => {
    const next = new URLSearchParams(params);
    if (value === "") next.delete("q");
    else next.set("q", value);
    setParams(next, { replace: true });
  };
  const hrefFor = (title: string) => {
    const next = new URLSearchParams(params);
    next.set("title", title);
    return `?${next.toString()}`;
  };

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
        title={t("inventory.software.title")}
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

      <div className="grid grid-2">
        <Panel bare>
          <FilterBar>
            <SearchField
              placeholder={t("inventory.software.searchPlaceholder")}
              maxLength={128}
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              aria-label={t("inventory.software.searchAria")}
            />
            <span className="filter-spacer" />
            {/* THE COUNT USED TO BE A FULL-WIDTH STAT CARD, and `StatCard`
                requiring a destination is what ended that. It counted the very
                table it sat above: there is no other list of software titles
                to open, so the tile could only ever have linked to the page
                the reader was already on. It is a result count, so it sits
                where v3 puts one -- on the toolbar's right, in mono. */}
            <span className="note mono" data-testid="software-count">
              {software.data
                ? t("inventory.software.titles", { count: software.data.total })
                : "…"}
            </span>
          </FilterBar>

          <QueryState
            loading={software.isLoading}
            error={software.error}
            empty={items.length === 0}
            emptyLabel={
              search === "" ? t("inventory.software.emptyNone") : t("inventory.software.emptySearch")
            }
            emptyIcon="package"
          >
            <TableWrap>
              <table>
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
                          the selection, as a link. */}
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
          </QueryState>
        </Panel>

        <Panel
          title={selected ?? t("inventory.software.installedOn")}
          subtitle={selected ? t("inventory.software.devicesReporting") : undefined}
          bare
        >
          {selected ? (
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
          ) : (
            /* THE KIT'S EMPTY STATE, not a hand-rolled one. The bare `<svg>`
               took Tailwind preflight's `display:block` and sat flush against
               the panel's left padding while the sentence centred underneath,
               so the glyph and the words did not read as one unit;
               `EmptyState` wraps it in `.empty-icon` for that. It also
               requires the sentence saying WHY the panel is empty, which
               "Select a software row." never said -- an operator who has not
               yet worked out that the inventory list drives this panel is
               exactly the one reading this.

               AND IT NAMES NO DIRECTION. The copy read "in the list to the
               left", which is true at 1440 and false at 390: below the kit's
               640px fold the two panes stack, so the list is ABOVE, not
               beside. A width-aware sentence would be a second place for the
               breakpoint to be written down and a second place for it to rot
               when the fold moves; naming the list instead is true at every
               width and cannot go stale. */
            <EmptyState
              icon="package"
              title={t("inventory.software.noneSelected")}
              body={t("inventory.software.chooseTitle")}
            />
          )}
        </Panel>
      </div>
    </>
  );
}
