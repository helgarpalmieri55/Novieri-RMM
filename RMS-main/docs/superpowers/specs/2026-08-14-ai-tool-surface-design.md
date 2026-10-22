# The AI tool surface: a sixth property, and the measurement made checkable

**Date:** 2026-08-14
**Module:** `ai-admin` (read mode), kernel `app/core/ai/tools/`
**Status:** design, approved for planning

---

## 0. What this cycle is, and what changed while scoping it

The cycle was commissioned to do the thing three documents say is owed: reporting
shipped routes that pass `docs/modules/ai-admin/spec.md` §0.3's five properties,
its plan deliberately left the `ReadTool` entries out as "that module's reviewed
change", and nobody had made the change. Register them, correct the stale
measurements, and add a test that pins the counts so the next drift goes red.

**The registration turned out to be impossible, and finding out why is the
substance of this cycle.** The five properties do not imply a route can become a
tool. They measure *documentation*. `app/core/ai/scrub.py` demands something
else — a **closed key space** — and no property states it. The gap went unnoticed
because both currently registered tools happen to satisfy it by accident.

So this spec adds the missing property, makes the whole measurement executable
against the live router, and pins it. It registers no new tool, because after the
sixth property there is nothing registrable to register, and that is the finding
rather than a shortfall.

### 0.1 The numbers, measured 2026-08-14 against the live router

| measurement | count |
| --- | --- |
| `GET` routes under `/api/v1` | **188** |
| passing §0.3's five properties | **10** |
| **also closed, i.e. actually registrable** | **3** |
| registered as tools | **2** |

Route-level failure counts across all 188:

| property | failing routes |
| --- | --- |
| P2 gated (`route_permission` returns `None`) | 3 |
| P3 typed (`response_model` is a model or `list[Model]`) | 24 |
| P4 bounded (`list[Model]` with an explicit `limit`) | 78 |
| P5 self-describing (route prose + documented fields) | 166 |

The three that pass everything: `GET /api/v1/health/alerts`,
`GET /api/v1/devices/{device_id}/health` (both already registered) and
`GET /api/v1/ai/status`. The third answers no question about the estate — it
reports which model provider this deployment configured — so the registry
correctly stays at two.

**Every previously published figure is stale, and each was stale in the same
direction: it understated the route count and overstated how close the codebase
was.** `docs/modules/ai-admin/spec.md` §17.2 says 147 routes and 2 passing.
`docs/modules/reporting/spec.md` §0.10 says 173 and 3, §16.2 repeats it.
`docs/06-roadmap.md` Phase 17 says 2 of 147. All three predate routes that have
since shipped. The corrections are part of this cycle's deliverable, and §4
explains why the *next* one will not need a human to notice.

---

## 1. The finding: five properties are necessary and not sufficient

### 1.1 What `scrub` actually requires

`app/core/ai/scrub.py::scrub` runs two passes over every key at every depth of a
tool result, and either one rejects the **entire** result:

1. **The secret-name pattern.** `looks_like_secret_key(key)` — a word-level
   match over two decompositions of the key name.
2. **The declared contract.** Any key not in `ReadTool.response_fields` is
   blocked.

`response_fields` is a frozen literal, and
`test_declared_response_fields_match_the_live_model` asserts it equals
`_every_field_name(response_model)` **exactly, in both directions**. A key in the
model and not the declaration is an unreviewed addition; a key in the
declaration and not the model is a rotting allowlist. That equality is the
control, and it is correct.

The consequence nobody had drawn: **the declaration is forced to equal the set of
key names derivable from the Pydantic model.** So a response model whose payload
can carry a key that is *not* a model field name — an open `dict` — is
structurally unregisterable. There is no legal declaration. Declaring the keys
turns the drift test red as `stale`; omitting them blocks every result at
runtime, silently, forever.

### 1.2 Verified, not reasoned

Both failure modes were reproduced against the real `scrub` and the real
serializers before this spec was written, by constructing the declaration the
drift test would force and running the actual gate:

```
reports.catalogue    : BLOCKED at 'reports[0].key' -- key name matches the secret-material pattern
reports.fleet-posture: BLOCKED at 'meta.report_key' -- key name matches the secret-material pattern
```

and, with the secret-shaped names renamed away so the second pass could be
reached, the open-dict keys measured straight off the compiled SQL:

```
fleet-posture   totals keys = ['devices', 'stale_devices', 'devices_with_open_alerts']
patch-compliance totals keys = ['devices', 'devices_with_zero_missing', 'devices_never_scanned']
backup-coverage  totals keys = ['devices', 'covered', 'stale', 'failing', 'unprotected',
                                'devices_never_restore_tested']
                → none of them declarable
```

`ReportPage.totals` is `dict[str, int]` whose keys are the labels of a `SELECT`.
`CatalogueEntry.params_schema` is `dict[str, Any]` holding a JSON Schema, so its
key space includes `properties`, `required`, `additionalProperties`, `$defs` and
every parameter's wire name. `AiActionOut.rendered_params` is the same shape.

### 1.3 `key` is a secret word, on purpose

`key` is in `_SECRET_WORDS` and the matcher splits on word boundaries, so
`report_key` and `approved_action_key` match too. This is not a bug to route
around. The over-block is deliberate, and
`test_no_declared_response_field_is_secret_shaped` exists precisely to convert it
from a silent production degradation into a red build at declaration time, with
the remedy in its own message: *"Rename them in the module that owns the model,
or drop the route from the registry."*

This cycle takes the second option for every affected route, and §3 says why.

---

## 2. The sixth property: **closed**

Added to `docs/modules/ai-admin/spec.md` §0.3 as property 6.

> **6. Closed.** Every key the response can carry is derivable from the response
> model, and none of them is secret-shaped. Concretely: **no `dict` at any depth
> of the model**, and no field name at any depth for which
> `looks_like_secret_key` is true.

**No exception for `dict[str, SomeModel]`.** A mapping of typed values still has
data-dependent *keys*, and keys are what `scrub` walks — `{"<hostname>": {...}}`
is exactly as unregisterable as `{"<hostname>": 3}`. The rule is therefore stated
without a carve-out, which also means the predicate has one branch instead of
two. No route in the codebase currently returns that shape, so this costs
nothing today; stating it now is what stops it being discovered later by a module
author who reasonably assumed typed values were enough.

**Why a property and not a note.** The five properties are the thing the roadmap
calls a forcing function and the thing every module is told its bill is measured
against. A module can pay that bill in full and still be unable to expose a
single route. A bill that does not include the blocking requirement is not a
bill. Property 6 is the requirement `scrub` has enforced since it shipped, stated
where module authors read it.

**Why it is not "make `scrub` more permissive".** `app/core/ai/tools/registry.py`
is explicit that there must never be a per-tool field exemption, and the
declared-contract pass is described as *the* control — the one that catches the
homoglyph spellings the name pattern deliberately does not fold. Widening either
pass to admit open dicts would admit arbitrary data-controlled key names into
model context, which is the whole thing the gate exists to prevent. The
properties move; the gate does not.

**One consequence worth stating plainly.** Property 6 *lowers* the useful number,
from 10 to 3. That is the point. A measurement that counts routes which cannot be
used is worse than no measurement, because it is quoted.

---

## 3. What this cycle does not do, and why

**It does not change reporting.** Making its five routes registrable needs
`meta.report_key` and `CatalogueEntry.key` renamed, and `totals` and
`params_schema` restructured into typed models. The first is a breaking change to
the wire contract of a module that has just shipped, with a frontend bound to it
and `params_schema` published as a client-binding contract (reporting §11
requires clients to build their params bar from it). The second is a change to
the generic envelope that reporting §0.10's whole argument rests on. Neither is a
rename; both are that module's cycle, with its own spec.

**It does not register `/ai/status`.** It passes all six. It reports this
deployment's provider configuration and answers nothing about the estate, and
"passes the properties" has never been the criterion for registration — §0.2's
"deliberate, reviewed act" is.

**It does not fix the three ungated routes.** `GET /api/v1/me`,
`GET /api/v1/remote/sessions` and `GET /api/v1/remote/sessions/{session_id}` are
genuinely gated but invisible to `route_permission`, because `remote/api.py`
defines a local `require_any_permission`. Reporting §16.3 already recorded this
as a finding belonging to the remote module. This cycle pins the count at 3 so
that it cannot grow unnoticed, and leaves the fix where it belongs.

---

## 4. The enforcement

### 4.1 One predicate, two consumers

A new `backend/tests/tool_surface.py` holds the property implementations —
`route_property_failures(route)` returning a list of failed property labels, and
`model_openness(model)` returning the reasons a model's key space is not closed.

Two consumers, and the sharing is the point:

- `tests/test_ai_read_tools.py::test_every_registered_tool_passes_the_five_properties`
  (renamed to `..._the_six_properties`) — the **gate**, scoped to `REGISTRY`.
  Currently reimplements the properties inline; it moves onto the shared
  predicate so the gate and the measurement cannot diverge.
- `tests/test_ai_tool_surface.py` (new) — the **sweep**, over every `GET` route
  under `/api/v1`.

A property implemented twice is a property that will be implemented differently.

### 4.2 The sweep pins exact counts

Per the scoping decision: exact numbers, not a floor.

```python
EXPECTED = ToolSurface(
    get_routes=188,
    passing_five=10,
    passing_six=3,
    ungated=3,
)
```

The assertion compares the whole record at once and its failure message names
each number that moved, in which direction, and — for the pass counts — the
route paths that entered or left the set. A floor would not have noticed that
between the ai-admin measurement and today the route count grew by 41 while the
registrable count grew by 1.

**This test is deliberately brittle, and the brittleness is the deliverable.**
Any route added anywhere turns it red. The message says what to do: re-run the
measurement, update the number, and — if the route was meant to be
agent-reachable and is not — read which property it failed. The alternative is
the status quo, in which three documents carried three different wrong numbers
and the drift was found only because someone re-measured by hand.

### 4.3 The sweep must not need a database

The existing `app` fixture requires a Postgres cluster. The properties are static
analysis of the router — no query runs — so the sweep builds its own application
with `create_app` and throwaway settings, and asserts on `app.router`. This keeps
the measurement runnable anywhere, including environments without a database.

`create_app` sets the process-wide audit HMAC key as a side effect. Every test
that needs a real one goes through `api_env`, which calls `create_app` itself, so
the key is re-established per test. The plan verifies this rather than assuming
it: the full backend suite must be green with the new test present, and the
verification is stated as a step, not a hope.

### 4.4 The denylist grows two entries

`GET /api/v1/ai/actions` and `GET /api/v1/ai/actions/{ai_action_id}` join
`_NEVER_A_TOOL` in `app/core/ai/tools/registry.py`, citing ai-admin §12.3: *"Read
mode does not read that registry at all. It does not list approved actions, does
not describe them to the model, and adds nothing to them."*

Today that rule is a sentence in a spec. `test_denylist_entries_resolve_to_real_routes`
turns it into a tripwire that cannot rot, which is the same treatment the
credential surface already gets.

Note that these two routes *also* fail property 6 (`approved_action_key`,
`rendered_params`), so they are doubly excluded. The denylist entry is still the
right mechanism: property 6 is a fact about today's model that a future refactor
could change, and §12.3 is a decision that should outlive it.

### 4.5 A seam left alone, and named

`app/modules/reporting/registry.py` has its own `_NEVER_A_TOOL` covering the
export routes, tested by `test_the_never_a_tool_list_covers_exactly_the_export_routes`.
The kernel's registry test consults only the kernel's list, so the two denylists
are not federated: a reporting export route registered as a tool would not be
caught by the kernel's "nothing forbidden is registered" test. It would be caught
by property 3 (`PlainTextResponse`), so nothing is currently exposed. Recorded
here because the defence is incidental rather than designed; federating the lists
is not in this cycle.

---

## 5. Documentation corrections

Three documents carry the stale measurement. Each is corrected in place, with the
superseded figure quoted rather than deleted — the house style for a correction,
so a reader who remembers the old number can see what happened to it.

1. **`docs/modules/ai-admin/spec.md` §0.3** — property 6 added.
   **§17.2** — the table re-measured (147→188, 2→10 passing five, 3 passing six),
   and the finding of §1 recorded: the five properties never implied
   registrability, and this is why the registry did not grow when reporting
   shipped five qualifying routes.
2. **`docs/modules/reporting/spec.md` §0.10 and §16.2** — re-measured, plus the
   §1 finding stated where it bites hardest: *this module's routes pass the five
   properties by construction and are not registrable*, which is a true and
   uncomfortable sentence that §0.10's argument needs in order to stay honest.
   §10.1's "each becomes a `ReadTool` entry in the ordinary, deliberate, reviewed
   way" is amended to record that the reviewed act happened, and its answer was
   no, with the bill for changing that answer.
3. **`docs/06-roadmap.md` Phase 17** — the "2 of 147" paragraph re-measured, and
   its "specific, checkable bill of work" sentence extended with the sixth item,
   since a module that pays the stated bill in full today still cannot be
   registered.

---

## 6. Testing requirements

Test-first per `CLAUDE.md`.

1. **The sweep pins the four counts** and fails with a message naming what moved
   (§4.2). Falsifier: add a `GET` route to any module and the test goes red.
2. **The gate runs six properties, not five**, over `REGISTRY`, on the shared
   predicate. Falsifier: give a registered tool's model an open `dict` field and
   the gate goes red.
3. **`model_openness` finds both failure modes**, unit-tested on constructed
   models: an open `dict[str, int]`, an open `dict[str, Any]` nested two models
   deep, a secret-shaped field name, a `dict[str, SomeModel]`, and a closed model
   that must return no reasons. Falsifier: the closed case is the one that
   catches a predicate that flags everything.
4. **The predicate agrees with `scrub`.** For each of the seven routes that pass
   five but fail six, assert that the declaration the drift test would force
   (`_every_field_name(model)`) does **not** survive `scrub` against a realistic
   payload. This is the test that makes §1 a property of the code rather than a
   claim in a document, and it is the one that would have prevented this cycle's
   premise from being wrong.
5. **The two new denylist entries resolve to real routes**, via the existing
   parametrised liveness test — no new test, the entries join the existing one.
6. **The full backend suite is green with the new test present** (§4.3), run and
   its output reported, not asserted.

---

## 7. What would falsify this design

- **If `create_app` in the sweep pollutes other tests**, §4.3's premise is wrong
  and the sweep needs a router built another way. The full-suite run is what
  would show it.
- **If the pinned counts turn red on unrelated work often enough to be
  routinely bumped without reading**, the brittleness has become noise rather
  than signal and the floor variant should be reconsidered. The observable is
  a commit that changes the numbers without changing any route.
- **If property 6 blocks a route that `scrub` would in fact pass**, the predicate
  is stricter than the gate and would be understating the count — testing
  requirement 4 is what checks the direction that matters, and a symmetric case
  should be added if one is found.

---

## 8. Sources

- `docs/modules/ai-admin/spec.md` §0.2, §0.3, §0.5, §4, §7.1, §7.3, §12.3, §17.2
- `docs/modules/reporting/spec.md` §0.10, §10.1–§10.3, §11, §16.2, §16.3
- `docs/06-roadmap.md` Phase 17
- `backend/app/core/ai/scrub.py`, `backend/app/core/ai/tools/registry.py`
- `backend/tests/test_ai_read_tools.py`
- Measurements run 2026-08-14 against the live router at branch HEAD.
