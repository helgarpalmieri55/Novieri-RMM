"""The suite's Redis: `ITS_TEST_REDIS_URL`, or database 9 of the local server.

Every test that needs a Redis takes it from here, never from a literal
(design 2026-11-23-vps-y-marca-novieri §6). Several of them empty the
database they are given (`flushdb()`), so two suites at once -- two
worktrees, two CI jobs on one runner -- must be handed different ones, and
only a value read in one place can be.
"""

import os

TEST_REDIS_URL = os.environ.get("ITS_TEST_REDIS_URL", "redis://localhost:6379/9")
