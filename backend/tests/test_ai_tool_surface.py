"""THE MEASUREMENT, PINNED (spec §4.2).

Three documents carried three different wrong numbers -- 2 of 147, 3 of 173,
2 of 147 again -- and the drift was found only because somebody re-measured by
hand. This test is what makes the next drift a red build.

IT IS DELIBERATELY BRITTLE. Any route added anywhere turns it red, and that is
the deliverable, not a defect: the numbers below are quoted in specs and in the
roadmap as a statement about this codebase, so a number nobody is forced to
re-measure is a number that rots. When it fails, re-run it, read which property
the new route failed, and update the record here in the same commit.

NO DATABASE. See `tool_surface.build_router_only_app`.
"""

from typing import Any

import pytest

from app.core.ai.scrub import Blocked, scrub
from app.core.ai.types import ReadTool
from tests.tool_surface import (
    api_v1_get_routes,
    build_router_only_app,
    every_field_name,
    inner_model,
    model_openness,
    route_property_failures,
)

# Measured 2026-08-15 against the live router: error-monitoring Task 5 added
# `GET /api/v1/platform/errors` and `GET /api/v1/platform/errors/{group_id}`
# -- gated, typed, bounded (neither response model is a bare list, so
# property 4 does not apply to either), self-describing, and CLOSED: every
# field is a scalar, a UUID, a datetime or a nested response model with a
# docstring, and none of `fingerprint`/`exc_type`/.../`device_id` matches
# `looks_like_secret_key`. Both pass all six properties.
# Re-measured 2026-08-16: the org-settings cycle added `GET /api/v1/orgs/current`
# (192nd GET route). It fails exactly two properties and both are deliberate:
#   * P2 gated -- it is authenticated-only BY DESIGN, like `GET /me`. It returns
#     the caller's OWN org and the console chrome needs it on every screen, so a
#     permission code here would be one every role must hold. Recorded in
#     `EXPECTED_UNGATED_PATHS` below and, with the full reasoning, in
#     `tests/test_route_gating.py::UNGATED_BY_DESIGN`. Its sibling
#     `PATCH /orgs/current` IS gated (`orgs.manage`) and is not a GET, so it is
#     outside this sweep entirely.
#   * P6 closed -- `OrgOut.settings` is `dict[str, Any]`, which `model_openness`
#     reads as open, and it genuinely is: the column is JSONB. What bounds it is
#     the WRITE side (a closed allowlist of typed keys plus a byte cap, in
#     `app/api/v1/orgs.py`), which is not a property this static sweep can see.
#     P6 is not one of the counted properties below, so nothing moves for it.
# It passes P3 (typed: `OrgOut`), P4 (not a bare list) and P5 (the handler has a
# docstring and `OrgOut` has one) -- hence P5's count is unchanged at 166.
# Re-measured 2026-08-18: agent distribution added `GET /api/v1/agent/releases`
# (193rd GET route). It fails exactly ONE property, and the failure is a
# decision rather than an oversight:
#   * P4 bounded -- the response model is `list[ReleaseOut]` with no `limit`
#     query parameter, so this sweep reads it as unbounded, and structurally it
#     is. Pagination was not added because the catalogue is what an OPERATOR
#     publishes by hand, one artefact at a time, through a route gated on
#     `agent.release.manage`; the console renders every row and an operator
#     comparing versions wants them all. If a deployment ever accumulates
#     enough releases for this to matter, the fix is `limit`/`offset` here and
#     a bump of the number below -- not a silent widening of what "bounded"
#     means.
# It passes P2 (`agent.release.read`), P3 (`ReleaseOut`) and P5 -- the handler
# and the model both gained docstrings in the same commit as this note, which
# is why P5's count is unchanged at 166 rather than 167. P6 is not counted.
# Re-measured 2026-08-18: asset-inventory part 2 added
# `GET /api/v1/devices/candidates` (194th GET route), and it passes ALL SIX
# properties -- so it joins `EXPECTED_PASSING_SIX` below, which is the set this
# whole file exists to keep honest.
#
# THAT IS ELIGIBILITY, NOT REGISTRATION, and the difference matters. Passing six
# means the route COULD be exposed as an AI read tool: gated (`devices.read`),
# typed (`CandidateListResponse`), bounded (a `limit` query parameter and a
# wrapper carrying the real total), self-describing (handler and models both
# documented) and closed (every field a scalar, a datetime or a UUID, none
# matching `looks_like_secret_key`). It is NOT registered in `ReadTool`, and
# registering it is an ai-admin decision rather than a side effect of this
# cycle -- what an AI agent may read is a question about that module's
# autonomy dial, not about whether a route happens to be well-formed.
#
# The response is a WRAPPER rather than a bare list on purpose, which is also
# why P4 did not move: a `/16` sweep observes tens of thousands of addresses,
# and the count that says how many were cut cannot ride on a list.
# Re-measured 2026-08-18 again: asset-inventory part 3 added
# `GET /api/v1/devices/suggestions` (195th GET route), which also passes all
# six -- for the same reason as `/candidates`, since it was written to the same
# shape: gated `devices.read`, typed, wrapped rather than a bare list,
# documented on the handler and on every model, and closed (ids, strings,
# datetimes and booleans, nothing matching `looks_like_secret_key`).
#
# Also PENDING_TOOL_REVIEW rather than registered. Whether an AI persona should
# read the queue of records the platform is UNSURE about is a judgement call
# about that persona's autonomy, not a consequence of the route being
# well-formed -- and the registry's own header is explicit that becoming a tool
# is a deliberate, reviewed act.
# Re-measured 2026-08-19 with the access-control cycle's routes ALL present —
# once, at the end, not per workstream. Four workstreams landed routes in this
# cycle; each re-measuring against a tree holding only its own would have
# written an arithmetically false number, and the last to merge would have left
# this file green and wrong, which is the one outcome it exists to prevent.
# 195 -> 199 GET routes, 15 -> 19 passing five.
# Re-measured 2026-08-20 with the invisible-identity-layer cycle's routes ALL
# present. 199 -> 202 GET routes, 19 -> 21 passing five. Eight workstreams
# landed routes; measuring per-workstream would have written an arithmetically
# false number that the last merge made green.
# Re-measured 2026-08-21: the UX-closeout backlog's recommendation 4 added
# `GET /api/v1/patch/approvals` (203rd GET route), the list that makes
# `DELETE /patch/approvals/{approval_id}` reachable at all — before it, an
# approval id was returned only by the POST that created it, so the gate
# deciding which patches may reach a machine was one-way. It passes all six:
# gated (`patch.read`), typed (`ApprovalListResponse`), bounded (`limit`, and
# a wrapper carrying the real `total` rather than a bare list),
# self-describing (the handler and both models carry docstrings, written in
# this commit — which is why P5 stays at 166 rather than becoming 167) and
# closed (UUIDs, a string, a bool and datetimes; no dict, nothing matching
# `looks_like_secret_key`). So it joins `EXPECTED_PASSING_SIX` and
# `PENDING_TOOL_REVIEW`, not the registry. 202 -> 203 GET routes, 21 -> 22
# passing five.
# Re-measured 2026-08-21 again: the same backlog's recommendation 3 added
# `GET /api/v1/device-groups` (204th GET route) -- the list that makes a device
# group ADDRESSABLE at all. A group id is what a deployment's `target_id`, a
# patch/backup/remote policy's `device_group_id`, a health profile assignment
# and a `user_roles` grant scope all take, and no route had ever returned one,
# so the console could express exactly one scope: the org default, which is the
# row that has no id. Measured once, here, against the tree holding it -- not
# arithmetic on the number three paragraphs up.
#
# It passes all six: gated (`devices.read`, the code the reporting registry
# already maps `device_groups` to), typed (`DeviceGroupListResponse`), bounded
# (`limit`, and a wrapper carrying `total` and `ungrouped_device_count` rather
# than a bare list), self-describing (the handler and both models carry
# docstrings, written in this commit -- which is again why P5 stays at 166
# rather than becoming 167) and closed: UUIDs, strings, a bool and ints, and
# `rules` is DELIBERATELY not returned because a `dict[str, Any]` field would
# be an open key space that fails P6 forever. So it joins
# `EXPECTED_PASSING_SIX` and `PENDING_TOOL_REVIEW`. 203 -> 204 GET routes,
# 22 -> 23 passing five.
# Re-measured 2026-08-22, ONCE, with recommendations 2, 6, 7 and 10 of the
# UX-closeout backlog ALL present. Four workstreams' worth of change landed in
# one cycle here deliberately: this file goes red on purpose when the surface
# moves, and four agents each bumping it against a tree holding only their own
# work would write arithmetic that is false and green, which is the single
# outcome it exists to prevent. Measured against the tree, not derived from
# the entry above.
#
# NO ROUTE WAS ADDED. 204 -> 204. None of the four recommendations was a new
# endpoint: signature fields on `JobOut` (rec. 2), actor names on
# `AuditEventOut` (rec. 6), two filters and an audit event on the existing
# evidence CSV (rec. 7), and a paged envelope on the existing ticket list plus
# `order` on the existing `GET /jobs` (rec. 10).
#
# THE BILL WENT DOWN BY FOUR AND NOTHING WAS RECLASSIFIED. P5 166 -> 162, and
# the four routes that left it are the four whose response models this cycle
# changed: `/api/v1/jobs`, `/api/v1/jobs/{job_id}`, `/api/v1/audit` and
# `/api/v1/service-desk/tickets`. Each was failing P5 TWICE -- an undocumented
# handler and an undocumented response model -- and each was being edited
# anyway, so both halves were written rather than left as debt beside new
# prose. P2, P3 and P4 are untouched; `EXPECTED_UNGATED_PATHS` is unchanged.
#
# ONE HONEST FOOTNOTE ON HOW THAT LAST ONE PASSED. The sweep inspects only the
# OUTERMOST response model, so wrapping `list[TicketOut]` in an envelope moved
# `TicketOut` -- whose fields carry no descriptions -- out of its view. A class
# that stopped being inspected has not become self-describing, so `TicketOut`
# was given a docstring in the same commit; the pass would hold if the sweep
# ever learned to recurse.
#
# `/api/v1/service-desk/tickets` NOW PASSES ALL SIX, and it is the only one of
# the four that can: `jobs` carries `params: dict[str, Any]` and `audit`
# carries `metadata: dict[str, Any]`, both genuinely open key spaces, so those
# three routes reach `passing five` and stop there. 23 -> 27 passing five,
# 15 -> 16 passing six.
#
# IT JOINS PENDING_TOOL_REVIEW, NOT THE REGISTRY, and here the blast radius is
# not a subtlety. `TicketOut` carries `subject` AND `description` -- the free
# text a requester typed -- so the route is every incident and every service
# request in the tenant, in full, one page at a time: the credential somebody
# pasted into a password-reset ticket, the detail of an ongoing security
# incident, who reported what about whom. `ticket.read` is the right gate for
# a technician, who is the person that text was written for. Whether an
# automated actor should hold the org's whole ticket corpus is a question
# about that persona's blast radius rather than about whether the route is
# well-formed, and it is the question PENDING_TOOL_REVIEW exists to hold open.
# Re-measured 2026-08-22 (job-lifecycle cycle 1) AGAINST THE TREE, not by
# arithmetic, because a route WAS added and the honest answer is that this
# sweep cannot see it. `POST /api/v1/jobs/{job_id}/cancel` is a POST, and
# `api_v1_get_routes` selects `"GET" in route.methods` -- so the new route is
# outside this measurement entirely. Recorded rather than left silent: "a route
# landed and the census did not move" reads like a broken census unless the
# reason is written down.
#
# `JobOut` GAINED TWO FIELDS AND NEITHER MOVED ANYTHING, which is worth one
# line because it is the half that COULD have moved. `cancel_requested_at`
# (datetime) and `cancel_outcome` (str) are scalars, so `model_openness` is
# unchanged -- both jobs routes still fail P6 alone, on `params: dict[str,
# Any]`, exactly as before. P5 does not move either: the sweep only counts
# undescribed fields when the model has NO docstring, and `JobOut` has carried
# one since closeout rec. 2.
#
# 204 -> 204 GET routes, 27 -> 27 passing five, 16 -> 16 passing six, and the
# failure bill is untouched at P2 5 / P3 24 / P4 79 / P5 162.
#
# Re-measured 2026-08-22 (device-group management, cycle 1) AGAINST THE TREE.
# `GET /api/v1/device-groups/{group_id}` is a new GET route, so the census
# moved by one: 204 -> 205 GET routes and 27 -> 28 passing five. The other four
# routes in that cycle are POST/PATCH/DELETE and `api_v1_get_routes` selects on
# GET, so they are outside this measurement entirely.
#
# THE DETAIL ROUTE PASSES FIVE AND FAILS P6, AND IT IS NOT IN
# `EXPECTED_PASSING_SIX` FOR THAT REASON RATHER THAN AS A JUDGEMENT ABOUT AN AI
# PERSONA'S REACH. `DeviceGroupDetailOut.references.credentials` counts the
# credential rows still scoped to the group -- an integer, never a secret --
# but `looks_like_secret_key` is word-level and `credential` is a whole word in
# `_SECRET_WORDS`, deliberately, because §0.5 excludes even credential metadata
# from the agent. So `scrub` would block this response at runtime on the KEY
# NAME whatever the value is, and a route whose payload can never survive the
# gate has no legal tool declaration. Renaming the field to dodge the predicate
# was the alternative and it is worse: the count is what an operator has to act
# on to archive the group, and a field called anything else says less on the
# screen than it costs here.
#
# `EXPECTED_FAILING_BY_PROPERTY` is unchanged because P6 is not one of the four
# properties it counts, and `PENDING_TOOL_REVIEW` gains nothing because that set
# holds routes that COULD be registered and are not -- this one could not.
# `DeviceGroupOut` gained `precedence` (int) and `archived_at` (datetime|None);
# both are scalars, both carry `Field(description=...)`, so the list route is
# unmoved on every property including P6.
#
# 204 -> 205 GET routes, 27 -> 28 passing five, 16 -> 16 passing six, and the
# failure bill is untouched at P2 5 / P3 24 / P4 79 / P5 162.
#
# Re-measured 2026-08-24 (revoked tokens hidden by default): NO NEW ROUTE.
# `GET /api/v1/enrollment-tokens` changed shape -- it stopped returning revoked
# tokens by default and, because a bare list cannot carry the count of what it
# withheld, its response became `TokenListResponse`. The route count is
# therefore unmoved at 206 and two entries in the failure bill are:
#
#   * P4 bounded 79 -> 78, AND IT WAS NOT A REAL BOUND WHEN THIS WAS WRITTEN.
#     The predicate reads `get_origin(response_model) is list`, so wrapping the
#     payload satisfied it while the route still had no `limit` and still
#     returned every matching token. Recorded here rather than quietly banked,
#     in the spirit of the `/agent/releases` note above: the fix was named as
#     `limit`/`offset` on the route, not a wider reading of "bounded".
#
#     SETTLED 2026-08-26 by 2643859 ("the token list was unbounded, on the one
#     screen that mints them"): `GET /api/v1/enrollment-tokens` now takes
#     `limit` (capped at `svc.TOKEN_PAGE_MAX`) and `offset`, and `total`
#     deliberately ignores both so the screen can say what it withheld. The
#     census did not move for it and could not -- P4's predicate was already
#     satisfied by the wrapper, which is the whole reason the caveat above had
#     to be written in prose. This paragraph is corrected rather than deleted:
#     the debt was real, the discharge is the interesting half, and a record
#     that quietly loses its own caveats is one nobody can audit.
#   * P5 self-describing 162 -> 161. The handler, `TokenListItem` and
#     `TokenListResponse` all gained docstrings in the same commit.
#
# Which puts it on all six and into `EXPECTED_PASSING_SIX` and
# `PENDING_TOOL_REVIEW` below: gated (`enrollment.manage`), typed, wrapped,
# documented, and closed -- UUID, strings, ints, datetimes and a bool, with the
# plaintext credential served only by `POST` and never stored. 29 -> 30 passing
# five, and passing six gains one entry.
#
# MEASURED AGAINST THE TREE TWICE, and the second time is the one that counts.
# The first ran in a git worktree holding the then-HEAD plus this change alone,
# because the patch cycle below had an uncommitted route in the same working
# copy and measuring over it would have banked somebody else's census move
# inside this commit. That cycle has since landed, so the numbers above are the
# straight measurement over the tree with both changes present -- which is why
# they read 206/30 rather than the 205/29 the worktree gave.
#
# Re-measured 2026-08-24 (patch bulk approval) AGAINST THE TREE, in a git
# worktree holding HEAD plus this change alone. That is not fussiness: another
# cycle has an uncommitted route in this working copy, and measuring over it
# would have banked somebody else's census move inside this commit.
#
# `GET /api/v1/patch/rings` is the new GET route -- 205 -> 206 GET routes and
# 28 -> 29 passing five, measured that way. `POST /api/v1/patch/approvals/bulk`
# landed in the same change and is outside this measurement entirely, because
# `api_v1_get_routes` selects on GET. Recorded rather than left silent, since
# "two routes landed and the census moved by one" reads like a broken census
# until the reason is written down.
#
# IT PASSES ALL SIX AND JOINS `PENDING_TOOL_REVIEW`, NOT THE REGISTRY, and the
# blast radius here has the same shape as `/device-groups`' rather than the
# standing "well-formed is not registered" note. The route answers, for every
# deployment ring this tenant has, exactly how many machines that ring reaches
# -- which IS the reach of an approval, because clearing a patch for a ring
# clears it for every device in that ring now and at every future scan. Sorted
# by rollout order it is a menu of which single ring name moves the most
# endpoints, and it sits one POST away from `/patch/approvals/bulk`, which takes
# one ring name and up to five hundred patch ids. A HUMAN reading it holds
# `patch.read` and still cannot approve anything without `patch.approve`;
# whether an automated actor should be handed the sizing for that decision is a
# question about that persona's autonomy dial, and it is the question
# `PENDING_TOOL_REVIEW` exists to hold open rather than answer by default.
#
# The failure bill is UNTOUCHED. The handler, `RingOut` and `RingListResponse`
# all carry docstrings, so P5 did not grow; the response is a WRAPPER rather
# than a bare `list[RingOut]`, so P4 does not apply; and every field is a `str`
# or an `int`, so P6 is closed.
#
# Re-measured 2026-08-25 (the assistant ships enabled; spec §0.14) AGAINST THE
# TREE, and the measurement carries TWO CYCLES' MOVES because five of the
# routes below had already landed with this record left unwritten. Both are set
# out separately, because "the census moved by five and one of them is mine"
# is unreadable as a single number.
#
# **MINE. NO ROUTE WAS ADDED OR REMOVED** -- the AI surface is still eleven
# routes. What moved is one route's failure bill:
#
#   * `GET /api/v1/ai/settings` now passes P5. `SettingsOut` had no docstring;
#     it gained one when `enabled` and `provider_key` were removed from it and
#     the model had to say what it is now FOR ("the org's dials; whether the
#     assistant runs is not one of them"). That is passing five +1 and P5
#     self-describing -1.
#   * It still fails P6, so it does NOT join `EXPECTED_PASSING_SIX`:
#     `daily_token_budget` is `int | None`, which `model_openness` reads as
#     open. Unchanged by this cycle and not banked as progress.
#   * `GET /api/v1/ai/providers` is unchanged in every respect. It still returns
#     a bare `list[ProviderOut]` (P4) and still reads open (P6); what changed is
#     that the list now holds at most one entry and nothing writes it back. A
#     census property is about the shape of the response, and the shape did not
#     move.
#
# **ALREADY IN THE TREE, FROM THE INVENTORY-EXPORT, HEALTH-BADGE AND PATCH
# CYCLES**, all committed with this file untouched -- so the record was red on
# this tree before this change and is measured here rather than guessed. Five
# new GET routes, 206 -> 211, each read off the router:
#
#   * `GET /api/v1/export/devices.xlsx` and `GET /api/v1/export/software.xlsx`
#     -- P3 typed, P5 self-describing and P6 closed, all three because the
#     response is a binary `.xlsx` `StreamingResponse` with no response model
#     at all. A workbook has no Pydantic shape to describe, so these three are
#     structural for this route kind rather than an oversight; P3 typed
#     24 -> 26 and P5 +2 are entirely these two.
#   * `GET /api/v1/health/alerts/summary`, `GET /api/v1/patch/device-rollup`
#     and `GET /api/v1/patch/updates` -- gated, typed, bounded and documented,
#     failing only P6, which is not one of the counted properties. Three more
#     into passing five.
#
# **THE ARITHMETIC, STATED SO THE NEXT READER CAN CHECK IT AGAINST THE TREE
# RATHER THAN AGAINST THIS COMMENT:** 206 + 5 = 211 GET routes; passing five
# 30 + 3 (theirs) + 1 (mine) = 34; P3 typed 24 + 2 = 26; P5 self-describing
# 161 + 2 - 1 = 162. `EXPECTED_PASSING_SIX` is UNCHANGED -- every one of the
# five new routes fails P6, so none of them is registrable.
# ADDED 2026-08-26 (stored documents, `docs/modules/documentation/
# spec-attachments.md` cycle A). FOUR new GET routes, 211 -> 215, each read off
# the router rather than guessed:
#
#   * `GET /api/v1/docs/documents`,
#     `GET /api/v1/docs/documents/{document_id}` and
#     `GET /api/v1/docs/documents/{document_id}/versions` -- gated
#     (`docs.read`), typed, bounded and documented, and they pass P6 as well.
#     Three into passing five AND into `EXPECTED_PASSING_SIX`.
#   * `GET /api/v1/docs/documents/{document_id}/versions/{version}/content`
#     -- P3 typed and P6 closed, both because the response is raw bytes with
#     no response model at all, exactly as the two `.xlsx` export routes above
#     are. A signed contract PDF has no Pydantic shape to describe. This route
#     is ALSO the one route in the module that must never become a tool under
#     any review: it is the bytes themselves. P3 typed 26 -> 27; P4 and P5 are
#     unchanged, because the three list routes were written to pass them
#     rather than pinned as failing them.
#
# THE THREE REGISTRABLE ONES JOIN `PENDING_TOOL_REVIEW`, NOT THE REGISTRY, and
# the blast-radius question here is specific rather than the standing one. A
# document library's metadata is tenant CONTENT: the titles, descriptions,
# categories and filenames an IT team gave to its vendor contracts, its site
# diagrams and its firewall exports. "What is this organisation's library
# called" is a decent reconnaissance answer on its own -- it names the vendors,
# the sites and the systems -- and whether an AI persona should hold it is a
# question about that persona's autonomy dial, not about whether these routes
# are well formed. What bounds the worst case either way is that the bytes are
# unreachable: the content route above fails P3 and P6 structurally, so no
# review could register it without changing what it returns.
#
# **THE ARITHMETIC:** 211 + 4 = 215 GET routes; passing five 34 + 3 = 37; P3
# typed 26 + 1 = 27; P4 bounded and P5 self-describing unchanged at 78 and 162.
# ADDED 2026-08-26 (agent push install, `docs/modules/agent-distribution/
# spec-push-install.md` cycle B1, under `CLAUDE.md` invariant 2a). MEASURED
# AGAINST THE TREE, NOT DERIVED BY ARITHMETIC FROM THE ENTRY ABOVE -- and the
# tree held somebody else's committed work as well as mine, which is exactly
# the situation this file's own header warns produces "arithmetic that is false
# and green".
#
# **THE TREE MOVED BY THREE ROUTES AND ONLY TWO ARE MINE.** Re-measuring with
# the push routes excluded gives 216 GET routes and P5 163, against a pinned
# 215/162 -- so commit 39afb7f ("a sweep records what it finds") landed a GET
# route and a P5 failure without re-measuring, and HEAD was already red on this
# test before this cycle touched it. Recorded rather than absorbed silently:
# pinning 218 without saying where the third route came from would make the
# next reader believe this cycle added it.
#
# MY TWO ROUTES, and what each fails:
#
#   * `GET /api/v1/agent-push/authorizations` -- gated
#     (`agent.push.scope.manage`), typed (`AuthorizationOut`),
#     self-describing (handler and model both documented, every field
#     described) and CLOSED. It fails P4 alone: the response is
#     `list[AuthorizationOut]` with no `limit`, and structurally it is
#     unbounded. Pagination was not added for the reason
#     `GET /api/v1/agent/releases` records one entry up: this list is what an
#     operator authorizes BY HAND, one prefix at a time, through an admin-tier
#     audited route, and somebody reviewing where this platform may present
#     administrative credentials wants all of it on one screen. If a
#     deployment ever accumulates enough grants for that to matter the fix is
#     `limit`/`offset` here and a bump of the number below, not a quiet
#     widening of what "bounded" means.
#   * `GET /api/v1/agent-push/credential-scopes` -- fails P4 for the same
#     reason AND P6, and the P6 failure is structural and permanent.
#     `CredentialScopeOut.credential_id` / `.credential_name` carry no secret
#     and never can -- the models have no field that could hold one -- but
#     `looks_like_secret_key` is word-level and `credential` is a whole word in
#     `_SECRET_WORDS`, deliberately, because §0.5 excludes even credential
#     metadata from the agent. So `scrub` would block this response at runtime
#     on the KEY NAME whatever the value is, and a route whose payload can
#     never survive the gate has no legal tool declaration. This is exactly
#     `GET /api/v1/device-groups/{group_id}`'s situation, one entry up, and the
#     same conclusion: renaming the field to dodge the predicate is worse than
#     the failure, because the credential's name is the thing an operator has
#     to read to know which secret may be presented where.
#
# **NEITHER JOINS `EXPECTED_PASSING_SIX` AND NEITHER MAY EVER BE REGISTERED AS
# AN AI READ TOOL**, and here that is not the standing well-formedness
# question. Between them these two routes are the complete map of where this
# platform may present an administrative credential to machines nobody has
# enrolled: the authorized prefixes, and which stored secret is bound to which
# of them. `CLAUDE.md` invariant 2a clause (e) says this path is "never
# reachable by any automated, scheduled or AI-driven actor"; handing an AI
# persona the reconnaissance for it would be the same decision one step
# removed. Both fail P4 anyway, so neither is eligible today -- but the
# ineligibility is not what settles it.
#
# **THE ARITHMETIC:** 215 + 2 (mine) + 1 (39afb7f's, unmeasured) = 218 GET
# routes; P4 bounded 78 + 2 = 80; P5 self-describing 162 + 1 (39afb7f's) = 163;
# P3 typed unchanged at 27; `EXPECTED_PASSING_FIVE` unchanged at 37, because
# both of my routes fail P4 and 39afb7f's fails P5.
# RE-MEASURED 2026-08-26 (defect handover) AGAINST THE TREE, AND NOTHING MOVED.
# This one is recorded precisely because the expected answer was "red". Five
# cycles landed routes in this tree the same day -- documentation, jobs, device
# groups, enrollment and patch -- and the handover said to re-measure and bump.
# The measurement says 218 GET routes, 37 passing five, and every entry of the
# failure bill unchanged, so nothing is bumped: each of those cycles either
# added no GET route (jobs' cancel and patch's bulk approval are POSTs, which
# `api_v1_get_routes` excludes) or re-measured this record in its own commit,
# which is the discipline working rather than the census being stale.
#
# A number that survives a re-measurement is worth exactly as much as the
# re-measurement, and a green run nobody records reads later like a run nobody
# did. Bumping to make it green is the failure mode this file names in its own
# header; so is leaving the reader unable to tell "checked, unchanged" from
# "never checked".
# Re-measured 2026-09-01 (patch apply outcomes + the customizable dashboard).
# Two GET routes land: `/api/v1/patch/runs` and `/api/v1/dashboard/layout`.
#
# `/patch/runs` PASSES ALL SIX and joins `EXPECTED_PASSING_SIX` and
# `PENDING_TOOL_REVIEW`, not the registry: gated (`patch.read`), typed
# (`PatchRunListResponse`), not a bare list, self-describing (the handler and
# both models carry docstrings, written in this commit -- which is why P5
# stays at 163 rather than becoming 164) and closed (UUIDs, ints, a bool,
# datetimes, and a `failures` list of three nullable strings; no dict reaches
# the wire -- `PatchRun.results` is the agent's raw document and is
# deliberately NOT returned). Whether an AI persona should read which updates
# failed on which machines is a judgement about that persona, not a
# consequence of the route being well-formed.
#
# `/dashboard/layout` is UNGATED BY DESIGN and joins `EXPECTED_UNGATED_PATHS`,
# so P2 gated goes 5 -> 6. The reasoning is `app/api/v1/dashboard.py`'s and
# `tests/test_route_gating.py`'s: a code here would be a code every role must
# hold, and the target is `ctx.user_id` from the validated token with no
# `user_id` parameter on any of the three verbs.
#
# 218 -> 220 GET routes, 37 -> 38 passing five.
#
# 220 -> 221, AND THE DEVICE-GROUPS REGRESSION RESOLVED (2026-09-08). The
# previous revision of this note left three measurements deliberately
# unaccounted -- one extra GET route, `passing six -- lost
# ['/api/v1/device-groups']`, and `P4 bounded 80 -> 81` -- because they were
# drift already on `main` and absorbing them silently is the one thing this
# file exists to prevent. They are reviewed here, and all three turn out to be
# the same two cycles.
#
# THE 221st GET ROUTE is `GET /api/v1/agent-push/host-keys` (invariant 2a's
# module). It is one of three routes that module mounts, and the only one that
# was not already counted; like its two siblings it answers a bare
# `list[...]`, so it FAILS P4 bounded -- which is the whole of `P4 bounded
# 80 -> 81`. A bare list is a real property failure and is left as one rather
# than wrapped in a response model to move a number: an AI tool over a fleet's
# recorded SSH host keys is not a thing anyone has asked for, and P4 is what
# stops it being registerable by accident.
#
# `/api/v1/device-groups` LOST P6 CLOSED, and the cause is two field names.
# The device-group cycle added an `evaluation` block to the list response, and
# `looks_like_secret_key` reads `full_pass` and `last_full_pass_at` as
# secret-shaped on the word `pass`. That over-block is deliberate and is
# documented as such in `scrub.py` and in `tool_surface.model_openness`: a
# field with such a name blocks its tool's ENTIRE result at runtime, so a
# route carrying one genuinely cannot be a tool, whatever the field means.
# Here it means a sweep that read every device rather than the changed ones.
#
# SO IT IS RECORDED AS LOST RATHER THAN RENAMED AWAY, and the reason is cost,
# not preference: `last_full_pass_at` is not only a response field, it is
# `device_group_evaluation_state`'s own column, read and written by
# `services/device_groups.py`'s watermark logic. Renaming it to `..._sweep_at`
# -- which is what the rest of this codebase calls that operation, and would
# restore the property -- reaches a migration, the service, the schema, the
# console's `api.ts` and its fixtures. That is its own cycle with its own
# review, not a line in a bookkeeping commit. Until someone wants an AI tool
# over the group list, the platform loses nothing by the route being
# ineligible: it was never registered, only eligible.
#
# 221 -> 224 (2026-09-23): the backup off-site copy adds
# `GET /backup/offsite-targets`, `GET /backup/recovery-keys` and
# `GET /backup/points/{point_id}/copies`. All three answer a bare list (P4) and
# carry no field descriptions (P5); none is registered as a tool.
# 224 -> 228 (2026-09-23): agent self-update adds four GET routes
# (`/agent/fleet-versions`, `/agent/update-rollouts`, `.../{rollout_id}`,
# `.../{rollout_id}/targets`). None passes P5 (no route description), and the
# two list routes answer bare lists, failing P4. None is registered as a tool:
# `agent.update` is refused to the AI layer by name (spec §8.5), and nothing
# here needs reading through it.
# 228 -> 233 (2026-09-23): people adds five GET routes. All five pass P1-P5
# (the list takes `limit`); four also pass P6. The Person page fails P6
# because it nests the checklist. None is registered: who works here and what
# they hold is personal data, and whether an AI persona should read it is a
# decision for its own review, so the four wait in PENDING_TOOL_REVIEW.
# 233 -> 234 (2026-09-23): Microsoft 365 sign-in for email intake adds
# `GET /email-intake/oauth-app`. Gated (`email_intake.read`), typed, not a
# list and documented, so passing five 43 -> 44; `has_secret` reads as a
# secret-shaped name, so it fails P6 and is not registrable. Settings → Email's
# new response fields are named `microsoft_*` rather than `token_*` so that
# route keeps all six.
# 234 -> 236 (2026-09-23): patch adds `GET /patch/restarts` and
# `GET /patch/app-sources`. Their wrappers carry no description, so both fail
# P5; neither is registered as a tool.
# 236 -> 238 (2026-09-23): scheduled scripts add `GET /deploy/script-policies`
# (a bare list: P4) and `.../{policy_id}/runs`, which passes all six and waits
# in PENDING_TOOL_REVIEW.
# 238 -> 239 (2026-09-23): change gating adds `GET /changes/gates`, a bare
# list (P4); not registered as a tool.
# 239 -> 241 (2026-09-23): the ticket workbench adds
# `GET /service-desk/canned-responses` (saved replies, bounded by `limit`) and
# `GET /docs/suggest` (articles matching a ticket subject, bounded by
# `limit`). Both pass all six and wait in PENDING_TOOL_REVIEW: saved replies
# are text the org sends to customers, and whether an AI persona should read
# the runbook store through a second door is the docs module's own question.
# 241 -> 243 (2026-09-23): portal help adds `GET /portal/help/suggest` and
# `GET /portal/help/articles/{article_id}`. Like the other portal routes they
# carry no route description (P5); the end-user persona reaches them through
# its own registry (`app/modules/ai_portal/tools.py`), not the staff one.
# 243 -> 244 (2026-09-28): the install link adds GET /install-link; it
# passes all six and waits in PENDING_TOOL_REVIEW.
# 244 -> 249 (2026-09-30): subnet push runs (design
# `docs/superpowers/specs/2026-09-30-gpo-msi-and-subnet-push-design.md` §1.7)
# add five GET routes under `/agent-push/runs`, all gated `agent.push.run`
# and typed. Measured against the tree, per route:
#   * `/runs/prefixes` fails P4 alone (a bare list of the org's active
#     authorizations, bounded by what was authorized, not by a `limit`);
#   * `/runs/preview` fails P6 alone (its `reason_help` is an open map), so it
#     is the one that moves "passing five" 48 -> 49;
#   * `/runs` fails P4, P5 and P6; `/runs/{run_id}` and
#     `/runs/{run_id}/targets` fail P5 and P6.
# None passes all six, so neither EXPECTED_PASSING_SIX nor PENDING_TOOL_REVIEW
# changes -- and none ever should: CLAUDE.md 2a(e) puts every push route out
# of reach of an AI-driven actor, which `tests/test_push_run_api.py::
# test_no_ai_tool_reaches_a_push_route` pins against the registry.
# 249 -> 250 (2026-10-14): the MCP server's console admin API adds
# `GET /mcp/connections` (gated `mcp.use`, typed, a wrapper bounded to 200
# apps, documented, closed). It passes all six and waits in
# PENDING_TOOL_REVIEW: an AI persona reading which AI apps act for the
# organization, and who revoked them, is the organization's own decision about
# AI access -- the MCP server already keeps both routes off its own tools
# (`app/mcp/exclusions.py::NOT_FOR_MCP`).
# 250 -> 254 (2026-10-24): the MSP clients API (spec §3.2, Task 9) adds
# `GET /clients`, `GET /clients/{client_id}`, `GET /clients/{client_id}/sites`
# and `GET /clients/{client_id}/contacts`, all gated (`clients.read`), typed,
# described and closed. The three lists answer bare lists of a handful of rows
# (spec P6: up to 10 clients in the first year), so they fail P4 alone: 90 ->
# 93. `GET /clients/{client_id}` passes all six -- passing five 50 -> 51 -- and
# waits in PENDING_TOOL_REVIEW: which clients exist and the email domains that
# route their mail are the map of who Novieri serves, and whether an AI persona
# should read it is the AI module's decision, not a byproduct of this one.
# 254 -> 253 (2026-10-28): IPAM's own sites are merged into the clients' (MSP
# clients phase 2, Task 10; migration 0240 drops `ipam_site`), so
# `GET /ipam/sites` is gone. It answered a bare, undescribed list, so P4 93 ->
# 92 and P5 177 -> 176; it passed neither five nor six, so neither set moves.
# Its replacement for the console is `GET /clients/{client_id}/sites`, above.
EXPECTED_GET_ROUTES = 253
EXPECTED_PASSING_FIVE = 51

# The registrable set, pinned by PATH rather than count: this is the number the
# whole exercise is about, it is small, and "which routes" is the actionable
# half of "how many".
EXPECTED_PASSING_SIX = frozenset(
    {
        # Added 2026-10-24 (MSP clients API). Eligible, not registered -- see
        # the note above EXPECTED_GET_ROUTES.
        "/api/v1/clients/{client_id}",
        # Added 2026-10-14 (MCP console admin API). Eligible, not registered --
        # see the note above EXPECTED_GET_ROUTES.
        "/api/v1/mcp/connections",
        # Added 2026-09-28 (install link). Eligible on all six, and deliberately
        # not registered: it names the networks from which a machine may enrol
        # itself into this organisation -- the one fact an actor wanting to
        # plant a machine would ask for first. Per site since 2026-10-23 (MSP
        # clients spec §1.5): one link per site, the same fact, a site at a time.
        "/api/v1/sites/{site_id}/install-link",
        # Added 2026-09-23 (scheduled scripts). Eligible, not registered: a
        # run's output is whatever the script printed and may carry secrets.
        "/api/v1/deploy/script-policies/{policy_id}/runs",
        "/api/v1/health/alerts",
        "/api/v1/devices/{device_id}/health",
        "/api/v1/ai/status",
        "/api/v1/platform/errors",
        "/api/v1/platform/errors/{group_id}",
        # Added 2026-08-18. Eligible, not registered -- see the note above.
        "/api/v1/devices/candidates",
        "/api/v1/devices/suggestions",
        # Added 2026-08-19 (access control). Eligible on all six properties and
        # deliberately NOT registered as AI tools — see the note below, which is
        # a judgement about these four specifically rather than the standing one
        # about well-formedness.
        "/api/v1/permissions",
        "/api/v1/roles",
        "/api/v1/users",
        "/api/v1/users/{user_id}/effective-permissions",
        # Added 2026-08-20 (invisible identity layer). Eligible on all six and
        # deliberately unregistered.
        #
        # `/settings/email` returns the realm's mail configuration — host, port,
        # sender, and whether a password is set. Never the password itself
        # (`test_realm_settings_api.py` asserts the literal is absent from the
        # body), but an AI persona reading a deployment's mail relay and sender
        # identity is reconnaissance for phishing that arrives from the right
        # address.
        #
        # `/orgs/current/console-domain-preview` returns EVERY user's email and
        # display name in the tenant — it exists so the allowlist form can show
        # who a value would lock out. That is the org's whole directory behind
        # one GET.
        "/api/v1/orgs/current/console-domain-preview",
        "/api/v1/settings/email",
        # Added 2026-08-21 (patch approvals list). Eligible, and deliberately
        # not registered: the list names every patch cleared to reach a
        # machine, and hands out the ids `DELETE /approvals/{id}` takes.
        # Whether an AI persona should read the fleet's patch-approval gate is
        # a question about that persona's blast radius, not about whether the
        # route happens to be well-formed.
        "/api/v1/patch/approvals",
        # Added 2026-09-01 (patch apply runs). Eligible, and deliberately not
        # registered: it names which updates failed on which machine and why,
        # in the agent's own error strings. Reasoning above.
        "/api/v1/patch/runs",
        # Added 2026-08-21 (device-group list). Eligible, and deliberately not
        # registered -- this is the one where the blast radius is the WHOLE
        # point of the route. It hands back the exact `target_id` values that
        # `deploy` and the script runner accept for `target_type="group"`,
        # each next to a `device_count` saying how many machines that id
        # reaches. That is a fleet's group-targeting menu sorted by size: an
        # automated actor holding it does not need to guess which single
        # identifier moves the most endpoints, it is told. `devices.read` is
        # the right gate for a HUMAN operator, who cannot dispatch without
        # `jobs.dispatch` anyway; whether an AI persona should hold the menu
        # is a question about that persona's autonomy dial, and it is the
        # question `PENDING_TOOL_REVIEW` exists to hold open rather than
        # answer by default.
        #
        # REMOVED 2026-09-08: it no longer passes six. The `evaluation` block
        # the device-group cycle added carries `full_pass` and
        # `last_full_pass_at`, which `looks_like_secret_key` reads as
        # secret-shaped, so the route fails P6 closed. The full reasoning, and
        # why the fields are not renamed here, is in the note above
        # `EXPECTED_GET_ROUTES`.
        # Added 2026-08-22 (paged ticket list, closeout rec. 10). Eligible on
        # all six, and deliberately not registered -- see the note above
        # EXPECTED_GET_ROUTES: this route is the full text of every ticket in
        # the tenant, `description` included.
        "/api/v1/service-desk/tickets",
        # Added 2026-08-24 (revoked tokens hidden by default). Eligible on all
        # six, and the one in this set whose non-registration is least
        # arguable: it enumerates a tenant's LIVE bootstrap credentials -- what
        # each is called, when it expires and how many redemptions it has left.
        # The plaintext is not here and never can be, but the list is the map
        # of every credential that can put a new machine into the fleet, which
        # is the first thing worth knowing to an actor that wants to add one.
        "/api/v1/enrollment-tokens",
        # Added 2026-08-24 (patch ring sizing). Eligible on all six, and
        # deliberately not registered -- the note above EXPECTED_GET_ROUTES says
        # why this one is a blast-radius question rather than the standing
        # well-formedness one.
        "/api/v1/patch/rings",
        # Added 2026-08-26 (stored documents). Eligible on all six; the
        # reasoning for leaving all three pending is above EXPECTED_GET_ROUTES.
        # The module's fourth GET route -- the one that serves the bytes -- is
        # deliberately absent from this set and cannot enter it.
        "/api/v1/docs/documents",
        "/api/v1/docs/documents/{document_id}",
        "/api/v1/docs/documents/{document_id}/versions",
        # Added 2026-09-23 (people); reasoning above EXPECTED_GET_ROUTES.
        "/api/v1/people",
        "/api/v1/people/by-user/{user_id}",
        "/api/v1/people/devices/{device_id}",
        "/api/v1/people/unmatched",
        # Added 2026-09-23 (ticket workbench); reasoning above EXPECTED_GET_ROUTES.
        "/api/v1/service-desk/canned-responses",
        "/api/v1/docs/suggest",
    }
)

# WHY THE FOUR ACCESS-CONTROL READS STAY UNREGISTERED, stated here because
# "pending review" with no reason decays into "nobody looked".
#
# Together they are the complete map of who can do what: the user directory,
# every role, the full 92-code catalogue, and a per-user resolution of exactly
# which permissions a person holds and by which grant. That is precisely the
# reconnaissance an attacker who had captured an AI persona would want first,
# and it is useful to them in a way that a device list is not — it names the
# accounts worth going after and the ones already able to reach everything.
#
# None of that makes the routes badly formed; they pass all six properties on
# their merits. It makes REGISTERING them a separate decision about a persona's
# blast radius, which is the distinction PENDING_TOOL_REVIEW exists to hold.

# Routes failing each property, counted PER ROUTE (a route failing property 5
# twice -- no summary and undescribed fields -- counts once). Quoted in
# ai-admin spec §17.2 as this codebase's outstanding bill, so they are pinned
# here: a number quoted in a document and measured nowhere is the exact thing
# this file exists to stop.
EXPECTED_FAILING_BY_PROPERTY = {
    "P2 gated": 6,
    "P3 typed": 27,
    # 80 -> 81 (2026-09-08): `GET /api/v1/agent-push/host-keys` answers a bare
    # list, like the two agent-push routes already counted here. See the note
    # above `EXPECTED_GET_ROUTES`.
    # +2 P4, +4 P5 (2026-09-23): the self-update GET routes.
    # +2 (2026-09-30): `/agent-push/runs` and `/agent-push/runs/prefixes`.
    # +3 (2026-10-24): `/clients`, `/clients/{client_id}/sites` and
    # `/clients/{client_id}/contacts`.
    # -1 (2026-10-28): `/ipam/sites`, removed (migration 0240).
    "P4 bounded": 92,
    # +2 (2026-09-23): the two portal help routes.
    # +3 (2026-09-30): `/agent-push/runs`, `/runs/{run_id}`, `/runs/{run_id}/targets`.
    # -1 (2026-10-28): `/ipam/sites`, removed (migration 0240).
    "P5 self-describing": 176,
}

# Genuinely gated, but invisible to `route_permission`: `remote/api.py` defines
# a local `require_any_permission`. Recorded as a finding about the remote
# module (reporting spec §16.3), pinned here so it cannot grow unnoticed.
EXPECTED_UNGATED_PATHS = frozenset(
    {
        "/api/v1/me",
        "/api/v1/remote/sessions",
        "/api/v1/remote/sessions/{session_id}",
        # Authenticated-only by design, not an oversight -- see the note on
        # EXPECTED_GET_ROUTES above and tests/test_route_gating.py.
        "/api/v1/orgs/current",
        # UNAUTHENTICATED, not merely ungated, and the only one in this set.
        # It answers before anybody can sign in — a fresh platform has no
        # identity to present — so a permission code here would be a code
        # nobody could hold. It discloses ONE boolean: whether the platform has
        # any users yet. What guards the act it precedes is the install code
        # plus zero-users, checked in `POST /setup/claim`; see
        # `tests/test_route_gating.py::UNGATED_BY_DESIGN` for the full
        # reasoning and `tests/test_setup_api.py` for the refusals.
        "/api/v1/setup/status",
        # Added 2026-09-01. The caller's OWN dashboard arrangement, and ungated
        # for the same reason `/api/v1/me` is: a permission code here would be
        # a code every role must hold, because this is how a person arranges
        # their own first screen. `Depends(bound_auth_context)`; the target is
        # `ctx.user_id` from the validated token and no verb takes a `user_id`,
        # so a caller reaches exactly one row and it is theirs. Storing a
        # layout grants nothing either -- every widget renders from a module
        # query gated on its own.
        "/api/v1/dashboard/layout",
    }
)


def _failures_by_path() -> dict[str, list[str]]:
    app = build_router_only_app()
    return {path: route_property_failures(route) for path, route in api_v1_get_routes(app)}


def test_the_tool_surface_measurement_is_unchanged() -> None:
    failures = _failures_by_path()
    passing_five = {p for p, f in failures.items() if not [x for x in f if x != "P6 closed"]}
    passing_six = {p for p, f in failures.items() if not f}
    ungated = {p for p, f in failures.items() if "P2 gated" in f}

    problems: list[str] = []
    if len(failures) != EXPECTED_GET_ROUTES:
        problems.append(f"GET routes: {len(failures)}, pinned {EXPECTED_GET_ROUTES}")
    if len(passing_five) != EXPECTED_PASSING_FIVE:
        problems.append(f"passing five: {len(passing_five)}, pinned {EXPECTED_PASSING_FIVE}")
    if passing_six != EXPECTED_PASSING_SIX:
        problems.append(
            f"passing six -- gained {sorted(passing_six - EXPECTED_PASSING_SIX)}, "
            f"lost {sorted(EXPECTED_PASSING_SIX - passing_six)}"
        )
    if ungated != EXPECTED_UNGATED_PATHS:
        problems.append(
            f"ungated -- gained {sorted(ungated - EXPECTED_UNGATED_PATHS)}, "
            f"lost {sorted(EXPECTED_UNGATED_PATHS - ungated)}"
        )
    for prop, expected in EXPECTED_FAILING_BY_PROPERTY.items():
        actual = sum(1 for f in failures.values() if prop in f)
        if actual != expected:
            problems.append(f"routes failing {prop}: {actual}, pinned {expected}")

    assert not problems, (
        "The tool surface moved. Re-measure, read which property each new route "
        "failed, and update this record in the same commit:\n  " + "\n  ".join(problems)
    )


def test_the_flattener_finds_routes() -> None:
    """The failure this whole file is worthless without: `app.routes` alone
    finds NOTHING under `/api/v1` in this codebase, and a sweep over an empty
    set reports a clean bill of health."""
    assert len(api_v1_get_routes(build_router_only_app())) > 100


def test_passing_six_is_a_subset_of_passing_five() -> None:
    """Property 6 can only ever REMOVE routes. If this fails, the predicate is
    counting something the five-property reading does not."""
    failures = _failures_by_path()
    passing_five = {p for p, f in failures.items() if not [x for x in f if x != "P6 closed"]}
    passing_six = {p for p, f in failures.items() if not f}
    assert passing_six <= passing_five


# --------------------------------------------------------------------------- #
# Spec §1, made falsifiable: the five properties do not imply registrability.
# --------------------------------------------------------------------------- #

# The seven routes that pass five properties and fail the sixth. Written out
# rather than derived, so that a route silently LEAVING this set (because
# somebody fixed it, or deleted it) is visible in a diff.
OPEN_BUT_OTHERWISE_PASSING = (
    "/api/v1/reports",
    "/api/v1/reports/fleet-posture",
    "/api/v1/reports/patch-compliance",
    "/api/v1/reports/backup-coverage",
    "/api/v1/reports/sla-attainment",
    "/api/v1/ai/actions",
    "/api/v1/ai/actions/{ai_action_id}",
)


def _tool_for(model: Any) -> ReadTool:
    """A `ReadTool` carrying the declaration the drift test would FORCE.

    `test_declared_response_fields_match_the_live_model` asserts
    `response_fields == every_field_name(response_model)` exactly, in both
    directions. So this is the only legal declaration for the model, and if it
    does not survive `scrub`, no legal declaration does.
    """
    return ReadTool(
        name="probe",
        method="GET",
        path="/api/v1/probe",
        permission="probe.read",
        args=None,  # type: ignore[arg-type]
        hint="probe",
        response_fields=frozenset(every_field_name(model)),
    )


@pytest.mark.parametrize("path", OPEN_BUT_OTHERWISE_PASSING)
def test_an_open_route_has_no_legal_declaration_that_survives_scrub(path: str) -> None:
    """SPEC §1, MADE FALSIFIABLE.

    For each route: build the forced declaration, then build the smallest
    payload the model permits which exercises the openness reason, and assert
    the REAL `scrub` blocks it. This is what makes "the five properties do not
    imply registrability" a fact about running code rather than a claim in a
    document -- the species of unfalsifiable assertion this project keeps
    finding in its own specs.
    """
    app = build_router_only_app()
    (route,) = [r for p, r in api_v1_get_routes(app) if p == path]
    _, model = inner_model(route.response_model)
    assert model is not None, "these routes pass P3 by definition"

    reasons = model_openness(model)
    assert reasons, f"{path} is closed -- it should not be in this list"

    tool = _tool_for(model)
    # EVERY reason must block, not merely one of them. An `any` here would let
    # a route with both a secret-shaped name AND an open dict pass on the name
    # alone, leaving the open-dict claim asserted by nothing -- and the reports
    # routes are exactly that shape, so the weaker form would have tested five
    # of the seven routes for only half of what this test claims about them.
    survived: list[str] = []
    for reason in reasons:
        field = reason.split("`")[1].split(".")[-1]
        if reason.startswith("secret-shaped"):
            payload: Any = {field: "x"}
        else:
            # An open dict carrying one data-derived column label, of the
            # shape `build_totals` actually produces. THE NAME MATTERS: an
            # earlier draft used one containing the word "key", which
            # `looks_like_secret_key` matched -- so pass 1 blocked it and
            # this branch passed without ever exercising the declared-
            # contract pass it exists to demonstrate. Widening the
            # declaration did not turn the test red, which is how that was
            # caught.
            payload = {field: {"devices_never_restore_tested": 1}}
        if not isinstance(scrub(tool, payload), Blocked):
            survived.append(reason)
    assert not survived, (
        f"{path}: the forced declaration SURVIVED scrub for {survived} -- "
        f"property 6 is stricter than the gate there, which would mean the "
        f"pinned count UNDERSTATES what is registrable"
    )


def test_the_two_registered_tools_are_closed() -> None:
    """The other direction: property 6 must not condemn what already works.

    Both registered tools serve results in production, so a predicate that
    called either one open would be wrong about the gate rather than about
    them."""
    from app.core.ai.tools import REGISTRY

    by_path = dict(api_v1_get_routes(build_router_only_app()))
    for tool in REGISTRY.values():
        _, model = inner_model(by_path[tool.path].response_model)
        assert model is not None
        assert model_openness(model) == [], f"{tool.name} is open"


# Passing all six properties makes a route REGISTERABLE, not registered --
# `app/core/ai/tools/registry.py`'s own header: "becoming an AI tool is a
# DELIBERATE, REVIEWED ACT." Task 5 (error monitoring's operator API) made
# `/api/v1/platform/errors` and `/api/v1/platform/errors/{group_id}` pass all
# six as a byproduct of writing an honestly-documented, closed-shape API --
# not as a request to expose either to the agent. Whether an AI persona
# should be able to read error groups (cross-org correlation ids included,
# spec §4.1) is a separate decision for whoever owns that registry, so this
# is a NAMED, PINNED exception rather than a silent widening of `registerable
# == registered` -- exactly the "route on the left" the test below already
# expects and asks a human to resolve.
PENDING_TOOL_REVIEW = frozenset(
    {
        # Added 2026-10-24 (MSP clients API); reasoning above
        # EXPECTED_GET_ROUTES.
        "/api/v1/clients/{client_id}",
        # Added 2026-10-14 (MCP console admin API); reasoning above
        # EXPECTED_GET_ROUTES.
        "/api/v1/mcp/connections",
        # Added 2026-09-28 (install link). Eligible on all six, and deliberately
        # not registered: it names the networks from which a machine may enrol
        # itself into this organisation -- the one fact an actor wanting to
        # plant a machine would ask for first. Per site since 2026-10-23 (MSP
        # clients spec §1.5): one link per site, the same fact, a site at a time.
        "/api/v1/sites/{site_id}/install-link",
        # Added 2026-09-23: script output may carry secrets; review first.
        "/api/v1/deploy/script-policies/{policy_id}/runs",
        "/api/v1/platform/errors",
        "/api/v1/platform/errors/{group_id}",
        # Added 2026-08-18. Eligible, not registered -- see the note above.
        "/api/v1/devices/candidates",
        "/api/v1/devices/suggestions",
        # Added 2026-08-19 (access control). Together these four are the
        # complete map of who can do what in the platform; the reasoning for
        # holding them is beside EXPECTED_PASSING_SIX above.
        "/api/v1/permissions",
        "/api/v1/roles",
        "/api/v1/users",
        "/api/v1/users/{user_id}/effective-permissions",
        # Added 2026-08-20 (invisible identity layer); reasoning beside
        # EXPECTED_PASSING_SIX above.
        "/api/v1/orgs/current/console-domain-preview",
        "/api/v1/settings/email",
        # Added 2026-08-21 (patch approvals list); reasoning beside
        # EXPECTED_PASSING_SIX above.
        "/api/v1/patch/approvals",
        # Added 2026-09-01 (patch apply runs); reasoning beside
        # EXPECTED_PASSING_SIX above.
        "/api/v1/patch/runs",
        # Added 2026-08-21 (device-group list), REMOVED 2026-09-08: the route
        # stopped being registerable at all when its `evaluation` block gained
        # two secret-shaped field names, so there is no longer a decision to
        # hold open. Reasoning beside EXPECTED_PASSING_SIX above.
        # Added 2026-08-22 (paged ticket list, closeout rec. 10). Eligible on
        # all six, and deliberately not registered -- see the note above
        # EXPECTED_GET_ROUTES: this route is the full text of every ticket in
        # the tenant, `description` included.
        "/api/v1/service-desk/tickets",
        # Added 2026-08-24 (enrollment-token list); reasoning beside
        # EXPECTED_PASSING_SIX above.
        "/api/v1/enrollment-tokens",
            # Added 2026-08-24 (patch ring sizing); reasoning beside
        # EXPECTED_PASSING_SIX above.
        "/api/v1/patch/rings",
        # Added 2026-08-26 (stored documents); reasoning beside
        # EXPECTED_GET_ROUTES above. Document metadata is tenant content.
        "/api/v1/docs/documents",
        "/api/v1/docs/documents/{document_id}",
        "/api/v1/docs/documents/{document_id}/versions",
        # Added 2026-09-23 (people); reasoning above EXPECTED_GET_ROUTES.
        "/api/v1/people",
        "/api/v1/people/by-user/{user_id}",
        "/api/v1/people/devices/{device_id}",
        "/api/v1/people/unmatched",
        # Added 2026-09-23 (ticket workbench); reasoning above EXPECTED_GET_ROUTES.
        "/api/v1/service-desk/canned-responses",
        "/api/v1/docs/suggest",
    }
)

def test_the_registry_already_holds_every_registerable_route() -> None:
    """THE CLOSURE, and the sharpest way to state this cycle's finding.

    Three routes pass all six properties. One of them is `/api/v1/ai/status`,
    and `test_ai_read_tools.py::test_nothing_forbidden_is_registered` bars every
    `/api/v1/ai` path outright -- a tool that calls the chat API is a recursion
    primitive. So the set of routes that COULD be registered today is exactly
    the set that IS registered, plus `PENDING_TOOL_REVIEW` above -- two more
    that Task 5 made registerable and deliberately left for a separate review.

    If this test ever fails with a route on the left that is not already named
    in `PENDING_TOOL_REVIEW`, a module has done the work and its registration
    is a real, reviewable decision somebody should make.
    """
    from app.core.ai.tools import REGISTRY

    failures = _failures_by_path()
    registerable = {
        p for p, f in failures.items() if not f and not p.startswith("/api/v1/ai")
    }
    registered = {t.path for t in REGISTRY.values()}
    assert registerable - PENDING_TOOL_REVIEW == registered, (
        f"unregistered but registerable: "
        f"{sorted(registerable - PENDING_TOOL_REVIEW - registered)}; "
        f"registered but no longer passing: {sorted(registered - registerable)}"
    )
    stale = PENDING_TOOL_REVIEW - registerable
    assert not stale, f"no longer registerable, drop from PENDING_TOOL_REVIEW: {sorted(stale)}"
