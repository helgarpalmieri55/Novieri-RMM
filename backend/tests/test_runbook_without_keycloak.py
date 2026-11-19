"""The runbooks after own authentication phase 4 (plan 2026-11-15, Task 9).

Keycloak left the stack, so a runbook that still told an operator to open its
admin console, read its realm import or wait for `keycloak-init` would send
them after something that is not there. What may still name it: the section
that retires its data on a host upgraded from before
(`## Retiring the old identity provider's data`, which `restore.sh`,
`backup.sh` and `deploy.sh` point at; operator decisions K1 and K2,
2026-11-15), the dated table of what was broken on the first boots, and any
line that says it is history by naming own authentication's phase 4, or a
date of 2026-11 or later, in the past tense (`_dated`).

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
DEPLOY_SH = SCRIPTS / "deploy.sh"

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
#: What dates a line as history: own authentication's phase 4 BY NAME -- "own
#: authentication phase 4", "phase 4 of own authentication", "autenticación
#: propia, fase 4", "fase 4 de la autenticación propia" -- or a date of
#: November 2026 or later. A bare "phase 4" is not one: the roadmap has a
#: Phase 4 of its own (Patch, App-deploy), and it dated a Keycloak line beside
#: it (final review of phase 4, C4). Matched against `_sentences`' text, never
#: a single line (Task 11, fix round 1): a phrase wrapped across two lines or
#: two `# ` comment lines reads as the words it is.
DATED = re.compile(
    r"\bown authentication(?: s)? phase 4\b"
    r"|\bphase 4 of own authentication\b"
    r"|\bautenticación propia fase 4\b"
    r"|\bfase 4 de (?:la )?autenticación propia\b"
    r"|\b(?:2026 1[12]|20(?:2[7-9]|[3-9][0-9]) (?:0[1-9]|1[0-2]))(?: [0-3][0-9])?\b"
)
#: The same words in the future tense date nothing: they were written while
#: Keycloak was still here (fix round 1; final review, C4). An "until" before
#: them in their sentence, however far ("until it is removed in ... phase 4"),
#: unless another phase is named between the two ("until own authentication
#: phase 1 ... went in own authentication phase 4": that "until" is phase
#: 1's) -- so a past sentence that says "until ... phase 4" is refused as
#: well, and says "before ... phase 4 removed it" instead -- and "phase 4
#: will", "phase 4 removes".
UNTIL = re.compile(r"\b(?:until|till|hasta)\b")
OTHER_PHASE = re.compile(r"\b(?:phase|fase) (?!4\b)[0-9]+\b")
FUTURE = re.compile(
    r"(?: [0-9]{4} [0-9]{2} [0-9]{2})? (?:will|shall|is going to|removes|retires|drops"
    r"|deletes|quita|quitará|retira|retirará)\b"
)
#: Where a sentence ends: `.`, `;`, `!` or `?` before a space or the end, so
#: `deploy.sh` and `3.6` end nothing.
SENTENCE_END = re.compile(r"[.;!?](?=\s|$)")


def _sentences(lines: list[str]) -> list[str]:
    """Lines read as text, the way `DATED` reads them: joined, cut into
    sentences, lower-cased, and every run of spaces, punctuation and markup
    made one space -- so "Until phase 4", a sentence wrapped across two lines
    or two `# ` comment lines, and "*phase 4*" all read as the words they
    are, and an "until" in one sentence does not reach the next."""
    return [
        re.sub(r"\W+", " ", sentence).lower() for sentence in SENTENCE_END.split(" ".join(lines))
    ]


def _until(before: str) -> bool:
    """`before` ends in an "until" that is still open: no other phase named
    after it."""
    untils = list(UNTIL.finditer(before))
    return bool(untils) and OTHER_PHASE.search(before, untils[-1].end()) is None


def _history(sentence: str) -> bool:
    """A dating phrase in `sentence` that is not in the future tense."""
    return any(
        not _until(sentence[: found.start()]) and FUTURE.match(sentence, found.end()) is None
        for found in DATED.finditer(sentence)
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
    """History says when it stopped being true: own authentication's phase 4,
    or a date of 2026-11 or later (`DATED`), on the line, on one of the three
    before it (a command under its comment) or on the next one (a sentence
    wrapped across the two). Not in the future tense (`UNTIL`, `FUTURE`), on
    one line or across two."""
    return any(map(_history, _sentences(lines[max(0, index - 3) : index + 2])))


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
    assert _dated(["integrated with Keycloak before own", "authentication phase 4 removed it."], 0)
    assert not _dated(["# phase 4", "a", "b", "c", "pg_restore -d keycloak", "d"], 4)
    # The future tense is not history (fix round 1).
    assert not _dated(["Keycloak answers /realms until phase 4."], 0)
    assert not _dated(["Keycloak, until own authentication phase 4 removes it"], 0)
    assert _dated(["removed in own authentication phase 4 (2026-11-15)"], 0)
    # Nor wrapped, capitalised or split across comment lines (Task 11 fix
    # round 1); and "until" is refused in the past tense too.
    assert not _dated(["integrated with Keycloak until own", "authentication phase 4."], 0)
    assert not _dated(["Keycloak signs people in until", "phase 4."], 0)
    assert not _dated(["Until phase 4, Keycloak signs people in."], 0)
    assert not _dated(["# Keycloak answers until own authentication", "# phase 4."], 0)
    assert _dated(["Keycloak left the stack in", "Phase 4 (2026-11-15)."], 0)


def test_only_own_authentications_phase_4_or_a_date_dates_a_line() -> None:
    """Final review of phase 4, C4: the rule could not hear tense, and any
    "phase 4" dated a line. The reviewer's probes, each passed as history:"""
    # The present tense of a plan.
    assert not _dated(["The console signs people in through Keycloak; phase 4 removes it."], 0)
    assert not _dated(["Keycloak signs people in; own authentication phase 4 removes it."], 0)
    assert not _dated(["Own authentication phase 4 will remove Keycloak."], 0)
    # The roadmap's own Phase 4 (Patch, App-deploy) beside a Keycloak line.
    roadmap = ["## Phase 4 — Patch, App-deploy", "", "Auth: Keycloak as identity broker."]
    assert not _dated(roadmap, 2)
    # "until", however far before.
    assert not _dated(["Keycloak signs people in until it is removed in phase 4."], 0)
    assert not _dated(["Keycloak stays until it is removed in own authentication phase 4."], 0)
    assert not _dated(["Keycloak stays until 2026-11-15."], 0)
    # A date from before November 2026 is not one of phase 4.
    assert not _dated(["Keycloak's realm was imported on 2026-10-30."], 0)
    # What does date it: own authentication's phase 4 by name, past tense,
    # in either language and either order, or a date of 2026-11 or later.
    assert _dated(["Keycloak was removed in phase 4 of own authentication."], 0)
    assert _dated(["Keycloak se retiró en autenticación propia, fase 4."], 0)
    assert _dated(["Keycloak se retiró en la fase 4 de la autenticación propia."], 0)
    assert _dated(["Keycloak left the stack (2026-11-15)."], 0)
    assert _dated(["Keycloak left the stack in own authentication's phase 4."], 0)
    # And the sentence is the unit: an "until" in the sentence before does
    # not reach it, nor one that another phase closed.
    retire = ["Keep them until you retire them.", "Own authentication phase 4", "removed Keycloak."]
    assert _dated(retire, 1)
    phase_1 = "until own authentication phase 1 the subject Keycloak issued -- Keycloak went"
    assert _dated([phase_1, "in own authentication phase 4, and rows from before"], 0)
    assert not _dated(["Keycloak stays until own authentication phase 4 (2026-11-15)."], 0)


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
        # Non-vacuity (fix round 1): the README is where whole sections went,
        # and a link pattern that stopped matching would pass every file.
        # GO-LIVE.md has no in-page link today; one added later must land.
        if path == README:
            assert links, "no in-page link found in the README: the check is vacuous"
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


def _expand(pattern: str) -> set[str]:
    """Every string an alternation of literals matches: `A(B|C)D` gives
    `{"ABD", "ACD"}`. Anything else -- a class, a quantifier -- is refused, so
    a pattern that stops being a finite list of names fails here."""

    def alternatives(i: int) -> tuple[set[str], int]:
        options: list[set[str]] = []
        current = {""}
        while i < len(pattern) and pattern[i] != ")":
            char = pattern[i]
            if char == "|":
                options.append(current)
                current, i = {""}, i + 1
            elif char == "(":
                inner, i = alternatives(i + 1)
                assert pattern[i : i + 1] == ")", pattern
                current, i = {a + b for a in current for b in inner}, i + 1
            else:
                assert re.fullmatch(r"[A-Z0-9_]", char), f"not a literal name: {char!r}"
                current, i = {a + char for a in current}, i + 1
        options.append(current)
        return set().union(*options), i

    names, end = alternatives(0)
    assert end == len(pattern), pattern
    return names


def _shell_array(name: str) -> set[str]:
    match = re.search(rf"^{name}=\((.*?)\)", DEPLOY_SH.read_text(), re.DOTALL | re.MULTILINE)
    assert match, name
    return set(match.group(1).split())


def test_the_retiring_step_deletes_exactly_the_keys_deploy_sh_names() -> None:
    """Parts 4 and 5 delete `.env` keys by name, and `deploy.sh` names the same
    keys on every run (`OBSOLETE_ENV_KEYS`, of which `ROLLBACK_ENV_KEYS` are
    the four a rollback needs). Pinned both ways, so neither list can grow or
    shrink without the other: a key deploy.sh names and the step leaves keeps
    being named for ever, and a key the step deletes that deploy.sh does not
    name is a line of `.env` deleted that nobody said was obsolete (fix round
    1)."""
    body = _section(RETIRING)
    seds = [_expand(m) for m in re.findall(r"sed -i -E '/\^\((.+?)\)=/d' \.env", body)]
    greps = [_expand(m) for m in re.findall(r"grep -cE '\^\((.+?)\)=' \.env", body)]
    assert len(seds) == 2 and len(greps) == 2, (len(seds), len(greps))
    obsolete, rollback = _shell_array("OBSOLETE_ENV_KEYS"), _shell_array("ROLLBACK_ENV_KEYS")
    assert len(rollback) == 4 and rollback < obsolete, (sorted(rollback), sorted(obsolete))
    assert seds[0] == rollback, sorted(seds[0] ^ rollback)
    assert seds[1] == obsolete - rollback, sorted(seds[1] ^ (obsolete - rollback))
    assert greps[0] == rollback, sorted(greps[0] ^ rollback)
    assert greps[1] == obsolete, sorted(greps[1] ^ obsolete)
    assert "ITS_DEFAULT_ORG_SLUG" not in seds[0] | seds[1]
    # The expander itself, so the pin cannot pass by reading nothing.
    assert _expand("A(B|C)D|E") == {"ABD", "ACD", "E"}


def _logical_commands(block: str) -> list[str]:
    """A shell block's commands as bash reads them: a line ending in `\\`
    goes on into the next, and comment and blank lines are not commands."""
    commands: list[str] = []
    pending = ""
    for raw in block.splitlines():
        line = raw.strip()
        if not pending and (not line or line.startswith("#")):
            continue
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        commands.append(pending + line)
        pending = ""
    if pending:
        commands.append(pending)
    return commands


PROMPT = re.compile(r"(?:^|&&|;|\|\|)\s*read -r")


def test_a_prompt_is_the_last_command_of_its_block() -> None:
    """A `read` that asks for a token reads the terminal, and when a terminal
    pastes a block without bracketed paste, the terminal holds the rest of
    the block: the line after the `read` becomes the token, and is written to
    `.env` or handed to the agent (fix round 1). So the command a prompt is in
    ends its block, and what uses the value is in that same command."""
    found = []
    for path in (README, GO_LIVE, BACKUP_DOC):
        for block in re.findall(r"```sh\n(.*?)```", path.read_text(), re.DOTALL):
            commands = _logical_commands(block)
            for index, command in enumerate(commands):
                if PROMPT.search(command) and index != len(commands) - 1:
                    found.append(f"{path.name}: {command[:80]}")
    assert found == [], found
    # Non-vacuity: the prompts exist, and a prompt followed by a command is
    # seen as two commands.
    prompts = re.findall(r"(?m)^\s*(?:stty -echo; )?read -r", README.read_text())
    assert len(prompts) >= 7, len(prompts)
    assert _logical_commands("read -rsp 'x: ' T && \\\n  use \"$T\"\n# c\nnext\n") == [
        "read -rsp 'x: ' T &&  use \"$T\"",
        "next",
    ]
