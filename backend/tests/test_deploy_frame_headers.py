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


def _locations(text: str) -> dict[str, str]:
    blocks: dict[str, str] = {}
    for match in re.finditer(r"location\s+(\S+)\s*\{", text):
        depth, end = 1, match.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(text[end], 0)
            end += 1
        blocks[match.group(1)] = text[match.end() : end - 1]
    return blocks


def test_the_parser_sees_both_locations() -> None:
    assert set(_locations(NGINX.read_text())) == {"/assets/", "/"}


def test_every_location_refuses_to_be_framed() -> None:
    for path, body in _locations(NGINX.read_text()).items():
        assert CSP in body, path
        assert XFO in body, path
