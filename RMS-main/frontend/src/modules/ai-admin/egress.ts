// Spec §11.2's egress disclosure, as ONE constant.
//
// **THIS IS THE HONEST ANSWER AND IT IS A LOT.** It is written here once and
// rendered verbatim by `AiSettingsPage` and by Settings → Assistant, so what an
// administrator is shown cannot drift from what the spec says leaves.
//
// **IT IS INFORMATION, NOT CONSENT, AND THAT CHANGED ON 2026-08-25** (spec
// §0.14). There used to be a tickbox beside this list, and a server-side
// `egress_acknowledged` behind that. Both are gone with the per-org enable they
// gated: the provider, its model and its key are now the deployment's `.env`,
// and WRITING THAT FILE IS THE CONSENT — deliberate, server-side, made by
// whoever runs the control plane, and reviewable in configuration management,
// which a click never was.
//
// THE LIST STAYS, AND STAYS VERBATIM. What the console owes the administrator
// is an accurate account of what leaves their network, and that obligation has
// nothing to do with who ticked what. It is rendered on both surfaces from this
// one constant so the two cannot drift, and the settings tests assert each line
// is on the page rather than that "some disclosure rendered".

// EACH FIELD IS READ FROM THE CATALOGUE WHEN IT IS READ, not when this module
// loads (`get`), and `leaves` / `neverLeaves` are rebuilt on every read. A
// constant built at load time would freeze the language the page started in,
// and this is the one list the console owes an administrator in the language
// they read: what leaves their network. Spanish says the same things the
// English does -- nothing is softened, shortened or merged.
import { translate, type MessageKey } from "../../i18n/core";

const LEAVES: readonly MessageKey[] = [
  "aiAdmin.egress.leaves1",
  "aiAdmin.egress.leaves2",
  "aiAdmin.egress.leaves3",
  "aiAdmin.egress.leaves4",
];
const NEVER_LEAVES: readonly MessageKey[] = [
  "aiAdmin.egress.never1",
  "aiAdmin.egress.never2",
  "aiAdmin.egress.never3",
];

export const EGRESS_DISCLOSURE: {
  readonly heading: string;
  readonly leaves: readonly string[];
  readonly neverLeaves: readonly string[];
  readonly contractual: string;
  readonly selfHosted: string;
} = {
  get heading() {
    return translate("aiAdmin.egress.heading");
  },
  get leaves() {
    return LEAVES.map((key) => translate(key));
  },
  get neverLeaves() {
    return NEVER_LEAVES.map((key) => translate(key));
  },
  // §11.2's closing paragraph, which is a deployment fact rather than a code
  // guarantee and is labelled as one here for the same reason it is labelled as
  // one in the spec.
  get contractual() {
    return translate("aiAdmin.egress.contractual");
  },
  get selfHosted() {
    return translate("aiAdmin.egress.selfHosted");
  },
};
