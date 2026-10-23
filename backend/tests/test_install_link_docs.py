"""The two recorded rules the install link overturns say so where they live
(install-link spec 2026-09-28 §0 items 1 and 2, §6 item 1).

A rule that was overturned in a spec and left standing in the document an
engineer actually reads is a rule the next engineer "restores" -- by removing
the install link."""

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def test_the_packaging_readme_records_the_exception_and_its_bounds() -> None:
    text = (REPO / "agent" / "packaging" / "README.md").read_text()
    assert "## The install link: a code in a file name (operator decision, 2026-09-28)" in text
    for fragment in (
        "ITMS-Agent-Setup_<host>[@<port>]_<code>.exe",
        "--elevated-install",
        "object-store access log",
        "Waiting for review",
        "argv[0]",
        "Referrer-Policy: origin",
    ):
        assert fragment in text, fragment


def test_the_packaging_readme_describes_the_file_name_and_not_the_mark_of_the_web() -> None:
    """D6a: the console host rides in the file name, so a copy from a share or a
    USB stick installs too. The Mark of the Web is not read."""
    text = (REPO / "agent" / "packaging" / "README.md").read_text()
    assert "Mark of the Web" in text  # named, to say it is not the source
    assert "not read" in text
    assert "ITMS-Agent-Setup_<code>.exe" not in text
    assert "refuses to install and says to download it again" not in text


def test_the_distribution_spec_amends_section_2_and_section_7() -> None:
    text = (REPO / "docs" / "modules" / "agent-distribution" / "spec.md").read_text()
    assert "**Amended 2026-09-28 (operator's decision" in text
    assert "except the install link's page" in text
    assert "ITMS-Agent-Setup_<host>[@<port>]_<code>.exe" in text
    assert "ITMS-Agent-Setup_<code>.exe" not in text


MSI_SECTION = (
    "## The Group Policy MSI: the code inside a file on SYSVOL (operator decision, 2026-09-30)"
)


def test_the_packaging_readme_records_the_msi_and_its_residuals() -> None:
    """GPO MSI spec 2026-09-30 §2.6: the three residuals go where the rule the
    MSI overturns lives -- the token table's "distributable artifact" row."""
    text = (REPO / "agent" / "packaging" / "README.md").read_text()
    assert MSI_SECTION in text
    for fragment in (
        "Anyone who can read SYSVOL can take the code",
        "Regenerate",
        "The share's permissions are the only integrity control",
        "The code sits on disk briefly",
        "msi-enroll.json",
        "--msi-install",
        "--msi-uninstall",
        "Its Group Policy MSI is the second",
    ):
        assert fragment in text, fragment
    assert "### No MSI" not in text


def test_the_distribution_spec_amends_section_2_and_section_7_for_the_msi() -> None:
    text = (REPO / "docs" / "modules" / "agent-distribution" / "spec.md").read_text()
    assert "**Amended 2026-09-30 (operator's decision, GPO MSI spec Q3).**" in text
    assert "msi-enroll.json" in text
    assert "- **No MSI, no `.pkg`, no signed Windows binary.**" not in text


def test_the_windows_guide_says_how_to_assign_the_msi() -> None:
    text = (REPO / "deploy" / "WINDOWS-PUSH.md").read_text()
    assert "## 7. The Group Policy MSI" in text
    assert "Assigned" in text and "Approve all" in text


def test_the_windows_guide_says_where_to_look_when_a_gpo_install_fails() -> None:
    text = (REPO / "deploy" / "WINDOWS-PUSH.md").read_text()
    section = text[text.index("## 7. The Group Policy MSI") :]
    assert "C:\\ProgramData\\ITSuite\\msi.log" in section
    assert "It never holds the link code" in section


def test_the_residuals_say_who_can_read_the_settings_and_what_a_failed_install_leaves() -> None:
    """Review items 10 and 13, in the README and in spec §2.6 alike."""
    readme = (REPO / "agent" / "packaging" / "README.md").read_text()
    spec = (
        REPO / "docs" / "superpowers" / "specs" / "2026-09-30-gpo-msi-and-subnet-push-design.md"
    ).read_text()
    residuals = spec[spec.index("### 2.6 Residuals") : spec.index("## Testing")]
    for raw in (readme, residuals):
        text = " ".join(raw.split())  # line wrapping is not content
        assert "administrators-only" not in text and "is admin-only, and" not in text
        assert "only by administrators" in text
        assert "nothing beyond SYSVOL" in text
        assert "A failed `--msi-install` is not rolled back by the MSI" in text
        assert "msi.log" in text
