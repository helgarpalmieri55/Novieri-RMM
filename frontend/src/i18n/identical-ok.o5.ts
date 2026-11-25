/**
 * IDENTICAL_OK entries of wave O5 (Task 10): the catalogues `licenses`, `subscriptions`, `ipam`.
 * The wave adds and removes its own entries here and nowhere else, so the eight
 * waves that run in parallel never edit one file (redesign plan, "Archivos
 * compartidos"); `catalogue.identical.test.ts` holds each file to its catalogues.
 */
export const IDENTICAL_OK_O5: Record<string, string> = {
  "ipam.nav.label": "\"IPAM\" is an acronym (IP address management), written the same in Spanish.",
  "ipam.fields.vlan": "\"VLAN\" is an acronym (virtual LAN), written the same in Spanish.",
  "ipam.fields.mac": "\"MAC\" is an acronym (media access control), written the same in Spanish.",
  "ipam.fields.vrf": "\"VRF\" is an acronym (virtual routing and forwarding), written the same in Spanish.",
  "ipam.vlan.label": "Only a placeholder and the acronym \"VLAN\", written the same in Spanish.",
  "ipam.vlan.named": "Only placeholders and the acronym \"VLAN\", written the same in Spanish.",
  "ipam.fields.vrfOfClient": "Only placeholders (the VRF's name and its client's) and a separator, the same in Spanish.",
  "ipam.fields.siteOfClient": "Only placeholders (the site's name and its client's) and a separator, the same in Spanish.",
  "ipam.importExport.vrfOption": "Only placeholders (the VRF's name and its client's) and a separator, the same in Spanish.",
  "ipam.addressStatus.dhcp.label": "\"DHCP\" is an acronym (dynamic host configuration protocol), written the same in Spanish.",
  "ipam.allocation.label.dhcp": "\"DHCP\" is an acronym (dynamic host configuration protocol), written the same in Spanish.",
  "ipam.allocation.label.slaac": "\"SLAAC\" is an acronym (stateless address autoconfiguration), written the same in Spanish.",
  "ipam.allocation.label.manual": "\"Manual\" is the same word in Spanish.",
  "ipam.allocation.label.pool": "\"Pool\" is the loanword Spanish IT uses for an address pool (\"pool DHCP\").",
  "ipam.sweepRun.head": "Only placeholders: the already-worded state and the age, both worded where they are supplied.",
  "ipam.importAction.error.label": "\"Error\" is the same word in Spanish.",
  "ipam.subnet.dns": "\"DNS\" is an acronym (domain name system), written the same in Spanish.",
  "ipam.subnet.inVrf": "Only a placeholder and the acronym \"VRF\", written the same in Spanish.",
  "ipam.registry.colRd": "\"RD\" is an acronym (route distinguisher), written the same in Spanish.",
  "ipam.registry.vid": "\"VID\" is an acronym (VLAN identifier), written the same in Spanish.",
  "licenses.new.costPlaceholder": "An example amount, digits only, written the same in Spanish.",
  "licenses.new.currencyPlaceholder": "\"USD\" is an ISO 4217 currency code, written the same in Spanish.",
  "licenses.vendors.websitePlaceholder": "The start of a web address, a technical identifier written the same in Spanish.",
  "subscriptions.picker.ownerStatus": "Only placeholders: an address and the already-worded account status.",
  "subscriptions.detail.plan": "\"Plan\" is the same word in Spanish.",
  "subscriptions.detail.no": "\"No\" is the same word in Spanish.",
  "subscriptions.renewals.summary": "Only placeholders: the already-worded count in the window and count needing action.",
};
