# Product — what ITMS is, and what it does

**Audience:** anyone who needs to understand the product without reading the
code — a new engineer on day one, an evaluator, a buyer, an auditor.

**Relationship to the other docs.** `00-vision.md` says *why this exists and
who it is for*. This document says *what it is and what it does*, concretely
and as measured. `01-architecture.md` says *how it is built*. Where this
document states a number, that number was read off the tree or the test
suite on **2026-08-16**, and the section says which. Where something is
planned rather than built, it says so.

<!-- impeccable:product-schema 1 -->

## Platform

web

<!-- The heading above is read by tooling (`.claude/skills/impeccable`), which
     looks for a `## Platform` section and takes its first non-empty line. The
     value describes the DESIGN LANGUAGE the product renders, not the
     toolchain: the console is a browser application, so `web`. The agent is a
     headless Go binary with no interface, and the site server has none either,
     so neither changes this answer. -->

---

## 1. In one paragraph

ITMS is a self-hosted IT management platform. You install one
lightweight agent on every computer and server you manage; from a single web
console your IT team inventories that fleet, patches it, deploys software to
it, takes remote-control sessions on it, backs it up, documents it, and runs
the governance workflows an auditor asks for. It is one control plane, one
agent, and — where a site needs LAN-local work — an optional site server.
It runs in your rack. Nothing about a managed endpoint leaves your network
unless you configure it to.

---

## 2. The shape of the system

Three pieces, and the boundaries between them are the product's main
structural decisions.

**The control plane.** A FastAPI application, PostgreSQL, Redis, and an
object store, behind a reverse proxy. It holds the fleet's state, decides
what should happen, signs the instructions, and serves the console. Every
business capability is a module registered into a shared kernel — modules do
not fork the kernel, they extend it at defined points.

**The agent.** One static Go binary per endpoint, on Windows, Linux and
macOS. It connects *outbound only*, over mutual TLS, and parks on a single
long-lived connection. It never listens on a port. It executes only
instructions the server has cryptographically signed, and it verifies every
signature before acting. When idle it is designed to be invisible: one
blocking read, timers rather than polling, heavy capabilities dormant until
a job for them actually arrives.

**The site server (optional).** For LAN-heavy work — imaging, large
deployments, local caching — a site can run a relay so that bulk traffic
stays local rather than crossing the WAN twice.

---

## 3. What it does today

Measured on 2026-08-16 by `backend/tests/test_roadmap_status.py`, which reads
the **live router** rather than counting decorators in source: **376 routes
mounted under `/api/v1` — 358 across 25 business modules, 18 from the kernel
itself.** Grouped by what an operator would call them:

### Fleet and inventory
Enrollment and certificate lifecycle, device records, hardware/software
inventory, IP address management, and network discovery. The agent's
`inventory` and `netsweep` capabilities feed this.

### Keeping machines current
Patch management and application deployment, both driven by the signed-job
engine, with the agent's `patch` and `deploy` capabilities doing the work on
the endpoint.

### Access and support
A service desk with SLA tracking, an end-user portal for requesters, and email
intake so a mailbox becomes tickets. **Remote control does not connect yet:**
the server can send `remote.session.open`, but the agent has no handler for
it (`agent/internal/wire/schema_test.go:116`), so the console's Start Session
button cannot open a session today. Credential decryption at point of use is
built and enforced for the capabilities that do run jobs; see §4.

### Continuity
BCP/DR test recording and endpoint health with alerting. **Backup and restore
is built but unproven:** the snapshot providers and prefix-scoped restore
reads through the gateway exist and are wired, but `backup.run` has never
completed end to end on any platform (`HANDOFF.md` §5, "What has never
executed"); see `docs/reviews/2026-09-23-platform-critique.md` §3.

### Governance and the paperwork
Change management, security-log collection, documentation, licensing,
subscriptions, budget, and reporting. These exist because the platform that
*performs* the changes is the only honest place to *evidence* them.

### The platform's own operations
Structured logging with redaction at the sink, a platform health and status
page, an error monitor, and a verified backup/restore and rollback
procedure. This is the newest work and is described in §6.

### AI assistance
An operator copilot, a portal-facing assistant, and an admin surface for
configuring both. Enabling a provider that sends data outside your network
requires an explicit, audited acknowledgement per organisation.

**Scale of the thing, measured:** ~98,600 lines of Python, ~68,400 of Go,
~48,700 of TypeScript; 25 backend module packages; 29 frontend feature
folders; 58 database migrations; 125 tables.

---

## 4. The properties that are not features

These are enforced, not aspired to. They are the reason the product can be
trusted with domain-admin-equivalent reach over a fleet.

| Property | How it is actually enforced |
|---|---|
| **No unsigned command path** | Every job is signed server-side and verified by the agent before execution. There is no bypass — not for testing, not for internal tools. |
| **Agents never accept connections** | Outbound-only, mutual TLS. No listening port on any endpoint. |
| **Credentials are decrypted at point of use** | They live encrypted in the vault, are never returned by an API, and never reach a log. |
| **Every privileged action is audited** | An append-only, hash-chained log: job dispatch, credential access, remote sessions, policy and config changes. A restore rewinds it, and the runbook says so. |
| **Tenant isolation is in the database** | Row-Level Security, `FORCE`d so it applies even to the owning role. Measured at head: **133 policies across 122 tables**. Not application-layer filtering. |
| **Authorization is server-side** | **85 permission codes**; the frontend never gates security. RBAC completeness is *derived from the router* by reflection, so a new route cannot quietly ship ungated. |
| **The agent stays out of the way** | A hard idle budget design target — low-tens-of-MB resident, ~0% CPU. CI does not measure that number directly; it runs four falsifiable proxies against real code (zero allocations on an idle wakeup, a constant goroutine count, no gateway write on a quiet tick, and flat allocation across many ticks — `agent/internal/budget/budget.go`), and RSS itself is a real `/proc/self/statm` reading only on the Linux runner, falling back to a heap-allocation proxy on Windows and macOS. |

---

## 5. What it deliberately does not do

Saying this plainly is part of the product.

- **It is not a SIEM.** It collects and ships security logs; it does not
  correlate and alert like a detection platform.
- **It is not an MDM — today.** Computers and servers only; phones and tablets
  are not managed. **This boundary was reversed as a decision on 2026-08-16**
  (true MDM, including phones and tablets, is now in scope) **and no code
  exists yet.** Until it does, this line describes the product accurately and
  should be read as present tense, not as a commitment either way. See
  `docs/00-vision.md`.
- **It is not a network monitoring suite.** It discovers and inventories;
  it does not replace an NMS.
- **It does not phone home.** No telemetry to a vendor. The deployment is
  yours.
- **`docs/00-vision.md` §"Scope boundaries" is the authoritative list.**

---

## 6. Operational maturity, honestly

The platform recently completed a four-part observability and operations
programme, and the state it left behind is worth stating precisely because
it is the part most products overstate.

**Verified, by something that runs:**
- Structured JSON logging with redaction at the sink, and an AST sweep that
  pins the exact number of log call sites so the discipline cannot drift
  unnoticed.
- A platform status page over infrastructure probes and a module registry.
- An error monitor that stores **no exception text** — type, raise site and
  correlation ids only — proven by draining a genuinely secret-bearing
  exception into Postgres and searching every column of both tables.
- Migration correctness: `upgrade head → downgrade base → upgrade head` runs
  in CI with a schema diff and a leftover-objects assertion.
- Backup and restore, exercised end to end between two purpose-built
  clusters, with the restore asserting its own end state (RLS policy count
  against the count the backup recorded; the application role narrowed).
- Versioned images, so a rollback retags rather than rebuilds.

**Not yet verified, and labelled as such wherever it is claimed:**
- A `git pull`-over-an-older-release upgrade on a live host.
- A rollback on a live host.
- The Keycloak realm restore beyond "the command runs".
- That `docker compose up -d` *reuses* a tagged image rather than rebuilding
  it — the CI check proves tag interpolation only, and says so in its own
  comment.

**One exposure found and closed rather than hidden:** the console's
WebSocket token used to travel in the query string — browser WebSocket
clients cannot set headers — and so landed verbatim in the server's access
line, a credential in the log that no redaction rule could reach. It now
travels in the WebSocket subprotocol header, which is a request header and
is not logged; a query-string token is refused. Measured against a real
server, with a control that still reproduces the old exposure so the check
cannot pass vacuously (`backend/tests/test_ui_ws.py`).

---

## 7. How it is deployed

On-premises today: Docker Compose, everything in your rack — PostgreSQL,
Redis, MinIO, Keycloak for identity, OpenBao for secrets, Traefik in front.
`deploy/GO-LIVE.md` is the checklist of what must be changed from defaults
before anything real touches it; `deploy/README.md` is the runbook for first
boot, upgrade and rollback.

Cloud-ready by design rather than by rewrite: multi-tenancy, outbound-only
agents and the site-server role exist from day one, so putting the control
plane in a cloud and site servers on customer LANs is a deployment change.

---

## 8. Who uses it, and in what situation

Stated here rather than only in `00-vision.md` because every interface
decision depends on it, and a design brief that has to go and find it will
sooner or later guess instead.

**Today, and the only real deployment: Toeshee's own IT function.** ~250–300
endpoints across a BPO operation. The people in the console are a small
in-house team doing patching, imaging new-hire machines, helpdesk remote
sessions and backup assurance — generalists who own every module rather than
specialists who each own one. They are not visiting; the console is open all
day, on a desk, beside a ticket queue. The console's density is a response to
that, not an accident.

**Next, and not yet real: small and mid-sized IT teams, and MSPs.** The MSP
case is one console over many client organisations, which is why
multi-tenancy is in the database rather than in the application. See
`00-vision.md` "Who it's for" for the commercial framing and
"Positioning against alternatives" for what this displaces.

**The second audience nobody designs for: the auditor.** Governance,
evidence and reporting exist because the platform that *performs* a change is
the only honest place to *evidence* it. That reader is not a user of the
fleet; they read output. It is why "0 breached" must never be able to mean
"nothing was measured".

---

## 9. Brand commitments, and what is deliberately still open

**`ITMS` IS A PLACEHOLDER. The product name has not been chosen.** Confirmed
by the operator on 2026-08-21. It is currently the visible name in 46 places
across 18 frontend source files, in three files of the sign-in theme, and in
`<title>`. Nothing may harden an identity around it — no wordmark built from
those four letters, no logo whose meaning depends on them, no marketing
surface that treats it as settled. When the name is chosen it is a rename,
and the count above is the size of that job.

**No user-visible string may name the identity vendor.** Enforced, not
requested: `frontend/src/lib/noVendorNames.test.ts` scans string literals and
JSX text and fails the build. The rule is scoped to what a user can see —
imports, type names and comments are exempt, because the platform does
integrate with Keycloak and source that pretended otherwise would be lying
about its own dependency. The reasoning generalises: a buyer should never
need to learn that there is a second system behind the one they bought.

**Voice, as practised in the product's own copy.** Say what happened and what
follows from it. Name the thing that failed and what was and was not changed.
No reassurance in place of information — "Nothing was changed" is a fact an
operator can act on; "Oops!" is not.

---

## 10. Evidence on hand

**There is none yet, and that is the point of recording it.** Confirmed by
the operator on 2026-08-21: the internal deployment is the first customer and
the proving ground, so as of this date there are **no customer names, no
testimonials, no case studies, no third-party benchmarks, no press and no
published pricing.**

Future work — a marketing surface especially — must not invent any of them.
The only quotable figures are the measured ones in this document and in the
test suite that produced them, and §13 governs how those are updated.

---

## 11. Accessibility

**WCAG 2.2 Level AA is a product requirement.** Confirmed by the operator on
2026-08-21. It is not a target to trade against a visual idea: a design that
cannot reach AA is the wrong design.

What holds it, today, in CI:

| Guard | What fails the build |
|---|---|
| `frontend/src/test/contrast.test.ts` | Any token pair the console paints as small text falling under 4.5:1. The ratios are **computed** from the shipped tokens rather than transcribed, and the file carries both auditors' original measurements as a live falsifier, so a guard that stops being able to see the defect goes red rather than green. |
| the same file | Any `font-size` below 12px in the four shell stylesheets, or written inline in JSX — the second half exists because the first half passed green while three sizes shipped below the floor. |
| `frontend/src/test/responsive.test.ts` | A missing breakpoint, a drawer that is off-screen but still in the tab order, or a touch target under 44px where the pointer is coarse. |
| `frontend/src/test/tokens.test.ts` | A palette that has drifted from `docs/design/itms-prototype.html`. A deliberate change has to be made in the design first. |

**What none of that covers, stated so nobody mistakes green for accessible.**
These read tokens and stylesheets as text. jsdom implements no cascade and
evaluates no media query, so no test here can see a colour composited by
opacity, painted by a gradient, or the actual rendered result at a real
viewport. A browser pass with axe over the running console is the complement.

**That pass was run on 2026-08-21** against the dev-only harness at
`/__preview`, in headless Chromium, with axe-core plus a sweep that measures
composited and gradient colour from the rendered pixels:
`docs/superpowers/audits/2026-08-21-browser-axe-pass.md`. It found what the
paragraph above predicted it would — including a destructive-confirmation
dialog that renders in the document flow underneath its own scrim, and a
visually-hidden table that scrolls the page sideways at every viewport, neither
of which any text-reading guard can express. Two contrast defects were fixed
and guarded; the rest are reported there and **open**.

`frontend/scripts/a11y-browser.mjs` is that pass, runnable and falsifiable
(`--falsify` reproduces auditor A's 3.49:1 before it measures anything else).
It runs headless in ~45s and exits non-zero on a violation, so it could be a CI
job; it is deliberately not wired into one, because the browser download is a
cost the operator chooses.

The full record of what was found and fixed is
`docs/superpowers/audits/2026-08-21-ux-remediation-closeout.md`: 233 findings
from two independent auditors, 209 closed, 12 open, 12 not defects.

---

## 12. Where to read next

| You want | Read |
|---|---|
| Why this exists, who it's for, positioning, licensing | `00-vision.md` |
| How the pieces fit together | `01-architecture.md` |
| Language, framework and version choices | `02-tech-stack.md` |
| The security model in full | `03-security-model.md` |
| Schema and tenancy | `04-data-model.md` |
| What the agent is and how it behaves | `05-agent-spec.md` |
| Build order and phase status | `06-roadmap.md` (and `test_roadmap_status.py`, which is the authority) |
| Backup, restore and the audit chain | `07-backup-restore.md` |
| How the platform reports on itself | `08-observability.md` |
| A specific capability | `docs/modules/<module>/spec.md` |
| Deploying it | `deploy/README.md`, `deploy/GO-LIVE.md` |

---

## 13. A note on how this document is maintained

Every number here is measured and dated. This project has been bitten
repeatedly by status claims that nothing forced anyone to re-check — a
roadmap table that understated the project by fifteen phases, an AI tool
surface stale in three documents at once, six named falsifiers that had never
run. The lesson recorded across those incidents applies to this file too:

**If you update a number here, take it from the tree or the test suite, and
date it. If you cannot measure it, say it is unmeasured.**
