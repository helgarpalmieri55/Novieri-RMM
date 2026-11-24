# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

The primary user is a technician on an in-house IT team or at an MSP, with the
console open all day on a desk beside a ticket queue: patching, onboarding and
imaging machines, remote sessions, backup assurance, and the evidence an
auditor asks for. They are generalists who touch every module. They also open
the console from a phone during an incident.

Two other audiences are confirmed: the auditor, who reads output (reports, the
audit log) rather than operating the fleet, and the end users of an MSP's
clients, who file and follow their own tickets in the support portal.

## Product Purpose

Novieri RMM is a self-hosted IT management platform by Novieri: one control
plane, one lightweight agent on every computer and server, and optional site
servers for work that must happen on a client's own network. From one web
console a team inventories its fleet, patches it, deploys software, takes
remote sessions, backs it up, documents it and runs the governance workflows
an auditor asks for. Success is an operator who finds the right client,
machine or ticket at once, acts through a signed job, and can prove what
happened afterwards.

## Positioning

The organisation of the category's leader, with a security model it can show.
Every job is signed by the server and verified on the endpoint before it runs;
agents connect outbound only, over mutual TLS; credentials are decrypted only
at the point of use; every privileged action is written to a hash-chained
audit log; and an MSP's clients are kept apart by the database itself, down to
each technician's scope. The platform does not phone home.

## Operating Context

- One console over many client organisations (MSP). A client selector in the
  top bar narrows every request to one client; "All my clients" shows the
  person's whole scope.
- Spanish and English; the operator works in Spanish (Colombia).
- Dense desk use first; a 390px phone is a supported viewport.
- Deployed on the operator's own server (`rmm.novieri.com`); many deployments
  have no internet egress, so nothing is fetched from a CDN.

## Capabilities and Constraints

- Modules: devices and inventory, alerts and health, patching, software
  deployment, remote sessions, backup, imaging, IT documentation, changes,
  continuity tests, reports, licenses, subscriptions, IT budget, IP address
  management, tickets with a support portal, notifications, an AI assistant,
  and the administration around them (enrollment, agent packages, push
  install, credentials, audit and security logs, platform status, settings).
- Authorization is server-side; the console never gates security. A hidden
  link is a courtesy.
- Light theme only. Inter is self-hosted.
- Undecided: dark mode (out of scope), an in-app notification inbox (none
  exists; notifications go by email and webhook), server-saved views.

## Brand Commitments

- Name: **Novieri RMM** (operator, 2026-11-23). "Novieri" in the console's
  bar; "Page · Novieri RMM" in the browser tab; "Novieri RMM" in emails and on
  managed computers.
- Identity: direction A, "Cordillera", chosen by the operator on 2026-11-23
  from three proposals: a night-blue rail, one cobalt accent, an uppercase
  geometric wordmark and a zigzag N read as two peaks and a monitoring pulse.
  The assets and the exact values are in `docs/design/marca/`.
- Atera's organisation, never its look (operator, 2026-11-23): its menu, its
  per-client pages, its filtered lists, its tabbed records and its "New" menu;
  Novieri's colours and shapes.
- Voice: say what happened and what follows from it; name what failed and
  what was and was not changed; no reassurance in place of information.
- No user-visible string names the identity vendor.

## Evidence on Hand

None for marketing: no customer names, testimonials, case studies,
third-party benchmarks, press or published pricing (operator, 2026-08-21).
The only quotable figures are the measured ones in `docs/product.md` and the
test suite that produced them. Nothing may be invented.

## Product Principles

1. Copy the category's structure and compete on consistency: one way to save,
   every view addressable by URL, a unique tab title, an empty state that says
   its real cause.
2. Every control leads somewhere real. Nothing measured is drawn as absent,
   never as zero.
3. Security is enforced by the server and the database, and the console says
   so rather than pretending to guard it.
4. Dense for the people who live in it, legible on a phone during an incident.
5. Two languages, one product: every word exists in both.

## Accessibility & Inclusion

WCAG 2.2 Level AA is a product requirement (operator, 2026-08-21). It is
measured, not asserted: `frontend/src/test/contrast.test.ts` computes every
token pair, the harmony gate runs axe on every route in both languages at four
viewports, and `frontend/scripts/a11y-browser.mjs` measures composited
contrast on the component harness. Touch targets reach 44px on coarse
pointers; nothing renders below 10.5px.
