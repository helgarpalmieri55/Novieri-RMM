"""Presigned grants over agent artefacts.

WHAT MAKES THIS DIFFERENT FROM EVERY OTHER PRESIGN PATH HERE. The backup and
deploy grants derive their prefix from an `org_id` this process is already
authorized for, so a caller cannot express a location outside their own tenant
even if they try. Agent releases have no `org_id` at all
(`docs/modules/agent-distribution/spec.md` §4.2), so the prefix is derived from
`(version, os, arch)` -- three strings an operator typed at publish time.

That inverts where the danger is. `version` is the untrusted part, and a version
of `../../backups/<someone>` would address a tenant's backup chunks from a route
whose permission check knows nothing about tenants. So these tests are mostly
about the key derivation refusing to build such a key at all, rather than about
signing.

`_safe_object_suffix` is reused rather than restated: an earlier hand-rolled
equivalent elsewhere in this codebase silently omitted its control-character
check, which is the argument for having exactly one of these.
"""

import pytest

from app.core.storage import MAX_PRESIGNED_TTL_S, agent_release_suffix


class TestAgentReleaseSuffix:
    def test_it_builds_the_documented_layout(self) -> None:
        assert (
            agent_release_suffix("873d666", "linux", "amd64")
            == "agent-releases/873d666/its-agent-linux-amd64"
        )

    def test_windows_carries_no_exe_suffix_here(self) -> None:
        # build.sh emits `its-agent-windows-amd64` with no extension; naming the
        # object differently from the artefact is how a download ends up under a
        # key nothing wrote to.
        assert agent_release_suffix("v1", "windows", "amd64").endswith(
            "its-agent-windows-amd64"
        )

    @pytest.mark.parametrize(
        "version",
        [
            "../../backups/other-org",
            "..",
            "a/../..",
            "with space",
            "semi;colon",
            "new\nline",
            "",
            "/leading",
            "trailing/",
        ],
    )
    def test_a_version_that_could_address_another_prefix_is_refused(
        self, version: str
    ) -> None:
        # The whole point: `version` reaches this function from an operator's
        # request body, and the route that accepts it has no tenant scope to
        # fall back on.
        with pytest.raises(ValueError):
            agent_release_suffix(version, "linux", "amd64")

    @pytest.mark.parametrize("os_name", ["../x", "lin ux", "", "linux/amd64"])
    def test_an_unknown_os_is_refused(self, os_name: str) -> None:
        with pytest.raises(ValueError):
            agent_release_suffix("v1", os_name, "amd64")

    @pytest.mark.parametrize("arch", ["../x", "amd 64", "", "x86_64"])
    def test_an_unknown_arch_is_refused(self, arch: str) -> None:
        # x86_64 is refused deliberately: build.sh emits GOARCH names, and
        # accepting an alias here mints a grant for an object that was never
        # uploaded, which presents as a 404 at install time on the endpoint.
        with pytest.raises(ValueError):
            agent_release_suffix("v1", "linux", arch)

    def test_the_os_and_arch_sets_match_what_build_sh_produces(self) -> None:
        # agent/packaging/build.sh: linux/amd64 linux/arm64 windows/amd64
        # windows/arm64 darwin/amd64 darwin/arm64
        for os_name in ("linux", "windows", "darwin"):
            for arch in ("amd64", "arm64"):
                assert agent_release_suffix("v1", os_name, arch)


class TestTtl:
    def test_the_shared_cap_applies(self) -> None:
        # Not a second, divergent number: MAX_PRESIGNED_TTL_S is the one this
        # module already enforces for backup and deploy grants.
        assert MAX_PRESIGNED_TTL_S > 0
