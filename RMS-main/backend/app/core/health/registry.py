"""Runs probes concurrently, under a per-probe timeout and a total budget.

FAIL-CLOSED, TWICE OVER. A probe that raises becomes `DOWN`/`UNREACHABLE` --
its exception never propagates to the endpoint and its text never reaches the
result. A probe that hangs is cancelled at `probe_timeout_s` and becomes
`DOWN`/`TIMEOUT`. Neither can take the status endpoint down with the
dependency it exists to describe.
"""

import asyncio
import logging
import time
from typing import Any

from app.core.health.types import Probe, ProbeResult, ReasonCode, Status

logger = logging.getLogger(__name__)

PROBE_TIMEOUT_S = 2.0
TOTAL_BUDGET_S = 5.0
CACHE_TTL_S = 5.0


class ProbeRegistry:
    def __init__(
        self,
        *,
        probe_timeout_s: float = PROBE_TIMEOUT_S,
        total_budget_s: float = TOTAL_BUDGET_S,
        cache_ttl_s: float = CACHE_TTL_S,
    ) -> None:
        self._probes: dict[str, Probe] = {}
        self._hard: set[str] = set()
        self._probe_timeout_s = probe_timeout_s
        self._total_budget_s = total_budget_s
        self._cache_ttl_s = cache_ttl_s
        self._cached: list[ProbeResult] | None = None
        self._cached_at = float("-inf")

    def register(self, name: str, fn: Probe, *, hard: bool = False) -> None:
        self._probes[name] = fn
        if hard:
            self._hard.add(name)

    @property
    def hard(self) -> frozenset[str]:
        return frozenset(self._hard)

    async def _run_one(self, name: str, fn: Probe, state: Any) -> ProbeResult:
        started = time.monotonic()

        def elapsed_ms() -> int:
            return int((time.monotonic() - started) * 1000)

        try:
            return await asyncio.wait_for(fn(state), timeout=self._probe_timeout_s)
        except TimeoutError:
            return ProbeResult(name, Status.DOWN, elapsed_ms(), ReasonCode.TIMEOUT)
        except Exception:
            # exc_info only: the logging kernel renders `exc_type` and never the
            # exception's text. The RESULT carries a code, never the text.
            logger.warning("health probe failed", extra={"probe": name}, exc_info=True)
            return ProbeResult(name, Status.DOWN, elapsed_ms(), ReasonCode.UNREACHABLE)

    async def run(self, state: Any) -> list[ProbeResult]:
        now = time.monotonic()
        if self._cached is not None and now - self._cached_at < self._cache_ttl_s:
            return self._cached
        tasks = [self._run_one(name, fn, state) for name, fn in self._probes.items()]
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*tasks), timeout=self._total_budget_s
            )
        except TimeoutError:
            # The whole set blew the budget. Report it as data rather than
            # raising: an endpoint that 500s is less useful than one that says
            # which probes it could not finish.
            results = [
                ProbeResult(name, Status.DOWN, int(self._total_budget_s * 1000), ReasonCode.TIMEOUT)
                for name in self._probes
            ]
        self._cached = list(results)
        self._cached_at = now
        return self._cached
