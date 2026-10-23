import { useState } from "react";
import { Link } from "react-router-dom";
import { EmptyState } from "../../components/emptystate";
import { Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import type { MessageKey } from "../../i18n/core";
import { useIpamSearch } from "./api";
import { ErrorLine } from "./ErrorLine";
import { IpamNav } from "./IpamNav";
import { searchKindMeaning } from "./ipamState";
import { Icon } from "../../shell/icons";
import { Facts } from "../../shell/viz";
import { Btn, PageHead, Panel, SearchField, StatusPill } from "../../shell/components";

// One box (spec §9). Operators do not know in advance whether the thing in
// their hand is an IP, a CIDR, a MAC, a hostname or a device name, so there is
// one endpoint and it decides.
//
// ON THE v3 KIT (`P['ipam/search']` in docs/design/itms-prototype-v3.html):
// one panel, the search field at the top of it, the results as rows under it.
// The API answers with three lists of identifiers and a count, so the rows
// are grouped by what they are, each led by the glyph of its kind.
//
// THE INTERPRETATION IS SHOWN. `kind` says HOW the query was read, because a
// hostname that happens to parse as something else gets a different question
// answered than the one that was asked, and an empty result for the wrong
// question looks exactly like an empty result for the right one.
const SEARCH_KIND_WORD: Readonly<Record<string, MessageKey>> = {
  address: "ipam.search.kind.address",
  cidr: "ipam.search.kind.cidr",
  mac: "ipam.search.kind.mac",
  text: "ipam.search.kind.text",
};

export function SearchPage() {
  const t = useT();
  const [draft, setDraft] = useState("");
  const [query, setQuery] = useState("");
  const results = useIpamSearch(query);

  return (
    <div>
      <PageHead
        title={t("ipam.search.title")}
        description={t("ipam.search.description")}
      />
      <IpamNav />

      <Panel>
        <form
          className="tb"
          role="search"
          onSubmit={(event) => {
            event.preventDefault();
            setQuery(draft.trim());
          }}
        >
          <div className="tb-l" style={{ flex: 1 }}>
            <SearchField
              aria-label={t("ipam.search.aria")}
              // i18n-exempt: example addresses and a hostname, identifiers written the same in every language
              placeholder="10.4.7.9, 10.4.7.0/24, a4:bb:6d:11:22:33, ws-01"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
            />
          </div>
          <div className="tb-r">
            <Btn label={t("ipam.search.searchBtn")} icon="search" variant="primary" type="submit" />
          </div>
        </form>

        {/* THE RESTING STATE, and it is the kit's `EmptyState` (design review
            I3). Before the first search this route drew the box and then
            nothing at all: bare page background from under the input to the
            bottom of the viewport, at 390 and at 1440 alike, which reads as a
            route that was never finished rather than one waiting for a query.
            Every sibling surface in this console answers the same question
            with the same component -- /software "No software selected",
            /bcp/report "No period chosen", /remote/:id, /changes/:id -- so the
            fix is that component and not a sentence of this page's own.

            IT IS GATED ON THE QUERY, NOT ON `results.data`. `useIpamSearch` is
            `enabled` only for a non-empty query, so `data` is also undefined
            for the instant a real search is in flight; keying on the query
            keeps this out of the way the moment one is submitted, and lets the
            "Searching…" line below own that instant.

            NO FIXTURE IS REACHED. Nothing is fetched to draw it, so headless
            rendering of /ipam/search has one less answer to need. */}
        {query.trim() === "" && !results.isLoading && (
          <EmptyState
            icon="search"
            // THE PANEL HERE HAS NO TITLE, so it renders no `h2` and the
            // default `h3` would follow the page `h1` directly. axe reported
            // `heading-order` at both widths; the harmony gate passed it
            // anyway, because that rule is `moderate` and the gate fails on
            // `serious`.
            headingLevel={2}
            title={t("ipam.search.emptyTitle")}
            body={t("ipam.search.emptyBody")}
          />
        )}

        {results.isLoading && <p className="note">{t("ipam.search.searching")}</p>}
        {results.error && <ErrorLine error={results.error} />}

        {results.data && (
          <div>
            <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12 }}>
              <StatusPill
                value="read-as"
                kind="info"
                label={t("ipam.search.readAs", {
                  kind: Object.hasOwn(SEARCH_KIND_WORD, results.data.kind)
                    ? t(SEARCH_KIND_WORD[results.data.kind] as MessageKey)
                    : results.data.kind,
                })}
              />
              <span className="note">{searchKindMeaning(results.data.kind)}</span>
            </div>

            <Facts
              items={[
                { k: t("ipam.search.subnets"), v: <span className="mono">{results.data.subnets.length}</span> },
                {
                  k: t("ipam.search.addresses"),
                  v: <span className="mono">{results.data.addresses.length}</span>,
                },
                { k: t("ipam.search.devices"), v: <span className="mono">{results.data.devices.length}</span> },
                {
                  k: t("ipam.search.observations"),
                  v: <span className="mono">{results.data.observations}</span>,
                },
              ]}
            />

            <Table>
              <thead>
                <tr>
                  <Th>{t("ipam.search.colResult")}</Th>
                  <Th>{t("ipam.fields.kind")}</Th>
                </tr>
              </thead>
              <tbody>
                {results.data.subnets.map((id) => (
                  <tr key={`subnet-${id}`}>
                    <Td lead={<Icon name="network" size={18} />}>
                      <div className="cell-title">
                        <Link className="mono" to={`/ipam/subnets/${id}`}>
                          {id}
                        </Link>
                      </div>
                    </Td>
                    <Td>{t("ipam.search.kindSubnet")}</Td>
                  </tr>
                ))}
                {results.data.addresses.map((address) => (
                  <tr key={`address-${address}`}>
                    <Td lead={<Icon name="device-desktop" size={18} />}>
                      <div className="cell-title mono">{address}</div>
                    </Td>
                    <Td>{t("ipam.search.kindAddress")}</Td>
                  </tr>
                ))}
                {results.data.devices.map((id) => (
                  <tr key={`device-${id}`}>
                    <Td lead={<Icon name="devices" size={18} />}>
                      <div className="cell-title">
                        <Link className="mono" to={`/devices/${id}`}>
                          {id}
                        </Link>
                      </div>
                    </Td>
                    <Td>{t("ipam.search.kindDevice")}</Td>
                  </tr>
                ))}
                {results.data.subnets.length === 0 &&
                  results.data.addresses.length === 0 &&
                  results.data.devices.length === 0 && (
                    <tr>
                      <Td colSpan={2}>
                        <span className="note">
                          {t("ipam.search.noMatch")}
                        </span>
                      </Td>
                    </tr>
                  )}
              </tbody>
            </Table>
            <p className="note" style={{ marginTop: 10 }}>
              {t("ipam.search.sightings")}
            </p>
          </div>
        )}
      </Panel>
    </div>
  );
}
