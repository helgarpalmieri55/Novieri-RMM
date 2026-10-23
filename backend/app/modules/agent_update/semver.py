"""Strict semver for agent self-update ordering (spec-self-update.md §4.3).

One comparator on both sides, pinned by the shared vectors in
`docs/kernel/wire-protocol/testdata/semver-order.json`; the Go twin is
`agent/internal/update/semver.go`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import total_ordering

__all__ = ["MAX_VERSION_LEN", "Version", "parse_version", "try_parse_version"]

MAX_VERSION_LEN = 50

_NUM = r"(?:0|[1-9][0-9]*)"
_PRE_ID = r"(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
_RE = re.compile(rf"^({_NUM})\.({_NUM})\.({_NUM})(?:-({_PRE_ID}(?:\.{_PRE_ID})*))?")


@total_ordering
@dataclass(frozen=True)
class Version:
    major: int
    minor: int
    patch: int
    pre: tuple[str, ...] = ()

    def __str__(self) -> str:
        core = f"{self.major}.{self.minor}.{self.patch}"
        return f"{core}-{'.'.join(self.pre)}" if self.pre else core

    def _key(self) -> tuple[int, int, int]:
        return (self.major, self.minor, self.patch)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self._key() == other._key() and self.pre == other.pre

    def __hash__(self) -> int:
        return hash((self._key(), self.pre))

    def __lt__(self, other: Version) -> bool:
        if self._key() != other._key():
            return self._key() < other._key()
        if not self.pre or not other.pre:
            return bool(self.pre) and not other.pre
        for a, b in zip(self.pre, other.pre, strict=False):
            if a == b:
                continue
            a_num, b_num = a.isdigit(), b.isdigit()
            if a_num and b_num:
                return int(a) < int(b)
            if a_num != b_num:
                return a_num
            return a < b
        return len(self.pre) < len(other.pre)


def parse_version(text: str) -> Version:
    """Parse `MAJOR.MINOR.PATCH[-pre]`; one leading `v` is stripped.

    Raises `ValueError` for anything else, including build metadata.
    """
    s = text[1:] if text.startswith("v") else text
    if not s or len(s) > MAX_VERSION_LEN:
        raise ValueError(  # i18n-exempt: callers catch it (try_parse_version, _parse_target)
            f"not a strict semantic version: {text!r}"
        )
    m = _RE.fullmatch(s)
    if m is None:
        raise ValueError(  # i18n-exempt: callers catch it (try_parse_version, _parse_target)
            f"not a strict semantic version: {text!r}"
        )
    pre = tuple(m.group(4).split(".")) if m.group(4) else ()
    return Version(int(m.group(1)), int(m.group(2)), int(m.group(3)), pre)


def try_parse_version(text: str | None) -> Version | None:
    if not text:
        return None
    try:
        return parse_version(text)
    except ValueError:
        return None
