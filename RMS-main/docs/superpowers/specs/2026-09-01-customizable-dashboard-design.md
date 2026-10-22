# Customizable dashboard — design

**Status:** built, 2026-09-01.
**Request (verbatim):** "main dashboard should be customizable with tiles also
give me the ability to resize and order in whichever way i want."

---

## 1. What was there

`DashboardPage.tsx` composed thirteen widgets — six stat tiles and seven
panels — from the module queries that already exist, because there is no
dashboard endpoint and there never has been. The arrangement was three fixed
containers: a `.stats-grid` of tiles, then two `.grid-2` rows of panels.

The containers are why "order it whichever way i want" was not expressible as
a small change. A widget cannot move from the second row into the first when
the two rows are different elements with different column definitions, so the
first decision is structural: **one grid for the whole page**, and a widget's
width becomes a span within it rather than a property of which box it is in.

## 2. Decisions

### D1. Six columns, three named sizes

`small` spans 2, `medium` 3, `wide` 6. The stock arrangement therefore lands
where the page already was — three tiles per row, two panels per row — with
one visible change: the panel row's deliberate `1.55fr / 0.8fr` asymmetry
becomes an even split. That is the price of the widths being something the
operator sets rather than something the stylesheet asserts, and it is paid
once.

The stored value is the NAME, never the column count. Pinning "small" to `2`
in the database would put a CSS decision in a schema, and a later change to the
grid would have to migrate every stored row to keep the same screen.

### D2. Per-user, server-side, not `localStorage`

`dashboard_layouts (org_id, user_id) → layout JSONB`, one row, overwritten in
place, migration 0082.

The cheap answer is the browser and it is wrong for this product specifically:
an operator signs in from the NOC workstation, a laptop and somebody else's
machine while standing at their desk, and a layout that exists on one of the
three is worse than no customization — it looks broken rather than absent. It
is also lost by the private window, the cleared site data and the reimaged
workstation, which is a fleet-management platform's ordinary Tuesday.

Not a column on `users`: that row is read on the authentication path for every
request, and a JSONB blob a console feature rewrites on every drag does not
belong in it. A separate table also makes "reset to default" a `DELETE` with no
`NULL`-versus-empty ambiguity to interpret.

### D3. The server validates shape, never membership

The set of widgets is a property of the console BUILD. A release that adds a
tile must not need a server change; one that removes a tile must not strand
every stored row that names it. So the server bounds the document — at most 64
entries, ids matching `^[a-z0-9][a-z0-9-]{0,63}$`, sizes from a closed set,
`extra="forbid"` — and does not check that any id is a widget anyone has heard
of.

The reconciliation lives in `layout.ts::resolveLayout`, and it must handle the
disagreement in **both** directions, because both occur on the first release
after somebody saves a layout:

* an id the build does not have is **dropped** from the rendering, and not
  written back — a save from an old tab must not delete a widget a newer tab
  arranged;
* a widget the stored list does not mention is **appended, visible**. Hiding
  what the row does not name would make every new dashboard feature invisible
  to exactly the operators who use the product enough to have customized it.

Both halves are load-bearing. Without the server's bound, anyone with a session
could store an arbitrarily large document; without the client's tolerance, a
stored row would blank somebody's dashboard on the first release that touched
the widget list.

### D4. Ungated beyond authentication

Same argument as `PATCH /me`: a permission code here would be a code every role
must hold, since this is how a person arranges their own first screen. Safe
without one because **the target is `ctx.user_id` from the validated token and
there is no `user_id` parameter on any of the three routes**, so a caller
reaches exactly one row and it is theirs. Recorded in
`tests/test_route_gating.py::UNGATED_BY_DESIGN`, one stated reason per entry.

Storing a layout grants nothing. Each widget renders from a module query gated
on its own, and the server re-checks every one (invariant 6); naming a widget
in a layout does not make its data readable.

**Not audited.** Invariant 4 covers privileged action — job dispatch,
credential access, config and policy change. Where a person put a tile on their
own screen is none of those, and writing it to the hash-chained log would
dilute a record whose entire value is that everything in it matters.

### D5. Permission filters BEFORE the layout resolves

A widget the caller may not see is not in the arrangement at all, rather than
present-and-hidden. The alternative rewrites a saved layout because somebody's
role changed for an afternoon.

### D6. Buttons are the feature; drag is the convenience

WCAG 2.2 SC 2.5.7 (Dragging Movements) requires a single-pointer alternative to
any drag and SC 2.1.1 requires a keyboard one; `docs/product.md` §11 makes AA
binding for this console. Move / Size / Hide satisfy both by construction, and
HTML5 drag-and-drop is layered on top for a mouse. Building it the other way
round — drag first, keyboard "later" — is how a rearrangeable dashboard ships
inaccessible.

Consequences that are not decoration:

* every button names its widget in its accessible name ("Move Open alerts
  earlier"). Edit mode puts three controls on each of a dozen widgets; three
  dozen buttons called "Move earlier" are indistinguishable to anyone not
  looking at the screen.
* a move that would fall off an end is **disabled**, not inert. `moveWidget`
  returns the same array for it, so no save is attempted either.
* one Size button that CYCLES, not a grow/shrink pair — with three sizes a pair
  is disabled a third of the time each, which reads as broken rather than as
  at-the-limit.

### D7. Edit is a mode

A dashboard whose tiles are always draggable fights its reader: selecting a
hostname to copy it becomes a drag, and a toolbar over every panel turns a
glance-at screen into a settings screen. Outside edit mode the page renders
exactly what it rendered before.

### D8. Write-through, and a failed save keeps the screen

Every change saves immediately, so there is no Save button to forget and no
unsaved state to lose on navigation; "Done" only leaves edit mode. The PUT is
last-write-wins over the WHOLE arrangement, which is the right shape here: the
document is small, the writer is one person, and a merge of two orderings has
no meaning anyone could define.

If a save fails the arrangement **stays** and the page says so. Reverting would
throw away work the operator can see themselves doing, to reach a state they
did not ask for.

### D9. A hidden widget keeps its place

`hidden: true` on the entry, in position, rather than removal from the list.
Putting a widget back must restore where it was; a layout that dropped hidden
widgets would make "hide" and "forget where this was" the same gesture. In edit
mode it renders as a dimmed empty frame so the place is visible; outside edit
mode it is not rendered at all.

## 3. What is measured

* `layout.test.ts` — the reconciliation in both directions, duplicate ids,
  the identity return on a no-op move.
* `DashboardPage.test.tsx` — the buttons drive every assertion (see D6), the
  save payload, the disabled end-move, the failed save keeping the screen, the
  permission filter, and a stored layout with a widget this build added since.
* `test_dashboard_layout.py` — absent-vs-empty, order preserved, upsert not
  append, idempotent reset, **two operators in one org do not share a layout**,
  shape bounded without membership, and an RLS test that exercises the
  BYPASSRLS admin engine (a route test cannot fail on a dropped policy, since
  `org_session` runs NOBYPASSRLS and the route also filters in Python).
* `/__preview` renders the edit-mode frame in all three of its states, so the
  axe sweep and the composited-contrast pass measure the new toolbar and the
  dashed outline (a non-text UI boundary, SC 1.4.11's 3:1). Without that the
  new surface would be one the harness never sees.

## 4. Not built, and why

* **Free 2-D placement (row × column, arbitrary gaps).** A wrapping span grid
  keeps every arrangement legible at every breakpoint on its own; a saved
  absolute grid has to be re-flowed for phones anyway, at which point the
  stored positions are advisory and the operator's arrangement is a fiction
  below 1024px.
* **Per-org default layouts an admin sets.** Nobody asked, and it needs a
  second row shape, a precedence rule and an "inherit vs. override" state on
  every widget.
* **Widgets that are not already on the page.** This cycle rearranges what the
  dashboard composes. New panels are new data questions and each needs its own
  endpoint argument — the table of what is deliberately absent, and why, is at
  the top of `DashboardPage.tsx` and is unchanged.
