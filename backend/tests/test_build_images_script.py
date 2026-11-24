"""`deploy/scripts/build-images.sh`'s two lists, run for real against a stub
`docker` (the first-boot rehearsal of plan 2026-11-23, Task 12, finding F1).

`Recent tags:` is the rollback target list the runbook (step 1b) and every
deploy report read. The docker CLI trims the spaces at both ends of a
`--format` template before it renders it (docker/cli's formatter,
`strings.Trim(format, " ")`), so a template that starts with an indent prints
no indent. Measured on Docker 29.9.3 on 2026-11-24:
`docker images --format '      {{.Repository}}:{{.Tag}}'` printed
`itsuite/backend:7b1e385`, and a grep for the indented form matched nothing:
build-images.sh said `(none yet)` with two `itsuite/backend` tags in the
store, and listed nothing under `built:`. The stub renders a template the
same way: trimmed, then each image's fields.
"""

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BUILD_IMAGES = REPO / "deploy" / "scripts" / "build-images.sh"

DOCKER = r"""#!/usr/bin/env bash
case "$1" in
  compose) exit 0 ;;   # `docker compose build api web`
  images)
    [ "$2" = --format ] || exit 1
    format="$3"
    # docker/cli trims spaces (only spaces) at both ends of the template.
    while [ "${format# }" != "$format" ]; do format="${format# }"; done
    while [ "${format% }" != "$format" ]; do format="${format% }"; done
    for image in $FAKE_IMAGES; do
      line="${format//\{\{.Repository\}\}/${image%%:*}}"
      line="${line//\{\{.Tag\}\}/${image#*:}}"
      line="${line//\{\{.Size\}\}/1MB}"
      printf '%s\n' "$line"
    done ;;
  *) exit 1 ;;
esac
"""


def _run(tmp_path: Path, images: str) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(DOCKER)
    (bin_dir / "docker").chmod(0o755)
    return subprocess.run(
        ["bash", str(BUILD_IMAGES)],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "IT_SUITE_VERSION": "abc1234",
            "FAKE_IMAGES": images,
        },
        stdin=subprocess.DEVNULL,
    )


def test_recent_tags_lists_the_backend_tags_newest_first(tmp_path: Path) -> None:
    run = _run(
        tmp_path,
        "itsuite/backend:abc1234 itsuite/backend:dev itsuite/web:abc1234 nginx:alpine",
    )
    assert run.returncode == 0, run.stderr
    tags = run.stdout.split("Recent tags:\n", 1)[1]
    assert tags == "      itsuite/backend:abc1234\n      itsuite/backend:dev\n", run.stdout


def test_built_lists_the_two_images_of_this_tag(tmp_path: Path) -> None:
    run = _run(tmp_path, "itsuite/backend:abc1234 itsuite/backend:dev itsuite/web:abc1234")
    assert run.returncode == 0, run.stderr
    built = run.stdout.split("built:\n", 1)[1].split("\n\n", 1)[0]
    assert built == "  itsuite/backend:abc1234  1MB\n  itsuite/web:abc1234  1MB", run.stdout


def test_recent_tags_says_none_yet_only_when_there_is_none(tmp_path: Path) -> None:
    run = _run(tmp_path, "itsuite/web:abc1234 nginx:alpine")
    assert run.returncode == 0, run.stderr
    assert run.stdout.split("Recent tags:\n", 1)[1] == "      (none yet)\n", run.stdout


def test_recent_tags_holds_at_most_five(tmp_path: Path) -> None:
    images = " ".join(f"itsuite/backend:t{n}" for n in range(8))
    run = _run(tmp_path, images)
    assert run.returncode == 0, run.stderr
    tags = run.stdout.split("Recent tags:\n", 1)[1].splitlines()
    assert tags == [f"      itsuite/backend:t{n}" for n in range(5)], run.stdout
