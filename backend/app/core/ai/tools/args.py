"""Per-tool argument models -- CLOSED field sets (spec §4.4).

Every model here is `extra="forbid"`. The model produces JSON; anything that is
not exactly these fields is a tool error returned to the model, never an HTTP
request. **Arguments are never coerced, repaired, or guessed** (§10.5): a
best-effort repair is the executor deciding what the model meant, which is the
executor writing tool calls.

PATH PARAMETERS ARE TYPED `UUID`, NOT `str`. That is what makes a crafted path
segment (`"../credentials"`) fail VALIDATION before any URL is built, rather
than being caught later by string handling that has to be right every time.

NO MODEL HERE DECLARES AN ORG. Tenancy comes from the token (§0.9), and a test
asserts no `org_id`/`org`/`organization_id` field exists on any of them.
"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class _ClosedArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DeviceHealthArgs(_ClosedArgs):
    """`GET /api/v1/devices/{device_id}/health`."""

    device_id: UUID = Field(description="The device to report health for.")


class HealthAlertsArgs(_ClosedArgs):
    """`GET /api/v1/health/alerts`.

    `state` and `severity` are plain strings because the route accepts plain
    strings and answers an unknown value with an empty list; narrowing them to
    a `Literal` here would make the tool 422 where the route would not, and the
    registry must describe the route it actually calls.
    """

    state: str | None = Field(
        default=None, description="Filter by alert state, e.g. 'open' or 'acknowledged'."
    )
    severity: str | None = Field(default=None, description="Filter by severity.")
    device_id: UUID | None = Field(default=None, description="Only alerts for this device.")
    limit: int | None = Field(
        default=None,
        description="Maximum alerts to return. Clamped server-side; ask for fewer, not more.",
    )
