import subprocess
from unittest.mock import patch

import core.image_audit as image_audit
from core.volcengine_ocr import run_ocr_on_images_with_volcengine


def test_run_ocr_on_images_timeout_is_skipped(monkeypatch):
    monkeypatch.setenv("OCR_PROVIDER", "paddle")
    monkeypatch.setenv("OCR_TIMEOUT_SECONDS", "5")

    with patch.object(image_audit.os.path, "exists", return_value=True), \
         patch.object(image_audit.subprocess, "run", side_effect=subprocess.TimeoutExpired(cmd=["ocr"], timeout=5)):
        result = image_audit.run_ocr_on_images(["/tmp/image_1.jpg", "/tmp/image_2.jpg"])

    assert result["available"] is False
    assert "skip_reason" in result
    assert "timed out" in result["skip_reason"]
    assert result["merged_text"] == ""


def test_run_ocr_on_images_uses_volcengine_provider(monkeypatch):
    monkeypatch.setenv("OCR_PROVIDER", "volcengine")

    expected = {
        "texts": [{"image_path": "/tmp/image_1.jpg", "text": "hello", "error": ""}],
        "merged_text": "hello",
        "errors": [],
        "available": True,
        "skip_reason": "",
    }

    with patch.object(image_audit, "run_ocr_on_images_with_volcengine", return_value=expected) as mock_run:
        result = image_audit.run_ocr_on_images(["/tmp/image_1.jpg"])

    mock_run.assert_called_once()
    assert result == expected


def test_volcengine_ocr_permission_denied_returns_unavailable(monkeypatch):
    monkeypatch.setenv("VOLCENGINE_ACCESS_KEY", "ak")
    monkeypatch.setenv("VOLCENGINE_SECRET_KEY", "sk")

    class FakeService:
        def post(self, *_args, **_kwargs):
            return '{"code":50400,"message":"Access Denied"}'

    with patch("core.volcengine_ocr._build_service", return_value=(FakeService(), {
        "mode": "text_block",
        "filter_thresh": "80",
        "approximate_pixel": "0",
    })), patch("core.volcengine_ocr._encode_image_base64", return_value="abc"):
        result = run_ocr_on_images_with_volcengine(["/tmp/image_1.jpg"])

    assert result["available"] is False
    assert "50400" in result["skip_reason"]
