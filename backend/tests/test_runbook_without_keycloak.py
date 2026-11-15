"""The runbooks after own authentication phase 4 (plan 2026-11-15, Task 9).

Keycloak left the stack, so a runbook that still told an operator to open its
admin console, read its realm import or wait for `keycloak-init` would send
them after something that is not there. What may still name it: the section
that retires its data on a host upgraded from before
(`## Retiring the old identity provider's data`, which `restore.sh`,
`backup.sh` and `deploy.sh` point at; operator decisions K1 and K2,
2026-11-15), the dated table of what was broken on the first boots, and any
line that says it is history by naming phase 4.

And because whole sections went, every in-page link must still land on a
heading: a deleted section is otherwise a link that scrolls nowhere.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
README = REPO / "deploy" / "README.md"
GO_LIVE = REPO / "deploy" / "GO-LIVE.md"
BACKUP_DOC = REPO / "docs" / "07-backup-restore.md"
SCRIPTS = REPO / "deploy" / "scripts"
RESTORE_SH = SCRIPTS / "restore.sh"

# The names case-blind; the `.env` keys as keys, so the audit action
# `auth.entra_credential_used` (Microsoft sign-in, phase 2) is not one.
NAMES = re.compile(r"(?i:keycloak|kcadm|realm-it-suite)|KC_HOSTNAME|ITS_OIDC_|\bENTRA_[A-Z]")
RETIRING = "## Retiring the old identity provider's data"
ALLOWED_SECTIONS = (RETIRING, "## What was broken, and how you would recognise it")
GONE_HEADINGS = (
    "## Break-glass: the identity admin console",
    "## Branding and extending sign-in",
    "## Forwarding Keycloak's credential events",
    "## Keycloak SMTP",
    "## The provisioning client",
    "### What keycloak-init still converges",
)


def _sections(text: str) -> list[tuple[str, str]]:
    """`(heading, body)` for every `## ` section, outside code fences; the
    text before the first one is `("", ...)`."""
    sections: list[tuple[str, list[str]]] = [("", [])]
    fenced = False
    for line in text.splitlines():
        if line.startswith("```"):
            fenced = not fenced
        if not fenced and line.startswith("## "):
            sections.append((line, []))
        sections[-1][1].append(line)
    return [(heading, "\n".join(lines)) for heading, lines in sections]


def _dated(lines: list[str], index: int) -> bool:
    """History says when it stopped being true: "phase 4" on the line, on one
    of the three before it (a command under its comment) or on the next one
    (a sentence wrapped across the two)."""
    return any("phase 4" in line for line in lines[max(0, index - 3) : index + 2])


def _offenders(path: Path, allowed: tuple[str, ...] = ()) -> list[str]:
    found = []
    for heading, body in _sections(path.read_text()):
        if allowed and heading.startswith(allowed):
            continue
        lines = body.splitlines()
        for index, line in enumerate(lines):
            if NAMES.search(line) and not _dated(lines, index):
                found.append(f"{path.name} [{heading or 'top'}]: {line.strip()[:100]}")
    return found


def test_the_guard_finds_a_stray_name() -> None:
    """Non-vacuity: a line naming the old provider outside the allowed
    sections, and not dated, is found; a dated one is not."""
    assert NAMES.search("run `docker compose up keycloak-init` again")
    assert NAMES.search("revoke ENTRA_CLIENT_SECRET") and NAMES.search("ITS_OIDC_ISSUER")
    assert not NAMES.search("the platform's own sign-in")
    assert not NAMES.search("(`auth.entra_credential_used`)")
    sections = _sections("intro\n## A\nkeycloak here\n```\n## not a heading\n```\n## B\nok\n")
    assert [h for h, _ in sections] == ["", "## A", "## B"]
    lines = ["# Only before own authentication", "# phase 4:", "x", "pg_restore -d keycloak"]
    assert _dated(lines, 3)
    assert _dated(["integrated with Keycloak until own", "authentication phase 4."], 0)
    assert not _dated(["# phase 4", "a", "b", "c", "pg_restore -d keycloak", "d"], 4)


def test_the_runbook_names_the_old_provider_only_where_it_retires_it() -> None:
    assert _offenders(README, ALLOWED_SECTIONS) == []


def test_go_live_and_the_backup_document_name_it_only_as_history() -> None:
    assert _offenders(GO_LIVE) + _offenders(BACKUP_DOC) == []


def test_the_sections_about_the_old_provider_are_gone() -> None:
    text = README.read_text()
    assert [h for h in GONE_HEADINGS if h in text] == []
    # What the SMTP section held that was not Keycloak's keeps a home.
    assert "## Notification email channels over Microsoft 365" in text


def test_the_retiring_section_exists_where_the_scripts_point() -> None:
    assert RETIRING in README.read_text()
    for script in ("restore.sh", "backup.sh", "deploy.sh"):
        assert "Retiring the old identity provider's data" in (SCRIPTS / script).read_text(), script


def _section(heading: str) -> str:
    return next(body for h, body in _sections(README.read_text()) if h.startswith(heading))


def test_the_retiring_step_is_the_operators_decision_entire() -> None:
    """Operator decisions K1 and K2 (2026-11-15): one step, run when the
    operator chooses, that drops the database, deletes the three secret
    files and the four keys a rollback needs, then the other keys, says it
    closes the way back, and sends them to revoke the old Entra secret --
    with commands that need nothing filled in (CLAUDE.md)."""
    body = _section(RETIRING)
    flat = " ".join(body.split())
    for needed in (
        "dropdb -U its_admin keycloak",
        "keycloak-admin.secret",
        "keycloak-events.secret",
        "keycloak-bootstrap-admin.secret",
        "KEYCLOAK_ADMIN_USER|KEYCLOAK_ADMIN_PASSWORD|ITS_OIDC_ISSUER|ITS_OIDC_JWKS_URL",
        "closes the way back",
        "ENTRA_CLIENT_SECRET",
        "Do not delete",
    ):
        assert needed in flat, needed
    # Said before anything is deleted, not only after (re-review, m8).
    assert flat.index("closes the way back") < flat.index("dropdb")
    assert "one release later" not in flat
    code = "\n".join(re.findall(r"```sh\n(.*?)```", body, re.DOTALL))
    assert code and not re.search(r"<[a-z][a-z -]*>", code), "a placeholder in a command"


def test_the_first_upgrade_past_phase_4_pulls_first() -> None:
    """The release before phase 4 runs its own deploy.sh, which pulls part-way
    through and goes on running the old text (coordinator's decision,
    2026-11-15; review 1, I5)."""
    body = _section("## Upgrading an existing deployment")
    assert "git -C .. pull --ff-only && ./scripts/deploy.sh" in body
    assert "--remove-orphans" not in body


def test_the_status_keeps_what_an_old_host_still_needs() -> None:
    """An upgrade from before phase 1 crosses all four phases at once, so
    the facts phases 1 and 3 changed stay (review 1, I3); and nothing tells
    the operator to delete the keys a rollback needs (I4)."""
    status = " ".join(_section("## Status of this runbook").split())
    assert "no path to one" in status
    assert "stops working (401)" in status
    assert "harmless" not in status.lower()
    text = README.read_text()
    assert "delete them by hand" not in text and "remove them by hand" not in text


def _slug(heading: str) -> str:
    """GitHub's anchor for a heading: lower case, punctuation but `-` and `_`
    dropped, spaces to hyphens."""
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def test_every_in_page_link_lands_on_a_heading() -> None:
    for path in (README, GO_LIVE):
        text = path.read_text()
        anchors: set[str] = set()
        seen: dict[str, int] = {}
        fenced = False
        for line in text.splitlines():
            if line.startswith("```"):
                fenced = not fenced
                continue
            match = None if fenced else re.match(r"^#{1,6} (.+)$", line)
            if match:
                base = _slug(match.group(1))
                count = seen.get(base, 0)
                seen[base] = count + 1
                anchors.add(base if count == 0 else f"{base}-{count}")
        links = set(re.findall(r"\]\(#([^)]+)\)", text))
        assert links - anchors == set(), (path.name, sorted(links - anchors))


def test_a_removal_by_hand_finds_the_containers_by_their_compose_labels() -> None:
    """Task 6's fix round: deploy.sh finds the old identity provider's
    containers by the labels Compose puts on every container it makes, this
    project's `com.docker.compose.project` and the service's
    `com.docker.compose.service`, never by `compose ps`, whose listing of a
    container whose service left the file is Compose's choice to make. A
    command the runbook gives to remove them by hand asks the same question
    the same way, or it can answer "none left" while Traefik still routes to
    one."""
    blocks = re.findall(r"```sh\n(.*?)```", README.read_text(), re.DOTALL)
    removals = [b for b in blocks if "docker rm" in b and "keycloak-init" in b]
    # The manual upgrade sequence and the retiring step.
    assert len(removals) == 2, len(removals)
    for block in removals:
        assert "awk '/^name: / { print $2; exit }'" in block, block
        assert re.search(r"label=com\.docker\.compose\.project=\$\{?project\b", block), block
        assert "label=com.docker.compose.service=$service" in block, block
        assert not re.search(r"compose ps[^\n]*\n?[^\n]*docker rm", block), block
