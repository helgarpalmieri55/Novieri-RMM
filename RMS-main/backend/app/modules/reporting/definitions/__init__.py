"""The v1 report catalogue: import a definition module, register it, verify.

**`verify_registry()` RUNS HERE, AT IMPORT, NOT ONLY IN THE TEST SUITE.** A
definition whose SQL reads a table it did not declare fails the APPLICATION's
import -- the process does not start -- rather than failing a test somebody may
have skipped. The failure it prevents is a data disclosure (§0.4: a definition
declaring `{"devices"}` and joining `ticket` derives `{"devices.read"}` and
serves ticket data to a caller the ticket API refuses), and "the suite would
have caught it" is the wrong side of that trade for a fail-closed property.

The cost is stated rather than hidden: a bad definition takes the whole control
plane down at boot instead of taking one report down. That is the correct
direction for this particular check -- the alternative is a control plane that
starts and serves the leak.

Registration is by an explicit call per definition, not by scanning the package
for modules. A scanner would make "is this report registered" depend on a
filename, and would happily pick up a half-finished definition somebody left in
the directory.
"""

from app.modules.reporting.definitions.backup_coverage import DEFINITION as BACKUP_COVERAGE
from app.modules.reporting.definitions.fleet_posture import DEFINITION as FLEET_POSTURE
from app.modules.reporting.definitions.patch_compliance import (
    DEFINITION as PATCH_COMPLIANCE,
)
from app.modules.reporting.definitions.sla_attainment import DEFINITION as SLA_ATTAINMENT
from app.modules.reporting.registry import register
from app.modules.reporting.sources import verify_registry

register(FLEET_POSTURE)
register(PATCH_COMPLIANCE)
register(BACKUP_COVERAGE)
register(SLA_ATTAINMENT)

verify_registry()

__all__ = [
    "BACKUP_COVERAGE",
    "FLEET_POSTURE",
    "PATCH_COMPLIANCE",
    "SLA_ATTAINMENT",
]
