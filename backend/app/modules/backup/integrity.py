"""The constraint an `IntegrityError` names, for the backup module's routes
and services that answer one of their own keys' violations as a 409.

asyncpg puts it on the driver error SQLAlchemy wraps (`exc.orig.__cause__`);
any other error, or one naming no constraint, gives None -- re-raised by the
caller, never answered.
"""

from sqlalchemy.exc import IntegrityError


def violated_constraint(exc: IntegrityError) -> str | None:
    return getattr(getattr(exc.orig, "__cause__", None), "constraint_name", None)
