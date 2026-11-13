"""The console cannot be framed (plan 2026-11-06, Task 4, decision 6; B3-16).

RFC 9700 §4.16 and OAuth 2.1-17 §7.11: "Authorization servers MUST prevent
clickjacking attacks." The consent page at `/oauth/authorize` is a console
page that nginx serves (`deploy/nginx.conf`), so the headers are nginx's. nginx
does NOT inherit `add_header` into a `location` that declares its own, so every
`location` must carry both.
"""

import re
from pathlib import Path

NGINX = Path(__file__).resolve().parents[2] / "deploy" / "nginx.conf"
CSP = """add_header Content-Security-Policy "frame-ancestors 'none'" always;"""
XFO = 'add_header X-Frame-Options "DENY" always;'

# What the parser must not be fooled by (Task 4 fix round 1): an exact and a
# regex location, where nginx drops an inherited `add_header` as in any other;
# a quoted argument and a quoted value with a brace in them; and a location
# that is only a comment.
TRICKY = r"""
server {
    location / {
        add_header Content-Security-Policy "frame-ancestors 'none'" always;
        add_header X-Frame-Options "DENY" always;
    }
    # location /retired { add_header X-Frame-Options "DENY" always; }
    location = /index.html {
        add_header Cache-Control "no-store";
    }
    location ~* \.map$ {
        return 404;
    }
    location ~ "^/v{1,2}/" {
        add_header X-Note "a } in a value";
    }
}
"""

_STRING_OR_COMMENT = re.compile(r""""(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|#[^\n]*""")


def _code(text: str) -> str:
    """`text` with its comments and its quoted strings' insides blanked, at the same offsets."""

    def blank(match: re.Match[str]) -> str:
        found = match.group(0)
        if found.startswith("#"):
            return " " * len(found)
        return found[0] + " " * (len(found) - 2) + found[-1]

    return _STRING_OR_COMMENT.sub(blank, text)


def _directives(text: str) -> int:
    """How many `location` directives `text` declares, outside comments and strings."""
    return len(re.findall(r"(?:^|[;{}])\s*location\b", _code(text), re.MULTILINE))


def _locations(text: str) -> dict[str, str]:
    """`location` argument (its modifier included, `= /index.html`) -> its block, as written.

    Read on `_code(text)`, so a comment is no location and a brace in a string
    neither opens nor closes one; the argument and the body are then sliced
    from `text` at the same offsets.
    """
    code = _code(text)
    blocks: dict[str, str] = {}
    for match in re.finditer(r"(?:^|[;{}])\s*location\s+([^{;]+?)\s*\{", code, re.MULTILINE):
        depth, end = 1, match.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(code[end], 0)
            end += 1
        blocks[" ".join(text[match.start(1) : match.end(1)].split())] = text[match.end() : end - 1]
    return blocks


def _unframed(text: str) -> list[str]:
    """The locations that do not send both headers."""
    return [path for path, body in _locations(text).items() if CSP not in body or XFO not in body]


def test_the_parser_sees_both_locations() -> None:
    assert set(_locations(NGINX.read_text())) == {"/assets/", "/"}


def test_the_parser_sees_exact_and_regex_locations_and_no_comment() -> None:
    assert set(_locations(TRICKY)) == {"/", "= /index.html", r"~* \.map$", '~ "^/v{1,2}/"'}


def test_a_location_without_both_headers_is_named() -> None:
    assert _unframed(TRICKY) == ["= /index.html", r"~* \.map$", '~ "^/v{1,2}/"']


def test_the_parser_misses_no_location_directive() -> None:
    # The guard on the parser itself: a `location` it could not read is a
    # location nobody checked.
    for text in (NGINX.read_text(), TRICKY):
        assert len(_locations(text)) == _directives(text)


def test_every_location_refuses_to_be_framed() -> None:
    assert _unframed(NGINX.read_text()) == []
