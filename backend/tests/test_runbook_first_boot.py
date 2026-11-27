"""The runbook for a new VPS (design 2026-11-23, §3): what the operator
pastes on a host this repository cannot see.

**WHAT THESE PIN.** `CLAUDE.md`, "Reporting to the operator": a
copy-pasteable command contains no placeholders (the `<sha printed>` that
ended up in `.env` on 2026-09-11); every sequence after the first boot starts
with `docker compose exec openbao bao status`, because a sealed OpenBao makes
`up -d` block; and the design's §3: the `:443` certificate comes from ACME and
nothing is copied into `deploy/certs/`, and no runnable block sends
`Authorization: Bearer`, which the console's API has not read since own
authentication phase 1.

**SINCE 2026-11-27** (design 2026-11-27-despliegue-automatizado §6) the first
boot is three scripts and the moments between them, and the blocks the scripts
run are the appendix, "What each script does, by hand". What the runbook said
of a command, it still says, in the appendix; and the scripts are held to it:
the commands a step's block runs are the commands its script runs, the lines
the runbook quotes are lines the scripts print.

**WHAT THEY DO NOT CHECK.** That a command works: the rehearsal does (plan
2026-11-23, Task 12), and so does CI's `deploy` job for the commands it runs.
These read the text an operator reads.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
README = REPO / "deploy" / "README.md"
GO_LIVE = REPO / "deploy" / "GO-LIVE.md"
BACKUP_DOC = REPO / "docs" / "07-backup-restore.md"
RUNBOOKS = (README, GO_LIVE, BACKUP_DOC)

#: What reads as "put your value here" in a block that is pasted as it is.
PLACEHOLDER = re.compile(
    r"<[a-z][a-z0-9 _-]*>"  # <sha printed>, <host>
    r"|your-host|example\.(?:com|org|net)"
    r"|…"
    r"|\bCHANGE[-_]?ME\b|\bXXXX?\b"
)


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    end = text.find("\n## ", start + 1)
    return text[start:] if end == -1 else text[start:end]


def _sh_blocks(text: str) -> list[str]:
    return re.findall(r"```sh\n(.*?)```", text, re.DOTALL)


def _commands(block: str) -> list[str]:
    """A block's commands: continuation lines joined, comments and blank
    lines dropped."""
    found: list[str] = []
    pending = ""
    for raw in block.splitlines():
        line = raw.strip()
        if not pending and (not line or line.startswith("#")):
            continue
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        found.append(pending + line)
        pending = ""
    return found


PREREQUISITES = "## Prerequisites: a new VPS (Ubuntu Server 24.04 LTS)\n"

#: GitHub's published Ed25519 host key (docs.github.com, "GitHub's SSH key
#: fingerprints": SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU).
GITHUB_ED25519 = (
    "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
)


SCRIPTS = REPO / "deploy" / "scripts"
APPENDIX = "## Appendix: what each script does, by hand\n"


def _first_boot() -> str:
    return _section(README.read_text(), "## First boot\n")


def _appendix() -> str:
    return _section(README.read_text(), APPENDIX)


def _part(heading: str) -> str:
    """One script's part of the appendix: from its ### heading to the next."""
    text = _appendix()
    start = text.index(heading)
    end = text.find("\n### ", start + 1)
    return text[start:] if end == -1 else text[start:end]


def _code(script: str) -> str:
    """A deploy script without its comment lines."""
    lines = (SCRIPTS / script).read_text().splitlines()
    return "\n".join(line for line in lines if not line.lstrip().startswith("#"))


def _after_reboot() -> str:
    return _section(README.read_text(), "## After a reboot or a restart\n")


def _vps_upgrade_blocks() -> list[str]:
    """The upgrade on this VPS: `bao status` alone, then -- once it says
    `Sealed false` -- the pull and deploy.sh."""
    text = _section(README.read_text(), "## Upgrading an existing deployment\n")
    marker = "**On the VPS this runbook builds**"
    assert marker in text, "the upgrade section names the VPS sequence"
    return _sh_blocks(text[text.index(marker) :])[:2]


def _existing_host_blocks() -> list[str]:
    """The two blocks that add the console's host and the ACME address to the
    `.env` of an existing host, which sit above the VPS marker."""
    text = _section(README.read_text(), "## Upgrading an existing deployment\n")
    start = text.index("**Any other existing host**")
    return _sh_blocks(text[start : text.index("**On the VPS this runbook builds**")])


OFF_HOST = "### The off-host copy, step by step\n"


def _off_host() -> str:
    text = README.read_text()
    start = text.index(OFF_HOST)
    end = text.find("\n## ", start)
    nxt = text.find("\n### ", start + 1)
    stops = [i for i in (end, nxt) if i != -1]
    return text[start : min(stops)] if stops else text[start:]


def test_the_first_boot_the_reboot_and_the_upgrade_have_no_placeholders() -> None:
    blocks = [
        *_sh_blocks(_section(README.read_text(), PREREQUISITES)),
        *_sh_blocks(_first_boot()),
        *_sh_blocks(_appendix()),
        *_sh_blocks(_after_reboot()),
        *_existing_host_blocks(),
        *_vps_upgrade_blocks(),
        *_sh_blocks(_off_host()),
    ]
    assert len(blocks) >= 20, len(blocks)
    assert len(_existing_host_blocks()) == 2
    found = [
        command for block in blocks for command in _commands(block) if PLACEHOLDER.search(command)
    ]
    assert found == [], found
    # Non-vacuity: the pattern sees the form that shipped on 2026-09-11.
    assert PLACEHOLDER.search('echo "IT_SUITE_VERSION=<sha printed>" >> .env')


def test_every_sequence_after_first_boot_starts_by_asking_openbao() -> None:
    """And asks ALONE: a pasted block goes on after `Sealed true`, so nothing
    may follow the question in its block."""
    status, upgrade = _vps_upgrade_blocks()
    for name, block in (
        ("after a reboot", _sh_blocks(_after_reboot())[0]),
        ("the VPS upgrade", status),
    ):
        commands = [c for c in _commands(block) if not c.startswith("cd ")]
        assert commands == ["docker compose exec openbao bao status"], (name, commands)
    assert _commands(upgrade) == [
        "git -C .. pull --ff-only",
        "./scripts/deploy.sh --plan",
        "./scripts/deploy.sh",
    ]


def test_after_a_reboot_openbao_unseals_itself_and_the_key_file_is_the_way_back() -> None:
    """Design 2026-11-27 §4: `bao status` must say `Sealed false` alone; with
    `Sealed true` the key file is missing or wrong, and the shares -- recovery
    keys now -- cannot unseal. Only a host still on Shamir is told to use them,
    in prose, never in a block pasted after a reboot."""
    commands = [c for block in _sh_blocks(_after_reboot()) for c in _commands(block)]
    assert "docker compose exec openbao bao operator unseal" not in commands
    assert "`docker compose exec openbao bao operator unseal`" in _after_reboot()
    assert "docker compose up -d" in commands
    assert "./scripts/check-public-tls.sh" in commands
    assert "sudo ./scripts/openbao-seal.sh status" in commands
    restore = "sudo install -D -m 400 ~/unseal.key /etc/it-suite/openbao-unseal/unseal.key"
    assert any(c.startswith(restore) for c in commands)
    assert commands.index("docker compose restart openbao") > commands.index(
        "sudo ./scripts/openbao-seal.sh status"
    )
    assert "### If `bao status` says `Sealed true`" in _after_reboot()


def test_the_first_boot_is_three_scripts() -> None:
    """Design §1 and §8, criterion 1: `prepare-host.sh` in the prerequisites,
    then `first-boot.sh` and `first-boot-check.sh`, each alone in its block,
    and nothing else to paste in First boot itself."""
    blocks = _sh_blocks(_section(README.read_text(), PREREQUISITES))
    prereq = [c for b in blocks for c in _commands(b)]
    assert "sudo ./scripts/prepare-host.sh" in prereq
    assert [_commands(b) for b in _sh_blocks(_first_boot())] == [
        ["./scripts/first-boot.sh"],
        ["./scripts/first-boot-check.sh"],
    ]
    for script in ("prepare-host.sh", "first-boot.sh", "first-boot-check.sh"):
        assert (SCRIPTS / script).stat().st_mode & 0o111, f"{script} is not executable"


def test_the_first_boot_runs_the_scripts_that_check_it() -> None:
    """The commands of steps 1 to 7, in the appendix's first-boot.sh part, in
    their order, with the auto-unseal between step 3.6 and step 4; and
    first-boot.sh runs each of them, in that order."""
    part = _part("### `first-boot.sh`: steps 1 to 7, with the auto-unseal after 3.6\n")
    commands = [c for block in _sh_blocks(part) for c in _commands(block)]
    for expected in (
        "./scripts/new-env.sh --host rmm.novieri.com",
        "./scripts/build-images.sh",
        "./scripts/preflight.sh",
        "docker compose up -d",
        "./scripts/check-public-tls.sh",
        "docker compose exec traefik stat -c %a /etc/traefik/acme/acme.json",
    ):
        assert expected in commands, expected
    order = [
        commands.index(c)
        for c in (
            "./scripts/new-env.sh --host rmm.novieri.com",
            "./scripts/preflight.sh",
            "docker compose up -d",
            "./scripts/check-public-tls.sh",
        )
    ]
    assert order == sorted(order), order
    # The tag goes into .env by a command that computes it and deletes any
    # earlier line first (CLAUDE.md: a second IT_SUITE_VERSION= silently wins).
    tag = [c for c in commands if "IT_SUITE_VERSION=" in c]
    assert len(tag) == 1 and "sed -i '/^IT_SUITE_VERSION=/d' .env" in tag[0], tag
    code = _code("first-boot.sh")
    assert tag[0] in code, "first-boot.sh writes the tag as step 1b does"
    in_script = [
        code.index(c)
        for c in (
            './scripts/new-env.sh "${args[@]}"',
            "./scripts/build-images.sh </dev/null",
            "./scripts/preflight.sh </dev/null",
            "  dc up -d || die",
            './scripts/check-public-tls.sh "${tls_args[@]}" </dev/null',
            "dc exec -T traefik stat -c %a /etc/traefik/acme/acme.json",
        )
    ]
    assert in_script == sorted(in_script), in_script
    # The auto-unseal right after step 3.6, before step 4 starts anything
    # (coordinator's ruling Q6, 2026-11-27): a failure from step 4 on is run
    # again with no share. In the appendix, and in the script's own order.
    seal = (
        "sudo env -u COMPOSE_FILE ./scripts/openbao-seal.sh enable "
        "--key-file /etc/it-suite/openbao-unseal/unseal.key"
    )
    around = [
        commands.index(c)
        for c in ("docker compose up openbao-init", seal, "docker compose restart openbao",
                  "docker compose up migrate")
    ]  # fmt: skip
    assert around == sorted(around), around
    calls = [line.strip() for line in code.splitlines()]
    main = calls[calls.index("check_state_before_anything") :]
    assert main.index("step_3_6") < main.index("auto_unseal") < main.index("restart_proof")
    assert main.index("restart_proof") < main.index("step_4")
    # Step 4b's question, by hand, is the script's, and so is the line quoted
    # for its answer: what only postgres/grant-app-privileges.sql leaves.
    source = (SCRIPTS / "first-boot.sh").read_text()
    query = "".join(re.findall(r'^narrowed_query\+?="(.*)"$', source, re.MULTILINE))
    said = re.findall(r'^narrowed_said="(.*)"$', source, re.MULTILINE)
    assert query.startswith("select has_table_privilege('app_user', 'devices'") and len(said) == 1
    assert any(query in c for c in commands), query
    assert f"  ok      step 4b: {said[0]}\n" in part
    check = [c for b in _sh_blocks(_part("### `first-boot-check.sh`: steps 10 to 12\n"))
             for c in _commands(b)]  # fmt: skip
    assert "sudo ./scripts/install-backup-timer.sh" in check
    assert "sudo ./scripts/install-backup-timer.sh </dev/null" in _code("first-boot-check.sh")


def test_the_first_boot_installs_the_gateway_certificates_renewal() -> None:
    """The gateway's certificate is 365 days, does not renew itself, and is
    the only server certificate every agent dials: without the timer, the
    whole fleet disconnects at once a year after the first boot. It goes in
    before step 7, from the 0600 token step 3.4 wrote; and first-boot.sh
    reloads systemd before enabling it, as the block does."""
    commands = [c for block in _sh_blocks(_appendix()) for c in _commands(block)]
    enable = "sudo systemctl enable --now its-gateway-cert.timer"
    assert enable in commands
    assert commands.index("sudo systemctl daemon-reload") < commands.index(enable)
    assert commands.index(enable) < commands.index("docker compose up -d")
    assert "sudo systemctl list-timers its-gateway-cert.timer --no-pager" in commands
    unit = next(c for c in commands if c.startswith("ExecStart="))
    assert "issue-gateway-cert.py rmm.novieri.com" in unit
    assert "< /etc/it-suite/gateway-issuer.token" in unit
    code = _code("first-boot.sh")
    assert code.index("sudo systemctl daemon-reload") < code.index(enable)


def test_the_lines_the_runbook_quotes_are_lines_the_scripts_print() -> None:
    """First boot shows how a good run of each script ends. A quoted `ok`,
    `run` or `note` line, or a heading, is one its script prints (with the
    host where the script has `$host`); `…` stands for what differs per run."""
    sources = {
        "first-boot.sh": (SCRIPTS / "first-boot.sh").read_text(),
        "first-boot-check.sh": (SCRIPTS / "first-boot-check.sh").read_text(),
        "prepare-host.sh": (SCRIPTS / "prepare-host.sh").read_text(),
    }
    quoted = re.findall(r"```text\n(.*?)```", _first_boot(), re.DOTALL)
    prereq = _section(README.read_text(), PREREQUISITES)
    quoted += re.findall(r"```text\n(.*?)```", prereq, re.DOTALL)
    checked = 0
    for block in quoted:
        for line in block.splitlines():
            message = re.sub(r"^  (ok|run|note|skip) {2,}", "", line).strip()
            if not message or "…" in message:
                continue
            message = message.replace("rmm.novieri.com", "$host").replace("rmmadmin", "$operator")
            message = message.replace("/opt/it-suite/deploy", "$deploy_dir")
            assert any(message in source for source in sources.values()), line
            checked += 1
    assert checked >= 10, checked


def test_the_walkthrough_says_what_the_scripts_ask_for_and_refuse() -> None:
    """What the operator meets between the three commands, said where they
    meet it and held to the scripts (coordinator, 2026-11-27): the typed words
    in front of every destructive way on of first-boot.sh, which make one of
    its FAILED reports safe to paste whole, each starting with its `cd`; the
    absolute path prepare-host.sh gives, and the account it does not make;
    the check of :8443 from outside, which nothing on this VPS can make; a
    new enrollment token for a check run again, since every failure retires
    its agent; and what an unsigned job means."""
    walk = _first_boot()
    prereq = _section(README.read_text(), PREREQUISITES)
    boot, check, prepare = (_code(s) for s in ("first-boot.sh", "first-boot-check.sh",
                                               "prepare-host.sh"))  # fmt: skip
    for word in ("SAVED", "DESTROY", "DELETE", "UNDO"):
        assert f"guard {word} " in boot, word
        assert f"`{word}`" in walk, word
    assert '[ "$answer" != SEAL ]' in boot and "`SEAL`" in walk
    assert "pasted whole" in walk
    assert "printf '        cd %s\\n' \"$deploy_q\"" in boot
    assert "`cd /opt/it-suite/deploy`" in walk
    assert 'rerun="sudo $self"' in prepare and 'self="$(readlink -f "$0")"' in prepare
    assert "`sudo /opt/it-suite/deploy/scripts/prepare-host.sh`" in prereq
    assert "by hand: deploy/README.md, P1" in prepare and "makes no account" in prereq
    outside = "openssl s_client -connect rmm.novieri.com:8443 -servername rmm.novieri.com"
    assert outside in walk and "Test-NetConnection rmm.novieri.com -Port 8443" in walk
    assert "-connect \"$connect:8443\"" in boot  # the script dials this VPS itself
    assert "A re-run enrols a new one: have a new enrollment token ready (step 11b)." in check
    browser = walk[walk.index("### 2. In the browser") : walk.index("### 3. ")]
    assert "a new enrollment token" in browser
    assert "INVARIANT 1 BROKEN" in check and "Do not put this host in service" in check
    assert "INVARIANT 1 BROKEN" in walk and "Do not put this host in service" in walk


def test_go_live_and_the_restore_say_openbao_unseals_itself() -> None:
    """Design §4 (the operator, 2026-11-27, "Activarlo siempre"): GO-LIVE §4
    says every first boot turns the auto-unseal on, that the five shares are
    recovery keys, and where the key file is kept; and no block there, or in
    docs/07's restore, unseals with shares, which no longer unseal a new host."""
    seal = _section(GO_LIVE.read_text(), "## 4. OpenBao seal")
    flat = " ".join(seal.split())
    assert "2026-11-27" in flat and "Activarlo siempre" in flat
    assert "**recovery keys**" in flat and "**cannot unseal**" in flat
    assert "/etc/it-suite/openbao-unseal/unseal.key" in flat
    assert "apart from any copy of `openbao-data`" in flat
    restore = _section(BACKUP_DOC.read_text(), "## 3. Restoring into a clean stack")
    for name, text in (("GO-LIVE §4", seal), ("docs/07 §3", restore)):
        commands = [c for block in _sh_blocks(text) for c in _commands(block)]
        assert commands, name
        assert not any("bao operator unseal" in c for c in commands), (name, commands)
    assert "docker compose exec openbao bao status" in [
        c for block in _sh_blocks(restore) for c in _commands(block)
    ]


def test_the_prerequisites_open_only_ssh_the_console_and_the_gateway() -> None:
    """P2 to P5 are prepare-host.sh's since 2026-11-27, and their blocks the
    appendix's: the firewall opens 22, 443 and 8443 and nothing else, in both;
    Docker comes from Docker's repository, never Ubuntu's docker.io; and
    GitHub's host key is pinned before the first ssh to it (P6)."""
    prereq = _section(README.read_text(), PREREQUISITES)
    part = _part("### `prepare-host.sh`: P2 to P5\n")
    commands = [c for block in _sh_blocks(part) for c in _commands(block)]
    allowed = sorted(c for c in commands if c.startswith("sudo ufw allow"))
    assert allowed == ["sudo ufw allow 22/tcp", "sudo ufw allow 443/tcp", "sudo ufw allow 8443/tcp"]
    assert "sudo ufw --force enable" in commands
    assert any("download.docker.com/linux/ubuntu" in c for c in commands)
    assert not any(re.search(r"apt(-get)? install\b.*\bdocker\.io\b", c) for c in commands)
    assert any("unattended-upgrades" in c for c in commands)
    assert any(c.startswith("timedatectl status") for c in commands)
    # Docker's own first step: remove what conflicts with its packages.
    assert any(
        c.startswith("sudo apt-get remove -y $(dpkg --get-selections docker.io") for c in commands
    )
    code = _code("prepare-host.sh")
    opened = [line.strip() for line in code.splitlines() if line.strip().startswith("ufw allow")]
    assert sorted(opened) == [
        "ufw allow 22/tcp", "ufw allow 443/tcp", "ufw allow 8443/tcp"
    ]  # fmt: skip
    assert "apt-get remove -y $(dpkg --get-selections docker.io" in code
    assert "docker.io" not in code.replace("dpkg --get-selections docker.io", "")
    # GitHub's published Ed25519 host key, before the first ssh to it.
    p6 = [c for block in _sh_blocks(prereq) for c in _commands(block)]
    assert any(GITHUB_ED25519 in c for c in p6)
    assert prereq.index("### P6 — The checkout") < prereq.index("### P2 to P5 — `prepare-host.sh`")


def test_the_off_host_copy_is_offered_and_asks_for_what_only_the_operator_has() -> None:
    """Recommended, not part of First boot (operator, 2026-11-24): the backup of
    step 12 is on the VPS and goes with it. Every value comes from a prompt,
    and the S3 secret reaches backup.conf on stdin, never in an argv."""
    first_boot = _first_boot()
    assert "OFFSITE" not in "".join(_sh_blocks(first_boot))
    assert "#the-off-host-copy-step-by-step" in first_boot
    commands = [c for block in _sh_blocks(_off_host()) for c in _commands(block)]
    assert "sudo apt-get install -y age" in commands
    for key in ("OFFSITE_AGE_RECIPIENT", "OFFSITE_RSYNC_TARGET", "OFFSITE_S3_ENDPOINT"):
        assert any(c.startswith("read -rp ") and key in c for c in commands), key
    secret = next(c for c in commands if "OFFSITE_S3_SECRET_KEY" in c)
    assert secret.startswith("read -rsp ") and "| sudo sh -c" in secret
    assert "sudo systemctl start its-platform-backup.service" in commands


def test_no_runnable_block_sends_a_bearer_token_or_skips_verification() -> None:
    found = []
    for path in RUNBOOKS:
        for block in _sh_blocks(path.read_text()):
            for command in _commands(block):
                if "Authorization: Bearer" in command or re.search(r"\bcurl\b.*\s-s?k\b", command):
                    found.append(f"{path.name}: {command[:90]}")
    assert found == [], found


#: deploy/certs/ as a path a command reads or writes: never /etc/ssl/certs/,
#: never the /certs/ of a URL (Let's Encrypt's staging roots, step 7).
DEPLOY_CERTS = re.compile(r"(?<![\w./-])(?:deploy/)?certs/")


def test_nothing_copies_a_certificate_into_deploy_certs() -> None:
    found = []
    for path in RUNBOOKS:
        for block in _sh_blocks(path.read_text()):
            found += [f"{path.name}: {c[:90]}" for c in _commands(block) if DEPLOY_CERTS.search(c)]
    assert found == [], found
    tls = _section(GO_LIVE.read_text(), "## 3. TLS")
    assert "./scripts/check-public-tls.sh" in tls
    assert "traefik-acme" in tls
    assert "/etc/letsencrypt" not in tls
    row = next(line for line in BACKUP_DOC.read_text().splitlines() if line.startswith("| 10 |"))
    assert "traefik-acme" in row and "deploy/certs" not in row, row
    # Non-vacuity: the form the old step 7 shipped is seen, and a URL is not.
    assert DEPLOY_CERTS.search("cp /etc/letsencrypt/live/x/fullchain.pem certs/fullchain.pem")
    assert not DEPLOY_CERTS.search("curl https://letsencrypt.org/certs/staging/x.pem")
