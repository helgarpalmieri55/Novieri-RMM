import pytest
from pydantic import ValidationError

from app.modules.patch.schema import PatchApplyParams, PatchApplyResult, PatchScanDocument


def test_scan_document_validates_and_rejects_bad_severity() -> None:
    doc = PatchScanDocument(os_family="windows", scanned_at="2026-07-19T00:00:00Z",
        patches=[{"source": "windows_update", "identifier": "KB1", "title": "x",
                  "severity": "critical"}])
    assert doc.patches[0].severity == "critical"
    with pytest.raises(ValidationError):
        PatchScanDocument(os_family="windows", scanned_at="t", patches=[
            {"source": "s", "identifier": "i", "title": "t", "severity": "nope"}])


def test_apply_result_and_params() -> None:
    r = PatchApplyResult(applied_at="t", reboot_required=True, results=[
        {"source": "apt", "identifier": "openssl", "state": "installed"}])
    assert r.reboot_required is True
    p = PatchApplyParams(patches=[{"source": "apt", "identifier": "openssl"}], reboot="if_required")
    assert p.deferral_hours == 24
