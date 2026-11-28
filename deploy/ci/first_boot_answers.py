"""The CI's operator at `deploy/scripts/first-boot.sh` (design
2026-11-27-despliegue-automatizado §5).

It runs the script with its output on a pipe -- which the script allows only
with ITS_FIRST_BOOT_ALLOW_NONTTY=1 -- and answers what a person at its
prompts would: `SAVED` once the five shares and the root token are printed,
then three of those shares, on the script's stdin and never in an argv. Every
share, the root token and the install code is masked in the job's log
(`::add-mask::`) before the line that carries it is printed, so the log shows
`***` where a terminal would have shown the secret; and so is every key of the
raw `bao operator init` output a failed step 3.2 prints.

It answers nothing else. A FAILED report puts each way on that destroys or
undoes behind a word to type (`guard` in first-boot.sh: DESTROY, DELETE, UNDO,
SAVED), and the auto-unseal asks for SEAL when it holds no shares: a run that
shows one of those went wrong, and the driver closes the script's stdin
without typing it and fails the run.

    python3 ci/first_boot_answers.py \\
        --require 'First boot: every step is done.' -- --tls-cafile "$PEBBLE_ROOT"

Run from deploy/. It sets ITS_FIRST_BOOT_ALLOW_NONTTY=1 for the script it
runs, and for nothing else: never export it yourself. The exit status is
the script's; or 1 when a --require line never appeared, when it printed
shares that were not five with a root token, when it showed a word to type,
or when it was still running after --timeout seconds (then it is killed,
with every process it started).
"""

from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
import threading

SHARE = re.compile(r"^  share ([1-5])     (\S+)$")
ROOT = re.compile(r"^  root token  (\S+)$")
CODE = re.compile(r"^  install code  (\S+)$")
#: `bao operator init` as it came, which step 3.2 prints when it cannot read
#: five shares and a root token out of it.
RAW_KEY = re.compile(r"^Unseal Key [1-9][0-9]*: (\S+)$")
RAW_ROOT = re.compile(r"^Initial Root Token: (\S+)$")
SECRETS = (SHARE, ROOT, CODE, RAW_KEY, RAW_ROOT)
#: A word the operator is asked to type: `guard WORD WHAT` prints "This WHAT.
#: Type WORD to go on: " into a FAILED report, and the auto-unseal's prompt
#: asks for SEAL. Any capitalised word, so that a guard added later is caught.
GUARD = re.compile(r"\bType ([A-Z]{3,}) to go on\b|\btype (SEAL) to seal\b")
#: The three shares typed back: not the first three, so a script that kept
#: only the first ones it printed would be caught.
TYPED = (1, 3, 5)


def answers(shares: dict[int, str]) -> str:
    return "SAVED\n" + "".join(shares[n] + "\n" for n in TYPED)


def secret_of(line: str) -> str | None:
    for pattern in SECRETS:
        found = pattern.match(line)
        if found:
            return found.group(found.lastindex or 0)
    return None


def guard_of(line: str) -> str | None:
    found = GUARD.search(line)
    return (found.group(1) or found.group(2)) if found else None


def run(command: list[str], required: list[str], timeout: float) -> int:
    proc = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env={**os.environ, "ITS_FIRST_BOOT_ALLOW_NONTTY": "1"},
        # Its own process group: a timeout kills what it started too, whose
        # copies of its stdout would otherwise keep this read waiting.
        start_new_session=True,
    )
    assert proc.stdin is not None and proc.stdout is not None
    stdin = proc.stdin
    timed_out = threading.Event()

    def kill_all() -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def stop() -> None:
        timed_out.set()
        kill_all()

    def close_stdin() -> None:
        try:
            stdin.close()
        except BrokenPipeError:
            pass

    killer = threading.Timer(timeout, stop)
    killer.start()
    shares: dict[int, str] = {}
    root = False
    answered = False
    guarded: str | None = None
    seen: set[str] = set()
    try:
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            secret = secret_of(line)
            if secret is not None:
                print(f"::add-mask::{secret}", flush=True)
            print(line, flush=True)
            share = SHARE.match(line)
            if share:
                shares[int(share.group(1))] = share.group(2)
            root = root or ROOT.match(line) is not None
            if line in required:
                seen.add(line)
            word = guard_of(line)
            if word is not None and guarded is None:
                # Never typed: the script's stdin ends here, unanswered.
                guarded = word
                if not answered:
                    close_stdin()
                    answered = True
            # The root token comes after the five shares: answer then, or, with
            # fewer than five, close stdin so the script's prompt ends at once.
            if root and not answered:
                try:
                    if len(shares) == 5:
                        stdin.write(answers(shares))
                    stdin.close()
                except BrokenPipeError:
                    pass
                answered = True
        code = proc.wait()
    finally:
        killer.cancel()
        if proc.poll() is None:
            kill_all()
            proc.wait()
        if not stdin.closed:
            close_stdin()
    if timed_out.is_set():
        print(f"FAIL  first-boot.sh was still running after {timeout:.0f} s", flush=True)
        return 1
    if guarded is not None:
        print(
            f"FAIL  first-boot.sh asked for a typed {guarded}: this driver types SAVED and "
            "three shares, and never a word that destroys, undoes or seals",
            flush=True,
        )
        return 1
    if root and len(shares) != 5:
        print(f"FAIL  first-boot.sh printed {len(shares)} shares with its root token", flush=True)
        return 1
    missing = [line for line in required if line not in seen]
    if code == 0 and missing:
        print(f"FAIL  first-boot.sh never printed: {'; '.join(missing)}", flush=True)
        return 1
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--script", default="./scripts/first-boot.sh")
    parser.add_argument("--require", action="append", default=[], help="a line it must print")
    parser.add_argument("--timeout", type=float, default=2400.0)
    parser.add_argument("args", nargs="*", help="first-boot.sh's own arguments, after --")
    options = parser.parse_args(argv)
    return run([options.script, *options.args], options.require, options.timeout)


if __name__ == "__main__":
    sys.exit(main())
