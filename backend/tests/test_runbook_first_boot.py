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


def _first_boot() -> str:
    return _section(README.read_text(), "## First boot\n")


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


def test_after_a_reboot_unseals_starts_and_checks_the_certificate() -> None:
    commands = [c for block in _sh_blocks(_after_reboot()) for c in _commands(block)]
    assert commands.count("docker compose exec openbao bao operator unseal") == 3
    assert "docker compose up -d" in commands
    assert "./scripts/check-public-tls.sh" in commands


def test_the_first_boot_runs_the_scripts_that_check_it() -> None:
    commands = [c for block in _sh_blocks(_first_boot()) for c in _commands(block)]
    for expected in (
        "./scripts/new-env.sh --host rmm.novieri.com",
        "./scripts/build-images.sh",
        "./scripts/preflight.sh",
        "docker compose up -d",
        "./scripts/check-public-tls.sh",
        "docker compose exec traefik stat -c %a /etc/traefik/acme/acme.json",
        "sudo ./scripts/install-backup-timer.sh",
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


def test_the_first_boot_installs_the_gateway_certificates_renewal() -> None:
    """The gateway's certificate is 365 days, does not renew itself, and is
    the only server certificate every agent dials: without the timer, the
    whole fleet disconnects at once a year after the first boot. It goes in
    before step 7, from the 0600 token step 3.4 wrote."""
    commands = [c for block in _sh_blocks(_first_boot()) for c in _commands(block)]
    enable = "sudo systemctl enable --now its-gateway-cert.timer"
    assert enable in commands
    assert commands.index(enable) < commands.index("docker compose up -d")
    assert "sudo systemctl list-timers its-gateway-cert.timer --no-pager" in commands
    unit = next(c for c in commands if c.startswith("ExecStart="))
    assert "issue-gateway-cert.py rmm.novieri.com" in unit
    assert "< /etc/it-suite/gateway-issuer.token" in unit


def test_the_prerequisites_open_only_ssh_the_console_and_the_gateway() -> None:
    prereq = _section(README.read_text(), PREREQUISITES)
    commands = [c for block in _sh_blocks(prereq) for c in _commands(block)]
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
    # GitHub's published Ed25519 host key, before the first ssh to it.
    assert any(GITHUB_ED25519 in c for c in commands)


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
