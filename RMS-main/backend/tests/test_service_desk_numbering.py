"""Ticket-number allocation (docs/modules/service-desk/spec.md §2.1).

The concurrency test is the point of this file. A check-then-increment
implementation passes every sequential test here and produces duplicate
numbers in production.
"""

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.models import TicketNumberCounter
from app.modules.service_desk.numbering import next_number


async def test_numbers_start_at_one_and_increment(api_env) -> None:
    org_id = await api_env.create_org("sd-num")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        assert await next_number(s, org_id, "incident") == "INC-1"
        assert await next_number(s, org_id, "incident") == "INC-2"
        await s.commit()


async def test_incident_and_request_sequences_are_independent(api_env) -> None:
    org_id = await api_env.create_org("sd-num-split")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        assert await next_number(s, org_id, "incident") == "INC-1"
        assert await next_number(s, org_id, "request") == "REQ-1"
        assert await next_number(s, org_id, "incident") == "INC-2"
        await s.commit()


async def test_sequences_are_per_org(api_env) -> None:
    org_a = await api_env.create_org("sd-num-a")
    org_b = await api_env.create_org("sd-num-b")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        assert await next_number(s, org_a, "incident") == "INC-1"
        assert await next_number(s, org_b, "incident") == "INC-1"
        await s.commit()


async def test_concurrent_allocation_never_duplicates(
    api_env, admin_engine: AsyncEngine
) -> None:
    """Two sessions allocating "at the same instant" must never get the same
    number.

    This is a DETERMINISTIC interleave, not a hopeful `asyncio.gather` --
    two things were tried and rejected first:

    1. Plain sequential awaits on two sessions (`a = await next_number(sa,
       ...); await sa.commit(); b = await next_number(sb, ...)`) never
       overlap at all: session B's read always lands strictly after session
       A's commit, so this passes against a read-then-write implementation
       just as easily as the atomic one -- it proves nothing. Verified
       directly: run against a temporary read-then-write swap of
       `next_number`, this shape still returns two different numbers.
    2. `asyncio.gather` on two full `next_number` calls does create real
       overlap against a read-then-write implementation, but was observed
       (empirically, in this environment) to hang rather than race cleanly:
       `pg_stat_activity` showed one session's `UPDATE` sitting `idle in
       transaction` waiting on the client while the other task never got
       scheduled to send its own statement. Not safe to ship as a test that
       must terminate.

    Instead, following the same shape as
    `test_budget_fx.test_get_settings_concurrent_first_calls_do_not_race`,
    session A's own `execute` is wrapped so that the instant A's SELECT of
    its counter row returns -- the read half of a read-then-write bug --
    session B runs a full, independent allocation and commits before A's
    write proceeds. That reproduces exactly the window a check-then-act
    implementation gets wrong, deterministically, every run.

    The hook is gated on the statement being a `Select`: a plain SELECT
    holds no row lock under READ COMMITTED, so session B's nested run
    cannot block on session A. The atomic `UPDATE ... RETURNING`
    implementation never issues a bare SELECT of the counter -- its read
    and write are one statement -- so the hook simply never fires against
    it, gating out the one interleave that would instead wedge session B
    against session A's held row lock (the `asyncio.gather` hang above,
    reproduced deliberately with this same hook when gated on `Update`
    instead of `Select`).
    """
    org_id = await api_env.create_org("sd-num-race")

    async with AsyncSession(admin_engine, expire_on_commit=False) as warmup:
        await next_number(warmup, org_id, "incident")  # creates the counter row
        await warmup.commit()

    injected = False
    b_number: str | None = None

    async with AsyncSession(admin_engine, expire_on_commit=False) as sa:
        original_execute = sa.execute

        async def interleaving_execute(statement, *args, **kwargs):
            nonlocal injected, b_number
            result = await original_execute(statement, *args, **kwargs)
            if not injected and isinstance(statement, Select):
                injected = True
                async with AsyncSession(admin_engine, expire_on_commit=False) as sb:
                    b_number = await next_number(sb, org_id, "incident")
                    await sb.commit()
            return result

        sa.execute = interleaving_execute  # type: ignore[method-assign]
        a_number = await next_number(sa, org_id, "incident")
        await sa.commit()

    if b_number is not None:
        # The hook fired: this implementation reads before it writes.
        # Session B allocated and committed off the same stale read A is
        # about to overwrite with -- prove that duplicates.
        assert a_number != b_number, (
            f"duplicate ticket number allocated: {a_number} == {b_number}"
        )
        assert {a_number, b_number} == {"INC-2", "INC-3"}
        expected_next_incident = 4
    else:
        # The hook never fired: this implementation's read and write are one
        # atomic statement, so there was nothing to interleave B into.
        assert a_number == "INC-2"
        expected_next_incident = 3

    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        counter = (
            await s.execute(
                select(TicketNumberCounter).where(TicketNumberCounter.org_id == org_id)
            )
        ).scalar_one()
        assert counter.next_incident == expected_next_incident


async def test_allocation_issues_no_bare_select_of_the_counter(
    api_env, admin_engine: AsyncEngine
) -> None:
    """Assert the atomicity property STRUCTURALLY, not by racing.

    The interleave test above is necessarily shape-aware: catching a
    read-then-write means injecting between the read and the write, so its
    hook keys on `Select`. That leaves a gap the review flagged — a future
    regression that reads the counter some *other* way (`session.get()`, a
    raw `text()` SELECT, a `with_for_update()` load) would slip past the
    hook and be silently reported as "atomic, nothing to interleave".

    This closes that gap from the other side: whatever shape it takes, a
    correct allocation must never read the counter row as its own statement.
    Any standalone read of `ticket_number_counter` — of any shape — means the
    increment is no longer atomic, and this fails regardless of how the read
    was spelled.
    """
    org_id = await api_env.create_org("sd-num-atomic")

    async with AsyncSession(admin_engine, expire_on_commit=False) as warmup:
        await next_number(warmup, org_id, "incident")
        await warmup.commit()

    seen: list[str] = []

    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        original_execute = s.execute

        async def recording_execute(statement, *args, **kwargs):
            seen.append(str(statement).strip().split()[0].upper())
            return await original_execute(statement, *args, **kwargs)

        s.execute = recording_execute  # type: ignore[method-assign]
        assert await next_number(s, org_id, "incident") == "INC-2"
        await s.commit()

    assert "SELECT" not in seen, (
        "allocation read the counter in its own statement, so the increment is "
        f"no longer atomic: statements were {seen}"
    )
    assert seen == ["INSERT", "UPDATE"], seen
