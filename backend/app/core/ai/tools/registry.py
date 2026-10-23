"""THE STATIC READ-TOOL ALLOWLIST -- the control the whole AI layer rests on.

docs/modules/ai-admin/spec.md §0.2, §0.3, §0.4, §0.5, §4, §7.1;
docs/modules/ai-automation.md §2.1, §19.9.

KERNEL, NOT A PERSONA'S. This file shipped under `app/modules/ai_admin/tools/`
and the paragraph below -- "a read tool is PLATFORM CAPABILITY, authored by the
platform" -- was already the argument for moving it. Every staff persona's tool
set is built from these objects: the admin persona takes the whole mapping, the
technician copilot names a subset (`app/modules/ai_copilot/tools.py`). Sharing
the OBJECTS rather than copying them is what keeps `response_fields` under one
drift test; see `__init__.py` for why a copy is the silently-wrong option.

WHY THE ALLOWLIST AND NOT "THE API DOESN'T RETURN SECRETS": IT DOES.
`app/api/v1/credentials.py::checkout_credential` returns
`CheckoutResponse(secret=...)` -- plaintext, in an HTTP response body -- and
`GET /api/v1/licenses/{license_id}/key` returns a DECRYPTED license key. Both
are correct point-of-use behaviour under invariant #3, which is exactly why
**the allowlist, not the API's own reticence, is what protects vault material
from the agent.** An admin who legitimately holds `credentials.checkout` would
get plaintext back from a loopback call, because §0.1's whole design is that the
agent gets exactly what the admin gets. "Bounded by the admin's RBAC" is
therefore NOT sufficient for invariant #3. Read that sentence again before
adding anything below.

THIS IS A PYTHON DICT COMPILED INTO THE IMAGE, NOT A DATABASE TABLE (§0.2).
Whatever compromises the database would otherwise choose the agent's
capabilities, and the entire prompt-injection story would then rest on database
integrity rather than on code. This is deliberately ASYMMETRIC with the
`approved_action` registry, which IS a table: an approved action is ORG POLICY,
authored by an org-admin; a read tool is PLATFORM CAPABILITY, authored by the
platform. Read mode does not read that table at all (§12.3).

EXPOSING EVERY `GET` ROUTE AUTOMATICALLY IS REJECTED (§0.2). There are 147 `GET`
routes under `/api/v1`; some return bytes, some return `PlainTextResponse` CSV,
and one returns a decrypted secret. Auto-exposure means a module author adds a
route and silently adds an AI tool -- the opposite of the property this module
needs, which is that becoming an AI tool is a DELIBERATE, REVIEWED ACT. The
cost is that adding a module's read API to the agent requires a code change and
a release. That is the point.

AN ENTRY DECLARES WHAT ITS ROUTE MAY RETURN, NOT ONLY WHICH ROUTE IT CALLS.
`response_fields` is every key name the response may carry at any depth, and
`scrub` blocks the entire tool result on anything else (§7.3). Adding a tool
therefore means reading its response model, not just its path -- and the day a
module adds a field to a model this list names, the build goes red here
(`test_declared_response_fields_match_the_live_model`) and the tool goes dark at
runtime until somebody looks at the new field. THAT IS THE POINT, and it is the
same "deliberate, reviewed act" property the paragraph above claims for routes,
finally applied to payloads. The cost -- a field-name list duplicated from
another module's schema -- is accepted for the same reason the allowlist itself
is a Python literal: a set computed from the live model would grow WITH the
model, and would allowlist the very field the gate exists to catch.

---

THE v1 SET IS TWO TOOLS, AND THAT IS A FINDING ABOUT THE CODEBASE, NOT A
SHORTCUT. §0.3 requires five mechanical properties of any exposed route, the
fifth being "the route carries a `summary`, and every field of its response
model carries a `Field(description=...)` or a docstring". Measured against the
live router at build time:

  * 147 `GET` routes under `/api/v1`;
  * **0** carry `summary=` on the decorator (the whole repo has one `summary=`,
    and it is a dict passed to something else);
  * **1** `Field(description=...)` exists across every module's `schema.py`;
  * 13 routes carry an endpoint docstring (which FastAPI surfaces as
    `description`), and 14 typed response models carry a class docstring;
  * **2** routes satisfy all five properties.

Both survivors are from the endpoint-health module, which was built to a higher
documentation bar than its predecessors. Spec §4.2's fourteen-area table is
therefore ASPIRATIONAL against today's code, and §0.3 predicted exactly this
("the first build of this module will find that most response models have no
field descriptions... the honest consequence is that the v1 tool set is small").
The bar was NOT lowered to hit the table, and no other module's response models
were edited to reach it -- that is separate, reviewable work in those modules.
`tests/test_ai_read_tools.py::test_every_registered_tool_passes_the_five_properties`
runs the five properties against the live router for every entry here, so
widening this dict to an undescribed, untyped or unbounded route is a red build
naming the property that failed.
"""

from collections.abc import Mapping
from types import MappingProxyType

from app.core.ai.tools.args import DeviceHealthArgs, HealthAlertsArgs
from app.core.ai.types import ReadTool

# --------------------------------------------------------------------------- #
# The allowlist.
# --------------------------------------------------------------------------- #

_TOOLS: tuple[ReadTool, ...] = (
    ReadTool(
        name="devices.health",
        method="GET",
        path="/api/v1/devices/{device_id}/health",
        # Read out of `route_permission(route)`, never the code we expected:
        # this route is gated on `devices.read` and NOT `health.read`, because a
        # technician looking at a device needs its health without holding health
        # administration rights.
        permission="devices.read",
        args=DeviceHealthArgs,
        hint=(
            "Use for one named device's current health: its effective profile, "
            "each check's state, policy drift, and its open alerts."
        ),
        # §7.3's CONTROL: every key `DeviceHealthOut` may return, at any depth
        # -- its own fields plus `ProfileOut`, `AssignmentOut`,
        # `DeviceCheckStateOut` and `AlertOut`. `scrub` blocks the whole result
        # on anything else, so the day `health/schema.py` grows a field this
        # tool goes dark until somebody reviews the addition and lands it here.
        # That is the intended cost; see `scrub.py`'s header.
        #
        # WRITTEN OUT RATHER THAN COMPUTED FROM THE MODEL. A set derived from
        # `route.response_model` would grow with the model and allowlist the new
        # field automatically, which is the exact failure this replaced.
        # `test_declared_response_fields_match_the_live_model` is what keeps the
        # literal honest.
        response_fields=frozenset(
            {
                "acknowledged_at", "acknowledged_by", "acknowledged_policy_version",
                "active", "agent_last_seen_at", "agent_online", "assignment",
                "assignment_count", "check_count", "check_id", "check_type", "checks",
                "cleared_at", "cleared_by", "cleared_reason", "comparator",
                "created_at", "created_by", "description", "device_group_id",
                "device_hostname", "device_id", "escalated_at", "evaluated_by",
                "flap_count", "flapping", "for_seconds", "for_ticks", "has_profile",
                "hostname", "id", "last_seen_at", "last_value", "level", "muted_by",
                "muted_until", "name", "observed_value", "occurrence_count",
                "offline_after_s", "open_alerts", "opened_at",
                "opened_policy_version", "os_family", "policy_drift",
                "policy_version", "posture_interval_s", "previous_alert_id",
                "profile", "profile_id", "profile_name", "reported_at",
                "resolution_level", "selector", "severity", "state",
                "summary_interval_s", "threshold_display", "tick_interval_s",
                "ticket_id", "updated_at",
            }
        ),
    ),
    ReadTool(
        name="health.alerts",
        method="GET",
        path="/api/v1/health/alerts",
        permission="health.read",
        args=HealthAlertsArgs,
        hint="Use for 'what is alerting right now', optionally narrowed to one device.",
        # §10.2: the model cannot request 10,000 rows. The route itself allows
        # up to 500; the executor clamps to this before the request is built.
        max_limit=50,
        # `list[AlertOut]`. The executor's `{items, returned, total, truncated}`
        # envelope is built AFTER `scrub` runs, so it is deliberately absent
        # here -- declaring it would mean the scanner had been made to accept a
        # shape it never sees.
        response_fields=frozenset(
            {
                "acknowledged_at", "acknowledged_by", "check_id", "check_type",
                "cleared_at", "cleared_by", "cleared_reason", "device_group_id",
                "device_hostname", "device_id", "escalated_at", "flap_count",
                "flapping", "id", "last_seen_at", "muted_by", "muted_until",
                "observed_value", "occurrence_count", "opened_at",
                "opened_policy_version", "previous_alert_id", "profile_id",
                "selector", "severity", "state", "threshold_display", "ticket_id",
            }
        ),
    ),
)

# **A `MappingProxyType`, NOT A DICT.** The type annotation says `Mapping`, which
# stops a type checker accepting `REGISTRY[name] = tool` -- but a type annotation
# is not a runtime property, and `REGISTRY["credentials.list"] = ...` from any
# module in the process succeeded until this wrapper was added (found by
# `tests/test_ai_boundaries.py`, which had to mutate it to prove it could
# not be mutated, and polluted the rest of the run doing so). §0.2 says the
# allowlist is compiled into the image; a mapping any code can widen at runtime
# is not that. The proxy makes widening a `TypeError`.
REGISTRY: Mapping[str, ReadTool] = MappingProxyType({tool.name: tool for tool in _TOOLS})


# --------------------------------------------------------------------------- #
# The two denylists (§7.1).
# --------------------------------------------------------------------------- #

# Explicit `(method, path)` pairs that must NEVER become a tool, each verified
# to exist on the live router by `test_denylist_entries_resolve_to_real_routes`.
# That liveness test is the important half: a denylist entry whose path has
# drifted denies NOTHING, and the "nothing forbidden is registered" test would
# then pass happily against a registry that had acquired the real credential
# route under its new path.
#
# The ENTIRE credential surface is here, including harmless metadata (§0.5).
# That is deliberately conservative: excluding metadata too means a successful
# injection cannot even get the agent to ENUMERATE credential names as
# reconnaissance for a later step. If that answer is wanted later it needs its
# own decision, not a quiet registry addition.
_NEVER_A_TOOL: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/api/v1/credentials"),
        ("GET", "/api/v1/credentials/{credential_id}"),
        ("POST", "/api/v1/credentials/{credential_id}/checkout"),
        ("PUT", "/api/v1/credentials/{credential_id}"),
        ("DELETE", "/api/v1/credentials/{credential_id}"),
        ("POST", "/api/v1/credentials"),
        ("GET", "/api/v1/licenses/{license_id}/key"),
        # ACT MODE'S OWN SURFACE (ai-admin spec §12.3): "Read mode does not read
        # that registry at all. It does not list approved actions, does not
        # describe them to the model, and adds nothing to them."
        #
        # Until now that was a sentence in a spec and nothing else.
        # `test_denylist_entries_resolve_to_real_routes` makes it a tripwire
        # that cannot rot into pointing at a path nothing serves.
        #
        # Both routes ALSO fail property 6 today -- `approved_action_key` is
        # secret-shaped and `rendered_params` is an open dict -- so they are
        # doubly excluded. The denylist entry is still the right mechanism:
        # property 6 is a fact about today's models that a refactor could
        # change, and §12.3 is a decision meant to outlive it.
        ("GET", "/api/v1/ai/actions"),
        ("GET", "/api/v1/ai/actions/{ai_action_id}"),
    }
)

# Gate-based exclusion, which catches routes that MOVE. A path can be renamed;
# the permission a secret-revealing route is gated on is far stickier.
_FORBIDDEN_GATES: frozenset[str] = frozenset(
    {
        "credentials.read",
        "credentials.checkout",
        "credentials.manage",
        "licenses.keys.read",
    }
)
