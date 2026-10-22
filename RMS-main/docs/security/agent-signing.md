# Agent release signing boundary

Run `release-agent` manually from `main`, supplying a strict semantic version.
The exact commit must have a successful latest push run of `ci.yml`. A tag push
no longer starts signing. The output remains a draft release for operator review.

The `agent-release` environment must allow only the **branch** `main` (no tags).
Main requires all CI checks and a reviewed PR through the managed repository
ruleset. This is the approval boundary: private repositories on GitHub Team do
not support environment reviewer gates. Do not claim the environment provides
an additional manual approval. Do not add a signing key at repository/org scope.

Tests: `node --test tests/ci/release-gate.test.cjs`. They reject branch/tag runs,
unrelated commits, failed/missing CI, and pending or failed newer runs. CI runs
these tests in the frontend job. No real signing key is used by these tests.

Operational settings applied on 2026-09-25: main-only environment branch policy
and required-check/review ruleset. Workflow hardening takes effect when this PR
merges. Rollback must retain the environment restriction; restore a prior vetted
workflow through a reviewed PR, never grant arbitrary branches signing access.
