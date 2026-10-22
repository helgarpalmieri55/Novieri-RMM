# UX remediation programme — decomposition

**Date:** 2026-08-20
**Source:** `docs/superpowers/audits/2026-08-19-ux-audit-auditor-a.md` (134
findings, 16 routes) and `…-auditor-b.md` (99 findings, 16 routes), two
independent audits of the live deployment at `itms.toeshee.com`.
**Approved:** 2026-08-20, "full program, my sequencing".

---

## 1. What the audits found, and why it is not 233 pieces of work

233 findings: **16 BLOCKER, 81 HIGH, 89 MEDIUM, 47 LOW.**

They are not 233 defects. The auditors say so themselves, repeatedly and in
their own words:

> "Raw Pydantic JSON errors confirmed in a THIRD module — **this is a
> product-wide error-handling failure, not a /docs bug**"

> "Submit with an empty form is a silent no-op — **the seventh occurrence**"

> "'Run now' with an empty Device ID is a silent no-op — **the eighth
> occurrence**"

> "Ticket Description is a single-line `<input>` here too — **the defect is
> systemic to ticket creation**"

The same handful of missing primitives is being rediscovered on every route
the auditors walked. A module-by-module remediation would therefore fix each
one eight to twelve times, and would still leave the twelfth instance broken
when the audit is next run against a route nobody thought to check. The
programme below is organised by **cause**, not by route, and each cycle is
sized to the primitive it introduces.

The counter-risk is real and is worth stating: a cause-first programme leaves
individually-broken flows (patch policy, deployments, provisioning) broken
for longer than a blocker-first one would. Cycle 7 exists to close that, and
it is placed after the primitives precisely so that it can use them instead
of hand-rolling a twelfth variant.

## 2. Verified before planning

Two of the audits' BLOCKERs were confirmed in the source within seconds of
reading them, which is the reason the rest of both documents is being treated
as credible rather than sampled:

| Finding | Source |
|---|---|
| "Clicking the profile chip signs the user out instantly — no menu" | `frontend/src/shell/AppShell.tsx:85` — `<button className="profile-btn" onClick={onSignOut}>` |
| "Tenant switcher is a dead control — looks like a dropdown, does nothing" | `frontend/src/shell/AppShell.tsx:81` — `<button className="tenant-switcher" type="button">`, no handler |

A third — "Sign-out redirect is misconfigured … Invalid redirect uri" — was
traced to realm drift rather than to frontend code, and shares its cause with
the operator-reported failure that opened this cycle (§3).

## 3. Cycle 1 is not from the audit

The operator reported, on the live deployment, that **Change password** and
**Add a passkey** do nothing: the page reloads and returns to Settings. Root
cause: the realm JSON that built that Keycloak declares a `requiredActions`
array containing exactly one entry, `CONFIGURE_TOTP`. Keycloak's realm import
does not merge that array with the default set — if it is present, it *is* the
set. `UPDATE_PASSWORD` and `webauthn-register-passwordless` were therefore
never registered, and `kc_action` naming an unregistered action is ignored:
the user is re-authenticated and redirected back, silently.

Falsifiable, and worth stating as a prediction rather than a conclusion:
**`CONFIGURE_TOTP` ("Manage MFA device") should work on that same deployment
while the other two do not.** If all three fail, this diagnosis is wrong.

The audit's sign-out BLOCKER has the same shape: the live `it-suite-web`
client still carries `redirectUris: ["https://*/auth/callback"]`, and Keycloak
honours a wildcard only in the trailing position, so `post.logout.redirect.uris:
"+"` resolves to a set matching nothing.

Three live failures, one cause: **a running realm is never re-imported, and
nothing converges it.** `keycloak-init` converges clients and secrets and stops
there. Cycle 1 extends it to realm-level state. This is not only the
operator's install — it is every future customer whose realm predates a
realm-file change, which after the first sale is all of them.

## 4. The cycles, in dependency order

| # | Cycle | Retires | Depends on |
|---|---|---|---|
| 1 | Realm convergence in `keycloak-init` | 1 BLOCKER + the operator's live bug | — |
| 2 | App chrome: user menu, `/profile` route, tenant control, sign-out, session expiry, skip link | 2 BLOCKER, ~10 HIGH | 1 (sign-out needs the client fixed) |
| 3 | Form kernel: end the silent no-op | 1 BLOCKER, ~45 findings | — |
| 4 | One error surface: no raw JSON in the UI | 3 BLOCKER, ~20 | 3 (the failure path is the form's) |
| 5 | Datetime: the 5-hour shift | 1 BLOCKER, ~6 | 2 (needs the profile timezone) |
| 6 | Entity pickers: kill the hand-typed UUID | ~12 | 3 |
| 7 | Module blockers: flows that cannot complete | 6 BLOCKER, ~10 | 3, 4, 6 |
| 8 | Destructive actions: confirm, and mean it | 1 BLOCKER, ~10 | 3 |
| 9 | Accessibility and contrast | ~15 | 2 |
| 10 | Responsive layout | ~6 | 2 |
| 11 | Data display: charts, JSON blobs, empty tiles | ~10 | 4 |
| 12 | The long tail, then re-audit | remainder | all |

Cycles 3 and 1 are independent and could run in either order; 3 is the larger
win and 1 is the one the operator is currently blocked by, so 1 goes first on
urgency, not on size.

## 5. What "retires" has to mean

A finding is retired when the behaviour it describes no longer occurs **and a
test would fail if it returned**. The audits are checked into
`docs/superpowers/audits/` for exactly this reason: they are the acceptance
criteria, not background reading. Cycle 12 ends by walking both documents
finding by finding and reporting, in writing, which are closed, which are
open, and which were judged not worth fixing — with the reason. A programme
that reports "233 findings addressed" without that walk has not been checked;
it has been asserted.

## 6. Out of scope, named so it is a backlog and not an omission

The audits describe product behaviour, so nothing they raise is out of scope
on principle. Two things they raise are nonetheless **not** UI work and are
tracked elsewhere: "Four of five jobs have sat `queued` with 0 attempts for
over 24 hours" is a worker/dispatch defect that the dashboard merely fails to
report (cycle 11 fixes the reporting; the dispatch defect needs its own
investigation), and "The platform is genuinely DOWN" was the state of the
deployment during the audit rather than a finding about the interface.

## 7. Closed out, 2026-08-21

All twelve cycles are done. The walk §5 demands is written up finding by
finding in
[`docs/superpowers/audits/2026-08-21-ux-remediation-closeout.md`](../audits/2026-08-21-ux-remediation-closeout.md):
233 verdicts, one per finding, no gaps — **209 closed, 12 open, 12 not
defects** (the audits' own `Positive:` entries).

Three things about that document are worth carrying forward rather than
leaving inside it.

**The verdicts were produced against the code, not against the record of the
work.** Twelve verification agents were each handed the verbatim text of their
findings and told not to trust a commit message, a code comment, a previous
agent's report, or a plausible-looking component. A finding counts as closed
only where the behaviour is gone **and** a test exists that would fail if it
returned — the bar this section already set, applied by someone who did not
write the fix.

**Nothing was marked WONTFIX.** The verdict was available and unused. Every one
of the twelve still open is open because the console cannot show or do what the
API does not expose, and each entry names the routes that were searched for.
The closeout carries those as ten numbered backend recommendations; they are
normal module changes under the build discipline in `CLAUDE.md`, not UI work,
which is why this programme stopped at naming them.

**The estimate in §1 was right for the wrong reason and wrong for the right
one.** 233 findings really were a handful of kernel defects seen from many
pages — the shared modal alone closed six findings on six pages with one
`useEffect`. But two of the twelve cycles' worth of work went into defects
*introduced by* the remediation, found only because the suite caught them: a
`Button` with no `type` turned every Cancel inside a form into a submit, and
the modal's focus effect keyed on an inline `onClose` silently ate every
keystroke. Both were invisible to review and obvious to a test.

What remains undone is a browser pass. Every verdict in the closeout is about
code and tests; none of it says whether the console *looks* right at a real
viewport, and jsdom cannot answer that. `/preview` now renders every primitive
this programme added, which is where that pass starts.
