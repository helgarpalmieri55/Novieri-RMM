"""The committed realm and theme, pinned (docs/modules/console-auth/spec.md §5).

WHY A TEXT-LEVEL GUARD IS THE RIGHT TOOL HERE. None of this runs in the test
suite: there is no Keycloak, the theme is FreeMarker rendered by a Java process,
and the realm JSON is consumed by an importer that only runs against an empty
database. So the achievable check is over what is COMMITTED -- and that is where
three of the spec's falsifiers actually live, because each one is a property of
a file somebody edits.

WHAT THIS CANNOT SHOW, stated so nobody reads green here as "sign-in works": it
never renders a page, never mints a token, and says nothing about the LIVE
realm, which diverges from this file the moment an operator touches the admin
console -- and must, because realm import runs only against an empty database.
`deploy/README.md`'s "Branding and extending sign-in" is the other half.
"""

import ast
import json
import re
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
REALM_PATH = REPO / "deploy/keycloak/realm-it-suite.json"
THEME = REPO / "deploy/keycloak/themes/itms/login"
REALM: dict[str, Any] = json.loads(REALM_PATH.read_text())


def test_the_realm_file_is_where_this_thinks_it_is() -> None:
    """Non-vacuity.

    Every assertion below reads `REALM`. Making the file's location an
    assertion rather than an assumption is what stops a moved or renamed realm
    from turning this module into a set of checks over something else.
    """
    assert REALM_PATH.is_file(), REALM_PATH
    assert REALM["realm"] == "it-suite", REALM.get("realm")


def test_no_client_enables_the_password_grant() -> None:
    """§5.1, and the reason the password field is not in the React app at all.

    A public client with `directAccessGrantsEnabled` turns every leaked
    password into an API token with no browser and no MFA prompt -- which also
    routes around the MFA this module exists to present. `deploy/GO-LIVE.md` §2
    states the prohibition; this makes it a test.
    """
    offenders = [c["clientId"] for c in REALM["clients"] if c.get("directAccessGrantsEnabled")]
    assert not offenders, offenders


def test_the_realm_asks_for_the_itms_theme() -> None:
    assert REALM.get("loginTheme") == "itms", REALM.get("loginTheme")


def test_the_theme_exists_at_the_path_the_realm_names() -> None:
    """A `loginTheme` naming a directory Keycloak cannot see fails SILENTLY --
    it falls back to its own theme, so the symptom is an unbranded page with
    nothing in any log to say why. Two files that must exist together."""
    assert (THEME / "theme.properties").is_file()
    assert (THEME / "login.ftl").is_file()
    assert (THEME / "resources/css/itms.css").is_file()


def test_the_theme_is_mounted_into_the_container() -> None:
    """The other half of the same silent failure: a theme committed here and
    not mounted is a theme Keycloak has never heard of."""
    compose = (REPO / "deploy/docker-compose.yml").read_text()
    assert "./keycloak/themes:/opt/keycloak/themes:ro" in compose


#: FreeMarker comments, which must be stripped before any of the checks below.
#:
#: FOUND BY FALSIFICATION, and it is worth recording because the test looked
#: fine. The first version of `test_the_login_template_...` searched the raw
#: file for "realm.password". The template's own header comment EXPLAINS
#: `realm.password`, so when the conditional was deliberately replaced with
#: `<#if true>` -- a hardcoded form, exactly the defect -- the test still
#: passed, matching prose. A guard over source text that reads its own
#: documentation is a guard that cannot fail.
_FTL_COMMENT = re.compile(r"<#--.*?-->", re.DOTALL)


def _login_template() -> str:
    return _FTL_COMMENT.sub("", (THEME / "login.ftl").read_text())


def test_the_login_template_uses_the_field_loop_rather_than_hardcoded_inputs() -> None:
    """§5.2, as close as this gets without a running Keycloak.

    The AUTHENTICATION FLOW decides which credentials are asked for; the
    template only decides how they look. A theme that renders its own
    unconditional username and password inputs shows a password box during a
    passkey-only flow -- the feature appears present and does the other thing.
    The achievable proxy is that the template asks Keycloak what to show.

    Asserting the DIRECTIVES, not the words: `<#if realm.password>` is a
    conditional, `realm.password` on its own is any mention at all.
    """
    ftl = _login_template()
    assert "<#if realm.password>" in ftl, ftl[:400]
    assert "<#if !usernameHidden??>" in ftl, (
        "the username block must be conditional: a passkey flow hides it"
    )
    # `?has_content` as well as `??` since 2026-08-20: `??` alone asks whether
    # the variable exists, and Keycloak sets it to an EMPTY LIST when there is
    # no provider -- so the page drew a divider reading "or sign in with" over
    # nothing at all.
    assert "<#if social.providers?? && social.providers?has_content>" in ftl, (
        "the identity-provider block must be conditional on the flow offering any"
    )


def test_the_template_never_hardcodes_an_always_true_branch() -> None:
    """The shape the falsification used, refused directly.

    `<#if true>` around a credential block is how a hardcoded form gets written
    while still looking conditional to a reader skimming for `<#if`.
    """
    assert "<#if true>" not in _login_template()


def test_the_theme_carries_the_console_design_tokens() -> None:
    """Copied from `frontend/src/index.css`, deliberately -- Keycloak serves
    this file from its own container and cannot reach the React app's
    stylesheet. The four below ARE the identity a sign-in page has to share
    with the console: the rail it sits on, the one accent, the panel's corner
    and the face. `test_every_token_the_theme_copies_is_the_consoles_own`
    holds the rest of the copy to the console's values, name by name."""
    css = (THEME / "resources/css/itms.css").read_text()
    for token in ("--rail: #0f1a20", "--accent: #1f5fd1", "--r-panel: 10px", "Inter"):
        assert token in css, token


# --------------------------------------------------------------------------- #
# The sign-in page on the v3 tokens, measured rather than quoted (2026-09-07)
#
# Keycloak cannot load `frontend/src/index.css`, so the theme keeps literal
# values in its own `:root`. Three things are held here, computed from the
# files themselves on every run:
#
#   1. THE COPY. Every token the theme declares exists in the FIRST `:root` of
#      `frontend/src/index.css` under the same name at the same value (the
#      console's block is itself pinned to the design file by
#      `frontend/src/test/tokens.test.ts`), and every token copied is painted
#      by some rule, so nothing sits in the copy unused and drifts.
#   2. THE RATIOS. `docs/product.md` §11 makes WCAG 2.2 AA a product
#      requirement. Every text pair the page paints is graded at SC 1.4.3's
#      4.5:1, every control boundary and focus ring at SC 1.4.11's 3:1, and
#      the ground for light text is read out of the gradient the `body` rule
#      declares rather than assumed.
#   3. THE REFUSALS. The accent is not text or a ring on the dark field, and
#      the decorative hairline is not a control's boundary. Both are measured
#      to fail and asserted to be unused in that role, so the next editor who
#      reaches for them meets a red test rather than a comment.
# --------------------------------------------------------------------------- #

THEME_CSS = THEME / "resources/css/itms.css"
CONSOLE_CSS = REPO / "frontend/src/index.css"
CONSOLE_KIT_CSS = REPO / "frontend/src/shell/components.css"

#: CSS comments, stripped before anything below reads a declaration: the
#: stylesheet's header DISCUSSES the colours these tests measure, and a guard
#: that read the raw file would happily match the prose it exists to check.
_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def _theme_css() -> str:
    return _CSS_COMMENT.sub("", THEME_CSS.read_text())


def _rules(css: str | None = None) -> list[tuple[str, str]]:
    """Every flat CSS rule, as (selector, declarations).

    Deliberately naive: `[^{}]` on both sides means an `@media` wrapper is
    skipped over rather than parsed, while the rules INSIDE it are still
    found. That is the right trade for the guards that read this, which ask
    which SELECTOR carries a declaration -- and a rule nested in a media query
    carries it just the same.
    """
    return [
        (match.group(1).strip(), match.group(2))
        for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", css if css is not None else _theme_css())
    ]


def _first_root(css: str) -> dict[str, str]:
    """The FIRST `:root { ... }` block of a stylesheet, one declaration per line.

    Line by line, the way `frontend/src/test/tokens.test.ts` reads the design
    file and the console: a value may carry commas, quotes and a data URI, so
    the declaration boundary is the line, not the semicolon.
    """
    stripped = _CSS_COMMENT.sub("", css)
    start = stripped.index(":root {")
    block = stripped[start + len(":root {") : stripped.index("}", start)]
    tokens: dict[str, str] = {}
    for line in block.splitlines():
        declaration = line.strip().rstrip(";")
        if not declaration.startswith("--") or ":" not in declaration:
            continue
        name, value = declaration.split(":", 1)
        tokens[name.strip()] = re.sub(r"\s+", " ", value.strip())
    return tokens


def _theme_tokens() -> dict[str, str]:
    """The tokens the theme declares in its OWN `:root` -- the copy a browser
    actually paints, and therefore the only thing worth measuring."""
    return _first_root(THEME_CSS.read_text())


def _console_tokens() -> dict[str, str]:
    return _first_root(CONSOLE_CSS.read_text())


THEME_TOKENS = _theme_tokens()

#: Declared by the theme for itself: the Tabler glyphs it paints as masks on
#: markup Keycloak's base template owns. Not colours, not copied, not pinned.
PAGE_ONLY_PREFIX = "--icon-"

_HEX = re.compile(r"^#[0-9a-f]{6}$")


def _luminance(colour: str) -> float:
    """WCAG 2.2 relative luminance of an `#rrggbb` colour."""

    def channel(at: int) -> float:
        value = int(colour[at : at + 2], 16) / 255
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    return 0.2126 * channel(1) + 0.7152 * channel(3) + 0.0722 * channel(5)


def _contrast(a: str, b: str) -> float:
    """WCAG 2.2 contrast ratio between two `#rrggbb` colours. Order-independent."""
    first, second = _luminance(a), _luminance(b)
    return (max(first, second) + 0.05) / (min(first, second) + 0.05)


def _colour(name: str) -> str:
    """A token name resolved through the theme's `:root`; a literal passes through."""
    if name.startswith("#"):
        return name
    value = THEME_TOKENS.get(name, "")
    assert _HEX.match(value), f"itms.css declares no colour {name} -- it has {sorted(THEME_TOKENS)}"
    return value


def _field_stops() -> list[str]:
    """The token names the `body` rule's gradient runs between.

    Read out of the rule rather than hardcoded, because the stops are the
    hinge: point the gradient at a lighter token and every light-on-dark
    ratio falls, silently, unless the ground is derived from the file.
    """
    # The selector `body` appears twice: the field, and the narrow-viewport
    # padding inside `@media`. The one that draws the field is the one that
    # declares the gradient.
    gradients = [
        re.search(r"linear-gradient\(([^;]*)\)", body)
        for selector, body in _rules()
        if selector == "body" and "linear-gradient" in body
    ]
    assert len(gradients) == 1, (
        "the field is not what this assumes: expected exactly one body gradient"
    )
    gradient = gradients[0]
    assert gradient
    stops = re.findall(r"var\((--[\w-]+)\)", gradient.group(1))
    assert stops, "the gradient names no token"
    return stops


def _field_grounds() -> tuple[str, str]:
    """(brightest, darkest) colour any text on the sign-in field is drawn against.

    Light-on-dark is worst at the ground's LIGHTEST point, so every on-field
    ratio is measured there or it is measured somewhere flattering.
    """
    colours = sorted((_colour(stop) for stop in _field_stops()), key=_luminance)
    return colours[-1], colours[0]


FIELD_BRIGHT, FIELD_DARK = _field_grounds()

#: Small text on a dark ground: the field above the card, and the one filled
#: button on it. Kept apart from the light-surface table below so a reader can
#: see which way round each ratio runs -- these are all LIGHT on DARK.
#:
#: Everything on this page is under WCAG's large-text thresholds (18.66px at
#: 700, or 24px), the wordmark included at 22px/600, so the floor is SC 1.4.3's
#: strict 4.5:1 throughout; `test_no_text_on_the_page_qualifies_for_the_relaxed_floor`
#: holds that.
LIGHT_TEXT_ON_DARK: list[tuple[str, str, str]] = [
    (
        "--accent-ink",
        FIELD_BRIGHT,
        "#kc-header-wrapper 22px/600, #kc-attempted-username, #reset-login, #kc-locale's open list",
    ),
    ("--accent-ink", FIELD_DARK, "the same, where the field is darkest"),
    ("--rail-ink", FIELD_BRIGHT, "#kc-page-title, and #kc-locale's closed dropdown"),
    ("--rail-ink", FIELD_DARK, "the same, where the field is darkest"),
    (
        "--accent-ink",
        "--accent",
        ".itms-primary and #authenticateWebAuthnButton: the one button the page exists for",
    ),
]

#: Small text on the white card and the washes inside it: DARK on LIGHT, the
#: other direction. Floor is again SC 1.4.3's 4.5:1.
DARK_TEXT_ON_LIGHT: list[tuple[str, str, str]] = [
    (
        "--ink",
        "--surface",
        "body copy, .itms-field label, what you type, the secondary controls, "
        ".itms-authenticator-name",
    ),
    (
        "--ink",
        "--surface-2",
        "the secondary controls under the pointer; .itms-secret-value in its panel",
    ),
    ("--ink", "--accent-s", "::selection"),
    (
        "--muted",
        "--surface",
        ".itms-help, .itms-footnote, .itms-step-note, .itms-steps, .itms-divider, "
        "::placeholder, .itms-authenticator-help",
    ),
    ("--muted", "--surface-2", ".itms-secret-label; .itms-authenticator-help under the pointer"),
    ("--accent", "--surface", ".itms-secondary-link"),
    ("--accent", "--accent-s", ".alert-info"),
    ("--sev-crit", "--surface", "[id^=input-error], the inline validation errors"),
    ("--sev-crit", "--sev-crit-s", ".alert-error"),
    ("--sev-warn", "--sev-warn-s", ".alert-warning"),
    ("--sev-ok", "--sev-ok-s", ".alert-success, .itms-pill-ok"),
]

#: Focus indicators on the dark field, at SC 1.4.11's 3:1 -- A NON-TEXT FLOOR,
#: which is why this table and the next two are separate from the text tables.
#: The text tables answer to SC 1.4.3; a focus ring or a control's boundary is
#: not type at all, so 1.4.11 reaches it instead, at 3:1, whatever the size of
#: the thing it surrounds. Folding these rows into a text table would land on
#: a number by the wrong argument, and the next reader would inherit it.
#:
#: `--accent-ink` (white) is the page-wide `:focus-visible` default, so every
#: focusable thing above the card gets it without a rule of its own -- which
#: is the point of the split `test_the_field_can_never_inherit_the_accent_ring`
#: holds in place.
FOCUS_RING_ON_FIELD: list[tuple[str, str, str]] = [
    ("--accent-ink", FIELD_BRIGHT, "the :focus-visible default, inherited above the card"),
    ("--accent-ink", FIELD_DARK, "the same ring, where the field is darkest"),
]

#: Focus indicators inside the card, same 3:1 non-text floor, opposite colour.
#: The ring is drawn OUTSIDE the control it surrounds, so its ground is the
#: card behind it rather than the control's own fill.
FOCUS_RING_ON_CARD: list[tuple[str, str, str]] = [
    (
        "--accent",
        "--surface",
        "#kc-content :focus-visible, 2px out; the focused field's ring, 1px out",
    ),
]

#: The boundary that identifies a control (SC 1.4.11, 3:1): v3's
#: `--line-control` on every surface a control lands on here, the hover edge,
#: and the accent edge of the primary button and the focused field.
CONTROL_BOUNDARY_ON_CARD: list[tuple[str, str, str]] = [
    ("--line-control", "--surface", "every input, select and secondary control at rest"),
    ("--ink-2", "--surface-2", "the secondary controls under the pointer"),
    ("--accent", "--surface", ".itms-primary's edge; the focused field's border"),
]

#: The colours the stylesheet REFUSES for a job, the ground each is refused
#: against, and the floor it fails. See `test_the_refused_colours_really_fail`
#: for why this table is the one with teeth.
REFUSED: list[tuple[str, str, float, str]] = [
    ("--accent", FIELD_BRIGHT, 4.5, "text on the field (the Two Grounds Rule)"),
    ("--accent", FIELD_BRIGHT, 3.0, "a focus ring on the field"),
    ("--line", "--surface", 3.0, "the boundary of a control"),
    ("--rail-ink", "--surface", 4.5, "the rail's text tone used on the card"),
]

#: Selectors that render ABOVE the card, on the dark field. Under
#: `parent=base` these are `#kc-content`'s siblings; everything else the theme
#: styles is a descendant of the card.
ON_FIELD = (
    "#kc-header-wrapper",
    "#kc-page-title",
    "#kc-username",
    "#kc-attempted-username",
    "#reset-login",
    "#kc-locale",
)

#: Selectors that are CONTROLS, whose boundary SC 1.4.11 reaches.
CONTROLS = (
    ".itms-field input",
    ".itms-primary",
    ".itms-social-link",
    ".itms-alt",
    "#try-another-way",
    ".itms-authenticator",
    "#authenticateWebAuthnButton",
)


def test_the_stylesheet_parsed_into_something_worth_measuring() -> None:
    """Non-vacuity. Every row below reads through `_colour`, which raises on a
    token it cannot find -- but a `:root` that parsed as EMPTY would make every
    row fail identically, which reads as one broken stylesheet rather than
    "the parser stopped working and none of these ratios were ever computed".
    Fail here, loudly, instead."""
    assert len(THEME_TOKENS) > 20, sorted(THEME_TOKENS)
    assert THEME_TOKENS["--surface"] == "#ffffff"
    assert THEME_TOKENS["--rail"] == "#0f1a20"
    assert _field_stops() == ["--rail", "--rail-2"], _field_stops()
    assert FIELD_BRIGHT == _colour("--rail-2") and FIELD_DARK == _colour("--rail")


def test_the_contrast_arithmetic_matches_hand_verified_references() -> None:
    """The formula itself needs a witness, or the tables are grading a bug.

    Black on white is exactly 21:1 by definition and white on white exactly
    1:1; #767676 is the canonical mid grey that lands just over the 4.5:1 floor
    on white, and was checked against an independent calculator.
    """
    assert round(_contrast("#000000", "#ffffff"), 5) == 21
    assert round(_contrast("#ffffff", "#ffffff"), 5) == 1
    assert round(_contrast("#767676", "#ffffff"), 2) == 4.54


def test_every_token_the_theme_copies_is_the_consoles_own() -> None:
    """THE PIN. Keycloak cannot import the console's stylesheet, so the theme
    carries a copy, and a copy is only a copy while it matches. Every token in
    the theme's `:root` -- colour, size, radius, shadow, face -- has to exist
    in the FIRST `:root` of `frontend/src/index.css` under the same name at
    exactly the same value. That block is itself pinned to the design file by
    `frontend/src/test/tokens.test.ts`, so a value moved in the design has to
    move here in the same commit or this goes red."""
    console = _console_tokens()
    assert len(console) > 30, "the console's first :root did not parse"
    for name, value in THEME_TOKENS.items():
        if name.startswith(PAGE_ONLY_PREFIX):
            continue
        assert name in console, f"{name} is not a console token; the theme invented it"
        assert value == console[name], (
            f"{name} is {value} in the theme but {console[name]} in the console"
        )


def test_the_theme_copies_the_identity_it_paints() -> None:
    """The other direction of the pin: the copy is complete for what the page
    draws. A page that styled the card in `--surface` and left `--line-control`
    out would fall back to the browser's default border, silently."""
    required = {
        "--t-h1",
        "--t-h2",
        "--t-body",
        "--t-sm",
        "--r-ctl",
        "--r-panel",
        "--r-pill",
        "--surface",
        "--surface-2",
        "--ink",
        "--ink-2",
        "--muted",
        "--line",
        "--line-control",
        "--rail",
        "--rail-2",
        "--rail-ink",
        "--accent",
        "--accent-s",
        "--accent-ink",
        "--sev-crit",
        "--sev-crit-s",
        "--sev-warn",
        "--sev-warn-s",
        "--sev-ok",
        "--sev-ok-s",
        "--shadow",
        "--mono",
        "--sans",
    }
    missing = sorted(required - THEME_TOKENS.keys())
    assert missing == [], f"the theme does not copy {missing}"


def test_no_copied_token_is_unused() -> None:
    """An unused copy is a copy that drifts unnoticed: nothing paints it, so
    nothing looks wrong when it is. Every token in the theme's `:root` has to
    be read by a rule."""
    css = _theme_css()
    body = css[css.index("}", css.index(":root {")) :]
    unused = sorted(name for name in THEME_TOKENS if f"var({name})" not in body)
    assert unused == [], f"{unused} are copied into itms.css and painted by nothing"


def test_the_retired_palette_is_gone() -> None:
    """v3 has one accent. The magenta and the two-accent vocabulary that
    carried it are retired, and the strongest way to say a colour is not used
    is for its name and its hex to appear nowhere in the file."""
    css = _theme_css()
    for retired in (
        "--action",
        "--primary",
        "--rail-glow",
        "--rail-label",
        "--sidebar",
        "--canvas",
        "--radius",
        "--line-strong",
        "--muted-2",
        "--red-",
        "--gold-",
        "--green-",
        "#c2186f",
        "#0d2026",
        "#1e5fd0",
        "#6aa6ff",
    ):
        assert retired not in css, f"{retired} is still named in itms.css"


def test_the_theme_paints_no_translucent_literal() -> None:
    """The v3 field has no glow: the one `rgba()` the theme may carry is the
    console's own `--shadow`, copied verbatim. A translucent literal anywhere
    else is a colour the tables above cannot see, composited onto a ground
    they cannot derive."""
    css = _theme_css()
    without_shadow = re.sub(r"--shadow:[^;]+;", "", css)
    assert "rgba(" not in without_shadow, "a translucent literal outside --shadow"
    assert "rgb(" not in without_shadow


def test_every_size_and_radius_is_on_the_ladder() -> None:
    """Five type sizes and three radii, named, never literal. The harmony gate
    holds the console to this in a browser; Keycloak's page is outside that
    gate, so the same rule is held here at the source."""
    css = _theme_css()
    sizes = re.findall(r"font-size:\s*([^;]+);", css)
    assert sizes, "no font-size declared"
    off_ramp = [s for s in sizes if not re.fullmatch(r"var\(--t-(h1|h2|body|sm)\)", s.strip())]
    assert off_ramp == [], f"font sizes off the ramp: {off_ramp}"
    shorthand = re.findall(r"\bfont:\s*([^;]+);", css)
    for value in shorthand:
        if value.strip() == "inherit":
            continue
        assert "var(--t-" in value, f"a font shorthand with a literal size: {value}"
    radii = re.findall(r"border-radius:\s*([^;]+);", css)
    assert radii, "no border-radius declared"
    off_ladder = [r for r in radii if not re.fullmatch(r"var\(--r-(ctl|panel|pill)\)", r.strip())]
    assert off_ladder == [], f"radii off the ladder: {off_ladder}"


def test_no_text_on_the_page_qualifies_for_the_relaxed_floor() -> None:
    """WHY THERE IS NO LARGE-TEXT TABLE. SC 1.4.3 relaxes to 3:1 for text at
    18.66px bold (700) or 24px at any weight. The ramp's largest step is 22px
    and the theme's heaviest weight is 600, so nothing here qualifies and
    every text pair is graded at 4.5:1 -- and a future 700 at 22px would
    quietly become text the tables grade at the wrong bar. This holds the
    premise."""
    css = _theme_css()
    largest = max(
        int(re.sub(r"[^\d.]", "", THEME_TOKENS[t]).split(".")[0])
        for t in ("--t-h1", "--t-h2", "--t-body", "--t-sm")
    )
    assert largest < 24, largest
    weights = [int(w) for w in re.findall(r"font-weight:\s*(\d{3})\s*;", css)]
    weights += [int(w) for w in re.findall(r"\bfont:\s*(\d{3})\s+var", css)]
    assert weights, "no font-weight declared"
    assert max(weights) <= 600, f"a weight of {max(weights)} at {largest}px qualifies as large text"


@pytest.mark.parametrize(("text", "ground", "painted_by"), LIGHT_TEXT_ON_DARK)
def test_light_text_on_a_dark_ground_clears_the_small_text_floor(
    text: str, ground: str, painted_by: str
) -> None:
    """SC 1.4.3, strict bar: 4.5:1, because none of this text is large.

    Breaking a row here means someone signing in cannot read the step they are
    on, the account they are signing in as, or the label on the only button --
    on the first screen of a product that grants domain-admin-equivalent
    control over a fleet. `docs/product.md` §11 makes AA a requirement rather
    than an aspiration, so this is a defect and not a preference.
    """
    ratio = _contrast(_colour(text), _colour(ground))
    assert ratio >= 4.5, (
        f"{text} {_colour(text)} on {ground} {_colour(ground)} is {ratio:.2f}:1, "
        f"under the 4.5:1 floor -- painted by {painted_by}"
    )


@pytest.mark.parametrize(("text", "ground", "painted_by"), DARK_TEXT_ON_LIGHT)
def test_dark_text_on_a_light_ground_clears_the_small_text_floor(
    text: str, ground: str, painted_by: str
) -> None:
    """SC 1.4.3 again, on the card: same 4.5:1 floor, opposite direction.

    The rows that matter most are the feedback ones. A validation error or an
    `.alert-error` that fails contrast is a failed sign-in that looks like no
    sign-in at all -- the user retries the same wrong thing because the reason
    is on screen and unreadable.
    """
    ratio = _contrast(_colour(text), _colour(ground))
    assert ratio >= 4.5, (
        f"{text} {_colour(text)} on {ground} {_colour(ground)} is {ratio:.2f}:1, "
        f"under the 4.5:1 floor -- painted by {painted_by}"
    )


@pytest.mark.parametrize(("ring", "ground", "painted_by"), FOCUS_RING_ON_FIELD)
def test_a_focus_ring_on_the_field_clears_the_non_text_floor(
    ring: str, ground: str, painted_by: str
) -> None:
    """SC 1.4.11, 3:1, and the reason it is 3:1 and not 4.5:1 is that a focus
    ring is not text.

    Breaking a row here means a keyboard user cannot see where they are on the
    page. On this page that is worse than it sounds: the field carries the
    "start over" link and the language switcher, so losing the ring out here
    means tabbing blind on the two controls that exist to rescue somebody
    already having trouble signing in.
    """
    ratio = _contrast(_colour(ring), _colour(ground))
    assert ratio >= 3, (
        f"{ring} {_colour(ring)} on {ground} {_colour(ground)} is {ratio:.2f}:1, "
        f"under the 3:1 non-text floor -- painted by {painted_by}"
    )


@pytest.mark.parametrize(("ring", "ground", "painted_by"), FOCUS_RING_ON_CARD)
def test_a_focus_ring_on_the_card_clears_the_non_text_floor(
    ring: str, ground: str, painted_by: str
) -> None:
    """SC 1.4.11 again, on the white card, where the colour is the other one."""
    ratio = _contrast(_colour(ring), _colour(ground))
    assert ratio >= 3, (
        f"{ring} {_colour(ring)} on {ground} {_colour(ground)} is {ratio:.2f}:1, "
        f"under the 3:1 non-text floor -- painted by {painted_by}"
    )


@pytest.mark.parametrize(("edge", "ground", "painted_by"), CONTROL_BOUNDARY_ON_CARD)
def test_a_control_boundary_on_the_card_clears_the_non_text_floor(
    edge: str, ground: str, painted_by: str
) -> None:
    """SC 1.4.11's other half: the boundary that identifies a control. This is
    the measurement the previous theme could not clear -- its resting border
    was a hairline at 1.41:1, reported rather than papered over -- and v3's
    `--line-control` exists to clear it."""
    ratio = _contrast(_colour(edge), _colour(ground))
    assert ratio >= 3, (
        f"{edge} {_colour(edge)} on {ground} {_colour(ground)} is {ratio:.2f}:1, "
        f"under the 3:1 non-text floor -- painted by {painted_by}"
    )


def test_the_field_can_never_inherit_the_accent_ring() -> None:
    """THE LOOPHOLE GUARD FOR THE RING, and the defect that produced it.

    The page-wide `:focus-visible` default was once the accent, which is under
    the 3:1 floor on the field's brightest point; the page was accessible only
    because the two focusable things above the card each happened to override
    it. A default that is wrong everywhere it is not overridden is a defect
    waiting for the next element somebody adds above the card.

    The default is now white and the accent is scoped to `#kc-content`. This
    holds all three halves of that: the accent still fails on the field where
    it binds, the accent is not in the field's ring table, and no rule
    OUTSIDE the card paints an outline with it. The last one is the structural
    claim -- without it somebody reintroduces `outline: 2px solid var(--accent)`
    on a field element and both of the first two stay green.
    """
    accent = _colour("--accent")
    ratio = _contrast(accent, FIELD_BRIGHT)
    assert ratio < 3, (
        f"--accent {accent} now measures {ratio:.2f}:1 on the field's brightest point -- "
        "it clears the 3:1 floor, so the reason the ring is split no longer holds and "
        "the comment at the foot of itms.css must be rewritten, not left"
    )

    assert "--accent" not in {colour for row in FOCUS_RING_ON_FIELD for colour in row[:2]}, (
        f"--accent is in the field's ring table, and it measures {ratio:.2f}:1 there"
    )

    # `outline[^;:]*:` so this reads OUTLINE declarations rather than any rule
    # that happens to mention both words.
    offenders = [
        selector
        for selector, body in _rules()
        if re.search(r"outline[^;:]*:\s*[^;]*var\(--accent\)", body)
        and not selector.startswith("#kc-content")
    ]
    assert offenders == [], (
        f"{offenders} paint an outline in --accent but are not inside #kc-content, "
        f"where it is {_contrast(accent, _colour('--surface')):.2f}:1; out on the field "
        f"it is {ratio:.2f}:1"
    )


def test_the_safe_ring_is_the_default_rather_than_the_exception() -> None:
    """The inversion itself, asserted as structure rather than as a ratio.

    The table above proves white is readable on the field. It does NOT prove
    the field actually gets it -- that depends on which rule is the default
    and which is scoped, and the whole defect this replaced was a
    correct-looking set of colours arranged the wrong way round.

    `#kc-content` and not the outer wrapper is load-bearing: this rule selects
    DESCENDANTS, and `.itms-login` (`kcLoginClass`) wraps the field too.
    """
    rules = dict(_rules())

    default = rules.get(":focus-visible")
    assert default, "no page-wide :focus-visible rule; the field's ring is unaccounted for"
    assert "var(--accent-ink)" in default, (
        f"the page-wide ring is {default.strip()} -- the DEFAULT is what an element "
        "added above the card inherits, so it has to be the colour that is safe there"
    )

    scoped = rules.get("#kc-content :focus-visible")
    assert scoped, "the accent ring is not scoped to the card; either it is loose or it is gone"
    assert "var(--accent)" in scoped, scoped
    for outer in (
        ".itms-login :focus-visible",
        ".login-pf-page :focus-visible",
        "html :focus-visible",
    ):
        assert outer not in rules, (
            f"{outer} is an ancestor of the field, so it hands the accent back to it"
        )


@pytest.mark.parametrize(("token", "against", "floor", "job"), REFUSED)
def test_the_refused_colours_really_fail(token: str, against: str, floor: float, job: str) -> None:
    """THE ROW WITH TEETH. The stylesheet rests two design decisions on
    refusals: the accent is not text or a ring on the field (v3's Two Grounds
    Rule), and the hairline is not a control's boundary. A refusal recorded
    only as prose is one the next editor can undo without noticing, because
    nothing disagrees with them. So this measures the refusal: if the ratio
    ever clears its floor, the reason is gone and the comment must be
    rewritten rather than left standing."""
    ratio = _contrast(_colour(token), _colour(against))
    assert ratio < floor, (
        f"{token} {_colour(token)} now measures {ratio:.2f}:1 for {job} -- it clears the "
        f"{floor}:1 floor, so the reason for refusing it no longer holds"
    )


def test_the_field_paints_only_the_rail_tones() -> None:
    """The refusal of the accent on the field, held as structure: every
    `color:` an on-field rule declares names one of the two tones the
    light-on-dark table grades. A literal, or the accent, or a card tone,
    would be a colour the table cannot see -- and it is a lesson the console's
    equivalent guard learned the hard way, when a rail label was a hex literal
    with its ratio in a comment beside it and the comment was a lie."""
    graded = {row[0] for row in LIGHT_TEXT_ON_DARK if row[1] in (FIELD_BRIGHT, FIELD_DARK)}
    seen: set[str] = set()
    for selector, body in _rules():
        if not selector.startswith(ON_FIELD):
            continue
        for value in re.findall(r"(?<![-\w])color:\s*([^;]+);", body):
            seen.add(selector)
            name = re.fullmatch(r"var\((--[\w-]+)\)", value.strip())
            assert name, f"{selector} paints {value.strip()} rather than a token"
            assert name.group(1) in graded, (
                f"{selector} paints {name.group(1)} on the field, which the light-on-dark "
                f"table does not grade: {sorted(graded)}"
            )
    for selector in (
        "#kc-header-wrapper",
        "#kc-page-title",
        "#kc-attempted-username",
        "#reset-login",
    ):
        assert selector in seen, (
            f"{selector} declares no colour; the field's text is unaccounted for"
        )


def test_the_card_never_paints_the_rail_tone() -> None:
    """The refusal of `--rail-ink` on the card, held as structure. `REFUSED`
    measures it at 3.13:1 on `--surface`, under the 4.5:1 text floor; this is
    the half that says nothing on the card actually paints it. Every rule
    whose selector is not one of the on-field siblings is a descendant of the
    card (or the page's own ground, which the card overrides), so a `color:`
    naming the rail's text tone there is a light-on-dark tone on a white
    surface -- and a comment beside it saying otherwise would be a lie."""
    offenders = [
        selector
        for selector, body in _rules()
        if not selector.startswith(ON_FIELD)
        and any(
            re.fullmatch(r"var\(--rail-ink\)", value.strip())
            for value in re.findall(r"(?<![-\w])color:\s*([^;]+);", body)
        )
    ]
    assert offenders == [], (
        f"{offenders} paint --rail-ink off the field, where it measures 3.13:1 on "
        f"--surface and fails the 4.5:1 text floor"
    )


def test_no_control_is_bounded_by_a_hairline() -> None:
    """The refusal of the hairline as a boundary, held as structure. `--line`
    measures under 3:1 on white (the table above proves it) and is copied for
    the card's edge, the divider and the footnote's rule -- decoration, which
    SC 1.4.11 exempts. A control rule that borrowed it would look finished and
    fail the criterion; `--line-2` is not copied here in any form."""
    assert "--line-2" not in THEME_TOKENS
    offenders = [
        selector
        for selector, body in _rules()
        if selector.startswith(CONTROLS)
        and re.search(r"border(?:-color)?:\s*[^;]*var\(--line\)", body)
    ]
    assert offenders == [], f"{offenders} draw a control's boundary in --line"
    bounded = [
        selector
        for selector, body in _rules()
        if selector.startswith(CONTROLS)
        and re.search(r"border:\s*1px solid var\(--(line-control|accent)\)", body)
    ]
    assert len(bounded) >= 3, f"the controls are not bounded where this expects: {bounded}"


def _declarations(body: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for declaration in body.split(";"):
        if ":" not in declaration:
            continue
        prop, value = declaration.split(":", 1)
        out[prop.strip()] = re.sub(r"\s+", " ", value.strip())
    return out


def _console_kit_rule(selector: str) -> dict[str, str]:
    kit = _CSS_COMMENT.sub("", CONSOLE_KIT_CSS.read_text())
    for found, body in _rules(kit):
        if found.split(",")[0].strip() == selector:
            return _declarations(body)
    raise AssertionError(f"components.css has no {selector} rule")


def _theme_rule(selector: str, prop: str) -> dict[str, str]:
    for found, body in _rules():
        declarations = _declarations(body)
        if found.split(",")[0].strip() == selector and prop in declarations:
            return declarations
    raise AssertionError(f"itms.css has no {selector} rule declaring {prop}")


#: (theme selector, console kit selector, the properties that make the shape).
SAME_SHAPE_AS_THE_CONSOLE: list[tuple[str, str, tuple[str, ...]]] = [
    (".itms-primary", ".btn", ("padding", "border-radius", "font")),
    (".itms-primary", ".btn.pri", ("background", "border-color", "color")),
    (".itms-social-link", ".btn", ("padding", "border-radius", "font", "background", "color")),
    (".itms-social-link:hover", ".btn:hover", ("background", "border-color")),
    (".itms-field input", ".inp", ("padding", "border-radius", "min-height", "background")),
    (".alert-error", ".notice", ("padding", "border-radius", "font-size", "gap")),
    (".itms-pill", ".pill", ("padding", "border-radius", "font-size", "font-weight", "gap")),
]


@pytest.mark.parametrize(("theme_selector", "kit_selector", "props"), SAME_SHAPE_AS_THE_CONSOLE)
def test_the_theme_draws_the_consoles_shapes(
    theme_selector: str, kit_selector: str, props: tuple[str, ...]
) -> None:
    """The button, the field, the notice and the pill are the console kit's
    shapes, transcribed -- and "transcribed" is checked against the kit rather
    than remembered. `.btn.pri`'s edge is `border-color` in the kit and part of
    a `border` shorthand here, so the edge is compared through the shorthand."""
    kit = _console_kit_rule(kit_selector)
    theme = _theme_rule(theme_selector, props[0])
    for prop in props:
        expected = kit.get(prop)
        assert expected is not None, f"{kit_selector} declares no {prop} in components.css"
        actual = theme.get(prop)
        if actual is None and prop == "border-color":
            actual = theme.get("border", "").split(" ", 2)[-1]
        assert actual == expected, (
            f"{theme_selector} {prop} is {actual!r}; {kit_selector} draws {expected!r}"
        )


def test_no_plausible_secret_appears_anywhere_in_the_realm_file() -> None:
    """§5.4. This file is in git, so a secret committed here is a credential in
    every clone, every fork and every CI cache -- and rotating it means
    rewriting history rather than editing a file.

    Deliberately broader than any one field: the next person to add an identity
    provider will not add it under the key this test would otherwise name.
    """
    suspects = re.findall(
        r'"(?:clientSecret|secret|password)"\s*:\s*"([^"]{8,})"', REALM_PATH.read_text()
    )
    assert not suspects, suspects


# --------------------------------------------------------------------------
# Passkeys (spec §3.2). What the realm file can carry; the browser flow that
# offers them is a runbook step, for the reason in the spec's §7.4.
# --------------------------------------------------------------------------


def test_passwordless_webauthn_is_configured_as_a_first_factor() -> None:
    """`webAuthnPolicy*` with no infix is the SECOND-factor policy.

    The reference screen asks for "sign in with a passkey" as a first factor,
    which is the PASSWORDLESS policy. Setting the other one produces a realm
    where passkeys work only after a password -- the feature appears present
    and does a different thing, which is the hardest kind of wrong to notice.
    """
    assert REALM.get("webAuthnPolicyPasswordlessRpEntityName") == "ITMS"
    assert REALM.get("webAuthnPolicyPasswordlessSignatureAlgorithms") == [
        "ES256",
        "RS256",
    ]
    assert REALM.get("webAuthnPolicyPasswordlessAttestationConveyancePreference") == "none"
    assert REALM.get("webAuthnPolicyPasswordlessRequireResidentKey") == "Yes"


def test_user_verification_is_required_because_it_is_the_second_factor() -> None:
    """The policy decision, pinned rather than left as prose.

    A passwordless passkey with user verification required IS two factors --
    the authenticator, plus the biometric or PIN that unlocks it -- which is
    why a passkey sign-in does not additionally prompt for TOTP. Relax this to
    `preferred` and the passkey quietly becomes ONE factor while the flow that
    skips TOTP stays exactly as it was. Nothing else would notice.
    """
    assert REALM["webAuthnPolicyPasswordlessUserVerificationRequirement"] == "required"


def test_an_operator_can_enrol_a_passkey_themselves() -> None:
    actions = {a["alias"]: a for a in REALM["requiredActions"]}
    assert "webauthn-register-passwordless" in actions, sorted(actions)
    action = actions["webauthn-register-passwordless"]
    assert action["enabled"] is True
    # OPTIONAL, not default. `defaultAction: true` fires for every account at
    # next login, and an operator with no authenticator to hand is then locked
    # out of the console by a feature whose whole purpose is another way IN.
    assert action["defaultAction"] is False


def test_totp_is_still_a_default_action() -> None:
    """Non-regression for the factor that exists today: adding passkeys must
    not quietly remove the MFA every password account currently gets."""
    actions = {a["alias"]: a for a in REALM["requiredActions"]}
    assert actions["CONFIGURE_TOTP"]["defaultAction"] is True


# --------------------------------------------------------------------------
# Microsoft / Entra (spec §3.3). Three failure modes, each with its own test,
# because all three produce a WORKING login and a wrong outcome.
# --------------------------------------------------------------------------


def _microsoft() -> dict[str, Any]:
    providers = {p["alias"]: p for p in REALM.get("identityProviders", [])}
    assert "microsoft" in providers, sorted(providers)
    provider: dict[str, Any] = providers["microsoft"]
    return provider


def test_the_microsoft_provider_exists_and_carries_no_secret() -> None:
    """§5.4, at the field the secret would actually be typed into.

    `test_no_plausible_secret_appears_anywhere_in_the_realm_file` is the sweep;
    this is the specific one, so a red result names the provider rather than a
    regex match.
    """
    provider = _microsoft()
    assert provider["providerId"] == "oidc"
    assert provider["config"].get("clientSecret", "") == "", provider["config"]


def test_a_federated_login_is_forced_to_carry_an_org_claim() -> None:
    """§5.3, and the one `ITS_MULTI_TENANT=false` actively HIDES.

    Without this mapper a federated user arrives with no `org` attribute, the
    API falls back to `ITS_DEFAULT_ORG_SLUG`, the login SUCCEEDS, and every
    Microsoft account silently lands in the default org. Fine with one org; a
    tenancy hole the moment there are two -- so the mapper is required now,
    while the mistake is invisible and cheap to fix.

    NOTE what this does NOT prove: that a real token carries the claim. That
    needs an Entra tenant and a browser. This asserts the mapper is committed;
    `deploy/README.md` tells the operator to decode the token once.

    READS THE TOP-LEVEL `identityProviderMappers`, which is where Keycloak
    actually keeps these. It used to read `_microsoft()["mappers"]` -- a nested
    array Keycloak has no field for -- so this test passed against a realm file
    Keycloak could not load at all, and went on passing while the container
    crash-looped in production. A guard that reads an invented location
    confirms the invention.
    """
    mappers = {
        m["name"]: m
        for m in REALM.get("identityProviderMappers", [])
        if m.get("identityProviderAlias") == "microsoft"
    }
    assert "org" in mappers, sorted(mappers)
    config = mappers["org"]["config"]
    assert config["user.attribute"] == "org"
    assert config["attribute.value"], "the mapper must SET a value, not just name one"


def test_account_linking_cannot_be_silent() -> None:
    """§5.7. Auto-linking a Microsoft identity to a local account whose email
    address it merely claims is an account-takeover primitive: whoever controls
    that name at the IdP inherits the local account's roles, including
    `org-admin`. `trustEmail: false` plus the review-profile step means a match
    prompts instead."""
    provider = _microsoft()
    assert provider.get("trustEmail") is False
    assert provider.get("firstBrokerLoginFlowAlias") == "first broker login"


# --------------------------------------------------------------------------- #
# Forwarding Keycloak's credential events: the operator's half (§4, §7.1)
# --------------------------------------------------------------------------- #

README = REPO / "deploy/README.md"
ENV_EXAMPLE = REPO / "deploy/.env.example"


def _forwarding_section() -> str:
    """The runbook section, isolated.

    Isolated because the file is 1,200 lines and `view-events` or
    `KEYCLOAK_ADMIN_PASSWORD` appearing SOMEWHERE in it would satisfy a
    whole-file grep while the section that matters said something else."""
    text = README.read_text()
    start = text.index("## Forwarding Keycloak's credential events")
    end = text.index("\n## ", start + 1)
    return text[start:end]


def test_the_runbook_section_is_where_this_thinks_it_is() -> None:
    """Non-vacuity, same reason as the realm file above: every assertion below
    reads this slice."""
    assert README.is_file(), README
    section = _forwarding_section()
    assert len(section) > 1000, len(section)


def test_the_operator_is_told_to_scope_the_credential_to_view_events() -> None:
    """THE ONE THAT MATTERS. A credential that can only read an event log has a
    worst case of an information leak; `realm-admin` -- or the bootstrap admin
    from `.env`, which GO-LIVE tells the operator to delete -- has a worst case
    of every account in the realm."""
    section = _forwarding_section()
    assert "view-events" in section
    assert "realm-management" in section
    assert "KEYCLOAK_ADMIN_PASSWORD" in section, (
        "the runbook must name the account NOT to use; a reader who is not "
        "told will reach for the admin credentials they already have"
    )


def test_the_operator_is_told_the_event_log_is_off_by_default() -> None:
    """WITHOUT THIS THE FEATURE HAS NOTHING TO READ and sits at "configured,
    zero events" for ever -- a status page that looks healthy next to a chain
    that stays empty. It is the single likeliest way this deployment goes
    wrong, because nothing in the platform can detect it."""
    section = _forwarding_section()
    assert "Realm settings → Events" in section
    assert "off by default" in section.lower()


def test_the_secret_is_configured_as_a_path_and_never_inlined() -> None:
    """Same rule as `ITS_AUDIT_HMAC_KEY_PATH`: an env var holding a credential
    is readable by `docker inspect`, lands in shell history, and is copied into
    every process's environment."""
    env = ENV_EXAMPLE.read_text()
    assert "ITS_KEYCLOAK_EVENTS_SECRET_PATH=/var/lib/its/secrets/" in env
    assert "ITS_KEYCLOAK_EVENTS_SECRET=" not in env, (
        "a setting holding the secret itself, rather than its path"
    )


def test_the_runbook_says_what_forwarding_does_not_make_true() -> None:
    """A runbook that only says what a feature achieves leaves the reader with
    a stronger belief than the feature earns. Keycloak's own log remains the
    source, and a Keycloak compromised before a sweep runs can drop an event
    this will never see."""
    section = _forwarding_section()
    assert "does not make true" in section
    assert "live Keycloak" in section, (
        "the section must say plainly that none of this has run against a real "
        "Keycloak -- a reader who is not told will assume it has"
    )


# --------------------------------------------------------------------------- #
# The 2026-08-19 RBAC / user-lifecycle cycle: the operator's half.
# `docs/superpowers/specs/2026-08-19-rbac-and-user-lifecycle-design.md`
# §2.1 (the provisioning credential), §2.3 (SMTP), §3.3 (the lockout), §4.
#
# Same shape and the same reasoning as `_forwarding_section` above: each guard
# reads ONE isolated section, because `manage-users` or `preflight` appearing
# SOMEWHERE in a 2,500-line file would satisfy a whole-file grep while the
# section an operator actually reads said something else entirely.
# --------------------------------------------------------------------------- #

COMPOSE = REPO / "deploy/docker-compose.yml"
SPEC = REPO / "docs/modules/console-auth/spec.md"


def _readme_section(heading: str) -> str:
    """One `## ` section of the runbook, from its heading to the next one."""
    text = README.read_text()
    start = text.index(heading)
    try:
        end = text.index("\n## ", start + 1)
    except ValueError:  # last section in the file
        end = len(text)
    return text[start:end]


SMTP_HEADING = "## Keycloak SMTP"
PROVISIONING_HEADING = "## The provisioning client"
DOMAIN_HEADING = "## The email-domain allowlist"


def test_the_three_new_runbook_sections_are_where_this_thinks_they_are() -> None:
    """Non-vacuity, for the same reason `_forwarding_section` has one: every
    assertion below reads a slice, and `str.index` raising is a worse failure
    message than an assertion naming the missing section."""
    for heading in (SMTP_HEADING, PROVISIONING_HEADING, DOMAIN_HEADING):
        assert heading in README.read_text(), heading
        assert len(_readme_section(heading)) > 700, heading


# --- SMTP (spec §2.3) ------------------------------------------------------ #


def test_the_smtp_section_says_user_creation_does_not_work_without_it() -> None:
    """WITHOUT SMTP, CREATING A USER PRODUCES AN ACCOUNT NOBODY CAN EVER SIGN
    INTO. The account has no credential, the enrolment link is delivered only
    by email, and Keycloak 26 exposes no admin API that returns that link for
    an operator to pass on by hand. An operator who reads this section as
    optional will discover that after creating users, not before."""
    section = _readme_section(SMTP_HEADING)
    assert "Realm settings" in section and "Email" in section
    assert "no credential" in section.lower()
    assert "no admin api" in section.lower(), (
        "the section must say the link cannot be retrieved by hand -- otherwise "
        "the obvious workaround looks available"
    )


def _smtp_password_is_empty(realm: dict[str, Any]) -> bool:
    """True iff the realm carries no `smtpServer`, or one with no password.

    SCOPED TO THE BLOCK, which the first version of this guard was not. It
    read `'"password": ""' in realm_text` -- a whole-FILE substring test, so
    any unrelated block anywhere in the JSON gaining an empty `password` would
    have satisfied it while the SMTP block carried a real one. It also never
    ran: the left half of its `or` short-circuits, because there is no
    `smtpServer` in the file and no `password` key anywhere in it either.

    There is no `"password": ""` precedent in this realm to lean on. The
    Microsoft identity provider ships `"clientSecret": ""` -- a different key
    -- which is why `test_the_microsoft_provider_exists_and_carries_no_secret`
    is written against `clientSecret` and this one has to stand on its own.

    Keycloak nests the credential under `smtpServer.password`; both an absent
    key and a whitespace-only value are "no password committed".
    """
    smtp = realm.get("smtpServer")
    if not smtp:
        return True
    return not str(smtp.get("password", "")).strip()


def test_the_smtp_guard_can_actually_fail() -> None:
    """The falsification, because the real realm makes the guard below vacuous.

    `realm-it-suite.json` has no `smtpServer` today, so asserting over it says
    nothing about what happens when somebody adds one -- which is the entire
    scenario the guard exists for. Running the predicate against the shape a
    future edit would introduce is what turns "currently true" into "would go
    red". If this passes and the next one is green, the next one is green for
    a reason.
    """
    assert not _smtp_password_is_empty(
        {"smtpServer": {"host": "smtp.toeshee.com", "auth": "true", "password": "hunter2"}}
    )
    assert _smtp_password_is_empty({"smtpServer": {"host": "smtp.toeshee.com"}})
    assert _smtp_password_is_empty({})


def test_the_smtp_section_refuses_to_put_the_password_in_the_realm_json() -> None:
    """The same argument the Entra section already makes, at the second field
    somebody will reach for. `realm-it-suite.json` is in git: a secret there is
    a credential in every clone and every CI cache."""
    section = _readme_section(SMTP_HEADING)
    assert "realm-it-suite.json" in section
    assert "git" in section
    assert _smtp_password_is_empty(REALM), (
        "if the realm file ever carries an smtpServer block, its password must "
        "be empty -- the SMTP password is typed into the running realm's Email "
        "tab, never committed"
    )


# --- The provisioning credential (spec §2.1) ------------------------------- #


def test_the_provisioning_client_is_separate_from_the_events_client() -> None:
    """Two credentials with wildly different worst cases. One reads an event
    log; the other can create any user and reset any password in the realm. A
    single client holding both means the smaller job carries the bigger blast
    radius for ever."""
    section = _readme_section(PROVISIONING_HEADING)
    assert "it-suite-provisioning" in section
    assert "it-suite-events" in section, (
        "the section must name the OTHER client, or 'separate' is a word with "
        "nothing on the other side of it"
    )
    assert "realm-management" in section
    assert "manage-users" in section


def test_the_provisioning_section_states_the_cost_without_softening_it() -> None:
    """THE ONE THAT MATTERS, and it is a disclosure rather than a control.

    `manage-users` has no finer split in Keycloak. The credential is broader
    than the four operations the code calls, and no wording changes that -- so
    the runbook says so in the operator's own terms rather than implying the
    grant is narrow. Spec §2.1 and §6."""
    section = _readme_section(PROVISIONING_HEADING)
    lowered = section.lower()
    assert "no finer split" in lowered
    assert "reset any password" in lowered
    assert "create any user" in lowered
    assert "every identity" in lowered, (
        "the plain statement -- whoever owns ITMS owns every identity in the "
        "realm -- is the sentence this section exists to make unavoidable"
    )
    assert "impersonat" in lowered, (
        "impersonation is also inside manage-users and is never called; a "
        "reader auditing the grant will look for it"
    )


def test_the_provisioning_secret_is_a_path_and_never_inlined() -> None:
    """Same rule as the events secret and the audit HMAC key: an env var
    holding a credential is readable by `docker inspect`, lands in shell
    history, and is copied into every process's environment."""
    env = ENV_EXAMPLE.read_text()
    assert "ITS_KEYCLOAK_ADMIN_SECRET_PATH=/var/lib/its/secrets/" in env
    assert "ITS_KEYCLOAK_ADMIN_SECRET=" not in env, (
        "a setting holding the secret itself, rather than its path"
    )
    assert "ITS_KEYCLOAK_ADMIN_CLIENT_ID=it-suite-provisioning" in env


def test_the_env_example_ships_a_secret_path_something_actually_writes() -> None:
    """`.env.example` AND `from_settings` MUST AGREE, and what makes them agree
    CHANGED UNDER THIS TEST.

    The original assertion was the exact inverse: ship the path COMMENTED OUT.
    That was right at the time. `from_settings` returns `None` — the documented
    "not configured, routes answer 503" branch — only when the URL *or* the
    path is falsy; the URL is shared with the event sweep, so on any deployment
    forwarding events it is already set, and a populated path with no file
    behind it raised `FileNotFoundError` on every lifecycle request instead.
    Commenting it out was the only way to reach the branch the file promised.

    THE FACT THAT CHANGED: `deploy/docker-compose.yml`'s `keycloak-init` job now
    creates the provisioning client and writes that secret on every
    `docker compose up`. So the path shipped populated is a path something
    actually writes — and shipped COMMENTED it is now a first-boot failure, the
    only boot where it matters: `POST /api/v1/setup/claim` answers 503 and the
    buyer's very first action fails.

    The invariant this test has always defended is unchanged — the file must not
    promise a state the code cannot reach — so the assertion inverted while the
    reason stayed put. It is pinned to the INIT JOB rather than to a literal, so
    if that job stops writing the secret, this fails instead of the operator's
    install.
    """
    env = ENV_EXAMPLE.read_text()
    active = re.search(r"^ITS_KEYCLOAK_ADMIN_SECRET_PATH=(\S+)", env, re.MULTILINE)
    assert active is not None, (
        "ship this ACTIVE: `keycloak-init` writes the secret on every start, and "
        "commented out it makes a fresh install refuse its own setup screen"
    )
    # Composed from the job's OWN constants rather than matched as a literal:
    # `init-clients.py` builds the path as `secrets_dir / secret_filename`, so
    # the full string never appears in its source and a substring check would
    # fail against a job that writes exactly the right file.
    init_job = (REPO / "deploy/keycloak/init-clients.py").read_text()
    directory = re.search(r'DEFAULT_SECRETS_DIR = Path\("([^"]+)"\)', init_job)
    filename = re.search(r'secret_filename="(keycloak-admin[^"]*)"', init_job)
    assert directory and filename, (
        "`init-clients.py` no longer declares DEFAULT_SECRETS_DIR and the "
        "provisioning client's secret_filename in the shape this reads; "
        "re-derive the binding rather than deleting it"
    )
    written = f"{directory.group(1).rstrip('/')}/{filename.group(1)}"
    assert active.group(1) == written, (
        f"`.env.example` points at {active.group(1)} and `init-clients.py` "
        f"writes {written} — the path and its writer must name the same file, "
        "or first boot fails on a secret nothing created"
    )
    source = KC_CLIENT.read_text()
    assert "if not url or not secret_path:" in source, (
        "the reasoning above is about this exact condition; if it changed, "
        "re-derive what .env.example should ship"
    )
    assert "FileNotFoundError" in source


def _env_block(start: str, end: str) -> str:
    """One block of `.env.example`, between two of its comment headers.

    SCOPED FOR THE SAME REASON `_readme_section` IS, and this one was written
    after the whole-file version of the guard below turned out to be green for
    free: `.env.example` mentions `503` and `FileNotFoundError` several
    hundred lines away, about the TURN hosted provider. A whole-file check was
    therefore satisfied by text about a different setting entirely -- the same
    defect as the SMTP guard above, found the same way.
    """
    text = ENV_EXAMPLE.read_text()
    lo = text.index(start)
    return text[lo : text.index(end, lo)]


PROVISIONING_BLOCK = ("# User lifecycle: the PROVISIONING client", "# The console email")


def test_the_provisioning_block_is_where_this_thinks_it_is() -> None:
    """Non-vacuity: `str.index` raising is a worse message than an assertion."""
    text = ENV_EXAMPLE.read_text()
    for marker in PROVISIONING_BLOCK:
        assert marker in text, marker
    assert len(_env_block(*PROVISIONING_BLOCK)) > 500


def test_the_env_example_says_what_a_configured_but_missing_secret_does() -> None:
    """The three-way contract, in the operator's own file.

    Unset is a supported state (503, named setting). Set-and-present works.
    Set-and-MISSING raises, deliberately -- that operator believes provisioning
    is running. A file documenting only the first two leaves the third looking
    like a bug in ITMS, which is what the commented-out line above now depends
    on the reader understanding.
    """
    block = _env_block(*PROVISIONING_BLOCK)
    assert "503" in block
    assert "FileNotFoundError" in block, (
        "the block must say that a configured-but-missing secret raises rather "
        "than falling back to 'not configured'"
    )
    assert "shared" in block.lower(), (
        "and why the 'not configured' branch is unreachable once the URL is "
        "set -- the URL is shared with the event sweep"
    )


KC_CLIENT = REPO / "backend/app/modules/user_admin/kc_client.py"
DESIGN = REPO / "docs/superpowers/specs/2026-08-19-rbac-and-user-lifecycle-design.md"

#: Small enough that spelling them out beats a dependency.
_SPELLED = {2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven"}


def test_every_document_that_counts_the_admin_operations_counts_the_same() -> None:
    """A NUMBER REPEATED IN THREE PLACES DRIFTS IN TWO OF THEM.

    "The credential is broader than the code" is the whole mitigation argument
    for `manage-users` (design §2.1, §6). Its force comes from the code half
    being small and *known*, so a document claiming five operations against a
    client that performs four is not a rounding error -- it is the mitigation
    describing something other than what shipped, and the direction of the
    error is the one that overstates the blast radius nobody re-derives.

    DERIVED FROM THE SOURCE, not from a second hardcoded number: the count is
    the module-level public coroutines in `kc_client.py`, so adding a fifth
    operation goes red on every document that still says four.
    """
    module = ast.parse(KC_CLIENT.read_text())
    operations = sorted(
        node.name
        for node in module.body
        if isinstance(node, ast.AsyncFunctionDef) and not node.name.startswith("_")
    )
    spelled = _SPELLED[len(operations)]
    assert operations == [
        "create_user",
        "delete_user",
        "execute_actions_email",
        # Added 2026-10-14: the MCP server's revoke removes one AI app's
        # consent for one person (MCP server design §3.4).
        "revoke_consent",
        "set_enabled",
        # Added 2026-10-14: `PATCH /me` mirrors the caller's own language.
        "set_locale_attribute",
    ], operations

    def flat(path: Path) -> str:
        return re.sub(r"\s+", " ", path.read_text())

    assert f"performs exactly {spelled} operations" in flat(KC_CLIENT)
    assert f"calls exactly {spelled}" in flat(DESIGN), (
        "design §2.1's mitigation list must count what the client actually does"
    )
    assert f"calls exactly {spelled} operations" in flat(README)
    for name in operations:
        assert name != "impersonate", "invariant: impersonation is never called"


# --- The email-domain allowlist (spec §3.3) -------------------------------- #


def test_the_domain_section_documents_the_preflight_as_a_required_step() -> None:
    """THE ONLY WAY TO LEARN THE COST BEFORE PAYING IT. Turning the allowlist
    on refuses every token whose email fails the rule at all three doors,
    including the operator's own -- and the console is the thing that just
    stopped admitting them, so there is no in-console recovery."""
    section = _readme_section(DOMAIN_HEADING)
    assert "scripts/preflight_email_domain.py" in section
    assert (REPO / "scripts/preflight_email_domain.py").is_file(), (
        "the runbook names a script that must exist"
    )
    lowered = section.lower()
    assert "your own" in lowered or "own account" in lowered, (
        "the operator must be told the lockout includes THEM; a warning about "
        "'users' reads as being about other people"
    )


def test_the_domain_section_documents_the_break_glass() -> None:
    """Unset the variable and restart the api, because settings are read once
    at `create_app`. Without this sentence the recovery from a self-lockout is
    a guess made under pressure."""
    section = _readme_section(DOMAIN_HEADING)
    lowered = section.lower()
    assert "break-glass" in lowered
    assert "unset" in lowered
    assert "create_app" in section, (
        "the WHY of the restart: settings are read once at startup, so editing "
        ".env alone changes nothing"
    )
    assert "docker compose up -d api" in section


def test_the_domain_section_names_all_three_doors() -> None:
    """An allowlist enforced at one of three doors is not an allowlist (spec
    §0.3). The operator turning it on should know what it covers."""
    section = _readme_section(DOMAIN_HEADING)
    lowered = section.lower()
    assert "websocket" in lowered
    assert "tunnel" in lowered
    assert "4403" in section, (
        "the distinct WebSocket close code, so 'wrong domain' stays "
        "distinguishable from 'bad token' on the wire"
    )


DEPS = REPO / "backend/app/api/deps.py"


def test_the_runbook_does_not_promise_an_email_refresh_the_code_does_not_do() -> None:
    """THE RUNBOOK MUST NOT INSTRUCT SOMETHING IMPOSSIBLE.

    The exit-`1` recovery used to read "sign those users in once so ITMS
    refreshes the stored address". No such refresh exists: `app/api/deps.py`
    writes `email` only in its `if user is None` branch and never updates it
    again (design §7.2, deferred to migration 0066). An operator following
    that step re-runs the preflight for ever and it keeps saying `1`.

    THIS GUARD READS THE CODE, deliberately, so it points the other way too --
    and it fired. §7.2 landed in migration `0066`, `deps.py` now refreshes the
    address on every admission, and this test went red on the commit that did
    it, which is exactly what it was for. Both branches are live: revert the
    refresh and the runbook must go back to promising nothing.

    THE RESIDUAL LAG IS STILL REAL and the runbook must keep saying so. The
    refresh happens on ADMISSION, so the stored row converges on the claim only
    when the user signs in. "Never refreshes" became "refreshes at each
    sign-in", which is a narrower window and not a closed one -- and a runbook
    claiming the two sources now agree would be the same class of overstatement
    in the opposite direction.
    """
    source = DEPS.read_text()
    refreshes = re.search(r"^\s*user\.email\s*=", source, re.MULTILINE)
    section = _readme_section(DOMAIN_HEADING)
    if refreshes is None:
        assert "so ITMS refreshes the stored address" not in section, (
            "deps.py has no email refresh, so the runbook must not tell the "
            "operator to trigger one by signing the user in"
        )
        assert "0066" in section, (
            "the recovery step must name the deferred fix (§7.2 / migration "
            "0066), or the reader cannot tell a limitation from a typo"
        )
    else:
        assert "UPDATE users SET email" not in section, (
            "deps.py refreshes users.email now, so the runbook must not still "
            "tell the operator to correct the row by hand with SQL"
        )
        assert "never refreshes" not in section, (
            "the runbook still says the stored row never refreshes; §7.2 has "
            "landed and it does, on every admission"
        )
        assert "last sign-in" in section, (
            "the refresh happens on ADMISSION, so the row converges only when "
            "the user signs in. The runbook must keep stating that residual "
            "lag -- 'they now agree' is the same overstatement the other way"
        )


def test_the_domain_section_states_the_stored_row_live_claim_divergence() -> None:
    """The preflight and the gate read DIFFERENT SOURCES, and the section that
    tells an operator to trust the preflight has to say so.

    The gate tests `claims.email` -- the live value in the token. The preflight
    reads `users.email` -- a row written once at first sign-in. Until §7.2
    lands they can disagree in both directions: a corrected address passes the
    gate while the preflight still lists it, and a stale row can read clean
    while the live claim fails. "Exit 0 means safe" is only true if the reader
    knows which of the two it measured.
    """
    section = _readme_section(DOMAIN_HEADING)
    lowered = section.lower()
    assert "what this does not achieve" in lowered, section[:200]
    outcome = section[lowered.index("what this does not achieve") :]
    assert "stored" in outcome.lower()
    assert "claim" in outcome.lower()
    assert "§7.2" in outcome or "7.2" in outcome, (
        "the divergence must be attributed to the deferred fix, so a reader "
        "can tell when it stops being true"
    )


def test_the_break_glass_explains_why_a_blank_value_is_actually_safe() -> None:
    """A FALSE REASON IS WORSE THAN NO REASON, because the next person builds
    on it.

    The section used to say "Pydantic reads an empty string as unset". It does
    not: pydantic-settings yields `""`, and `console_email_domain` is then
    `""`, not `None`. The blank-not-deleted break-glass is safe for a
    different reason entirely -- `domain_refusal` strips the base and treats an
    empty or whitespace-only one as no restriction, which is the branch that
    also absorbs a stray trailing newline. Cite the thing that actually holds,
    so that changing it goes looking for this sentence.
    """
    section = _readme_section(DOMAIN_HEADING)
    assert "Pydantic reads an empty string as unset" not in section, (
        "pydantic-settings yields '' for a blank env line, not None"
    )
    assert "domain_refusal" in section, (
        "the break-glass must name the function whose empty-base branch is "
        "what makes a blank line safe"
    )
    source = (REPO / "backend/app/core/identity/email_domain.py").read_text()
    assert "def domain_refusal(" in source, (
        "the runbook cites a function that must exist under that name"
    )


def test_the_unset_default_is_documented_as_load_bearing() -> None:
    """`None` means no restriction. ANY other default refuses every token on
    every existing deployment the moment this ships -- a fleet-wide lockout
    delivered by an upgrade. `.env.example` must ship it EMPTY."""
    env = ENV_EXAMPLE.read_text()
    assert re.search(r"^ITS_CONSOLE_EMAIL_DOMAIN=\s*$", env, re.MULTILINE), (
        "the shipped default must be empty; a value here locks out every "
        "deployment that copies .env.example"
    )
    assert "no restriction" in env.lower()


# --- Spec §7.3: self-service passkey enrolment (spec §4 of the cycle) ------- #


def test_the_account_console_path_is_actually_routed_by_traefik() -> None:
    """The EVIDENCE behind the answer written into `console-auth` §7.3.

    `/realms/it-suite/account` is served only if Traefik routes `/realms` to
    Keycloak, and the console's own assets only if `/resources` is routed too.
    Narrow that rule and the documented answer becomes false with nothing else
    going red -- which is precisely why this reads the compose file rather than
    trusting the prose."""
    compose = COMPOSE.read_text()
    rule = next(line for line in compose.splitlines() if "traefik.http.routers.kc.rule" in line)
    assert "PathPrefix(`/realms`)" in rule, rule
    assert "PathPrefix(`/resources`)" in rule, rule


#: Markdown strikethrough, i.e. text the document has RETRACTED.
_STRUCK = re.compile(r"~~.*?~~", re.DOTALL)


def _spec_live_claims() -> str:
    """The spec minus everything it has struck through, whitespace normalised.

    TWO SEPARATE DEFECTS, BOTH OF WHICH MADE THE GUARD BELOW UNFALSIFIABLE.

    1. The forbidden sentence is LINE-WRAPPED in the source (`...is not
       currently\\n   routed by Traefik`), so a raw `in` test for
       "not currently routed by Traefik" never matched -- not against the
       current file, and not against the pre-answer one either. It was green
       for the reason a `grep` for a phrase that no line contains is green.
       Collapsing runs of whitespace to single spaces is what makes the search
       read the prose rather than the line breaks.
    2. The premise is still LITERALLY PRESENT in the current file, inside the
       `~~...~~` that retracts it -- which is correct: an answered question
       keeps its original wording so the answer has something to be an answer
       to. What must not survive is the premise as a LIVE claim. Stripping the
       struck text is what distinguishes "recorded and withdrawn" from
       "still asserted".
    """
    return re.sub(r"\s+", " ", _STRUCK.sub(" ", SPEC.read_text()))


def test_the_open_question_about_self_service_passkeys_is_answered() -> None:
    """§7.3 asked the question and told the next reader to VERIFY rather than
    assume. Leaving the hedge in place after verifying it is how an answered
    question gets re-investigated a year later.

    Falsified against the pre-answer wording (`git show
    cb9ba18:docs/modules/console-auth/spec.md`), where the premise stands
    un-struck: this goes red there and green here, which is the whole point.
    """
    live = _spec_live_claims()
    assert "not currently routed by Traefik" not in live, (
        "the open question's premise was wrong and must not survive the answer "
        "as a live claim; strike it through or delete it"
    )
    # NON-VACUITY for the stripping above: if `_STRUCK` ever swallowed the
    # document, the assertion would pass over an empty string. These two live
    # OUTSIDE the strikethrough, so they prove there is still a spec here.
    assert "ANSWERED 2026-08-19" in live
    assert "PathPrefix(`/realms`)" in live, (
        "the answer must quote the actual routing rule, not assert a conclusion"
    )


# --------------------------------------------------------------------------
# Schema shape. Added 2026-08-19, AFTER this file crash-looped a live Keycloak.
# --------------------------------------------------------------------------

#: Fields Keycloak 26's `IdentityProviderRepresentation` accepts. Copied from
#: the error it prints when it rejects one, which lists them all.
_IDP_FIELDS = frozenset(
    {
        "enabled",
        "firstBrokerLoginFlowAlias",
        "trustEmail",
        "linkOnly",
        "hideOnLogin",
        "providerId",
        "displayName",
        "organizationId",
        "alias",
        "postBrokerLoginFlowAlias",
        "addReadTokenRoleOnCreate",
        "authenticateByDefault",
        "internalId",
        "updateProfileFirstLogin",
        "updateProfileFirstLoginMode",
        "config",
        "storeToken",
    }
)


def test_no_identity_provider_carries_a_field_keycloak_will_reject() -> None:
    """THE ONE THAT COST A LIVE OUTAGE, written after it happened.

    `73a04ad` nested a `mappers` array INSIDE the Microsoft identity provider.
    Keycloak has no such field -- mappers are a top-level
    `identityProviderMappers` array -- so `--import-realm` died with
    "Unrecognized field 'mappers' ... not marked as ignorable" and the
    container crash-looped.

    WHY NOTHING HERE CAUGHT IT FOR TWO DAYS. Every other assertion in this file
    reads keys it expects and ignores keys it does not, so an INVENTED key is
    invisible to all of them. And realm import runs only against an EMPTY
    DATABASE, so a deployment that had already imported kept running happily on
    the realm in its database while the committed file was unloadable. The bug
    detonated on the next `docker compose up -d`, which recreated the
    container -- days after the commit, during an unrelated upgrade, with
    nothing pointing back at the change that caused it.

    The parse happens BEFORE Keycloak checks whether the realm already exists,
    which is why existing data does not save you.

    This asserts the shape of what is COMMITTED against the field list Keycloak
    itself printed while refusing it. It cannot verify semantics -- a valid
    field carrying nonsense still imports -- but the class of error that took
    the console down is a key Keycloak does not know, and that is checkable.
    """
    for idp in REALM.get("identityProviders", []):
        unknown = set(idp) - _IDP_FIELDS
        assert not unknown, (
            f"identity provider {idp.get('alias')!r} carries field(s) Keycloak "
            f"will refuse at import: {sorted(unknown)}. Mappers belong in the "
            f"realm's top-level 'identityProviderMappers' array."
        )


def test_the_org_mapper_survived_the_move_to_the_top_level() -> None:
    """Moving the mapper out must not LOSE it.

    Its absence is the failure `test_a_federated_login_is_forced_to_carry_an
    _org_claim` exists for: with `ITS_MULTI_TENANT=false` a federated user
    carrying no `org` attribute falls back to the default org, so sign-in
    WORKS and every Microsoft account silently lands in one tenant.
    """
    mappers = REALM.get("identityProviderMappers", [])
    org = [m for m in mappers if m.get("identityProviderAlias") == "microsoft"]
    assert org, "the microsoft org mapper is gone from identityProviderMappers"
    assert org[0]["identityProviderMapper"] == "oidc-hardcoded-attribute-idp-mapper"
    assert org[0]["config"]["user.attribute"] == "org"


# --------------------------------------------------------------------------- #
# The SECOND PORTAL, closed at the realm.
# `docs/superpowers/specs/2026-08-19-invisible-identity-layer-design.md` §1.2
# has two halves. `/admin` left the Traefik rule (see
# `backend/tests/test_public_surface.py`); this is the other half, and it needs
# the realm because the account console lives at
# `/realms/it-suite/account`, UNDER a prefix sign-in cannot do without.
#
# WHAT WAS VERIFIED AGAINST KEYCLOAK 26.0's OWN SOURCE, because none of it is
# observable from this suite and the alternative is guessing at a file that has
# already crash-looped a live console once:
#
#   * `RealmManager.importRealm` calls `setupAccountManagement(realm)` BEFORE
#     `RepresentationToModel.importRealm` creates the clients in the file, and
#     guards that call on `hasAccountManagementClient(rep)` -- which tests for
#     `account`, NOT for `account-console`. So a file that declares
#     `account-console` ALONE gets Keycloak's built-in copy created first and
#     then `realm.addClient("account-console")` on top of it: a duplicate
#     clientId, an aborted import, and a container that will not start. That is
#     why `account` is declared here as well; it is the switch that suppresses
#     the built-in pair, and `test_declaring_one_account_client_without_the
#     _other_breaks_the_import` is the guard that keeps them together.
#   * `AccountLoader.getAccountService` resolves ONLY the `account` client and
#     404s the whole subtree when it is missing or disabled. The console SPA
#     shell does not consult `account-console` at all -- it hands that client id
#     to the browser, which then asks the authorization endpoint for it and is
#     refused because the client is disabled. So a visitor to the account
#     console gets no account console; what they cannot get is a working second
#     portal, which is the property §1.2 is after.
#   * `account` STAYS ENABLED deliberately. `UserResource.executeActionsEmail`
#     -- the enrolment and password-reset mail this cycle's user lifecycle is
#     built on, sent by `kc_client.execute_actions_email` with NO `client_id`
#     parameter -- resolves its client through
#     `SystemClientUtil.getSystemClient`, which prefers an enabled `account`.
#     Disabling `account` is the tempting one-word change here and it moves the
#     credential mail onto a `_system` client Keycloak invents at first use.
#     Not on the strength of a source read, and not in the same cycle that
#     built the mail.
# --------------------------------------------------------------------------- #

#: Keycloak's own two built-in account clients, by the roles they play.
ACCOUNT_CLIENT = "account"
ACCOUNT_CONSOLE_CLIENT = "account-console"


def _clients(realm: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["clientId"]: c for c in realm.get("clients", [])}


def test_the_account_console_client_is_disabled() -> None:
    """THE SEAM THE WHOLE CYCLE EXISTS TO REMOVE.

    A buyer who reaches `/realms/it-suite/account` finds a second application
    with different chrome, its own navigation and the vendor's name on it --
    able to change the password, the MFA device and the email address that ITMS
    deliberately does not expose. Path routing cannot take it away: it sits
    under `/realms`, which sign-in needs.
    """
    console = _clients(REALM).get(ACCOUNT_CONSOLE_CLIENT)
    assert console is not None, (
        f"{ACCOUNT_CONSOLE_CLIENT} is not declared in the realm at all, so "
        "Keycloak creates its built-in copy ENABLED and the account console is "
        "reachable by URL"
    )
    assert console["enabled"] is False, console


def test_the_account_client_is_declared_and_left_enabled() -> None:
    """The other half of the pair, and the one it would be a mistake to switch
    off. See the section header: the admin API's `execute-actions-email` -- the
    only way a new user ever gets a credential -- resolves `account` as the
    system client for the action token it mails."""
    account = _clients(REALM).get(ACCOUNT_CLIENT)
    assert account is not None, (
        "declaring account-console without account makes Keycloak create BOTH "
        "built-ins before this file's clients are read, and the import then "
        "dies on a duplicate clientId"
    )
    assert account.get("enabled", True) is True, (
        "an enabled `account` client is what SystemClientUtil.getSystemClient "
        "returns for the enrolment mail; disabling it moves that mail onto an "
        "invented `_system` client"
    )


def _account_clients_are_consistent(realm: dict[str, Any]) -> bool:
    """True iff the realm declares NEITHER built-in account client or BOTH.

    The asymmetric case is the one that breaks: `account-console` alone means
    Keycloak's `setupAccountManagement` runs (it checks for `account`), creates
    both built-ins, and the file's own `account-console` then collides with the
    copy Keycloak just made.
    """
    declared = _clients(realm).keys()
    return (ACCOUNT_CLIENT in declared) == (ACCOUNT_CONSOLE_CLIENT in declared)


def test_the_consistency_predicate_can_actually_fail() -> None:
    """Falsification, because the committed realm satisfies the guard below and
    therefore says nothing about the shape that breaks."""
    assert not _account_clients_are_consistent({"clients": [{"clientId": "account-console"}]})
    assert not _account_clients_are_consistent({"clients": [{"clientId": "account"}]})
    assert _account_clients_are_consistent({"clients": []})
    assert _account_clients_are_consistent(
        {"clients": [{"clientId": "account"}, {"clientId": "account-console"}]}
    )


def test_declaring_one_account_client_without_the_other_breaks_the_import() -> None:
    """A CRASH-LOOP GUARD, not a style rule, and the same failure class as
    `test_no_identity_provider_carries_a_field_keycloak_will_reject`: the file
    parses, every other assertion here is green, and the container will not
    start on the next `docker compose up -d`."""
    assert _account_clients_are_consistent(REALM), sorted(_clients(REALM))


#: Fields Keycloak 26's `ClientRepresentation` accepts, restricted to the ones
#: this realm has any business setting. The list exists for the reason the
#: identity-provider one does -- an INVENTED key is invisible to every other
#: assertion in this file and fatal at import -- and it is deliberately not the
#: full representation: a key nobody here uses cannot be typo'd here either.
_CLIENT_FIELDS = frozenset(
    {
        "clientId",
        "name",
        "description",
        "enabled",
        "publicClient",
        "standardFlowEnabled",
        "implicitFlowEnabled",
        "directAccessGrantsEnabled",
        "serviceAccountsEnabled",
        "protocol",
        "rootUrl",
        "baseUrl",
        "adminUrl",
        "redirectUris",
        "webOrigins",
        "attributes",
        "protocolMappers",
        "fullScopeAllowed",
        "alwaysDisplayInConsole",
        "frontchannelLogout",
        "consentRequired",
        "bearerOnly",
        "defaultClientScopes",
        "optionalClientScopes",
    }
)


def test_no_client_carries_a_field_keycloak_will_reject() -> None:
    """The clients half of the schema guard the identity providers already have.

    `73a04ad` cost a live console because a nested key Keycloak has never heard
    of aborts `--import-realm` before it even looks at the realm. Clients are
    the biggest objects in this file and had no such guard.
    """
    for client in REALM.get("clients", []):
        unknown = set(client) - _CLIENT_FIELDS
        assert not unknown, (
            f"client {client.get('clientId')!r} carries field(s) that are not "
            f"in this file's accepted set: {sorted(unknown)}. If Keycloak really "
            f"accepts one, add it to _CLIENT_FIELDS with the reason."
        )


# --------------------------------------------------------------------------- #
# The required actions, which are the credential steps themselves.
#
# DECLARING `requiredActions` AT ALL SUPPRESSES KEYCLOAK'S DEFAULTS. Verified
# in 26.0's `DefaultExportImportManager.importRealm`:
#
#     if (rep.getRequiredActions() != null) { ...only what the file lists... }
#     else { DefaultRequiredActions.addActions(newRealm); }
#
# So this array is not an ADDITION to the built-in set, it REPLACES it -- and
# the imported realm had exactly two entries, neither of them UPDATE_PASSWORD.
# `AuthenticationManager.getApplicableRequiredAction` looks the alias up with
# `realm.getRequiredActionProviderByAlias`, gets null, logs "Requested action
# UPDATE_PASSWORD not configured for realm", sets `kc_action_status=error` and
# returns the user to ITMS having done NOTHING. That is the "Change password"
# button on the profile page -- the flagship of design §1.1 -- and the failure
# is silent, which is exactly what `lib/auth.ts`'s comment on the closed union
# says it would be.
#
# The mail path fails differently and worse: `execute_actions_email` passes the
# admin API's validator (`RequiredActionsValidator` checks the deployment's SPI
# factories, which are global, not the realm's registrations), so the enrolment
# mail is SENT -- and when the new user opens the link, the unregistered
# UPDATE_PASSWORD is filtered out of their required actions and they are signed
# in without ever setting a credential.
# --------------------------------------------------------------------------- #

FRONTEND_AUTH = REPO / "frontend/src/lib/auth.ts"


def _actions_the_console_starts() -> set[str]:
    """The `kc_action` values the browser sends, read from the closed union."""
    block = re.search(r"const ACCOUNT_ACTIONS:[^=]*=\s*\[(.*?)\]", FRONTEND_AUTH.read_text(), re.S)
    assert block, f"{FRONTEND_AUTH} no longer declares ACCOUNT_ACTIONS"
    return set(re.findall(r'"([^"]+)"', block.group(1)))


def _actions_the_backend_mails() -> set[str]:
    """The actions `kc_client` asks Keycloak to mail a link for.

    Read from the source rather than restated, so a fourth action added to
    either side lands in the realm or goes red here.
    """
    module = ast.parse(KC_CLIENT.read_text())
    literals: dict[str, str] = {}
    groups: dict[str, set[str]] = {}
    for node in module.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            name, value = node.targets[0].id, node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name, value = node.target.id, node.value
        else:
            continue
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            literals[name] = value.value
        elif isinstance(value, ast.Tuple):
            members = set()
            for element in value.elts:
                if isinstance(element, ast.Constant) and isinstance(element.value, str):
                    members.add(element.value)
                elif isinstance(element, ast.Name) and element.id in literals:
                    # e.g. ENROLMENT_ACTIONS reuses PASSKEY_REQUIRED_ACTION.
                    members.add(literals[element.id])
            groups[name] = members
    mailed = groups.get("ENROLMENT_ACTIONS", set()) | groups.get("PASSWORD_RESET_ACTIONS", set())
    assert mailed, f"{KC_CLIENT} no longer names the actions it mails"
    return mailed


def _enabled_actions(realm: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {a["alias"]: a for a in realm.get("requiredActions", []) if a.get("enabled") is True}


def test_the_action_sets_are_read_from_both_sides_and_are_not_empty() -> None:
    """Non-vacuity, first: the two extractors above are regex and `ast` over
    files that move. An empty set makes the guard below assert nothing over
    nothing, twice."""
    console, mailed = _actions_the_console_starts(), _actions_the_backend_mails()
    assert "UPDATE_PASSWORD" in mailed, sorted(mailed)
    assert "UPDATE_PASSWORD" in console, sorted(console)
    assert len(console) == 3, sorted(console)


def test_every_credential_step_itms_asks_for_exists_in_the_realm() -> None:
    """THE SILENT ONE. Every path in this platform that changes a credential
    names an action and asks Keycloak to run it; an action the realm never
    registered does not fail, it does nothing."""
    registered = _enabled_actions(REALM)
    for action in sorted(_actions_the_console_starts() | _actions_the_backend_mails()):
        assert action in registered, (
            f"{action} is asked for by ITMS and is not an enabled required "
            f"action in the realm. Declared: {sorted(registered)}. A kc_action "
            f"for it returns the user to ITMS with kc_action_status=error and "
            f"no visible failure."
        )


#: Keycloak 26's own default set (`DefaultRequiredActions.Action`), minus the
#: two entries gated on PREVIEW features -- see the next test.
_KEYCLOAK_DEFAULT_ACTIONS = frozenset(
    {
        "VERIFY_EMAIL",
        "UPDATE_PROFILE",
        "CONFIGURE_TOTP",
        "UPDATE_PASSWORD",
        "TERMS_AND_CONDITIONS",
        "delete_account",
        "delete_credential",
        "update_user_locale",
        "webauthn-register",
        "webauthn-register-passwordless",
        "VERIFY_PROFILE",
    }
)

#: `Profile.Feature.UPDATE_EMAIL` and `RECOVERY_CODES` are `Type.PREVIEW` in
#: 26.0, so `addActions` skips them and no SPI factory is loaded for them.
_PREVIEW_ONLY_ACTIONS = frozenset({"UPDATE_EMAIL", "CONFIGURE_RECOVERY_AUTHN_CODES"})


def test_the_realm_declares_the_whole_default_action_set() -> None:
    """Because declaring ANY suppresses ALL of them (section header above).

    This is stated as the full set rather than "the ones we use" on purpose: the
    next person to need VERIFY_EMAIL or a locale prompt would otherwise find it
    missing from a realm that looks like a stock one, and the admin console
    gives no hint that the list was ever replaced.
    """
    declared = {a["alias"] for a in REALM["requiredActions"]}
    missing = _KEYCLOAK_DEFAULT_ACTIONS - declared
    assert not missing, (
        f"the realm replaces Keycloak's default required actions and omits "
        f"{sorted(missing)}; each omission is an action that silently does "
        f"nothing when anything asks for it"
    )


def test_no_preview_gated_action_is_declared() -> None:
    """The opposite error, and it is not silent -- it is a 500.

    A registered action whose provider factory is not loaded reaches
    `AuthenticationManager.executeAction`, which throws "Unable to find factory
    for Required Action". Both of these are behind preview features that this
    deployment does not enable.
    """
    declared = {a["alias"] for a in REALM["requiredActions"]}
    assert not (declared & _PREVIEW_ONLY_ACTIONS), sorted(declared & _PREVIEW_ONLY_ACTIONS)


#: `RequiredActionProviderRepresentation`, whole -- it is a small object.
_ACTION_FIELDS = frozenset(
    {"alias", "name", "providerId", "enabled", "defaultAction", "priority", "config"}
)


def test_no_required_action_carries_a_field_keycloak_will_reject() -> None:
    """Same schema argument as the clients and the identity providers, now that
    this array is eleven objects rather than two."""
    for action in REALM["requiredActions"]:
        unknown = set(action) - _ACTION_FIELDS
        assert not unknown, f"{action.get('alias')!r} carries {sorted(unknown)}"


def test_the_password_step_runs_before_the_passkey_step_at_enrolment() -> None:
    """`ENROLMENT_ACTIONS` mails both in one link and its comment states the
    order: "set something you can sign in with, then enrol the passkey that
    replaces it". Keycloak orders required actions by PRIORITY, and ties are
    resolved by nothing at all -- the actions arrive from a map. Equal
    priorities mean the passkey prompt shows first roughly half the time."""
    by_alias = {a["alias"]: a for a in REALM["requiredActions"]}
    assert (
        by_alias["UPDATE_PASSWORD"]["priority"]
        < by_alias["webauthn-register-passwordless"]["priority"]
    ), "the enrolment mail's two steps have no deterministic order"


# --------------------------------------------------------------------------- #
# The runbook's half of the same decision.
# --------------------------------------------------------------------------- #

BRANDING_HEADING = "## Branding and extending sign-in (VENDOR-ONLY)"
BREAK_GLASS_HEADING = "## Break-glass: the identity admin console (VENDOR-ONLY)"


def test_the_identity_surface_sections_are_where_this_thinks_they_are() -> None:
    """Non-vacuity, same argument as `_readme_section`'s other callers."""
    for heading in (BRANDING_HEADING, BREAK_GLASS_HEADING):
        assert heading in README.read_text(), heading
        assert len(_readme_section(heading)) > 700, heading


def _instructions(section: str) -> str:
    """A runbook section with its blockquotes removed.

    SAME SCOPING ARGUMENT AS `test_public_surface`'s rule-line grep. The
    branding section now explains why the old branding check was retired, and
    that explanation necessarily quotes the URL it retired. A guard over the
    whole section would be red on its own rationale and would then be loosened
    until it measured nothing, so it reads what the section TELLS THE OPERATOR
    TO DO -- the lines that are not commentary.
    """
    return "\n".join(line for line in section.splitlines() if not line.lstrip().startswith(">"))


def test_the_blockquote_filter_is_not_swallowing_the_section() -> None:
    """Non-vacuity for the guard below, and it has teeth: the sentence being
    checked for is IN a blockquote, so a filter that dropped everything would
    make the assertion below pass on an empty string."""
    section = _readme_section(BRANDING_HEADING)
    instructions = _instructions(section)
    assert "/realms/it-suite/account" in section, (
        "the section must still explain what happened to the old check"
    )
    # THE SENTINEL MOVED, AND WHY IS THE POINT. It used to be "Realm settings",
    # from the admin-console breadcrumb that told a vendor to select the login
    # theme by hand. `keycloak-init` converges `loginTheme` from the realm file
    # as of 2026-08-20, so that breadcrumb is gone from the section and a test
    # still demanding it would be demanding a step nobody performs. The two
    # probes below are the instructions that are STILL manual, and each is
    # manual for a reason the section states.
    assert "Authentication → Flows → browser → Duplicate" in instructions, (
        "the passkey sign-in flow is still bound by hand -- deliberately, since "
        "a wrong flow graph locks everyone out -- so its instruction must remain"
    )
    assert "Identity providers → OpenID Connect v1.0" in instructions, (
        "the Entra provider is not converged (its secret cannot live in the "
        "realm file), so its instruction must remain too"
    )
    assert len(instructions) > 500


def test_the_branding_check_does_not_send_the_operator_to_the_account_console() -> None:
    """IT USED TO, AND THAT INSTRUCTION IS NOW FALSE.

    "Check it in a private window at `/realms/it-suite/account`" was how the
    runbook told a vendor to confirm the login theme had loaded. With the
    account-console client disabled that URL cannot render a themed page at
    all, so following the step produces a failure that looks exactly like the
    missing theme mount it was written to detect -- and the fix the reader would
    then reach for is remounting a theme that was never the problem.
    """
    instructions = _instructions(_readme_section(BRANDING_HEADING))
    assert "/realms/it-suite/account" not in instructions, (
        "the branding check still points at the account console, which is "
        "disabled at the realm and cannot show whether the theme loaded"
    )
    assert "sign-in page" in instructions, (
        "the check has to name the page the login theme actually styles"
    )


def test_the_runbook_states_the_account_console_is_closed_at_the_realm() -> None:
    """A CLOSED DOOR NOBODY DOCUMENTED IS A BUG REPORT WAITING TO HAPPEN.

    A vendor who opens `/realms/it-suite/account` and is refused needs to find,
    in one place, that this is deliberate, which switch does it, why the switch
    is not the routing rule, and how to reverse it for break-glass. Otherwise
    the first response to a refused page is to "fix" it.
    """
    section = _readme_section(BREAK_GLASS_HEADING).lower()
    assert "account-console" in section
    assert "disabled" in section
    assert "realm-it-suite.json" in section, "name the file that holds the switch"
    assert "/realms/it-suite/account" in section, (
        "name the URL, so somebody searching for the page they were refused finds this section"
    )
    assert "duplicate client id" in section, (
        "and the trap: the `account` client is declared alongside it because "
        "declaring one alone makes the realm unimportable"
    )


# --- sign-out, which is a two-file agreement (2026-08-20) ------------------


def _web_client() -> dict[str, Any]:
    return next(c for c in REALM["clients"] if c["clientId"] == "it-suite-web")


def test_the_post_logout_uri_the_browser_asks_for_is_one_the_realm_accepts() -> None:
    """AUDITOR A'S FIRST BLOCKER, AS A TEST ACROSS THE TWO FILES THAT DISAGREED.

    Signing out dead-ended on Keycloak's "Invalid redirect uri" -- a full-screen
    error page with, in the auditor's words, no link, no button and no
    navigation of any kind, reached by one click on the profile chip. The user's
    only way back was retyping the URL.

    It is a two-file defect and neither file is wrong alone. `lib/auth.ts` asks
    to be returned to a URI; the realm decides whether that URI is registered.
    `post.logout.redirect.uris: "+"` is Keycloak's shorthand for "the same set
    as `redirectUris`", and that set holds callback paths only -- so an app
    asking to return to its own front door was refused by a value that LOOKS
    permissive and, for this one purpose, permits nothing.

    Correcting only the realm's `redirectUris` -- which is what the realm
    convergence job does -- does NOT fix this: it changes what `+` expands to
    from one non-matching set into a different non-matching set. That is why
    this test compares the two files rather than checking either against a
    constant.
    """
    registered = _web_client()["attributes"]["post.logout.redirect.uris"]
    assert registered != "+", (
        "'+' resolves to the redirect URIs, which are callback paths -- it "
        "cannot match the origin the console asks to be returned to"
    )

    requested = re.search(r"post_logout_redirect_uri:\s*(.+?),\n", FRONTEND_AUTH.read_text())
    assert requested, f"{FRONTEND_AUTH} no longer sets post_logout_redirect_uri"

    # The browser builds it from `window.location.origin`; what has to agree
    # with the realm is the PATH it appends, since the origin is the same one
    # Keycloak is served from (the `kc` router, same host).
    path = re.sub(r"\$\{[^}]*window\.location\.origin[^}]*\}", "", requested.group(1))
    path = path.strip("`\"' ")
    assert path in registered.split("##"), (
        f"the console asks to return to {path!r} and the realm registers "
        f"{registered!r}; Keycloak refuses every sign-out this way"
    )


def test_the_post_logout_uri_is_a_path_and_not_a_hostname() -> None:
    """Same argument as the login redirect's. A hostname baked into the realm
    file is wrong for every deployment except the one it was written for, and
    a wildcard in the host position matches NOTHING -- Keycloak honours `*`
    only as a trailing segment, which is the defect that produced the
    `https://*/auth/callback` this file used to ship."""
    registered = _web_client()["attributes"]["post.logout.redirect.uris"]

    for uri in registered.split("##"):
        assert uri.startswith("/"), f"{uri!r} is not relative to the deployment"
        assert "*" not in uri, f"{uri!r} carries a wildcard Keycloak will not honour"


# --- the login theme covers the flow, not one page of it (2026-08-20) ------

THEME = REPO / "deploy/keycloak/themes/itms/login"

#: Every page a user of THIS product reaches, and the template that styles it.
#: `parent=base` supplies templates and no styling whatsoever, so a page absent
#: from this directory renders as bare HTML -- which is what the operator
#: photographed on 2026-08-20: the authenticator-code step, in a serif system
#: font, on a grey background, one step into signing in to the product.
THEMED_PAGES = {
    "login.ftl": "the credential form",
    "login-otp.ftl": "the authenticator code, on every password sign-in",
    "login-config-totp.ftl": "enrolling an authenticator (Manage MFA device)",
    "login-update-password.ftl": "Change password",
    "error.ftl": "the terminal failure page auditor A found had no way back",
    "info.ftl": "the 'that worked' page after a credential action",
    "login-page-expired.ftl": "a sign-in left half-finished",
    "select-authenticator.ftl": "choosing a passkey over the password form",
    "webauthn-register.ftl": "creating a passkey, which every new account meets",
    "webauthn-error.ftl": "where a refused passkey step lands, with Try again",
}

#: DELIBERATELY NOT THEMED: Keycloak's WebAuthn templates carry live JavaScript
#: that calls `navigator.credentials` with values the server injects. Copying
#: that into a theme means owning it, and getting it subtly wrong breaks
#: passkey enrolment. The rule this list was written with: a page stays on base
#: "until there is a way to exercise [it] against a real Keycloak".
#:
#: `webauthn-register.ftl` LEFT THIS LIST ON 2026-09-22, and only on that
#: condition. Its bare base rendering had left a user stranded when Windows
#: could not reach Microsoft Password Manager, so it was themed -- and then run
#: against a real Keycloak 26.2.4 with this realm file, registering a passkey
#: through a Chromium virtual authenticator, confirming Keycloak stored it and
#: handed the authorization code back, and separately making the authenticator
#: refuse to exercise `webauthn-error.ftl` and its Try again. 19 of 19 checks.
#: That run is also what found the realm's 273-character client description
#: and the swallowed apostrophes; see the tests below.
#:
#: `webauthn-authenticate.ftl` -- signing IN with a passkey -- was not
#: exercised by that run and so stays here. Theming it is the same job, done
#: the same way, when someone does it.
UNTHEMED_BY_DESIGN = {
    "webauthn-authenticate.ftl",
}


def test_every_page_the_flow_reaches_has_a_template() -> None:
    missing = [name for name in THEMED_PAGES if not (THEME / name).exists()]
    assert missing == [], (
        f"{missing} render through `parent=base`, which supplies no styling at "
        "all -- an unbranded page in the middle of signing in to the product"
    )


def test_the_unthemed_pages_are_still_unthemed_and_say_why() -> None:
    """Non-vacuity for the list above, and a tripwire in the other direction.

    If somebody adds a WebAuthn template later they should have to delete their
    name from `UNTHEMED_BY_DESIGN` and, in doing so, read why they were left
    alone. A silent addition is the failure this catches."""
    present = [name for name in UNTHEMED_BY_DESIGN if (THEME / name).exists()]
    assert present == [], (
        f"{present} now exist; remove them from UNTHEMED_BY_DESIGN and make sure "
        "passkey enrolment was exercised against a real Keycloak first"
    )


def _directives(source: str) -> list[tuple[str, str]]:
    """FreeMarker block directives in order: ("open"|"close", name)."""
    found: list[tuple[str, str]] = []
    # `</#if>`, not `<#/if>`: the slash goes before the hash. The first
    # version of this regex looked for the latter, matched no closing
    # directive at all, and therefore reported every template as a stack of
    # unclosed blocks -- a test that fails for the wrong reason is only one
    # edit away from being "fixed" into one that passes for the wrong reason.
    for match in re.finditer(r"<(/?)#(if|list|items|macro|switch|attempt)\b", source):
        found.append(("close" if match.group(1) else "open", match.group(2)))
    return found


@pytest.mark.parametrize("name", sorted(THEMED_PAGES))
def test_the_template_is_structurally_balanced(name: str) -> None:
    """WHAT THIS CATCHES AND WHAT IT CANNOT.

    A FreeMarker error in a login template is not a cosmetic defect: Keycloak
    answers 500 and NOBODY CAN SIGN IN, including the person who would fix it.
    On a deployment with one administrator that is a lockout. These templates
    were written without a Keycloak to render them against, so the risk is
    real and this is the cheapest guard that exists offline.

    It checks that every block directive closes, in order, and that the layout
    macro is opened and closed. It does NOT prove the template renders: a
    variable that does not exist in a given Keycloak version, a typo in a
    `${...}` expression, or a message key that is absent all pass this and fail
    in the browser. `deploy/README.md` carries the manual check and the
    one-command rollback for exactly that reason.
    """
    source = (THEME / name).read_text()
    stack: list[str] = []
    for kind, directive in _directives(source):
        if directive == "items":  # <#items> is self-contained inside <#list>
            continue
        if kind == "open":
            stack.append(directive)
        else:
            assert stack, f"{name}: </#{directive}> with nothing open"
            opened = stack.pop()
            assert opened == directive, f"{name}: </#{directive}> closes <#{opened}>"
    assert stack == [], f"{name}: unclosed {stack}"

    assert source.count("<@layout.registrationLayout") == 1, name
    assert source.count("</@layout.registrationLayout>") == 1, name


@pytest.mark.parametrize("name", sorted(THEMED_PAGES))
def test_the_template_only_uses_classes_the_stylesheet_defines(name: str) -> None:
    """An `itms-` class with no rule behind it is an unstyled element that
    LOOKS deliberate in the markup -- the failure mode is invisible in review
    and obvious on the page."""
    stylesheet = (THEME / "resources/css/itms.css").read_text()
    used = set(re.findall(r'class="([^"]*)"', (THEME / name).read_text()))
    classes = {c for group in used for c in group.split() if c.startswith("itms-")}

    undefined = sorted(c for c in classes if f".{c}" not in stylesheet)

    assert undefined == [], f"{name} uses {undefined}, which itms.css does not define"


def test_the_provider_divider_is_not_drawn_over_an_empty_list() -> None:
    """A divider reading "or sign in with", with nothing under it.

    `social.providers??` asks whether the variable EXISTS. Keycloak sets it to
    an empty list when the realm has no identity provider the flow can offer,
    so the guard was true and the page drew a separator introducing nothing --
    visible in the operator's screenshot of the themed login page. `?has_content`
    is the question that was meant: is there anything in it.
    """
    source = (THEME / "login.ftl").read_text()

    assert "social.providers?has_content" in source, (
        "guard the provider block on the list having content, not on the "
        "variable existing -- Keycloak sets it to [] rather than leaving it unset"
    )


def test_the_realm_names_itself_so_the_page_does_not_say_it_twice() -> None:
    """The operator's screenshot showed TWO headings: "it-suite" above "ITMS".

    Keycloak's base template prints the realm's display name in
    `#kc-header-wrapper`, and the theme's `header` section in `#kc-page-title`.
    With no display name set, the first fell back to the realm's ID -- an
    internal identifier, `it-suite`, printed above the product's own name on
    the first screen a buyer sees.

    Setting it means the brand appears once, from the realm, and each page's
    `header` section names the STEP instead of repeating the brand.
    """
    assert REALM.get("displayName") == "ITMS"
    assert REALM.get("displayNameHtml") == "ITMS"

    from tests.test_keycloak_init_job import _load_script

    init = _load_script()

    # ...and it has to REACH a realm that already exists, which is every
    # deployment past its first boot.
    assert "displayName" in init.CONVERGED_REALM_KEYS
    assert "displayNameHtml" in init.CONVERGED_REALM_KEYS


@pytest.mark.parametrize("name", sorted(THEMED_PAGES))
def test_each_page_names_its_step_rather_than_the_brand(name: str) -> None:
    """`#kc-page-title` sits under the brand, so repeating "ITMS" there says
    the product's name twice and the user's position not at all. Each page
    answers "where am I in signing in" instead."""
    source = (THEME / name).read_text()
    header = source.split('<#if section = "header">', 1)[1].split("<#elseif", 1)[0]

    assert "ITMS" not in header, f"{name}: the brand comes from the realm's display name"
    assert header.strip(), f"{name}: the header section is empty"


def test_the_icons_are_one_set_rather_than_four_drawings() -> None:
    """Inline SVG, one grid, one stroke weight, `currentColor`.

    Three properties, each with a failure the eye catches before any test does
    but which nothing stops from being reintroduced:

    * INLINE, not a font or a sprite from a CDN. This theme ships into a
      Keycloak container with no build step, and a login page that fetches an
      icon font over the network is a login page that renders wrong on an
      air-gapped install -- which is a deployment this product explicitly
      supports.
    * ONE GRID AND ONE STROKE. Icons drawn on different viewBoxes or at
      different weights read as clip-art collected from several places, which
      is the single most reliable tell that a page was assembled rather than
      designed.
    * `currentColor`, so an icon inherits the state of the thing it sits in
      instead of being repainted at every call site.
    """
    for name in sorted(THEMED_PAGES):
        source = (THEME / name).read_text()
        for svg in re.findall(r"<svg\b[^>]*>", source):
            assert 'viewBox="0 0 24 24"' in svg, f"{name}: {svg[:60]} is on another grid"
            assert 'stroke-width="1.75"' in svg, f"{name}: {svg[:60]} is another weight"
            assert 'stroke="currentColor"' in svg, f"{name}: {svg[:60]} hardcodes its colour"
            # Decoration beside a label that already says the same thing.
            assert 'aria-hidden="true"' in svg, f"{name}: {svg[:60]} is announced twice"


#: Keys this theme invents, which therefore have to exist in its own bundle.
#: Anything else is Keycloak's and resolves from the parent theme.
THEME_MESSAGES = THEME / "messages/messages_en.properties"


def test_every_message_key_the_theme_invents_is_defined() -> None:
    """THE BUG THAT PUT `itmsAccessNotice` ON THE OPERATOR'S LOGIN PAGE.

    The templates were written as `${msg("itmsAccessNotice", "Authorized access
    only.")}` on the assumption that the second argument is a fallback. It is
    not: `msg` takes SUBSTITUTION PARAMETERS, and a key with no entry renders
    as the key itself. So a sentence meant to say what the system is displayed
    a camel-case identifier instead, on the first screen of the product.

    A key that is Keycloak's own resolves from the parent bundle and is not
    this theme's business. A key that is not -- anything starting `itms`, and
    the handful of others this theme coined -- must be in the file below or it
    will print itself. That distinction is what this checks.
    """
    defined = {
        line.split("=", 1)[0].strip()
        for line in THEME_MESSAGES.read_text().splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }
    assert "itmsAccessNotice" in defined, "the bundle is not being read as expected"

    coined = {
        "itmsAccessNotice",
        "itmsPasswordAccepted",
        "loginOtpHelp",
        "loginTotpStepNote",
        "updatePasswordMessage",
    }
    used: set[str] = set()
    for name in THEMED_PAGES:
        used |= set(re.findall(r'\$\{msg\("([A-Za-z0-9_.-]+)"', (THEME / name).read_text()))

    # The docstring's own rule, which the code used to narrow to the five keys
    # above: EVERY `itms*` key is this theme's, so a new one must be defined.
    # A hardcoded list cannot notice a key nobody added to it -- the seven
    # `itmsPasskey*` keys of 2026-09-22 would have printed themselves unseen.
    coined |= {key for key in used if key.startswith("itms")}
    missing = sorted((used & coined) - defined)
    assert missing == [], f"{missing} render as their own key name"


THEME_MESSAGES_ES = THEME / "messages/messages_es.properties"


def _bundle(path: Path) -> dict[str, str]:
    """A properties file as `key -> raw value`, comments and blanks dropped."""
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value
    return out


def test_every_english_message_has_a_spanish_one() -> None:
    """Keycloak falls back to English for a key a locale lacks, so a missing
    line is not an error anywhere: it is one English sentence in the middle of
    a Spanish sign-in page. The two bundles must name the same keys."""
    en, es = _bundle(THEME_MESSAGES), _bundle(THEME_MESSAGES_ES)
    assert "itmsAccessNotice" in en, "the English bundle is not being read as expected"
    assert sorted(set(en) - set(es)) == [], "in English, not in Spanish"
    assert sorted(set(es) - set(en)) == [], "in Spanish, not in English"


def test_the_spanish_messages_keep_every_placeholder_and_tag() -> None:
    """`{0}` is a MessageFormat argument and `<strong>` is markup the template
    renders unescaped. A translation that drops either breaks the page, not
    the sentence."""
    en, es = _bundle(THEME_MESSAGES), _bundle(THEME_MESSAGES_ES)
    for key, english in en.items():
        assert re.findall(r"\{\d+[^}]*\}", es[key]) == re.findall(r"\{\d+[^}]*\}", english), key
        assert re.findall(r"</?[A-Za-z][^>]*>", es[key]) == re.findall(
            r"</?[A-Za-z][^>]*>", english
        ), key


def test_a_spanish_apostrophe_is_written_twice() -> None:
    """Every message goes through Java's MessageFormat, where a lone `'` opens
    a quoted literal and is swallowed (the 2026-09-22 "cant"). Spanish rarely
    needs one, but the rule is the file's, not the language's."""
    for key, value in _bundle(THEME_MESSAGES_ES).items():
        assert "'" not in value.replace("''", ""), f"{key}: a single apostrophe"


def test_the_locale_menu_is_closed_until_its_script_opens_it() -> None:
    """Base ships no stylesheet for the dropdown the realm's two languages turn
    on. Its script opens the menu with an inline `display: block`, so the
    default has to be closed here or the list of languages is open on every
    sign-in, under a button that looks like a browser form control."""
    bodies = [body for selector, body in _rules() if selector == "#kc-locale ul"]
    assert bodies, "no rule for the menu: it renders permanently open"
    assert any(re.search(r"display:\s*none", body) for body in bodies)


def test_the_spanish_bundle_is_utf_8_that_keycloak_will_read_as_utf_8() -> None:
    """HOW KEYCLOAK READS THIS FILE. 26.x loads theme bundles through
    `PropertyResourceBundle`, which tries UTF-8 and falls back to ISO-8859-1
    ONLY IF THE BYTES ARE NOT VALID UTF-8. So a file saved as Latin-1 would be
    read as Latin-1 and render correctly, and a file that is UTF-8 but carries
    a byte-order mark would be read as UTF-8 with U+FEFF glued to the front of
    its first key, which then matches nothing -- silently, on one line. The
    failures to catch are therefore: not UTF-8, a BOM, a replacement
    character, and text that was decoded wrongly and re-saved (mojibake).
    """
    raw = THEME_MESSAGES_ES.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "a BOM becomes part of the first key"
    text = raw.decode("utf-8")  # strict: raises on Latin-1 bytes
    assert "\ufffd" not in text, "a replacement character: bytes were lost"
    assert not re.search(r"[\u00c2\u00c3][\u0080-\u00bf]", text), "mojibake: UTF-8 read as Latin-1"
    assert re.search(r"[áéíóúñÁÉÍÓÚÑ¿¡]", text), "no accented text: is this the Spanish file?"
    assert "\\u" not in text, "write the characters, not escapes: one encoding, one rule"


@pytest.mark.parametrize("name", sorted(THEMED_PAGES))
def test_no_template_passes_prose_where_msg_takes_a_parameter(name: str) -> None:
    """The shape of the mistake, not just its instances.

    `msg("someKey", "Some sentence.")` reads exactly like a default and is a
    positional substitution parameter. Every occurrence of a quoted SENTENCE in
    that position was a key that printed itself. Substitution with a real value
    -- `msg("backTo", client.name)` -- is legitimate and unaffected, because
    the argument is an expression rather than a string literal containing
    spaces.
    """
    source = (THEME / name).read_text()

    prose = [
        call
        for call in re.findall(r'msg\("[A-Za-z0-9_.-]+",\s*"[^"]*"\)', source)
        if " " in call.split(",", 1)[1]
    ]

    assert prose == [], f"{name}: {prose} -- the second argument is not a default"


def test_the_theme_renders_one_try_another_way_control() -> None:
    """It rendered TWO: a styled button of this theme's own, and below it
    base's bare link, because Keycloak's parent template already emits the
    control whenever the flow has another execution to offer.

    Styling what the parent renders is the right side of that trade. It removes
    the duplicate, and it means no template here has to stay in step with
    Keycloak's own judgement about when the control applies."""
    for name in THEMED_PAGES:
        source = (THEME / name).read_text()
        assert "showTryAnotherWayLink" not in source, (
            f"{name} renders its own try-another-way; base already renders one"
        )

    assert "#try-another-way" in (THEME / "resources/css/itms.css").read_text(), (
        "base's control is the one that ships, so it has to be styled"
    )


def test_the_theme_is_not_cached_into_a_month_long_stale_window() -> None:
    """WHY A CORRECT DEPLOY LOOKED BROKEN, TWICE.

    Keycloak serves theme resources from `/resources/<keycloak-build>/login/...`
    -- a path that changes when KEYCLOAK is upgraded, never when the theme is
    -- and in production mode caches them for `static-max-age`, which defaults
    to 2592000 seconds. Thirty days.

    So every browser that has loaded the sign-in page holds that stylesheet for
    a month. Ship a theme change and returning users get the NEW HTML against
    the OLD CSS: icons outside the fields they belong in, help text at body
    size, a bare link where a button was drawn. The operator photographed
    exactly that, twice, after deploys that were correct -- and a customer
    would see it for a month after any theme fix, with nothing in the product
    or the runbook to explain it.

    This is the deployment's problem rather than the theme's, which is why the
    guard is here rather than in a stylesheet.
    """
    compose = COMPOSE.read_text()
    keycloak = compose[compose.index("\n  keycloak:") : compose.index("\n  keycloak-init:")]

    assert "KC_SPI_THEME_STATIC_MAX_AGE: -1" in keycloak, (
        "theme resources are served from a URL that does not change when the "
        "theme does, so any max-age is a stale-login-page window"
    )
    assert 'KC_SPI_THEME_CACHE_THEMES: "false"' in keycloak, (
        "the theme is a bind mount; a restart should not be what makes an edit visible"
    )


# ---------------------------------------------------------------------------
# The TOTP enrolment page
# ---------------------------------------------------------------------------


def _totp_template() -> str:
    return _FTL_COMMENT.sub("", (THEME / "login-config-totp.ftl").read_text())


def test_totp_secret_is_posted_on_every_path_not_only_the_manual_one() -> None:
    """The QR path must submit the secret too, or enrolment 500s.

    THIS IS A REGRESSION TEST FOR AN OUTAGE, not a style rule. The hidden
    `totpSecret` field sat inside the template's `mode = "manual"` branch, so
    it was posted only by someone who clicked "Unable to scan?". Everyone who
    scanned the QR code -- the default, and what almost every person does --
    posted a code with no secret beside it, and Keycloak's
    `UpdateTotp.validateOTPCredential` threw

        NullPointerException: ... because the return value of
        OTPSecretData.getValue() is null

    which reaches the operator as "An internal server error has occurred" with
    nothing naming the cause. Stock Keycloak's own template renders this field
    unconditionally; the theme's copy is what narrowed it.
    """
    ftl = _totp_template()

    assert 'name="totpSecret"' in ftl, (
        "the TOTP page posts no secret at all -- Keycloak has nothing to "
        "validate the submitted code against"
    )

    guarded = re.search(
        r'<#if mode\?\? && mode = "manual">\s*<input[^>]*name="totpSecret"',
        ftl,
    )
    assert guarded is None, (
        "the TOTP secret is submitted only in manual mode, so scanning the QR "
        "code posts nothing and UpdateTotp NPEs on a null OTP secret"
    )


# ---------------------------------------------------------------------------
# Column widths: the crash-loop guard nobody had
# ---------------------------------------------------------------------------

#: Keycloak's schema stores these client fields in VARCHAR(255) columns
#: (CLIENT.CLIENT_ID, NAME, DESCRIPTION, ROOT_URL, BASE_URL, MANAGEMENT_URL).
#: A longer value is not truncated -- the import transaction fails and the
#: server refuses to start.
_CLIENT_VARCHAR_255 = ("clientId", "name", "description", "rootUrl", "baseUrl", "adminUrl")


def _overlong_client_fields(realm: dict[str, Any]) -> list[tuple[str, str, int]]:
    return [
        (client.get("clientId", "?"), field, len(value))
        for client in realm.get("clients", [])
        for field in _CLIENT_VARCHAR_255
        if isinstance(value := client.get(field), str) and len(value) > 255
    ]


def test_the_column_width_predicate_can_actually_fail() -> None:
    """Falsification: the committed realm passes, so it proves nothing alone."""
    assert _overlong_client_fields({"clients": [{"clientId": "x", "description": "d" * 256}]})
    assert not _overlong_client_fields({"clients": [{"clientId": "x", "description": "d" * 255}]})


def test_no_client_field_is_wider_than_its_column() -> None:
    """A CRASH-LOOP GUARD, the same failure class as
    `test_declaring_one_account_client_without_the_other_breaks_the_import`:
    the file parses, every other assertion here is green, and Keycloak will not
    start.

    FOUND BY RUNNING IT, not by reading it. Booting a real Keycloak 26.2.4 with
    this realm on 2026-09-22 died at import with

        Value too long for column "DESCRIPTION CHARACTER VARYING(255)":
        "'Declared here, and left ENABLED, because the admin API resolves this
        client as ... (273)"

    `account` carried 273 characters and `account-console` 431, both added on
    2026-08-20. No running deployment noticed, because `--import-realm` skips a
    realm that already exists -- so the one host in service had imported it
    before that date and never read the long text. A NEW install would have
    crashed on first boot, and so would a disaster-recovery rebuild onto an
    empty database, which is the moment nobody wants a second surprise.

    The same mechanism is why every realm change since a host's first import
    has never reached that host: see deploy/README.md on realm drift.
    """
    assert _overlong_client_fields(REALM) == []


# ---------------------------------------------------------------------------
# Passkeys: what the real-Keycloak run proved, pinned so an edit cannot undo it
# ---------------------------------------------------------------------------
#
# webauthn-register.ftl and webauthn-error.ftl were exercised end to end on
# 2026-09-22 against Keycloak 26.2.4: registration succeeded through a virtual
# authenticator, and a refused authenticator reached the error page and Try
# again. That run cannot happen in CI. These pin the contract it depended on.

#: Filled by id by base/login/resources/js/webauthnRegister.js (26.2.4), then
#: posted. A field this page drops is a field the server never receives.
_WEBAUTHN_REGISTER_FIELDS = (
    "clientDataJSON", "attestationObject", "publicKeyCredentialId",
    "authenticatorLabel", "transports", "error",
)
#: Read by registerByWebAuthn(input) in the same script.
_WEBAUTHN_REGISTER_INPUTS = (
    "challenge", "userid", "username", "signatureAlgorithms", "rpEntityName",
    "rpId", "attestationConveyancePreference", "authenticatorAttachment",
    "requireResidentKey", "userVerificationRequirement", "createTimeout",
    "excludeCredentialIds", "initLabel", "initLabelPrompt", "errmsg",
)


def _ftl(name: str) -> str:
    return _FTL_COMMENT.sub("", (THEME / name).read_text())


def _register_form_body() -> str:
    ftl = _ftl("webauthn-register.ftl")
    body = re.search(r'<form id="register"[^>]*>(.*?)</form>', ftl, re.DOTALL)
    assert body is not None, "webauthn-register.ftl has no form#register for the script to post"
    return body.group(1)


def test_the_passkey_page_posts_every_field_the_stock_script_fills() -> None:
    """The login-config-totp.ftl lesson, applied before it could recur: that
    template scoped one hidden field to one branch and every QR enrolment ended
    in a server NullPointerException."""
    body = _register_form_body()
    for field in _WEBAUTHN_REGISTER_FIELDS:
        assert f'id="{field}"' in body and f'name="{field}"' in body, (
            f"form#register has no `{field}`; webauthnRegister.js writes it by "
            "id and the server reads it by name"
        )
    assert "<#if" not in body, (
        "a hidden field inside form#register is conditional -- the exact shape "
        "of the TOTP outage"
    )
    assert "logoutOtherSessions" in body, "the 'Sign out from other devices' option is gone"


def test_the_create_button_cannot_post_the_form_before_the_browser_answers() -> None:
    """The script submits form#register itself, after
    navigator.credentials.create() settles. A submit button INSIDE that form
    posts it empty the instant it is clicked."""
    ftl = _ftl("webauthn-register.ftl")
    assert 'id="registerWebAuthn"' not in _register_form_body(), (
        "the Create button is inside form#register and will post it empty"
    )
    button = re.search(r'<button[^>]*id="registerWebAuthn"[^>]*>', ftl)
    assert button is not None, "no #registerWebAuthn for the script to attach to"
    assert 'type="button"' in button.group(0), button.group(0)


def test_the_passkey_page_hands_the_script_every_input_it_reads() -> None:
    ftl = _ftl("webauthn-register.ftl")
    assert "/js/webauthnRegister.js" in ftl and "registerByWebAuthn(input)" in ftl
    missing = [key for key in _WEBAUTHN_REGISTER_INPUTS if not re.search(rf"\b{key}\s*:", ftl)]
    assert missing == [], f"registerByWebAuthn would receive no {missing}"


def test_try_again_on_the_passkey_error_page_restarts_the_step() -> None:
    """refreshPage fills both hidden fields and posts the form; without all
    four pieces "Try again" reloads a dead page instead of retrying."""
    ftl = _ftl("webauthn-error.ftl")
    form = re.search(r'<form id="kc-error-credential-form"[^>]*>(.*?)</form>', ftl, re.DOTALL)
    assert form is not None, "no kc-error-credential-form to post the retry through"
    assert 'name="authenticationExecution"' in form.group(1)
    assert 'name="isSetRetry"' in form.group(1)
    assert "${execution}" in ftl and "requestSubmit()" in ftl
    assert re.search(r'id="kc-try-again"[^>]*onclick="refreshPage\(\)"', ftl), (
        "Try again is not wired to refreshPage"
    )


def _unpaired_apostrophes(value: str) -> bool:
    return "'" in value.replace("''", "")


def test_the_apostrophe_predicate_can_actually_fail() -> None:
    assert _unpaired_apostrophes("can't")
    assert not _unpaired_apostrophes("can''t")
    assert not _unpaired_apostrophes("no apostrophe")


def test_no_message_loses_its_apostrophe() -> None:
    """Keycloak renders every message through Java's MessageFormat, where a
    single ' opens a quoted literal and is swallowed. FOUND BY RENDERING, not
    reading: on 2026-09-22 the passkey page showed "cant" and "couldnt".
    '' renders as one apostrophe; Keycloak's own bundle writes "Don''t"."""
    bad = [
        line.split("=", 1)[0].strip()
        for line in THEME_MESSAGES.read_text().splitlines()
        if "=" in line
        and not line.lstrip().startswith("#")
        and _unpaired_apostrophes(line.split("=", 1)[1])
    ]
    assert bad == [], f"{bad} will render with the apostrophe missing; write it as ''"
